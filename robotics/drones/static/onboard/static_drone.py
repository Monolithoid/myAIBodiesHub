#!/usr/bin/env python3
"""
static_drone.py — Onboard program of the static drone (ADR 004).

Runs on an Ubuntu PC or a Raspberry Pi 5 (Raspberry Pi OS 64-bit) placed in the
room. It opens an OUTBOUND WebSocket connection to rosbridge on the VPS (same
principle as the NAO, ADR 002) and handles:

    - heartbeat            → /drones/{id}/status        (every 30 s)
    - listening            → /drones/{id}/audio         (base64 WAV + angle)
    - local wake word      → /drones/{id}/wake          (WAKE_MODE=local)
    - speech               ← /drones/{id}/audio_out     (base64 MP3, or JSON
                                                         {"mp3", "sync", "speech_s"})
                           → /drones/{id}/speaking      (true / false)
                           ← /speech_gesture_sync       ({"kind": "go", "sync", "at"}:
                                                         a sentence tied to a NAO
                                                         gesture waits for its start)
    - image                → /drones/{id}/image         (base64 JPEG)
                           ← /drones/{id}/image_request

Configuration through environment variables (see .env.example). A .env file
placed next to the script is read at startup, without overwriting variables
that are already set.

Run:
    python3 static_drone.py
"""

import base64
import collections
import io
import json
import logging
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
import wave

import numpy as np

import direction
import wakeword

log = logging.getLogger("static_drone")

HERE = os.path.dirname(os.path.abspath(__file__))

# USB IDs of the ReSpeaker USB Mic Array v2.0 (Seeed's tuning.py,
# github.com/respeaker/usb_4_mic_array).
RESPEAKER_VID = 0x2886
RESPEAKER_PID = 0x0018

# WAKE_MODE=local: after detection, the speech of the first moments may
# still be the end of "nao" and does not start the sentence.
WAKE_TAIL_S = 0.3


# ======================================================================
# Configuration
# ======================================================================

