"""Regras de confirmação por voz/texto e leitura do pedido de permissão. Só biblioteca padrão (testável no CI).

Voz: o microfone fica sempre aberto, então só vale a frase INTEIRA "palavra de ativação + confirmação"
("Jarvis, confirmo") ou uma frase específica ("confirmo a ação"). "ok", "claro", "isso" ou "sim" soltos não valem.
Texto digitado é intencional: aceita também "sim", "confirmo", "pode".
Negação em qualquer lugar da frase sempre vence."""
import re
import unicodedata

NO = {"nao", "nem", "nunca", "jamais", "negativo", "nega", "nego", "negar", "negue", "cancela", "cancelar", "cancele",
      "cancelado", "deixa", "esquece", "para", "pare"}
CONFIRM = {"sim", "confirma", "confirmo", "confirmar", "confirmado", "autorizo", "aprovo", "pode", "pode sim",
           "pode mandar", "pode confirmar", "sim confirmo", "sim pode", "sim pode mandar"}
PHRASES = {"confirmo a acao", "confirma a acao", "confirmar a acao", "confirmo essa acao", "pode executar a acao"}
TYPED = {"sim", "confirma", "confirmo", "pode", "pode sim", "pode mandar", "sim confirmo"}
PREFIXES = {"ei", "hey", "oi"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def confirm_words(text, wake_words, typed=False):
    """True = confirmou, False = negou, None = não vale como resposta (continua esperando)"""
    words = norm(text).split()
    if set(words) & NO:
        return False
    phrase = " ".join(words)
    if phrase in PHRASES or (typed and phrase in TYPED):
        return True
    if len(words) > 1 and words[0] in PREFIXES:
        words = words[1:]
    if len(words) > 1:
        for wake, rest in ((words[0], words[1:]), (words[-1], words[:-1])):
            if wake in wake_words and " ".join(rest) in CONFIRM | PHRASES:
                return True
    return None


# ---------- pedido de permissão na tela do Claude ----------
BUTTON, LISTITEM, TEXT, GROUP = 50000, 50007, 50020, 50026
# botões que liberam além deste pedido: o Jarvis nunca clica neles
LASTING = {"sempre", "always", "sessao", "session"}
ALLOW = ["permitir", "permitir uma vez", "aprovar", "allow", "sim", "yes"]
# início da pergunta do cartão de permissão; sem ela, não chutamos o que vai ser aprovado
_QUESTION = re.compile(r"(permitir que (o )?claude|deixar (o )?claude|(o )?claude (quer|deseja|gostaria)|"
                       r"allow claude to|claude wants to|claude would like to)\b")


def is_allow(name):
    """Botão que aprova só o pedido atual ("Permitir sempre" e parecidos ficam de fora)"""
    n = norm(name)
    return not set(n.split()) & LASTING and any(n == p or n.startswith(p + " ") for p in ALLOW)


def find_permission(items, window=12):
    """items = [(tipo, nome)] em ordem de tela. Devolve None se não há botão de permitir; senão
    {"button": nome do botão que será clicado, "detail": texto do pedido, ou "" se não deu para identificar}"""
    main = False
    for i, (kind, name) in enumerate(items):
        if kind == GROUP and name == "Painel principal":
            main = True
        if not (main and kind == BUTTON and is_allow(name)):
            continue
        before = items[max(0, i - window):i]
        start = next((j for j in range(len(before) - 1, -1, -1)
                      if before[j][0] == TEXT and _QUESTION.match(norm(before[j][1]))), None)
        detail = []
        if start is not None:
            for k, n in before[start:]:
                if k in (TEXT, LISTITEM) and n and (not detail or detail[-1] != n):
                    detail.append(n)
        return {"button": name, "detail": " ".join(" ".join(detail).split())}
    return None


def permission_text(req, limit=200):
    """Frase que diz o que vai ser aprovado"""
    if not req or not req.get("detail"):
        button = f' (botão "{req["button"]}")' if req else ""
        return f"não consegui identificar a ferramenta ou o comando; confira no app Claude{button}"
    detail = req["detail"]
    cut = "texto cortado, confira tudo no app Claude; " if len(detail) > limit else ""
    return f'{cut}pedido lido na tela: {detail[:limit]} (botão "{req["button"]}")'
