"""Ponte entre os ganchos do Claude Code (jarvis_hook.py) e o Jarvis: troca a leitura da tela por eventos.

Cada sessão do Claude Code (app Claude desktop, terminal ou Orca) avisa quando começa, trabalha, termina,
pede atenção ou pede permissão. A HookBridge guarda o estado por sessão e chama os callbacks do Jarvis.

Uso no jarvis.py:
    bridge = HookBridge(on_change=..., on_finished=..., on_permission=..., on_permission_done=..., orca=orca)
    web_server.HUB.rpc["hook"] = bridge.handle

Instalação dos ganchos em ~/.claude/settings.json (mescla, com backup datado):
    py -3.12 hooks_bridge.py --install      |   py -3.12 hooks_bridge.py --uninstall
"""
import datetime
import json
import os
import queue
import shutil
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_SCRIPT = os.path.join(HERE, "jarvis_hook.py")
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
MARK = "jarvis_hook.py"  # identifica as nossas entradas no settings.json

# Ferramentas que não dá para aprovar de fora: precisam de resposta no próprio app
NO_DECIDE = {"AskUserQuestion", "ExitPlanMode"}
WAIT_TYPES = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}


def _project_name(cwd):
    """Nome legível a partir da pasta: worktrees (.claude/worktrees/x, .orca/...) usam o projeto de cima"""
    p = (cwd or "").replace("/", "\\").rstrip("\\")
    low = p.lower()
    for mark in ("\\.claude\\worktrees\\", "\\.git\\worktrees\\"):
        i = low.find(mark)
        if i > 0:
            p = p[:i]
            break
    return os.path.basename(p) or p or "Claude"


def _summary(tool, inp):
    """Resumo curto do pedido de permissão: comando, arquivo, url…"""
    inp = inp if isinstance(inp, dict) else {}
    for k in ("command", "file_path", "notebook_path", "url", "path", "pattern", "query", "prompt", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            if k in ("file_path", "notebook_path", "path"):
                v = os.path.basename(v.rstrip("\\/")) or v
            return v[:160]
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())[:160]
    return tool or ""


def _last_assistant(transcript_path, max_bytes=400_000):
    """Última fala do assistente no transcript JSONL (só se o Stop não trouxe last_assistant_message)"""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return ""
    for line in reversed(lines):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        msg = j.get("message") if isinstance(j, dict) else None
        if j.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        texts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
        t = "\n".join(x for x in texts if x.strip()).strip()
        if t:
            return t
    return ""


def _short(text, n=400):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


