"""
gesture_playback.py — Rend jouable un geste du générateur nao-emotion-motion, côté drone.

Le générateur exporté (`nao_gesture.py` du dataset) valide le geste appris *brut*.
L'interface de nao-emotion-motion, elle, le rend d'abord jouable
(`nao_motion/guardian/playable.py`) : saturation douce dans la zone du guardian,
ralentissement local là où une vitesse ou une accélération dépasse la limite.
Sans cette étape, presque tous les gestes sont refusés (0 sur 54 le 18/11/2026).

Ce module refait cette étape ici, sans toucher au projet nao-emotion-motion :
`make_playable(gen)` remplace `gen._plan()` (projection des points de contrôle
dans la zone) et `gen.gesture()` (saturation douce, ralentissement) sur
l'instance chargée, si bien
que `gen.generate()` ajoute ensuite les transitions et que **le guardian du
générateur exporté juge le mouvement final**, comme avant.  Rien n'est
contourné : un geste encore refusé après adaptation n'est pas joué.

Copie de la logique de playable.py (version du 18/11/2026) : si elle évolue
dans nao-emotion-motion, la reporter ici.  Python standard seulement.
"""

import bisect
import math


def _linspace(a, b, n):
    if n == 1:
        return [float(a)]
    step = (float(b) - float(a)) / (n - 1)
    return [float(a) + i * step for i in range(n - 1)] + [float(b)]


SOFT_BAND_RAD = 0.06
TIME_SAFETY = 1.05
WARP_WINDOW_S = 0.2
SLOW_DOWN_MODES = ("local", "uniform")


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _zone(guardian, joints):
    """(lo, hi, half-spaces [(a, b)]) of the guardian zone for one group, margins included."""
    lo = [guardian.allowed_range(j)[0] for j in joints]
    hi = [guardian.allowed_range(j)[1] for j in joints]
    half = []
    for c in guardian.constraints:
        if all(j in joints for j in c["terms"]):
            a = [0.0] * len(joints)
            for j, coef in c["terms"].items():
                a[joints.index(j)] = float(coef)
            half.append((a, float(c["bound"]) - 1e-3))
    return lo, hi, half


def _inside(x, lo, hi, half, tol=1e-9):
    return (all(v >= l - tol for v, l in zip(x, lo)) and all(v <= h + tol for v, h in zip(x, hi))
            and all(_dot(a, x) <= b + tol for a, b in half))


def _clip_box(x, lo, hi):
    return [min(h, max(l, v)) for v, l, h in zip(x, lo, hi)]


def _project_point(x, lo, hi, half, iters=200):
    """Nearest point of {lo <= y <= hi, a.y <= b} to x (Dykstra's alternating projections)."""
    x = [float(v) for v in x]
    if _inside(x, lo, hi, half):
        return list(x)
    sets = [None] + list(half)
    y = list(x)
    inc = [[0.0] * len(x) for _ in sets]
    for _ in range(iters):
        prev = list(y)
        for k, h in enumerate(sets):
            z = [a + b for a, b in zip(y, inc[k])]
            if h is None:
                p = _clip_box(z, lo, hi)
            else:
                a, b = h
                s = _dot(a, z) - b
                p = [zi - (s / _dot(a, a)) * ai for zi, ai in zip(z, a)] if s > 0 else list(z)
            inc[k] = [zi - pi for zi, pi in zip(z, p)]
            y = p
        if max(abs(a - b) for a, b in zip(y, prev)) < 1e-10:
            break
    return _clip_box(y, lo, hi)  # the box is never left, even if the half-spaces could not all be met


def _project_control_points(guardian, ctrl, joints):
    """(projected control points, max translation in rad, number of points moved)."""
    lo, hi, half = _zone(guardian, joints)
    out, moved, n = [], 0.0, 0
    for p in ctrl:
        q = _project_point(p, lo, hi, half)
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(p, q)))
        moved, n = max(moved, d), n + (d > 1e-9)
        out.append(q)
    return out, moved, n


def _soft_upper(xs, hi, band):
    """Identity below hi - band; above, compressed smoothly (C1) so that it never exceeds hi."""
    knee = hi - band
    return [knee + band * math.tanh((x - knee) / band) if x > knee else x for x in xs]


def _soft_box(xs, lo, hi, band):
    band = max(1e-4, min(band, (hi - lo) / 4))
    return [-v for v in _soft_upper([-v for v in _soft_upper(xs, hi, band)], -lo, band)]


def _gradient(t, q):
    """Like numpy.gradient(q, t, axis=0): second order inside, first order at the ends."""
    n, J = len(t), len(q[0])
    g = [[0.0] * J for _ in range(n)]
    if n < 2:
        return g
    for j in range(J):
        g[0][j] = (q[1][j] - q[0][j]) / (t[1] - t[0])
        g[-1][j] = (q[-1][j] - q[-2][j]) / (t[-1] - t[-2])
    for i in range(1, n - 1):
        hs, hd = t[i] - t[i - 1], t[i + 1] - t[i]
        den = hs * hd * (hd + hs)
        for j in range(J):
            g[i][j] = (hs * hs * q[i + 1][j] + (hd * hd - hs * hs) * q[i][j] - hd * hd * q[i - 1][j]) / den
    return g


