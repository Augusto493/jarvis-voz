"""Jarvis: assistente de voz que controla o app Claude desktop (aba Code).

Ouvido   microfone sempre ligado → detector de fala → Whisper local (GPU quando houver).
Cérebro  `claude -p` persistente em streaming (Haiku por padrão): a primeira frase da resposta já vai
         para a voz enquanto o resto ainda está sendo gerado.
Mãos     UI Automation no app Claude (claude_ui.py).
Voz      edge-tts por frase, decodificada e tocada pelo sounddevice, com a próxima frase sendo
         sintetizada enquanto a anterior toca.
Vigia    acompanha as sessões; quando uma tarefa termina, avisa e pergunta se você está aí (ou só
         notifica, se o microfone estiver mudo).

Ele só fala quando você chama ("Jarvis, ...") ou quando uma tarefa termina.
"""
import asyncio
import ctypes
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _add_cuda_dlls():
    """cuBLAS/cuDNN instalados pelo pip (nvidia-*-cu12) para o Whisper usar a GPU"""
    base = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
        d = os.path.join(base, sub, "bin")
        if os.path.isdir(d):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dlls()
import audio  # noqa: E402
from claude_ui import ClaudeApp  # noqa: E402
from orca_app import OrcaApp  # noqa: E402
from island_ui import IslandUI  # noqa: E402
from confirm import confirm_words, permission_text  # noqa: E402

CONFIG = {
    "user_name": "",           # como o Jarvis te chama; vazio = sem nome
    "wake_words": ["jarvis", "jarves", "jarviz", "djarvis", "jarbas", "javis", "jarvi", "charvis", "jervis", "yarvis",
                   "chavis", "jardis", "garvis", "jarvas"],
    "voice": "pt-BR-AntonioNeural",
    "voice_rate": "+12%",
    "whisper_model_gpu": "large-v3-turbo",
    "whisper_model_cpu": "small",
    "brain_model": "haiku",
    "follow_up_secs": 10,      # depois de ele falar, escuta sem precisar dizer "Jarvis"
    "end_silence_ms": 2500,    # silêncio máximo que encerra a sua fala (frase com pausa no meio espera isso)
    "end_silence_min_ms": 1200,  # basta quando a parcial é pergunta, comando curto ou só "Jarvis"
    "end_silence_mid_ms": 2000,  # basta quando a parcial termina em ponto (provável fim; pausa de pensar é maior)
    "watch_secs": 3,           # de quanto em quanto tempo olha as sessões
    "sample_rate": 16000,
    # ativação: "oww" = detector openWakeWord ("Ei Jarvis") sempre ligado e Whisper só depois do disparo;
    # "whisper" = transcreve toda fala e procura a palavra no texto (cai nele se o openWakeWord não carregar)
    "wake_mode": "oww",
    "wake_threshold": 0.03,    # escore do hey_jarvis que abre a escuta (sotaque pt-BR dá 0,04–0,4; frases sem a palavra < 0,005)
    "wake_sure": 0.5,          # acima disso vale mesmo se o Whisper não escrever "Jarvis"; abaixo, o texto precisa confirmar
    "wake_pre_secs": 1.5,      # disparo até 1,5 s antes do início da fala ainda conta para ela
    "barge_in": True,          # falar por cima da voz dele cala a voz e passa a ouvir o novo pedido
    "barge_in_ms": 350,        # quanto tempo de fala por cima para interromper
    "barge_ratio": 3.0,        # o microfone precisa estar 3x acima do eco esperado da caixa
    "aec": True,               # cancelador de eco WebRTC (pacote livekit) no microfone enquanto ele fala
    "audio_follow_default": True,  # usa o Mapeador de som do Windows: segue a troca de fone/caixa
}
cfg_path = os.path.join(HERE, "config.json")
if os.path.exists(cfg_path):
    CONFIG.update(json.load(open(cfg_path, encoding="utf-8")))

USER_NAME = str(CONFIG.get("user_name") or "").strip()

