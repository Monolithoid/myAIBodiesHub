"""Tests of game-master without the network: shared rules, then the HTTP API on a local port.

    cd robotics/games && python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game_core  # noqa: E402
import game_master  # noqa: E402
import story  # noqa: E402
from game_core import Event, GameMaster, Player  # noqa: E402


class CountGame(game_core.Game):
    """Say the secret number (3); 2 wrong guesses and the game is lost."""
    id = "count"
    name = "Compte"
    how_to_play = 'un nombre : {"type": "number", "value": 3}'

    def __init__(self):
        self.misses = 0
        self.found = False

    def start(self, player, options):
        return Event("started", "Partie lancée.")

    def on_move(self, move, player):
        if move.get("type") != "number":
            return Event("invalid", "Coup non compris.", ok=False)
        if move.get("value") == 3:
            self.found = True
            return Event("won", f"{player.label()} a trouvé.", points={player.login: 5})
        self.misses += 1
        return Event("lost" if self.misses >= 2 else "bad", "Raté.")

    def summary(self):
        return f"{self.misses} erreur(s)"

    def public_view(self):
        return {"big": "? ? ?"}

    def is_over(self):
        return self.found or self.misses >= 2


VIEWER = Player("alice", "Alice")
OTHER = Player("bob", "Bob")


class TestPlayer(unittest.TestCase):

    def test_from_dict_keeps_known_roles_only(self):
        p = Player.from_dict({"login": "Alice", "name": "Alice",
                              "roles": ["vip", "admin", "moderator"], "source": "twitch"})
        self.assertEqual(p.login, "alice")
        self.assertEqual(p.roles, frozenset({"vip", "moderator"}))
        self.assertFalse(p.is_streamer)

    def test_other_sources_are_the_streamer(self):
        self.assertTrue(Player.from_dict({"source": "webui"}).is_streamer)
        self.assertTrue(Player.from_dict({"roles": ["broadcaster"]}).is_streamer)
        self.assertTrue(Player.from_dict(None).source == "twitch")

    def test_allowed_to_start(self):
        self.assertTrue(game_core.allowed_to_start(VIEWER, ["everyone"]))
        self.assertFalse(game_core.allowed_to_start(VIEWER, ["moderator", "vip"]))
        self.assertTrue(game_core.allowed_to_start(Player("v", roles=frozenset({"vip"})),
                                                   ["moderator", "vip"]))
        self.assertTrue(game_core.allowed_to_start(Player(source="webui"), ["moderator"]))


class TestGameMaster(unittest.TestCase):

    def setUp(self):
        self.now = 100.0
        self.changes = 0
        self.master = GameMaster({"count": CountGame}, clock=lambda: self.now,
                                 on_change=self._changed)

    def _changed(self):
        self.changes += 1

    def test_start_needs_a_question(self):
        event = self.master.start("count", VIEWER, "on joue au compte")
        self.assertEqual(event.kind, "not_a_question")
        self.assertFalse(event.ok)
        self.assertFalse(self.master.busy)
        # Open WebUI (the streamer) needs no question mark.
        self.assertTrue(self.master.start("count", Player(source="webui")).ok)

    def test_unknown_game(self):
        event = self.master.start("chess", VIEWER, "on joue ?")
        self.assertEqual(event.kind, "unknown_game")
        self.assertIn("Compte", event.text)

    def test_one_activity_at_a_time(self):
        self.assertTrue(self.master.start("count", VIEWER, "on joue ?").ok)
        event = self.master.start("count", OTHER, "et moi ?")
        self.assertEqual(event.kind, "busy")
        self.assertFalse(event.ok)

    def test_roles(self):
        master = GameMaster({"count": CountGame}, start_roles=["moderator"])
        self.assertEqual(master.start("count", VIEWER, "on joue ?").kind, "not_allowed")
        mod = Player("m", "M", frozenset({"moderator"}))
        self.assertTrue(master.start("count", mod, "on joue ?").ok)

    def test_move_without_game(self):
        self.assertEqual(self.master.move({"type": "number", "value": 3}, VIEWER).kind, "no_game")

    def test_game_runs_to_the_end_and_frees_the_slot(self):
        self.master.start("count", VIEWER, "on joue ?")
        self.assertEqual(self.master.move({"type": "number", "value": 1}, OTHER).kind, "bad")
        self.assertEqual(self.master.state()["activity"]["last_player"], "Bob")
        event = self.master.move({"type": "number", "value": 3}, VIEWER)
        self.assertEqual(event.kind, "won")
        self.assertFalse(self.master.busy)
        self.assertIsNone(self.master.state()["activity"])
        self.assertEqual(self.master.state()["last_event"]["kind"], "won")

    def test_lost_game_frees_the_slot(self):
        self.master.start("count", VIEWER, "on joue ?")
        self.master.move({"type": "number", "value": 1}, VIEWER)
        self.assertEqual(self.master.move({"type": "number", "value": 2}, OTHER).kind, "lost")
        self.assertFalse(self.master.busy)

    def test_recap_is_public_state(self):
        self.master.start("count", VIEWER, "on joue ?")
        event = self.master.move({"type": "recap"}, OTHER)
        self.assertEqual(event.kind, "recap")
        self.assertIn("0 erreur(s)", event.text)
        self.assertTrue(self.master.busy)

    def test_cooldown(self):
        master = GameMaster({"count": CountGame}, move_cooldown_s=30, clock=lambda: self.now)
        master.start("count", VIEWER, "on joue ?")
        master.move({"type": "number", "value": 1}, VIEWER)
        self.assertEqual(master.move({"type": "number", "value": 3}, VIEWER).kind, "cooldown")
        self.now += 31
        self.assertEqual(master.move({"type": "number", "value": 3}, VIEWER).kind, "won")

    def test_no_cooldown_by_default(self):
        self.master.start("count", VIEWER, "on joue ?")
        self.master.move({"type": "number", "value": 1}, VIEWER)
        self.assertEqual(self.master.move({"type": "number", "value": 3}, VIEWER).kind, "won")

    def test_invalid_move_does_not_count(self):
        self.master.start("count", VIEWER, "on joue ?")
        self.assertEqual(self.master.move({"type": "letter"}, VIEWER).kind, "invalid")
        self.assertEqual(self.master.state()["activity"]["summary"], "0 erreur(s)")

    def test_stop(self):
        self.master.start("count", VIEWER, "on joue ?")
        event = self.master.stop(Player("m", "Modo"))
        self.assertEqual(event.kind, "cancelled")
        self.assertIn("Modo", event.text)
        self.assertFalse(self.master.busy)
        self.assertEqual(self.master.stop(Player("m", "Modo")).kind, "no_game")

    def test_state_never_holds_more_than_the_public_view(self):
        self.master.start("count", VIEWER, "on joue ?")
        activity = self.master.state()["activity"]
        self.assertEqual(set(activity), {"game", "name", "how", "summary", "view",
                                         "started_by", "last_player"})
        self.assertGreater(self.changes, 0)

    def test_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            scores = game_core.ScoreStore(os.path.join(tmp, "games.db"))
            master = GameMaster({"count": CountGame}, scores=scores)
            for _ in range(2):
                master.start("count", VIEWER, "on joue ?")
                master.move({"type": "number", "value": 3}, VIEWER)
            self.assertEqual(scores.top("count"), [{"name": "Alice", "points": 10}])
            self.assertEqual(scores.top("other"), [])


class TestSettings(unittest.TestCase):

    SCHEMA = [{"key": "n", "label": "N", "type": "int", "min": 1, "max": 9, "default": 3},
              {"key": "f", "label": "F", "type": "float", "min": 0, "max": 1, "default": 0.0},
              {"key": "start_roles", "label": "R", "type": "roles", "default": ["everyone"]}]

    def test_defaults_update_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.db")
            s = game_core.Settings(self.SCHEMA, path)
            self.assertEqual(s.values(), {"n": 3, "f": 0.0, "start_roles": ["everyone"]})
            s.update({"n": "5", "start_roles": ["vip", "moderator"]})
            again = game_core.Settings(self.SCHEMA, path)
            self.assertEqual(again.values()["n"], 5)
            self.assertEqual(again.values()["start_roles"], ["vip", "moderator"])

    def test_refused_values_change_nothing(self):
        s = game_core.Settings(self.SCHEMA)
        for bad in ({"n": 10}, {"n": 2.5}, {"n": "x"}, {"f": 2}, {"start_roles": ["admin"]},
                    {"start_roles": []}, {"other": 1}, {"f": 0.5, "n": 99}):
            with self.assertRaises(ValueError, msg=bad):
                s.update(bad)
        self.assertEqual(s.values()["f"], 0.0)

    def test_master_applies_them(self):
        s = game_core.Settings(self.SCHEMA + [
            {"key": "move_cooldown_s", "label": "D", "type": "float", "min": 0, "max": 9,
             "default": 0.0}])
        master = GameMaster({"count": CountGame}, settings=s)
        master.apply(s.update({"start_roles": ["vip"], "move_cooldown_s": 5}))
        self.assertEqual(master.start_roles, ["vip"])
        self.assertEqual(master.move_cooldown_s, 5.0)


class TestStage(unittest.TestCase):

    def setUp(self):
        self.now = 0.0
        self.feed = game_master.Feed()
        self.q = self.feed.subscribe()
        self.stage = game_master.Stage(self.feed, {"version": 0}, hold_s=15,
                                       clock=lambda: self.now)

    def published(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait()["version"])
        return out

    def test_held_until_revealed(self):
        self.stage.changed({"version": 1})
        self.stage.changed({"version": 2})
        self.stage.changed({"version": 3})
        self.assertEqual(self.published(), [])
        self.assertEqual(self.stage.shown["version"], 0)
        self.assertTrue(self.stage.reveal(2))
        self.assertEqual(self.published(), [2])
        self.assertFalse(self.stage.reveal(2))        # already shown
        self.assertTrue(self.stage.reveal(9))         # newest held up to 9
        self.assertEqual(self.stage.shown["version"], 3)

    def test_shown_anyway_after_the_hold(self):
        self.stage.changed({"version": 1})
        self.now = 10.0
        self.stage.changed({"version": 2})
        self.stage.expire()
        self.assertEqual(self.published(), [])
        self.now = 15.0
        self.stage.expire()
        self.assertEqual(self.published(), [1])
        self.now = 25.0
        self.stage.expire()
        self.assertEqual(self.published(), [2])

    def test_no_hold(self):
        stage = game_master.Stage(self.feed, {"version": 0}, hold_s=0)
        stage.changed({"version": 1})
        self.assertEqual(self.published(), [1])


class TestHttp(unittest.TestCase):

    def setUp(self):
        self.announced = []
        self.master = GameMaster({"count": CountGame})
        announcer = game_master.Announcer.__new__(game_master.Announcer)
        announcer.announce = lambda event, state: self.announced.append(event.kind)
        self.stage = game_master.Stage(game_master.Feed(), self.master.state())
        self.master.on_change = lambda: self.stage.changed(self.master.state())
        handler = game_master.make_handler(self.master, self.stage, announcer, "secret")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _call(self, path, body=None, token="secret"):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_settings_and_scores_routes(self):
        self.master.settings = game_core.Settings(game_master.SETTINGS_SCHEMA)
        code, data = self._call("/settings")
        self.assertEqual(code, 200)
        self.assertEqual(data["values"]["hangman_max_errors"], 7)
        self.assertEqual(self._call("/settings", {"hangman_max_errors": 5})[1]["values"]
                         ["hangman_max_errors"], 5)
        code, data = self._call("/settings", {"hangman_max_errors": 50})
        self.assertEqual(code, 400)
        self.assertIn("entre 3 et 12", data["error"])
        self.assertEqual(self._call("/scores?game=count")[1], {"game": "count", "top": []})
        self.assertEqual(self._call("/scores/reset", {}), (200, {"ok": True}))

    def test_check(self):
        self.assertEqual(self._call("/check", {"text": "x"}), (200, {"leak": False}))

    def test_seen(self):
        self.assertEqual(self._call("/seen", {"logins": ["Alice", "bob", 3, ""]}), (200, {"ok": True}))
        self.assertEqual(self.master.present(), {"alice", "bob"})

    def test_reveal(self):
        _, answer = self._call("/start", {"game": "count", "player": {"login": "a"},
                                          "message": "on joue ?"})
        version = answer["state"]["version"]
        self.assertEqual(self.stage.shown["activity"], None)   # held: NAO has not spoken
        self.assertEqual(self._call("/reveal", {"version": version}), (200, {"revealed": True}))
        self.assertEqual(self.stage.shown["activity"]["game"], "count")
        self.assertEqual(self._call("/reveal", {})[0], 400)

    def test_ping_needs_no_token(self):
        self.assertEqual(self._call("/ping", token=""), (200, {"ok": True, "service": "game-master"}))

    def test_token_required(self):
        self.assertEqual(self._call("/state", token="")[0], 403)
        self.assertEqual(self._call("/state", token="wrong")[0], 403)
        self.assertEqual(self._call("/start", {"game": "count"}, token="")[0], 403)

    def test_start_move_stop(self):
        player = {"login": "alice", "name": "Alice", "roles": [], "source": "twitch"}
        code, data = self._call("/start", {"game": "count", "player": player, "message": "on joue ?"})
        self.assertEqual(code, 200)
        self.assertEqual(data["event"]["kind"], "started")
        self.assertEqual(data["state"]["activity"]["game"], "count")
        code, data = self._call("/move", {"move": {"type": "number", "value": 1}, "player": player})
        self.assertEqual(data["event"]["kind"], "bad")
        code, data = self._call("/stop", {"player": {"source": "regie", "name": "régie"}})
        self.assertEqual(data["event"]["kind"], "cancelled")
        self.assertIsNone(data["state"]["activity"])
        self.assertEqual(self.announced, ["cancelled"])

    def test_hidden_call_round_trip(self):
        import hangman

        class Word(hangman.Hangman):
            def __init__(self):
                super().__init__(words={"medium": ["araignée"]})

        self.master._factories["hangman"] = Word
        code, data = self._call("/start", {"game": "hangman", "player": {"source": "webui"}})
        self.assertEqual(data["event"]["kind"], "started")
        ask = data["ask"]
        self.assertIn("araignée", ask["user"])
        self.assertNotIn("araign", json.dumps(data["state"]))
        code, data = self._call("/answer", {"id": ask["id"], "text": "Petite bête à huit pattes.",
                                            "player": {"source": "webui"}})
        self.assertNotIn("ask", data)
        # The definition goes to the overlay and is read by the code, never to the LLM.
        self.assertNotIn("huit pattes", data["state"]["activity"]["summary"])
        self.assertIn("huit pattes", data["state"]["activity"]["view"]["definition"])
        self.assertIn("huit pattes", data["event"]["read"])

    def test_overlay_page_with_query_token(self):
        with urllib.request.urlopen(self.base + "/overlay?token=secret", timeout=5) as resp:
            self.assertIn(b"/overlay/stream", resp.read())

    def test_bad_json(self):
        req = urllib.request.Request(self.base + "/move", data=b"[1, 2]",
                                     headers={"Authorization": "Bearer secret"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)



class TestStory(unittest.TestCase):
    """The "story" activity: the control room's only, one activity at a time."""

    VIEW = {"title": "Voyage", "author": "Verne", "year": 1864, "chapter": 1, "chapters": 45}
    REGIE = Player(name="la régie", source="regie")

    def setUp(self):
        self.master = GameMaster({"count": CountGame, "story": story.Story})

    def test_only_the_control_room(self):
        self.assertEqual([g["id"] for g in self.master.catalog()], ["count"])
        viewer = Player(login="a", name="A")
        self.assertEqual(self.master.start("story", viewer, "une histoire ?").kind, "unknown_game")
        self.assertTrue(self.master.start("story", self.REGIE, options=self.VIEW).ok)
        self.assertEqual(self.master.state()["activity"]["view"], self.VIEW)

    def test_one_activity_at_a_time_both_ways(self):
        self.master.start("count", self.REGIE)
        self.assertEqual(self.master.start("story", self.REGIE, options=self.VIEW).kind, "busy")
        self.assertEqual(self.master.stop(self.REGIE, "story").kind, "no_game")
        self.assertTrue(self.master.busy)                   # the game is not cancelled
        self.assertEqual(self.master.state()["last_event"]["kind"], "started")   # overlay untouched
        self.master.stop(self.REGIE)
        self.master.start("story", self.REGIE, options=self.VIEW)
        self.assertIn("histoire", self.master.start("count", self.REGIE).text)
        self.assertFalse(self.master.move({"type": "number", "value": 3}, self.REGIE).ok)
        event = self.master.stop(self.REGIE, "story")
        self.assertTrue(event.ok)
        self.assertEqual(event.kind, "closed")     # the overlay clears: no "game over" panel
        self.assertFalse(self.master.busy)

    def test_view_update_keeps_the_last_event(self):
        self.assertFalse(self.master.update_view("story", self.VIEW))
        self.master.start("story", self.REGIE, options=self.VIEW)
        before = self.master.state()
        self.assertTrue(self.master.update_view("story", {**self.VIEW, "chapter": 2, "x": 1}))
        after = self.master.state()
        self.assertEqual(after["activity"]["view"], {**self.VIEW, "chapter": 2})
        self.assertEqual(after["last_event"], before["last_event"])
        self.assertGreater(after["version"], before["version"])

if __name__ == "__main__":
    unittest.main()