def _cubic_resample(t, q, tt):
    """Cubic Hermite interpolation (tangents from _gradient): no kinks, so no artificial
    acceleration peaks (linear interpolation would add some)."""
    m = _gradient(t, q)
    n, out = len(t), []
    for x in tt:
        i = min(max(bisect.bisect_right(t, x) - 1, 0), n - 2)
        h = t[i + 1] - t[i]
        u = (x - t[i]) / h
        h00, h10 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u
        h01, h11 = -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
        out.append([h00 * a + h10 * h * ma + h01 * b + h11 * h * mb
                    for a, b, ma, mb in zip(q[i], q[i + 1], m[i], m[i + 1])])
    return out


def _grid(duration, rate_hz):
    return _linspace(0.0, duration, max(2, int(math.ceil(duration * rate_hz)) + 1))


def _velocities(t, q):
    return [[(b - a) / (t[i + 1] - t[i]) for a, b in zip(q[i], q[i + 1])] for i in range(len(t) - 1)]


def _accelerations(t, vel):
    return [[(b - a) / ((t[i + 2] - t[i]) / 2) for a, b in zip(vel[i], vel[i + 1])] for i in range(len(vel) - 1)]


def _time_scale(guardian, t, q, joints):
    if len(t) < 2:
        return 1.0
    vel = _velocities(t, q)
    s = 1.0
    for k, j in enumerate(joints):
        s = max(s, max(abs(v[k]) for v in vel) / guardian.velocity_limit(j))
    if len(vel) >= 2:
        acc = _accelerations(t, vel)
        for k, j in enumerate(joints):
            s = max(s, math.sqrt(max(abs(a[k]) for a in acc) / guardian.acceleration_limit(j)))
    return s


def _local_need(guardian, t, q, joints):
    """Slow-down needed on each interval: velocity ratio, and the square root of the
    acceleration ratio at both ends."""
    vel = _velocities(t, q)
    vlim = [guardian.velocity_limit(j) for j in joints]
    need = [max(abs(v) / l for v, l in zip(row, vlim)) for row in vel]
    if len(vel) >= 2:
        alim = [guardian.acceleration_limit(j) for j in joints]
        for i, row in enumerate(_accelerations(t, vel)):
            ra = math.sqrt(max(abs(a) / l for a, l in zip(row, alim)))
            need[i] = max(need[i], ra)
            need[i + 1] = max(need[i + 1], ra)
    return need


def _warp_rate(need, window):
    """Smooth local slow-down factor >= need (and >= 1): running max, then running mean."""
    r = [1.0 if x <= 1.0 else max(x * TIME_SAFETY, 1.0) for x in need]
    n = len(r)
    w = max(1, min(window, n))
    pad = [r[0]] * w + r + [r[-1]] * w
    mx = [max(pad[i:i + 2 * w + 1]) for i in range(n)]
    pad = [mx[0]] * w + mx + [mx[-1]] * w
    return [sum(pad[i:i + 2 * w + 1]) / (2 * w + 1) for i in range(n)]


def _stretch(t, q, s, rate_hz):
    """Same path, played s times slower (uniform)."""
    tn = _grid((t[-1] - t[0]) * s, rate_hz)
    return tn, _cubic_resample(t, q, [min(x / s, t[-1]) for x in tn])


def _local_warp(guardian, t, q, joints, rate_hz, rounds=8):
    """Slow down only where a limit is exceeded (smooth time warp, same path). None if the
    result would exceed the maximum duration."""
    w = int(round(WARP_WINDOW_S * rate_hz))
    for _ in range(rounds):
        need = _local_need(guardian, t, q, joints)
        if max(need) <= 1.0:
            break
        rate = _warp_rate(need, w)
        tw = [0.0]
        for i in range(len(t) - 1):
            tw.append(tw[-1] + (t[i + 1] - t[i]) * rate[i])
        if tw[-1] > guardian.max_duration_s:
            return None
        tn = _grid(tw[-1], rate_hz)
        q = _cubic_resample(tw, q, [min(x, tw[-1]) for x in tn])
        t = tn
    return t, q


