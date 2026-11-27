"""Cérebro do Jarvis com ferramentas de verdade (MCP).

Um processo `claude -p` persistente em stream-json. O texto que o modelo escreve é o que o Jarvis FALA (as frases
completas vão para on_sentence assim que chegam); as ações são chamadas de ferramenta reais do servidor MCP
jarvis_mcp.py, que as encaminha ao Jarvis por POST /rpc/tool. Não há mais formato "FALA:/ACOES:/OBSERVAR:".

Segurança: o cérebro só enxerga as ferramentas do MCP "jarvis" (--tools "" tira Bash/Edit/Read/etc.,
--strict-mcp-config ignora outros MCPs, --allowedTools libera só mcp__jarvis e --permission-mode dontAsk nega o
resto sem perguntar). As ações sensíveis (send, orca_send, approve, deny) pedem confirmação do lado do Jarvis,
no handler do /rpc/tool; se o Augusto não confirmar, a ferramenta devolve isso como resultado e o modelo fala.

Uso (mesma interface de antes):
    brain = Brain(CONFIG["brain_model"])
    reply = brain.ask(f"{fresh_context()}\\n\\nAUGUSTO DISSE: {cmd}", on_sentence=speak_now)
    reply == {"say": "...", "actions": [], "observe": False, "tools": [{"tool": "open_session", "input": {...},
              "result": "...", "ok": True}], "first_sentence": 1.2, "total": 3.4}
"""
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("jarvis")
SERVER = "jarvis"  # nome do servidor MCP -> ferramentas aparecem como mcp__jarvis__<nome>
PREFIX = f"mcp__{SERVER}__"

SYSTEM = """Você é Jarvis, o assistente de voz pessoal do Augusto. Tudo o que você escreve é falado em voz alta por um
sintetizador, em português do Brasil.

COMO FALAR
- Natural, caloroso e direto, como o Jarvis do Homem de Ferro. Pode chamá-lo de "Augusto" às vezes, nunca de "senhor".
- Respostas curtas: no máximo 3 frases (até 60 palavras num resumo de andamento). Comece pela informação principal.
- Nada de markdown, listas, asteriscos, emojis, títulos, código ou caminhos de arquivo. Só frases faladas.
- Antes de uma ação demorada (ler conversa, abrir sessão, consultar o Orca) diga uma frase curta como "Deixa eu ver."
  e só então chame a ferramenta. Depois do resultado, responda de novo, curto.
- Ações rápidas e óbvias (trocar modelo, rolar, minimizar): chame a ferramenta e confirme numa frase curta depois.

O QUE VOCÊ CONTROLA (pelas ferramentas; nunca descreva uma ação sem chamar a ferramenta)
- O app Claude desktop, aba Code: as sessões da barra lateral são conversas do Claude Code, cada uma num projeto.
  open_session, new_session, read_conversation, tasks, status, send, type, set_model, set_effort, approve, deny,
  stop, scroll, show, minimize.
- O app Orca, onde cada workspace tem um agente (Claude, Codex, Antigravity): orca_status, orca_open,
  orca_send, orca_read_terminal, show_in_orca. Quando ele disser "no Orca", use as orca_*; senão, as do Claude.
  Se o nome existir nos dois, pergunte.
- Sua memória (anotações no Obsidian): memoria_ler e memoria_anotar.

REGRAS
- Nunca invente estado. Se não souber o que está rodando, o que o Claude fez ou qual sessão está aberta, pergunte à
  ferramenta (status, read_conversation, tasks, orca_status) em vez de chutar. Use o CONTEXTO da mensagem quando ele
  já responder.
- "abre o X" -> open_session X. "nova sessão no X" -> new_session X.
- "como está o projeto X" / "o que o Claude fez" -> open_session X (se não for a aberta) e read_conversation; depois
  conte como um colega: o que foi feito, o que está rolando, se algo espera o Augusto. Fale do Claude na terceira
  pessoa e nunca copie trechos da conversa. Se fizer sentido, consulte memoria_ler do projeto.
- "pede pro Claude fazer Y no X" -> open_session X e send Y (texto claro e completo, como o Augusto escreveria).
- send, orca_send, approve e deny pedem confirmação ao Augusto automaticamente; não peça confirmação você mesmo.
  Se o resultado disser que ele não confirmou, diga que não fez e pronto.
- Só use approve, deny e stop quando ele pedir claramente.
- Memória: quando ele pedir para lembrar algo, ou ficar uma pendência/decisão de um projeto, use memoria_anotar com o
  nome do projeto. Quando perguntar o que ficou pendente, use memoria_ler.
- Quando o CONTEXTO trouxer "evento" (uma tarefa que terminou e você perguntou se ele estava aí) e ele responder
  ("tô aqui", "fala", "o que foi?"), leia a conversa daquela sessão e explique o resultado.
- O reconhecimento de voz erra grafias ("meu ape" = "Meu App"); use os nomes como aparecem no CONTEXTO.
- Você não tem acesso a arquivos nem à internet, só às ferramentas. Nunca ofereça buscar em outro lugar.
- Se uma ferramenta falhar, diga em uma frase o que houve. Se não entender o pedido, pergunte curto.
- Se for só conversa, responda sem ferramentas.
"""

