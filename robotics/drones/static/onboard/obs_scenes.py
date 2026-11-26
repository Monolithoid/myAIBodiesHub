"""
obs_scenes.py — OBS follows the running activity (C33).

body-gateway publishes /activity ({"activity": "hangman" | null}) on every change
and again every ~15 s.  The static drone, on the same local network as OBS,
switches OBS's program scene when the activity changes, through obs-websocket v5
(built into OBS 28+, Tools > WebSocket Server Settings).  Only the scene changes:
no sources, no audio.

    OBS_WS_URL       ws://<ip of the OBS PC>:4455 (empty = feature off)
    OBS_WS_PASSWORD  obs-websocket password (empty when authentication is off)
    OBS_SCENES       activity=scene pairs, comma separated; "default" is the scene
                     when no activity runs, and for an activity without its own
                     scene.  e.g. default=Plateau,hangman=Pendu

The first value received after startup is only remembered (a restart of the
drone during a stream does not force a scene): only changes switch.  Between
two changes the streamer keeps control of OBS.  OBS unreachable, password
refused or unknown scene → a log line, nothing else.
"""

import base64
import hashlib
import itertools
import json
import logging
import queue
import threading

log = logging.getLogger("static_drone.obs")

DEFAULT = "default"
RPC_VERSION = 1


def parse_scenes(text):
    """ "default=Plateau, hangman=Pendu" → {"default": "Plateau", "hangman": "Pendu"}."""
    scenes = {}
    for part in (text or "").split(","):
        if "=" not in part:
            if part.strip():
                log.warning("OBS_SCENES: '%s' ignored (activity=scene expected)", part.strip())
            continue
        activity, scene = part.split("=", 1)
        if activity.strip() and scene.strip():
            scenes[activity.strip().lower()] = scene.strip()
    return scenes


def scene_for(activity, scenes):
    """Scene of an activity (None = idle), else the default scene, else None."""
    if activity and activity.lower() in scenes:
        return scenes[activity.lower()]
    return scenes.get(DEFAULT)


def auth_string(password, salt, challenge):
    """obs-websocket v5 authentication: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsError(Exception):
    pass


class ObsClient:
    """One short obs-websocket v5 session per request: Hello → Identify → Request."""

    def __init__(self, url, password="", timeout=5.0, connect=None):
        self.url = url
        self.password = password
        self.timeout = timeout
        self._connect = connect or self._default_connect
        self._ids = itertools.count(1)

    def _default_connect(self, url, timeout):
        import websocket   # websocket-client
        return websocket.create_connection(url, timeout=timeout)

    def set_scene(self, scene):
        ws = self._connect(self.url, self.timeout)
        try:
            hello = self._recv(ws, 0)
            identify = {"rpcVersion": RPC_VERSION, "eventSubscriptions": 0}
            auth = hello.get("authentication")
            if auth:
                if not self.password:
                    raise ObsError("OBS asks for a password (OBS_WS_PASSWORD empty)")
                identify["authentication"] = auth_string(self.password, auth["salt"],
                                                         auth["challenge"])
            ws.send(json.dumps({"op": 1, "d": identify}))
            self._recv(ws, 2)
            request_id = str(next(self._ids))
            ws.send(json.dumps({"op": 6, "d": {"requestType": "SetCurrentProgramScene",
                                               "requestId": request_id,
                                               "requestData": {"sceneName": scene}}}))
            status = self._recv(ws, 7).get("requestStatus") or {}
            if not status.get("result"):
                raise ObsError("scene '{}' refused: {} {}".format(
                    scene, status.get("code"), status.get("comment", "")).strip())
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _recv(self, ws, op):
        """Next message of this op code; a closed socket here usually means a bad password."""
        while True:
            raw = ws.recv()
            if not raw:
                raise ObsError("connection closed by OBS (wrong OBS_WS_PASSWORD?)")
            msg = json.loads(raw)
            if msg.get("op") == op:
                return msg.get("d") or {}


class SceneSwitcher:
    """Turns /activity messages into scene switches, in its own thread."""

    def __init__(self, client, scenes):
        self.client = client
        self.scenes = scenes
        self._known = False
        self._activity = None
        self._lock = threading.Lock()
        self._todo = queue.Queue()

    def on_activity(self, msg):
        """rosbridge callback (std_msgs/String): must not block."""
        try:
            data = json.loads(msg.get("data") or "{}")
            activity = data.get("activity") or None
        except (ValueError, AttributeError):
            log.warning("/activity: unreadable message %r", msg)
            return
        scene = self.change(activity)
        if scene:
            self._todo.put((activity, scene))

    def change(self, activity):
        """Scene to switch to for this activity, or None (first value, no change, no scene)."""
        with self._lock:
            first, before = not self._known, self._activity
            self._known, self._activity = True, activity
        if first:
            log.info("Activity at startup: %s (OBS scene left as is)", activity or "none")
            return None
        if activity == before:
            return None
        scene = scene_for(activity, self.scenes)
        if not scene:
            log.info("Activity %s: no scene in OBS_SCENES", activity or "none")
        return scene

    def run(self, stop):
        while not stop.is_set():
            try:
                activity, scene = self._todo.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.client.set_scene(scene)
                log.info("Activity %s → OBS scene '%s'", activity or "none", scene)
            except Exception as exc:
                log.warning("OBS scene '%s' not set: %s", scene, exc)
