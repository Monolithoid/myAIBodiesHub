"""
motion_policy.py — NAO motion decisions, with no ROS 2 dependency.

The robot is a puppet: it crashes too often to decide on its own position. The
NAO driver therefore decides everything that goes out on /nao/motion,
/nao/gesture and /nao/cmd_vel; nao_ws_bridge.py, on the robot, only keeps bounds
(angles, speed, stiffness, walk) as a last safety net.

Constrained motor universe: NAO stays seated and only moves three chains (head,
left arm, right arm). The motor profile, calibrated from the control room and
announced by the robot (motor_profile in /nao/capabilities), gives for each chain
a rest position, a start position, a range per joint and a speed. The LLM only
knows the ranges.

Decisions taken here:
  - sequence: every gesture goes rest → start → target → start → rest. The
    target starts from the start position, completed by the requested joints
    clamped into their range. A new order during the gesture chains target →
    start → new target, without going back through rest;
  - grouping: orders for the same chain received within merge_window seconds
    form a single target (the LLM often sends one action per joint);
  - end of step: a step lasts max(min_move_time, duration estimated from
    JOINT_MAX_VELOCITY); the next step starts afterwards;
  - rest: a chain inactive for rest_delay goes back to rest through the start
    pose;
  - without a profile, or for a joint outside the chains, the order is refused;
  - legs: postures and walking move the legs, which this NAO does not move. They
    are therefore off by default (empty postures, walk=false). Once re-enabled:
    list of postures, delay between two changes, gestures blocked for
    posture_block seconds and during walking, no walking during a posture and
    no posture during walking;
  - heat: a chain that is too hot (motor_temp from /nao/status) refuses new
    orders, but can still return to rest;
  - learned gestures (ADR 009): an emotional gesture is computed and played by
    the static drone (emotion_gesture.py), on the head, arms, wrists and hands at
    once. The driver only accepts it without a posture, without walking and
    without a chain that is too hot; it then drops the current targets, waits
    for the chains to finish their step, then reserves all the chains for the
    drone. While the drone holds the body (owns_body in
    /nao/emotion_gesture_state), no /nao/motion, posture or walk order goes out.
    The drone hands back control at rest; if it is silent for more than
    gesture_timeout seconds, or hands back control elsewhere than at rest, the
    chains go back to rest through the start pose.
"""

DEFAULT_LIMITS = {
    "min_interval":      0.1,    # s between two /nao/motion sends (10 Hz max)
    "min_move_time":     0.3,    # minimum s per sequence step
    "velocity_scale":    1.0,    # share of JOINT_MAX_VELOCITY actually reached (0 = no estimate)
    "merge_window":      0.3,    # s during which a chain's orders form a single target
    "max_motor_temp":    70,     # °C; above it the chain refuses orders (0 = disabled)
    "postures":          [],     # allowed postures (e.g. ["Stand", "Sit"]); empty = none
    "walk":              False,  # autoriser /nao/cmd_vel (marche)
    "posture_cooldown":  5.0,    # s between two posture changes
    "posture_block":     5.0,    # s during which a posture change blocks gestures
    "joints_while_busy": False,  # accept gestures during a posture or a walk
    "rest_return":       True,   # return to rest after rest_delay of inactivity
    "rest_delay":        3.0,    # s of inactivity (after the end of the gesture) before returning
    "gestures":          True,   # accept learned gestures (static drone, ADR 009)
    "gesture_timeout":   60.0,   # s without news from the drone before taking the chains back
}

# Commandable chains and their joints. Wrists and hands are not
# included: no hand movement in this universe.
CHAINS = {
    "head": ("HeadYaw", "HeadPitch"),
    "larm": ("LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll"),
    "rarm": ("RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll"),
}

# Max velocity (rad/s) at speed 1.0: no-load speed of the head/arm motor
# (RE-Max 17, 11,900 rpm = 1246 rad/s) / reduction ratio (Aldebaran
# "H25 - Motors" doc: A 150.27, B 173.22, C 50.61).
JOINT_MAX_VELOCITY = {
    "HeadYaw": 8.29, "HeadPitch": 7.19,
    "ShoulderPitch": 8.29, "ShoulderRoll": 7.19,
    "ElbowYaw": 8.29, "ElbowRoll": 7.19,
    "WristYaw": 24.62,
}


def chain_of(joint: str) -> str:
    """Chain (head / larm / rarm) of a commandable joint, '' otherwise.
    Same names as the motor_temp keys of /nao/status."""
    for name, joints in CHAINS.items():
        if joint in joints:
            return name
    return ""


