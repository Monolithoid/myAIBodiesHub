"""
player — plays a pre-recording through body-gateway, with a bookmark.

Passage = one text entry.  The bar of the control room: play, pause, stop, seek
(passage or chapter).  While playing, every command waits for the end of the
passage being said, never cutting a sentence; seeks add up.  Paused or stopped,
a seek moves the bookmark at once.

While it reads, the story is game-master's running activity (id "story"): no
game can start, and the OBS scene follows.  The activity is given back when the
reading stops, whatever the reason, errors included.

  texts follow each other on GET /speaking, then "pause_after_s"
  "at_ms": the text waits for that instant on the story clock (paused with the reading)
"""

import json
import pathlib
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from storyteller import prerecording

STOPPED, PLAYING, PAUSED = "stopped", "playing", "paused"
ACTIVITY = "story"
_STORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")


class PlayerError(Exception):
    """A command refused or a service that failed; the message is shown on the page."""


# ─────────────────────────────────────────────────────────────────────────────
# Stories and bookmark
# ─────────────────────────────────────────────────────────────────────────────

class Story:
    def __init__(self, story_id: str, header: dict, entries: list):
        self.id = story_id
        self.header = header
        # Sounds are not played yet.
        self.passages = [e for e in entries if "sound" not in e]
        if not self.passages:
            raise ValueError("no text to read")

    def chapter(self, index: int) -> int:
        return self.passages[index]["chapter"]

    def chapter_start(self, index: int) -> int:
        while index > 0 and self.chapter(index - 1) == self.chapter(index):
            index -= 1
        return index

    def seek(self, index: int, unit: str, delta: int) -> int:
        """New index, one passage or chapter before (delta < 0) or after, kept in the story.

        A chapter backwards first goes to the start of this chapter, then to the
        previous one.  There is nothing after the last chapter: no move.
        """
        if unit == "passage":
            return max(0, min(len(self.passages) - 1, index + delta))
        for _ in range(abs(delta)):
            start = self.chapter_start(index)
            if delta < 0:
                index = start if index != start else self.chapter_start(max(0, start - 1))
            else:
                index = next((i for i in range(index + 1, len(self.passages))
                              if self.chapter(i) != self.chapter(index)), index)
        return index


class Library:
    def __init__(self, root):
        self.root = pathlib.Path(root)

    def load(self, story_id: str) -> Story:
        story_id = str(story_id or "")
        if not _STORY_ID.fullmatch(story_id) or not (self.root / story_id).is_dir():
            raise PlayerError(f"histoire inconnue : {story_id!r}")
        try:
            return Story(story_id, *prerecording.load_story(self.root / story_id))
        except ValueError as e:
            raise PlayerError(f"{story_id} illisible : {e}") from None

    def list(self) -> list[dict]:
        found = []
        if not self.root.is_dir():
            return found
        for path in sorted(self.root.iterdir()):
            if not _STORY_ID.fullmatch(path.name) or not prerecording.script_files(path):
                continue
            try:
                story = self.load(path.name)
            except PlayerError as e:
                found.append({"id": path.name, "error": str(e)})
                continue
            found.append({"id": path.name, "title": story.header["title"],
                          "author": story.header["author"], "passages": len(story.passages)})
        return found


class Bookmarks:
    """Where each story stands (SQLite): the next passage to read, by segment number."""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS bookmarks (story TEXT PRIMARY KEY,"
                             " chapter INTEGER, seg INTEGER, updated REAL)")
            self._db.commit()

    def put(self, story: Story, index: int):
        passage = story.passages[index]
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO bookmarks VALUES (?, ?, ?, ?)",
                             (story.id, passage["chapter"], passage["seg"], time.time()))
            self._db.commit()

    def index(self, story: Story) -> int:
        with self._lock:
            row = self._db.execute("SELECT chapter, seg FROM bookmarks WHERE story = ?",
                                   (story.id,)).fetchone()
        if row is None:
            return 0
        # The script may have changed: the first passage at or after the bookmark.
        return next((i for i, p in enumerate(story.passages)
                     if (p["chapter"], p["seg"]) >= tuple(row)), 0)


class Clock:
    """Story time in milliseconds, for "at_ms"; stands still while paused."""

    def __init__(self, now=time.monotonic):
        self._now = now
        self._offset = 0.0
        self._since: float | None = None

    def start(self, at_ms: float = 0.0):
        self._offset, self._since = float(at_ms), self._now()

    def pause(self):
        if self._since is not None:
            self._offset, self._since = self.ms(), None

    def resume(self):
        if self._since is None:
            self._since = self._now()

    def ms(self) -> float:
        running = 0.0 if self._since is None else (self._now() - self._since) * 1000
        return self._offset + running


