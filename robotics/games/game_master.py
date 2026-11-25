#!/usr/bin/env python3
"""
game_master — games with the stream's viewers (ADR 010).  The code decides, the LLM speaks.

Everything goes through "!nao" in natural language: body-gateway's LLM turns a
chat message into a start_game or game_move action, body-gateway forwards it
here, this service settles it and returns the event and the public state;
body-gateway then asks the LLM for NAO's reaction.  The LLM never sees the
solution.  One activity at a time.

    body-gateway ── POST /start, /move ──► game-master ── event + public state ──► body-gateway
    regie, twitch-input "!stop" ── POST /stop ──► game-master ── POST /game-event ──► body-gateway
    OBS browser source ◄── /overlay?token=GAME_TOKEN (Server-Sent Events)

HTTP API (token: "Authorization: Bearer $GAME_TOKEN" or ?token=):
  GET  /ping                       no token
  GET  /state                      {"activity": null | {...}, "games": [...], "last_event"}
  POST /start {"game", "player", "message", "options"}  → {"event", "state"}
  POST /move  {"move", "player"}                         → {"event", "state"}
  POST /stop  {"player"}                                 → {"event", "state"}, announced by NAO
  POST /answer {"id", "text", "player"}                  → {"event", "state"}: text of a
                                   hidden LLM call ("" = failed)
  An answer may also hold "ask": {"id", "purpose", "system", "user"}, a hidden LLM call
  body-gateway runs outside NAO's prompt before NAO reacts, then posts to /answer.
  POST /reveal {"version"}                               → {"revealed"}: NAO has said
                                   the result, the overlay may show the state of that version
  GET  /overlay?token=…            OBS page; /overlay/stream?token=… its SSE feed (each
                                   new state held until /reveal, OVERLAY_HOLD_S at most)

"player" is {"login", "name", "roles": [...], "source"}: roles are the chat badges
forwarded by twitch-input (broadcaster, moderator, vip, subscriber); a source
other than "twitch" (Open WebUI, control room) is the streamer.

Environment variables:
  GAME_TOKEN          secret of the API and of the overlay (required)
  OVERLAY_HOLD_S      longest a new state waits for /reveal before the overlay
                      shows it anyway, default 30 (0 = show at once)
  GAME_PORT           default 8098
  BODY_GATEWAY_URL    where events not asked by body-gateway are announced
                      (POST /game-event), default http://chat-relay:8095
  GAME_START_ROLES    who may start a game: "everyone" (default), or a comma list
                      among moderator, vip, subscriber (the streamer always may)
  MOVE_COOLDOWN_S     per-viewer delay between two moves, default 0 (none)
  GAMES_DB_PATH       SQLite file of the scores, default /data/games.db; empty = no scores
  HANGMAN_MAX_ERRORS  errors before the hangman game is lost, default 7
  HANGMAN_MIN_LETTERS shortest hangman word, default 6 (the list holds 6 to 12 letters)
"""

import hmac
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import game_core
import hangman

GAME_TOKEN       = os.environ.get("GAME_TOKEN", "").strip()
GAME_PORT        = int(os.environ.get("GAME_PORT", "8098"))
BODY_GATEWAY_URL = os.environ.get("BODY_GATEWAY_URL", "http://chat-relay:8095").rstrip("/")
GAME_START_ROLES = [r.strip().lower() for r in
                    os.environ.get("GAME_START_ROLES", game_core.EVERYONE).split(",") if r.strip()]
MOVE_COOLDOWN_S  = float(os.environ.get("MOVE_COOLDOWN_S", "0"))
GAMES_DB_PATH    = os.environ.get("GAMES_DB_PATH", "/data/games.db").strip()
OVERLAY_HOLD_S   = float(os.environ.get("OVERLAY_HOLD_S", "30"))

HERE = os.path.dirname(os.path.abspath(__file__))

# Games offered to the viewers: id → Game class.
GAMES: dict = {hangman.Hangman.id: hangman.Hangman}


def log(text: str):
    print(f"[game-master] {text}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Overlay feed: every change is pushed to the open OBS pages
# ─────────────────────────────────────────────────────────────────────────────

class Feed:
    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=20)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, data: dict):
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass   # a stalled page misses an update, the next one carries the full state


class Stage:
    """What the overlay shows.  A new state is held until NAO has said it
    (POST /reveal from body-gateway, with the state's version), so the overlay
    never gives a result away before NAO; OVERLAY_HOLD_S at most (0 = no hold)."""

    def __init__(self, feed: Feed, initial: dict, hold_s: float = OVERLAY_HOLD_S,
                 clock=time.monotonic):
        self._feed = feed
        self._hold_s = hold_s
        self._clock = clock
        self._held: list[tuple[int, dict, float]] = []   # (version, state, since), oldest first
        self._lock = threading.Lock()
        self.shown = initial

    def changed(self, state: dict):
        if self._hold_s <= 0:
            self._show(state)
            return
        with self._lock:
            self._held.append((int(state.get("version", 0)), state, self._clock()))

    def reveal(self, version: int) -> bool:
        """Shows the newest held state up to this version; False if none."""
        with self._lock:
            ready = [h for h in self._held if h[0] <= version]
            if not ready:
                return False
            self._held = [h for h in self._held if h[0] > version]
        self._show(ready[-1][1])
        return True

    def expire(self):
        """States held longer than OVERLAY_HOLD_S are shown anyway (NAO never said them)."""
        with self._lock:
            limit = self._clock() - self._hold_s
            late = [h for h in self._held if h[2] <= limit]
        if late:
            self.reveal(late[-1][0])

    def subscribe(self) -> queue.Queue:
        return self._feed.subscribe()

    def unsubscribe(self, q: queue.Queue):
        self._feed.unsubscribe(q)

    def _show(self, state: dict):
        self.shown = state
        self._feed.publish(state)


