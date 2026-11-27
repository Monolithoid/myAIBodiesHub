"""
web — the /stories/… routes of the control room (the token is checked by regie.py).

  GET  /stories/list                      {"stories": [...]}
  GET  /stories/state?story=              state of the player (bookmark of ?story= when stopped)
  POST /stories/play  {"story"}
  POST /stories/pause
  POST /stories/stop
  POST /stories/seek  {"story", "unit": "passage"|"chapter", "delta": -1|1}

Answers: the player's state, or 409 {"error", ...state} for a refused command.
"""

import os
import threading

from storyteller import player as player_module

STORIES_DIR = os.environ.get("STORIES_DIR", "/data/stories")


class StoryRoutes:

    def __init__(self, env=os.environ, log=lambda text: None):
        self.root = env.get("STORIES_DIR", "").strip() or STORIES_DIR
        self.env = env
        self.log = log
        self._player = None
        self._lock = threading.Lock()

    def player(self) -> player_module.Player:
        """Built at the first request: the volume may be missing (stories unused)."""
        with self._lock:      # one reader only, whatever the first requests
            if self._player is None:
                os.makedirs(self.root, exist_ok=True)
                self._player = player_module.Player(
                    player_module.Library(self.root),
                    player_module.Bookmarks(os.path.join(self.root, "bookmarks.db")),
                    player_module.BodyGateway(
                        self.env.get("BODY_GATEWAY_URL", "").strip() or "http://chat-relay:8095"),
                    player_module.GameMasterActivity(
                        self.env.get("GAME_MASTER_URL", "").strip() or "http://game-master:8098",
                        self.env.get("GAME_TOKEN", "").strip()),
                    log=self.log)
            return self._player

    def get(self, path: str, query: dict) -> tuple[int, dict]:
        try:
            if path == "/stories/list":
                return 200, {"stories": self.player().library.list()}
            if path == "/stories/state":
                return 200, self.player().state(query.get("story", [""])[0])
        except OSError as e:
            return 503, {"error": f"histoires indisponibles : {e}"}
        return 404, {"error": "not found"}

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        try:
            player = self.player()
            story = str(body.get("story") or "")
            if path == "/stories/play":
                return 200, player.play(story)
            if path == "/stories/pause":
                return 200, player.pause()
            if path == "/stories/stop":
                return 200, player.stop()
            if path == "/stories/seek":
                return 200, player.seek(str(body.get("unit")), body.get("delta"), story)
        except player_module.PlayerError as e:
            return 409, {**self.player().state(), "error": str(e)}
        except OSError as e:
            return 503, {"error": f"histoires indisponibles : {e}"}
        return 404, {"error": "not found"}
