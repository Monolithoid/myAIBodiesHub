#!/usr/bin/env python3
"""
emotion_gesture.py — Learned NAO gestures, generated on the static drone (ADR 009).

The nao-emotion-motion generator (`nao_gesture.py` + `nao_gesture.json`) is taken
from the public nao-emotion-motion-dataset repository, at the version pinned by
hand in `emotion_gesture.env` (GESTURE_DATASET_REF: tag or commit). No automatic
update: changing the model = changing that line, then "reload" from the control
room or restart the service.

Chain of a gesture:

    /nao/emotion_gesture   ← {"id", "emotion", "intensity", "event"}  (NAO driver / control room)
    /nao/read_angles       → {"id"}                                   (to the robot)
    /nao/joint_angles      ← {"id", "angles"}                         (measured posture)
    generate(..., current_pose, from_rest, return_to="start")         (guardian included)
    /nao/trajectory        → {"id", "names", "angles", "times", "start", "relax_end"}
    /nao/trajectory_state  ← playing | done | stopped | refused | error
    /nao/emotion_gesture_state → the state of each gesture and the version in service,
                                 plus owns_body / at_rest for the NAO driver

After a gesture the robot stays stiff in the start pose; if it receives no new
gesture for GESTURE_REST_DELAY seconds, the drone sends the return to rest
(validated by the guardian) and the robot relaxes at the end.

    /nao/emotion_gesture_control ← {"op": "reload"}: rereads emotion_gesture.env
                                 ← {"op": "speech_offset", "ms": 150}: speech /
                                   gesture offset, live (until the next reload)

Synchronisation with speech (gesture and sentence of the same LLM answer, linked
by body_gateway with a "sync" id):

    /nao/emotion_gesture   ← {..., "sync": {"id", "speech_s", "speech_s_first"}}
    /speech_gesture_sync   ← {"kind": "speech", "sync", "speech_s"}  (measured MP3 duration)
    1. the gesture is generated, then the service waits for the measured sentence
       duration (GESTURE_SPEECH_WAIT_S at most, otherwise body_gateway's estimate);
    2. the expressive part (start → gesture → back to start) is stretched or
       tightened to last as long as the speech (GESTURE_SPEECH_SCALE_MIN/MAX;
       tightened only if the guardian still accepts it);
    3. /speech_gesture_sync → {"kind": "go", "sync", "at", "in_s"}: the drone's speaker
       (static_drone.py) and the OBS voice (obs_agent.py, which uses `in_s`, the
       delay from now, since its clock is not the drone's) say the sentence at
       `at` = start of the expressive part
       + GESTURE_SPEECH_OFFSET_MS. Negative offset: speech starts before, the
       gesture is sent that much later if needed.

Run:
    python3 emotion_gesture.py
"""

import hashlib
import importlib.util
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from static_drone import Rosbridge, _env, _env_float, load_dotenv  # noqa: E402

log = logging.getLogger("emotion_gesture")

GENERATOR_FILES = ("generator/nao_gesture.py", "generator/nao_gesture.json")
RAW_URL = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
STRING = "std_msgs/String"
POSE_TIMEOUT_S = 2.0      # wait for /nao/joint_angles
END_MARGIN_S = 1.0        # margin after a gesture's planned end before considering it finished
CHECK_EMOTIONS_INTENSITY = 0.5
SYNC_TOPIC = "/speech_gesture_sync"
MAX_OFFSET_S = 10.0       # décalage parole / geste accepté, dans un sens ou dans l'autre


class GestureError(RuntimeError):
    pass


# ======================================================================
# Configuration
# ======================================================================