logging.basicConfig(filename=os.path.join(HERE, "jarvis.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("faster_whisper").setLevel(logging.WARNING)
log = logging.getLogger("jarvis")

STATE = {"paused": False, "busy": False, "follow_until": 0.0, "speech_end": 0.0, "event": ""}
ui = ClaudeApp()
orca = OrcaApp()
island = None  # ilha (janela); criada em main()
ui_lock = threading.Lock()  # UI Automation não é usado por duas threads ao mesmo tempo


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


# ======================= Bandeja / notificação =======================
TRAY = {}
COLORS = {"ouvindo": (40, 200, 120), "pensando": (255, 190, 0), "falando": (90, 160, 255), "pausado": (130, 130, 130),
          "microfone mudo": (200, 80, 80)}


def tray_image(status):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = COLORS.get(status, (200, 80, 80))
    d.ellipse((4, 4, 60, 60), fill=(15, 15, 20, 255), outline=col, width=6)
    d.ellipse((24, 24, 40, 40), fill=col)
    return img


ISLAND_STATE = {"ouvindo": "idle", "pensando": "thinking", "falando": "speaking", "pausado": "paused",
                "microfone mudo": "muted", "carregando reconhecimento de voz": "thinking"}


def set_status(s):
    if s == "ouvindo" and EAR_SILENT["silent"]:
        s = "microfone mudo"
    log.info("status: %s", s)
    if island:
        island.state(ISLAND_STATE.get(s, "idle"), None if s != "carregando reconhecimento de voz" else "Carregando…")
    icon = TRAY.get("icon")
    if icon:
        try:
            icon.title = f"Jarvis — {s}"
            icon.icon = tray_image(s)
        except Exception:
            pass


def notify(title, msg):
    icon = TRAY.get("icon")
    log.info("notificação: %s — %s", title, msg)
    if icon:
        try:
            icon.notify(msg, title)
        except Exception:
            pass


def mic_muted():
    """Microfone padrão mudo no Windows?"""
    try:
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetMicrophone()
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume
        vol = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
        return bool(vol.GetMute())
    except Exception:
        return EAR_SILENT["silent"]


EAR_SILENT = {"silent": False}


# ======================= Voz =======================
audio.Devices.follow_default = CONFIG.get("audio_follow_default", True)


class Voice:
    """Fila de frases: sintetiza a próxima enquanto a atual toca.
    Cada frase leva a "geração" em que foi pedida; stop() troca a geração, então tudo o que estava na fila
    (ou sendo sintetizado) é descartado sem travar as threads. stop(hold=True) também ignora as frases que
    o pedido interrompido ainda mandar, até o pedido acabar ou release() (usado no barge-in)."""

    def __init__(self):
        self.text_q = queue.Queue()
        self.audio_q = queue.Queue(maxsize=4)
        self.pending = 0
        self.lock = threading.Lock()
        self.gen = 0
        self.hold = False
        self.cancel = threading.Event()  # compatibilidade: marcado enquanto a geração atual foi cancelada
        self.player = audio.Player()
        self._sapi = None
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    @property
    def speaking(self):
        return self.pending > 0

    def say(self, text):
        text = for_speech(text)
        if not text:
            return
        if self.hold:
            log.info("fala descartada (interrompido): %s", text)
            return
        log.info("fala: %s", text)
        if island:
            island.said(text)
        with self.lock:
            self.pending += 1
            gen = self.gen
        self.cancel.clear()
        self.text_q.put((gen, text))

    def stop(self, hold=False):
        """Cala agora: corta o áudio tocando e descarta a fila"""
        with self.lock:
            self.gen += 1
        self.cancel.set()
        p = self._sapi
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        if hold:
            self.hold = True

            def release_when_idle():  # o pedido interrompido terminou: volta a falar normalmente
                t = time.time()
                time.sleep(0.5)
                while STATE["busy"] and time.time() - t < 60:
                    time.sleep(0.1)
                self.release()
            threading.Thread(target=release_when_idle, daemon=True).start()

    def release(self):
        self.hold = False

    def wait(self):
        while self.speaking:
            time.sleep(0.03)

    def _done_one(self):
        with self.lock:
            self.pending = max(0, self.pending - 1)
            if self.pending == 0:
                STATE["speech_end"] = time.time()
                STATE["follow_until"] = time.time() + CONFIG["follow_up_secs"]
                set_status("ouvindo")

    def _stale(self, gen):
        return gen != self.gen

    _piper = None

    def _synth(self, text):
        if CONFIG.get("tts") == "piper":  # local, ~0,3 s por frase, menos natural
            if Voice._piper is None:
                from piper import PiperVoice
                Voice._piper = PiperVoice.load(os.path.join(HERE, "voices", "pt_BR-faber-medium.onnx"))
            chunks = list(Voice._piper.synthesize(text))
            return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate
        import edge_tts
        import miniaudio

        async def get():
            buf = bytearray()
            async for c in edge_tts.Communicate(text, CONFIG["voice"], rate=CONFIG["voice_rate"]).stream():
                if c["type"] == "audio":
                    buf += c["data"]
            return bytes(buf)

        mp3 = asyncio.run(get())
        dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=24000)
        return np.frombuffer(dec.samples, dtype=np.float32), 24000

    def _synth_loop(self):
        while True:
            gen, text = self.text_q.get()
            if self._stale(gen):
                self._done_one()
                continue
            t = time.time()
            try:
                item = ("pcm",) + self._synth(text)
                log.info("voz sintetizada em %.2f s", time.time() - t)
            except Exception as e:
                log.warning("síntese falhou (%s); usando voz do Windows", e)
                item = ("sapi", text)
            self.audio_q.put((gen, item))

    def _play_loop(self):
        while True:
            gen, item = self.audio_q.get()
            try:
                if self._stale(gen):
                    continue
                set_status("falando")
                if item[0] == "sapi":
                    ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                          "try{$s.SelectVoice('Microsoft Maria Desktop')}catch{}; $s.Speak([Console]::In.ReadToEnd())")
                    self._sapi = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], stdin=subprocess.PIPE,
                                                  text=True, encoding="utf-8", creationflags=0x08000000)
                    try:
                        self._sapi.communicate(item[1], timeout=120)
                    except subprocess.TimeoutExpired:
                        self._sapi.kill()
                else:
                    _, samples, sr = item
                    if not self.player.play(samples, sr, should_stop=lambda: self._stale(gen)) and not self._stale(gen):
                        log.error("falha ao tocar áudio: %s", self.player.last_error)
            except Exception:
                log.exception("falha ao tocar áudio")
            finally:
                self._sapi = None
                self._done_one()