# ─────────────────────────────────────────────────────────────────────────────
# Announcer: events nobody asked body-gateway for (stop, timer) → NAO says them
# ─────────────────────────────────────────────────────────────────────────────

class Announcer:
    """Sends events to body-gateway POST /game-event, one at a time and in order."""

    def __init__(self, url: str, post=requests.post):
        self._url = url
        self._post = post
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def announce(self, event: game_core.Event, state: dict):
        self._queue.put({"event": event.to_dict(), "state": state})

    def _run(self):
        while True:
            item = self._queue.get()
            try:
                # body-gateway waits for its inference lock: the timeout covers it and the LLM.
                self._post(f"{self._url}/game-event", json=item, timeout=120).raise_for_status()
            except requests.RequestException as e:
                log(f"announce failed ({item['event']['kind']}): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

def make_handler(master: game_core.GameMaster, stage: Stage, announcer: Announcer | None,
                 token: str):

    class Handler(BaseHTTPRequestHandler):

        def _authorized(self, query: dict) -> bool:
            given = query.get("token", [""])[0]
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                given = auth[len("Bearer "):].strip()
            return bool(token) and hmac.compare_digest(given.encode(), token.encode())

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/ping":
                self._json(200, {"ok": True, "service": "game-master"})
                return
            if not self._authorized(query):
                self._json(403, {"error": "forbidden"})
                return
            if url.path == "/state":
                self._json(200, master.state())
            elif url.path == "/overlay":
                with open(os.path.join(HERE, "overlay.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif url.path == "/overlay/stream":
                self._stream()
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if not self._authorized(urllib.parse.parse_qs(url.query)):
                self._json(403, {"error": "forbidden"})
                return
            body = self._read_json()
            if body is None:
                return
            if url.path == "/reveal":
                try:
                    version = int(body.get("version"))
                except (TypeError, ValueError):
                    self._json(400, {"error": "version required"})
                    return
                self._json(200, {"revealed": stage.reveal(version)})
                return
            player = game_core.Player.from_dict(body.get("player"))
            if url.path == "/start":
                event = master.start(str(body.get("game", "")), player,
                                     str(body.get("message", "")), body.get("options") or {})
            elif url.path == "/move":
                event = master.move(body.get("move") or {}, player)
            elif url.path == "/answer":
                event = master.answer(str(body.get("id", "")), str(body.get("text", "")), player)
            elif url.path == "/stop":
                event = master.stop(player)
                if event.ok and announcer is not None:
                    announcer.announce(event, master.state())
            else:
                self._json(404, {"error": "not found"})
                return
            log(f"{url.path[1:]} by {player.label()}: {event.kind} — {event.text[:80]}")
            answer = {"event": event.to_dict(), "state": master.state()}
            ask = master.pending_ask()
            if ask is not None and url.path != "/stop":
                answer["ask"] = ask
            self._json(200, answer)

        def _stream(self):
            q = stage.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                data = stage.shown
                while True:
                    self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    try:
                        data = q.get(timeout=15)
                    except queue.Empty:
                        data = stage.shown   # keep-alive, also heals a missed update
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                stage.unsubscribe(q)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                return data
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "invalid JSON"})
                return None

        def _json(self, code: int, data: dict):
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def _send(self, code: int, payload: bytes, content_type: str):
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass

    return Handler


def tick_loop(master: game_core.GameMaster, announcer: Announcer, stage: Stage):
    while True:
        time.sleep(1.0)
        stage.expire()
        try:
            event = master.tick()
        except Exception as e:   # a broken game must not stop the service
            log(f"tick error: {e}")
            continue
        if event is not None:
            announcer.announce(event, master.state())


def main():
    # PID 1 in the container: without a handler, `docker stop` waits for SIGKILL.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not GAME_TOKEN:
        log("ERROR: GAME_TOKEN is required")
        sys.exit(1)
    scores = None
    if GAMES_DB_PATH:
        os.makedirs(os.path.dirname(GAMES_DB_PATH) or ".", exist_ok=True)
        scores = game_core.ScoreStore(GAMES_DB_PATH)
    master = game_core.GameMaster(GAMES, GAME_START_ROLES, MOVE_COOLDOWN_S, scores)
    stage = Stage(Feed(), master.state())
    master.on_change = lambda: stage.changed(master.state())
    announcer = Announcer(BODY_GATEWAY_URL)
    threading.Thread(target=tick_loop, args=(master, announcer, stage), daemon=True).start()
    log(f":{GAME_PORT} | games: {', '.join(GAMES) or 'none'} | start: {', '.join(GAME_START_ROLES)}"
        f" | move delay: {MOVE_COOLDOWN_S:g}s | events → {BODY_GATEWAY_URL}/game-event")
    ThreadingHTTPServer(("0.0.0.0", GAME_PORT),
                        make_handler(master, stage, announcer, GAME_TOKEN)).serve_forever()


if __name__ == "__main__":
    main()
