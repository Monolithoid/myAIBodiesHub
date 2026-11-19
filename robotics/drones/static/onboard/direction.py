"""
direction.py — Sound direction for the static drone (ADR 004, §4).

Pure functions (numpy only), testable without hardware.

Common convention: the camera axis is 0°, a positive angle is to the right of
the camera, a negative angle to the left, in the interval ]-180, 180].
"""

import numpy as np

# direction_source values published on /drones/{id}/audio
SOURCE_RESPEAKER = "respeaker"
SOURCE_LOUDEST_MIC = "loudest_mic"
SOURCE_IMAGE = "image"
SOURCE_NONE = "none"


def choose_direction_source(has_respeaker, n_mics, has_camera):
    """Fixed rule from ADR 004: ReSpeaker, otherwise loudest mic, otherwise image."""
    if has_respeaker:
        return SOURCE_RESPEAKER
    if n_mics >= 2:
        return SOURCE_LOUDEST_MIC
    if has_camera:
        return SOURCE_IMAGE
    return SOURCE_NONE


def normalize_angle(deg):
    """Brings an angle back into ]-180, 180]."""
    a = (float(deg) + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def parse_mics(spec):
    """Reads MICS="gauche:-60,droite:60" → [("gauche", -60.0), ("droite", 60.0)].

    The list order is the order of the audio input channels.
    """
    mics = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, angle = item.rpartition(":")
        if not sep or not name:
            raise ValueError("Invalid MICS entry (expected name:angle): {!r}".format(item))
        mics.append((name.strip(), float(angle)))
    return mics


def channel_rms(frames):
    """RMS per channel of a block (samples × channels), as float."""
    x = np.asarray(frames, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[0] == 0:
        return np.zeros(x.shape[1])
    return np.sqrt(np.mean(x * x, axis=0))


def loudest_mic(levels, angles):
    """Angle of the mic with the highest level (RMS or energy).

    levels: one level per mic, in the order of `angles`.
    Returns None if all levels are zero.
    """
    levels = np.asarray(levels, dtype=np.float64)
    if len(levels) != len(angles):
        raise ValueError("{} levels for {} mics".format(len(levels), len(angles)))
    if len(levels) == 0 or not np.any(levels > 0):
        return None
    return normalize_angle(angles[int(np.argmax(levels))])


def respeaker_angle(doa_deg, offset_deg=0.0, invert=False):
    """Converts DOAANGLE (0..359, ReSpeaker frame) to the camera frame."""
    a = -float(doa_deg) if invert else float(doa_deg)
    return normalize_angle(a + offset_deg)


def circular_mean_deg(angles):
    """Circular mean of a list of angles in degrees (None if empty)."""
    if len(angles) == 0:
        return None
    r = np.radians(np.asarray(angles, dtype=np.float64))
    return normalize_angle(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())))


def motion_angle(prev_gray, curr_gray, hfov_deg, threshold=25, min_fraction=0.002):
    """Angle of the motion between two greyscale images (ADR 004).

    Difference with the previous image, mean x of the changed pixels:
        angle = (x / width − 0.5) × field_of_view
    Returns None if less than `min_fraction` of the pixels changed.
    """
    prev = np.asarray(prev_gray, dtype=np.int16)
    curr = np.asarray(curr_gray, dtype=np.int16)
    if prev.shape != curr.shape:
        return None
    changed = np.abs(curr - prev) > threshold
    count = int(changed.sum())
    if count == 0 or count < min_fraction * changed.size:
        return None
    width = changed.shape[1]
    xs = np.nonzero(changed)[1]
    # Pixel centres: (x + 0.5) so that a symmetrical image gives 0°.
    x_mean = xs.mean() + 0.5
    return float((x_mean / width - 0.5) * hfov_deg)
