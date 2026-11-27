"""
body_rules.py — Règles de body_gateway sans dépendance ROS 2 (testables seules).

  - type d'un corps d'après son identifiant (nao_01 → nao, static_01 → static,
    obs_01 → obs)
  - table de routage : quel(s) corps exécute(nt) quel type d'action ; la parole
    part sur toutes les voix connectées (pièce et stream, ADR 005)
  - mot de réveil pour les corps qui envoient toute la parole entendue (static)
  - direction de la voix : texte pour le LLM et angle de tête du NAO

Angles du drone statique (ADR 004) : degrés, 0 = axe de la caméra,
positif = à droite.  HeadYaw du NAO : radians, positif = vers la gauche du robot.
"""

import math
import re
import threading
import time
import unicodedata

# ─────────────────────────────────────────────────────────────────────────────
# Body types and capabilities
# ─────────────────────────────────────────────────────────────────────────────

# Actions each body type can execute.  The NAO keeps "speech" so that it still
# talks when no static drone is connected.
CAPABILITIES: dict[str, set[str]] = {
    "static": {"speech"},
    "obs":    {"speech"},
    "nao":    {"speech", "motion", "leds", "move", "look", "gesture"},
}

# One line per body type for the LLM system prompt.
CAPABILITY_LABELS: dict[str, str] = {
    "static": "parler, entendre, voir (haut-parleur, micros, caméra de la pièce)",
    "obs":    "parler sur le stream (voix envoyée dans le mixer OBS)",
    "nao":    "gestes, tête, LEDs (motion, gesture, look, leds, move)",
}

# LLM_MOTION=0: the NAO only moves through learned gestures.
CAPABILITY_LABEL_NAO_GESTURES = "gestes appris, LEDs (gesture, leds)"
RAW_MOTION_ACTIONS = ("motion", "look")


def first_gesture_only(actions: list) -> list:
    """One answer, one emotion, one learned gesture: keeps the first "gesture" action.

    A second gesture of the same answer would replace the first one on the drone,
    and release the sentence before its gesture starts."""
    out, seen = [], False
    for a in actions:
        if isinstance(a, dict) and a.get("type") == "gesture":
            if seen:
                continue
            seen = True
        out.append(a)
    return out

# Bodies that only give NAO a voice: "speech" goes to every connected one.
VOICE_TYPES = ("static", "obs")

HEAD_YAW_LIMIT_RAD = 2.0857   # NAO HeadYaw hardware range (cf. nao_ws_bridge.py _JOINT_HW_LIMITS)


def body_type(body_id: str) -> str:
    """'static_01' → 'static', 'obs_01' → 'obs', anything else → 'nao' (every
    body was a NAO before ADR 004)."""
    for kind in VOICE_TYPES:
        if body_id.startswith(kind):
            return kind
    return "nao"


def is_voice(body_id: str) -> bool:
    return body_type(body_id) in VOICE_TYPES


def bodies_of_type(registry: list[str], kind: str) -> list[str]:
    return [b for b in registry if body_type(b) == kind]


def describe_bodies(registry: list[str], speaker: str, motion: bool = True) -> str:
    """Lines '- id : capacités' injected in the system prompt.

    A NAO that is the speaking body (no voice body connected) also gets "parler".
    """
    lines = []
    for b in registry:
        label = CAPABILITY_LABELS[body_type(b)]
        if body_type(b) == "nao" and not motion:
            label = CAPABILITY_LABEL_NAO_GESTURES
        if b == speaker and not is_voice(b):
            label = f"parler, {label}"
        lines.append(f"- {b} : {label}")
    return "\n".join(lines)


def speech_bodies(registry: list[str], connected: dict[str, bool], default: str) -> list[str]:
    """Bodies that should speak: every connected voice body, else [`default`]."""
    voices = [b for b in registry if is_voice(b) and connected.get(b, False)]
    return voices or [default]


def speech_body(registry: list[str], connected: dict[str, bool], default: str) -> str:
    """Body named as the speaker in the system prompt: first of speech_bodies()."""
    return speech_bodies(registry, connected, default)[0]


def route_action(action: dict, registry: list[str], connected: dict[str, bool]) -> str | None:
    """Return the body that must execute this action, or None if unknown body.

    1. "speech" → first connected voice body (route_targets() returns them all).
    2. The body named by the LLM if it has the capability.
    3. Otherwise the first registered body able to do it (voice → NAO for gestures).
    """
    targets = route_targets(action, registry, connected)
    return targets[0] if targets else None


