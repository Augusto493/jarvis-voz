"""Servidor local (só 127.0.0.1) que serve a ilha e leva os mesmos eventos para qualquer página aberta:
a janela do topo da tela (pywebview) e a aba do Orca. A página chama /api/<método> e recebe /events (SSE).

Também atende os ganchos do Claude Code (jarvis_hook.py) em /rpc/<nome>, que responde na hora com JSON.
Todo POST exige o cabeçalho X-Jarvis-Token: o token é sorteado a cada início e gravado em
%APPDATA%\\jarvis\\token (o gancho lê dali; a ilha recebe o token embutido no HTML)."""
import atexit
import json
import os
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("JARVIS_PORT") or 48777)
# Pasta do token (JARVIS_HOME só nos testes, para não trocar o token de um Jarvis que esteja rodando)
JARVIS_DIR = os.environ.get("JARVIS_HOME") or os.path.join(os.environ.get("APPDATA") or HERE, "jarvis")
TOKEN = secrets.token_urlsafe(24)
TOKEN_FILE = os.path.join(JARVIS_DIR, "token")
PORT_FILE = os.path.join(JARVIS_DIR, "port")
MAX_BODY = 4 * 1024 * 1024


def _write_token():
    os.makedirs(JARVIS_DIR, exist_ok=True)
    for path, text in ((TOKEN_FILE, TOKEN), (PORT_FILE, str(PORT))):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)


def _drop_token():
    """Ao sair, apaga o token (se ainda for o nosso): o gancho vê que não há Jarvis e sai sem nem conectar"""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            mine = f.read().strip() == TOKEN
        if mine:
            os.remove(TOKEN_FILE)
    except OSError:
        pass


_write_token()
atexit.register(_drop_token)


class Hub:
    def __init__(self):
        self.clients = []
        self.latest = {}  # último valor de cada função de estado, para páginas que abrem depois
        self.handlers = {}  # /api/<nome>: chamada em segundo plano, resposta 204
        self.rpc = {}  # /rpc/<nome>: fn(payload) -> objeto JSON, resposta síncrona
        self.history = []  # mensagens do chat, para a aba do Orca começar com a conversa

    def publish(self, fn, *args):
        m = json.dumps({"fn": fn, "args": list(args)}, ensure_ascii=False)
        if fn in ("state", "lists", "alert", "confirm", "approval"):
            self.latest[fn] = m
        if fn in ("msg", "said", "heard"):
            self.history = (self.history + [m])[-40:]
        for q in list(self.clients):
            try:
                q.put_nowait(m)
            except queue.Full:
                pass


HUB = Hub()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _local(self):
        # Host fixo contra DNS rebinding; Origin ausente (gancho, pywebview) ou a própria página
        host = (self.headers.get("Host") or "").lower()
        return (self.client_address[0] == "127.0.0.1"
                and host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
                and self.headers.get("Origin") in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"))

    def _authorized(self):
        return secrets.compare_digest(self.headers.get("X-Jarvis-Token") or "", TOKEN)

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # o gancho de evento comum fecha a conexão sem esperar o corpo

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html") and self._local():
            with open(os.path.join(HERE, "ui", "island.html"), "rb") as f:
                body = f.read()
            body = body.replace(b"__JARVIS_TOKEN__", TOKEN.encode("ascii"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events" and self._local():
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue(maxsize=200)
            for m in list(HUB.latest.values()) + HUB.history:
                q.put_nowait(m)
            HUB.clients.append(q)
            try:
                while True:
                    try:
                        m = q.get(timeout=15)
                        self.wfile.write(f"data: {m}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                if q in HUB.clients:
                    HUB.clients.remove(q)
        else:
            self.send_error(404)

    def _body(self, default):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > MAX_BODY:
            return default
        try:
            return json.loads(self.rfile.read(n) or b"null") or default
        except ValueError:
            return default

    def _reject(self, code):
        # No Windows, fechar a conexão com o corpo ainda não lido aborta o socket (WinError 10053)
        # e o cliente pode não receber o status. Descarta o corpo (até MAX_BODY) antes de responder.
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        if 0 < n <= MAX_BODY:
            self.rfile.read(n)
        self.send_error(code)

    def do_POST(self):
        path = self.path.split("?")[0]
        if not path.startswith(("/api/", "/rpc/")) or not self._local() or not self._authorized():
            return self._reject(403)
        if path.startswith("/rpc/"):
            # Síncrono: o gancho espera a resposta (o pedido de permissão pode esperar o clique por minutos)
            fn = HUB.rpc.get(path[5:])
            if not fn:
                return self._reject(404)
            payload = self._body({})
            try:
                out = fn(payload)
            except Exception as e:
                return self._send_json(500, {"error": str(e)[:300]})
            return self._send_json(200, out if out is not None else {})
        name = path[5:]
        args = self._body([])
        if not isinstance(args, list):
            args = [args]
        fn = HUB.handlers.get(name)
        if fn:
            threading.Thread(target=lambda: fn(*args), daemon=True).start()
        self.send_response(204)
        self.end_headers()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        pass  # conexão cortada pelo cliente não é erro do Jarvis


def start():
    srv = _Server(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{PORT}/"