def read_settings():
    """Rereads emotion_gesture.env (pinned by hand), then .env (rosbridge connection)."""
    for key in ("GESTURE_DATASET_REPO", "GESTURE_DATASET_REF", "GESTURE_CACHE_DIR",
                "GESTURE_REST_DELAY", "GESTURE_RATE_HZ", "GESTURE_SPEECH_OFFSET_MS",
                "GESTURE_MATCH_SPEECH", "GESTURE_SPEECH_SCALE_MIN", "GESTURE_SPEECH_SCALE_MAX",
                "GESTURE_SPEECH_WAIT_S", "GESTURE_REFLEX", "GESTURE_REFLEX_SOUND",
                "GESTURE_REFLEX_MOTION", "GESTURE_REFLEX_COOLDOWN_S",
                "GESTURE_REFLEX_AZIMUTH_OFFSET_DEG", "GESTURE_REFLEX_NAO_ID",
                "GESTURE_LATENT_TEMPERATURE"):
        os.environ.pop(key, None)   # a reload rereads the file, not the old value
    load_dotenv(os.environ.get("GESTURE_ENV_FILE") or os.path.join(HERE, "emotion_gesture.env"))
    load_dotenv(os.path.join(HERE, ".env"))
    ref = _env("GESTURE_DATASET_REF")
    if not ref:
        raise GestureError("GESTURE_DATASET_REF manquant dans emotion_gesture.env (tag ou commit)")
    if not re.match(r"^[A-Za-z0-9._/-]+$", ref) or ".." in ref:
        raise GestureError("GESTURE_DATASET_REF invalide : {}".format(ref))
    return {
        "repo": _env("GESTURE_DATASET_REPO", "Monolithoid/nao-emotion-motion-dataset"),
        "ref": ref,
        "cache_dir": os.path.expanduser(_env("GESTURE_CACHE_DIR", "~/.cache/nao-gesture")),
        "rest_delay": _env_float("GESTURE_REST_DELAY", 3.0),
        "rate_hz": _env_float("GESTURE_RATE_HZ", 25.0),
        # CVAE generator: temperature of the latent draw (None = the generator file's default)
        "latent_temperature": parse_temperature(_env("GESTURE_LATENT_TEMPERATURE", "")),
        # Synchronisation avec la parole
        "speech_offset_ms": _clamp_offset_ms(_env_float("GESTURE_SPEECH_OFFSET_MS", 0.0)),
        "match_speech": _env("GESTURE_MATCH_SPEECH", "1").lower() not in ("0", "false", "no", "non", "off"),
        "scale_min": _env_float("GESTURE_SPEECH_SCALE_MIN", 0.7),
        "scale_max": _env_float("GESTURE_SPEECH_SCALE_MAX", 3.0),
        "speech_wait_s": _env_float("GESTURE_SPEECH_WAIT_S", 3.0),
        # Reflex: reaction to what the drone perceives, without the LLM
        "reflex": _env("GESTURE_REFLEX", "1").lower() not in ("0", "false", "no", "non", "off"),
        "reflex_gestures": {"sound": parse_reflex(_env("GESTURE_REFLEX_SOUND", "surprise:0.3")),
                            "motion": parse_reflex(_env("GESTURE_REFLEX_MOTION", "calm:0.3"))},
        "reflex_cooldown_s": _env_float("GESTURE_REFLEX_COOLDOWN_S", 20.0),
        "reflex_azimuth_offset_deg": _env_float("GESTURE_REFLEX_AZIMUTH_OFFSET_DEG", 0.0),
        "nao_id": _env("GESTURE_REFLEX_NAO_ID", "nao_01"),
        "drone_id": _env("DRONE_ID", "static_01"),
    }


def parse_reflex(spec):
    """"surprise:0.3" -> ("surprise", 0.3); empty or "0"/"off" -> None (no reflex)."""
    spec = (spec or "").strip()
    if not spec or spec.lower() in ("0", "off", "none", "non"):
        return None
    emotion, _, intensity = spec.partition(":")
    try:
        value = float(intensity) if intensity else 0.3
    except ValueError:
        value = 0.3
    return emotion.strip(), max(0.0, min(1.0, value))


def parse_temperature(spec):
    """"0.8" -> 0.8; empty or invalid -> None (the generator file's default)."""
    try:
        value = float((spec or "").strip())
    except ValueError:
        return None
    return value if value >= 0 and value == value and value != float("inf") else None


def generation_tries(gen, event, temperature=None, rng=None):
    """Successive generate() arguments for one gesture, the first accepted is played.

    CVAE generator (`gen.latent_dims` not empty): a new latent code at each gesture, so
    the same emotion and intensity give a different learned gesture each time; if the
    guardian refuses it, the same z without the event, another z, then the central
    gesture (z = 0). Other generators: the learned gesture, without the event, then a
    small variation (docs/generator-integration.md §7)."""
    rng = rng or random.SystemRandom()
    if getattr(gen, "latent_dims", None):
        s1, s2 = rng.randrange(1 << 30), rng.randrange(1 << 30)
        t = {} if temperature is None else {"temperature": float(temperature)}
        tries = [dict(t, event=event, seed=s1)]
        if event:
            tries.append(dict(t, event=None, seed=s1))
        tries += [dict(t, event=event, seed=s2), {"event": event, "temperature": 0.0}]
        return tries
    tries = [{"event": event}]
    if event:
        tries.append({"event": None})
    return tries + [{"event": event, "variation": 0.05, "seed": 1}]


