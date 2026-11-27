"""Controle do Orca (app instalado no PC) pelo CLI `orca`.

Mesma ideia do claude_ui.py: ler o estado dos agentes de cada workspace, abrir um, ler o que o agente
disse e mandar texto. O Orca expõe tudo por `orca worktree ps --json` (estado dos agentes, última resposta,
prompt) e `orca terminal ...` (terminais vivos), então não precisa clicar na tela.
"""
import difflib
import json
import re
import shutil
import subprocess
import time
import unicodedata

ORCA = shutil.which("orca") or "orca"
STATE_PT = {"working": "trabalhando", "running": "trabalhando", "done": "concluído", "idle": "parado",
            "blocked": "esperando você", "waiting": "esperando você", "error": "com erro", "interrupted": "interrompido"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _run(args, timeout=25):
    p = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, encoding="utf-8",
                       timeout=timeout, creationflags=0x08000000)
    try:
        j = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError((p.stderr or p.stdout or "o Orca não respondeu").strip()[:200])
    if not j.get("ok", True):
        raise RuntimeError(str(j.get("error") or j)[:200])
    return j.get("result", j)


class OrcaApp:
    def __init__(self):
        self._cache = (0.0, [])

    def available(self):
        try:
            return bool(_run(["status"], 8))
        except Exception:
            return False

    def _worktrees(self, max_age=4.0):
        t, data = self._cache
        if time.time() - t < max_age and data:
            return data
        rows = _run(["worktree", "ps", "--limit", "80"]).get("worktrees", [])
        out = []
        for w in rows:
            if w.get("isArchived"):
                continue
            agents = w.get("agents") or []
            main = next((a for a in agents if a.get("mainAgent")), agents[0] if agents else None)
            out.append({
                "id": w.get("worktreeId"),
                "name": f"{w.get('repo', '')} {w.get('displayName', '')}".strip() if w.get("displayName") not in (None, "main", w.get("repo")) else w.get("repo", ""),
                "repo": w.get("repo", ""),
                "path": w.get("path", ""),
                "state": (main or {}).get("state", ""),
                "agent": (main or {}).get("agentType", ""),
                "task": (main or {}).get("taskTitle") or (main or {}).get("conversationName") or "",
                "prompt": (main or {}).get("prompt", ""),
                "last": (main or {}).get("lastAssistantMessage", ""),
                "tool": (main or {}).get("toolName", ""),
                "updated": (main or {}).get("updatedAt") or w.get("lastActivityAt") or 0,
                "terminals": w.get("liveTerminalCount", 0),
                "raw": w,
            })
        self._cache = (time.time(), out)
        return out

    # ---------- leitura ----------
    def snapshot(self):
        wts = self._worktrees()
        active = [w for w in wts if w["state"] or w["terminals"]]
        active.sort(key=lambda w: -(w["updated"] or 0))
        return {
            "workspaces_com_agente": [
                f"{w['name']} ({STATE_PT.get(w['state'], w['state'] or 'sem agente')}"
                f"{', ' + w['agent'] if w['agent'] else ''})" for w in active[:25]],
            "total_workspaces": len(wts),
        }

    def running(self):
        return [w["name"] for w in self._worktrees() if w["state"] in ("working", "running")]

    def states(self):
        return {w["name"]: w["state"] for w in self._worktrees(2.0) if w["state"]}

    def _find(self, query):
        wts = self._worktrees()
        q = norm(query)
        best, score = None, 0.0
        for w in wts:
            for cand in (w["name"], w["repo"], w["task"]):
                n = norm(cand)
                if not n:
                    continue
                s = 2.0 if q == n else (1.2 if (q in n or n in q) else difflib.SequenceMatcher(None, q, n).ratio())
                if s > score:
                    best, score = w, s
        return best if score >= 0.55 else None

    def status(self, name):
        w = self._find(name)
        if not w:
            raise RuntimeError(f"não achei workspace parecido com '{name}' no Orca")
        txt = [f"workspace {w['name']}: {STATE_PT.get(w['state'], w['state'] or 'sem agente rodando')}"]
        if w["agent"]:
            txt.append(f"agente {w['agent']}")
        if w["task"]:
            txt.append(f"tarefa: {w['task']}")
        if w["prompt"]:
            txt.append(f"último pedido: {w['prompt'][:300]}")
        if w["tool"] and w["state"] in ("working", "running"):
            txt.append(f"usando a ferramenta {w['tool']}")
        if w["last"]:
            txt.append(f"última resposta do agente: {w['last'][-1200:]}")
        return "\n".join(txt)

    # ---------- ações ----------
    def open(self, name):
        w = self._find(name)
        if not w:
            raise RuntimeError(f"não achei workspace parecido com '{name}' no Orca")
        _run(["worktree", "activate", "--worktree", w["id"]])
        return f"abri o workspace {w['name']} no Orca"

    def _terminals(self, w):
        res = _run(["terminal", "list", "--worktree", w["id"]])
        return res.get("terminals", [])

    def send(self, name, text):
        w = self._find(name)
        if not w:
            raise RuntimeError(f"não achei workspace parecido com '{name}' no Orca")
        terms = self._terminals(w)
        if not terms:
            raise RuntimeError(f"o workspace {w['name']} não tem terminal aberto para eu digitar")
        handle = terms[0].get("handle") or terms[0].get("id")
        _run(["terminal", "send", "--terminal", handle, "--text", text, "--enter"])
        return f"enviei o pedido ao terminal do workspace {w['name']}"

    def read_terminal(self, name, lines=40):
        w = self._find(name)
        if not w:
            raise RuntimeError(f"não achei workspace parecido com '{name}' no Orca")
        terms = self._terminals(w)
        if not terms:
            return self.status(name)
        handle = terms[0].get("handle") or terms[0].get("id")
        res = _run(["terminal", "read", "--terminal", handle])
        return (res.get("text") or json.dumps(res, ensure_ascii=False))[-1800:]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    t = time.time()
    o = OrcaApp()
    print(o.snapshot(), round(time.time() - t, 1), "s")
    print(o.running())
