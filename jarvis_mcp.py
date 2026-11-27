"""Servidor MCP (stdio) do Jarvis: expõe ao cérebro (`claude -p`) as ações do Jarvis como ferramentas de verdade.

Implementação mínima do protocolo MCP à mão (JSON-RPC 2.0, uma mensagem JSON por linha): initialize,
tools/list, tools/call e ping. Sem dependências além da biblioteca padrão.

Cada tools/call das ações é encaminhado ao processo do Jarvis:
    POST http://127.0.0.1:{JARVIS_PORT}/rpc/tool   corpo {"name": ..., "args": {...}, "arg": "..."}
    cabeçalho X-Jarvis-Token = conteúdo de %APPDATA%\\jarvis\\token
e a resposta {"ok": bool, "result": str} vira o conteúdo da ferramenta. "arg" é o argumento já no formato de
string que as funções do TOOLS do jarvis.py recebem (ex.: orca_send = "workspace | texto").
A confirmação das ações sensíveis (send, orca_send, approve, deny) é feita pelo Jarvis no handler do /rpc/tool.

As ferramentas de memória (memoria_ler / memoria_anotar) rodam aqui mesmo, no cofre Obsidian.

Variáveis de ambiente (para testes): JARVIS_PORT, JARVIS_TOKEN_FILE, JARVIS_VAULT.
"""
import datetime
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request

PORT = int(os.environ.get("JARVIS_PORT") or 48777)
TOKEN_FILE = os.environ.get("JARVIS_TOKEN_FILE") or os.path.join(
    os.environ.get("JARVIS_HOME") or os.path.join(os.environ.get("APPDATA", ""), "jarvis"), "token")
VAULT = os.environ.get("JARVIS_VAULT") or os.path.expanduser(r"~\Documents\Obsidian")
NOTE = os.path.join("00 Inbox", "Jarvis - memória.md")
RPC_TIMEOUT = 120  # a confirmação por voz pode levar até ~25 s, e a UI mais alguns

S = {"type": "string"}


def _obj(props=None, required=None):
    o = {"type": "object", "properties": props or {}}
    if required:
        o["required"] = required
    return o


