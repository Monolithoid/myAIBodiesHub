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
Long games (the mystery, ADR 010) are persistent: GameMaster saves their state
after each change (SaveStore) and restores it at start-up, so they last over
several streams; they stay the running activity until solved or cancelled.

  ScoreStore   points per game and per viewer (SQLite)
  SaveStore    the saved state of the running persistent game (SQLite)
"""

import json
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
    source: str = "twitch"      # twitch | webui | voice | regie | nao (NAO plays too)

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

    @property
    def is_nao(self) -> bool:
        return self.source == "nao"

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
    read_as  what "read" is, in French, for the LLM ("un indice" by default)
    keep     NAO's reaction is posted back to the game (Game.note), which keeps it
    """
    kind: str
    text: str
    say: str = ""
    ok: bool = True
    points: dict = field(default_factory=dict)
    mood: str = ""
    read: str = ""
    read_as: str = ""
    keep: bool = False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "say": self.say or self.text,
                "ok": self.ok, "points": dict(self.points), "mood": self.mood,
                "read": self.read, "read_as": self.read_as, "keep": self.keep}


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
    # Set by GameMaster before start(): logins of the viewers still in the chat
    # (wrote there or played lately). None = no such source (the game alone).
    present = None
    persistent = False      # saved after each change, restored after a restart
    hidden = False          # not offered to the viewers: only the control room starts it
    start_cooldown_s = 0.0  # delay before the next start, counted from a game that got ready

    def busy_event(self) -> "Event":
        """Refusal of another start while this activity runs."""
        return Event("busy", f"Une partie de {self.name} est déjà en cours : "
                             "une seule activité à la fois.",
                     f"On a déjà une partie de {self.name} en cours !", ok=False)

    def cancelled_event(self, player: "Player") -> "Event":
        return Event("cancelled", f"La partie de {self.name} a été annulée par {player.label()}.",
                     f"La partie de {self.name} est annulée.")

    def configure(self, settings: dict):
        """Settings of the control room (Settings.values()), before start()."""

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

    def is_ready(self) -> bool:
        """False while the game is still being prepared (hidden calls)."""
        return True

    def is_aborted(self) -> bool:
        """Over without having been played (preparation failed): no final view."""
        return False

    def save(self) -> dict | None:
        """JSON state of a persistent game; None while there is nothing to keep."""
        return None

    def restore(self, state: dict):
        """Rebuilds a saved game (instead of start); ValueError if it cannot."""
        raise ValueError("this game cannot be restored")

    def note(self, text: str) -> bool:
        """NAO's reaction to an event with "keep"; True when the game kept it."""
        return False

    def clue(self) -> str:
        """Text NAO reads word for word with the recap, never shown to the LLM
        (the hangman definition: the LLM would guess the word from it)."""
        return ""

    def leaks(self, text: str) -> bool:
        """True when NAO's text gives the solution away (checked before NAO speaks)."""
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
                 on_change=None, clock=time.monotonic, settings: "Settings | None" = None,
                 saves: "SaveStore | None" = None):
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
        self.presence_s = 600.0           # a viewer who wrote within this delay is in the chat
        self._seen: dict = {}             # login → clock of its last chat message or move (memory only)
        self.settings = settings
        if settings is not None:
            self.apply(settings.values())
        self._ready_at: dict = {}         # game id → clock of its last game that got ready
        self.saves = saves
        self._restore()

    # ── Read ────────────────────────────────────────────────────────────────

    def catalog(self) -> list[dict]:
        return [{"id": gid, "name": f.name, "how": f.how_to_play, "start": f.start_help}
                for gid, f in self._factories.items() if not getattr(f, "hidden", False)]

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
            shown = game.id if game is not None else (self._final or {}).get("game")
            top = self.scores.top(shown) if self.scores is not None and shown else []
            return {"activity": activity, "games": self.catalog(),
                    "last_event": self._last_event, "final": self._final,
                    "scores": {"game": shown, "top": top} if shown else None,
                    "version": self.version}

    @property
    def busy(self) -> bool:
        return self._game is not None

    def pending_ask(self) -> dict | None:
        with self._lock:
            return self._game.pending_ask() if self._game is not None else None

    def check(self, text: str) -> bool:
        """True when NAO's text gives the running game's solution away."""
        with self._lock:
            return self._game is not None and self._game.leaks(str(text or ""))

    def note(self, text: str) -> bool:
        """NAO's words the running game asked to keep (Event.keep)."""
        with self._lock:
            if self._game is None or not self._game.note(str(text or "")):
                return False
            self._persist()
            return True

    def apply(self, values: dict):
        """Control-room settings: who may start and the delay apply at once,
        the games' own settings at the next game (Game.configure)."""
        with self._lock:
            if "start_roles" in values:
                self.start_roles = list(values["start_roles"]) or [EVERYONE]
            if "move_cooldown_s" in values:
                self.move_cooldown_s = float(values["move_cooldown_s"])
            if "presence_min" in values:
                self.presence_s = float(values["presence_min"]) * 60

    def seen(self, logins):
        """Viewers who just wrote in the chat (twitch-input): they are still there."""
        now = self._clock()
        with self._lock:
            for login in logins:
                if isinstance(login, str) and login.strip():
                    self._seen[login.strip().lower()] = now

    def present(self) -> set:
        """Logins seen in the chat or playing within presence_s."""
        now = self._clock()
        with self._lock:
            for login in [k for k, t in self._seen.items() if now - t > self.presence_s]:
                del self._seen[login]
            return set(self._seen)

    def touch(self):
        """Something outside the game changed (ranking reset): the overlay refreshes."""
        with self._lock:
            self.version += 1
            if self.on_change is not None:
                try:
                    self.on_change()
                except Exception:
                    pass

    # ── Write ───────────────────────────────────────────────────────────────

    def start(self, game_id: str, player: Player, message: str = "",
              options: dict | None = None) -> Event:
        with self._lock:
            if self._game is not None:
                if self._game.persistent and self._game.id == str(game_id) and self._game.is_ready():
                    # "Shall we resume the investigation?": the saved game, retold.
                    return self._done(Event(
                        "resume", f"La partie de {self._game.name} reprend. Résume où elle en "
                                  f"est : {self._game.recap()}", self._game.recap()),
                        player, count=False)
                if getattr(self._factories.get(str(game_id)), "hidden", False):
                    return self._game.busy_event()    # asked by the control room: not shown
                return self._done(self._game.busy_event(), player)
            factory = self._factories.get(str(game_id))
            if factory is None or (getattr(factory, "hidden", False) and player.source != "regie"):
                names = ", ".join(g["name"] for g in self.catalog()) or "aucun"
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
            if player.login and not player.is_nao:
                self._seen[player.login.lower()] = self._clock()
            game = factory()
            game.present = self.present
            if self.settings is not None:
                game.configure(self.settings.values())
            last = self._ready_at.get(game.id)
            if last is not None and self._clock() - last < game.start_cooldown_s:
                wait = int(game.start_cooldown_s - (self._clock() - last)) + 1
                return self._done(Event(
                    "start_cooldown", f"Une nouvelle partie de {game.name} sera possible "
                                      f"dans {wait} s.",
                    f"Pas tout de suite ! Encore {wait} secondes.", ok=False), player)
            event = game.start(player, options or {})
            if event.ok:
                self._game = game
                self._started_by = player.label()
                self._last_move.clear()
                self._final = None
                if game.is_ready():
                    self._ready_at[game.id] = self._clock()
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
                                                 f"{game.recap()}", game.recap(),
                                        read=game.clue()), player, count=False)
            now = self._clock()
            if player.login and not player.is_nao:
                self._seen[player.login.lower()] = now
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
            was_ready = game.is_ready()
            event = game.on_answer(ask_id, str(text or ""))
            if not was_ready and game.is_ready():
                self._ready_at[game.id] = self._clock()
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

    def update_view(self, game_id: str, view: dict) -> bool:
        """New public state of a hidden activity (the story's chapter); not a move:
        the last event stays.  False when it is not the running activity."""
        with self._lock:
            game = self._game
            if game is None or not game.hidden or game.id != game_id:
                return False
            game.set_view(view)
            self.touch()
            return True

    def stop(self, player: Player, game_id: str = "") -> Event:
        """game_id: only stops that activity (the story gives back its own)."""
        with self._lock:
            if game_id and (self._game is None or self._game.id != game_id):
                # Nothing of that activity to give back: the overlay is not touched.
                return Event("no_game", "Cette activité n'est pas en cours.", ok=False)
            if self._game is None:
                return self._done(Event("no_game", "Aucune partie n'est en cours.",
                                        "Il n'y a pas de partie en cours.", ok=False), player)
            event = self._game.cancelled_event(player)
            self._forget(self._game)
            self._game = None
            return self._done(event, player)

    # ── Internals ───────────────────────────────────────────────────────────

    def _finish_if_over(self, event: Event, player: Player) -> Event:
        game = self._game
        if self.scores is not None and event.points and game is not None:
            names = {player.login: player.label()}
            for login, pts in event.points.items():
                self.scores.add(game.id, login, names.get(login, login), int(pts))
        if game is not None and game.is_over():
            if not game.is_aborted():
                self._final = {"game": game.id, "name": game.name, "view": game.public_view()}
            self._forget(game)
            self._game = None
        return self._done(event, player)

    def _done(self, event: Event, player: Player, count: bool = True) -> Event:
        self._last_event = {**event.to_dict(), "by": player.label()}
        if count and event.ok and player.login:
            self._last_player = player.label()
        self.version += 1
        self._persist()
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass
        return event

    def _persist(self):
        game = self._game
        if self.saves is None or game is None or not game.persistent:
            return
        state = game.save()
        if state is not None:
            self.saves.put(game.id, self._started_by, state)

    def _forget(self, game: Game):
        """A persistent game is over or cancelled: its save goes."""
        if self.saves is not None and game.persistent:
            self.saves.delete(game.id)

    def _restore(self):
        """The saved game becomes the running activity again.  A save that cannot be
        restored is left in the base (a later version may read it)."""
        if self.saves is None:
            return
        for game_id, started_by, state in self.saves.all():
            factory = self._factories.get(game_id)
            if factory is None:
                continue
            game = factory()
            game.present = self.present
            try:
                game.restore(state)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                print(f"[game-master] saved game {game_id} not restored: {e}", flush=True)
                continue
            self._game, self._started_by = game, started_by
            return


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

    def reset(self, game: str | None = None):
        """Ranking back to zero: one game, or every game."""
        with self._lock:
            if game:
                self._db.execute("DELETE FROM scores WHERE game = ?", (game,))
            else:
                self._db.execute("DELETE FROM scores")
            self._db.commit()

    def top(self, game: str, n: int = 5) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name, points FROM scores WHERE game = ? ORDER BY points DESC, name"
                " LIMIT ?", (game, n)).fetchall()
        return [{"name": name, "points": points} for name, points in rows]