class HookBridge:
    def __init__(self, on_change=None, on_finished=None, on_permission=None, on_permission_done=None,
                 on_attention=None, orca=None, min_run=20, stale_run=2 * 3600, forget_done=6 * 3600,
                 max_wait=600):
        """Callbacks (rodam numa thread própria, em ordem, sem travar o gancho):
        on_change(sessions_list)            lista para a ilha (formato de sessions())
        on_finished(session_dict, last_msg) tarefa terminou (só se trabalhou por min_run s ou mais)
        on_permission(info_dict)            {id, session, n, w, tool, text, t}: mostrar Negar/Permitir
        on_permission_done(id)              o pedido acabou (decidido, solto, expirou)
        on_attention(session_dict, msg)     opcional: Notification que pede o usuário
        orca: instância de orca_app.OrcaApp (None = cria quando precisar; False = não consulta o Orca)"""
        self.on_change, self.on_finished = on_change, on_finished
        self.on_permission, self.on_permission_done = on_permission, on_permission_done
        self.on_attention = on_attention
        self.min_run, self.stale_run, self.forget_done, self.max_wait = min_run, stale_run, forget_done, max_wait
        self._orca = orca
        self._orca_rows, self._orca_t, self._orca_busy = [], 0.0, False
        self.lock = threading.RLock()
        self.sess = {}  # session_id -> dict
        self.perms = {}  # id -> {info, event, result}
        self._q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name="hooks-callbacks").start()

    # ---------- callbacks numa thread só ----------
    def _emit(self, fn, *args):
        if fn:
            self._q.put((fn, args))

    def _worker(self):
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception:
                pass

    def _changed(self):
        self._emit(self.on_change, self.sessions())

    # ---------- Orca: cache dos worktrees, atualizado em segundo plano ----------
    def _orca_refresh(self):
        if self._orca is False or self._orca_busy or time.time() - self._orca_t < 30:
            return
        self._orca_busy = True

        def run():
            try:
                if self._orca is None:
                    import orca_app
                    self._orca = orca_app.OrcaApp()
                self._orca_rows = self._orca._worktrees(max_age=10)
                self._orca_t = time.time()
                with self.lock:
                    for s in self.sess.values():
                        self._apply_orca(s)
                self._changed()
            except Exception:
                self._orca_t = time.time()
            finally:
                self._orca_busy = False

        threading.Thread(target=run, daemon=True).start()

    def _apply_orca(self, s):
        wid, cwd = s.get("orca_worktree"), os.path.normcase(os.path.normpath(s.get("cwd") or "."))
        for w in self._orca_rows:
            path = w.get("path") or ""
            hit = (wid and w.get("id") == wid) or (path and (cwd == os.path.normcase(os.path.normpath(path))
                                                           or cwd.startswith(os.path.normcase(os.path.normpath(path)) + os.sep)))
            if hit:
                s["w"] = "Orca"
                s["n"] = w.get("name") or s["n"]
                return True
        return False

    # ---------- entrada: POST /rpc/hook ----------
    def handle(self, req):
        req = req if isinstance(req, dict) else {}
        event = req.get("event") or ""
        p = req.get("payload") if isinstance(req.get("payload"), dict) else {}
        env = req.get("env") if isinstance(req.get("env"), dict) else {}
        sid = p.get("session_id") or ""
        if not sid:
            return {}
        if event == "SessionEnd":
            with self.lock:
                gone = self.sess.pop(sid, None)
            if gone:
                self._changed()
            return {}
        s = self._touch(sid, p, env)
        now = time.time()
        if event == "SessionStart":
            if s["s"] != "run":
                s["s"] = "done"
        elif event == "UserPromptSubmit":
            s.update(s="run", tool="", since=now, prompt=_short(p.get("prompt"), 300))
        elif event in ("PreToolUse", "PostToolUse"):
            if s["s"] != "run":
                s.update(s="run", since=s.get("since") or now)
            s["tool"] = p.get("tool_name") or ""
        elif event == "Notification":
            kind = p.get("notification_type") or ""
            if kind in WAIT_TYPES:
                s["s"] = "wait"
                self._emit(self.on_attention, dict(s), p.get("message") or "")
        elif event in ("Stop", "StopFailure"):
            ran = now - (s.get("since") or now)
            was = s["s"]
            msg = p.get("last_assistant_message") or ""
            if not msg and event == "Stop":
                msg = _last_assistant(p.get("transcript_path"))
            s.update(s="done", tool="", last=_short(msg), ran=round(ran, 1), since=None)
            if was in ("run", "wait") and ran >= self.min_run:
                self._emit(self.on_finished, dict(s), s["last"])
        elif event == "PermissionRequest":
            return self._permission(s, p, req.get("wait"))
        self._changed()
        return {}

    def _touch(self, sid, p, env):
        with self.lock:
            self._expire()
            s = self.sess.get(sid)
            cwd = p.get("cwd") or (s or {}).get("cwd") or ""
            if not s:
                s = self.sess[sid] = {"id": sid, "n": _project_name(cwd), "cwd": cwd, "s": "done", "w": "Claude",
                                      "tool": "", "prompt": "", "last": "", "since": None, "ran": 0,
                                      "app": "", "orca_worktree": ""}
            s["at"] = time.time()
            s["cwd"] = cwd
            if p.get("session_title"):
                s["title"] = p["session_title"]
            ep = env.get("entrypoint") or ""
            s["app"] = "desktop" if ep == "claude-desktop" else (ep or s.get("app") or "")
            if env.get("orca") or env.get("orca_worktree"):
                s["w"] = "Orca"
                s["orca_worktree"] = env.get("orca_worktree") or s.get("orca_worktree") or ""
                if not self._apply_orca(s):
                    self._orca_refresh()
            elif self._orca_rows:
                self._apply_orca(s)
            return s

    def _expire(self):
        now = time.time()
        for sid, s in list(self.sess.items()):
            idle = now - s.get("at", now)
            if s["s"] == "run" and idle > self.stale_run:
                s.update(s="done", tool="", since=None)
            elif s["s"] != "run" and idle > self.forget_done:
                self.sess.pop(sid, None)

    # ---------- permissões ----------
    def _permission(self, s, p, wait):
        tool = p.get("tool_name") or ""
        s["s"] = "wait"
        s["tool"] = tool
        try:
            wait = min(float(wait or 0), self.max_wait)
        except (TypeError, ValueError):
            wait = 0
        if tool in NO_DECIDE or wait <= 0:
            self._changed()
            return {}  # o pedido aparece no app normalmente
        pid = uuid.uuid4().hex[:12]
        info = {"id": pid, "session": s["id"], "n": s["n"], "w": s["w"], "tool": tool,
                "text": _summary(tool, p.get("tool_input")), "t": time.time()}
        entry = {"info": info, "event": threading.Event(), "result": None}
        with self.lock:
            self.perms[pid] = entry
        self._emit(self.on_permission, dict(info))
        entry["event"].wait(wait)
        with self.lock:
            self.perms.pop(pid, None)
        self._emit(self.on_permission_done, pid)
        res = entry["result"]
        if res == "allow":
            s.update(s="run", at=time.time())
        self._changed()
        if res == "allow":
            return {"decision": "allow"}
        if res == "deny":
            return {"decision": "deny", "message": "O usuário negou pelo Jarvis.", "interrupt": False}
        return {}  # sem decisão (soltou ou expirou): o Claude Code mostra o pedido no app

    def decide(self, pid, yes):
        """Permitir (yes=True) ou negar o pedido pid. Devolve False se ele já acabou"""
        with self.lock:
            e = self.perms.get(pid)
            if not e:
                return False
            e["result"] = "allow" if yes else "deny"
            e["event"].set()
        return True

    def release(self, pid):
        """Solta o pedido sem decidir: o usuário vai responder no próprio app"""
        with self.lock:
            e = self.perms.get(pid)
            if not e:
                return False
            e["result"] = None
            e["event"].set()
        return True

    def pending(self):
        """Pedidos de permissão esperando, do mais antigo ao mais novo"""
        with self.lock:
            return sorted((dict(e["info"]) for e in self.perms.values()), key=lambda i: i["t"])

    # ---------- leitura ----------
    def sessions(self):
        """Lista para a ilha: [{n, s, w, id, tool, ago}] — esperando primeiro, depois rodando, depois as recentes"""
        now = time.time()
        with self.lock:
            self._expire()
            rows = [{"n": s["n"], "s": s["s"], "w": s["w"], "id": s["id"], "tool": s.get("tool") or "",
                     "ago": int(now - s.get("at", now))} for s in self.sess.values()]
        order = {"wait": 0, "run": 1, "done": 2}
        rows.sort(key=lambda r: (order.get(r["s"], 3), r["ago"]))
        return rows

    def session(self, sid):
        with self.lock:
            s = self.sess.get(sid)
            return dict(s) if s else None


