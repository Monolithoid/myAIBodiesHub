"""Tests of the hangman game, alone and through game-master (hidden definition call).

    cd robotics/games && python3 -m unittest discover -s tests -v
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hangman  # noqa: E402
from game_core import GameMaster, Player, ScoreStore  # noqa: E402

ALICE = Player("alice", "Alice")
BOB = Player("bob", "Bob")
WORDS = {"easy": ["éléphant"], "medium": ["araignée"], "hard": ["cœur"]}


class FixedHangman(hangman.Hangman):
    """Hangman on the test words: game-master builds a game from its class."""
    def __init__(self):
        super().__init__(words=WORDS)


def game(word_level="medium", max_errors=7):
    g = hangman.Hangman(words=WORDS, rng=random.Random(0), max_errors=max_errors)
    g.start(ALICE, {"difficulty": word_level})
    return g


def letter(g, c, player=ALICE):
    return g.on_move({"type": "letter", "letter": c}, player)


class TestWordList(unittest.TestCase):

    def test_shipped_list(self):
        words = hangman.load_words(hangman.WORDS_PATH)
        for level in ("easy", "medium", "hard"):
            self.assertGreater(len(words[level]), 100, level)
            for w in words[level]:
                self.assertRegex(hangman.plain(w), r"^[a-z]{6,12}$", w)

    def test_blocklist_applied(self):
        words = {w for level in hangman.load_words(hangman.WORDS_PATH).values() for w in level}
        self.assertNotIn("putain", words)
        self.assertNotIn("suicide", words)


class TestPlain(unittest.TestCase):

    def test_plain(self):
        self.assertEqual(hangman.plain("Éléphant"), "elephant")
        self.assertEqual(hangman.plain("cœur"), "coeur")


class TestStart(unittest.TestCase):

    def test_difficulty_and_mask(self):
        g = game("facile")
        self.assertEqual(g.word, "éléphant")
        self.assertEqual(g.mask(), "_ _ _ _ _ _ _ _")
        self.assertEqual(game("difficile").target, "coeur")
        self.assertEqual(game("n'importe quoi").word, "araignée")

    def test_definition_is_asked_and_checked(self):
        g = game()
        ask = g.pending_ask()
        self.assertIn("araignée", ask["user"])
        event = g.on_answer(ask["id"], "« Petit animal à huit pattes qui tisse une toile. »")
        self.assertIsNone(g.pending_ask())
        self.assertEqual(g.definition, "Petit animal à huit pattes qui tisse une toile.")
        self.assertNotIn("huit pattes", event.text)
        self.assertEqual(event.read, "Indice : Petit animal à huit pattes qui tisse une toile.")
        self.assertEqual(event.to_dict()["read"], event.read)
        self.assertNotIn("huit pattes", g.summary())
        self.assertEqual(g.public_view()["definition"], g.definition)

    def test_leaking_definition_is_dropped(self):
        for text in ("Une araignée est un animal.", "Les ARAIGNÉES tissent.",
                     "Animal : a-r-a-i-g-n-e-e.", "x" * 300, ""):
            g = game()
            event = g.on_answer(g.pending_ask()["id"], text)
            self.assertEqual(g.definition, "", text)
            self.assertIn("Pas de définition", event.text)
            self.assertEqual(event.read, "")

    def test_leaks(self):
        self.assertTrue(hangman.leaks("le cœur bat", "cœur"))
        self.assertFalse(hangman.leaks("Organe qui pompe le sang.", "cœur"))
        self.assertTrue(hangman.leaks("un éléphanteau", "éléphant"))


class TestMoves(unittest.TestCase):

    def test_good_and_bad_letters(self):
        g = game()   # araignée
        event = letter(g, "A")
        self.assertEqual((event.kind, event.points), ("good", {"alice": 2}))
        self.assertEqual(g.mask(), "A _ A _ _ _ _ _")
        event = letter(g, "é", BOB)   # é = e
        self.assertEqual(event.kind, "good")
        self.assertEqual(g.mask(), "A _ A _ _ _ E E")
        event = letter(g, "z")
        self.assertEqual(event.kind, "bad")
        self.assertEqual(g.errors, 1)
        self.assertEqual(g.public_view()["wrong"], ["Z"])
        self.assertEqual(g.public_view()["tried"], ["A", "E", "Z"])

    def test_already_and_invalid_letters_cost_nothing(self):
        g = game()
        letter(g, "a")
        self.assertEqual(letter(g, "a").kind, "already")
        self.assertEqual(letter(g, "ab").kind, "invalid")
        self.assertEqual(g.on_move({"type": "dance"}, ALICE).kind, "invalid")
        self.assertEqual(g.errors, 0)

    def test_win_with_the_last_letter(self):
        g = game("difficile")   # coeur
        for c in "coeu":
            self.assertEqual(letter(g, c).kind, "good")
        event = letter(g, "r", BOB)
        self.assertEqual(event.kind, "won")
        self.assertEqual(event.points, {"bob": 1 + hangman.WORD_BONUS})
        self.assertTrue(g.is_over())

    def test_word(self):
        g = game()
        letter(g, "a")
        event = g.on_move({"type": "word", "word": "Araignee"}, BOB)
        self.assertEqual(event.kind, "won")
        self.assertEqual(event.points, {"bob": hangman.WORD_BONUS + 6})
        self.assertIn("araignée", event.text)
        self.assertEqual(g.public_view()["mask"], "A R A I G N E E")
        self.assertEqual(g.public_view()["tried"], ["A"])   # only the letters played

    def test_wrong_word_costs_an_error_and_bars_the_viewer(self):
        g = game()
        event = g.on_move({"type": "word", "word": "araignées"}, BOB)
        self.assertEqual(event.kind, "bad")
        self.assertEqual(g.errors, 1)
        event = g.on_move({"type": "word", "word": "araignée"}, BOB)
        self.assertEqual(event.kind, "word_refused")
        self.assertFalse(event.ok)
        self.assertEqual(letter(g, "a", BOB).kind, "good")   # letters still allowed
        self.assertEqual(g.on_move({"type": "word", "word": "araignée"}, ALICE).kind, "won")

    def test_lost_after_max_errors(self):
        g = game(max_errors=3)
        letter(g, "z")
        letter(g, "x")
        event = letter(g, "w")
        self.assertEqual(event.kind, "lost")
        self.assertIn("araignée", event.text)
        self.assertTrue(g.is_over())
        self.assertEqual(g.mask(), "A R A I G N E E")   # word shown once lost

    def test_summary_never_holds_the_word(self):
        g = game()
        letter(g, "a")
        letter(g, "z")
        self.assertNotIn("araign", g.summary().lower())
        self.assertNotIn("araign", g.recap().lower())
        self.assertNotIn("araign", str(g.public_view()).lower())


class TestNaoPlays(unittest.TestCase):

    NAO = Player("nao", "NAO", source="nao")

    def test_nao_holds_its_guess_while_too_little_is_found(self):
        g = game()   # araignée: nothing found yet
        g.nao_min_found = 0.5
        event = g.on_move({"type": "said", "text": "une araignée ?"}, self.NAO)
        self.assertEqual(event.kind, "nao_holds")
        self.assertFalse(event.ok)
        self.assertNotIn("araign", event.say)
        self.assertFalse(g.is_over())

    def test_nao_says_the_word_and_wins(self):
        g = game()   # araignée
        letter(g, "a")
        letter(g, "e")   # 4 of 8 letters found: half of the word
        event = g.on_move({"type": "said", "text": "Je parie sur une ARAIGNEE !"}, self.NAO)
        self.assertEqual((event.kind, event.mood), ("won", "frimeur"))
        self.assertEqual(event.points, {"nao": hangman.WORD_BONUS + 4})
        self.assertIn("frimeur", event.text)
        self.assertTrue(g.is_over())

    def test_said_needs_the_word_and_nao(self):
        g = game()
        self.assertEqual(g.on_move({"type": "said", "text": "un chat"}, self.NAO).kind, "invalid")
        self.assertEqual(g.on_move({"type": "said", "text": "araignée"}, ALICE).kind, "invalid")
        self.assertFalse(g.is_over())


class TestThroughGameMaster(unittest.TestCase):

    def test_check_blocks_the_word_while_running(self):
        master = GameMaster({"hangman": FixedHangman})
        self.assertFalse(master.check("éléphant"))                  # no game
        master.start("hangman", ALICE, "pendu ?", {"difficulty": "facile"})
        self.assertTrue(master.check("Ce gros ELEPHANT va tomber !"))
        self.assertTrue(master.check("des éléphanteaux"))
        self.assertFalse(master.check("Bien vu, le E est là."))
        master.move({"type": "word", "word": "elephant"}, BOB)
        self.assertFalse(master.check("Le mot était éléphant !"))  # game over: said freely

    def test_ask_answer_flow(self):
        master = GameMaster({"hangman": FixedHangman})
        event = master.start("hangman", ALICE, "on joue au pendu ?", {"difficulty": "facile"})
        self.assertEqual(event.kind, "started")
        ask = master.pending_ask()
        self.assertEqual(ask["purpose"], "définition du mot du pendu")
        self.assertNotIn("éléphant", str(master.state()))
        self.assertEqual(master.answer("wrong-id", "x", ALICE).kind, "no_ask")
        event = master.answer(ask["id"], "Grand animal gris à trompe.", ALICE)
        self.assertEqual(event.kind, "started")
        self.assertNotIn("trompe", event.text)        # the LLM never sees the definition
        self.assertIn("trompe", event.read)           # the code reads it
        self.assertIsNone(master.pending_ask())
        self.assertNotIn("trompe", master.state()["activity"]["summary"])
        recap = master.move({"type": "recap"}, BOB)
        self.assertNotIn("trompe", recap.text)
        self.assertEqual(recap.read, "Indice : Grand animal gris à trompe.")
        self.assertEqual(master.catalog()[0]["start"], hangman.Hangman.start_help)

    def test_scores_in_the_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            master = GameMaster({"hangman": FixedHangman},
                                scores=ScoreStore(os.path.join(tmp, "s.db")))
            self.assertIsNone(master.state()["scores"])
            master.start("hangman", ALICE, "pendu ?", {"difficulty": "difficile"})
            master.move({"type": "letter", "letter": "c"}, ALICE)
            self.assertEqual(master.state()["scores"],
                             {"game": "hangman", "top": [{"name": "Alice", "points": 1}]})
            master.move({"type": "word", "word": "coeur"}, BOB)
            top = master.state()["scores"]["top"]           # kept after the end
            self.assertEqual(top[0], {"name": "Bob", "points": hangman.WORD_BONUS + 4})

    def test_final_view_kept_after_the_end(self):
        master = GameMaster({"hangman": FixedHangman})
        master.start("hangman", ALICE, "pendu ?", {"difficulty": "difficile"})
        master.move({"type": "word", "word": "coeur"}, BOB)
        state = master.state()
        self.assertIsNone(state["activity"])
        self.assertEqual(state["final"]["view"]["mask"], "C O E U R")
        self.assertEqual(state["last_event"]["kind"], "won")


if __name__ == "__main__":
    unittest.main()