# ─────────────────────────────────────────────────────────────────────────────
# The services around
# ─────────────────────────────────────────────────────────────────────────────

def _call(url: str, body: dict | None = None, headers: dict | None = None,
          timeout: float = 30.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


class BodyGateway:
    """POST /perform and GET /speaking of body-gateway."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def perform(self, order: dict) -> bool:
        """True once sent to the bodies, False while body-gateway is busy with someone else."""
        try:
            # The single-threaded server answers after its running inference.
            status, data = _call(f"{self.url}/perform", order, timeout=150)
        except TimeoutError:
            # Sent and queued there: sending it again would say it twice.
            return True
        if status == 503:
            return False
        if status != 200:
            raise PlayerError(f"body-gateway /perform : {status} {data.get('error', '')}")
        return True

    def speaking(self) -> bool:
        # The single-threaded server answers after its running inference.
        status, data = _call(f"{self.url}/speaking", timeout=130)
        if status != 200:
            raise PlayerError(f"body-gateway /speaking : {status}")
        return bool(data.get("speaking"))


class GameMasterActivity:
    """The "story" activity of game-master.  Without a token (games disabled) there is
    nothing to lock."""

    PLAYER = {"name": "la régie", "source": "regie"}

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _post(self, path: str, body: dict) -> dict:
        try:
            status, data = _call(self.url + path, {**body, "player": self.PLAYER},
                                 {"Authorization": f"Bearer {self.token}"}, timeout=10)
        except OSError as e:
            raise PlayerError(f"game-master injoignable : {e}") from None
        if status != 200:
            raise PlayerError(f"game-master {path} : {status} {data.get('error', '')}")
        return data

    def take(self, view: dict):
        if not self.token:
            return
        answer = self._post("/start", {"game": ACTIVITY, "options": view})
        event = answer.get("event") or {}
        running = ((answer.get("state") or {}).get("activity") or {}).get("game")
        # Already a story: ours, left behind by a restart.
        if not event.get("ok") and running != ACTIVITY:
            raise PlayerError(event.get("text") or "activité refusée par game-master")
        if not event.get("ok"):
            self.update(view)

    def update(self, view: dict):
        if self.token:
            self._post("/activity/view", {"game": ACTIVITY, "view": view})

    def release(self):
        if self.token:
            self._post("/stop", {"game": ACTIVITY})

    def held(self) -> bool | None:
        """False when the story is not the running activity any more; None when unknown."""
        if not self.token:
            return None
        try:
            status, data = _call(f"{self.url}/state",
                                 headers={"Authorization": f"Bearer {self.token}"}, timeout=5)
        except OSError:
            return None
        if status != 200:
            return None
        return (data.get("activity") or {}).get("game") == ACTIVITY


# ─────────────────────────────────────────────────────────────────────────────
# The player
# ─────────────────────────────────────────────────────────────────────────────

class Player:
    """One reading at a time.  Commands come from the HTTP threads, a worker thread reads."""

    SPEAKING_POLL_S = 0.4
    BUSY_RETRY_S = 1.0
    BUSY_TRIES = 120
    NETWORK_TRIES = 3

    def __init__(self, library: Library, bookmarks: Bookmarks, bodies, activity,
                 log=lambda text: None, clock: Clock | None = None):
        self.library = library
        self.bookmarks = bookmarks
        self.bodies = bodies
        self.activity = activity
        self.log = log
        self.clock = clock or Clock()
        self._cond = threading.Condition()
        self._status = STOPPED
        self._story: Story | None = None
        self._index = 0                 # passage being said, or next one when idle
        self._saying = False            # a passage is being said: commands wait
        self._pending: str | None = None     # PAUSED or STOPPED asked while saying
        self._target: int | None = None      # seek asked while saying
        self._chapter: int | None = None     # last chapter heard in this reading
        self._error = ""
        self._lag_ms = 0
        self._worker: threading.Thread | None = None

    # ── Commands ────────────────────────────────────────────────────────────

    def play(self, story_id: str = "") -> dict:
        with self._cond:
            if self._status == PLAYING:
                self._check_same(story_id)
                self._pending = None            # a pause or stop asked is withdrawn
            elif self._status == PAUSED:
                self._check_same(story_id)
                self._status, self._error = PLAYING, ""
                self.clock.resume()
                self._cond.notify_all()
            else:
                if self._worker is not None and self._worker.is_alive():
                    raise PlayerError("la lecture précédente se termine, réessaie")
                story = self.library.load(story_id or (self._story.id if self._story else ""))
                index = self.bookmarks.index(story)
                try:
                    self.activity.take(self._view(story, index))
                except PlayerError:
                    # game-master may have accepted before its answer was lost.
                    self._release()
                    raise
                self._story, self._index = story, index
                self._pending = self._target = None
                self._error, self._lag_ms = "", 0
                self._saying = True             # the resume sentence: commands wait
                self._chapter = story.chapter(index)
                self._status = PLAYING
                self._worker = threading.Thread(target=self._run, daemon=True)
                try:
                    self._worker.start()
                except RuntimeError as e:
                    self._status, self._saying = STOPPED, False
                    self._release()
                    raise PlayerError(f"lecture impossible à lancer : {e}") from None
            return self._state()

    def pause(self) -> dict:
        with self._cond:
            if self._status == PLAYING and self._saying:
                if self._pending != STOPPED:
                    self._pending = PAUSED
            elif self._status == PLAYING:
                self._status = PAUSED
                self.clock.pause()
                self._cond.notify_all()
            return self._state()

    def stop(self) -> dict:
        """Stops after the passage being said.  Already stopped: gives back a "story"
        activity left behind."""
        with self._cond:
            if self._status == STOPPED:
                self.activity.release()
            elif self._saying:
                self._pending = STOPPED
            else:
                self._status = STOPPED
                self._cond.notify_all()
            return self._state()

    def seek(self, unit: str, delta: int, story_id: str = "") -> dict:
        if unit not in ("passage", "chapter") or type(delta) is not int or delta not in (-1, 1):
            raise PlayerError("seek : unit passage|chapter, delta -1|1")
        with self._cond:
            if self._status == STOPPED:
                if story_id and (self._story is None or story_id != self._story.id):
                    story = self.library.load(story_id)
                elif self._story is not None:
                    story = self.library.load(self._story.id)
                else:
                    raise PlayerError("choisis une histoire")
                self._story = story
                self._index = story.seek(self.bookmarks.index(story), unit, delta)
                self.bookmarks.put(story, self._index)
            else:
                self._check_same(story_id)
                if self._saying:
                    current = self._index if self._target is None else self._target
                    asked = self._story.seek(current, unit, delta)
                    if asked != current:      # nothing further: the passage is not said again
                        self._target = asked
                else:
                    self._index = self._story.seek(self._index, unit, delta)
                    self.bookmarks.put(self._story, self._index)
                    self._retime(self._index)
                    self._cond.notify_all()
            return self._state()

    def state(self, story_id: str = "") -> dict:
        with self._cond:
            if self._status == STOPPED and story_id:
                try:
                    self._story = self.library.load(story_id)
                    self._index = self.bookmarks.index(self._story)
                except PlayerError as e:
                    return {**self._state(), "error": str(e)}
            return self._state()

    # ── Internals (called with the lock held) ───────────────────────────────

    def _check_same(self, story_id: str):
        if story_id and story_id != self._story.id:
            raise PlayerError("arrête d'abord l'histoire en cours")

    def _state(self) -> dict:
        state = {"status": self._status, "story": None, "pending": None, "error": self._error,
                 "lag_ms": self._lag_ms}
        story = self._story
        if story is None:
            return state
        passage = story.passages[self._index]
        in_chapter = [p for p in story.passages if p["chapter"] == passage["chapter"]]
        state.update(
            story=story.id, title=story.header["title"], author=story.header["author"],
            chapter=passage["chapter"], chapters=story.header["chapters"],
            passage=self._index - story.chapter_start(self._index) + 1, passages=len(in_chapter),
            seg=passage["seg"], text=passage["text"])
        if self._pending or self._target is not None:
            target = None
            if self._target is not None:
                target = {"chapter": story.chapter(self._target),
                          "passage": self._target - story.chapter_start(self._target) + 1}
            state["pending"] = {"command": self._pending, "target": target}
        return state

    def _view(self, story: Story, index: int) -> dict:
        header = story.header
        return {"title": header["title"], "author": header["author"], "year": header.get("year"),
                "chapter": story.chapter(index), "chapters": header["chapters"]}

    def _retime(self, index: int):
        """At the start and after a seek, the story clock moves to the passage's instant
        (a passage without one: the last instant before it, which it follows)."""
        at_ms = next((p["at_ms"] for p in reversed(self._story.passages[:index + 1])
                      if "at_ms" in p), 0)
        self.clock.start(at_ms)
        if self._status == PAUSED:
            self.clock.pause()

    # ── Worker thread ───────────────────────────────────────────────────────

    def _run(self):
        try:
            self._read()
        except PlayerError as e:
            self._fail(str(e))
        except Exception as e:   # whatever happens, the activity is given back
            self._fail(f"{type(e).__name__} : {e}")
        finally:
            with self._cond:
                self._status, self._saying = STOPPED, False
                self._pending = self._target = None
                self.clock.pause()
            self._release()

    def _fail(self, message: str):
        self.log(f"story stopped on an error: {message}")
        with self._cond:
            self._error = message

    def _release(self):
        """Gives the activity back; the condition's lock is reentrant, callers may hold it."""
        try:
            if self.activity.held() is not False:
                self.activity.release()
        except Exception as e:
            self.log(f"story activity not released: {e}")
            with self._cond:
                self._error = self._error or f"activité non rendue (⏹ pour réessayer) : {e}"

    def _read(self):
        with self._cond:
            story, index = self._story, self._index
        header = story.header
        self._say({"text": f"Je vous lis {header['title']}, de {header['author']}, "
                           f"chapitre {story.chapter(index)}.", "emotion": "calm"})
        with self._cond:
            self._retime(self._index)
            self.clock.resume()
        if not self._settle(self._index):
            return
        while True:
            passage = self._next_passage()
            if passage is None:
                return
            if self.activity.held() is False:
                self.log("story activity taken away (!stop or game-master restarted)")
                with self._cond:
                    self._error = "activité « histoire » retirée (!stop ou game-master redémarré)"
                return
            if passage["chapter"] != self._chapter:
                self._chapter = passage["chapter"]
                self._progress()
                self._say({"text": f"Chapitre {passage['chapter']}.", "emotion": "calm"})
            self._say(passage)
            following = self._index + 1
            moved = self._target is not None
            if not self._settle(following):
                return
            if not moved:
                self._pause_after(passage, following)

    def _next_passage(self) -> dict | None:
        """Waits while paused and until the passage's instant; marks it as being said."""
        with self._cond:
            while True:
                if self._status == STOPPED:
                    return None
                if self._status == PAUSED:
                    self._cond.wait()
                    continue
                passage = self._story.passages[self._index]
                at_ms = passage.get("at_ms")
                wait = 0.0 if at_ms is None else (at_ms - self.clock.ms()) / 1000
                if wait > 0:
                    self._cond.wait(min(wait, 1.0))   # a command wakes the wait up
                    continue
                self._lag_ms = 0 if at_ms is None else int(-wait * 1000)
                self._saying = True
                return passage

    def _settle(self, following: int) -> bool:
        """The sentence has been heard: bookmark on the next passage (or the one asked
        meanwhile), then the pause or stop asked.  False when the reading is over."""
        with self._cond:
            story = self._story
            target, self._target = self._target, None
            command, self._pending = self._pending, None
            self._saying = False
            if target is not None:
                following = target
            if following >= len(story.passages):
                self.bookmarks.put(story, 0)      # the end: next time from the start
                self._index = 0
                self.log(f"{story.id}: end of the story")
                return False
            self._index = following
            self.bookmarks.put(story, following)
            if command == STOPPED:
                self._status = STOPPED
                return False
            if command == PAUSED:
                self._status = PAUSED
                self.clock.pause()
            if target is not None:
                self._retime(following)
            return True

    def _pause_after(self, passage: dict, following: int):
        """The silence after a passage; a command cuts it short."""
        pause = passage.get("pause_after_s", prerecording.DEFAULT_PAUSE_S)
        if pause > 0:
            with self._cond:
                self._cond.wait_for(lambda: self._status != PLAYING or self._index != following,
                                    timeout=pause)

    def _say(self, passage: dict):
        """Sends one text to the bodies and waits until it has been spoken."""
        actions = [{"type": "speech", "text": passage["text"]}]
        if passage.get("gesture"):
            actions.append({"type": "gesture", **passage["gesture"]})
        order = {"source": ACTIVITY, "emotion": passage["emotion"],
                 "energy": passage.get("energy", "medium"), "actions": actions}
        for _ in range(self.BUSY_TRIES):
            if self._retry(lambda: self.bodies.perform(order)):
                break
            time.sleep(self.BUSY_RETRY_S)
        else:
            raise PlayerError("body-gateway reste occupé")
        while self._retry(self.bodies.speaking):
            time.sleep(self.SPEAKING_POLL_S)

    def _retry(self, call):
        for attempt in range(1, self.NETWORK_TRIES + 1):
            try:
                return call()
            except OSError as e:
                self.log(f"body-gateway call failed ({attempt}): {e}")
                if attempt == self.NETWORK_TRIES:
                    raise PlayerError(f"body-gateway injoignable : {e}") from None
                time.sleep(self.BUSY_RETRY_S)

    def _progress(self):
        """The overlay follows the chapter; a failure does not stop the reading."""
        with self._cond:
            view = self._view(self._story, self._index)
        try:
            self.activity.update(view)
        except PlayerError as e:
            self.log(str(e))
