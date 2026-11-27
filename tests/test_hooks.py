"""Testes dos ganchos: web_server de verdade numa porta de teste + jarvis_hook.py chamado como o Claude Code chama
(processo novo, JSON no stdin). Não abre janelas, não mexe no Jarvis que estiver rodando (porta e pasta próprias).

    py -3.12 -m unittest tests.test_hooks -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

PORT = 48792
HOME = tempfile.mkdtemp(prefix="jarvis-hooks-")
os.environ["JARVIS_PORT"] = str(PORT)
os.environ["JARVIS_HOME"] = HOME  # o token de teste não sobrescreve o do Jarvis de verdade

import web_server  # noqa: E402
import hooks_bridge  # noqa: E402

HOOK = os.path.join(HERE, "jarvis_hook.py")
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
EVENTS = []


def base(event, sid="s1", cwd=r"C:\projetos\voz-claude", **kw):
    return {"session_id": sid, "transcript_path": "", "cwd": cwd, "permission_mode": "default",
            "hook_event_name": event, **kw}


def run_hook(payload, env=None, exe=None, bg=False):
    e = dict(os.environ, JARVIS_PORT=str(PORT), JARVIS_HOME=HOME)
    e.pop("JARVIS_BRAIN", None)
    e.update(env or {})
    args = [exe or sys.executable, "-I", "-S", HOOK]
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e,
                         creationflags=0x08000000)
    data = json.dumps(payload).encode("utf-8")
    if bg:
        p.stdin.write(data)
        p.stdin.close()
        return p
    t = time.perf_counter()
    out, err = p.communicate(data, timeout=60)
    return p.returncode, out.decode("utf-8"), time.perf_counter() - t


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def wait_for(cond, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class HooksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = hooks_bridge.HookBridge(
            on_change=lambda rows: EVENTS.append(("change", rows)),
            on_finished=lambda s, m: EVENTS.append(("finished", s, m)),
            on_permission=lambda i: EVENTS.append(("perm", i)),
            on_permission_done=lambda pid: EVENTS.append(("perm_done", pid)),
            orca=False, min_run=0)
        web_server.HUB.rpc["hook"] = cls.bridge.handle
        web_server.start()

    def setUp(self):
        EVENTS.clear()

    def last(self, kind):
        return [e for e in EVENTS if e[0] == kind]

    # ---------- servidor ----------
    def test_token_file_and_403(self):
        with open(os.path.join(HOME, "token"), encoding="utf-8") as f:
            self.assertEqual(f.read(), web_server.TOKEN)
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/rpc/hook", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(c.exception.code, 403)
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/x", data=b"[]", method="POST",
                                     headers={"X-Jarvis-Token": "errado"})
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(c.exception.code, 403)

    def test_rpc_unknown_404_and_api_204(self):
        h = {"X-Jarvis-Token": web_server.TOKEN, "Content-Type": "application/json"}
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/rpc/nada", data=b"{}", method="POST", headers=h)
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(c.exception.code, 404)
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/nada", data=b"[]", method="POST", headers=h)
        self.assertEqual(urllib.request.urlopen(req, timeout=3).status, 204)
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/nada", data=b"[]", method="POST",
                                     headers=dict(h, Origin="http://evil.example"))
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(c.exception.code, 403)

    def test_island_gets_token(self):
        html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=3).read().decode("utf-8")
        self.assertNotIn("__JARVIS_TOKEN__", html)

    # ---------- ciclo de uma sessão ----------
    def test_session_lifecycle(self):
        code, out, _ = run_hook(base("SessionStart", sid="life", source="startup", model="claude-opus-5"),
                                env={"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.bridge.session("life")["s"], "done")
        self.assertEqual(self.bridge.session("life")["n"], "voz-claude")
        self.assertEqual(self.bridge.session("life")["app"], "desktop")
        run_hook(base("UserPromptSubmit", sid="life", prompt="roda os testes"))
        self.assertEqual(self.bridge.session("life")["s"], "run")
        run_hook(base("PreToolUse", sid="life", tool_name="Bash", tool_input={"command": "pytest"},
                      tool_use_id="t1"))
        row = next(r for r in self.bridge.sessions() if r["id"] == "life")
        self.assertEqual((row["s"], row["tool"], row["w"]), ("run", "Bash", "Claude"))
        self.assertEqual(set(row), {"n", "s", "w", "id", "tool", "ago"})
        run_hook(base("Notification", sid="life", message="Claude needs your permission",
                      notification_type="permission_prompt"))
        self.assertEqual(self.bridge.session("life")["s"], "wait")
        run_hook(base("Stop", sid="life", stop_hook_active=False, last_assistant_message="Todos os testes passaram."))
        self.assertEqual(self.bridge.session("life")["s"], "done")
        self.assertTrue(wait_for(lambda: self.last("finished")))
        self.assertEqual(self.last("finished")[-1][2], "Todos os testes passaram.")
        run_hook(base("SessionEnd", sid="life", reason="other"))
        self.assertIsNone(self.bridge.session("life"))

    def test_stop_reads_transcript(self):
        path = os.path.join(HOME, "t.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "oi"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Pronto, corrigi o bug."}]}}) + "\n")
        run_hook(base("UserPromptSubmit", sid="tr", prompt="x"))
        run_hook(base("Stop", sid="tr", transcript_path=path))
        self.assertEqual(self.bridge.session("tr")["last"], "Pronto, corrigi o bug.")

    def test_orca_from_env(self):
        run_hook(base("UserPromptSubmit", sid="orca1", cwd=r"C:\x\loja-virtual", prompt="x"),
                 env={"ORCA_PANE_KEY": "p1", "ORCA_WORKTREE_ID": "w1"})
        self.assertEqual(self.bridge.session("orca1")["w"], "Orca")

    def test_brain_is_ignored(self):
        code, out, dt = run_hook(base("UserPromptSubmit", sid="brain", prompt="x"), env={"JARVIS_BRAIN": "1"})
        self.assertEqual((code, out), (0, ""))
        self.assertIsNone(self.bridge.session("brain"))

    # ---------- permissões ----------
    def _perm(self, sid, env=None, exe=None):
        p = run_hook(base("PermissionRequest", sid=sid, tool_name="Bash",
                          tool_input={"command": "rm -rf node_modules", "description": "Remove node_modules"},
                          permission_suggestions=[]), env=env, exe=exe, bg=True)
        self.assertTrue(wait_for(lambda: any(e[1]["session"] == sid for e in self.last("perm")), 10))
        info = next(e[1] for e in self.last("perm") if e[1]["session"] == sid)
        self.assertEqual(info["text"], "rm -rf node_modules")
        self.assertEqual(self.bridge.session(sid)["s"], "wait")
        return p, info

    def test_permission_allow(self):
        p, info = self._perm("pa")
        self.assertTrue(self.bridge.decide(info["id"], True))
        out = json.loads(p.communicate(timeout=10)[0].decode())
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                      "decision": {"behavior": "allow"}}})
        self.assertEqual(p.returncode, 0)
        self.assertTrue(wait_for(lambda: ("perm_done", info["id"]) in EVENTS))
        self.assertEqual(self.bridge.session("pa")["s"], "run")

    def test_permission_deny_with_pythonw(self):
        p, info = self._perm("pd", exe=PYW)
        self.bridge.decide(info["id"], False)
        out = json.loads(p.communicate(timeout=10)[0].decode())
        d = out["hookSpecificOutput"]["decision"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual((d["behavior"], d["interrupt"]), ("deny", False))
        self.assertTrue(d["message"])

    def test_permission_release(self):
        p, info = self._perm("pr")
        self.assertTrue(self.bridge.release(info["id"]))
        self.assertEqual(p.communicate(timeout=10)[0], b"")
        self.assertEqual(p.returncode, 0)
        self.assertFalse(self.bridge.decide(info["id"], True))  # já acabou

    def test_permission_timeout(self):
        t = time.perf_counter()
        p, info = self._perm("pt", env={"JARVIS_PERMISSION_WAIT": "1"})
        self.assertEqual(p.communicate(timeout=10)[0], b"")
        self.assertEqual(p.returncode, 0)
        self.assertLess(time.perf_counter() - t, 4)
        self.assertTrue(wait_for(lambda: ("perm_done", info["id"]) in EVENTS))

    def test_ask_user_question_not_held(self):
        code, out, dt = run_hook(base("PermissionRequest", sid="q", tool_name="AskUserQuestion", tool_input={}))
        self.assertEqual((code, out), (0, ""))
        self.assertLess(dt, 2)

    # ---------- Jarvis desligado ----------
    def test_jarvis_off_is_fast(self):
        payload = base("PreToolUse", sid="off", tool_name="Read", tool_input={"file_path": "a"})
        times = {}
        empty = tempfile.mkdtemp(prefix="jarvis-off-")  # sem arquivo de token
        stale = tempfile.mkdtemp(prefix="jarvis-stale-")  # token velho, porta fechada
        with open(os.path.join(stale, "token"), "w") as f:
            f.write("velho")
        for label, home, exe in (("sem token / python", empty, sys.executable), ("sem token / pythonw", empty, PYW),
                                 ("token velho, porta fechada / pythonw", stale, PYW)):
            best = 9.0
            for _ in range(3):
                code, out, dt = run_hook(payload, env={"JARVIS_HOME": home, "JARVIS_PORT": "48799"}, exe=exe)
                self.assertEqual((code, out), (0, ""))
                best = min(best, dt)
            times[label] = best
        print("\n  tempo do gancho com o Jarvis desligado (melhor de 3):",
              ", ".join(f"{k}: {v * 1000:.0f} ms" for k, v in times.items()))
        for v in times.values():
            self.assertLess(v, 1.0)


class InstallTest(unittest.TestCase):
    def test_merge_keeps_other_hooks(self):
        path = os.path.join(HOME, "settings.json")
        orig = {"env": {"A": "1"}, "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "py -3.12 obsidian_context.py", "timeout": 10}]}],
            "PermissionRequest": [{"matcher": "*", "hooks": [{"type": "command", "command": "orca-agent-hook.exe claude"}]}]}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(orig, f)
        bak = hooks_bridge.install_hooks(path)
        self.assertTrue(os.path.exists(bak))
        hooks_bridge.install_hooks(path)  # reinstalar não duplica
        d = load(path)
        self.assertEqual(d["env"], {"A": "1"})
        self.assertEqual(len(d["hooks"]["SessionStart"]), 2)
        self.assertIn("obsidian_context.py", d["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(len(d["hooks"]["PermissionRequest"]), 2)
        self.assertIn(HOOK, d["hooks"]["PreToolUse"][0]["hooks"][0]["args"])
        hooks_bridge.uninstall_hooks(path, backup=False)
        self.assertEqual(load(path), orig)


if __name__ == "__main__":
    unittest.main(verbosity=2)
