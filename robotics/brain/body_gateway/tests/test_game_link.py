"""Tests without ROS 2 of body-gateway's side of the games (game_link.py, ADR 010).

    cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import game_link  # noqa: E402

CATALOG = {"activity": None, "games": [{"id": "hangman", "name": "Pendu", "how": "…",
                                       "start": '"options": {"difficulty": "facile|moyen"}'}]}
RUNNING = {"activity": {"game": "hangman", "name": "Pendu", "summary": "R _ _ _ T, 2 erreurs",
                        "how": 'une lettre : {"type": "letter", "letter": "e"}'},
           "games": CATALOG["games"]}


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.error:
            raise self.error
        return self.response

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)


class TestPrompt(unittest.TestCase):

    def test_no_games(self):
        self.assertEqual(game_link.prompt_section(None), "")
        self.assertEqual(game_link.prompt_section({"activity": None, "games": []}), "")

    def test_catalog(self):
        text = game_link.prompt_section(CATALOG)
        self.assertIn('- Pendu : "game": "hangman", options possibles : "options": {"difficulty"', text)
        self.assertIn("start_game", text)
        self.assertNotIn("game_move", text)

    def test_running_game_shows_public_state_and_moves(self):
        text = game_link.prompt_section(RUNNING)
        self.assertIn("R _ _ _ T", text)
        self.assertIn('"letter"', text)
        self.assertIn("game_move", text)
        self.assertIn('"recap"', text)
        self.assertIn("seule exception", text)
        self.assertNotIn('{"type": "start_game"', text)

    def test_reaction_input(self):
        text = game_link.reaction_input({"text": "Le E est dans le mot."}, RUNNING, "Alice")
        self.assertEqual(text.splitlines(), [
            "[jeu] Le E est dans le mot.", "[jeu] Joueur : Alice",
            "[jeu] État public de la partie de Pendu : R _ _ _ T, 2 erreurs"])
        self.assertEqual(game_link.reaction_input({"text": "Annulée."}, CATALOG), "[jeu] Annulée.")
        self.assertIn("[jeu] Ton suggéré : joie",
                      game_link.reaction_input({"text": "Gagné.", "mood": "joie"}, None))

    def test_hidden_messages(self):
        ask = {"id": "a1", "purpose": "définition", "system": "Définis.", "user": "Mot : chat"}
        self.assertEqual(game_link.hidden_messages(ask), [
            {"role": "system", "content": "Définis."}, {"role": "user", "content": "Mot : chat"}])
        self.assertIsNone(game_link.hidden_messages({"id": "a1"}))
        self.assertIsNone(game_link.hidden_messages(None))


class TestActions(unittest.TestCase):

    def test_game_action(self):
        actions = [{"type": "speech", "text": "ok"},
                   {"type": "game_move", "move": {"type": "letter", "letter": "e"}},
                   {"type": "start_game", "game": "hangman"}]
        self.assertEqual(game_link.game_action(actions)["type"], "game_move")
        self.assertEqual(game_link.without_game_actions(actions), [{"type": "speech", "text": "ok"}])
        self.assertIsNone(game_link.game_action([{"type": "speech"}, "junk"]))
        self.assertIsNone(game_link.game_action(None))

    def test_player_of(self):
        viewer = {"login": "alice", "name": "Alice", "message": "?", "roles": ["vip"]}
        self.assertEqual(game_link.player_of(viewer, "twitch"),
                         {"login": "alice", "name": "Alice", "roles": ["vip"], "source": "twitch"})
        self.assertEqual(game_link.player_of(None, "webui")["source"], "webui")
        # A viewer dict on a non-Twitch source is ignored: only the chat has viewers.
        self.assertEqual(game_link.player_of(viewer, "webui")["login"], "")


class TestRead(unittest.TestCase):

    def test_read_appended_to_the_last_speech(self):
        event = {"text": "Partie lancée.", "read": "Indice : organe qui pompe le sang."}
        actions = [{"body": "nao_01", "type": "speech", "text": "C'est parti !"},
                   {"body": "nao_01", "type": "gesture", "name": "wave"}]
        out = game_link.with_read(actions, event, "nao_01")
        self.assertEqual(out[0]["text"], "C'est parti ! Indice : organe qui pompe le sang.")
        self.assertEqual(out[1], actions[1])
        self.assertEqual(game_link.with_read(out, event, "nao_01"), out)   # not twice

    def test_read_without_speech_or_without_read(self):
        out = game_link.with_read([], {"read": "Indice : x."}, "obs_01")
        self.assertEqual(out, [{"body": "obs_01", "type": "speech", "text": "Indice : x."}])
        actions = [{"type": "speech", "text": "Raté !"}]
        self.assertIs(game_link.with_read(actions, {"text": "bad"}, "nao_01"), actions)

    def test_reaction_input_announces_the_read(self):
        text = game_link.reaction_input({"text": "Lancée.", "read": "Indice : y."}, None)
        self.assertNotIn("Indice : y.", text)   # the LLM would guess the word from it
        self.assertIn("un indice sera lu", text)

    def test_nao_is_told_it_played(self):
        text = game_link.reaction_input({"text": "NAO propose le E."}, None, "NAO")
        self.assertIn("c'est toi", text)


class TestFallback(unittest.TestCase):

    def test_letter_while_hangman_runs(self):
        running = {"activity": {"game": "hangman"}, "games": []}
        for text in ("e", " E ", "é", "la lettre e", "lettre E ?"):
            self.assertEqual(game_link.fallback_action(text, running),
                             {"type": "game_move", "move": {"type": "letter", "letter": "e"}}, text)
        for text in ("et", "salut", "c'est quoi ?", ""):
            self.assertIsNone(game_link.fallback_action(text, running), text)

    def test_word_as_long_as_the_hidden_one(self):
        running = {"activity": {"game": "hangman", "view": {"letters": 7}}, "games": []}
        for text in ("grenier", "Grenier !", "c'est grenier", "le mot est grenier"):
            self.assertEqual(game_link.fallback_action(text, running),
                             {"type": "game_move", "move": {"type": "word", "word": "grenier"}},
                             text)
        for text in ("salut", "bravooooo", "c'est trop dur"):
            self.assertIsNone(game_link.fallback_action(text, running), text)

    def test_check(self):
        class Resp:
            def __init__(self, leak): self.leak = leak
            def raise_for_status(self): pass
            def json(self): return {"leak": self.leak}

        class Session:
            def post(self, url, json=None, **k):
                return Resp("grenier" in json["text"])

        link = game_link.GameLink("http://gm", "t", session=Session())
        self.assertTrue(link.check("ce grenier va tomber"))
        self.assertFalse(link.check("le E est là"))
        self.assertFalse(link.check(""))

    def test_start_from_a_question_naming_a_game(self):
        idle = {"activity": None, "games": [{"id": "hangman", "name": "Pendu"}]}
        self.assertEqual(game_link.fallback_action("hello on peut jouer au pendu ?", idle),
                         {"type": "start_game", "game": "hangman", "options": {}})
        self.assertIsNone(game_link.fallback_action("j'aime le pendu", idle))   # no question
        self.assertIsNone(game_link.fallback_action("on joue ?", idle))
        self.assertIsNone(game_link.fallback_action("e", idle))
        self.assertIsNone(game_link.fallback_action("pendu ?", None))


def _columns():
    names = {"s": ["Le colonel Brisard", "Madame Aubépine"], "w": ["la pelle", "le tisonnier"],
             "p": ["la serre", "l'écurie"]}
    return [{"label": p, "cards": [{"id": f"{p}{i}", "name": n} for i, n in enumerate(ns, 1)]}
            for p, ns in names.items()]


MYSTERY = {"activity": {"game": "mystery", "name": "Enquête", "summary": "victime : Lord Armand",
                        "how": "une question…", "view": {"columns": _columns()}},
           "games": [{"id": "mystery", "name": "Enquête", "how": "…", "start": "…"}]}


class TestMystery(unittest.TestCase):

    def test_prompt_never_lets_nao_lead(self):
        text = game_link.prompt_section(MYSTERY)
        self.assertIn("victime : Lord Armand", text)
        self.assertIn('"question"', text)
        self.assertIn('"accuse"', text)
        self.assertIn("tu ne sais pas", text)
        self.assertNotIn("Tu joues aussi", text)
        self.assertNotIn('"letter"', text)

    def test_hangman_prompt_and_rules_unchanged(self):
        self.assertIn("Tu joues aussi", game_link.prompt_section(RUNNING))
        self.assertEqual(game_link.reaction_rules(RUNNING), game_link.REACTION_RULES)
        self.assertEqual(game_link.reaction_rules(None), game_link.REACTION_RULES)

    def test_rules_of_a_running_or_ended_mystery(self):
        for state in (MYSTERY, {"activity": None, "final": {"game": "mystery"}}):
            self.assertEqual(game_link.reaction_rules(state), game_link.MYSTERY_REACTION_RULES)

    def test_read_text_stays_out_of_the_reaction(self):
        text = game_link.reaction_input(
            {"text": "Un indice.", "read": "La pelle était au sec.", "read_as": "l'indice"}, None)
        self.assertIn("l'indice sera lu mot pour mot", text)
        self.assertNotIn("au sec", text)

    def fallback(self, message):
        return game_link.fallback_action(message, MYSTERY)

    def test_full_accusation(self):
        action = self.fallback("J'accuse Madame Aubépine avec la pelle dans l'écurie !")
        self.assertEqual(action["move"], {"type": "accuse", "suspect": "s2", "weapon": "w1",
                                          "place": "p2"})

    def test_partial_or_double_accusation_is_not_guessed(self):
        self.assertIsNone(self.fallback("j'accuse madame Aubépine ?"))
        self.assertIsNone(self.fallback("j'accuse madame Aubépine, la pelle, le tisonnier, la serre"))

    def test_recap_and_resume(self):
        for message in ("on reprend l'enquête ?", "tu fais un récap ?", "où en est-on ?",
                        "résume l'affaire"):
            self.assertEqual(self.fallback(message)["move"], {"type": "recap"}, message)

    def test_question_naming_a_card(self):
        move = self.fallback("Où était le colonel hier soir ?")["move"]
        self.assertEqual(move, {"type": "question", "text": "Où était le colonel hier soir ?"})
        self.assertIsNone(self.fallback("tu vas bien NAO ?"))
        self.assertIsNone(self.fallback("la pelle, quelle drôle d'idée"))

    def test_start_takes_the_message_as_context(self):
        action = game_link.fallback_action("on fait une enquête dans un château ?",
                                           {"activity": None, "games": MYSTERY["games"]})
        self.assertEqual(action, {"type": "start_game", "game": "mystery",
                                  "options": {"context": "on fait une enquête dans un château ?"}})

    def test_note(self):
        session = FakeSession(FakeResponse({"kept": True}))
        link = game_link.GameLink("http://g", "t", session=session)
        self.assertTrue(link.note("Il pleuvait."))
        self.assertEqual(session.calls[0][1], "http://g/note")
        self.assertFalse(link.note("  "))


class TestReveal(unittest.TestCase):

    def test_reveal_posts_the_version(self):
        calls = []

        class Resp:
            def raise_for_status(self): pass
            def json(self): return {"revealed": True}

        class Session:
            def post(self, url, json=None, **k):
                calls.append((url, json))
                return Resp()

        link = game_link.GameLink("http://gm", "t", session=Session())
        self.assertTrue(link.reveal(7))
        self.assertEqual(calls, [("http://gm/reveal", {"version": 7})])
        self.assertFalse(link.reveal(None))
        self.assertFalse(game_link.GameLink("", "t").reveal(3))


class TestStateError(unittest.TestCase):

    def test_error_reason(self):
        class Resp:
            def __init__(self, code): self.status_code = code
            def raise_for_status(self): pass
            def json(self): return {"activity": None}

        class Session:
            def __init__(self, code): self.code = code
            def get(self, *a, **k):
                if self.code is None:
                    raise game_link.requests.ConnectionError("down")
                return Resp(self.code)

        link = game_link.GameLink("http://gm", "t", session=Session(401))
        self.assertIsNone(link.state())
        self.assertIn("GAME_TOKEN", link.error)
        link = game_link.GameLink("http://gm", "t", session=Session(None))
        self.assertIsNone(link.state())
        self.assertIn("injoignable", link.error)
        link = game_link.GameLink("http://gm", "t", session=Session(200))
        self.assertEqual(link.state(), {"activity": None})
        self.assertEqual(link.error, "")


class TestGameLink(unittest.TestCase):

    def test_disabled_without_url_or_token(self):
        self.assertFalse(game_link.GameLink("", "t").enabled)
        self.assertFalse(game_link.GameLink("http://gm", "").enabled)
        self.assertIsNone(game_link.GameLink("", "").state())
        self.assertIsNone(game_link.GameLink("", "").play({"type": "start_game"}, {}, "?"))

    def test_state_sends_the_token(self):
        session = FakeSession(FakeResponse(CATALOG))
        link = game_link.GameLink("http://gm/", "secret", session)
        self.assertEqual(link.state(), CATALOG)
        method, url, kw = session.calls[0]
        self.assertEqual((method, url), ("GET", "http://gm/state"))
        self.assertEqual(kw["headers"], {"Authorization": "Bearer secret"})

    def test_unreachable_is_none(self):
        link = game_link.GameLink("http://gm", "s", FakeSession(error=requests.ConnectionError()))
        self.assertIsNone(link.state())
        self.assertIsNone(link.play({"type": "game_move", "move": {}}, {}, ""))
        link = game_link.GameLink("http://gm", "s", FakeSession(FakeResponse({}, 403)))
        self.assertIsNone(link.state())

    def test_play_start_and_move(self):
        result = {"event": {"kind": "started", "text": "…"}, "state": RUNNING}
        session = FakeSession(FakeResponse(result))
        link = game_link.GameLink("http://gm", "s", session)
        player = {"login": "alice"}
        self.assertEqual(link.play({"type": "start_game", "game": "hangman"}, player, "on joue ?"),
                         result)
        self.assertEqual(session.calls[0][1], "http://gm/start")
        self.assertEqual(session.calls[0][2]["json"], {"game": "hangman", "player": player,
                                                       "message": "on joue ?", "options": {}})
        link.play({"type": "game_move", "move": "e"}, player, "e")
        self.assertEqual(session.calls[1][1], "http://gm/move")
        self.assertEqual(session.calls[1][2]["json"], {"move": {}, "player": player})

    def test_answer(self):
        session = FakeSession(FakeResponse({"event": {"kind": "started"}, "state": RUNNING}))
        link = game_link.GameLink("http://gm", "s", session)
        self.assertEqual(link.answer({"id": "a1"}, "Petit animal.", {"source": "webui"})["event"],
                         {"kind": "started"})
        self.assertEqual(session.calls[0][1], "http://gm/answer")
        self.assertEqual(session.calls[0][2]["json"],
                         {"id": "a1", "text": "Petit animal.", "player": {"source": "webui"}})

    def test_play_answer_without_event_is_none(self):
        link = game_link.GameLink("http://gm", "s", FakeSession(FakeResponse({"error": "x"})))
        self.assertIsNone(link.play({"type": "game_move", "move": {}}, {}, ""))


class TestActivityWatch(unittest.TestCase):

    def test_activity_id(self):
        self.assertEqual(game_link.activity_id(RUNNING), "hangman")
        self.assertIsNone(game_link.activity_id(CATALOG))
        self.assertIsNone(game_link.activity_id(None))

    def test_change_and_repeat(self):
        watch = game_link.ActivityWatch(repeat_s=15)
        self.assertEqual(watch.update(CATALOG, 0), '{"activity": null}')
        self.assertIsNone(watch.update(CATALOG, 2))
        self.assertEqual(watch.update(RUNNING, 4), '{"activity": "hangman"}')
        self.assertIsNone(watch.update(RUNNING, 10))
        self.assertEqual(watch.update(RUNNING, 19), '{"activity": "hangman"}')   # repeated

    def test_unreachable_publishes_nothing(self):
        watch = game_link.ActivityWatch()
        watch.update(RUNNING, 0)
        self.assertIsNone(watch.update(None, 100))
        self.assertEqual(watch.current, "hangman")



class TestStorySection(unittest.TestCase):

    def test_no_game_during_a_story(self):
        section = game_link.prompt_section({"activity": {
            "game": "story", "summary": "Histoire en cours : Voyage."}, "games": [{"id": "hangman"}]})
        self.assertIn("Voyage", section)
        self.assertIn("aucun jeu", section)
        self.assertNotIn("Coups possibles", section)

if __name__ == "__main__":
    unittest.main()