# ======================= instalação no ~/.claude/settings.json =======================
def _python_exe():
    """pythonw.exe (sem janela) da mesma instalação do Python 3.12"""
    base = os.path.dirname(sys.executable)
    for name in ("pythonw.exe", "python.exe"):
        p = os.path.join(base, name)
        if os.path.exists(p) and "WindowsApps" not in p:
            return p
    return shutil.which("pythonw") or sys.executable


def _hook_entry(timeout, is_async):
    # Forma exec (command + args): sem shell, sem problema com o espaço/acento de "Área de Trabalho"
    h = {"type": "command", "command": _python_exe(), "args": ["-I", "-S", HOOK_SCRIPT], "timeout": timeout}
    if is_async:
        h["async"] = True
    return h


def hook_plan(wait=30):
    """evento -> (matcher, timeout, async). Só PermissionRequest e SessionEnd esperam; o resto não atrasa o Claude"""
    return {
        "SessionStart": (None, 5, True),
        "UserPromptSubmit": (None, 5, True),
        "PreToolUse": ("*", 5, True),
        "Notification": (None, 5, True),
        "Stop": (None, 5, True),
        "StopFailure": (None, 5, True),
        "SessionEnd": (None, 3, False),
        "PermissionRequest": ("*", int(wait) + 30, False),
    }


def _ours(group):
    return any(MARK in (h.get("command", "") + " " + " ".join(h.get("args") or [])) for h in group.get("hooks", []))


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)  # valida antes de gravar
    tmp = path + ".jarvis-tmp"
    crlf = False
    try:
        with open(path, "rb") as f:
            crlf = b"\r\n" in f.read(4096)  # mantém o fim de linha do arquivo
    except OSError:
        pass
    with open(tmp, "w", encoding="utf-8", newline="\r\n" if crlf else "\n") as f:
        f.write(text)
    os.replace(tmp, path)
    _load(path)  # valida o que ficou no disco


def _backup(path):
    if not os.path.exists(path):
        return None
    dst = f"{path}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, dst)
    return dst


def uninstall_hooks(path=SETTINGS, backup=True):
    """Tira só as entradas do Jarvis; o resto do settings.json fica igual"""
    if not os.path.exists(path):
        return None
    data = _load(path)
    bak = _backup(path) if backup else None
    hooks = data.get("hooks") or {}
    for ev in list(hooks):
        groups = [g for g in hooks[ev] if not _ours(g)]
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if not hooks and "hooks" in data:
        del data["hooks"]
    _save(path, data)
    return bak


def install_hooks(path=SETTINGS, wait=30):
    """Mescla os ganchos do Jarvis no settings.json (backup datado antes). Reinstalar troca só os nossos"""
    data = _load(path) if os.path.exists(path) else {}
    bak = _backup(path)
    hooks = data.setdefault("hooks", {})
    for ev, (matcher, timeout, is_async) in hook_plan(wait).items():
        groups = [g for g in hooks.get(ev, []) if not _ours(g)]
        g = {"hooks": [_hook_entry(timeout, is_async)]}
        if matcher:
            g = {"matcher": matcher, **g}
        hooks[ev] = groups + [g]
    _save(path, data)
    return bak


if __name__ == "__main__":
    if "--install" in sys.argv:
        print("ganchos instalados; backup:", install_hooks())
    elif "--uninstall" in sys.argv:
        print("ganchos removidos; backup:", uninstall_hooks())
    else:
        print(__doc__)