# nome -> (descrição, esquema, função que monta o "arg" string do TOOLS do jarvis.py)
ACTIONS = {
    "open_session": (
        "Abre no app Claude desktop a sessão (conversa do Claude Code) ou projeto pelo nome, como foi ouvido. "
        "O Jarvis acha o nome mais parecido, inclusive dentro de projetos recolhidos.",
        _obj({"nome": {**S, "description": "nome da sessão ou do projeto, ex.: 'Meu App'"}}, ["nome"]),
        lambda a: a.get("nome", "")),
    "new_session": (
        "Cria uma sessão nova do Claude Code no app Claude, num projeto (ou genérica se o projeto vier vazio).",
        _obj({"projeto": {**S, "description": "nome do projeto; vazio = sessão nova genérica"}}),
        lambda a: a.get("projeto", "")),
    "send": (
        "Envia um pedido ao Claude Code na sessão ABERTA (digita e aperta enviar). Escreva o texto como o Augusto "
        "escreveria, claro e completo. AÇÃO SENSÍVEL: o Jarvis pede confirmação por voz antes de enviar; se o "
        "resultado disser que não foi confirmado, não insista.",
        _obj({"texto": {**S, "description": "pedido a enviar"}}, ["texto"]),
        lambda a: a.get("texto", "")),
    "type": (
        "Só digita um texto na caixa da sessão aberta, sem enviar.",
        _obj({"texto": S}, ["texto"]),
        lambda a: a.get("texto", "")),
    "read_conversation": (
        "Lê as últimas mensagens da sessão aberta no app Claude. Use para saber o andamento de um projeto "
        "(abra a sessão antes, se não for a aberta).",
        _obj(), lambda a: ""),
    "tasks": (
        "Lista as tarefas (to-dos) da sessão aberta no app Claude: em andamento, pendentes e concluídas.",
        _obj(), lambda a: ""),
    "status": (
        "Estado atual do app Claude e do Orca: sessão aberta, lista de sessões com status (em execução, não lida), "
        "projetos, tarefas e botões de permissão pendentes. Use antes de afirmar qualquer coisa sobre o estado.",
        _obj(), lambda a: ""),
    "set_model": (
        "Troca o modelo do chat aberto no app Claude.",
        _obj({"modelo": {**S, "enum": ["Opus", "Sonnet", "Haiku", "Fable"]}}, ["modelo"]),
        lambda a: a.get("modelo", "")),
    "set_effort": (
        "Troca o nível de esforço do chat aberto no app Claude.",
        _obj({"esforco": {**S, "enum": ["Baixo", "Médio", "Alto", "Máximo"]}}, ["esforco"]),
        lambda a: a.get("esforco", "")),
    "approve": (
        "Clica em Permitir no pedido de permissão aberto no app Claude. Só quando o Augusto pedir claramente. "
        "AÇÃO SENSÍVEL: o Jarvis pede confirmação.",
        _obj(), lambda a: ""),
    "deny": (
        "Clica em Negar no pedido de permissão aberto no app Claude. Só quando o Augusto pedir claramente. "
        "AÇÃO SENSÍVEL: o Jarvis pede confirmação.",
        _obj(), lambda a: ""),
    "stop": (
        "Interrompe o Claude que está respondendo na sessão aberta. Só quando o Augusto pedir.",
        _obj(), lambda a: ""),
    "scroll": (
        "Rola a conversa aberta no app Claude.",
        _obj({"direcao": {**S, "enum": ["up", "down"]}}),
        lambda a: a.get("direcao", "down")),
    "show": ("Traz a janela do app Claude para frente.", _obj(), lambda a: ""),
    "minimize": ("Minimiza a janela do app Claude.", _obj(), lambda a: ""),
    "orca_status": (
        "No app Orca: estado do agente de um workspace/projeto, o último pedido e a última resposta.",
        _obj({"workspace": {**S, "description": "nome do workspace ou projeto no Orca"}}, ["workspace"]),
        lambda a: a.get("workspace", "")),
    "orca_open": (
        "Abre um workspace no app Orca.",
        _obj({"workspace": S}, ["workspace"]),
        lambda a: a.get("workspace", "")),
    "orca_send": (
        "Digita um texto (e envia) no terminal do agente de um workspace do Orca. "
        "AÇÃO SENSÍVEL: o Jarvis pede confirmação.",
        _obj({"workspace": S, "texto": S}, ["workspace", "texto"]),
        lambda a: f"{a.get('workspace', '')} | {a.get('texto', '')}"),
    "orca_read_terminal": (
        "Lê as últimas linhas do terminal do agente de um workspace do Orca.",
        _obj({"workspace": S, "linhas": {"type": "integer", "description": "quantas linhas (padrão 40)"}},
             ["workspace"]),
        lambda a: a.get("workspace", "")),
    "show_in_orca": (
        "Abre o chat do Jarvis numa aba do Orca. Só quando o Augusto pedir.",
        _obj(), lambda a: ""),
}

MEMORY = {
    "memoria_ler": (
        "Lê a memória do Jarvis (anotações datadas no Obsidian): pendências, decisões e lembretes de projetos. "
        "Use quando ele perguntar o que ficou pendente, o que combinaram, ou antes de contar o andamento de um projeto.",
        _obj({"projeto": {**S, "description": "filtra pelas anotações deste projeto (opcional)"}})),
    "memoria_anotar": (
        "Acrescenta uma anotação datada à memória do Jarvis (nunca apaga nada). Use para lembrar pendências, "
        "decisões e pedidos do Augusto ('me lembra de...', 'anota que...', tarefa que ficou para depois).",
        _obj({"texto": {**S, "description": "anotação curta, uma linha"},
              "projeto": {**S, "description": "projeto relacionado (opcional)"}}, ["texto"])),
}