def for_speech(text):
    """Tira markdown e listas: o texto vai ser falado"""
    t = re.sub(r"[*_`#>]+", "", text or "")
    t = re.sub(r"^\s*(\d+[.)]|[-•])\s*", "", t, flags=re.M)
    t = re.sub(r"\s*\n+\s*", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


voice = Voice()


# ======================= Confirmação =======================
SENSITIVE = {"send": "enviar ao Claude", "orca_send": "enviar ao Orca", "approve": "aprovar a permissão", "deny": "negar a permissão"}
PENDING = {"ev": threading.Event(), "ans": None, "active": False}


def resolve_confirm(yes):
    if PENDING["active"]:
        PENDING["ans"] = bool(yes)
        PENDING["ev"].set()


def ask_confirm(text, timeout=25):
    """Pergunta e espera um sim/não por voz, por texto ou pelos botões da ilha. Sem resposta = não."""
    PENDING.update(active=True, ans=None)
    PENDING["ev"].clear()
    if island:
        island.confirm(text)
    voice.say(text.rstrip(".") + ". Se quiser liberar, diga: Jarvis, confirmo. Ou toque em Sim.")
    PENDING["ev"].wait(timeout)
    ans = PENDING["ans"] is True
    PENDING["active"] = False
    if island:
        island.confirm(None)
    return ans


# ======================= Cérebro =======================
SYSTEM = """Você é Jarvis, o assistente de voz pessoal do usuário. Você controla o app Claude desktop (aba Code)
no computador dele: as sessões da barra lateral são conversas do Claude Code, cada uma num projeto. Ele também
usa o app Orca, onde cada workspace tem um agente (Claude, Codex, Antigravity). Você controla os dois do mesmo jeito;
quando ele disser "no Orca", use as ações orca_*; senão, use as do Claude. Se o nome existir nos dois, pergunte.
Fale como um humano, em português do Brasil, natural, caloroso e direto, como o Jarvis do Homem de Ferro
(nunca o chame de "senhor"). Sua fala vai para um sintetizador de voz.

FORMATO DA RESPOSTA (sempre exatamente assim, nesta ordem):
FALA: <o que dizer em voz alta: uma linha, frases curtas, sem markdown, listas, emojis ou caminhos de arquivo;
       comece pela informação principal; no máximo 3 frases, ou até 60 palavras num resumo de status>
ACOES: <lista JSON de ações, ou []>
OBSERVAR: <sim ou nao>

Ações (executadas na ordem), cada uma {"tool": "...", "arg": "..."}:
- open_session: arg = nome da sessão ou projeto como ouvido (o sistema acha o mais parecido, inclusive dentro de projetos recolhidos)
- new_session: arg = nome do projeto, ou "" para uma sessão nova genérica
- send: arg = texto a enviar ao Claude Code na sessão aberta, escrito como o usuário escreveria, claro e completo
- type: arg = texto para só digitar, sem enviar
- read_conversation: lê as últimas mensagens da sessão aberta
- set_model: arg = "Opus", "Sonnet", "Haiku" ou "Fable" — troca o modelo do chat aberto
- set_effort: arg = "Baixo", "Médio", "Alto" ou "Máximo" — troca o esforço do chat aberto
- orca_status: arg = nome do workspace/projeto no Orca — estado do agente lá, último pedido e última resposta
- orca_open: arg = nome do workspace no Orca — abre no app Orca
- orca_send: arg = "workspace | texto" — digita o texto no terminal desse workspace do Orca
- show_in_orca: abre o chat do Jarvis numa aba do Orca (só quando ele pedir)
- approve / deny: clica em Permitir / Negar num pedido de permissão
- stop: para o Claude que está respondendo
- scroll: arg = "up" ou "down"
- show / minimize: traz a janela do Claude para frente / minimiza

Regras:
- OBSERVAR sim quando precisar ver o resultado das ações antes de concluir (ex.: ler uma conversa). Nesse caso a FALA
  deve ser vazia ou bem curta ("Deixa eu ver."); depois chega o RESULTADO e você responde de novo no mesmo formato.
- "como está o projeto X" / "o que o Claude fez" → open_session X (se não for a aberta) e read_conversation, OBSERVAR sim;
  depois explique como um colega contando o andamento: o que foi feito, o que está rolando, se algo espera o usuário.
  Fale do Claude na terceira pessoa e nunca copie trechos da conversa.
- "peça para o Claude fazer Y no X" → open_session X, send Y; confirme numa frase.
- Só use approve, deny e stop quando ele pedir claramente.
- Quando o CONTEXTO trouxer "evento" (uma tarefa que terminou e você perguntou se ele estava aí), e ele responder
  ("tô aqui", "fala", "o que foi?"), explique o resultado daquela sessão (leia a conversa dela antes).
- Você não tem acesso a arquivos nem internet: só às ações acima. Nunca ofereça buscar em outro lugar.
- O reconhecimento de voz erra grafias ("meu ape" = "Meu App"); use os nomes como aparecem no CONTEXTO.
- Se não entender, pergunte curto. Se for só conversa, responda sem ações.

Exemplos:
Usuário: "como está o Meu App?"
FALA: Deixa eu ver.
ACOES: [{"tool": "open_session", "arg": "Meu App"}, {"tool": "read_conversation", "arg": ""}]
OBSERVAR: sim

Usuário: "muda o modelo pra Sonnet"
FALA: Pronto, trocando para o Sonnet.
ACOES: [{"tool": "set_model", "arg": "Sonnet"}]
OBSERVAR: nao
"""
if USER_NAME:
    SYSTEM += f"\nO nome do usuário é {USER_NAME}; pode chamá-lo assim às vezes.\n"


class Brain:
    def __init__(self):
        self.p = None
        self.lock = threading.Lock()
        self.work = os.path.join(HERE, "work")
        os.makedirs(self.work, exist_ok=True)
        self.mcp = os.path.join(self.work, "empty-mcp.json")
        with open(self.mcp, "w") as f:
            f.write('{"mcpServers":{}}')

    def _start(self):
        import shutil
        # Sem shell: pelo cmd.exe o prompt de sistema era cortado na primeira quebra de linha
        exe = shutil.which("claude") or "claude"
        args = [exe, "-p", "--model", CONFIG["brain_model"], "--input-format", "stream-json",
                "--output-format", "stream-json", "--include-partial-messages", "--verbose",
                "--no-session-persistence", "--strict-mcp-config", "--mcp-config", self.mcp, "--tools", "",
                "--system-prompt", SYSTEM]
        self.p = subprocess.Popen(args, cwd=self.work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=0x08000000)
        log.info("cérebro iniciado (%s, pid %s)", CONFIG["brain_model"], self.p.pid)

    def ask(self, content, on_sentence=None, timeout=60):
        """Envia uma mensagem; cada frase de FALA vai para on_sentence assim que fica pronta"""
        with self.lock:
            if not self.p or self.p.poll() is not None:
                self._start()
            self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                                          ensure_ascii=False) + "\n")
            self.p.stdin.flush()
            text, spoken, t0, first = "", 0, time.time(), None
            for line in self.p.stdout:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("type") == "stream_event":
                    ev = j.get("event", {})
                    delta = ev.get("delta", {}) if ev.get("type") == "content_block_delta" else {}
                    if delta.get("type") == "text_delta":
                        if first is None:
                            first = time.time() - t0
                        text += delta.get("text", "")
                        spoken = self._emit(text, spoken, on_sentence, final=False)
                elif j.get("type") == "result":
                    text = j.get("result") or text
                    self._emit(text, spoken, on_sentence, final=True)
                    log.info("cérebro: 1º token %.2f s, total %.2f s", first or -1, time.time() - t0)
                    return parse_reply(text)
                if time.time() - t0 > timeout:
                    break
            self.p.kill()
            self.p = None
            raise RuntimeError("o cérebro não respondeu")

    @staticmethod
    def _emit(text, spoken, cb, final):
        """Manda para a voz as frases completas da FALA ainda não faladas"""
        if not cb:
            return spoken
        m = re.search(r"FALA:\s*", text)
        if not m:
            return spoken
        body = text[m.end():]
        stop = re.search(r"\n\s*ACOES:|\nACOES:|ACOES:", body)
        complete = stop is not None or final
        if stop:
            body = body[:stop.start()]
        pos = spoken
        while True:
            seg = body[pos:]
            mm = re.search(r"[.!?…](\s|$)", seg)
            if mm and (mm.end() < len(seg) or complete):
                sentence = seg[:mm.end()].strip()
                pos += mm.end()
                if sentence:
                    cb(sentence)
                continue
            if complete and seg.strip():
                cb(seg.strip())
                pos = len(body)
            break
        return pos


