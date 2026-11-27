"""Janela da ilha do Jarvis (pywebview/WebView2): ilha preta no topo central da tela, sem moldura, sempre
por cima. Passar o mouse mostra a compacta; abre inteira só por clique, por J.open() ou por pedido de
permissão/confirmação. Python empurra o estado para o JS (window.J.*).

Tamanhos (iguais a SIZES em ui/island.html, vindos do core/layout.ts do Coucou, código MIT de Louis-CFM/coucou):
fechada 184x32 r10 · compacta 288x32 r14 · visão geral 640x160 · permissão 640x168 · chat 640x256 (300 com
conversa) · nova sessão 640x196 (aberta r22). O JS pede o tamanho com resize(w, h, raio); aqui ele é animado com
a mola do Coucou (core/anim.ts: resposta 0,5 s, amortecimento 0,72 ao crescer; curva (.45,0,.2,1) em 340 ms
ao encolher)."""
import ctypes
import ctypes.wintypes
import inspect
import json
import math
import os
import threading
import time

os.environ.setdefault("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "00FFFFFF")  # fundo do WebView2 transparente
import webview
import web_server

HERE = os.path.dirname(os.path.abspath(__file__))
W_CLOSED, H_CLOSED = 184, 32   # tamanhos do Coucou para Windows (layout.ts)
SIZES = {"closed": (184, 32, 10), "compact": (288, 32, 14), "overview": (640, 160, 22), "approval": (640, 168, 22),
         "chat": (640, 256, 22), "chatFull": (640, 300, 22), "new": (640, 196, 22)}


_UI = None  # o pywebview percorre os atributos do js_api recursivamente: a ponte não pode guardar a janela


class _Api:
    def resize(self, w, h, r=14):
        _UI._resize(int(w), int(h), int(r))

    def open_item(self, kind, name, id=None):
        _h("open_item", kind, name, id)

    def decide(self, yes, id=None):
        _h("decide", yes, id)

    def release(self, id=None):
        """"Responder no app": solta o pedido de permissão sem decidir"""
        _h("release", id)

    def focus(self):
        _UI._focus()

    def toggle_pause(self):
        _h("toggle_pause")

    def stop_speaking(self):
        _h("stop_speaking")

    def chat(self, text):
        _h("chat", text)

    def confirm(self, yes):
        _h("confirm", yes)


def _h(name, *args):
    fn = web_server.HUB.handlers.get(name)
    if fn:
        threading.Thread(target=lambda: fn(*args), daemon=True).start()


def _lenient(fn):
    """A página pode mandar argumentos novos (ex.: o id do pedido): corta o que o handler não aceita, para
    on_decide(yes) e on_open_item(kind, name) antigos continuarem funcionando (também pela aba do Orca, /api)."""
    try:
        ps = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn
    if any(p.kind == p.VAR_POSITIONAL for p in ps):
        return fn
    n = sum(1 for p in ps if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    return lambda *a: fn(*a[:n])


def _spring_path(a, b, response=0.5, damping=0.72, dt=1 / 60):
    """Valor a cada quadro da mola do Coucou (SwiftUI .spring(response, dampingFraction)), de a até b"""
    w0, v, x, out = 2 * math.pi / response, 0.0, float(a), []
    for _ in range(90):
        for _ in range(4):
            acc = w0 * w0 * (b - x) - 2 * damping * w0 * v
            v += acc * dt / 4
            x += v * dt / 4
        out.append(x)
        if abs(b - x) < 0.5 and abs(v) < 2:
            break
    out[-1] = b
    return out


def _close_path(a, b, ms=340, dt=1 / 60):
    """Curva de fechar do Coucou: cubic-bezier(.45,0,.2,1) em 340 ms, sem passar do ponto"""
    def bez(t, p1, p2):
        return 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3

    n = max(2, int(ms / 1000 / dt))
    out = []
    for i in range(1, n + 1):
        x = i / n
        lo, hi, t = 0.0, 1.0, x
        for _ in range(14):
            if bez(t, .45, .2) < x:
                lo = t
            else:
                hi = t
            t = (lo + hi) / 2
        out.append(a + (b - a) * bez(t, 0, 1))
    return out


class IslandUI:
    def __init__(self, on_pause=None, on_stop=None, on_chat=None, on_confirm=None, on_open=None, on_decide=None,
                 on_release=None):
        H = web_server.HUB.handlers
        H.update(toggle_pause=on_pause or (lambda: None), stop_speaking=on_stop or (lambda: None),
                 chat=on_chat or (lambda t: None), confirm=on_confirm or (lambda y: None),
                 focus=lambda: None, resize=lambda *a: None,
                 open_item=on_open or (lambda k, n, id=None: None),
                 decide=on_decide or (lambda y, id=None: None),
                 release=on_release or (lambda id=None: None))
        for k in ("toggle_pause", "stop_speaking", "chat", "confirm", "open_item", "decide", "release"):
            H[k] = _lenient(H[k])
        self.url = web_server.start()
        self.window = None
        self.ready = threading.Event()
        self._cur = (W_CLOSED, H_CLOSED)
        self._r = 10
        self._hwnd = None
        self._gen = 0
        self._lock = threading.Lock()
        self._scr = (ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1))

    def create(self):
        global _UI
        _UI = self
        x = (self._scr[0] - W_CLOSED) // 2
        self.window = webview.create_window(
            "Jarvis", url=self.url + "?py=1", js_api=_Api(), width=W_CLOSED,
            height=H_CLOSED, x=x, y=0, frameless=True, transparent=False, on_top=True, easy_drag=False,
            resizable=False, focus=False, shadow=False, min_size=(100, 30))  # o padrão (200x100) deixava a ilha fechada grande demais
        self.window.events.loaded += lambda: (self.ready.set(), self._round())
        return self.window

    def start(self, func):
        """Bloqueia (thread principal): abre a janela e roda `func` numa thread"""
        webview.start(lambda: threading.Thread(target=func, daemon=True).start(), gui="edgechromium", debug=False)

    # --- tamanho (a janela só ocupa o espaço que a ilha usa, para não bloquear cliques) ---
    def _hw(self):
        if not self._hwnd:
            self._hwnd = ctypes.windll.user32.FindWindowW(None, "Jarvis") or None
        return self._hwnd

    def _dpi(self):
        try:
            return (ctypes.windll.user32.GetDpiForWindow(self._hw()) or 96) / 96
        except Exception:
            return 1

    def _round(self):
        """Cantos de baixo arredondados: recorta a própria janela (WebView2 não dá fundo transparente)"""
        try:
            hwnd = self._hw()
            r = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            d = max(2, int(self._r * 2 * self._dpi()))
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, -d - 4, w + 1, h + 1, d, d)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass

    def _scr_logical(self):
        """Largura da tela em pixels lógicos (resize/move do pywebview usam pixels lógicos)"""
        k = self._dpi()
        return int(self._scr[0] / k) if k > 1 else self._scr[0]

    def _resize(self, tw, th, r=14):
        """Anima até (tw, th): mola ao crescer, curva suave ao encolher. Um pedido novo interrompe o anterior."""
        with self._lock:
            self._gen += 1
            gen = self._gen
            cw, ch = self._cur
        grow = tw * th >= cw * ch
        if grow:
            self._r = r  # o raio novo entra já ao crescer; ao encolher, só no fim
        pw = _spring_path(cw, tw) if grow else _close_path(cw, tw)
        ph = _spring_path(ch, th) if grow else _close_path(ch, th)
        n = max(len(pw), len(ph))
        pw += [tw] * (n - len(pw))
        ph += [th] * (n - len(ph))
        try:
            for w, h in zip(pw, ph):
                if gen != self._gen:
                    return
                w, h = max(60, int(round(w))), max(24, int(round(h)))
                t0 = time.perf_counter()
                self.window.resize(w, h)
                self.window.move((self._scr_logical() - w) // 2, 0)
                self._cur = (w, h)
                self._round()
                time.sleep(max(0.0, 1 / 60 - (time.perf_counter() - t0)))
            self._r = r
            self._cur = (tw, th)
            self._round()
        except Exception:
            pass

    # --- chamadas para o JS (janela do topo + qualquer página aberta, como a aba do Orca) ---
    def call(self, fn, *args):
        web_server.HUB.publish(fn, *args)
        if not self.ready.is_set():
            return
        try:
            self.window.evaluate_js(f"J.{fn}(" + ",".join(json.dumps(a, ensure_ascii=False) for a in args) + ")")
        except Exception:
            pass

    def state(self, s, label=None):
        self.call("state", s, label)

    def heard(self, t):
        self.call("heard", t)

    def said(self, t):
        self.call("said", t)

    def msg(self, who, t):
        web_server.HUB.publish("msg", who, t, True)

    def level(self, v):
        self.call("level", round(v, 3))

    def lists(self, claude, orca):
        """Itens {n, s: run|done|wait} e, opcionais, {w, id, tool, ago (segundos desde a última mudança)}"""
        self.call("lists", claude, orca)

    def alert(self, text):
        self.call("alert", text)

    def confirm(self, text):
        self.call("confirm", text)

    def approval(self, info):
        """info: {w, name, text, code?, tool?, id?, t?} acrescenta/atualiza na fila (pelo id); lista substitui a
        fila; None limpa; {id, clear: True} tira um só"""
        self.call("approval", info)

    def open(self, hold=False):
        self.call("open", bool(hold))

    def _focus(self):
        try:
            ctypes.windll.user32.SetForegroundWindow(self._hw())
        except Exception:
            pass

    def keep_on_top(self):
        """A barra de tarefas e outras janelas "sempre por cima" passam à frente: reafirma a cada 3 s"""
        def loop():
            while True:
                time.sleep(3)
                try:
                    ctypes.windll.user32.SetWindowPos(self._hw(), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # TOPMOST, sem mover/ativar
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def call_local(self, code):
        if self.ready.is_set():
            try:
                self.window.evaluate_js(code)
            except Exception:
                pass

    def start_cursor_feed(self):
        """Os olhos seguem o cursor em qualquer lugar da tela, não só sobre a janela (só manda quando ele mexe)"""
        from ctypes import wintypes

        def loop():
            pt = wintypes.POINT()
            last = None
            while True:
                time.sleep(0.05)
                try:
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    p = (pt.x - self.window.x, pt.y - self.window.y)
                    if p != last:
                        last = p
                        self.call_local(f"J.mouse({p[0]},{p[1]})")
                except Exception:
                    pass

        threading.Thread(target=loop, daemon=True).start()