_SENT_END = re.compile(r"[.!?…](?=\s)")


def _clean(s):
    """Tira restos de markdown: o texto vai ser falado"""
    s = re.sub(r"[*_`#>]+", "", s)
    s = re.sub(r"^\s*(\d+[.)]|[-•])\s+", "", s, flags=re.M)
    return re.sub(r"\s+", " ", s).strip()


def _python_console():
    """python.exe (não pythonw.exe) para o servidor MCP stdio"""
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(alt):
            return alt
    return exe


class Brain:
    def __init__(self, model="haiku", system=SYSTEM, port=None, env=None, work=None, timeout=150):
        self.model = model
        self.system = system
        self.timeout = timeout  # inclui a espera da confirmação por voz dentro de uma ferramenta
        self.p = None
        self.q = None
        self.lock = threading.Lock()
        self.work = work or os.path.join(HERE, "work")
        os.makedirs(self.work, exist_ok=True)
        self.extra_env = dict(env or {})
        if port:
            self.extra_env["JARVIS_PORT"] = str(port)
        self.mcp = os.path.join(self.work, "jarvis-mcp.json")
        self._write_mcp_config()

    # ---------- processo ----------
    def _write_mcp_config(self):
        """Config do MCP com caminhos absolutos (a pasta tem espaço e acento: vai como JSON UTF-8, sem shell)"""
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        for k in ("JARVIS_PORT", "JARVIS_TOKEN_FILE", "JARVIS_HOME", "JARVIS_VAULT"):
            v = self.extra_env.get(k) or os.environ.get(k)
            if v:
                env[k] = v
        cfg = {"mcpServers": {SERVER: {"type": "stdio", "command": _python_console(),
                                       "args": ["-u", os.path.join(HERE, "jarvis_mcp.py")], "env": env}}}
        with open(self.mcp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)

    def _start(self):
        exe = shutil.which("claude") or "claude"
        # Sem shell: pelo cmd.exe o --system-prompt era cortado na primeira quebra de linha
        args = [exe, "-p", "--model", self.model,
                "--input-format", "stream-json", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose", "--no-session-persistence",
                "--mcp-config", self.mcp, "--strict-mcp-config",
                "--tools", "",                       # nenhuma ferramenta embutida (Bash, Edit, Read...)
                "--allowedTools", f"mcp__{SERVER}",  # todas as ferramentas do MCP jarvis, sem perguntar
                "--permission-mode", "dontAsk",      # o que não estiver liberado é negado
                "--disable-slash-commands",
                "--system-prompt", self.system]
        env = dict(os.environ, **self.extra_env, JARVIS_BRAIN="1")  # os ganchos ignoram o cérebro
        self.p = subprocess.Popen(args, cwd=self.work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.p, self.q), daemon=True).start()
        log.info("cérebro MCP iniciado (%s, pid %s)", self.model, self.p.pid)

    @staticmethod
    def _reader(p, q):
        try:
            for line in p.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)  # fim: o processo caiu

    def _alive(self):
        return self.p is not None and self.p.poll() is None

    def close(self):
        if self.p:
            try:
                self.p.kill()
            except Exception:
                pass
        self.p = None

    def warm(self):
        """Sobe o processo (e o MCP) antes do primeiro pedido, sem gastar uma chamada ao modelo"""
        with self.lock:
            if not self._alive():
                self._start()

    # ---------- conversa ----------
    def ask(self, content, on_sentence=None, timeout=None, _retry=True):
        """Envia uma mensagem e espera o fim do turno (com as ferramentas que o modelo chamar no caminho).
        Cada frase completa do texto vai para on_sentence assim que chega."""
        timeout = timeout or self.timeout
        with self.lock:
            if not self._alive():
                self._start()
            while not self.q.empty():  # restos de um turno anterior abortado
                self.q.get_nowait()
            try:
                self._send(content)
            except OSError:  # o processo morreu entre um pedido e outro: reinicia e tenta uma vez
                self.close()
                if not _retry:
                    raise RuntimeError("o cérebro não respondeu")
                self._start()
                self._send(content)
            return self._turn(on_sentence, timeout, content, _retry)

    def _send(self, content):
        self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                                      ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def _turn(self, on_sentence, timeout, content, _retry):
        t0 = time.time()
        st = {"blocks": {}, "said": [], "first": None, "first_tok": None}
        tools, pending = [], {}

        def emit(sentence):
            sentence = _clean(sentence)
            if not sentence:
                return
            if st["first"] is None:
                st["first"] = time.time() - t0
            st["said"].append(sentence)
            if on_sentence:
                try:
                    on_sentence(sentence)
                except Exception:
                    log.exception("on_sentence falhou")

        def flush(b, final):
            """Manda as frases completas do bloco de texto b; no fim do bloco, manda o resto"""
            while True:
                seg = b["text"][b["pos"]:]
                m = _SENT_END.search(seg)
                if not m:
                    break
                emit(seg[:m.end()])
                b["pos"] += m.end()
            if final:
                emit(b["text"][b["pos"]:])
                b["pos"] = len(b["text"])

        while True:
            left = timeout - (time.time() - t0)
            try:
                line = self.q.get(timeout=max(0.1, left))
            except queue.Empty:
                line = "TIMEOUT"
            if line is None or line == "TIMEOUT":
                self.close()
                log.warning("cérebro %s", "caiu" if line is None else "demorou demais")
                if line is None and _retry and not st["said"] and not tools:
                    self._start()  # reinício automático e uma nova tentativa
                    self._send(content)
                    return self._turn(on_sentence, timeout, content, False)
                raise RuntimeError("o cérebro não respondeu")
            try:
                j = json.loads(line)
            except ValueError:
                continue
            typ = j.get("type")
            if typ == "stream_event":
                ev = j.get("event", {})
                et = ev.get("type")
                if et == "message_start":
                    st["blocks"] = {}
                elif et == "content_block_start":
                    cb = ev.get("content_block", {})
                    st["blocks"][ev.get("index")] = {"type": cb.get("type"), "text": "", "pos": 0}
                elif et == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        b = st["blocks"].setdefault(ev.get("index"), {"type": "text", "text": "", "pos": 0})
                        if st["first_tok"] is None:
                            st["first_tok"] = time.time() - t0
                        b["text"] += d.get("text", "")
                        flush(b, False)
                elif et == "content_block_stop":
                    b = st["blocks"].get(ev.get("index"))
                    if b and b["type"] == "text":
                        flush(b, True)
            elif typ == "assistant":  # mensagem completa: pega as chamadas de ferramenta com o input inteiro
                for c in (j.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        name = c.get("name", "")
                        call = {"tool": name[len(PREFIX):] if name.startswith(PREFIX) else name,
                                "input": c.get("input") or {}, "result": None, "ok": None}
                        tools.append(call)
                        pending[c.get("id")] = call
                        log.info("cérebro chamou %s %s", call["tool"],
                                 json.dumps(call["input"], ensure_ascii=False)[:200])
            elif typ == "user":  # resultados das ferramentas
                for c in (j.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                        res = c.get("content")
                        if isinstance(res, list):
                            res = " ".join(x.get("text", "") for x in res if isinstance(x, dict))
                        call = pending.pop(c["tool_use_id"])
                        call["result"], call["ok"] = str(res or ""), not c.get("is_error")
            elif typ == "result":
                for b in st["blocks"].values():  # segurança: algum bloco sem content_block_stop
                    if b["type"] == "text" and b["pos"] < len(b["text"]):
                        flush(b, True)
                if not st["said"] and j.get("result"):  # sem streaming parcial: fala o resultado inteiro
                    for s in re.split(r"(?<=[.!?…])\s+", j["result"]):
                        emit(s)
                total = time.time() - t0
                log.info("cérebro: 1º token %.2f s, 1ª frase %.2f s, total %.2f s, %d ferramenta(s)",
                         st["first_tok"] or -1, st["first"] or -1, total, len(tools))
                return {"say": " ".join(st["said"]), "actions": [], "observe": False, "tools": tools,
                        "first_sentence": st["first"], "total": total, "error": j.get("is_error", False)}