def load_limits(section) -> tuple[dict, list[str]]:
    """Merges a configuration (dict) with DEFAULT_LIMITS.

    Unknown keys and values of the wrong type are ignored: a typo
    never disables a limit. Returns (limits, warnings).
    """
    limits = dict(DEFAULT_LIMITS)
    warnings = []
    if not isinstance(section, dict):
        return limits, warnings
    for key, value in section.items():
        if key not in limits:
            warnings.append(f"unknown key: {key}")
            continue
        default = limits[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type(default))
        if not ok:
            warnings.append(f"valeur invalide : {key}={value!r}")
            continue
        limits[key] = value
    return limits, warnings


def parse_motion(data: dict) -> tuple[list[str], list[float], float]:
    """(joints, angles, speed) of a /nao/motion command,
    multi-joint {joints, angles, speed} or single {joint, angle, speed}.
    Speed 0 when not given: the profile's speed applies."""
    speed = float(data.get("speed", 0.0))
    if "joints" in data:
        return [str(j) for j in data["joints"]], [float(a) for a in data["angles"]], speed
    if "joint" in data:
        return [str(data["joint"])], [float(data.get("angle", 0.0))], speed
    return [], [], speed


def _pose(section, joints) -> dict | None:
    if not isinstance(section, dict):
        return None
    try:
        return {j: float(section[j]) for j in joints}
    except (KeyError, TypeError, ValueError):
        return None


def load_profile(profile) -> tuple[dict, list[str]]:
    """Valid chains of a motor profile announced by the robot.

    Returns ({chain: {rest, start, range, speed}}, errors). An incomplete
    or inconsistent chain (start out of range) is dropped: it will not
    move. The robot already validates the profile; this protects against
    a hand-written profile.
    """
    chains, errors = {}, []
    if not isinstance(profile, dict):
        return chains, errors
    section = profile.get("chains", {})
    if not isinstance(section, dict):
        return chains, ["chains is not an object"]
    for name, cfg in section.items():
        if name not in CHAINS or not isinstance(cfg, dict):
            errors.append(f"unknown chain: {name}")
            continue
        joints = CHAINS[name]
        rest, start = _pose(cfg.get("rest"), joints), _pose(cfg.get("start"), joints)
        rng = {}
        for j in joints:
            try:
                lo, hi = (float(v) for v in cfg.get("range", {})[j])
            except (KeyError, TypeError, ValueError, AttributeError):
                break
            if lo > hi:
                break
            rng[j] = (lo, hi)
        try:
            speed = float(cfg.get("speed", 0.0))
        except (TypeError, ValueError):
            speed = 0.0
        if rest is None or start is None or len(rng) != len(joints) or not 0 < speed <= 1:
            errors.append(f"{name}: rest, start, range or speed missing")
            continue
        if any(not rng[j][0] - 1e-3 <= start[j] <= rng[j][1] + 1e-3 for j in joints):
            errors.append(f"{name}: start position out of range")
            continue
        chains[name] = {"rest": rest, "start": start, "range": rng, "speed": speed}
    return chains, errors


# s during which a drone state unrelated to the gesture sent is ignored
GESTURE_GRACE = 2.0


