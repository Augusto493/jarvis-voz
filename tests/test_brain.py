"""Testes do cérebro MCP do Jarvis, com um /rpc/tool falso (não abre janelas nem mexe no app Claude).

    py -3.12 -m unittest tests.test_brain -v

Os testes do Brain chamam o modelo de verdade (haiku, 2 pedidos). Para pular: set JARVIS_SKIP_LLM=1
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

PORT = 48791
TOKEN = "token-de-teste"
CALLS = []


class FakeJarvis(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != "/rpc/tool" or self.headers.get("X-Jarvis-Token") != TOKEN:
            self.send_error(403)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        CALLS.append(body)
        name = body.get("name")
        result = {"open_session": f"abri a sessão {body.get('arg')}",
                  "status": "sessão aberta: Meu App; nada em execução"}.get(name, "feito")
        data = json.dumps({"ok": True, "result": result}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


TMP = tempfile.mkdtemp(prefix="jarvis-test-")
TOKEN_FILE = os.path.join(TMP, "token")
VAULT = os.path.join(TMP, "cofre")
ENV = {"JARVIS_PORT": str(PORT), "JARVIS_TOKEN_FILE": TOKEN_FILE, "JARVIS_VAULT": VAULT}
SRV = None


def setUpModule():
    global SRV
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(TOKEN)
    SRV = ThreadingHTTPServer(("127.0.0.1", PORT), FakeJarvis)
    threading.Thread(target=SRV.serve_forever, daemon=True).start()


def tearDownModule():
    SRV.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)


class McpProcess:
    """Fala JSON-RPC por stdio com o jarvis_mcp.py"""

    def __init__(self):
        self.p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "jarvis_mcp.py")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=dict(os.environ, **ENV))
        self.n = 0

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.p.stdin.flush()
        if notify:
            return None
        return json.loads(self.p.stdout.readline().decode("utf-8"))

    def close(self):
        self.p.kill()
        self.p.wait()
        self.p.stdin.close()
        self.p.stdout.close()


class TestMcp(unittest.TestCase):
    def setUp(self):
        CALLS.clear()
        self.m = McpProcess()

    def tearDown(self):
        self.m.close()

    def test_initialize_e_tools_list(self):
        r = self.m.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "teste", "version": "0"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.m.rpc("notifications/initialized", notify=True)
        names = {t["name"] for t in self.m.rpc("tools/list")["result"]["tools"]}
        for n in ("open_session", "new_session", "send", "approve", "deny", "stop", "set_model", "set_effort",
                  "read_conversation", "tasks", "status", "orca_status", "orca_send", "memoria_ler", "memoria_anotar"):
            self.assertIn(n, names)

    def test_call_encaminha_ao_jarvis(self):
        r = self.m.rpc("tools/call", {"name": "orca_send", "arguments": {"workspace": "Loja Virtual", "texto": "rode os testes"}})
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(CALLS[-1]["name"], "orca_send")
        self.assertEqual(CALLS[-1]["arg"], "Loja Virtual | rode os testes")
        self.assertEqual(CALLS[-1]["args"]["texto"], "rode os testes")

    def test_memoria(self):
        r = self.m.rpc("tools/call", {"name": "memoria_anotar", "arguments": {"texto": "falta validar a 99", "projeto": "Copiloto"}})
        self.assertFalse(r["result"]["isError"])
        self.m.rpc("tools/call", {"name": "memoria_anotar", "arguments": {"texto": "rodar testes do Meu App"}})
        txt = self.m.rpc("tools/call", {"name": "memoria_ler", "arguments": {"projeto": "copiloto"}})["result"]["content"][0]["text"]
        self.assertIn("falta validar a 99", txt)
        self.assertNotIn("Meu App", txt)
        note = open(os.path.join(VAULT, "00 Inbox", "Jarvis - memória.md"), encoding="utf-8").read()
        self.assertTrue(note.startswith("---\ncriado: "))
        self.assertIn("tags: [jarvis]", note)


@unittest.skipIf(os.environ.get("JARVIS_SKIP_LLM"), "sem modelo")
class TestBrain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from brain import Brain
        cls.brain = Brain("haiku", port=PORT, env=ENV, work=os.path.join(TMP, "work"))
        t = time.time()
        cls.brain.warm()
        print(f"\n[processo iniciado em {time.time() - t:.2f} s]")

    @classmethod
    def tearDownClass(cls):
        cls.brain.close()

    CTX = ('CONTEXTO: {"sessao_aberta": "Jarvis voz", "sessoes": ["Meu App", "Jarvis voz", "Loja Virtual"], '
           '"projetos": ["Meu App", "voz-claude"]}')

    def test_1_abrir_projeto_chama_ferramenta(self):
        CALLS.clear()
        said = []
        r = self.brain.ask(f"{self.CTX}\n\nUSUÁRIO DISSE: abra o projeto meu ape", on_sentence=said.append)
        print(f"\n[abrir] 1ª frase {r['first_sentence']} s, total {r['total']:.2f} s, fala={r['say']!r}, tools={r['tools']}")
        opened = [c for c in CALLS if c["name"] == "open_session"]
        self.assertTrue(opened, f"não chamou open_session: {CALLS}")
        self.assertEqual(opened[0]["arg"].lower().replace(" ", "-"), "meu-app")
        self.assertEqual(r["tools"][0]["tool"], "open_session")
        self.assertTrue(said)

    def test_2_pergunta_simples_so_fala(self):
        CALLS.clear()
        said = []
        r = self.brain.ask(f"{self.CTX}\n\nUSUÁRIO DISSE: quanto é sete vezes oito?", on_sentence=said.append)
        print(f"\n[simples] 1ª frase {r['first_sentence']:.2f} s, total {r['total']:.2f} s, fala={r['say']!r}")
        self.assertEqual(CALLS, [])
        self.assertEqual(r["tools"], [])
        self.assertIn("56", r["say"].replace("cinquenta e seis", "56"))
        self.assertNotIn("FALA:", r["say"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