def reflex_event(azimuth_deg, offset_deg=0.0):
    """Drone angle (positive to the right) -> generator event in the NAO torso frame
    (positive to the left), like body_rules.gesture_event; None without a direction."""
    if azimuth_deg is None:
        return None
    az = max(-90.0, min(90.0, -float(azimuth_deg) + float(offset_deg)))
    return {"azimuth_deg": round(az, 1), "elevation_deg": 0.0}


def _clamp_offset_ms(ms):
    return max(-MAX_OFFSET_S * 1000.0, min(MAX_OFFSET_S * 1000.0, float(ms)))


# ======================================================================
# Durée du geste calée sur la parole
# ======================================================================

EXPRESSIVE = ("start_to_gesture", "gesture_to_start")


def expressive_window(g):
    """(début, fin) en s de la partie expressive : départ → geste → retour au départ.

    Sans segments (ancien générateur), tout le mouvement."""
    segs = dict((name, (float(a), float(b))) for name, a, b in (getattr(g, "segments", None) or []))
    if all(n in segs for n in EXPRESSIVE):
        return segs[EXPRESSIVE[0]][0], segs[EXPRESSIVE[1]][1]
    if "gesture" in segs:
        return segs["gesture"]
    return float(g.times[0]), float(g.times[-1])


def warp_times(times, a, b, scale):
    """Étire (scale > 1) ou resserre l'intervalle [a, b] ; ce qui suit est décalé d'autant."""
    out = []
    for t in times:
        if t <= a:
            out.append(t)
        elif t <= b:
            out.append(a + (t - a) * scale)
        else:
            out.append(t + (b - a) * (scale - 1.0))
    return out


def fit_to_speech(gen, g, speech_s, scale_min=0.7, scale_max=3.0):
    """Cale la partie expressive du geste `g` sur `speech_s` secondes de parole.

    Étirer ne fait que baisser vitesses et accélérations : toujours accepté.
    Resserrer les augmente : on garde le resserrement le plus fort que le guardian
    accepte encore sur la partie expressive (souvent aucun : le générateur a déjà
    ralenti le geste jusqu'aux limites).  Renvoie le facteur appliqué (1.0 =
    inchangé) et modifie `g` (times, segments)."""
    a, b = expressive_window(g)
    if not speech_s or speech_s <= 0 or b - a <= 0:
        return 1.0
    scale = max(float(scale_min), min(float(scale_max), float(speech_s) / (b - a)))
    if abs(scale - 1.0) < 0.02:
        return 1.0
    if scale < 1.0:
        check = getattr(gen.guardian, "check_trajectory", None)
        if check is None:
            return 1.0
        idx = [i for i, t in enumerate(g.times) if a - 1e-9 <= t <= b + 1e-9]
        rows = [list(g.positions[i]) for i in idx]

        def accepted(sc):
            tw = warp_times([float(g.times[i]) for i in idx], a, b, sc)
            return check([t - tw[0] for t in tw], list(g.joints), rows, check_duration=False).ok

        if not accepted(scale):
            ok, bad = 1.0, scale            # recherche du resserrement accepté le plus fort
            for _ in range(6):
                mid = (ok + bad) / 2.0
                if accepted(mid):
                    ok = mid
                else:
                    bad = mid
            if ok > 0.98:
                return 1.0
            scale = ok
    times = warp_times([float(t) for t in g.times], a, b, scale)
    g.times = times
    if getattr(g, "segments", None):
        g.segments = [(name, round(warp_times([x], a, b, scale)[0], 4),
                       round(warp_times([y], a, b, scale)[0], 4)) for name, x, y in g.segments]
    return round(scale, 3)



# ======================================================================
# Fetching the generator, at the pinned version
# ======================================================================

