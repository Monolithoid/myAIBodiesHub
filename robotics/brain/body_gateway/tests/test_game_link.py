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
        self.assertIn("« Indice : y. »", text)
        self.assertIn("Ne le répète pas", text)


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


if __name__ == "__main__":
    unittest.main()
