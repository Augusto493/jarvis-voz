"""Gancho do Claude Code -> Jarvis. Instalado em ~/.claude/settings.json por `hooks_bridge.py --install`.

O Claude Code chama este script nos eventos (SessionStart, UserPromptSubmit, PreToolUse, Notification, Stop,
StopFailure, SessionEnd, PermissionRequest) com o JSON do evento no stdin. Ele repassa para o Jarvis em
POST http://127.0.0.1:PORT/rpc/hook {"event", "payload", "env", "wait"}.

Regras: nunca travar o Claude Code. Sem Jarvis (sem arquivo de token ou porta fechada) sai em silêncio,
código 0. No PermissionRequest espera a decisão da ilha por até JARVIS_PERMISSION_WAIT segundos (padrão 30)
e imprime a decisão no formato oficial; sem decisão não imprime nada e o pedido normal aparece no app.
Só biblioteca padrão; roda com pythonw -I -S (sem janela, sem site-packages, início rápido)."""
import os
import sys


def main():
    if os.environ.get("JARVIS_BRAIN"):
        return  # o próprio cérebro do Jarvis (claude -p) não entra no painel
    home = os.environ.get("JARVIS_HOME") or os.path.join(os.environ.get("APPDATA") or "", "jarvis")
    try:
        with open(os.path.join(home, "token"), encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return  # Jarvis desligado: o servidor apaga o token ao sair
    if not token:
        return
    raw = sys.stdin.buffer.read() if sys.stdin else b""

    import json
    import socket
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        return
    event = payload.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    port = os.environ.get("JARVIS_PORT")
    if not port:
        try:
            with open(os.path.join(home, "port"), encoding="utf-8") as f:
                port = f.read().strip()
        except OSError:
            port = "48777"
    wait = 0.0
    if event == "PermissionRequest":
        try:
            wait = float(os.environ.get("JARVIS_PERMISSION_WAIT") or 30)
        except ValueError:
            wait = 30.0
    body = json.dumps({
        "event": event,
        "payload": _trim(payload),
        "wait": wait,
        "env": {  # de onde veio a sessão (app Claude, terminal, Orca)
            "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""),
            "orca": bool(os.environ.get("ORCA_PANE_KEY") or os.environ.get("ORCA_AGENT_PANE")),
            "orca_worktree": os.environ.get("ORCA_WORKTREE_ID", ""),
        },
    }, ensure_ascii=False).encode("utf-8")

    # No Windows uma porta fechada demora ~2 s para recusar: o connect tem prazo curto (no loopback é instantâneo)
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=0.15)
    except (OSError, ValueError):
        return
    try:
        s.settimeout(wait + 5 if wait else 1.0)
        head = (f"POST /rpc/hook HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                f"X-Jarvis-Token: {token}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n")
        s.sendall(head.encode("ascii") + body)
        if not wait:
            # Eventos comuns: basta o servidor ter lido; espera só o começo da resposta (até 1 s)
            try:
                s.recv(64)
            except OSError:
                pass
            return
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        return
    finally:
        s.close()
    head, _, resp = data.partition(b"\r\n\r\n")
    if not head.startswith(b"HTTP/1.") or b" 200 " not in head.split(b"\r\n")[0] + b" ":
        return
    try:
        out = json.loads(resp.decode("utf-8") or "{}")
    except ValueError:
        return
    decision = out.get("decision")
    if decision not in ("allow", "deny"):
        return  # sem decisão: o Claude Code mostra o pedido normal
    d = {"behavior": decision}
    if decision == "deny":
        d["message"] = out.get("message") or "O Augusto negou pelo Jarvis."
        d["interrupt"] = bool(out.get("interrupt"))
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}))
    sys.stdout.flush()


def _trim(v, depth=0):
    """Corta textos grandes (conteúdo de Write/Edit) para o POST ficar leve; o Jarvis só precisa de um resumo"""
    if isinstance(v, str):
        return v if len(v) <= 2000 else v[:2000] + "…"
    if isinstance(v, dict):
        return {k: _trim(x, depth + 1) for k, x in v.items()} if depth < 6 else {}
    if isinstance(v, list):
        return [_trim(x, depth + 1) for x in v[:50]] if depth < 6 else []
    return v


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass  # qualquer erro: silêncio e código 0, o Claude Code segue normal
    sys.exit(0)
