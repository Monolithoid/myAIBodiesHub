#!/usr/bin/env python3
"""
obs_agent — NAO's voice on the stream: a voice body like the static drone's
speaker, but its "speaker" is the OBS mixer (ADR 005).

It connects outbound to rosbridge with ROSBRIDGE_TOKEN, exactly like the
static drone (ADR 004), and uses the same topics, so static_driver (started a
second time with DRONE_ID=obs_01 in nao-core) does the edge-tts synthesis:

  /bodies/{id}/actuators/say  (String, text)        → caption
  /drones/{id}/audio_out      (String, MP3 base64)  → played by the overlay
  /drones/{id}/status         (String JSON, 30 s)   ← only while an overlay is open,
                                                      so speech is sent here only
                                                      when the stream can hear it
  /drones/{id}/speaking       (Bool)                ← overlay playback start/end,
                                                      the NAO animates meanwhile
  /speech_gesture_sync        (String JSON)         → {"kind": "go", "sync", "in_s"}:
                                                      a sentence that goes with a NAO
                                                      gesture waits for it, like the
                                                      static drone's speaker

Speech ↔ gesture sync: when body_gateway links a sentence to a learned gesture,
"say" is JSON {"text", "sync"} and audio_out is JSON {"mp3", "sync", "speech_s"}.
The sentence (voice and caption) is held until the drone's gesture service says
the gesture starts (+ its GESTURE_SPEECH_OFFSET_MS, + OBS_SPEECH_OFFSET_MS here),
at most SPEECH_SYNC_MAX_WAIT_S.  Sentences keep their order.

The overlay is an OBS browser source: /overlay?token=OBS_TOKEN.  With "Control
audio via OBS" checked, the voice is its own source in the OBS mixer.

Environment variables:
  DRONE_ID          body identifier, default "obs_01"
  ROSBRIDGE_URL     default ws://rosbridge-proxy:80
  ROSBRIDGE_TOKEN   bearer token checked by rosbridge-proxy (required)
  OBS_TOKEN         secret in the overlay URL (required)
  HTTP_PORT         overlay port, default 8096
  OBS_SPEECH_OFFSET_MS    extra offset of the stream voice over the room speaker,
                          ms, default 0 (e.g. to match the camera latency)
  SPEECH_SYNC_MAX_WAIT_S  longest wait for the gesture, default 4
"""

import base64
import collections
import hmac
import itertools
import json
import os
import pathlib
import queue
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DRONE_ID        = os.environ.get("DRONE_ID", "obs_01")
ROSBRIDGE_URL   = os.environ.get("ROSBRIDGE_URL", "ws://rosbridge-proxy:80")
ROSBRIDGE_TOKEN = os.environ.get("ROSBRIDGE_TOKEN", "")
OBS_TOKEN       = os.environ.get("OBS_TOKEN", "")
HTTP_PORT       = int(os.environ.get("HTTP_PORT", "8096"))
OBS_SPEECH_OFFSET_S = float(os.environ.get("OBS_SPEECH_OFFSET_MS", "0")) / 1000.0
SPEECH_SYNC_MAX_WAIT_S = float(os.environ.get("SPEECH_SYNC_MAX_WAIT_S", "4"))
SYNC_TOPIC = "/speech_gesture_sync"

STATUS_PERIOD_S = 30.0
CAPTION_MAX_AGE_S = 120.0
OVERLAY_HTML = pathlib.Path(__file__).with_name("overlay.html")


def log(text: str):
    print(f"[obs-agent] {text}", flush=True)


def topic(name: str) -> str:
    return f"/drones/{DRONE_ID}/{name}"


# ─────────────────────────────────────────────────────────────────────────────
# rosbridge client (outbound WebSocket, automatic reconnection)
# ─────────────────────────────────────────────────────────────────────────────