class _Chain:
    """State of a chain: last step sent, upcoming steps, target being formed."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.joints = CHAINS[name]
        self.rest = cfg["rest"]
        self.start = cfg["start"]
        self.range = cfg["range"]
        self.speed = cfg["speed"]
        self.pose = dict(self.rest)   # last commanded position (the robot starts at rest)
        self.kind = "rest"            # rest | start | target: kind of the last step
        self.hold_until = 0.0         # estimated end of the last step
        self.queue: list[tuple[str, dict, float]] = []   # (nature, position, vitesse)
        self.target: dict | None = None    # target being formed (not planned yet)
        self.target_speed = self.speed
        self.target_since = 0.0
        self.last_order = 0.0

    def clamp(self, joint: str, angle: float) -> float:
        lo, hi = self.range[joint]
        return max(lo, min(hi, angle))


class MotionPolicy:
    """All of the NAO's motion decisions. The times (now) are
    passed by the caller, which makes the class testable without a clock."""

    def __init__(self, limits: dict | None = None):
        self.limits = limits or dict(DEFAULT_LIMITS)
        self.chains: dict[str, _Chain] = {}
        self._last_send = float("-inf")
        self._hot: set[str] = set()
        self._walking = False
        self._posture_until = 0.0
        self._last_posture = float("-inf")
        self.refused = 0
        self._gesture_pending: dict | None = None   # accepted gesture, waiting for free chains
        self._gesture_until = 0.0                     # chains reserved for the drone until this time
        self._gesture_sent: tuple[str, float] = ("", 0.0)   # (id, time) of the last published gesture

    # -- state coming from the robot ------------------------------------------------

    def set_profile(self, chains: dict) -> None:
        """Motor profile (already validated by load_profile): the robot has just
        (re)started its script or finished a calibration, it is at rest."""
        self.chains = {name: _Chain(name, cfg) for name, cfg in chains.items()}

    def ranges(self) -> dict:
        """Allowed ranges per chain, for the LLM: {chain: {joint: [min, max]}}."""
        return {name: {j: [round(lo, 3), round(hi, 3)] for j, (lo, hi) in c.range.items()}
                for name, c in sorted(self.chains.items())}

    def set_motor_temps(self, temps: dict) -> set[str]:
        """temps: {"head"|"larm"|"rarm": °C}. Returns the chains that are too hot."""
        max_temp = self.limits["max_motor_temp"]
        self._hot = {g for g, t in temps.items() if max_temp and t >= max_temp}
        return self._hot

    def set_walking(self, walking: bool) -> None:
        self._walking = walking
        if walking:
            for c in self.chains.values():
                c.target = None

    def _busy(self, now: float) -> bool:
        return self._walking or now < self._posture_until

    def gesture_active(self, now: float) -> bool:
        """Learned gesture pending or in progress: the drone has (or will have) control."""
        if self._gesture_pending is not None:
            return True
        if self._gesture_until and now >= self._gesture_until:
            # No more news from the drone: take back control, without knowing where the arms are.
            self.release_gesture(at_rest=False, now=now)
        return now < self._gesture_until

    # -- ordres ------------------------------------------------------------

    def posture_allowed(self, name: str, now: float) -> tuple[bool, str]:
        """(allowed?, refusal reason). An allowed posture blocks gestures."""
        if not self.limits["postures"]:
            return False, "postures disabled (legs)"
        if self.gesture_active(now):
            return False, "learned gesture in progress"
        if name not in self.limits["postures"]:
            return False, "hors liste"
        if self._walking:
            return False, "walk in progress"
        if now - self._last_posture < float(self.limits["posture_cooldown"]):
            return False, "delay between two postures"
        self._last_posture = now
        self._posture_until = now + float(self.limits["posture_block"])
        for c in self.chains.values():
            c.target = None
        return True, ""

    def move_decision(self, walking: bool, now: float) -> tuple[bool, str]:
        """(send?, refusal reason) for a walk order.

        A zero-velocity order is only sent to stop a walk in
        progress: otherwise moveToward(0, 0, 0) wakes the walk engine for
        nothing, and during a posture change it crashes NAOqi.
        """
        if not self.limits["walk"]:
            return False, "walk disabled (legs)"
        if self.gesture_active(now):
            return False, "learned gesture in progress"
        if now < self._posture_until:
            return False, "posture change in progress"
        if not walking and not self._walking:
            return False, "zero velocity, the robot is not walking"
        self.set_walking(walking)
        return True, ""

    def submit(self, joints, angles, speed: float, now: float) -> int:
        """Adds joints to their chain's target; returns the
        number of accepted joints (the others are counted in refused)."""
        if (self._busy(now) and not self.limits["joints_while_busy"]) or self.gesture_active(now):
            self.refused += len(joints)
            return 0
        accepted = 0
        for j, a in zip(joints, angles):
            c = self.chains.get(chain_of(j))
            if c is None or c.name in self._hot:
                self.refused += 1
                continue
            if c.target is None:
                c.target = dict(c.start)
                c.target_speed = c.speed
                c.target_since = now
            c.target[j] = c.clamp(j, float(a))
            if speed > 0:
                c.target_speed = min(c.target_speed, float(speed))
            c.last_order = now
            accepted += 1
        return accepted

    def request_gesture(self, order: dict, now: float) -> tuple[bool, str]:
        """(accepted?, refusal reason) for a learned gesture. Once accepted, it goes
        out through take_gesture() as soon as the chains have finished their current step."""
        if not self.limits["gestures"]:
            return False, "learned gestures disabled"
        if not self.chains:
            return False, "no motor profile"
        if self._busy(now):
            return False, "posture or walk in progress"
        if self._hot:
            return False, "trop chaud : " + ", ".join(sorted(self._hot))
        if self._gesture_pending is not None:
            # The most recent one replaces the one still waiting for the chains.
            self._gesture_pending = order
            return True, ""
        for c in self.chains.values():
            c.target = None
            c.queue = []
        self._gesture_pending = order
        return True, ""

    def take_gesture(self, now: float) -> dict | None:
        """The gesture to publish on /nao/emotion_gesture, once the chains are still."""
        if self._gesture_pending is None:
            return None
        if any(c.hold_until > now for c in self.chains.values()):
            return None
        order, self._gesture_pending = self._gesture_pending, None
        self._gesture_until = now + float(self.limits["gesture_timeout"])
        self._gesture_sent = (str(order.get("id", "")), now)
        return order

    def gesture_state(self, owns_body: bool, at_rest: bool, now: float, gid=None) -> None:
        """State published by the drone: it still holds the body, or hands it back.

        Right after a gesture is sent, a "hands back control" state that does not
        mention this gesture may have left before the drone received it: it is
        ignored for GESTURE_GRACE seconds."""
        if owns_body:
            # Even a gesture that did not go through here (control room button) reserves the chains.
            if self._gesture_pending is None:
                self._gesture_until = now + float(self.limits["gesture_timeout"])
            return
        if not self._gesture_until:
            return
        sent_id, sent_at = self._gesture_sent
        if gid != sent_id and now - sent_at < GESTURE_GRACE:
            return
        self.release_gesture(at_rest, now)

    def release_gesture(self, at_rest: bool, now: float) -> None:
        """The drone hands the chains back. At rest (relaxed by the robot): nothing to
        do. Elsewhere: return to rest through the start pose, right away."""
        self._gesture_until = 0.0
        for c in self.chains.values():
            c.target = None
            c.queue = []
            # Inactive for rest_delay: the return to rest can go right away.
            c.hold_until = c.last_order = now - float(self.limits["rest_delay"])
            if at_rest:
                c.pose, c.kind = dict(c.rest), "rest"
            else:
                c.kind = "target"

    def take_refused(self) -> int:
        n, self.refused = self.refused, 0
        return n

    # -- envoi -------------------------------------------------------------

    def _duration(self, c: _Chain, pose: dict, speed: float) -> float:
        duration = float(self.limits["min_move_time"])
        for j, a in pose.items():
            vmax = JOINT_MAX_VELOCITY.get(j[1:] if j[:1] in ("L", "R") else j, 0.0) \
                * float(self.limits["velocity_scale"])
            if vmax > 0:
                duration = max(duration, abs(a - c.pose[j]) / (max(speed, 0.01) * vmax))
        return duration

    def _plan(self, c: _Chain, now: float) -> None:
        """Turns the target being formed into steps, or plans the return to rest."""
        if c.target is not None:
            if now - c.target_since < float(self.limits["merge_window"]):
                return
            # Already at start (or on the way): straight to the target. Otherwise, through start.
            steps = [] if c.kind == "start" else [("start", c.start, c.speed)]
            c.queue = steps + [("target", c.target, c.target_speed)]
            c.target = None
            return
        if c.queue or c.kind == "rest" or not self.limits["rest_return"]:
            return
        idle_since = max(c.last_order, c.hold_until)
        if now - idle_since >= float(self.limits["rest_delay"]):
            back = [("rest", c.rest, c.speed)]
            c.queue = back if c.kind == "start" else [("start", c.start, c.speed)] + back

    def take(self, now: float) -> list[dict]:
        """/nao/motion commands to send now, one per ready chain."""
        if now - self._last_send < float(self.limits["min_interval"]):
            return []
        cmds = []
        for name in sorted(self.chains):
            c = self.chains[name]
            if self._busy(now) or self.gesture_active(now):
                continue
            self._plan(c, now)
            if not c.queue or c.hold_until > now:
                continue
            kind, pose, speed = c.queue.pop(0)
            duration = self._duration(c, pose, speed)
            c.hold_until = now + duration
            c.pose = dict(pose)
            c.kind = kind
            cmd = {"joints": list(c.joints),
                   "angles": [round(pose[j], 4) for j in c.joints],
                   "speed": round(speed, 4)}
            if kind == "rest":
                # The bridge cuts the stiffness once the rest pose is reached.
                cmd["rest_in"] = round(duration, 3)
            cmds.append(cmd)
        if cmds:
            self._last_send = now
        return cmds
