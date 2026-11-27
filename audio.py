"""Áudio do Jarvis: reprodução robusta, VAD Silero em streaming, palavra de ativação (openWakeWord),
detector de fim de fala adaptativo e interrupção falando (barge-in).

Tudo aqui roda na CPU (Silero ~0,3–0,8 ms por quadro de 32 ms; openWakeWord ~9 ms por quadro de 80 ms, e só
quando o Silero ouve voz); a GPU fica só para o Whisper.
O detector de turno (TurnDetector) não conhece microfone nem Whisper: recebe quadros e devolve eventos,
por isso dá para testá-lo offline com WAVs sintéticos.
"""
import collections
import logging
import os
import re
import threading
import time
import unicodedata

import numpy as np

log = logging.getLogger("jarvis")

SR = 16000
FRAME = 512  # 32 ms: tamanho de quadro do Silero a 16 kHz
FRAME_S = FRAME / SR


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def resample(x, sr_from, sr_to):
    """Reamostra mono float32 (polifásico se houver scipy; senão interpolação linear)"""
    if sr_from == sr_to or len(x) == 0:
        return x.astype(np.float32, copy=False)
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(sr_from), int(sr_to))
        return resample_poly(x, int(sr_to) // g, int(sr_from) // g).astype(np.float32)
    except Exception:
        n = int(round(len(x) * sr_to / sr_from))
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


# ======================= Dispositivos =======================
class Devices:
    """Escolhe o "Mapeador de som da Microsoft" (MME), que segue o dispositivo padrão do Windows: trocar
    fone/caixa não exige reabrir nada. Quando um fluxo falha mesmo assim, reinicia o PortAudio (a lista de
    dispositivos dele é fixa desde a inicialização) de forma combinada com o ouvido, que fecha o microfone antes."""

    lock = threading.Lock()
    generation = 0              # muda a cada reinício do PortAudio; o ouvido reabre o microfone
    reinit_wanted = threading.Event()
    input_closed = threading.Event()
    follow_default = True

    @classmethod
    def _mapper(cls, kind):
        import sounddevice as sd
        try:
            mme = next(i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME")
            ch = "max_output_channels" if kind == "output" else "max_input_channels"
            for i, d in enumerate(sd.query_devices()):
                n = d["name"].lower()
                if d["hostapi"] == mme and d[ch] > 0 and ("mapeador" in n or "mapper" in n):
                    return i
        except Exception:
            pass
        return None

    @classmethod
    def output(cls):
        return cls._mapper("output") if cls.follow_default else None

    @classmethod
    def input(cls):
        return cls._mapper("input") if cls.follow_default else None

    @classmethod
    def reinit(cls, reason):
        """Reinicia o PortAudio. Pede ao ouvido para fechar o microfone antes (espera até 1,5 s)."""
        import sounddevice as sd
        with cls.lock:
            log.warning("reiniciando o PortAudio (%s)", reason)
            cls.input_closed.clear()
            cls.reinit_wanted.set()
            cls.input_closed.wait(1.5)
            try:
                sd._terminate()
            except Exception as e:
                log.warning("PortAudio terminate: %s", e)
            try:
                sd._initialize()
            except Exception as e:
                log.warning("PortAudio initialize: %s", e)
            cls.generation += 1
            cls.reinit_wanted.clear()


# ======================= Reprodução =======================
class Player:
    """Um único fluxo de saída por vez. Toca até o fim real (callback de término do PortAudio, não relógio),
    para na hora com should_stop(), tenta de novo de onde parou se o dispositivo sumir/mudar e reamostra
    se o dispositivo recusar a taxa. Guarda o envelope do que tocou (referência para o barge-in)."""

    def __init__(self):
        self.lock = threading.Lock()          # um fluxo de saída por vez
        self.ref = collections.deque(maxlen=4000)  # (instante em que sai na caixa, rms do bloco)
        self.playing = False
        self.last_error = None
        self.on_ref = None  # EchoCanceller.push_ref: recebe o que tocou, a 16 kHz

    def ref_level(self, t0, t1):
        """Maior rms tocado entre t0 e t1 (instantes de saída na caixa)"""
        best = 0.0
        for t, r in reversed(self.ref):
            if t < t0:
                break
            if t <= t1 and r > best:
                best = r
        return best

    def play(self, samples, sr, should_stop=lambda: False, tries=3):
        """Toca samples (float32 mono). Retorna True se tocou tudo, False se parou ou falhou."""
        import sounddevice as sd
        samples = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        if len(samples) == 0:
            return True
        with self.lock:
            pos = 0  # em amostras do sinal original
            for attempt in range(tries):
                if should_stop():
                    return False
                try:
                    done, pos = self._play_once(sd, samples, sr, pos, should_stop)
                    if done or should_stop():
                        return done
                    raise RuntimeError("fluxo de saída parou sem terminar (dispositivo perdido?)")
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.warning("reprodução falhou (tentativa %d/%d, %.1f s de %.1f s): %s", attempt + 1, tries,
                                pos / sr, len(samples) / sr, self.last_error)
                    if attempt == 0:
                        time.sleep(0.3)  # troca de fone/caixa: o Windows leva um instante
                    else:
                        Devices.reinit(self.last_error)
            log.error("desisti de tocar este áudio: %s", self.last_error)
            return False

    def _open(self, sd, sr, callback, finished):
        dev = Devices.output()
        rates = [sr]
        try:
            native = int(sd.query_devices(dev if dev is not None else sd.default.device[1])["default_samplerate"])
            if native != sr:
                rates.append(native)
        except Exception:
            pass
        err = None
        for rate in rates:
            try:
                st = sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=dev, latency="low",
                                     callback=callback(rate), finished_callback=finished)
                return st, rate
            except Exception as e:  # taxa recusada: tenta a nativa do dispositivo
                err = e
                log.info("saída recusou %d Hz (%s)", rate, e)
        raise err

    def _play_once(self, sd, samples, sr, pos, should_stop):
        finished = threading.Event()
        state = {"i": 0, "last_cb": time.time(), "data": None}

        def make_cb(rate):
            data = samples[pos:] if rate == sr else resample(samples[pos:], sr, rate)
            state["data"], state["rate"] = data, rate
            on_ref = self.on_ref
            data16 = resample(data, rate, SR) if on_ref else None

            def cb(outdata, frames, tinfo, status):
                i = state["i"]
                chunk = data[i:i + frames]
                n = len(chunk)
                outdata[:n, 0] = chunk
                if n < frames:
                    outdata[n:, 0] = 0
                state["i"] = i + n
                if on_ref is not None:
                    try:
                        on_ref(data16[int(i * SR / rate):int((i + frames) * SR / rate)])
                    except Exception:
                        pass
                state["last_cb"] = time.time()
                try:
                    delay = max(0.0, tinfo.outputBufferDacTime - tinfo.currentTime)
                except Exception:
                    delay = 0.0
                self.ref.append((time.time() + delay, float(np.sqrt(np.mean(chunk ** 2))) if n else 0.0))
                if n < frames:
                    raise sd.CallbackStop
            return cb

        st, rate = self._open(sd, sr, make_cb, finished.set)
        self.playing = True
        try:
            with st:
                total = len(state["data"])
                while not finished.wait(0.02):
                    if should_stop():
                        st.abort()
                        return False, pos
                    if time.time() - state["last_cb"] > 1.5:  # o callback parou de ser chamado
                        st.abort()
                        played = int(state["i"] * sr / rate)
                        return False, pos + played
                ok = state["i"] >= total
                return ok, pos + int(state["i"] * sr / rate)
        finally:
            self.playing = False


# ======================= Cancelamento de eco (AEC) =======================
class EchoCanceller:
    """WebRTC AEC3 do pacote livekit (wheel pronta, sem compilar). Recebe o que o Player está tocando
    (referência, 16 kHz) e limpa o microfone. Só trabalha enquanto ele fala; parado, custo zero."""

    def __init__(self):
        from livekit import rtc
        self.rtc = rtc
        self.apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
        self.lock = threading.Lock()
        self.ref = np.zeros(0, np.float32)
        self.cap = np.zeros(0, np.float32)
        self.out = np.zeros(160, np.float32)  # 10 ms de atraso para casar quadros de 512 com blocos de 160

    def _frame(self, x):
        pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
        return self.rtc.AudioFrame(pcm.tobytes(), SR, 1, len(pcm))

    def push_ref(self, x16):
        """Chamado pelo callback de saída com o bloco que acabou de ir para a caixa (16 kHz)"""
        with self.lock:
            self.ref = np.concatenate([self.ref, x16])[-SR:]  # no máximo 1 s guardado (mantém o sincronismo)

    def process(self, frame):
        self.cap = np.concatenate([self.cap, frame])
        outs = [self.out]
        while len(self.cap) >= 160:
            c, self.cap = self.cap[:160], self.cap[160:]
            with self.lock:
                r = self.ref[:160]
                self.ref = self.ref[160:]
            if len(r) < 160:
                r = np.concatenate([r, np.zeros(160 - len(r), np.float32)])
            self.apm.process_reverse_stream(self._frame(r))
            f = self._frame(c)
            self.apm.process_stream(f)
            outs.append(np.frombuffer(bytes(f.data), np.int16).astype(np.float32) / 32767)
        allout = np.concatenate(outs)
        res, self.out = allout[:len(frame)], allout[len(frame):]
        return res

    def idle(self):
        with self.lock:
            self.ref = np.zeros(0, np.float32)


# ======================= Silero VAD =======================
def silero_path():
    import faster_whisper
    return os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")


class SileroVAD:
    """Silero VAD v6 (o ONNX que vem com o faster-whisper) em streaming: 512 amostras a 16 kHz por chamada"""

    CTX = 64

    def __init__(self, path=None):
        import onnxruntime as ort
        o = ort.SessionOptions()
        o.inter_op_num_threads = 1
        o.intra_op_num_threads = 1
        o.log_severity_level = 4
        self.s = ort.InferenceSession(path or silero_path(), providers=["CPUExecutionProvider"], sess_options=o)
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros(self.CTX, np.float32)

    def __call__(self, frame):
        x = np.concatenate([self.ctx, frame.astype(np.float32, copy=False)])[None, :]
        out, self.h, self.c = self.s.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = frame[-self.CTX:].astype(np.float32, copy=True)
        return float(np.asarray(out).reshape(-1)[0])


# ======================= openWakeWord =======================
class WakeWord:
    """openWakeWord ("hey_jarvis", backend onnx, 1 thread). Recebe float32 16 kHz em qualquer tamanho."""

    def __init__(self, model="hey_jarvis"):
        from openwakeword.model import Model
        import openwakeword
        mdir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
        if not any(f.startswith(model) and f.endswith(".onnx") for f in (os.listdir(mdir) if os.path.isdir(mdir) else [])):
            import openwakeword.utils as u  # primeira vez: baixa os modelos do próprio projeto (~4 MB)
            u.download_models(model_names=[model])
        self.name = model
        self.m = Model(wakeword_models=[model], inference_framework="onnx")
        self.buf = np.zeros(0, np.int16)

    def reset(self):
        self.m.reset()
        self.buf = np.zeros(0, np.int16)

    def feed(self, frame):
        """Devolve o maior escore dos quadros de 80 ms completados agora (0 se nenhum)"""
        pcm = (np.clip(frame, -1, 1) * 32767).astype(np.int16)
        self.buf = np.concatenate([self.buf, pcm])
        best = 0.0
        while len(self.buf) >= 1280:
            best = max(best, float(self.m.predict(self.buf[:1280])[self.name]))
            self.buf = self.buf[1280:]
        return best


# ======================= Fim de fala =======================
CONNECTORS = set("e ou o a os as de do da dos das para pra pro pros que com no na nos nas em um uma uns umas mas "
                 "porque tipo entao ai se quando como qual quais pelo pela meu minha seu sua esse essa este esta "
                 "isso aquele aquela ao aos sobre sem ate mais menos muito tambem".split())
SHORT_DONE = {"obrigado", "obrigada", "valeu", "para", "chega", "silencio", "sim", "nao", "pode", "pode aprovar",
              "aprova", "aprovar", "nega", "negar", "cancela", "confirma", "confirmo", "tchau", "pode mandar", "isso",
              "ok", "manda", "claro", "positivo", "negativo", "esquece", "deixa", "pode parar", "para de falar",
              "cala a boca", "ta bom", "beleza", "fechado", "pode sim", "nao precisa", "ate logo"}
_LEAD = {"ei", "hey", "hei", "e", "oi", "ola", "ok", "o"}


def completeness(text, wake_words=()):
    """Quão terminada parece a transcrição parcial:
    2 = certeza (pergunta/exclamação, comando curto, só "Jarvis") → basta end_silence_min_ms
    1 = provável (termina em ponto; o Whisper põe ponto em quase tudo) → end_silence_mid_ms
    0 = não (vírgula, reticências, termina em "o", "de", "pra"...) → espera end_silence_ms"""
    t = (text or "").strip()
    if not t or t.endswith(("...", "…", ",", "-", ":", ";")):
        return 0
    words = norm(t).split()
    while words and (words[0] in _LEAD or words[0] in wake_words or "jarv" in words[0]):
        words = words[1:]
    if not words:
        return 2  # só "Jarvis" / "Ei Jarvis": ele responde "Pois não?"
    if " ".join(words) in SHORT_DONE:
        return 2
    if words[-1] in CONNECTORS:
        return 0
    if t[-1] in "?!":
        return 2
    return 1 if t[-1] == "." and len(words) >= 2 else 0


def looks_complete(text, wake_words=()):
    return completeness(text, wake_words) > 0


# ======================= Detector de turno =======================
class TurnDetector:
    """Máquina de estados por quadro de 32 ms.

    process(frame, now, speaking, armed) devolve uma lista de eventos (tuplas):
      ("wake", score)                         openWakeWord disparou
      ("start", addressed)                    começou a gravar uma fala (addressed: já se sabe que é com ele)
      ("partial", audio, key)                 vale transcrever o que já foi dito (silêncio perto do mínimo)
      ("end", audio, text|None, info)         fala terminou; text vem pronto se a parcial já cobre tudo
      ("barge", ms)                           o usuário começou a falar por cima da voz
      ("drop", secs)                          fala terminou mas não era com ele (modo oww, sem palavra)
    partial_result(key, text) recebe a transcrição parcial (de outra thread).
    """

    def __init__(self, cfg, vad=None, wake=None, player=None, aec=None):
        self.cfg = cfg
        self.vad = vad or SileroVAD()
        self.wake = wake           # None => modo "whisper" (palavra procurada na transcrição)
        self.player = player
        self.aec = aec             # EchoCanceller ou None (só a heurística de eco)
        self.aec_until = 0.0
        self.aec_used = False
        if aec is not None and player is not None:
            player.on_ref = aec.push_ref
        # com AEC o resíduo de eco muda enquanto o filtro converge: o ganho aprendido desce mais rápido
        self.echo_decay = 0.98 if aec is not None else 0.997
        self.lock = threading.Lock()
        self.floor = 0.003
        self.pre = collections.deque(maxlen=int(0.4 / FRAME_S))  # pré-gravação (início da fala)
        self.barge_pre = collections.deque(maxlen=int(1.2 / FRAME_S))
        self.echo_gain = None      # mic_rms / ref_rms aprendido (eco da caixa no microfone)
        self.echo_frames = 0
        self.wake_at = -1e9
        self.wake_score = 0.0
        self.gate_until = 0.0
        self.wake_ring = collections.deque(maxlen=int(0.5 / FRAME_S))
        self.stats = {"vad_ms": 0.0, "oww_ms": 0.0, "frames": 0, "oww_frames": 0}
        self._reset_rec()
        self.barge_run = 0
        self.barge_gap = 0

    # ---------- utilidades ----------
    def _reset_rec(self):
        self.rec = False
        self.buf = []
        self.onset = 0
        self.silence = 0
        self.speech_frames = 0
        self.addressed = False
        self.barged = False
        self.partial_key = None    # nº de quadros quando a parcial foi pedida
        self.partial_text = None
        self.partial_for = None
        self.started_at = 0.0

    def partial_result(self, key, text):
        with self.lock:
            if key == self.partial_key:
                self.partial_text, self.partial_for = text, key

    def _ms(self, frames):
        return frames * FRAME_S * 1000

    # ---------- quadro ----------
    def process(self, frame, now, speaking=False, armed=False, muted=False):
        ev = []
        cfg = self.cfg
        if self.aec is not None and not self.rec:
            if speaking:  # enquanto ele fala (e 0,6 s depois) o microfone passa pelo cancelador de eco
                self.aec_until = now + 0.6
            if now < self.aec_until:
                frame = self.aec.process(frame)
                self.aec_used = True
            elif self.aec_used:
                self.aec.idle()
                self.aec_used = False
        rms = float(np.sqrt(np.mean(frame ** 2)))
        # Silero só quando há algum som (silêncio digital/ambiente fica de graça)
        t = time.perf_counter()
        prob = self.vad(frame) if (rms > 0.0012 or self.rec) else 0.0
        self.stats["vad_ms"] += (time.perf_counter() - t) * 1000
        self.stats["frames"] += 1

        if speaking and not self.rec:
            if cfg.get("barge_in", True) and self.player is not None:
                if self._barge(frame, rms, prob, now):
                    ev.append(("barge", self._ms(self.barge_run)))
                    self._reset_rec()
                    self.rec, self.barged, self.addressed, self.started_at = True, True, True, now
                    self.buf = list(self.barge_pre)
                    self.speech_frames = self.barge_run
                    self.barge_run = self.barge_gap = 0
                    ev.append(("start", True))
            return ev
        self.barge_pre.clear()
        self.barge_run = self.barge_gap = 0
        if muted and not self.rec:  # cauda de eco logo depois que ele fala
            self.pre.clear()
            return ev

        # palavra de ativação: só roda quando o Silero ouve voz (+1,5 s de folga) ou gravando. Ruído de
        # ventilador/estática não liga o detector (e não gera falso positivo). Ao abrir a porta, alimenta
        # antes os últimos 0,5 s guardados, para o detector ouvir o começo do "Ei".
        if self.wake is not None:
            gated = self.rec or now < self.gate_until
            if prob > 0.3:
                self.gate_until = now + 1.5
            if not gated and now < self.gate_until:
                for old in self.wake_ring:
                    self.wake.feed(old)
                self.wake_ring.clear()
                gated = True
            if not gated:
                self.wake_ring.append(frame)
            else:
                t = time.perf_counter()
                sc = self.wake.feed(frame)
                self.stats["oww_ms"] += (time.perf_counter() - t) * 1000
                self.stats["oww_frames"] += 1
                if sc >= cfg.get("wake_threshold", 0.03):
                    if now - self.wake_at > 1.0:
                        ev.append(("wake", sc))
                    if now - self.wake_at > 3.0:
                        self.wake_score = 0.0
                    self.wake_score = max(self.wake_score, sc)
                    self.wake_at = now
                    if self.rec and not self.addressed:
                        self.addressed = True
                        ev.append(("start", True))

        is_speech = prob >= 0.5 and rms > max(self.floor * 2.0, 0.002)
        is_silence = prob < 0.35 or rms < self.floor * 1.5

        if not self.rec:
            # piso de ruído (só fora da fala)
            self.floor = 0.97 * self.floor + 0.03 * rms if rms < self.floor * 2.5 else self.floor * 1.001
            self.pre.append(frame)
            self.onset = self.onset + 1 if is_speech else 0
            if self.onset >= 3:
                self.rec, self.buf, self.silence, self.started_at = True, list(self.pre), 0, now
                self.speech_frames = self.onset
                woke_recent = now - self.wake_at < cfg.get("wake_pre_secs", 1.5)
                self.addressed = self.wake is None or armed or woke_recent
                if not woke_recent:
                    self.wake_score = 0.0
                ev.append(("start", self.addressed))
            return ev

        # gravando
        self.buf.append(frame)
        if armed:
            self.addressed = True
        if is_speech:
            self.speech_frames += 1
        if is_silence:
            self.silence += 1
        elif is_speech:
            self.silence = 0
            with self.lock:  # voltou a falar: a parcial não vale mais
                self.partial_key = self.partial_text = self.partial_for = None
        sil_ms = self._ms(self.silence)
        mx, mn = cfg.get("end_silence_ms", 2500), cfg.get("end_silence_min_ms", 1200)
        want_text = self.addressed or self.wake is None
        # pede a parcial um pouco antes do mínimo, para a resposta dela chegar a tempo
        if want_text and self.partial_key is None and sil_ms >= max(300, mn - cfg.get("partial_lead_ms", 500)) \
                and self.speech_frames >= 4:
            with self.lock:
                self.partial_key = len(self.buf)
            ev.append(("partial", np.concatenate(self.buf), self.partial_key))
        with self.lock:
            ptext, pfor = self.partial_text, self.partial_for
        done = sil_ms >= mx or len(self.buf) * FRAME_S > cfg.get("max_utterance_secs", 20)
        quick = False
        if not done and sil_ms >= mn and ptext is not None:
            level = completeness(ptext, cfg.get("wake_words", ()))
            quick = level == 2 or (level == 1 and sil_ms >= cfg.get("end_silence_mid_ms", (mn + mx) / 2))
        if done or quick:
            audio = np.concatenate(self.buf)
            secs = len(audio) / SR
            info = {"addressed": self.addressed, "barged": self.barged, "silence_ms": sil_ms,
                    "quick": quick, "wake_score": self.wake_score if now - self.wake_at < secs + 3 else 0.0,
                    "speech_ms": self._ms(self.speech_frames)}
            enough = self.speech_frames >= 4 and secs >= 0.35
            if not enough:
                pass
            elif not (self.addressed or self.wake is None):
                ev.append(("drop", secs))
            else:
                # se a parcial ainda vale, ela cobre tudo o que foi falado (depois dela só veio silêncio):
                # é o texto final e não precisa transcrever de novo
                ev.append(("end", audio, ptext, info))
            self._reset_rec()
            self.pre.clear()
            self.onset = 0
            self.vad.reset()
        return ev

    # ---------- barge-in ----------
    def _barge(self, frame, rms, prob, now):
        """Fala do usuário por cima da voz? Compara com o que está tocando (referência) para não disparar com o eco."""
        cfg = self.cfg
        self.barge_pre.append(frame)
        ref = self.player.ref_level(now - cfg.get("echo_delay_max", 0.35), now + 0.03) if self.player else 0.0
        learning = self.echo_frames < cfg.get("echo_learn_frames", 15)
        expected = (self.echo_gain or 0.0) * ref
        thr = max(expected * cfg.get("barge_ratio", 3.0), self.floor * 4.0, cfg.get("barge_min_rms", 0.008))
        cand = (not learning) and prob >= cfg.get("barge_vad", 0.6) and rms > thr
        # aprende o ganho do eco (caixa → microfone) seguindo o PICO da razão mic/referência: sobe rápido,
        # desce devagar. Quadros que já parecem você falando (acima do limiar) não entram na conta.
        if ref > 0.01 and (learning or rms <= thr):
            ratio = rms / ref
            self.echo_frames += 1
            if self.echo_gain is None:
                self.echo_gain = ratio
            elif ratio > self.echo_gain:
                self.echo_gain = 0.7 * self.echo_gain + 0.3 * ratio
            else:
                self.echo_gain = self.echo_decay * self.echo_gain + (1 - self.echo_decay) * ratio
        if cand:
            self.barge_run += 1
            self.barge_gap = 0
        elif self.barge_run:
            self.barge_gap += 1
            if self.barge_gap > 2:
                self.barge_run = self.barge_gap = 0
        return self._ms(self.barge_run) >= cfg.get("barge_in_ms", 350)