def parse_reply(text):
    say = re.search(r"FALA:\s*(.*?)(?:\n\s*ACOES:|ACOES:|$)", text, re.S)
    acts = re.search(r"ACOES:\s*(\[.*?\])\s*(?:\n|OBSERVAR|$)", text, re.S)
    obs = re.search(r"OBSERVAR:\s*(\w+)", text)
    actions = []
    if acts:
        try:
            actions = json.loads(acts.group(1))
        except ValueError:
            pass
    if not say and not acts:  # fora do formato: fala tudo
        return {"say": text.strip(), "actions": [], "observe": False}
    return {"say": (say.group(1).strip() if say else ""), "actions": actions,
            "observe": bool(obs and norm(obs.group(1)).startswith("s"))}


brain = Brain()

TOOLS = {
    "open_session": lambda a: ui.open_session(a),
    "new_session": lambda a: ui.new_session(a or ""),
    "send": lambda a: ui.type_text(a, send=True),
    "type": lambda a: ui.type_text(a, send=False),
    "read_conversation": lambda a: ui.conversation(last=4),
    "set_model": lambda a: ui.set_model(a),
    "set_effort": lambda a: ui.set_effort(a),
    "orca_status": lambda a: orca.status(a),
    "orca_open": lambda a: orca.open(a),
    "orca_send": lambda a: orca.send(*[x.strip() for x in a.split("|", 1)]) if "|" in a else (_ for _ in ()).throw(RuntimeError('formato: "workspace | texto"')),
    "show_in_orca": lambda a: (open_orca_tab(), "abri o chat do Jarvis numa aba do Orca")[1],
    "approve": lambda a: ui.approve(),
    "deny": lambda a: ui.deny(),
    "stop": lambda a: ui.stop(),
    "scroll": lambda a: ui.scroll(a or "down"),
    "show": lambda a: ui.show(),
    "minimize": lambda a: ui.minimize(),
}

_CTX = {"text": "", "at": 0.0}


def build_context():
    try:
        with ui_lock:
            snap = ui.snapshot()
            snap["modelo_do_chat_aberto"] = ui.current_model()
            snap["fim_da_conversa_aberta"] = ui.conversation(last=2, limit=900)
        try:
            snap["orca"] = orca.snapshot()
        except Exception:
            pass
        if STATE["event"]:
            snap["evento"] = STATE["event"]
        return "CONTEXTO: " + json.dumps(snap, ensure_ascii=False)
    except Exception as e:
        return f"CONTEXTO: indisponível ({e})"


def prefetch_context():
    """Chamado quando você começa a falar: o contexto fica pronto junto com a transcrição"""
    def run():
        _CTX["text"], _CTX["at"] = build_context(), time.time()
    threading.Thread(target=run, daemon=True).start()


def fresh_context():
    if time.time() - _CTX["at"] < 8 and _CTX["text"]:
        return _CTX["text"]
    return build_context()


