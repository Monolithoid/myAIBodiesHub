"""
game_core — rules shared by every game of game-master, without network (ADR 010).

The code decides, the LLM speaks: a game settles each move and returns an
Event (a fact in French for the LLM, a canned sentence in case the LLM is late,
points won).  The LLM never sees the solution: it only gets Game.summary(),
the public state.

  Game         interface of one game: start, on_move, public_view, summary, is_over
  Player       who plays: Twitch login, name, roles (chat badges), source
  GameMaster   one activity at a time, who may start, per-viewer delay, recap,
               stop, scores; notifies a listener (overlay) on each change

Hidden LLM calls: a game may need the LLM for something NAO must not see (the
definition of the hangman word, the mystery's archivist).  It then exposes
Game.pending_ask(), {"id", "purpose", "system", "user"}; game-master returns it
with the event, body-gateway runs that call outside NAO's prompt and posts the
text back (GameMaster.answer → Game.on_answer), which settles the event.
  ScoreStore   points per game and per viewer (SQLite)
"""

import sqlite3
import threading
import time
from dataclasses import dataclass, field

# Twitch badges forwarded by twitch-input, in the order they are checked.
ROLES = ("broadcaster", "moderator", "vip", "subscriber")
EVERYONE = "everyone"


@dataclass
class Player:
    login: str = ""
    name: str = ""
    roles: frozenset = frozenset()
    source: str = "twitch"      # twitch | webui | voice | regie

    @classmethod
    def from_dict(cls, data: dict | None) -> "Player":
        data = data if isinstance(data, dict) else {}
        roles = data.get("roles") or []
        return cls(
            login=str(data.get("login") or "").strip().lower(),
            name=str(data.get("name") or data.get("login") or "").strip(),
            roles=frozenset(str(r) for r in roles if str(r) in ROLES),
            source=str(data.get("source") or "twitch"),
        )

    @property
    def is_streamer(self) -> bool:
        """Open WebUI, the room voice and the control room belong to the streamer."""
        return self.source != "twitch" or "broadcaster" in self.roles

    def label(self) -> str:
        return self.name or self.login or "le streamer"


@dataclass
class Event:
    """What happened, settled by the code.

    kind     short id (started, good, bad, won, lost, invalid, recap, cancelled, refused…)
    text     the fact, in French, given to the LLM so NAO reacts to it
    say      canned sentence NAO says if the LLM does not answer
    ok       False when the request was refused (no game, busy, not allowed…)
    points   {login: points} won with this move
    mood     tone suggested to NAO's reaction (joie, déception, suspense…), optional
    read     text NAO must say word for word after its reaction (the hangman
             definition): body-gateway adds it to the speech, the LLM may drop it
    """
    kind: str
    text: str
    say: str = ""
    ok: bool = True
    points: dict = field(default_factory=dict)
    mood: str = ""
    read: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "say": self.say or self.text,
                "ok": self.ok, "points": dict(self.points), "mood": self.mood,
                "read": self.read}


class Game:
    """One game.  Subclasses set id, name and how_to_play, and implement the rest.

    how_to_play  for the LLM, in French: the moves and their JSON form, e.g.
                 'une lettre : {"type": "letter", "letter": "e"}'
    start_help   for the LLM, in French: the start options, e.g.
                 '"options": {"difficulty": "facile|moyen|difficile"}'
    """
    id = ""
    name = ""
    how_to_play = ""
    start_help = ""

    def start(self, player: Player, options: dict) -> Event:
        raise NotImplementedError

    def on_move(self, move: dict, player: Player) -> Event:
        raise NotImplementedError

    def on_tick(self, now: float) -> Event | None:
        """Called about once a second; an Event is announced by NAO."""
        return None

    def public_view(self) -> dict:
        """What the overlay shows (never the solution while the game runs)."""
        return {}

    def summary(self) -> str:
        """Public state in French for the LLM (never the solution)."""
        return ""

    def recap(self) -> str:
        """Context retold to a viewer who arrives mid-game; the summary by default."""
        return self.summary()

    def is_over(self) -> bool:
        return False

    def pending_ask(self) -> dict | None:
        """Hidden LLM call the game waits for ({"id", "purpose", "system", "user"})."""
        return None

    def on_answer(self, ask_id: str, text: str) -> Event | None:
        """Text of a hidden call ("" when it failed) → the event it settles."""
        return None


