"""Testes das regras de confirmação (só biblioteca padrão)"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from confirm import BUTTON, GROUP, TEXT, confirm_words, find_permission, permission_text  # noqa: E402

WAKE = ["jarvis", "jarbas"]


class VoiceTest(unittest.TestCase):
    def check(self, phrases, expected, typed=False):
        for p in phrases:
            with self.subTest(p):
                self.assertIs(confirm_words(p, WAKE, typed=typed), expected)

    def test_ambient_words_do_not_confirm(self):
        self.check(["ok", "claro", "isso", "sim", "pode", "manda", "vai", "fechado", "positivo", "pode mandar",
                    "confirma", "confirmo", "Confirma?", "jarvis", "jarvis ok", "jarvis claro", "jarvis isso",
                    "o jarvis disse que sim", "jarvis acho que pode ser", "sim claro isso mesmo", "",
                    "Se quiser liberar, diga: Jarvis, confirmo. Ou toque em Sim."], None)

    def test_wake_word_or_specific_phrase_confirms(self):
        self.check(["Jarvis, confirmo.", "Ei Jarvis, sim", "jarbas pode mandar", "Confirmo, Jarvis",
                    "Confirmo a ação"], True)

    def test_negation_always_wins(self):
        self.check(["não", "cancela", "Jarvis, não confirmo", "Jarvis, confirmo não", "sim, não, pera",
                    "não confirmo a ação", "Jarvis, pode cancelar", "nem pensar", "Jarvis, nega"], False)
        self.check(["não", "sim não"], False, typed=True)

    def test_typed(self):
        self.check(["sim", "Confirmo", "pode", "Jarvis, confirmo"], True, typed=True)
        self.check(["ok", "claro", "isso", "sim mas antes abre o projeto"], None, typed=True)


class PermissionTest(unittest.TestCase):
    def screen(self, *card):
        return [(BUTTON, "Permitir antigo"), (GROUP, "Painel principal"), (TEXT, "conversa qualquer"), *card]

    def test_identified(self):
        req = find_permission(self.screen((TEXT, "Permitir que o Claude execute"), (TEXT, "npm test"),
                                          (BUTTON, "Negar"), (BUTTON, "Permitir uma vez")))
        self.assertEqual(req, {"button": "Permitir uma vez", "detail": "Permitir que o Claude execute npm test"})
        self.assertIn("npm test", permission_text(req))

    def test_unknown_says_so(self):
        req = find_permission(self.screen((TEXT, "algo diferente"), (BUTTON, "Permitir")))
        self.assertEqual(req["detail"], "")
        self.assertIn("não consegui identificar", permission_text(req))
        self.assertIsNone(find_permission(self.screen()))
        self.assertIn("não consegui identificar", permission_text(None))

    def test_long_text_is_flagged(self):
        self.assertIn("cortado", permission_text({"button": "Permitir", "detail": "x" * 300}))


import confirm  # noqa: E402


class LastingPermissionTest(unittest.TestCase):
    def test_never_picks_lasting_button(self):
        for n in ("Permitir sempre", "Sempre permitir", "Allow always", "Permitir nesta sessão", "Allow for this session"):
            self.assertFalse(confirm.is_allow(n), n)
        for n in ("Permitir", "Permitir uma vez", "Allow once Enter"):
            self.assertTrue(confirm.is_allow(n), n)
        items = [(confirm.GROUP, "Painel principal"), (confirm.BUTTON, "Permitir sempre"), (confirm.BUTTON, "Permitir uma vez")]
        self.assertEqual(confirm.find_permission(items)["button"], "Permitir uma vez")


if __name__ == "__main__":
    unittest.main()