def handle(command):
    STATE["busy"] = True
    set_status("pensando")
    t0 = time.time()
    try:
        first_spoken = {"t": None}

        def speak_now(sentence):
            if first_spoken["t"] is None:
                first_spoken["t"] = time.time() - t0
                log.info("latência até começar a falar: %.2f s", first_spoken["t"])
            voice.say(sentence)

        reply = brain.ask(f"{fresh_context()}\n\nUSUÁRIO DISSE: {command}", on_sentence=speak_now)
        for _round in range(3):
            log.info("resposta: %s", reply)
            results, observe = [], reply.get("observe", False)
            for a in reply.get("actions") or []:
                tool, arg = a.get("tool", ""), str(a.get("arg", "") or "")
                fn = TOOLS.get(tool)
                if not fn:
                    results.append(f"{tool}: ferramenta desconhecida")
                    continue
                if tool in SENSITIVE:
                    voice.wait()
                    req = None
                    if tool == "approve":  # diz o que está na tela, não o texto que o modelo escreveu
                        req = read_permission()
                        what = f"Vou aprovar a permissão: {permission_text(req)}."
                    elif tool == "deny":
                        what = "Vou negar a permissão."
                    else:
                        what = f"Vou {SENSITIVE[tool]}" + (f": {arg[:140]}." if arg else ".")
                    if not ask_confirm(what):
                        results.append(f"{tool}: o usuário NÃO confirmou; não foi executado")
                        log.info("ação %s cancelada", tool)
                        continue
                    if tool == "approve" and read_permission() != req:
                        results.append(f"{tool}: o pedido na tela mudou durante a confirmação; não foi executado")
                        log.info("ação %s cancelada: o pedido mudou", tool)
                        observe = True
                        continue
                try:
                    with ui_lock:
                        out = fn(arg)
                    results.append(f"{tool}({arg[:60]}): {out}")
                except Exception as e:
                    results.append(f"{tool}({arg[:60]}): ERRO {e}")
                    observe = True
                log.info("ação %s", results[-1][:300])
            if not observe:
                break
            set_status("pensando")
            reply = brain.ask("RESULTADO DAS AÇÕES:\n" + "\n".join(results) + "\n\n" + build_context()
                              + "\n\nAgora responda ao usuário no formato. Só inclua novas ações se ainda faltar algo.",
                              on_sentence=voice.say)
        STATE["event"] = ""
        if island:
            island.alert(None)
    except Exception as e:
        log.exception("erro no comando")
        voice.say(f"Tive um problema: {e}")
        if island:
            island.alert(f"Tive um problema: {e}")
    finally:
        voice.wait()
        STATE["busy"] = False
        STATE["follow_until"] = time.time() + CONFIG["follow_up_secs"]
        set_status("ouvindo")


APPROVAL = {"key": None, "req": None}


def read_permission(nodes=None):
    """Pedido de permissão visível no Claude (None se não deu para ler)"""
    try:
        with ui_lock:
            return ui.permission_request(nodes)
    except Exception as e:
        log.warning("ler pedido de permissão: %s", e)
        return None


# ======================= Vigia de tarefas =======================
def watch_sessions():
    """Acompanha Claude e Orca: atualiza as listas da ilha e avisa quando uma tarefa REALMENTE termina.
    Para não abrir a ilha à toa: a tarefa precisa ter sido vista trabalhando por 20 s ou mais, o fim precisa se
    repetir em duas leituras seguidas e cada tarefa só é anunciada uma vez a cada 3 minutos."""
    started, missing, last_ann = {}, {}, {}
    while True:
        time.sleep(CONFIG["watch_secs"])
        if STATE["paused"]:
            continue
        now_t = time.time()
        sessions, ostates, running = None, {}, set()
        try:
            with ui_lock:
                nodes = ui.scan()
                sessions, _ = ui.sidebar(nodes)
                btns = ui.pending_buttons(nodes)
                cur = ui.current_session(nodes)
            check_approval(btns, cur, read_permission(nodes) if btns else None)
            running |= {("Claude", x["name"]) for x in sessions if x["status"] == "Em execução"}
        except Exception:
            pass
        try:
            ostates = orca.states()
            running |= {("Orca", k) for k, v in ostates.items() if v in ("working", "running")}
        except Exception:
            pass
        for key in running:
            started.setdefault(key, now_t)
            missing.pop(key, None)
        for key in list(started):
            if key in running:
                continue
            missing[key] = missing.get(key, 0) + 1
            if missing[key] >= 2:  # duas leituras seguidas sem estar rodando
                ran = now_t - started.pop(key)
                missing.pop(key, None)
                if ran >= 20 and now_t - last_ann.get(key, 0) > 180:
                    last_ann[key] = now_t
                    announce_finished(key[1], key[0])
        if island and sessions is not None:
            cl = [{"n": x["name"], "s": "run" if x["status"] == "Em execução" else ("wait" if x["status"] else "done")}
                  for x in sessions if x["status"]][:5]
            orl = [{"n": k, "s": "run" if v in ("working", "running") else ("wait" if v in ("blocked", "waiting") else "done")}
                   for k, v in list(ostates.items())[:5]]
            island.lists(cl, orl)


def check_approval(btns, cur, req=None):
    """Pedido de permissão do Claude (botões Permitir/Negar visíveis): mostra o cartão Negar/Permitir na ilha"""
    allow = [b for b in btns if norm(b).split(" ")[0] in ("permitir", "aprovar", "allow")]
    if allow:
        key = ("Claude", cur, permission_text(req))
        if APPROVAL["key"] != key:
            APPROVAL.update(key=key, req=req)
            log.info("pedindo permissão: %s — %s", cur, key[2])
            if island:
                island.approval({"w": "Claude", "name": cur or "Claude", "text": "pede sua permissão", "t": time.time(),
                                 "code": key[2]})
                island.open(hold=True)
    elif APPROVAL["key"] and APPROVAL["key"][0] == "Claude":
        APPROVAL["key"] = None
        if island:
            island.approval(None)


def announce_finished(name, where="Claude"):
    log.info("tarefa terminou (%s): %s", where, name)
    notify("Tarefa concluída", f"{name} terminou.")
    if island:
        island.state("done", f"{name} terminou")
        island.open()
    STATE["event"] = (f"A tarefa '{name}' ({where}) acabou de terminar e você perguntou se o usuário estava aí. "
                      f"Para explicar, use {'orca_status' if where == 'Orca' else 'open_session e read_conversation'}.")
    if STATE["paused"] or mic_muted():
        return  # só a notificação: sem voz
    while STATE["busy"]:
        time.sleep(0.3)
    voice.say(f"{USER_NAME + ', o' if USER_NAME else 'O'} {name} terminou. Você está aí?")


