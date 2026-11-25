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


class TestHttp(unittest.TestCase):

    def setUp(self):
        self.announced = []
        self.master = GameMaster({"count": CountGame})
        announcer = game_master.Announcer.__new__(game_master.Announcer)
        announcer.announce = lambda event, state: self.announced.append(event.kind)
        handler = game_master.make_handler(self.master, game_master.Feed(), announcer, "secret")
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

    def test_overlay_page_with_query_token(self):
        with urllib.request.urlopen(self.base + "/overlay?token=secret", timeout=5) as resp:
            self.assertIn(b"/overlay/stream", resp.read())

    def test_bad_json(self):
        req = urllib.request.Request(self.base + "/move", data=b"[1, 2]",
                                     headers={"Authorization": "Bearer secret"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