class SaveStore:
    """Saved state of persistent games, one per game, kept across restarts (SQLite)."""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS saves"
                             " (game TEXT PRIMARY KEY, started_by TEXT, state TEXT)")
            self._db.commit()

    def put(self, game: str, started_by: str, state: dict):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO saves (game, started_by, state)"
                             " VALUES (?, ?, ?)",
                             (game, started_by, json.dumps(state, ensure_ascii=False)))
            self._db.commit()

    def delete(self, game: str):
        with self._lock:
            self._db.execute("DELETE FROM saves WHERE game = ?", (game,))
            self._db.commit()

    def all(self) -> list[tuple[str, str, dict]]:
        with self._lock:
            rows = self._db.execute("SELECT game, started_by, state FROM saves").fetchall()
        saved = []
        for game, started_by, state in rows:
            try:
                saved.append((game, started_by, json.loads(state)))
            except ValueError:
                continue
        return saved


class Settings:
    """Game settings changed from the control room, kept across restarts (SQLite).

    schema: [{"key", "label", "type": "int"|"float"|"roles", "min", "max", "default", "help"}];
    the environment variables give the defaults, a value set from the control
    room overrides them until it is set again."""

    def __init__(self, schema: list[dict], path: str | None = None):
        self.schema = schema
        self._by_key = {item["key"]: item for item in schema}
        self._values: dict = {}
        self._lock = threading.Lock()
        self._db = None
        if path:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            self._db.commit()
            for key, value in self._db.execute("SELECT key, value FROM settings").fetchall():
                if key in self._by_key:
                    try:
                        self._values[key] = self._check(key, json.loads(value))
                    except ValueError:
                        pass   # an old value out of bounds: back to the default

    def values(self) -> dict:
        with self._lock:
            return {key: self._values.get(key, item["default"]) for key, item in self._by_key.items()}

    def update(self, changes: dict) -> dict:
        """Checks every value first (ValueError, nothing saved), then saves them."""
        checked = {}
        for key, value in (changes or {}).items():
            if key not in self._by_key:
                raise ValueError(f"réglage inconnu : {key}")
            checked[key] = self._check(key, value)
        with self._lock:
            self._values.update(checked)
            if self._db is not None:
                self._db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                                     [(k, json.dumps(v)) for k, v in checked.items()])
                self._db.commit()
        return self.values()

    def _check(self, key: str, value):
        item = self._by_key[key]
        kind = item["type"]
        if kind == "roles":
            roles = [str(r) for r in (value if isinstance(value, list) else [value])]
            if not roles or any(r not in (EVERYONE,) + ROLES[1:] for r in roles):
                raise ValueError(f"{item['label']} : rôles possibles everyone, moderator, vip, subscriber")
            return [EVERYONE] if EVERYONE in roles else roles
        try:
            number = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{item['label']} : nombre attendu") from None
        if kind == "int" and float(value) != number:
            raise ValueError(f"{item['label']} : nombre entier attendu")
        if not item["min"] <= number <= item["max"]:
            raise ValueError(f"{item['label']} : entre {item['min']} et {item['max']}")
        return number