# ======================= Uso do plano ao vivo (para o Codenotch) =======================
def feed_usage():
    """Copia, a cada segundo, o indicador "Uso: 66% do limite de 5 horas" do próprio app Claude para
    %APPDATA%/codenotch/claude-live.json. O app atualiza esse número a cada resposta, sem consultar a API
    de uso (que recusa consultas frequentes), então o Codenotch fica igual ao app."""
    path = os.path.join(os.environ.get("APPDATA", ""), "codenotch", "claude-live.json")
    el, last = None, None
    while True:
        time.sleep(1)
        try:
            name = None
            if el is not None:
                try:
                    name = el.CurrentName
                except Exception:
                    el = None
            if not name or not name.startswith("Uso"):
                with ui_lock:
                    node = next((n for n in ui.scan() if n.type == 50000 and n.name.startswith("Uso")), None)
                el = node.el if node else None
                name = node.name if node else None
            if name and name != last:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"at": int(time.time() * 1000), "label": name}, f, ensure_ascii=False)
                os.replace(tmp, path)
                last = name
            elif name:  # mantém o carimbo de tempo fresco
                os.utime(path, None)
        except Exception:
            el = None
            time.sleep(4)


# ======================= Ouvido =======================
class Ear:
    """Microfone sempre ligado → Silero VAD + openWakeWord (CPU, leves) → Whisper (GPU) só para o que é com ele.
    A lógica de fala/silêncio/ativação/barge-in fica em audio.TurnDetector; aqui só ligamos microfone,
    Whisper e on_heard."""

    def __init__(self):
        from faster_whisper import WhisperModel
        set_status("carregando reconhecimento de voz")
        self.model = None
        try:
            self.model = WhisperModel(CONFIG["whisper_model_gpu"], device="cuda", compute_type="int8_float16")
            for _ in range(2):  # a primeira transcrição na GPU leva ~2 s; aquece antes de você falar
                segs, _ = self.model.transcribe((np.random.randn(24000) * 0.05).astype(np.float32), language="pt",
                                                beam_size=1, without_timestamps=True)
                list(segs)
            log.info("Whisper na GPU (%s)", CONFIG["whisper_model_gpu"])
        except Exception as e:
            log.warning("GPU indisponível para o Whisper (%s); usando CPU", e)
            self.model = WhisperModel(CONFIG["whisper_model_cpu"], device="cpu", compute_type="int8")
        self.mode = CONFIG.get("wake_mode", "oww")
        wake = None
        if self.mode == "oww":
            try:
                wake = audio.WakeWord(CONFIG.get("wake_model", "hey_jarvis"))
                log.info("ativação: openWakeWord %s (limiar %.2f)", wake.name, CONFIG["wake_threshold"])
            except Exception as e:
                log.warning("openWakeWord não carregou (%s); procurando a palavra na transcrição", e)
                self.mode = "whisper"
        aec = None
        if CONFIG.get("barge_in", True) and CONFIG.get("aec", True):
            try:
                aec = audio.EchoCanceller()
                log.info("cancelamento de eco: WebRTC AEC3 (livekit)")
            except Exception as e:
                log.warning("cancelador de eco indisponível (%s); barge-in só pela heurística de eco", e)
        self.det = audio.TurnDetector(CONFIG, wake=wake, player=voice.player, aec=aec)
        self._warned = False
        self._armed_at_start = False
        self._prefetch_at = 0.0
        self.q = queue.Queue()
        self.work_q = queue.Queue()
        self.hint = "Jarvis, abre o Meu App. Como está o projeto? Muda o modelo para Sonnet. Claude Code."
        threading.Thread(target=self._stt_loop, daemon=True).start()

    def update_hint(self):
        try:
            with ui_lock:
                snap = ui.snapshot()
            names = ", ".join((snap["sessoes"] + snap["projetos"])[:40])
            self.hint = ("Jarvis, abre o Meu App. Como está o projeto? Muda o modelo para Sonnet. Claude Code. "
                         + names)[:800]
        except Exception:
            pass

    def _transcribe(self, a):
        segs, _ = self.model.transcribe(a, language="pt", beam_size=1, vad_filter=False,
                                        initial_prompt=self.hint, condition_on_previous_text=False,
                                        without_timestamps=True)
        return " ".join(s.text for s in segs).strip()

    def _stt_loop(self):
        """Única thread que usa a GPU. Parciais velhas (você voltou a falar) são puladas."""
        while True:
            kind, a, extra = self.work_q.get()
            if kind == "partial" and extra != self.det.partial_key:
                continue
            t = time.time()
            try:
                text = self._transcribe(a)
            except Exception:
                log.exception("transcrição falhou")
                continue
            secs = len(a) / CONFIG["sample_rate"]
            if kind == "partial":
                log.info("parcial em %.2f s (%.1f s de áudio): %s", time.time() - t, secs, text)
                self.det.partial_result(extra, text)
                continue
            log.info("transcrito em %.2f s (%.1f s de áudio)", time.time() - t, secs)
            if text:
                on_heard(text, extra)

    def _prefetch(self):
        if time.time() - self._prefetch_at > 4:
            self._prefetch_at = time.time()
            prefetch_context()

    def _event(self, ev, armed):
        kind = ev[0]
        if kind == "wake":
            log.info("ativação ouvida (escore %.2f)", ev[1])
            self._prefetch()
            if island:
                island.state("listening")
        elif kind == "start":
            self._armed_at_start = armed
            if ev[1]:
                self._prefetch()
                if island:
                    island.state("listening")
        elif kind == "partial":
            self.work_q.put(("partial", ev[1], ev[2]))
        elif kind == "end":
            _, a, text, info = ev
            info["armed"] = self._armed_at_start or armed
            log.info("fim da fala: %.1f s de silêncio%s", info["silence_ms"] / 1000,
                     " (frase completa: fim antecipado)" if info["quick"] else "")
            if text is not None:  # a transcrição parcial já cobre a fala inteira: não transcreve de novo
                log.info("transcrito (parcial reaproveitada, %.1f s de áudio)", len(a) / CONFIG["sample_rate"])
                if text:
                    threading.Thread(target=on_heard, args=(text, info), daemon=True).start()
            else:
                self.work_q.put(("final", a, info))
        elif kind == "barge":
            log.info("interrompido pela sua voz (%.0f ms falando por cima)", ev[1])
            voice.stop(hold=True)
            STATE["follow_until"] = time.time() + CONFIG["follow_up_secs"]
            if island:
                island.state("listening")

    def _check_silent(self, chunk):
        self._peak = max(self._peak, float(np.abs(chunk).max()))
        if time.time() - self._since <= 10:
            return
        EAR_SILENT["silent"] = self._peak < 2e-4  # silêncio digital = microfone mudo
        self._peak, self._since = 0.0, time.time()
        if EAR_SILENT["silent"] and not self._warned:
            self._warned = True
            notify("Jarvis", "Seu microfone está mudo: não consigo te ouvir. Escreva no chat da ilha.")
            if island:
                island.alert("Seu microfone do Windows está mudo, então não consigo te ouvir. Escreva aqui no chat.")
            set_status("ouvindo")
        elif not EAR_SILENT["silent"] and self._warned:
            self._warned = False
            if island:
                island.alert(None)
            set_status("ouvindo")

    def listen_forever(self):
        """Abre o microfone (o padrão do Windows) e reabre sozinho se ele sumir, trocar ou o PortAudio reiniciar"""
        fails = 0
        while True:
            try:
                self._listen_once()
                fails = 0
            except Exception as e:
                fails += 1
                log.warning("microfone: %s (tentativa %d)", e, fails)
                time.sleep(min(3, 0.5 * fails))
                if fails % 3 == 0:
                    audio.Devices.reinit(f"microfone falhou {fails}x")

    def _listen_once(self):
        sr = CONFIG["sample_rate"]
        q = self.q = queue.Queue()

        def cb(indata, frames, t, status):
            q.put((time.time(), indata[:, 0].copy()))

        stream = sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=audio.FRAME,
                                device=audio.Devices.input(), callback=cb)
        with stream:
            set_status("ouvindo")
            self._peak, self._since = 0.0, time.time()
            while True:
                if audio.Devices.reinit_wanted.is_set():
                    break  # o tocador vai reiniciar o PortAudio: fecha o microfone antes
                try:
                    ts, chunk = q.get(timeout=2)
                except queue.Empty:
                    raise RuntimeError("o microfone parou de mandar áudio (dispositivo trocado ou desligado?)")
                self._check_silent(chunk)
                if STATE["paused"]:
                    self.det._reset_rec()
                    continue
                armed = time.time() < STATE["follow_until"] or PENDING["active"]
                muted = ts < STATE["speech_end"] + 0.3  # cauda de eco logo depois que ele fala
                for ev in self.det.process(chunk, ts, speaking=voice.speaking, armed=armed, muted=muted):
                    self._event(ev, armed)
                if island and self.det.rec and self.det.addressed and len(self.det.buf) % 3 == 0:
                    island.level(min(1.0, float(np.sqrt(np.mean(chunk ** 2))) * 30))
        audio.Devices.input_closed.set()
        t = time.time()
        while audio.Devices.reinit_wanted.is_set() and time.time() - t < 5:
            time.sleep(0.05)


