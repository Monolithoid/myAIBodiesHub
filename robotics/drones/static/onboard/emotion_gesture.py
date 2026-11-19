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
    /nao/emotion_gesture_state → l'état de chaque geste et la version en service,
                                 plus owns_body / at_rest pour le driver NAO

Après un geste le robot reste raide à la pose de départ ; s'il ne reçoit pas de
nouveau geste pendant GESTURE_REST_DELAY secondes, le drone envoie le retour au
repos (validé par le guardian) et le robot se relâche à la fin.

    /nao/emotion_gesture_control ← {"op": "reload"} : relit emotion_gesture.env
                                 ← {"op": "speech_offset", "ms": 150} : décalage
                                   parole / geste, à chaud (jusqu'au prochain reload)

Synchronisation avec la parole (geste et phrase d'une même réponse du LLM,
liés par body_gateway avec un id "sync") :

    /nao/emotion_gesture   ← {..., "sync": {"id", "speech_s", "speech_s_first"}}
    /speech_gesture_sync   ← {"kind": "speech", "sync", "speech_s"}  (durée mesurée du MP3)
    1. le geste est généré, puis le service attend la durée mesurée de la phrase
       (GESTURE_SPEECH_WAIT_S au plus, sinon l'estimation de body_gateway) ;
    2. la partie expressive (départ → geste → retour au départ) est étirée ou
       resserrée pour durer autant que la parole (GESTURE_SPEECH_SCALE_MIN/MAX ;
       resserrée seulement si le guardian l'accepte encore) ;
    3. /speech_gesture_sync → {"kind": "go", "sync", "at"} : le haut-parleur du
       drone (static_drone.py) dit la phrase à `at` = début de la partie
       expressive + GESTURE_SPEECH_OFFSET_MS.  Décalage négatif : la parole part
       avant, le geste est envoyé plus tard d'autant si besoin.

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
SYNC_TOPIC = "/speech_gesture_sync"
MAX_OFFSET_S = 10.0       # décalage parole / geste accepté, dans un sens ou dans l'autre


class GestureError(RuntimeError):
    pass


# ======================================================================
# Configuration
# ======================================================================

def read_settings():
    """Relit emotion_gesture.env (fixé à la main) puis .env (connexion rosbridge)."""
    for key in ("GESTURE_DATASET_REPO", "GESTURE_DATASET_REF", "GESTURE_CACHE_DIR",
                "GESTURE_REST_DELAY", "GESTURE_RATE_HZ", "GESTURE_SPEECH_OFFSET_MS",
                "GESTURE_MATCH_SPEECH", "GESTURE_SPEECH_SCALE_MIN", "GESTURE_SPEECH_SCALE_MAX",
                "GESTURE_SPEECH_WAIT_S"):
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
        # Synchronisation avec la parole
        "speech_offset_ms": _clamp_offset_ms(_env_float("GESTURE_SPEECH_OFFSET_MS", 0.0)),
        "match_speech": _env("GESTURE_MATCH_SPEECH", "1").lower() not in ("0", "false", "no", "non", "off"),
        "scale_min": _env_float("GESTURE_SPEECH_SCALE_MIN", 0.7),
        "scale_max": _env_float("GESTURE_SPEECH_SCALE_MAX", 3.0),
        "speech_wait_s": _env_float("GESTURE_SPEECH_WAIT_S", 3.0),
    }


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
        self._pending = None            # dernière demande en attente (les plus anciennes sont remplacées)
        self._wake = threading.Event()
        self._angles = {}               # id -> angles reçus
        self._angles_cv = threading.Condition()
        self._playing = None            # {"id", "until"}
        self._at_start = False          # robot laissé raide à la pose de départ
        self._rested = True             # robot au repos et relâché (dernier retour au repos fini)
        self._last_end = 0.0
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

    def publish(self, topic, payload):
        self.bridge.publish(topic, {"data": json.dumps(payload)})

    def state(self, gid, state, **fields):
        v = self.version or {}
        info = v.get("info") or {}
        short = {"ref": v.get("ref"), "calibration": (info.get("calibration") or {}).get("calibration_version"),
                 "runs": dict((k, (m or {}).get("run")) for k, m in (info.get("models") or {}).items()),
                 "emotions": info.get("emotions") or []}
        full = state in ("version_loaded", "version_refused")
        # owns_body : le drone tient la tête et les bras (geste en cours, ou robot
        # raide à la pose de départ en attendant le retour au repos).  Le driver NAO
        # ne leur envoie rien tant que c'est vrai.
        msg = dict(fields, id=gid, state=state, version=self.version if full else short,
                   owns_body=self._playing is not None or self._at_start, at_rest=self._rested,
                   speech_offset_ms=self.settings.get("speech_offset_ms", 0.0))
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
            # Un générateur exporté avant nao-emotion-motion#62 valide le geste brut,
            # sans l'étape qui le rend jouable : il refuse presque tout.  Refusé ici, avec
            # la raison, plutôt que de voir chaque geste refusé un par un.
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
            old, self._pending = self._pending, req
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
            # Refusé : on réessaie sans l'événement, puis avec une petite variation
            # (conseil de docs/generator-integration.md §7) ; sinon rien n'est joué.
            tries = [(event, 0.0, None)] + ([(None, 0.0, None)] if event else []) + [(event, 0.05, 1)]
            for ev, variation, seed in tries:
                g = gen.generate(emotion, intensity, event=ev, current_pose=pose, from_rest=from_rest,
                                 return_to="start", variation=variation, seed=seed)
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
                   info={"emotion": req.get("emotion"), "intensity": req.get("intensity"),
                         "event": req.get("event"), "notes": g.notes,
                         "adaptation": getattr(g, "adaptation", None),
                         "retry": None if (ev, variation) == (event, 0.0) else
                         ("sans événement" if ev is None and event else "variation")})
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

    def _send(self, gid, g, pose, relax_end, to_rest, info, sync=None, timing=None):
        names, angles, times = g.angle_interpolation(rate_hz=self.settings["rate_hz"])
        if sync:
            # Début de la partie expressive (après le passage à la pose de départ),
            # plus le décalage réglé : c'est là que la phrase doit partir.  Décalage
            # négatif plus grand que ce passage : le geste attend un peu.
            lead = expressive_window(g)[0] - float(g.times[0])
            offset = float(self.settings.get("speech_offset_ms", 0.0)) / 1000.0
            hold = max(0.0, -(lead + offset))
            self.publish(SYNC_TOPIC, {"kind": "go", "sync": sync["id"],
                                      "at": round(self.wall() + hold + lead + offset, 3)})
            if hold > 0:
                self.sleep(hold)          # la parole part tout de suite, le geste un peu après
            info = dict(info, **(timing or {}))
            info["speech_after_s"] = round(lead + offset, 3)   # par rapport à l'envoi du geste
        with self._lock:
            self._playing = {"id": gid, "until": self.clock() + g.duration + 10 * END_MARGIN_S,
                             "to_rest": to_rest}
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
            self.publish(SYNC_TOPIC, {"kind": "go", "sync": sync["id"], "at": 0, "reason": reason})

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