def route_targets(action: dict, registry: list[str], connected: dict[str, bool]) -> list[str]:
    """Every body that must execute this action ([] if unknown body).

    Speech goes to all connected voice bodies at once (room speaker and OBS):
    whatever the input was (voice, WebUI, Twitch…), the answer is heard in the
    room and on the stream.  Other actions go to a single body.
    """
    kind = action.get("type", "")
    requested = action.get("body", registry[0] if registry else "")
    if requested not in registry:
        return []

    if kind == "speech":
        voices = [b for b in registry if is_voice(b) and connected.get(b, False)]
        if voices:
            return voices

    if kind in CAPABILITIES[body_type(requested)]:
        # A voice body that is offline cannot speak: fall back to a NAO.
        if is_voice(requested) and not connected.get(requested, False):
            for b in registry:
                if not is_voice(b) and kind in CAPABILITIES[body_type(b)]:
                    return [b]
        return [requested]

    for b in registry:
        if kind in CAPABILITIES[body_type(b)]:
            return [b]
    return [requested]


# ─────────────────────────────────────────────────────────────────────────────
# Wake word
# ─────────────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Lowercase, no accents, no punctuation, 'okay' → 'ok'."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bokay\b|\bo k\b", "ok", text)
    return " ".join(text.split())


class WakeWordGate:
    """Filter the speech of bodies that transcribe everything they hear.

    The NAO detects "ok nao" on board and only sends what follows.  The static
    drone sends every speech segment, so the same rule is applied here on the
    transcribed text:
      - "ok nao, quelle heure est-il ?" → "quelle heure est-il ?"
      - "ok nao" alone → nothing yet, the next sentence within `window_s`
        seconds is accepted without the wake word
      - anything else → ignored (None)

    Whisper often mishears a short "ok nao" ("Ok, now.", "OK no"): `aliases`
    are other spellings accepted as the wake word.

    With WAKE_MODE=local the drone detects "ok nao" itself (openWakeWord) and
    only sends what follows: that text goes through `strip`, not `filter`.
    """

    def __init__(self, wake_word: str, window_s: float = 10.0, aliases=()):
        self._wake = normalize(wake_word)
        self._wakes = {self._wake} | {normalize(a) for a in aliases if normalize(a)}
        self._window_s = window_s
        self._armed_until: dict[str, float] = {}

    def _after_wake(self, text: str, first_words: int | None = None) -> str | None:
        """Text after the wake word ("" if nothing follows), None without it.

        The wake word is found on normalised words, then the original text is cut.
        """
        words = text.split()
        starts = len(words) if first_words is None else min(len(words), first_words)
        for i in range(starts):
            for j in range(i + 1, min(len(words), i + 6) + 1):
                if normalize(" ".join(words[i:j])) in self._wakes:
                    return " ".join(words[j:]).lstrip(" ,.;:!?-").strip()
        return None

    def filter(self, body_id: str, text: str, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if not self._wake:
            return text
        rest = self._after_wake(text)
        if rest:
            self._armed_until.pop(body_id, None)
            return rest
        if rest is not None:
            self._armed_until[body_id] = now + self._window_s
            return None
        if self.armed(body_id, now):
            self._armed_until.pop(body_id, None)
            return text
        return None

    def strip(self, text: str) -> str:
        """Speech that follows a wake word the body detected itself (static drone,
        WAKE_MODE=local): never filtered, only a leading "ok nao" kept by the
        drone's pre-roll is cut."""
        if not self._wake:
            return text
        rest = self._after_wake(text, first_words=1)
        return text if rest is None else rest

    def armed(self, body_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        until = self._armed_until.get(body_id)
        return until is not None and until >= now


# ─────────────────────────────────────────────────────────────────────────────
# Voice direction
# ─────────────────────────────────────────────────────────────────────────────

def direction_label(azimuth_deg: float | None) -> str:
    """'[voix : 30° à gauche]' for the LLM context, '' when unknown."""
    if azimuth_deg is None:
        return ""
    a = round(float(azimuth_deg))
    if abs(a) < 5:
        return "[voix : en face]"
    return f"[voix : {abs(a)}° à {'droite' if a > 0 else 'gauche'}]"


def head_yaw_rad(azimuth_deg: float, offset_deg: float = 0.0) -> float:
    """NAO HeadYaw (rad) to look at a voice heard by the static drone.

    The drone counts positive to the right, NAO HeadYaw positive to the left,
    hence the minus sign.  NAO_HEAD_YAW_OFFSET (degrees, set by hand) accounts
    for the NAO not facing the same way as the camera.  Result clamped to the
    HeadYaw joint limits.
    """
    yaw = math.radians(-float(azimuth_deg) + float(offset_deg))
    return max(-HEAD_YAW_LIMIT_RAD, min(HEAD_YAW_LIMIT_RAD, yaw))


# ─────────────────────────────────────────────────────────────────────────────
# NAO motor ranges (motor profile calibrated from the régie)
# ─────────────────────────────────────────────────────────────────────────────

CHAIN_LABELS = {"head": "tête", "larm": "bras gauche", "rarm": "bras droit"}

# What the low and the high end of each joint mean, so the LLM knows which way
# to go (NAO conventions: ShoulderPitch negative raises the arm, and so on).
# ElbowYaw is left out: its sign is not intuitive enough to name safely.
JOINT_SENSE = {
    "HeadYaw":        ("regarde à droite", "regarde à gauche"),
    "HeadPitch":      ("tête levée", "tête baissée"),
    "LShoulderPitch": ("bras levé", "bras baissé"),
    "RShoulderPitch": ("bras levé", "bras baissé"),
    "LShoulderRoll":  ("bras contre le corps", "bras écarté"),
    "RShoulderRoll":  ("bras écarté", "bras contre le corps"),
    "LElbowRoll":     ("coude plié", "coude tendu"),
    "RElbowRoll":     ("coude tendu", "coude plié"),
}


def motor_ranges_label(ranges: dict) -> str:
    """Lines for the LLM prompt: each joint the NAO may move and its range (rad).

    `ranges` is {chain: {joint: [min, max]}} as published by the NAO driver on
    /bodies/{id}/sensors/motor_ranges.  Empty: the NAO is not calibrated.
    """
    lines = []
    for chain in ("head", "larm", "rarm"):
        joints = ranges.get(chain) if isinstance(ranges, dict) else None
        if not isinstance(joints, dict) or not joints:
            continue
        lines.append(f"- {CHAIN_LABELS[chain]} :")
        for joint, (lo, hi) in joints.items():
            low, high = JOINT_SENSE.get(joint, ("", ""))
            if low:
                # Both ends and the middle, each with its meaning: the LLM picks
                # a value in between from the intensity it wants.
                lines.append(f"  {joint} : min {lo:+.2f} = {low}, milieu {(lo + hi) / 2:+.2f}, "
                             f"max {hi:+.2f} = {high}")
            else:
                lines.append(f"  {joint} : min {lo:+.2f}, milieu {(lo + hi) / 2:+.2f}, max {hi:+.2f}")
    if not lines:
        return "Aucune articulation disponible (NAO non calibré) : n'envoie pas d'action motion."
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Learned gestures (nao-emotion-motion generator on the static drone, ADR 009)
# ─────────────────────────────────────────────────────────────────────────────

# "energy" of the LLM answer → gesture intensity, when the action gives none.
ENERGY_INTENSITY = {"very-low": 0.15, "low": 0.3, "medium": 0.5, "high": 0.7, "very-high": 0.9}


def gestures_label(emotions: list, motion: bool = True) -> str:
    """Prompt lines for the "gesture" action; empty list = not available."""
    if not emotions and not motion:
        return ("Gestes appris : indisponibles pour l'instant, NAO ne bouge pas : "
                "n'envoie que \"speech\" et \"leds\".")
    if not motion:
        return ("Gestes appris (tête, bras et mains ensemble, appris sur ce NAO) : "
                "action {\"type\": \"gesture\", \"emotion\": <une de : "
                + ", ".join(str(e) for e in emotions)
                + ">, \"intensity\": 0.0 à 1.0}. Un seul geste par réponse.")
    if not emotions:
        return ("Gestes appris : indisponibles pour l'instant, n'envoie pas d'action "
                "\"gesture\".")
    return ("Gestes appris (tête, bras et mains ensemble, appris sur ce NAO) : "
            "action {\"type\": \"gesture\", \"emotion\": <une de : "
            + ", ".join(str(e) for e in emotions)
            + ">, \"intensity\": 0.0 à 1.0}. Un geste appris remplace les actions "
              "\"motion\" et \"look\" de la même réponse : n'en mets qu'un, sans motion ni look. "
              "Préfère-le quand ton émotion est nette ; il se tourne seul vers la voix entendue.")


def gesture_order(action: dict, emotions: list, energy: str = "") -> dict | None:
    """{"emotion", "intensity"} for the NAO driver, or None if the action is unusable."""
    emotion = str(action.get("emotion", "")).strip()
    if not emotion or (emotions and emotion not in emotions):
        return None
    try:
        intensity = float(action["intensity"])
    except (KeyError, TypeError, ValueError):
        intensity = ENERGY_INTENSITY.get(str(energy).lower().strip(), 0.5)
    if intensity != intensity:   # NaN
        intensity = 0.5
    return {"emotion": emotion, "intensity": round(max(0.0, min(1.0, intensity)), 3)}


def gesture_event(azimuth_deg: float | None, offset_deg: float = 0.0) -> dict | None:
    """Voice direction heard by the static drone → event in the NAO torso frame.

    The drone counts positive to the right, the gesture generator positive to
    the robot's left (like HeadYaw), hence the minus sign; NAO_HEAD_YAW_OFFSET
    as in head_yaw_rad().  Elevation unknown: 0.  Clamped to ±90°, the range
    the gestures were recorded with.
    """
    if azimuth_deg is None:
        return None
    az = -float(azimuth_deg) + float(offset_deg)
    az = max(-90.0, min(90.0, az))
    return {"azimuth_deg": round(az, 1), "elevation_deg": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# POST /perform: actions already computed (storyteller), no LLM
# ─────────────────────────────────────────────────────────────────────────────

# Emotion of a story script → learned gesture of the generator, then /emotion_state.
STORY_GESTURES = {"neutral": "calm", "calm": "calm", "curious": "surprise", "joyful": "joy",
                  "excited": "joy", "uncertain": "surprise", "worried": "fear",
                  "scared": "fear", "angry": "anger", "sad": "sadness"}
STORY_EMOTION_STATE = {"curious": "curious", "excited": "excited", "joyful": "excited",
                       "angry": "excited", "uncertain": "uncertain", "worried": "uncertain",
                       "scared": "uncertain"}
PERFORM_ACTIONS = ("speech", "gesture", "leds")


def perform_request(body, registry: list[str], gesture_emotions: list) -> dict:
    """{"actions", "emotion", "energy"} ready to dispatch; ValueError when unusable.

    Actions without "body" go to the NAO (speech is then routed to every voice).
    A gesture emotion the NAO does not know is mapped to a learned one.
    """
    body = body if isinstance(body, dict) else {}
    actions = body.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("missing actions")
    nao = next((b for b in registry if not is_voice(b)), registry[0])
    ready = []
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in PERFORM_ACTIONS:
            raise ValueError(f"actions must be {', '.join(PERFORM_ACTIONS)} objects")
        action = {"body": nao, **action}
        if action["type"] == "speech" and not str(action.get("text", "")).strip():
            raise ValueError("a speech action needs a text")
        emotion = str(action.get("emotion", ""))
        if action["type"] == "gesture" and gesture_emotions and emotion not in gesture_emotions:
            action["emotion"] = STORY_GESTURES.get(emotion, emotion)
        ready.append(action)
    return {"actions": ready,
            "emotion": STORY_EMOTION_STATE.get(str(body.get("emotion", "")), "neutral"),
            "energy": str(body.get("energy") or "medium")}


# ─────────────────────────────────────────────────────────────────────────────
# Speech ↔ learned gesture synchronisation
# ─────────────────────────────────────────────────────────────────────────────

# Débit moyen d'edge-tts en français (fr-FR-DeniseNeural, vitesse normale) :
# sert d'estimation avant que la durée mesurée du MP3 n'arrive au drone.
SPEECH_CHARS_PER_S = 14.0
SPEECH_PAUSE_S = {".": 0.35, "!": 0.35, "?": 0.35, "…": 0.35, ";": 0.25, ":": 0.2, ",": 0.15}


def speech_seconds(text: str, chars_per_s: float = SPEECH_CHARS_PER_S) -> float:
    """Durée estimée d'une phrase dite par la synthèse vocale (s)."""
    text = " ".join(str(text or "").split())
    if not text:
        return 0.0
    letters = sum(1 for c in text if c.isalnum() or c == " ")
    pauses = sum(SPEECH_PAUSE_S.get(c, 0.0) for c in text.rstrip(".!?… "))
    return round(letters / float(chars_per_s) + pauses, 2)


def speech_gesture_sync(actions: list, registry: list[str], connected: dict[str, bool],
                        emotions: list, sync_id: str, energy: str = "",
                        chars_per_s: float = SPEECH_CHARS_PER_S) -> dict | None:
    """Lien entre la parole et le geste appris d'une même réponse du LLM.

    Seulement quand la première phrase est dite par une voix qui sait attendre le
    geste (haut-parleur du drone statique, voix OBS du stream) et qu'un geste
    utilisable part au NAO.  {"id", "speech_s", "speech_s_first"} : durée estimée de toute la
    parole et de la première phrase (celle que le drone retient et mesure).
    """
    speeches = [a for a in actions if a.get("type") == "speech" and str(a.get("text", "")).strip()]
    gestures = [a for a in actions
                if a.get("type") == "gesture" and gesture_order(a, emotions, energy) is not None]
    if not speeches or not gestures:
        return None
    if not any(body_type(b) in VOICE_TYPES for b in route_targets(speeches[0], registry, connected)):
        return None
    first = speech_seconds(speeches[0]["text"], chars_per_s)
    total = first + sum(speech_seconds(a["text"], chars_per_s) for a in speeches[1:])
    return {"id": sync_id, "speech_s": round(total, 2), "speech_s_first": first}


# ─────────────────────────────────────────────────────────────────────────────
# Speaking state: is NAO's voice still talking?
# ─────────────────────────────────────────────────────────────────────────────

class SpeakingTracker:
    """Tell when the last answer has been spoken, so an input can send the next one.

    Each sentence sent to a connected voice body (static, obs) is pending until
    that body reports speaking=true (synthesis and gesture sync delay the
    start), then busy until speaking=false.  A pending sentence that never
    starts stops counting after start_grace_s (counted from the end of the
    sentence it waits behind), a speaking=true without its
    false after max_speaking_s.  The NAO has no speaking signal: a sentence it
    says itself counts for its estimated duration (speech_seconds).

    started() tells when the last answer has begun (answer() marks where it
    begins): its first sentence has started on every voice, so its gesture is
    already playing and the next input can be prepared meanwhile.
    """

    def __init__(self, start_grace_s: float = 10.0, max_speaking_s: float = 120.0):
        self.start_grace_s = start_grace_s
        self.max_speaking_s = max_speaking_s
        self._pending: dict[str, list[float]] = {}   # body → start deadlines, oldest first
        self._speaking: dict[str, float] = {}         # body → speaking=true time
        self._estimated_until = 0.0
        self._answer: dict[str, int] = {}             # body → sentences of the last answer
        self._lock = threading.Lock()   # ROS callbacks and HTTP threads

    def answer(self):
        """A new answer starts: the next sentences said belong to it."""
        with self._lock:
            self._answer = {}

    def said(self, body_id: str, text: str, now: float | None = None,
             chars_per_s: float = SPEECH_CHARS_PER_S):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._said(body_id, text, now, chars_per_s)

    def _said(self, body_id, text, now, chars_per_s):
        if is_voice(body_id):
            self._pending.setdefault(body_id, []).append(now + self.start_grace_s)
            self._answer[body_id] = self._answer.get(body_id, 0) + 1
        else:
            self._estimated_until = max(self._estimated_until,
                                        now + speech_seconds(text, chars_per_s))

    def heard(self, body_id: str, speaking: bool, now: float | None = None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if speaking:
                self._speaking[body_id] = now
                if self._pending.get(body_id):
                    self._pending[body_id].pop(0)
            else:
                if self._speaking.pop(body_id, None) is not None and self._pending.get(body_id):
                    # The sentences queued behind this one may start from now on.
                    self._pending[body_id] = [now + self.start_grace_s for _ in self._pending[body_id]]

    def _expire(self, now):
        for body_id in list(self._pending):
            since = self._speaking.get(body_id)
            if since is not None and now - since < self.max_speaking_s:
                continue   # queued behind the sentence being said: not late
            self._pending[body_id] = [d for d in self._pending[body_id] if d > now]

    def started(self, now: float | None = None) -> bool:
        """The last answer's first sentence has started (or was given up) on
        every voice; an answer without speech once the older sentences started."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            # Pending sentences are started oldest first: the answer has begun once
            # fewer of them are left than it holds.
            return all(not left or len(left) < self._answer.get(body_id, 0)
                       for body_id, left in self._pending.items())

    def busy(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._expire(now)
            return (any(self._pending.values())
                    or any(now - t < self.max_speaking_s for t in self._speaking.values())
                    or now < self._estimated_until)