def on_chat(text):
    """Texto digitado na ilha ou na aba do Orca: sem palavra de ativação, mesmo caminho da voz"""
    text = (text or "").strip()
    if not text:
        return
    log.info("chat: %s", text)
    if island:
        island.msg("user", text)
    if PENDING["active"]:
        ans = confirm_words(text, CONFIG["wake_words"], typed=True)
        if ans is not None:
            return resolve_confirm(ans)
    if STATE["busy"]:
        if island:
            island.msg("jarvis", "Um instante, ainda estou terminando o pedido anterior.")
        return
    threading.Thread(target=handle, args=(text,), daemon=True).start()


def on_open_item(kind, name):
    """Clique num cartão de sessão da ilha: abre no Claude ou no Orca"""
    try:
        if kind == "Orca":
            orca.open(name)
        else:
            with ui_lock:
                ui.open_session(name)
    except Exception as e:
        log.warning("abrir %s/%s: %s", kind, name, e)
        if island:
            island.alert(f"Não consegui abrir {name}: {e}")


def on_decide(yes):
    """Botões Permitir / Negar da ilha: ou respondem a uma confirmação do Jarvis, ou clicam no pedido de permissão do Claude"""
    if PENDING["active"]:
        return resolve_confirm(yes)
    try:
        if yes and read_permission() != APPROVAL["req"]:
            APPROVAL["key"] = None  # o vigia mostra o pedido atual de novo
            raise RuntimeError("o pedido na tela mudou; confira e clique de novo")
        with ui_lock:
            (ui.approve if yes else ui.deny)()
        log.info("permissão %s pela ilha", "permitida" if yes else "negada")
    except Exception as e:
        log.warning("decidir permissão: %s", e)
        if island:
            island.alert(f"Não consegui clicar no botão do Claude: {e}")
    if island:
        island.approval(None)
    APPROVAL["key"] = None


STOP_ONLY = {"para", "chega", "silencio", "cala a boca", "para de falar", "pode parar", "ok", "ta bom", "ta", "beleza",
             "tudo bem", "entendi", "ja entendi", "certo"}


def _run_after_busy(command):
    """Pedido feito por cima da voz: espera o pedido interrompido terminar (as falas dele estão sendo descartadas)"""
    t = time.time()
    while STATE["busy"] and time.time() - t < 30:
        time.sleep(0.1)
    voice.release()
    handle(command)