def allowed_to_start(player: Player, start_roles: list[str]) -> bool:
    """GAME_START_ROLES: "everyone", or a list among moderator, vip, subscriber."""
    if player.is_streamer or EVERYONE in start_roles:
        return True
    return bool(player.roles & set(start_roles))


def is_question(message: str) -> bool:
    return "?" in (message or "")


class GameMaster:
    """One activity at a time; every call returns an Event, refused or not."""

    def __init__(self, games: dict, start_roles: list[str] | None = None,
                 move_cooldown_s: float = 0.0, scores: "ScoreStore | None" = None,
                 on_change=None, clock=time.monotonic):
        self._factories = dict(games)   # id → class (or factory) of Game
        self.start_roles = list(start_roles or [EVERYONE])
        self.move_cooldown_s = move_cooldown_s
        self.scores = scores
        self.on_change = on_change
        self._clock = clock
        self._lock = threading.RLock()
        self._game: Game | None = None
        self._started_by = ""
        self._last_move: dict = {}      # login → clock of its last move
        self._last_event: dict | None = None
        self._last_player = ""
        self._final: dict | None = None   # last view of the game that just ended (overlay)
        self.version = 0                  # bumped on every change: the overlay reveals by version

    # ── Read ────────────────────────────────────────────────────────────────

    def catalog(self) -> list[dict]:
        return [{"id": gid, "name": f.name, "how": f.how_to_play, "start": f.start_help}
                for gid, f in self._factories.items()]

    def state(self) -> dict:
        """Public state: what body-gateway puts in the prompt, what the overlay shows."""
        with self._lock:
            game = self._game
            activity = None
            if game is not None:
                activity = {
                    "game": game.id, "name": game.name, "how": game.how_to_play,
                    "summary": game.summary(), "view": game.public_view(),
                    "started_by": self._started_by, "last_player": self._last_player,
                }
            return {"activity": activity, "games": self.catalog(),
                    "last_event": self._last_event, "final": self._final,
                    "version": self.version}

    @property
    def busy(self) -> bool:
        return self._game is not None

    def pending_ask(self) -> dict | None:
        with self._lock:
            return self._game.pending_ask() if self._game is not None else None

    # ── Write ───────────────────────────────────────────────────────────────

    def start(self, game_id: str, player: Player, message: str = "",
              options: dict | None = None) -> Event:
        with self._lock:
            if self._game is not None:
                return self._done(Event(
                    "busy", f"Une partie de {self._game.name} est déjà en cours : "
                            "une seule activité à la fois.",
                    f"On a déjà une partie de {self._game.name} en cours !", ok=False), player)
            factory = self._factories.get(str(game_id))
            if factory is None:
                names = ", ".join(f.name for f in self._factories.values()) or "aucun"
                return self._done(Event(
                    "unknown_game", f"Ce jeu n'existe pas. Jeux disponibles : {names}.",
                    "Je ne connais pas ce jeu.", ok=False), player)
            if player.source == "twitch" and not is_question(message):
                return self._done(Event(
                    "not_a_question", "Pour lancer une partie, il faut le demander par une question.",
                    "Demande-le moi par une question !", ok=False), player)
            if not allowed_to_start(player, self.start_roles):
                return self._done(Event(
                    "not_allowed", f"{player.label()} n'a pas le droit de lancer une partie.",
                    "Désolé, tu ne peux pas lancer de partie.", ok=False), player)
            game = factory()
            event = game.start(player, options or {})
            if event.ok:
                self._game = game
                self._started_by = player.label()
                self._last_move.clear()
                self._final = None
            return self._done(event, player)

    def move(self, move: dict, player: Player) -> Event:
        with self._lock:
            game = self._game
            if game is None:
                return self._done(Event(
                    "no_game", "Aucune partie n'est en cours.",
                    "Il n'y a pas de partie en cours.", ok=False), player)
            move = move if isinstance(move, dict) else {}
            if move.get("type") == "recap":
                return self._done(Event("recap", f"Récapitulatif de la partie de {game.name} : "
                                                 f"{game.recap()}", game.recap()), player,
                                  count=False)
            now = self._clock()
            last = self._last_move.get(player.login)
            if (self.move_cooldown_s > 0 and player.login and last is not None
                    and now - last < self.move_cooldown_s):
                wait = int(self.move_cooldown_s - (now - last)) + 1
                return self._done(Event(
                    "cooldown", f"{player.label()} doit attendre {wait} s avant de rejouer.",
                    f"Attends un peu {player.label()}, laisse jouer les autres.", ok=False), player)
            event = game.on_move(move, player)
            if event.ok and player.login:
                self._last_move[player.login] = now
            return self._finish_if_over(event, player)

    def answer(self, ask_id: str, text: str, player: Player) -> Event:
        """Text of a hidden LLM call the running game asked for."""
        with self._lock:
            game = self._game
            ask = game.pending_ask() if game is not None else None
            if ask is None or ask.get("id") != ask_id:
                return self._done(Event("no_ask", "Aucun appel caché en attente.", ok=False),
                                  player, count=False)
            event = game.on_answer(ask_id, str(text or ""))
            if event is None:
                event = Event("answered", "Réponse reçue.")
            return self._finish_if_over(event, player)

    def tick(self) -> Event | None:
        with self._lock:
            if self._game is None:
                return None
            event = self._game.on_tick(self._clock())
            if event is None:
                return None
            return self._finish_if_over(event, Player(source="regie"))

    def stop(self, player: Player) -> Event:
        with self._lock:
            if self._game is None:
                return self._done(Event("no_game", "Aucune partie n'est en cours.",
                                        "Il n'y a pas de partie en cours.", ok=False), player)
            name = self._game.name
            self._game = None
            return self._done(Event(
                "cancelled", f"La partie de {name} a été annulée par {player.label()}.",
                f"La partie de {name} est annulée."), player)

    # ── Internals ───────────────────────────────────────────────────────────

    def _finish_if_over(self, event: Event, player: Player) -> Event:
        game = self._game
        if self.scores is not None and event.points and game is not None:
            names = {player.login: player.label()}
            for login, pts in event.points.items():
                self.scores.add(game.id, login, names.get(login, login), int(pts))
        if game is not None and game.is_over():
            self._final = {"game": game.id, "name": game.name, "view": game.public_view()}
            self._game = None
        return self._done(event, player)

    def _done(self, event: Event, player: Player, count: bool = True) -> Event:
        self._last_event = {**event.to_dict(), "by": player.label()}
        if count and event.ok and player.login:
            self._last_player = player.label()
        self.version += 1
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass
        return event


class ScoreStore:
    """Points per game and per viewer, kept across restarts (SQLite)."""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS scores (game TEXT, login TEXT, name TEXT,"
                " points INTEGER DEFAULT 0, PRIMARY KEY (game, login))")
            self._db.commit()

    def add(self, game: str, login: str, name: str, points: int):
        if not login:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO scores (game, login, name, points) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (game, login) DO UPDATE SET points = points + excluded.points,"
                " name = excluded.name", (game, login, name, points))
            self._db.commit()

    def top(self, game: str, n: int = 5) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name, points FROM scores WHERE game = ? ORDER BY points DESC, name"
                " LIMIT ?", (game, n)).fetchall()
        return [{"name": name, "points": points} for name, points in rows]