def load_dotenv(path):
    """Reads a simple KEY=VALUE file, without overwriting the existing environment."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


def _env(name, default=None):
    value = os.environ.get(name, "").strip()
    return value if value else default


def _env_float(name, default):
    return float(_env(name, default))


def _env_int(name, default):
    return int(_env(name, default))


def _env_bool(name, default=False):
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "oui", "on")


class Config:
    def __init__(self):
        self.drone_id = _env("DRONE_ID", "static_01")
        domain = _env("DOMAIN")
        self.rosbridge_url = _env("ROSBRIDGE_URL",
                                  "wss://rosbridge.{}".format(domain) if domain else None)
        self.rosbridge_token = _env("ROSBRIDGE_TOKEN")

        # Audio
        self.audio_input_device = _env("AUDIO_INPUT_DEVICE")    # (partial) name or index
        self.sample_rate = _env_int("SAMPLE_RATE", 16000)
        self.record_channel = _env_int("RECORD_CHANNEL", 0)
        self.mics = direction.parse_mics(_env("MICS", ""))
        self.rms_threshold = _env_float("RMS_THRESHOLD", 500)    # on int16 samples
        self.silence_s = _env_float("SILENCE_S", 0.8)
        self.min_speech_s = _env_float("MIN_SPEECH_S", 0.2)
        self.max_record_s = _env_float("MAX_RECORD_S", 15)
        self.pre_roll_s = _env_float("PRE_ROLL_S", 0.3)
        self.block_ms = _env_int("BLOCK_MS", 30)

        # Wake word: server = everything is sent, body-gateway looks for "ok nao"
        # in the text; local = openWakeWord on the drone, only the following
        # sentence is sent.
        self.wake_mode = _env("WAKE_MODE", "server").lower()
        self.wake_model = _env("WAKE_MODEL", "ok_nao")
        self.wake_threshold = _env_float("WAKE_THRESHOLD", 0.5)
        self.wake_listen_s = _env_float("WAKE_LISTEN_S", 5)
        self.wake_pre_roll_s = _env_float("WAKE_PRE_ROLL_S", 0.5)

        # ReSpeaker
        self.respeaker = _env("RESPEAKER", "auto").lower()       # auto | 0 | 1
        self.respeaker_aec = _env_bool("RESPEAKER_AEC", False)
        self.doa_offset = _env_float("DOA_OFFSET", 0)
        self.doa_invert = _env_bool("DOA_INVERT", False)

        # Haut-parleur
        self.player_cmd = _env("PLAYER_CMD", "mpg123 -q -")
        self.playback_tail_s = _env_float("PLAYBACK_TAIL_S", 0.3)
        # Phrase liée à un geste du NAO : attente maximale du départ du geste (s).
        self.speech_sync_max_wait_s = _env_float("SPEECH_SYNC_MAX_WAIT_S", 4.0)

        # Camera
        self.camera_index = _env("CAMERA_INDEX", "0")
        self.camera_hfov = _env_float("CAMERA_HFOV", 66)
        self.camera_width = _env_int("CAMERA_WIDTH", 320)
        self.camera_height = _env_int("CAMERA_HEIGHT", 240)
        self.camera_fps = _env_float("CAMERA_FPS", 5)
        self.jpeg_quality = _env_int("JPEG_QUALITY", 70)
        self.motion_threshold = _env_int("MOTION_THRESHOLD", 25)
        self.image_interval = _env_float("IMAGE_INTERVAL", 10)  # 0 = never periodic

        # Network intervals
        self.status_interval = _env_float("STATUS_INTERVAL", 30)
        self.reconnect_max_s = _env_float("RECONNECT_MAX_S", 30)

    def topic(self, name):
        return "/drones/{}/{}".format(self.drone_id, name)


# ======================================================================
# Client rosbridge (WebSocket sortant, reconnexion automatique)
# ======================================================================

class Rosbridge:
    """rosbridge JSON client: advertise / publish / subscribe.

    Declared advertisements and subscriptions are replayed on every reconnection.
    """

    def __init__(self, url, token, reconnect_max_s=30):
        self.url = url
        self.token = token
        self.reconnect_max_s = reconnect_max_s
        self._advertised = {}       # topic → type
        self._subscribed = {}       # topic → (type, callback)
        self._ws = None
        self._connected = threading.Event()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def connected(self):
        return self._connected.is_set()

    def advertise(self, topic, msg_type):
        self._advertised[topic] = msg_type

    def subscribe(self, topic, msg_type, callback):
        self._subscribed[topic] = (msg_type, callback)

    def publish(self, topic, msg):
        """Publishes a message; returns False (message lost) if disconnected."""
        return self._send({"op": "publish", "topic": topic, "msg": msg})

    def _send(self, payload):
        ws = self._ws
        if ws is None or not self.connected:
            log.debug("Offline, message dropped: %s", payload.get("topic"))
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(payload))
            return True
        except Exception as exc:
            log.warning("Envoi impossible (%s) : %s", payload.get("topic"), exc)
            return False

    def _on_open(self, ws):
        self._connected.set()
        for topic, msg_type in self._advertised.items():
            self._send({"op": "advertise", "topic": topic, "type": msg_type})
        for topic, (msg_type, _) in self._subscribed.items():
            self._send({"op": "subscribe", "topic": topic, "type": msg_type})
        log.info("Connected to %s", self.url)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("op") != "publish":
            return
        entry = self._subscribed.get(msg.get("topic"))
        if entry:
            try:
                entry[1](msg.get("msg", {}))
            except Exception:
                log.exception("Error while handling %s", msg.get("topic"))

    def _on_error(self, ws, error):
        import websocket
        if isinstance(error, websocket.WebSocketConnectionClosedException):
            return      # normal close, handled by the reconnection loop
        log.warning("WebSocket error: %s", error)

    def _on_close(self, ws, *args):
        self._connected.clear()

    def run_forever(self):
        """Connection loop with exponential backoff (1 s → reconnect_max_s)."""
        import websocket   # websocket-client

        header = ["Authorization: Bearer {}".format(self.token)] if self.token else []
        delay = 1.0
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self.url, header=header,
                on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close)
            started = time.monotonic()
            self._ws.run_forever(ping_interval=20, ping_timeout=10)
            self._connected.clear()
            if self._stop.is_set():
                break
            # A connection that stayed open for a while resets the delay.
            if time.monotonic() - started > 60:
                delay = 1.0
            log.info("Disconnected, retrying in %.0f s", delay)
            self._stop.wait(delay)
            delay = min(delay * 2, self.reconnect_max_s)

    def stop(self):
        self._stop.set()
        if self._ws:
            self._ws.close()


# ======================================================================
# ReSpeaker USB Mic Array v2.0 (DSP parameters through pyusb)
# ======================================================================

class ReSpeaker:
    """Reads DOAANGLE and SPEECHDETECTED, like Seeed's Tuning.read().

    Parameters taken from tuning.py (github.com/respeaker/usb_4_mic_array):
        DOAANGLE       : id 21, offset 0,  int, 0..359, read-only
        SPEECHDETECTED : id 19, offset 22, int, 0/1,    read-only
    """

    PARAMETERS = {
        "DOAANGLE": (21, 0),
        "SPEECHDETECTED": (19, 22),
    }
    TIMEOUT = 100000

    def __init__(self, dev):
        self.dev = dev
        self._lock = threading.Lock()

    @classmethod
    def find(cls):
        try:
            import usb.core
        except ImportError:
            log.info("pyusb missing: no ReSpeaker")
            return None
        try:
            dev = usb.core.find(idVendor=RESPEAKER_VID, idProduct=RESPEAKER_PID)
        except Exception as exc:    # no libusb backend, etc.
            log.warning("Recherche USB impossible : %s", exc)
            return None
        return cls(dev) if dev else None

    def _read_int(self, name):
        import usb.util
        param_id, offset = self.PARAMETERS[name]
        cmd = 0x80 | offset | 0x40      # reading an integer
        with self._lock:
            response = self.dev.ctrl_transfer(
                usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                0, cmd, param_id, 8, self.TIMEOUT)
        return int(np.frombuffer(bytes(response), dtype="<i4")[0])

    def doa(self):
        return self._read_int("DOAANGLE")

    def speech_detected(self):
        return self._read_int("SPEECHDETECTED") == 1


# ======================================================================
# Camera: last image + motion
# ======================================================================

class Camera:
    def __init__(self, cfg):
        self.cfg = cfg
        self._cap = None
        self._lock = threading.Lock()
        self._frame = None          # last BGR image
        self._prev_gray = None
        self._motion = None         # (horodatage, angle)

    def open(self):
        try:
            import cv2
        except ImportError:
            log.warning("OpenCV missing: no camera")
            return False
        wanted = self.cfg.camera_index
        index = find_camera(wanted)
        if index is None:
            log.warning("Camera %r not found (v4l2-ctl --list-devices)", wanted)
            return False
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            log.warning("Camera %s not found", index)
            return False
        log.info("Camera: %s", camera_name(index) or index)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera_height)
        self._cap = cap
        return True

    @property
    def available(self):
        return self._cap is not None

    def run(self, stop):
        import cv2
        period = 1.0 / max(self.cfg.camera_fps, 0.1)
        while not stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                stop.wait(1.0)
                continue
            if frame.shape[1] != self.cfg.camera_width:
                frame = cv2.resize(frame, (self.cfg.camera_width, self.cfg.camera_height))
            gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            angle = None
            if self._prev_gray is not None:
                angle = direction.motion_angle(self._prev_gray, gray, self.cfg.camera_hfov,
                                               threshold=self.cfg.motion_threshold)
            with self._lock:
                self._frame = frame
                if angle is not None:
                    self._motion = (time.monotonic(), angle)
            self._prev_gray = gray
            stop.wait(period)
        self._cap.release()

    def last_motion_since(self, t0):
        """Last motion angle observed after time t0 (monotonic)."""
        with self._lock:
            if self._motion and self._motion[0] >= t0:
                return self._motion[1]
        return None

    def jpeg(self):
        import cv2
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        return (buf.tobytes(), frame.shape[1], frame.shape[0]) if ok else None


def _video_index(name):
    """'video2' → 2, otherwise None."""
    return int(name[5:]) if name.startswith("video") and name[5:].isdigit() else None


def camera_name(index, sysfs="/sys/class/video4linux"):
    """Name of the /dev/video{index} camera given by the kernel, or None."""
    try:
        with open(os.path.join(sysfs, "video{}".format(index), "name"), encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, TypeError):
        return None


def find_camera(wanted, sysfs="/sys/class/video4linux"):
    """Camera to open according to CAMERA_INDEX.

    - "2" → index 2;
    - a path (/dev/video2, /dev/v4l/by-id/…) → index of the target /dev/videoN;
    - otherwise a (partial, case-insensitive) name, looked up in the kernel's camera
      names (those of v4l2-ctl --list-devices): first matching node.
      A webcam often creates two nodes (image + metadata): the first one is
      the image one.
    Returns an int, a path to pass as is to OpenCV, or None.
    """
    wanted = (wanted or "0").strip()
    if wanted.isdigit():
        return int(wanted)
    if wanted.startswith("/"):
        index = _video_index(os.path.basename(os.path.realpath(wanted)))
        if index is not None:
            return index
        return wanted if os.path.exists(wanted) else None
    try:
        nodes = sorted((n for n in os.listdir(sysfs) if _video_index(n) is not None),
                       key=_video_index)
    except OSError:
        return None
    for node in nodes:
        name = camera_name(_video_index(node), sysfs)
        if name and wanted.lower() in name.lower():
            return _video_index(node)
    return None


# ======================================================================
# Outils audio
# ======================================================================

def pcm_to_wav(pcm, rate):
    """int16 mono → octets WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


