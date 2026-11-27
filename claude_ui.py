"""Controle do app Claude desktop (aba Code) pela árvore de acessibilidade do Windows (UI Automation).

O app é Electron: o conteúdo web aparece como DocumentControl "RootWebArea" e os botões da barra lateral
têm o nome das sessões ("Meu App", "IDE IA Em execução", ...). A árvore é lida de uma vez só, com cache
(FindAllBuildCache): percorrer elemento por elemento levava 30 s, assim leva ~0,2 s.
"""
import difflib
import re
import time
import unicodedata

import uiautomation as auto
from uiautomation import uiautomation as _ua

auto.SetGlobalSearchTimeout(2)

BUTTON, EDIT, LISTITEM, TEXT, GROUP = 50000, 50004, 50007, 50020, 50026
STATUS_SUFFIXES = [" Em execução", " Resposta não lida", " Aguardando resposta", " Aguardando", " Precisa de atenção"]
NAV_NAMES = {
    "Criar", "Procurar", "Artefatos", "Novo a partir de um modelo", "Personalização", "Outros itens de navegação",
    "Fixados", "Configurações do Code", "Menu", "Ocultar barra lateral", "Mostrar barra lateral", "Voltar", "Avançar",
    "Terminal", "Alterações", "Navegador", "Arquivos", "Ver opções", "Painel lateral da sessão", "Agrupar e ordenar",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


class Node:
    __slots__ = ("type", "name", "el")

    def __init__(self, t, n, el):
        self.type, self.name, self.el = t, (n or "").strip(), el

    def control(self):
        return auto.Control.CreateControlFromElement(self.el)


class ClaudeApp:
    def __init__(self):
        self._uia = _ua._AutomationClient.instance().IUIAutomation
        self._cr = self._uia.CreateCacheRequest()
        for p in (30005, 30003):  # Name, ControlType
            self._cr.AddProperty(p)
        self._true = self._uia.CreateTrueCondition()

    def window(self):
        # FindWindow direto: percorrer todas as janelas pelo UI Automation levava ~10 s
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        found = []
        buf = ctypes.create_unicode_buffer(256)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _):
            u32.GetClassNameW(h, buf, 256)
            if buf.value == "Chrome_WidgetWin_1" and u32.IsWindowVisible(h):
                u32.GetWindowTextW(h, buf, 256)
                if buf.value == "Claude":
                    found.append(h)
            return True

        u32.EnumWindows(cb, 0)
        return auto.ControlFromHandle(found[0]) if found else None

    def _doc(self):
        w = self.window()
        if not w:
            raise RuntimeError("o app Claude não está aberto")
        cond = self._uia.CreatePropertyCondition(30011, "RootWebArea")  # AutomationId
        for _ in range(4):  # o Chromium só liga a acessibilidade depois da primeira consulta
            # há mais de um RootWebArea (webviews ocultas); o da interface tem nome
            docs = w.Element.FindAll(4, cond)
            for i in range(docs.Length):
                e = docs.GetElement(i)
                try:
                    if e.CurrentName:
                        return w, auto.Control.CreateControlFromElement(e)
                except Exception:
                    continue
            time.sleep(0.6)
        raise RuntimeError("não consegui ler a janela do Claude")

    def scan(self):
        """Todos os elementos da interface, em ordem de documento"""
        _, doc = self._doc()
        arr = doc.Element.FindAllBuildCache(4, self._true, self._cr)  # 4 = descendentes
        out = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                out.append(Node(e.CachedControlType, e.CachedName, e))
            except Exception:
                continue
        return out

    # ---------- leitura ----------
    def sidebar(self, nodes=None):
        """Sessões (com o projeto a que pertencem) e projetos da barra lateral.
        Projeto = cabeçalho recolhível seguido de "Nova sessão em <projeto>"; `header` alterna recolher/expandir."""
        nodes = nodes if nodes is not None else self.scan()
        side = []
        for n in nodes:
            if n.type == GROUP and n.name == "Painel principal":
                break
            if n.type == BUTTON:
                side.append(n)
        proj_names = {n.name[len("Nova sessão em "):] for n in side if n.name.startswith("Nova sessão em ")}
        sessions, projects, current = [], [], None
        for n in side:
            name = n.name
            if not name or name in NAV_NAMES or name.startswith("Mais opções"):
                continue
            if name.endswith(", renomear sessão"):
                continue
            if name.startswith("Nova sessão em "):
                p = next((x for x in projects if x["project"] == name[len("Nova sessão em "):]), None)
                if p:
                    p["new"] = n
                continue
            status = ""
            for suf in STATUS_SUFFIXES:
                if name.endswith(suf):
                    status, name = suf.strip(), name[: -len(suf)]
                    break
            if name in proj_names:
                current = {"project": name, "header": n, "new": None, "count": 0}
                projects.append(current)
                continue
            if current:
                current["count"] += 1
            sessions.append({"name": name, "status": status, "node": n,
                             "project": current["project"] if current else ""})
        for p in projects:
            p["node"] = p["new"] or p["header"]
        return sessions, projects

    def current_session(self, nodes=None):
        nodes = nodes if nodes is not None else self.scan()
        for n in nodes:
            if n.type == BUTTON and n.name.endswith(", renomear sessão"):
                return n.name.rsplit(", renomear sessão", 1)[0]
        return ""

    def conversation(self, last=3, limit=3500, nodes=None):
        """Texto das últimas mensagens da sessão aberta"""
        nodes = nodes if nodes is not None else self.scan()
        msgs, cur, inside = [], None, False
        for n in nodes:
            if n.type == GROUP and n.name == "Mensagens da conversa":
                inside = True
                continue
            if not inside:
                continue
            if n.type == EDIT and n.name == "Prompt":
                break
            if n.type == GROUP and (n.name.startswith("Mensagem")):
                cur = ["Claude (respondendo agora)" if n.name == "Mensagem sendo transmitida" else n.name]
                msgs.append(cur)
                continue
            if cur is not None and n.type in (TEXT, LISTITEM) and n.name and cur[-1] != n.name:
                cur.append(n.name)
        parts = [f"[{m[0]}] " + " ".join(m[1:]) for m in msgs[-last:]]
        return "\n".join(parts)[-limit:]

    def tasks(self, nodes=None):
        nodes = nodes if nodes is not None else self.scan()
        out = []
        for n in nodes:
            if n.type == LISTITEM:
                for k, lab in (("em andamento", "em andamento"), ("não concluído", "pendente"), ("concluído", "concluído")):
                    if n.name.startswith(k):
                        out.append(f"{lab}: {n.name[len(k):]}")
                        break
        return out

    def pending_buttons(self, nodes=None):
        """Botões de decisão visíveis no painel principal (permissões, perguntas)"""
        nodes = nodes if nodes is not None else self.scan()
        main, out = False, []
        keys = ("permitir", "aprovar", "negar", "recusar", "rejeitar", "allow", "deny", "parar", "interromper")
        for n in nodes:
            if n.type == GROUP and n.name == "Painel principal":
                main = True
            if main and n.type == BUTTON and norm(n.name).split(" ")[0] in keys:
                out.append(n.name)
        return out

    def snapshot(self):
        nodes = self.scan()
        sessions, projects = self.sidebar(nodes)
        return {
            "sessao_aberta": self.current_session(nodes),
            "sessoes": [s["name"] + (f" ({s['status']})" if s["status"] else "") for s in sessions][:70],
            "projetos": [p["project"] for p in projects],
            "tarefas_da_sessao_aberta": self.tasks(nodes)[:12],
            "botoes_de_decisao": self.pending_buttons(nodes),
        }

    # ---------- ações ----------
    @staticmethod
    def _press(node):
        c = node.control()
        try:
            c.GetInvokePattern().Invoke()
        except Exception:
            c.Click(simulateMove=False)

    @staticmethod
    def _best(query, items, key):
        q = norm(query)
        scored = []
        for it in items:
            n = norm(key(it))
            if not n:
                continue
            if q == n:
                s = 2.0
            elif q in n or n in q:
                s = 1.2 + min(len(q), len(n)) / max(len(n), len(q), 1) * 0.5
            else:
                s = difflib.SequenceMatcher(None, q, n).ratio()
            scored.append((s, it))
        scored.sort(key=lambda x: -x[0])
        return scored[0] if scored and scored[0][0] >= 0.55 else (None, None)

    def show(self):
        w = self.window()
        if not w:
            raise RuntimeError("o app Claude não está aberto")
        try:
            if w.IsMinimize():
                w.Restore()
        except Exception:
            pass
        w.SetActive()
        return "janela do Claude em primeiro plano"

    def open_session(self, name):
        sessions, projects = self.sidebar()
        _, s = self._best(name, sessions, lambda x: x["name"])
        if not s:
            # a sessão pode estar dentro de um projeto recolhido: expande os recolhidos, procura, recolhe de volta
            opened = [p for p in projects if p["count"] == 0]
            for p in opened:
                self._press(p["header"])
            if opened:
                time.sleep(0.6)
                sessions, projects = self.sidebar()
                _, s = self._best(name, sessions, lambda x: x["name"])
                again = {p["project"]: p for p in projects}
                for p in opened:
                    if (not s or s["project"] != p["project"]) and p["project"] in again:
                        self._press(again[p["project"]]["header"])
                if s:  # os nós mudaram depois de recolher os outros
                    time.sleep(0.4)
                    sessions, projects = self.sidebar()
                    _, s = self._best(name, sessions, lambda x: x["name"])
        if s:
            self._press(s["node"])
            time.sleep(1.5)
            return f"abri a sessão {s['name']}" + (f" do projeto {s['project']}" if s["project"] else "")
        _, p = self._best(name, projects, lambda x: x["project"])
        if p:
            return f"não há sessão chamada {name}, mas existe o projeto {p['project']}; posso abrir uma nova sessão nele"
        raise RuntimeError(f"não encontrei sessão nem projeto parecido com '{name}'")

    def new_session(self, project=""):
        nodes = self.scan()
        if project:
            _, projects = self.sidebar(nodes)
            _, p = self._best(project, projects, lambda x: x["project"])
            if not p:
                raise RuntimeError(f"não encontrei o projeto '{project}'")
            self._press(p["node"])
            time.sleep(1.5)
            return f"nova sessão aberta no projeto {p['project']}"
        for n in nodes:
            if n.type == BUTTON and n.name == "Criar":
                self._press(n)
                time.sleep(1.5)
                return "nova sessão aberta"
        raise RuntimeError("não achei o botão de nova sessão")

    def type_text(self, text, send=False):
        nodes = self.scan()
        edit = next((n for n in nodes if n.type == EDIT and n.name == "Prompt"), None)
        if not edit:
            raise RuntimeError("não achei a caixa de mensagem")
        self.show()
        ec = edit.control()
        ec.SetFocus()
        time.sleep(0.15)
        old = None
        try:
            old = auto.GetClipboardText()
        except Exception:
            pass
        auto.SetClipboardText(text)
        auto.SendKeys("{Ctrl}a", waitTime=0.05)
        auto.SendKeys("{Ctrl}v", waitTime=0.25)
        if send:
            btn = next((n for n in self.scan() if n.type == BUTTON and n.name == "Enviar"), None)
            if btn:
                self._press(btn)
            else:
                auto.SendKeys("{Enter}")
        if old is not None:
            time.sleep(0.4)
            try:
                auto.SetClipboardText(old)
            except Exception:
                pass
        return "mensagem enviada ao Claude" if send else "texto digitado na caixa de mensagem"

    def _click_first(self, prefixes, what):
        main = False
        for n in self.scan():
            if n.type == GROUP and n.name == "Painel principal":
                main = True
            if not main or n.type != BUTTON:
                continue
            nn = norm(n.name)
            if any(nn == p or nn.startswith(p + " ") for p in prefixes):
                self._press(n)
                return f"cliquei em {n.name}"
        raise RuntimeError(f"não há nenhum pedido de {what} na tela agora")

    def approve(self):
        return self._click_first(["permitir", "permitir uma vez", "aprovar", "allow", "sim", "yes"], "permissão")

    def deny(self):
        return self._click_first(["negar", "recusar", "rejeitar", "deny"], "permissão")

    def stop(self):
        return self._click_first(["parar", "interromper", "stop"], "parada")

    # ---------- modelo / esforço do chat ----------
    def _window_nodes(self):
        """Elementos da janela inteira: menus abertos ficam fora do documento principal"""
        w = self.window()
        arr = w.Element.FindAllBuildCache(4, self._true, self._cr)
        out = []
        for i in range(arr.Length):
            e = arr.GetElement(i)
            try:
                out.append(Node(e.CachedControlType, e.CachedName, e))
            except Exception:
                continue
        return out

    def _open_model_menu(self):
        b = next((n for n in self.scan() if n.type == BUTTON and n.name.startswith("Modelo:")), None)
        if not b:
            raise RuntimeError("não achei o seletor de modelo (abra uma sessão primeiro)")
        self.show()
        self._press(b)
        for _ in range(12):  # o menu demora a montar
            time.sleep(0.2)
            if any(n.type == 50011 for n in self._window_nodes()):
                break
        return b.name

    @staticmethod
    def _select(node):
        c = node.control()
        for getter in ("GetSelectionItemPattern", "GetInvokePattern"):
            try:
                p = getattr(c, getter)()
                (p.Select if getter == "GetSelectionItemPattern" else p.Invoke)()
                return
            except Exception:
                continue
        c.Click(simulateMove=False)

    def current_model(self):
        b = next((n for n in self.scan() if n.type == BUTTON and n.name.startswith("Modelo:")), None)
        return b.name.replace("Modelo:", "").strip() if b else ""

    def set_model(self, name):
        self._open_model_menu()
        radios = [n for n in self._window_nodes() if n.type == 50013]  # RadioButton
        _, r = self._best(name, radios, lambda x: x.name)
        if not r:
            auto.SendKeys("{Esc}")
            raise RuntimeError(f"não há modelo parecido com '{name}'. Opções: {', '.join(x.name for x in radios)}")
        self._select(r)
        time.sleep(0.5)
        auto.SendKeys("{Esc}")
        return f"modelo do chat agora: {self.current_model()}"

    def set_effort(self, level):
        self._open_model_menu()
        item = next((n for n in self._window_nodes() if n.type == 50011 and n.name.startswith("Esforço")), None)
        if not item:
            auto.SendKeys("{Esc}")
            raise RuntimeError("este modelo não tem ajuste de esforço")
        self._select(item)
        time.sleep(0.6)
        names = {"baixo", "medio", "alto", "maximo", "muito alto", "minimo", "extra alto"}
        opts = [n for n in self._window_nodes() if n.type in (50011, 50013) and norm(n.name) in names]
        _, o = self._best(level, opts, lambda x: x.name)
        if not o:
            auto.SendKeys("{Esc}{Esc}")
            raise RuntimeError(f"nível '{level}' não existe. Opções: {', '.join(x.name for x in opts)}")
        self._select(o)
        time.sleep(0.4)
        auto.SendKeys("{Esc}")
        return f"modelo do chat agora: {self.current_model()}"

    def scroll(self, direction="down"):
        self.show()
        auto.SendKeys("{PageDown}" if direction == "down" else "{PageUp}")
        return "rolei a conversa"

    def minimize(self):
        w = self.window()
        if w:
            w.Minimize()
        return "janela minimizada"


if __name__ == "__main__":
    t = time.time()
    app = ClaudeApp()
    print(app.snapshot())
    print(app.conversation(2)[:600])
    print(round(time.time() - t, 2), "s")