def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "nao-emotion-gesture"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_generator(repo, ref, cache_dir, get=_http_get):
    """Local folder containing nao_gesture.py and nao_gesture.json at version `ref`.

    The files are checked (sha256 from dataset.json → files), then kept in the
    cache, one copy per version: a version already seen needs no network.
    """
    folder = os.path.join(cache_dir, re.sub(r"[^A-Za-z0-9._-]", "_", ref))
    marker = os.path.join(folder, "dataset.json")
    if all(os.path.isfile(os.path.join(folder, os.path.basename(p))) for p in GENERATOR_FILES) \
            and os.path.isfile(marker):
        return folder
    raw = get(RAW_URL.format(repo=repo, ref=ref, path="dataset.json"))
    manifest = json.loads(raw.decode("utf-8"))
    files = manifest.get("files") or {}
    blobs = {}
    for path in GENERATOR_FILES:
        if path not in files:
            raise GestureError("{} absent de dataset.json à {}".format(path, ref))
        data = get(RAW_URL.format(repo=repo, ref=ref, path=path))
        digest = hashlib.sha256(data).hexdigest()
        if digest != files[path]:
            raise GestureError("{} : sha256 différent de dataset.json à {}".format(path, ref))
        blobs[path] = data
    tmp = folder + ".tmp-{}".format(os.getpid())
    os.makedirs(tmp, exist_ok=True)
    for path, data in blobs.items():
        with open(os.path.join(tmp, os.path.basename(path)), "wb") as f:
            f.write(data)
    with open(os.path.join(tmp, "dataset.json"), "wb") as f:
        f.write(raw)
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            os.remove(os.path.join(folder, name))
        os.rmdir(folder)
    os.rename(tmp, folder)
    return folder