def on_heard(text, info=None):
    info = info or {}
    log.info("ouvi: %s", text)
    if PENDING["active"]:
        ans = confirm_words(text, CONFIG["wake_words"])
        if ans and voice.speaking:
            ans = None  # pode ser o eco da própria pergunta
        if ans is not None:
            if island:
                island.heard(text.strip())
            return resolve_confirm(ans)
    words = norm(text).split()
    in_conversation = time.time() < STATE["follow_until"] or info.get("barged") or info.get("armed")
    wake_idx = next((i for i, w in enumerate(words[:4]) if w in CONFIG["wake_words"]), None)
    if wake_idx is None and not in_conversation:
        if info.get("wake_score", 0) < CONFIG.get("wake_sure", 0.5):
            if info.get("wake_score"):
                log.info("ativação não confirmada pela transcrição (escore %.2f): ignorado", info["wake_score"])
            return  # não falaram com ele: fica quieto
        wake_idx = -1  # o detector tem certeza, mesmo que o Whisper não tenha escrito "Jarvis"
    command = text.strip()
    if wake_idx is not None and wake_idx >= 0:
        command = re.sub(r"^\W*(?:\w+\W+){0,%d}?\w*(?:jarv|jarb|javi|jerv|yarv|chav|jard|charv|djarv|garv)\w*\W*" % wake_idx,
                         "", text, count=1, flags=re.I).strip()
    n = norm(command)
    if info.get("barged") and n in STOP_ONLY:
        log.info("interrupção: só calar")
        return  # a voz já foi calada no barge-in
    if STATE["busy"]:
        if info.get("barged"):
            if island:
                island.heard(text.strip())
            threading.Thread(target=_run_after_busy, args=(command,), daemon=True).start()
            return
        if wake_idx is not None and words[-1:] in (["para"], ["chega"], ["silencio"]):
            voice.stop()
        return
    voice.release()
    if island:
        island.heard(text.strip())
    if n in ("", "oi", "ola", "e ai", "ta ai", "esta ai", "ei", "hey"):
        voice.say("Pois não?")
        return
    if n in ("obrigado", "valeu", "pode parar", "ate logo", "tchau", "nada", "deixa", "esquece", "nao"):
        STATE["follow_until"] = 0
        STATE["event"] = ""
        if n not in ("nada", "nao", "deixa", "esquece"):
            voice.say("Por nada.")
        STATE["follow_until"] = 0
        return
    threading.Thread(target=handle, args=(command,), daemon=True).start()


# ======================= Principal =======================
def run_tray():
    import pystray

    def toggle(icon, item):
        toggle_pause()

    def quit_(icon, item):
        icon.stop()
        os._exit(0)

    icon = pystray.Icon("jarvis", tray_image("ouvindo"), "Jarvis", menu=pystray.Menu(
        pystray.MenuItem(lambda i: "Retomar" if STATE["paused"] else "Pausar", toggle),
        pystray.MenuItem("Abrir aba no Orca", lambda i, it: threading.Thread(target=open_orca_tab, daemon=True).start()),
        pystray.MenuItem("Abrir log", lambda i, it: os.startfile(os.path.join(HERE, "jarvis.log"))),
        pystray.MenuItem("Sair", quit_)))
    TRAY["icon"] = icon
    icon.run()


def run_all():
    threading.Thread(target=run_tray, daemon=True).start()
    threading.Thread(target=feed_usage, daemon=True).start()
    threading.Thread(target=lambda: voice._synth("Pronto."), daemon=True).start()  # carrega a voz antes do 1º uso
    threading.Thread(target=lambda: brain.ask("Inicialização: responda só 'FALA:' vazio, 'ACOES: []' e 'OBSERVAR: nao'."),
                     daemon=True).start()
    island.ready.wait(20)
    island.start_cursor_feed()
    island.keep_on_top()
    ear = Ear()
    ear.update_hint()
    threading.Thread(target=watch_sessions, daemon=True).start()

    def refresh_hint():
        while True:
            time.sleep(120)
            ear.update_hint()

    threading.Thread(target=refresh_hint, daemon=True).start()
    while True:
        try:
            ear.listen_forever()
        except Exception:
            log.exception("microfone falhou; tentando de novo em 3 s")
            time.sleep(3)


def open_orca_tab():
    """Abre a mesma ilha numa aba do navegador do Orca (uma só: não repete se já existir)"""
    import web_server
    url = f"http://127.0.0.1:{web_server.PORT}/?embed=1"
    try:
        if not orca.available():
            return
        from orca_app import _run
        tabs = _run(["tab", "list"])
        if "127.0.0.1:%d" % web_server.PORT in json.dumps(tabs):
            return
        _run(["tab", "create", "--url", url, "--worktree", "active"])
        log.info("aba do Jarvis aberta no Orca")
    except Exception as e:
        log.warning("não abri a aba no Orca: %s", e)


def toggle_pause():
    STATE["paused"] = not STATE["paused"]
    set_status("pausado" if STATE["paused"] else "ouvindo")


def main():
    global island
    ctypes.windll.kernel32.CreateMutexW(None, False, "JarvisClaudeVoice")
    if ctypes.windll.kernel32.GetLastError() == 183:  # já está rodando
        return
    island = IslandUI(on_pause=toggle_pause, on_stop=voice.stop, on_chat=on_chat, on_confirm=resolve_confirm,
                      on_open=on_open_item, on_decide=on_decide)
    island.create()
    island.start(run_all)  # a janela ocupa a thread principal; o resto roda em threads


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--say":  # teste sem microfone: py jarvis.py --say "Jarvis, ..."
        logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
        brain.ask("Inicialização: responda só 'FALA:' vazio, 'ACOES: []' e 'OBSERVAR: nao'.")  # como no app, já aquecido
        for phrase in sys.argv[2:]:
            on_heard(phrase)
            time.sleep(0.5)
            while STATE["busy"] or voice.speaking:
                time.sleep(0.2)
    else:
        main()