def find_input_device(sd, wanted):
    """Audio input index: AUDIO_INPUT_DEVICE, otherwise ReSpeaker, otherwise default."""
    if wanted is not None and wanted.isdigit():
        return int(wanted)
    needle = (wanted or "respeaker").lower()
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and needle in dev["name"].lower():
            return i
    if wanted:
        raise RuntimeError("Audio input {!r} not found".format(wanted))
    return None     # default device


def choose_sample_rate(sd, device, channels, wanted, info):
    """SAMPLE_RATE if the input accepts it, otherwise its default rate.

    USB microphones opened directly (hw:…) often refuse 16 kHz; the WAV
    sent states its rate, Whisper adapts.
    """
    try:
        sd.check_input_settings(device=device, channels=channels, samplerate=wanted,
                                dtype="int16")
        return wanted
    except Exception as exc:
        fallback = int(info["default_samplerate"])
        log.warning("%d Hz refused by %r (%s): %d Hz used", wanted, info["name"],
                    exc, fallback)
        return fallback


# ======================================================================
# Synchronisation parole ↔ geste du NAO
# ======================================================================

SYNC_TOPIC = "/speech_gesture_sync"


def parse_audio_out(data):
    """(MP3, id de synchro) : base64 seul, ou JSON {"mp3", "sync", "speech_s"}."""
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
    """Phrases retenues jusqu'au départ du geste qui va avec.

    Le service des gestes (emotion_gesture.py) publie {"kind": "go", "sync", "at"}
    sur /speech_gesture_sync : `at` = heure (time.time() de cette machine) où la
    phrase doit partir, 0 = tout de suite (geste refusé, driver NAO compris).
    """

    KEEP_S = 120.0
    MAX_LEAD_S = 20.0

    def __init__(self, max_wait_s, clock=time.time, sleep=time.sleep):
        self.max_wait_s = max(0.0, float(max_wait_s))
        self.clock, self.sleep = clock, sleep
        self._go = {}
        self._cv = threading.Condition()

    def on_message(self, payload):
        if payload.get("kind") != "go" or not payload.get("sync"):
            return
        try:
            at = float(payload.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        now = self.clock()
        with self._cv:
            self._go[str(payload["sync"])] = (at, now)
            for k in [k for k, (_, t) in self._go.items() if now - t > self.KEEP_S]:
                del self._go[k]
            self._cv.notify_all()

    def wait(self, sync):
        """Attend le départ du geste ; renvoie ce qui a décidé ("go …", "attente dépassée")."""
        deadline = self.clock() + self.max_wait_s
        with self._cv:
            while sync not in self._go:
                left = deadline - self.clock()
                if left <= 0:
                    return "attente dépassée ({:.1f} s), dite sans le geste".format(self.max_wait_s)
                self._cv.wait(left)
            at, _ = self._go.pop(sync)
        left = at - self.clock() if at else 0.0
        if left > 0:
            # Le geste peut d'abord passer plusieurs secondes à rejoindre sa pose de départ.
            self.sleep(min(left, self.MAX_LEAD_S))
        return "dite {:+.0f} ms après le signal du geste".format(max(0.0, left) * 1000)


# ======================================================================
# Drone statique
# ======================================================================

class StaticDrone:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.speaking = threading.Event()
        self.started = time.monotonic()
        self.bridge = Rosbridge(cfg.rosbridge_url, cfg.rosbridge_token, cfg.reconnect_max_s)
        self.respeaker = None
        self.camera = Camera(cfg)
        self.channels = 1
        self.source = direction.SOURCE_NONE
        self.sample_rate = cfg.sample_rate
        self.wake = None            # wakeword.WakeWordDetector if WAKE_MODE=local
        self._audio_q = queue.Queue(maxsize=200)
        self._play_lock = threading.Lock()
        self.speech_sync = SpeechSync(cfg.speech_sync_max_wait_s)
        self._rms_peak = 0.0
        self._rms_logged = 0.0

    # ------------------------------------------------------------------
    # Hardware
    # ------------------------------------------------------------------

    def detect_hardware(self):
        if self.cfg.respeaker in ("auto", "1"):
            self.respeaker = ReSpeaker.find()
            if self.respeaker is None and self.cfg.respeaker == "1":
                raise RuntimeError("RESPEAKER=1 but no USB ReSpeaker found")
        if self.respeaker:
            log.info("ReSpeaker USB detected")
        self.camera.open()
        self.source = direction.choose_direction_source(
            self.respeaker is not None, len(self.cfg.mics), self.camera.available)
        log.info("Direction: %s (%d mics declared, camera: %s)", self.source,
                 len(self.cfg.mics), "yes" if self.camera.available else "no")

    def mute_while_speaking(self):
        """Cuts listening during playback, except for a ReSpeaker with echo cancellation."""
        return not (self.respeaker is not None and self.cfg.respeaker_aec)

    # ------------------------------------------------------------------
    # Listening
    # ------------------------------------------------------------------

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            log.debug("Audio : %s", status)
        try:
            self._audio_q.put_nowait(indata.copy())
        except queue.Full:
            pass

    def listen_loop(self):
        import sounddevice as sd

        device = find_input_device(sd, self.cfg.audio_input_device)
        info = sd.query_devices(device, "input")
        max_ch = int(info["max_input_channels"])
        if self.respeaker is not None:
            # 6-channel firmware: channel 0 = processed audio, 1-4 = raw mics.
            # 1-channel firmware: channel 0 = processed audio.
            self.channels = min(max_ch, 6)
        else:
            self.channels = min(max_ch, max(self.cfg.record_channel + 1, len(self.cfg.mics), 1))
        if self.source == direction.SOURCE_LOUDEST_MIC and self.channels < len(self.cfg.mics):
            raise RuntimeError("MICS declares {} mics but {!r} only has {} channels".format(
                len(self.cfg.mics), info["name"], self.channels))
        rate = self.sample_rate = choose_sample_rate(sd, device, self.channels,
                                                     self.cfg.sample_rate, info)
        log.info("Audio input: %s (%d channels, %d Hz)", info["name"], self.channels, rate)

        if self.cfg.wake_mode == "local":
            model, name = wakeword.load_model(self.cfg.wake_model, os.path.join(HERE, "models"))
            self.wake = wakeword.WakeWordDetector(model, name, self.cfg.wake_threshold, rate)
            log.info("Local wake word: model %s, threshold %.2f", name, self.cfg.wake_threshold)

        block = int(rate * self.cfg.block_ms / 1000)
        with sd.InputStream(device=device, channels=self.channels, samplerate=rate,
                            blocksize=block, dtype="int16", callback=self._audio_callback):
            if self.wake is not None:
                self._listen_after_wake(rate)
            else:
                self._listen_all(rate)

    def _blocks(self, rate):
        """Received blocks: (frames, mono, audio clock), or None while the drone speaks."""
        # Audio clock (sum of the received blocks): insensitive to processing delays.
        now = 0.0
        while not self.stop.is_set():
            try:
                frames = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            now += frames.shape[0] / float(rate)
            if self.speaking.is_set() and self.mute_while_speaking():
                yield None
                continue
            yield frames, frames[:, min(self.cfg.record_channel, frames.shape[1] - 1)], now

    def _blocks_for(self, seconds):
        return max(1, int(seconds * 1000 / self.cfg.block_ms))

    def _listen_all(self, rate):
        """WAKE_MODE=server: every speech segment is sent to the VPS."""
        pre_roll = collections.deque(maxlen=self._blocks_for(self.cfg.pre_roll_s))
        recording = None            # list of mono blocks during a recording
        levels = None               # cumulative energy per mic
        doas = []
        t_start = t_voice = 0.0
        wall_start = 0.0
        voiced_s = 0.0

        for block in self._blocks(rate):
            if block is None:
                recording = None
                pre_roll.clear()
                continue
            frames, mono, now = block
            speech = self._is_speech(mono)

            if recording is None:
                pre_roll.append(mono)
                if not speech:
                    continue
                recording = list(pre_roll)
                pre_roll.clear()
                levels = np.zeros(max(len(self.cfg.mics), 1))
                doas = []
                t_start = t_voice = now
                wall_start = time.monotonic()
                voiced_s = 0.0
                log.info("Speech detected")
            else:
                recording.append(mono)

            if speech:
                t_voice = now
                voiced_s += frames.shape[0] / float(rate)
                self._sample_direction(frames, levels, doas)

            if now - t_voice >= self.cfg.silence_s or now - t_start >= self.cfg.max_record_s:
                if voiced_s >= self.cfg.min_speech_s:
                    found = self._final_direction(levels, doas, wall_start)
                    self.publish_audio(np.concatenate(recording), found)
                else:
                    log.info("Too short, ignored (%.2f s)", voiced_s)
                recording = None

    def _listen_after_wake(self, rate):
        """WAKE_MODE=local: continuous listening, only the sentence following "ok nao" is sent.

        Like a smart speaker: a ring buffer keeps the last
        WAKE_PRE_ROLL_S seconds; on the wake word, recording starts from that
        buffer ("ok nao quelle heure…" said in one go is not cut), waits
        for the start of the sentence up to WAKE_LISTEN_S, then stops after SILENCE_S
        of silence.
        """
        pre_roll = collections.deque(maxlen=self._blocks_for(self.cfg.wake_pre_roll_s))
        recording = None
        muted = False
        levels = None
        doas = []
        score = 0.0
        t_wake = t_voice = 0.0
        wall_start = 0.0
        voiced_s = 0.0

        for block in self._blocks(rate):
            if block is None:
                if not muted:
                    # The drone speaks: no wake-up on its own voice, no sentence in progress.
                    recording = None
                    pre_roll.clear()
                    self.wake.reset()
                    muted = True
                continue
            muted = False
            frames, mono, now = block

            if recording is None:
                pre_roll.append(mono)
                found = self.wake.process(mono)
                if found is None:
                    continue
                score = found
                self.wake.reset()
                self.publish_wake(score)
                recording = list(pre_roll)
                pre_roll.clear()
                levels = np.zeros(max(len(self.cfg.mics), 1))
                doas = []
                t_wake, t_voice = now, None
                wall_start = time.monotonic()
                voiced_s = 0.0
                continue

            recording.append(mono)
            # The end of "nao" may still sound right after detection: it
            # stays in the recording but does not count as the start of the sentence.
            if self._is_speech(mono) and (t_voice is not None or now - t_wake >= WAKE_TAIL_S):
                t_voice = now
                voiced_s += frames.shape[0] / float(rate)
                self._sample_direction(frames, levels, doas)

            if t_voice is None:
                if now - t_wake >= self.cfg.wake_listen_s:
                    log.info("Nothing heard after the wake word")
                    recording = None
                continue
            if now - t_voice >= self.cfg.silence_s or now - t_wake >= self.cfg.max_record_s:
                if voiced_s >= self.cfg.min_speech_s:
                    found = self._final_direction(levels, doas, wall_start)
                    self.publish_audio(np.concatenate(recording), found, wake_score=score)
                else:
                    log.info("Too short, ignored (%.2f s)", voiced_s)
                recording = None

    def _is_speech(self, mono):
        if self.respeaker is not None:
            try:
                return self.respeaker.speech_detected()
            except Exception as exc:
                log.warning("Unable to read SPEECHDETECTED, RMS threshold used: %s", exc)
        level = float(direction.channel_rms(mono)[0])
        # In DEBUG, the max level of each second helps tune RMS_THRESHOLD.
        self._rms_peak = max(self._rms_peak, level)
        if time.monotonic() - self._rms_logged >= 1.0:
            log.debug("Max RMS over 1 s: %.0f (threshold %.0f)", self._rms_peak, self.cfg.rms_threshold)
            self._rms_peak, self._rms_logged = 0.0, time.monotonic()
        return level >= self.cfg.rms_threshold

    def _sample_direction(self, frames, levels, doas):
        """Collects what is needed to compute the direction, block by block during speech."""
        if self.source == direction.SOURCE_RESPEAKER:
            try:
                doas.append(self.respeaker.doa())
            except Exception as exc:
                log.debug("Lecture DOAANGLE impossible : %s", exc)
        elif self.source == direction.SOURCE_LOUDEST_MIC:
            n = len(self.cfg.mics)
            levels += direction.channel_rms(frames[:, :n]) ** 2

    def _final_direction(self, levels, doas, wall_start):
        """Returns (angle or None, direction_source) according to the ADR 004 rule."""
        if self.source == direction.SOURCE_RESPEAKER:
            mean = direction.circular_mean_deg(doas)
            if mean is not None:
                return direction.respeaker_angle(mean, self.cfg.doa_offset,
                                                 self.cfg.doa_invert), self.source
        elif self.source == direction.SOURCE_LOUDEST_MIC:
            angle = direction.loudest_mic(levels, [a for _, a in self.cfg.mics])
            if angle is not None:
                return direction.normalize_angle(angle + self.cfg.doa_offset), self.source
        elif self.source == direction.SOURCE_IMAGE:
            angle = self.camera.last_motion_since(wall_start - 1.0)
            if angle is not None:
                return round(angle, 1), self.source
        return None, direction.SOURCE_NONE

    def publish_audio(self, pcm, angle_source, wake_score=None):
        angle, source = angle_source
        wav = pcm_to_wav(pcm, self.sample_rate)
        payload = {
            "wav": base64.b64encode(wav).decode("ascii"),
            "azimuth_deg": None if angle is None else round(angle, 1),
            "direction_source": source,
        }
        if wake_score is not None:
            # Sentence following a wake word detected here: body-gateway does not
            # filter it again.
            payload["wake"] = "local"
            payload["wake_score"] = round(wake_score, 2)
        sent = self.bridge.publish(self.cfg.topic("audio"), {"data": json.dumps(payload)})
        log.info("Audio %s: %.1f s, angle %s (%s)", "sent" if sent else "lost (offline)",
                 len(pcm) / float(self.sample_rate), payload["azimuth_deg"], source)

    def publish_wake(self, score):
        """Tells the VPS on detection: the control room shows "ok nao" right away."""
        log.info("Wake word heard (%s, score %.2f)", self.wake.name, score)
        self.bridge.publish(self.cfg.topic("wake"), {"data": json.dumps({
            "model": self.wake.name, "score": round(score, 2),
            "threshold": self.cfg.wake_threshold})})

    # ------------------------------------------------------------------
    # Parole
    # ------------------------------------------------------------------

    def on_audio_out(self, msg):
        mp3, sync = parse_audio_out(msg.get("data", ""))
        if mp3:
            threading.Thread(target=self.play_mp3, args=(mp3, sync), daemon=True).start()

    def on_speech_sync(self, msg):
        try:
            payload = json.loads(msg.get("data") or "{}")
        except ValueError:
            return
        if isinstance(payload, dict):
            self.speech_sync.on_message(payload)

    def play_mp3(self, mp3, sync=""):
        with self._play_lock:   # a single playback at a time, in arrival order
            if sync:
                # Sentence tied to a NAO gesture: it starts when the gesture starts
                # (plus the offset set in emotion_gesture.env), at the latest
                # after SPEECH_SYNC_MAX_WAIT_S.
                how = self.speech_sync.wait(sync)
                log.info("Sentence %s: %s", sync, how)
            self.speaking.set()
            self.bridge.publish(self.cfg.topic("speaking"), {"data": True})
            try:
                subprocess.run(shlex.split(self.cfg.player_cmd), input=mp3,
                               stdout=subprocess.DEVNULL, check=False, timeout=120)
            except Exception as exc:
                log.warning("Lecture impossible (%s) : %s", self.cfg.player_cmd, exc)
            finally:
                # Lets the room's echo die down before listening again.
                time.sleep(self.cfg.playback_tail_s)
                self.speaking.clear()
                self.bridge.publish(self.cfg.topic("speaking"), {"data": False})

    # ------------------------------------------------------------------
    # Image
    # ------------------------------------------------------------------

    def publish_image(self, reason):
        if not self.camera.available:
            return
        result = self.camera.jpeg()
        if result is None:
            return
        jpeg, width, height = result
        payload = {"jpeg": base64.b64encode(jpeg).decode("ascii"),
                   "width": width, "height": height, "reason": reason}
        self.bridge.publish(self.cfg.topic("image"), {"data": json.dumps(payload)})

    def on_image_request(self, msg):
        threading.Thread(target=self.publish_image, args=("request",), daemon=True).start()

    def image_loop(self):
        while not self.stop.wait(self.cfg.image_interval):
            self.publish_image("periodic")

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def status(self):
        capabilities = ["audio_in", "audio_out"]
        if self.camera.available:
            capabilities.append("image")
        if self.source != direction.SOURCE_NONE:
            capabilities.append("direction")
        if self.respeaker is not None:
            n_mics = 4
        else:
            n_mics = max(len(self.cfg.mics), 1)
        return {
            "drone_id": self.cfg.drone_id,
            "type": "static",
            "capabilities": capabilities,
            "mics": n_mics,
            "respeaker": self.respeaker is not None,
            "aec": self.respeaker is not None and self.cfg.respeaker_aec,
            "direction_mode": self.source,
            "wake_mode": self.cfg.wake_mode,
            "camera_hfov_deg": self.cfg.camera_hfov if self.camera.available else None,
            "speaking": self.speaking.is_set(),
            "uptime_s": int(time.monotonic() - self.started),
        }

    def status_loop(self):
        while not self.stop.is_set():
            if self.bridge.connected:
                self.bridge.publish(self.cfg.topic("status"), {"data": json.dumps(self.status())})
            self.stop.wait(self.cfg.status_interval)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def run(self):
        self.detect_hardware()
        b = self.bridge
        b.advertise(self.cfg.topic("status"), "std_msgs/String")
        b.advertise(self.cfg.topic("audio"), "std_msgs/String")
        b.advertise(self.cfg.topic("speaking"), "std_msgs/Bool")
        b.advertise(self.cfg.topic("wake"), "std_msgs/String")
        b.advertise(self.cfg.topic("image"), "std_msgs/String")
        b.subscribe(self.cfg.topic("audio_out"), "std_msgs/String", self.on_audio_out)
        b.subscribe(SYNC_TOPIC, "std_msgs/String", self.on_speech_sync)
        b.subscribe(self.cfg.topic("image_request"), "std_msgs/Empty", self.on_image_request)

        threads = [threading.Thread(target=b.run_forever, name="rosbridge"),
                   threading.Thread(target=self.status_loop, name="status"),
                   threading.Thread(target=self._guard(self.listen_loop), name="listen")]
        if self.camera.available:
            threads.append(threading.Thread(target=self.camera.run, args=(self.stop,),
                                            name="camera"))
            if self.cfg.image_interval > 0:
                threads.append(threading.Thread(target=self.image_loop, name="image"))
        for t in threads:
            t.daemon = True
            t.start()
        self.stop.wait()
        b.stop()

    def _guard(self, fn):
        """Stops the program if a critical loop fails (systemd will restart it)."""
        def wrapper():
            try:
                fn()
            except Exception:
                log.exception("Stopping on error in %s", fn.__name__)
                self.stop.set()
        return wrapper


def main():
    load_dotenv(os.path.join(HERE, ".env"))
    logging.basicConfig(level=_env("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    if not cfg.rosbridge_url:
        log.error("ROSBRIDGE_URL (or DOMAIN) is required")
        return 2
    if cfg.wake_mode not in ("server", "local"):
        log.error("WAKE_MODE=%s: server or local expected", cfg.wake_mode)
        return 2
    if not cfg.rosbridge_token:
        log.warning("ROSBRIDGE_TOKEN empty: rosbridge-proxy will refuse the connection")

    drone = StaticDrone(cfg)
    stopped = []

    def on_signal(*_):
        stopped.append(True)
        drone.stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("Drone %s → %s", cfg.drone_id, cfg.rosbridge_url)
    drone.run()
    # Non-zero code if the stop comes from an error: systemd will restart it.
    return 0 if stopped else 1


if __name__ == "__main__":
    sys.exit(main())