class Rosbridge:
    """Minimal rosbridge JSON client; same protocol as the static drone's."""

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._advertised: dict[str, str] = {}
        self._subscribed: dict[str, tuple] = {}
        self._ws = None
        self._connected = threading.Event()
        self._send_lock = threading.Lock()

    def advertise(self, name: str, msg_type: str):
        self._advertised[name] = msg_type

    def subscribe(self, name: str, msg_type: str, callback):
        self._subscribed[name] = (msg_type, callback)

    def publish(self, name: str, msg: dict) -> bool:
        ws = self._ws
        if ws is None or not self._connected.is_set():
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps({"op": "publish", "topic": name, "msg": msg}))
            return True
        except Exception as e:
            log(f"envoi impossible ({name}) : {e}")
            return False

    def _on_open(self, ws):
        with self._send_lock:
            for name, msg_type in self._advertised.items():
                ws.send(json.dumps({"op": "advertise", "topic": name, "type": msg_type}))
            for name, (msg_type, _) in self._subscribed.items():
                ws.send(json.dumps({"op": "subscribe", "topic": name, "type": msg_type}))
        self._connected.set()
        log(f"connecté à {self.url}")

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        entry = self._subscribed.get(msg.get("topic")) if msg.get("op") == "publish" else None
        if entry:
            try:
                entry[1](msg.get("msg", {}))
            except Exception as e:
                log(f"erreur en traitant {msg.get('topic')} : {e}")

    def run_forever(self):
        import websocket   # websocket-client

        header = [f"Authorization: Bearer {self.token}"] if self.token else []
        delay = 1.0
        while True:
            self._ws = websocket.WebSocketApp(
                self.url, header=header, on_open=self._on_open, on_message=self._on_message,
                on_close=lambda *a: self._connected.clear())
            started = time.monotonic()
            self._ws.run_forever(ping_interval=20, ping_timeout=10)
            self._connected.clear()
            if time.monotonic() - started > 60:
                delay = 1.0
            log(f"rosbridge déconnecté, nouvel essai dans {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


# ─────────────────────────────────────────────────────────────────────────────
# Overlay hub: SSE clients, recent audio, captions
# ─────────────────────────────────────────────────────────────────────────────

class OverlayHub:

    def __init__(self, keep: int = 20):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._audio: dict[str, bytes] = {}
        self._keep = keep
        self._ids = itertools.count(1)
        self._captions: collections.deque = collections.deque(maxlen=20)   # (t, text)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=50)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def audio(self, audio_id: str) -> bytes | None:
        with self._lock:
            return self._audio.get(audio_id)

    def add_caption(self, text: str, sound: bool = False):
        with self._lock:
            self._captions.append((time.monotonic(), text, sound))

    def _pop_caption(self) -> tuple[str, bool]:
        # Each "say" text is synthesized into one MP3, in the same order.
        now = time.monotonic()
        while self._captions:
            t, text, sound = self._captions.popleft()
            if now - t <= CAPTION_MAX_AGE_S:
                return text, sound
        return "", False

    def publish_audio(self, mp3: bytes) -> str:
        audio_id = str(next(self._ids))
        with self._lock:
            self._audio[audio_id] = mp3
            while len(self._audio) > self._keep:
                self._audio.pop(next(iter(self._audio)))
            text, sound = self._pop_caption()
            # "sound": a story's noise, captioned like for the deaf ("[coup de feu]").
            event = {"id": audio_id, "text": text, "sound": sound}
            for q in self._clients:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass
        return audio_id


# ─────────────────────────────────────────────────────────────────────────────
# Speech ↔ gesture sync
# ─────────────────────────────────────────────────────────────────────────────

def parse_say(data: str) -> str:
    """Caption text of an /actuators/say message: plain text or JSON {"text", "sync"}."""
    data = (data or "").strip()
    if data.startswith("{"):
        try:
            obj = json.loads(data)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "text" in obj:
            return str(obj.get("text") or "").strip()
    return data


def is_sound(data: str) -> bool:
    """A story's noise: JSON {"text": "[coup de feu]", "sound": base64 MP3}."""
    try:
        obj = json.loads(data or "")
    except ValueError:
        return False
    return isinstance(obj, dict) and isinstance(obj.get("sound"), str) and bool(obj["sound"])


def parse_audio_out(data: str) -> tuple[bytes, str]:
    """(MP3, sync id): plain base64, or JSON {"mp3", "sync", "speech_s"}."""
    data = (data or "").strip()
    sync = ""
    if data.startswith("{"):
        try:
            obj = json.loads(data)
        except ValueError:
            return b"", ""
        data, sync = str(obj.get("mp3") or ""), str(obj.get("sync") or "")
    try:
        return (base64.b64decode(data) if data else b""), sync
    except (ValueError, TypeError):
        return b"", ""


class SpeechSync:
    """Sentences held until the gesture that goes with them starts.

    The drone's clock is not this machine's: the "go" message carries `in_s`,
    the delay left when it was sent (0 = now), counted from its arrival here.
    """

    KEEP_S = 120.0
    MAX_LEAD_S = 20.0

    def __init__(self, max_wait_s: float, offset_s: float = 0.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.max_wait_s = max(0.0, float(max_wait_s))
        self.offset_s = float(offset_s)
        self.clock, self.sleep = clock, sleep
        self._go: dict[str, tuple[float, float]] = {}   # sync -> (due, received)
        self._cv = threading.Condition()

    def on_message(self, payload: dict):
        if payload.get("kind") != "go" or not payload.get("sync"):
            return
        try:
            in_s = float(payload.get("in_s") or 0.0)
        except (TypeError, ValueError):
            in_s = 0.0
        now = self.clock()
        with self._cv:
            self._go[str(payload["sync"])] = (now + max(0.0, in_s), now)
            for k in [k for k, (_, t) in self._go.items() if now - t > self.KEEP_S]:
                del self._go[k]
            self._cv.notify_all()

    def wait(self, sync: str) -> str:
        deadline = self.clock() + self.max_wait_s
        with self._cv:
            while sync not in self._go:
                left = deadline - self.clock()
                if left <= 0:
                    return f"attente dépassée ({self.max_wait_s:.1f} s), dite sans le geste"
                self._cv.wait(left)
            due, _ = self._go.pop(sync)
        left = due + self.offset_s - self.clock()
        if left > 0:
            self.sleep(min(left, self.MAX_LEAD_S))
        return "partie avec le geste"


HUB = OverlayHub()
BRIDGE = Rosbridge(ROSBRIDGE_URL, ROSBRIDGE_TOKEN)
SYNC = SpeechSync(SPEECH_SYNC_MAX_WAIT_S, OBS_SPEECH_OFFSET_S)
_audio_q: queue.Queue = queue.Queue(maxsize=50)


def on_say(msg: dict):
    data = msg.get("data") or ""
    text = parse_say(data)
    if text:
        HUB.add_caption(text, is_sound(data))


def on_audio_out(msg: dict):
    mp3, sync = parse_audio_out(msg.get("data", ""))
    if not mp3:
        return
    try:
        _audio_q.put_nowait((mp3, sync))
    except queue.Full:
        log("file de la voix pleine — phrase perdue")


def on_speech_sync(msg: dict):
    try:
        payload = json.loads(msg.get("data") or "{}")
    except ValueError:
        return
    if isinstance(payload, dict):
        SYNC.on_message(payload)


def audio_loop():
    """One sentence at a time, in order; a linked one waits for its gesture."""
    while True:
        mp3, sync = _audio_q.get()
        if sync:
            log(f"phrase {sync} : {SYNC.wait(sync)}")
        if HUB.client_count() == 0:
            # Still consumed so the next caption stays paired with its audio.
            log("voix reçue mais aucun overlay OBS ouvert — personne ne l'entend")
        HUB.publish_audio(mp3)


def status_loop():
    """Heartbeat only while OBS shows the overlay: static_driver then marks
    this voice connected, and body-gateway sends it speech."""
    while True:
        overlays = HUB.client_count()
        if overlays:
            BRIDGE.publish(topic("status"), {"data": json.dumps({"overlays": overlays})})
        time.sleep(STATUS_PERIOD_S)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP: /overlay, /events (SSE), /audio/<id>.mp3, /speaking, /ping
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def _authorized(self, query: dict) -> bool:
        token = query.get("token", [""])[0]
        if hmac.compare_digest(token.encode(), OBS_TOKEN.encode()):
            return True
        self._send(401, b"unauthorized", "text/plain")
        return False

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/ping":
            self._send(200, json.dumps({"ok": True, "service": "obs-agent",
                                        "overlays": HUB.client_count()}).encode(),
                       "application/json")
            return
        if not self._authorized(query):
            return
        if url.path == "/overlay":
            self._send(200, OVERLAY_HTML.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/events":
            self._events()
        elif url.path.startswith("/audio/") and url.path.endswith(".mp3"):
            mp3 = HUB.audio(url.path[len("/audio/"):-len(".mp3")])
            if mp3 is None:
                self._send(404, b"not found", "text/plain")
            else:
                self._send(200, mp3, "audio/mpeg")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/speaking":
            self._send(404, b"not found", "text/plain")
            return
        if not self._authorized(urllib.parse.parse_qs(url.query)):
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            speaking = bool(json.loads(self.rfile.read(length)).get("speaking"))
        except (ValueError, AttributeError):
            self._send(400, b"bad request", "text/plain")
            return
        BRIDGE.publish(topic("speaking"), {"data": speaking})
        self._send(200, b'{"ok": true}', "application/json")

    def _events(self):
        q = HUB.subscribe()
        log(f"overlay OBS ouvert ({HUB.client_count()})")
        # Announce the voice at once rather than at the next heartbeat.
        BRIDGE.publish(topic("status"), {"data": json.dumps({"overlays": HUB.client_count()})})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    payload = f"data: {json.dumps(q.get(timeout=15))}\n\n"
                except queue.Empty:
                    payload = ": keepalive\n\n"
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            HUB.unsubscribe(q)
            log(f"overlay OBS fermé ({HUB.client_count()})")

    def _send(self, code: int, body: bytes, content_type: str):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass


def main():
    # PID 1 in the container: without a handler, the kernel drops SIGTERM and
    # `docker stop` waits for its timeout before sending SIGKILL.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not OBS_TOKEN or not ROSBRIDGE_TOKEN:
        log("ERREUR : OBS_TOKEN et ROSBRIDGE_TOKEN sont obligatoires")
        sys.exit(1)
    BRIDGE.advertise(topic("status"), "std_msgs/String")
    BRIDGE.advertise(topic("speaking"), "std_msgs/Bool")
    BRIDGE.subscribe(f"/bodies/{DRONE_ID}/actuators/say", "std_msgs/String", on_say)
    BRIDGE.subscribe(topic("audio_out"), "std_msgs/String", on_audio_out)
    BRIDGE.subscribe(SYNC_TOPIC, "std_msgs/String", on_speech_sync)
    threading.Thread(target=audio_loop, daemon=True).start()
    threading.Thread(target=BRIDGE.run_forever, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    log(f"[{DRONE_ID}] overlay OBS → :{HTTP_PORT}/overlay?token=…  |  rosbridge → {ROSBRIDGE_URL}")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