def _shape(guardian, joints, q, context, rounds=6):
    """Joints past their bound and violated coupled constraints brought back smoothly; what
    stays inside is left exactly as the model made it."""
    q = [list(r) for r in q]
    bounds = [guardian.allowed_range(j) for j in joints]
    for _ in range(rounds):
        for c in guardian.constraints:
            terms = c["terms"]
            idx = [(joints.index(j), coef) for j, coef in terms.items() if j in joints]
            if not idx or any(j not in joints and j not in context for j in terms):
                continue
            fixed = sum(coef * float(context[j]) for j, coef in terms.items() if j not in joints)
            cvec = [coef for _, coef in idx]
            cols = [k for k, _ in idx]
            bound = c["bound"] - 1e-3
            s = [sum(r[k] * cv for k, cv in zip(cols, cvec)) + fixed for r in q]
            if not any(x > bound for x in s):
                continue
            s_new = _soft_upper(s, bound, SOFT_BAND_RAD)
            nn = _dot(cvec, cvec)
            for r, a, b in zip(q, s, s_new):
                d = b - a
                if d:
                    for k, cv in zip(cols, cvec):
                        r[k] += d * cv / nn
        for k, (lo, hi) in enumerate(bounds):
            col = [r[k] for r in q]
            if min(col) < lo or max(col) > hi:
                for r, v in zip(q, _soft_box(col, lo, hi, SOFT_BAND_RAD)):
                    r[k] = v
    return q


def _make_playable(guardian, joints, t, q, context=None, rate_hz=50.0, mode="local"):
    """Adapted copy of the gesture (joints, times, rows) + a description of the change. The
    guardian must still validate it. Same as nao_motion.guardian.playable.make_playable."""
    ctx = dict(context or {})
    t0 = [x - t[0] for x in t]
    tg = _grid(t0[-1], rate_hz)
    src = _cubic_resample(t0, q, tg)
    shaped = _shape(guardian, joints, src, ctx)
    diff = [max(abs(a[k] - b[k]) for a, b in zip(shaped, src)) for k in range(len(joints))]
    changed = [j for j, d in zip(joints, diff) if d > 1e-3]
    notes = []
    dur = tg[-1]
    bt, bq = tg, shaped
    s0 = max(1.0, guardian.min_duration_s / dur)
    if s0 > 1.0:
        bt, bq = _stretch(bt, bq, s0, rate_hz)
    local = 1.0
    if mode == "local" and _time_scale(guardian, bt, bq, joints) > 1.0:
        warped = _local_warp(guardian, bt, bq, joints, rate_hz)
        if warped is not None:
            local = warped[0][-1] / bt[-1]
            bt, bq = warped
    s, st, sq = 1.0, bt, bq
    for _ in range(6):
        need = _time_scale(guardian, st, sq, joints)
        if need <= 1.0:
            break
        s_new = s * need * TIME_SAFETY
        if bt[-1] * s_new > guardian.max_duration_s:
            s_new = max(s, guardian.max_duration_s / bt[-1])
            if s_new == s:
                break
            notes.append("slow-down capped by max duration (%s s): the guardian may refuse"
                         % guardian.max_duration_s)
            s = s_new
            st, sq = _stretch(bt, bq, s, rate_hz)
            break
        s = s_new
        st, sq = _stretch(bt, bq, s, rate_hz)
    if changed:
        notes.append("brought back inside limits / coupled zone: %s (max %.3f rad)" % (changed, max(diff)))
    if local > 1.0 + 1e-9:
        notes.append("fast moments slowed down locally: duration x%.2f" % local)
    if s > 1.0 + 1e-9:
        notes.append("slowed down uniformly x%.2f for velocity / acceleration limits" % s)
    bounds = [guardian.allowed_range(j) for j in joints]
    sq = [[min(hi, max(lo, v)) for v, (lo, hi) in zip(r, bounds)] for r in sq]
    total = st[-1] / dur
    info = {"changed": total > 1.0 + 1e-9 or max(diff) > 1e-6,
            "time_scale": round(total if total > 1.0 + 1e-9 else 1.0, 3),
            "max_shape_change_rad": round(max(diff), 4), "joints_changed": changed, "notes": notes}
    return st, sq, info




def make_playable(gen, mode="local"):
    """Branche l'adaptation sur un GestureGenerator chargé ; retourne gen.

    Après l'appel, gen.last_adaptation décrit ce qui a été fait au dernier
    geste (changed, time_scale, max_shape_change_rad, joints_changed, notes).
    """
    raw_gesture, raw_plan = gen.gesture, gen._plan
    gen.last_adaptation = None

    def plan(model, x, variation, seed):
        # Points de contrôle hors de la zone du guardian → point le plus proche dedans :
        # la courbe B-spline reste alors dans la zone (enveloppe convexe).
        ctrl, dur = raw_plan(model, x, variation, seed)
        ctrl, _, _ = _project_control_points(gen.guardian, ctrl, model.joints)
        return ctrl, dur

    def gesture(emotion, intensity, event=None, variation=0.0, seed=None):
        joints, tt, rows, extra = raw_gesture(emotion, intensity, event, variation, seed)
        tt, rows, info = _make_playable(gen.guardian, joints, tt, rows, mode=mode)
        gen.last_adaptation = info
        extra = dict(extra, notes=list(extra.get("notes") or []) + list(info.get("notes") or []))
        return joints, tt, rows, extra

    gen.gesture = gesture
    if hasattr(gen, "_plan"):
        gen._plan = plan
    return gen