def load_library(folder):
    """Loads nao_gesture.py from this folder, under a module name specific to the version."""
    name = "nao_gesture_" + hashlib.sha1(folder.encode("utf-8")).hexdigest()[:10]
    spec = importlib.util.spec_from_file_location(name, os.path.join(folder, "nao_gesture.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_generator(lib, gen, profile=None, tolerance=0.05):
    """Checks before putting into service: (blocking errors, warnings, acceptance rate)."""
    errors, warnings = [], []
    if not gen.emotions:
        errors.append("aucune émotion")
    for name in ("arms", "head"):
        if name not in gen.models:
            warnings.append("pas de modèle {}".format(name))
    start = gen.guardian.pose("start")
    joints = gen.groups["arms"] + gen.groups["head"] + [j for j in gen.groups.get("hands", [])
                                                         if "hands" in gen.models]
    if any(j not in start for j in joints):
        errors.append("pose de départ incomplète")
        return errors, warnings, 0.0
    # Poses of the robot's motor profile (control room): they must copy those of the calibration.
    for chain in ((profile or {}).get("chains") or {}).values():
        for pose_name in ("rest", "start"):
            ref_pose = gen.guardian.pose(pose_name)
            for j, v in (chain.get(pose_name) or {}).items():
                if j in ref_pose and abs(float(v) - float(ref_pose[j])) > tolerance:
                    warnings.append("profil moteur : {} {} à {:.2f} rad de la calibration".format(
                        pose_name, j, abs(float(v) - float(ref_pose[j]))))
    pose = dict((j, float(start[j])) for j in joints)
    ok = 0
    for emotion in gen.emotions:
        g = gen.generate(emotion, CHECK_EMOTIONS_INTENSITY, current_pose=pose, return_to="start")
        ok += bool(g.ok)
    rate = ok / float(len(gen.emotions)) if gen.emotions else 0.0
    if ok == 0:
        errors.append("aucun geste de contrôle accepté par le guardian")
    return errors, warnings, rate


# ======================================================================
# Service
# ======================================================================

class GestureService:
    """Receives the requested gestures, generates them from the measured posture, sends them."""

    def __init__(self, bridge, settings, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.bridge = bridge
        self.settings = settings
        self.clock = clock
        self.wall = wall                # heure partagée avec static_drone.py (même machine)
        self.sleep = sleep
        self._speech = {}               # sync -> (durée mesurée de la phrase ou None, reçue à)
        self._speech_cv = threading.Condition()
        self.lib = self.gen = None
        self.version = None
        self.profile = None
        self._lock = threading.Lock()
        self._pending = None            # last pending request (older ones are replaced)
        self._wake = threading.Event()
        self._angles = {}               # id -> angles received
        self._angles_cv = threading.Condition()
        self._playing = None            # {"id", "until"}
        self._at_start = False          # robot left stiff in the start pose
        self._rested = True             # robot at rest and relaxed (last return to rest finished)
        self._last_end = 0.0
        self._last_request = -1e9        # last gesture asked by the LLM or the control room
        self._last_reflex = -1e9
        self._stop = threading.Event()

    # -- topics -----------------------------------------------------------

    def setup(self):
        b = self.bridge
        for topic in ("/nao/read_angles", "/nao/trajectory", "/nao/emotion_gesture_state", SYNC_TOPIC):
            b.advertise(topic, STRING)
        b.subscribe(SYNC_TOPIC, STRING, lambda m: self.on_sync(_data(m)))
        b.subscribe("/nao/emotion_gesture", STRING, lambda m: self.on_request(_data(m)))
        b.subscribe("/nao/emotion_gesture_control", STRING, lambda m: self.on_control(_data(m)))
        b.subscribe("/nao/joint_angles", STRING, lambda m: self.on_angles(_data(m)))
        b.subscribe("/nao/trajectory_state", STRING, lambda m: self.on_trajectory_state(_data(m)))
        b.subscribe("/nao/capabilities", STRING, lambda m: self.on_capabilities(_data(m)))
        # Reflex: the drone's perceptions, gestures asked through the NAO driver
        # (heat, posture and walk checks), never straight to the robot.
        b.advertise("/nao/stop", "std_msgs/Empty")
        b.advertise(self._driver_topic(), STRING)
        b.subscribe("/drones/{}/event".format(self.settings.get("drone_id", "static_01")), STRING,
                    lambda m: self.on_event(_data(m)))

    def _driver_topic(self):
        return "/bodies/{}/actuators/gesture".format(self.settings.get("nao_id", "nao_01"))

    def publish(self, topic, payload):
        self.bridge.publish(topic, {"data": json.dumps(payload)})

    def state(self, gid, state, **fields):
        v = self.version or {}
        info = v.get("info") or {}
        short = {"ref": v.get("ref"), "calibration": (info.get("calibration") or {}).get("calibration_version"),
                 "runs": dict((k, (m or {}).get("run")) for k, m in (info.get("models") or {}).items()),
                 "emotions": info.get("emotions") or []}
        full = state in ("version_loaded", "version_refused")
        # owns_body: the drone holds the head and the arms (gesture in progress, or robot
        # stiff in the start pose while waiting for the return to rest). The NAO driver
        # sends them nothing while this is true.
        msg = dict(fields, id=gid, state=state, version=self.version if full else short,
                   owns_body=self._playing is not None or self._at_start, at_rest=self._rested,
                   speech_offset_ms=self.settings.get("speech_offset_ms", 0.0))
        log.info("Gesture %s: %s %s", gid, state, fields.get("reason", ""))
        self.publish("/nao/emotion_gesture_state", msg)

    # -- generator version ----------------------------------------------

    def load(self, fetch=fetch_generator):
        """Puts the pinned version into service; keeps the old one if the new one fails."""
        s = self.settings
        try:
            folder = fetch(s["repo"], s["ref"], s["cache_dir"])
            lib = load_library(folder)
            gen = lib.GestureGenerator.load(os.path.join(folder, "nao_gesture.json"))
            # A generator exported before nao-emotion-motion#62 validates the raw gesture,
            # without the step that makes it playable: it refuses almost everything. Refused here, with
            # the reason, rather than seeing each gesture refused one by one.
            if not hasattr(gen, "adapt_generated"):
                raise GestureError("générateur trop ancien : il ne rend pas les gestes jouables "
                                   "(exporter le dataset avec nao-emotion-motion#62 ou plus récent)")
            errors, warnings, rate = check_generator(lib, gen, self.profile)
        except Exception as exc:
            errors, warnings, rate, gen, lib = [str(exc)], [], 0.0, None, None
        version = {"repo": s["repo"], "ref": s["ref"], "acceptance": round(rate, 2),
                   "warnings": warnings}
        if gen is not None:
            version["info"] = gen.info
        if errors:
            self.state(None, "version_refused", reason="; ".join(errors), candidate=version)
            return False
        with self._lock:
            self.lib, self.gen, self.version = lib, gen, version
        self.state(None, "version_loaded")
        return True

    def on_control(self, payload):
        if payload.get("op") == "speech_offset":
            try:
                ms = _clamp_offset_ms(payload.get("ms"))
            except (TypeError, ValueError):
                return
            self.settings = dict(self.settings, speech_offset_ms=ms)
            self.state(None, "speech_offset", speech_offset_ms=ms)
            return
        if payload.get("op") == "reload":
            try:
                self.settings = read_settings()
            except GestureError as exc:
                self.state(None, "version_refused", reason=str(exc))
                return
            threading.Thread(target=self.load, daemon=True).start()

    def on_capabilities(self, payload):
        if isinstance(payload.get("motor_profile"), dict) or payload.get("motor_profile") is None:
            self.profile = payload.get("motor_profile")

    # -- demandes --------------------------------------------------------------

    def on_request(self, payload):
        gid = str(payload.get("id") or uuid.uuid4().hex[:8])
        req = dict(payload, id=gid)
        with self._lock:
            if req.get("reflex"):
                # A reflex never replaces nor delays a gesture asked by the LLM.
                ignored = ((self._pending is not None and not self._pending.get("reflex"))
                           or (self._playing is not None and not self._playing.get("reflex")))
            else:
                ignored = False
                self._last_request = self.clock()
            old = None
            if not ignored:
                old, self._pending = self._pending, req
        if ignored:
            self.state(gid, "ignored", reason="réflexe pendant un geste demandé", reflex=True)
            return
        if old is not None:
            self.state(old["id"], "replaced")
            self.release_speech(old, "geste remplacé")
        self._wake.set()

    def on_angles(self, payload):
        with self._angles_cv:
            self._angles[payload.get("id")] = payload.get("angles") or {}
            self._angles_cv.notify_all()

    def on_trajectory_state(self, payload):
        with self._lock:
            playing = self._playing
            if playing is None or payload.get("id") != playing["id"]:
                return
            st = payload.get("state")
            if st == "playing" or (playing.get("to_rest") and st in ("stopped", "error")):
                self._rested = False
            elif st == "done" and playing.get("to_rest"):
                self._rested = True
            if st in ("done", "stopped", "refused", "error"):
                self._playing = None
                self._last_end = self.clock()
                if st != "done" or playing.get("to_rest"):
                    self._at_start = False
        self.state(payload.get("id"), "robot_" + str(payload.get("state")), reason=payload.get("reason"))
        self._wake.set()

    def measure(self, timeout=POSE_TIMEOUT_S):
        rid = uuid.uuid4().hex[:8]
        self.publish("/nao/read_angles", {"id": rid})
        deadline = time.monotonic() + timeout
        with self._angles_cv:
            while rid not in self._angles:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise GestureError("posture du robot non reçue")
                self._angles_cv.wait(left)
            return self._angles.pop(rid)

    def _busy(self):
        p = self._playing
        if p is None:
            return False
        if self.clock() > p["until"]:      # end state lost: do not block forever
            self._playing = None
            return False
        return True

    def on_event(self, payload):
        """Something happens in front of the NAO (drone: sound, motion): short gesture by reflex.

        Only when the robot rests and nothing was asked or played for
        GESTURE_REFLEX_COOLDOWN_S (the robot's own gestures move in the camera)."""
        s = self.settings
        spec = (s.get("reflex_gestures") or {}).get(payload.get("kind"))
        if not s.get("reflex") or spec is None or self.gen is None:
            return
        emotion, intensity = spec
        if emotion not in (self.gen.emotions or []):
            return
        now, cooldown = self.clock(), float(s.get("reflex_cooldown_s", 20.0))
        with self._lock:
            idle = (self._playing is None and self._pending is None and self._rested
                    and min(now - self._last_end, now - self._last_request,
                            now - self._last_reflex) >= cooldown)
            if not idle:
                return
            self._last_reflex = now
        order = {"emotion": emotion, "intensity": intensity, "reflex": True}
        event = reflex_event(payload.get("azimuth_deg"), s.get("reflex_azimuth_offset_deg", 0.0))
        if event:
            order["event"] = event
        log.info("Réflexe (%s) : %s %.1f, %s", payload.get("kind"), emotion, intensity, event)
        self.publish(self._driver_topic(), order)

    def step(self):
        """One iteration: plays the pending request, or the due return to rest."""
        with self._lock:
            p = self._playing
            if self._busy():
                # A gesture asked by the LLM stops a reflex in progress, then starts
                # from the measured posture.
                if (p.get("reflex") and not p.get("stopping") and self._pending is not None
                        and not self._pending.get("reflex")):
                    p["stopping"] = True
                    stop = True
                else:
                    return
            else:
                stop = False
        if stop:
            self.bridge.publish("/nao/stop", {})
            return
        with self._lock:
            if self._busy():
                return
            req, self._pending = self._pending, None
            rest_due = (req is None and self._at_start
                        and self.clock() - self._last_end >= self.settings["rest_delay"])
        if req is not None:
            self.play(req)
        elif rest_due:
            self.back_to_rest()

    def play(self, req):
        gid = req["id"]
        gen = self.gen
        if gen is None:
            self.state(gid, "refused", reason="aucun générateur en service")
            self.release_speech(req, "aucun générateur")
            return
        try:
            pose = self.measure()
            joints = self._joints(gen)
            missing = [j for j in joints if j not in pose]
            if missing:
                raise GestureError("angles manquants : " + ", ".join(missing))
            pose = dict((j, float(pose[j])) for j in joints)
            from_rest = self._is_at_rest(gen, pose)
            emotion, intensity = req.get("emotion"), float(req.get("intensity", 0.5))
            event = req.get("event") or None
            # A new learned gesture each time (CVAE); refused: the fallbacks of
            # generation_tries, otherwise nothing is played.
            tries = generation_tries(gen, event, self.settings.get("latent_temperature"))
            for i, kw in enumerate(tries):
                g = gen.generate(emotion, intensity, current_pose=pose, from_rest=from_rest,
                                 return_to="start", **kw)
                if g.ok:
                    break
        except Exception as exc:
            self.state(gid, "refused", reason=str(exc))
            self.release_speech(req, "geste refusé")
            return
        if not g.ok:
            self.state(gid, "refused", reason=g.report.summary())
            self.release_speech(req, "geste refusé")
            return
        sync = self._sync(req)
        timing = {}
        if sync:
            speech_s = self.speech_seconds(sync)
            scale = 1.0
            if self.settings.get("match_speech", True):
                scale = fit_to_speech(gen, g, speech_s, self.settings.get("scale_min", 0.7),
                                      self.settings.get("scale_max", 3.0))
            timing = {"sync": sync["id"], "speech_s": speech_s, "time_scale": scale,
                      "speech_offset_ms": self.settings.get("speech_offset_ms", 0.0)}
        self._send(gid, g, pose, relax_end=False, to_rest=False, sync=sync, timing=timing,
                   reflex=bool(req.get("reflex")),
                   info={"emotion": req.get("emotion"), "intensity": req.get("intensity"),
                         "event": req.get("event"), "notes": g.notes,
                         "adaptation": getattr(g, "adaptation", None),
                         "latent": (getattr(g, "inputs", None) or {}).get("latent"),
                         "temperature": (getattr(g, "inputs", None) or {}).get("temperature"),
                         "retry": _retry_label(i, kw, event)})
        with self._lock:
            self._at_start = True

    def back_to_rest(self):
        """Return to rest after a delay without a gesture: validated by the guardian, then relaxed."""
        gen, lib = self.gen, self.lib
        gid = "rest-" + uuid.uuid4().hex[:6]
        try:
            pose = self.measure()
            joints = [j for j in self._joints(gen) if j in pose]
            rest = gen.guardian.pose("rest")
            joints = [j for j in joints if j in rest]
            q0 = [float(pose[j]) for j in joints]
            q1 = [float(rest[j]) for j in joints]
            g_ = gen.guardian
            seg = lib._min_jerk(q0, q1, g_.rest_velocity * 0.8, g_.max_acceleration * 0.25)
            rep = g_.check_rest_transition(seg["t"], joints, seg["q"],
                                           start_state=dict(zip(joints, q0)))
        except Exception as exc:
            with self._lock:
                self._at_start = False
            self.state(gid, "refused", reason=str(exc))
            return
        if not rep.ok:
            with self._lock:
                self._at_start = False
            self.state(gid, "refused", reason=rep.summary())
            return
        traj = lib.Gesture(joints, seg["t"], seg["q"], rep, rep, [("start_to_rest", 0.0, seg["t"][-1])],
                           {}, [], {})
        self._send(gid, traj, dict(zip(joints, q0)), relax_end=True, to_rest=True, info={"rest": True})

    def _send(self, gid, g, pose, relax_end, to_rest, info, sync=None, timing=None, reflex=False):
        names, angles, times = g.angle_interpolation(rate_hz=self.settings["rate_hz"])
        if sync:
            # Début de la partie expressive (après le passage à la pose de départ),
            # plus le décalage réglé : c'est là que la phrase doit partir.  Décalage
            # négatif plus grand que ce passage : le geste attend un peu.
            lead = expressive_window(g)[0] - float(g.times[0])
            offset = float(self.settings.get("speech_offset_ms", 0.0)) / 1000.0
            hold = max(0.0, -(lead + offset))
            # `at`: this machine's clock (static_drone.py, same machine);
            # `in_s`: delay from now, for the OBS voice on the VPS (another clock).
            self.publish(SYNC_TOPIC, {"kind": "go", "sync": sync["id"],
                                      "at": round(self.wall() + hold + lead + offset, 3),
                                      "in_s": round(hold + lead + offset, 3)})
            if hold > 0:
                self.sleep(hold)          # la parole part tout de suite, le geste un peu après
            info = dict(info, **(timing or {}))
            info["speech_after_s"] = round(lead + offset, 3)   # par rapport à l'envoi du geste
        with self._lock:
            self._playing = {"id": gid, "until": self.clock() + g.duration + 10 * END_MARGIN_S,
                             "to_rest": to_rest, "reflex": reflex}
        self.publish("/nao/trajectory", {
            "id": gid, "names": names, "angles": angles, "times": times,
            "start": dict((j, round(v, 5)) for j, v in pose.items() if j in names),
            "relax_end": relax_end})
        self.state(gid, "sent", duration_s=round(g.duration, 2), **info)

    # -- synchronisation avec la parole ---------------------------------------

    @staticmethod
    def _sync(req):
        sync = req.get("sync")
        return sync if isinstance(sync, dict) and sync.get("id") else None

    def on_sync(self, payload):
        """Durée mesurée de la phrase liée à un geste (static_driver_node, sur le VPS)."""
        if payload.get("kind") != "speech" or not payload.get("sync"):
            return
        try:
            seconds = float(payload["speech_s"]) if payload.get("speech_s") is not None else None
        except (TypeError, ValueError):
            seconds = None
        now = self.clock()
        with self._speech_cv:
            self._speech[str(payload["sync"])] = (seconds, now)
            for k in [k for k, (_, t) in self._speech.items() if now - t > 120.0]:
                del self._speech[k]
            self._speech_cv.notify_all()

    def speech_seconds(self, sync):
        """Durée de toute la parole liée : la première phrase mesurée (attendue au plus
        GESTURE_SPEECH_WAIT_S), les suivantes estimées par body_gateway."""
        estimate = float(sync.get("speech_s") or 0.0)
        first_est = float(sync.get("speech_s_first") or estimate)
        deadline = time.monotonic() + float(self.settings.get("speech_wait_s", 3.0))
        with self._speech_cv:
            while sync["id"] not in self._speech:
                left = deadline - time.monotonic()
                if left <= 0:
                    return round(estimate, 3)
                self._speech_cv.wait(left)
            measured, _ = self._speech.pop(sync["id"])
        if measured is None:
            return round(estimate, 3)
        return round(measured + max(0.0, estimate - first_est), 3)

    def release_speech(self, req, reason):
        """Le geste ne partira pas : la phrase qui l'attend est dite tout de suite."""
        sync = self._sync(req or {})
        if sync:
            self.publish(SYNC_TOPIC, {"kind": "go", "sync": sync["id"], "at": 0, "in_s": 0,
                                      "reason": reason})

    def _joints(self, gen):
        return gen.groups["arms"] + gen.groups["head"] + [j for j in gen.groups.get("hands", [])
                                                          if "hands" in gen.models]

    @staticmethod
    def _is_at_rest(gen, pose):
        rest, tol = gen.guardian.pose("rest"), 2 * gen.guardian.start_tolerance
        hands = set(gen.groups.get("hands") or [])
        return all(abs(pose[j] - rest[j]) <= tol for j in pose if j in rest and j not in hands)

    def run(self, announce_every=30.0):
        last = 0.0
        while not self._stop.is_set():
            self._wake.wait(0.2)
            self._wake.clear()
            try:
                self.step()
                # version in service repeated regularly (control room opened later, reconnection)
                if time.monotonic() - last >= announce_every and self.bridge.connected:
                    last = time.monotonic()
                    self.state(None, "version_loaded" if self.gen is not None else "no_version")
            except Exception:
                log.exception("Error in the gesture loop")

    def stop(self):
        self._stop.set()
        self._wake.set()


def _retry_label(i, kw, event):
    """Why the played gesture is not the first try (None: it is)."""
    if i == 0:
        return None
    if event and kw.get("event") is None:
        return "sans événement"
    if kw.get("temperature") == 0.0:
        return "geste central"
    return "variation" if kw.get("variation") else "autre tirage"


def _data(msg):
    try:
        out = json.loads(msg.get("data") or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = read_settings()
    url, token = _env("ROSBRIDGE_URL"), _env("ROSBRIDGE_TOKEN")
    if not url and _env("DOMAIN"):
        url = "wss://rosbridge.{}".format(_env("DOMAIN"))
    if not url:
        raise SystemExit("ROSBRIDGE_URL or DOMAIN missing from .env")
    bridge = Rosbridge(url, token, _env_float("RECONNECT_MAX_S", 30))
    service = GestureService(bridge, settings)
    service.setup()
    if not service.load():
        log.error("Version %s refused: no gesture until a valid version is loaded",
                  settings["ref"])
    threading.Thread(target=service.run, daemon=True).start()
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        bridge.stop()


if __name__ == "__main__":
    main()
