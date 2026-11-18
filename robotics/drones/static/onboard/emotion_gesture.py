#!/usr/bin/env python3
"""
emotion_gesture.py — Gestes appris du NAO, générés sur le drone statique (ADR 009).

Le générateur de nao-emotion-motion (`nao_gesture.py` + `nao_gesture.json`) est
pris dans le dépôt public nao-emotion-motion-dataset, à la version fixée à la
main dans `emotion_gesture.env` (GESTURE_DATASET_REF : tag ou commit). Aucune
mise à jour automatique : changer de modèle = changer cette ligne, puis
« recharger » depuis la régie ou redémarrer le service.

Chaîne d'un geste :

    /nao/emotion_gesture   ← {"id", "emotion", "intensity", "event"}  (driver NAO / régie)
    /nao/read_angles       → {"id"}                                   (au robot)
    /nao/joint_angles      ← {"id", "angles"}                         (posture mesurée)
    generate(..., current_pose, from_rest, return_to="start")         (guardian compris)
    /nao/trajectory        → {"id", "names", "angles", "times", "start", "relax_end"}
    /nao/trajectory_state  ← playing | done | stopped | refused | error
    /nao/emotion_gesture_state → l'état de chaque geste et la version en service

Après un geste le robot reste raide à la pose de départ ; s'il ne reçoit pas de
nouveau geste pendant GESTURE_REST_DELAY secondes, le drone envoie le retour au
repos (validé par le guardian) et le robot se relâche à la fin.

    /nao/emotion_gesture_control ← {"op": "reload"} : relit emotion_gesture.env

Lancement :
    python3 emotion_gesture.py
"""

import hashlib
import importlib.util
import json
import logging
import os
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
POSE_TIMEOUT_S = 2.0      # attente de /nao/joint_angles
END_MARGIN_S = 1.0        # marge après la fin prévue d'un geste avant de le croire fini
CHECK_EMOTIONS_INTENSITY = 0.5


class GestureError(RuntimeError):
    pass


# ======================================================================
# Configuration
# ======================================================================

def read_settings():
    """Relit emotion_gesture.env (fixé à la main) puis .env (connexion rosbridge)."""
    for key in ("GESTURE_DATASET_REPO", "GESTURE_DATASET_REF", "GESTURE_CACHE_DIR",
                "GESTURE_REST_DELAY", "GESTURE_RATE_HZ"):
        os.environ.pop(key, None)   # un rechargement relit le fichier, pas l'ancienne valeur
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
    }


# ======================================================================
# Récupération du générateur, à la version fixée
# ======================================================================