# ======================= encaminhamento ao Jarvis =======================
def _token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def call_jarvis(name, args):
    """POST /rpc/tool no processo do Jarvis; devolve (ok, texto)"""
    body = json.dumps({"name": name, "args": args, "arg": str(ACTIONS[name][2](args) or "")},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/rpc/tool", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Jarvis-Token": _token()})
    try:
        with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as r:
            j = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return False, f"o Jarvis recusou a ação (HTTP {e.code})"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"não consegui falar com o Jarvis ({e})"
    if isinstance(j, dict) and "result" in j:  # {"ok":..., "result":...} ou envelope do /rpc com result dentro
        res = j["result"]
        if isinstance(res, dict) and "result" in res:
            return bool(res.get("ok", True)), str(res["result"])
        return bool(j.get("ok", True)), res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
    return bool(j.get("ok", False)) if isinstance(j, dict) else False, json.dumps(j, ensure_ascii=False)


# ======================= memória no Obsidian =======================
def _norm(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


def _note_path():
    return os.path.join(VAULT, NOTE)


def memoria_anotar(texto, projeto=""):
    texto = re.sub(r"\s+", " ", texto or "").strip()
    if not texto:
        return False, "anotação vazia"
    path = _note_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = datetime.datetime.now()
    if not os.path.exists(path):
        with open(path, "x", encoding="utf-8") as f:
            f.write(f"---\ncriado: {now:%Y-%m-%d}\ntags: [jarvis]\n---\n\n# Jarvis - memória\n\n"
                    "Anotações do assistente de voz (só acrescenta; nunca apaga).\n\n")
    tag = f" [{projeto.strip()}]" if (projeto or "").strip() else ""
    with open(path, "a", encoding="utf-8") as f:  # só acrescenta
        f.write(f"- {now:%Y-%m-%d %H:%M}{tag} {texto}\n")
    return True, "anotado"


def memoria_ler(projeto="", max_lines=15):
    try:
        with open(_note_path(), encoding="utf-8") as f:
            lines = [ln.rstrip() for ln in f if ln.startswith("- ")]
    except OSError:
        return True, "a memória está vazia"
    if (projeto or "").strip():
        q = _norm(projeto).replace("-", " ")
        lines = [ln for ln in lines if q in _norm(ln).replace("-", " ")]
        if not lines:
            return True, f"nada anotado sobre {projeto}"
    return True, "\n".join(lines[-max_lines:]) or "a memória está vazia"


# ======================= protocolo MCP =======================
def tools_list():
    out = [{"name": n, "description": d, "inputSchema": s} for n, (d, s, _) in ACTIONS.items()]
    out += [{"name": n, "description": d, "inputSchema": s} for n, (d, s) in MEMORY.items()]
    return out


def tools_call(name, args):
    args = args or {}
    try:
        if name == "memoria_ler":
            ok, txt = memoria_ler(args.get("projeto", ""))
        elif name == "memoria_anotar":
            ok, txt = memoria_anotar(args.get("texto", ""), args.get("projeto", ""))
        elif name in ACTIONS:
            ok, txt = call_jarvis(name, args)
        else:
            ok, txt = False, f"ferramenta desconhecida: {name}"
    except Exception as e:  # nunca derruba o servidor
        ok, txt = False, f"erro: {e}"
    return {"content": [{"type": "text", "text": txt or ("ok" if ok else "falhou")}], "isError": not ok}


def handle(msg):
    """Uma mensagem JSON-RPC -> resposta (ou None para notificações)"""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notificação (notifications/initialized etc.)
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "jarvis", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": tools_list()}
    elif method == "tools/call":
        result = tools_call(params.get("name", ""), params.get("arguments"))
    elif method == "ping":
        result = {}
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"método desconhecido: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON inválido"}}
        else:
            msgs = msg if isinstance(msg, list) else [msg]
            resps = [r for r in (handle(m) for m in msgs if isinstance(m, dict)) if r]
            resp = resps if isinstance(msg, list) else (resps[0] if resps else None)
        if resp:
            out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()


if __name__ == "__main__":
    main()