def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "nao-emotion-gesture"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_generator(repo, ref, cache_dir, get=_http_get):
    """Dossier local contenant nao_gesture.py et nao_gesture.json à la version `ref`.

    Les fichiers sont vérifiés (sha256 de dataset.json → files) puis gardés en
    cache, une copie par version : une version déjà vue ne demande pas de réseau.
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
    """Charge nao_gesture.py de ce dossier, sous un nom de module propre à la version."""
    name = "nao_gesture_" + hashlib.sha1(folder.encode("utf-8")).hexdigest()[:10]
    spec = importlib.util.spec_from_file_location(name, os.path.join(folder, "nao_gesture.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_generator(lib, gen, profile=None, tolerance=0.05):
    """Contrôles avant mise en service : (erreurs bloquantes, avertissements, taux d'acceptation)."""
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
    # Poses du profil moteur du robot (régie) : elles doivent reprendre celles de la calibration.
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
    """Reçoit les gestes demandés, les génère d'après la posture mesurée, les envoie."""

    def __init__(self, bridge, settings, clock=time.monotonic):
        self.bridge = bridge
        self.settings = settings
        self.clock = clock
        self.lib = self.gen = None
        self.version = None
        self.profile = None
        self._lock = threading.Lock()
        self._pending = None            # dernière demande en attente (les plus anciennes sont remplacées)
        self._wake = threading.Event()
        self._angles = {}               # id -> angles reçus
        self._angles_cv = threading.Condition()
        self._playing = None            # {"id", "until"}
        self._at_start = False          # robot laissé raide à la pose de départ
        self._last_end = 0.0
        self._stop = threading.Event()

    # -- topics -----------------------------------------------------------

    def setup(self):
        b = self.bridge
        for topic in ("/nao/read_angles", "/nao/trajectory", "/nao/emotion_gesture_state"):
            b.advertise(topic, STRING)
        b.subscribe("/nao/emotion_gesture", STRING, lambda m: self.on_request(_data(m)))
        b.subscribe("/nao/emotion_gesture_control", STRING, lambda m: self.on_control(_data(m)))
        b.subscribe("/nao/joint_angles", STRING, lambda m: self.on_angles(_data(m)))
        b.subscribe("/nao/trajectory_state", STRING, lambda m: self.on_trajectory_state(_data(m)))
        b.subscribe("/nao/capabilities", STRING, lambda m: self.on_capabilities(_data(m)))

    def publish(self, topic, payload):
        self.bridge.publish(topic, {"data": json.dumps(payload)})

    def state(self, gid, state, **fields):
        v = self.version or {}
        info = v.get("info") or {}
        short = {"ref": v.get("ref"), "calibration": (info.get("calibration") or {}).get("calibration_version"),
                 "runs": dict((k, (m or {}).get("run")) for k, m in (info.get("models") or {}).items())}
        full = state in ("version_loaded", "version_refused")
        msg = dict(fields, id=gid, state=state, version=self.version if full else short)
        log.info("Geste %s : %s %s", gid, state, fields.get("reason", ""))
        self.publish("/nao/emotion_gesture_state", msg)

    # -- version du générateur ----------------------------------------------

    def load(self, fetch=fetch_generator):
        """Met en service la version fixée ; garde l'ancienne si la nouvelle échoue."""
        s = self.settings
        try:
            folder = fetch(s["repo"], s["ref"], s["cache_dir"])
            lib = load_library(folder)
            gen = lib.GestureGenerator.load(os.path.join(folder, "nao_gesture.json"))
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
            if self._pending is not None:
                self.state(self._pending["id"], "replaced")
            self._pending = req
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
        if self.clock() > p["until"]:      # état de fin perdu : on ne bloque pas indéfiniment
            self._playing = None
            return False
        return True

    def step(self):
        """Une itération : joue la demande en attente, ou le retour au repos dû."""
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
            return
        try:
            pose = self.measure()
            joints = self._joints(gen)
            missing = [j for j in joints if j not in pose]
            if missing:
                raise GestureError("angles manquants : " + ", ".join(missing))
            pose = dict((j, float(pose[j])) for j in joints)
            g = gen.generate(req.get("emotion"), float(req.get("intensity", 0.5)),
                             event=req.get("event") or None, current_pose=pose,
                             from_rest=self._is_at_rest(gen, pose), return_to="start")
        except Exception as exc:
            self.state(gid, "refused", reason=str(exc))
            return
        if not g.ok:
            self.state(gid, "refused", reason=g.report.summary())
            return
        self._send(gid, g, pose, relax_end=False, to_rest=False,
                   info={"emotion": req.get("emotion"), "intensity": req.get("intensity"),
                         "event": req.get("event"), "notes": g.notes,
                         "adaptation": g.adaptation})
        with self._lock:
            self._at_start = True

    def back_to_rest(self):
        """Retour au repos après un délai sans geste : validé par le guardian, puis relâché."""
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

    def _send(self, gid, g, pose, relax_end, to_rest, info):
        names, angles, times = g.angle_interpolation(rate_hz=self.settings["rate_hz"])
        with self._lock:
            self._playing = {"id": gid, "until": self.clock() + g.duration + 10 * END_MARGIN_S,
                             "to_rest": to_rest}
        self.publish("/nao/trajectory", {
            "id": gid, "names": names, "angles": angles, "times": times,
            "start": dict((j, round(v, 5)) for j, v in pose.items() if j in names),
            "relax_end": relax_end})
        self.state(gid, "sent", duration_s=round(g.duration, 2), **info)

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
                # version en service rappelée régulièrement (régie ouverte après coup, reconnexion)
                if time.monotonic() - last >= announce_every and self.bridge.connected:
                    last = time.monotonic()
                    self.state(None, "version_loaded" if self.gen is not None else "no_version")
            except Exception:
                log.exception("Erreur dans la boucle des gestes")

    def stop(self):
        self._stop.set()
        self._wake.set()


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
        raise SystemExit("ROSBRIDGE_URL ou DOMAIN manquant dans .env")
    bridge = Rosbridge(url, token, _env_float("RECONNECT_MAX_S", 30))
    service = GestureService(bridge, settings)
    service.setup()
    if not service.load():
        log.error("Version %s refusée : aucun geste tant qu'une version valable n'est pas chargée",
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
