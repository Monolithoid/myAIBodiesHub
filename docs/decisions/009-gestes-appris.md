# ADR 009 — Learned gestures: generated on the static drone, played by the robot

**Date:** 2026-11-18  
**Status:** Accepted  
**Extends:** [ADR 008](008-univers-moteur-contraint.md)

## Context

The [nao-emotion-motion](https://github.com/Monolithoid/nao-emotion-motion) project
learns, on this NAO, emotional gestures from takes recorded by hand. It exports a
standalone generator (`nao_gesture.py` + `nao_gesture.json`, standard Python):
emotion + intensity + event (direction of a voice) → trajectory of the head, arms,
wrists and hands, validated by a "guardian" (zone, velocities, accelerations, rest ↔
start transitions). The generator is published in the public
`nao-emotion-motion-dataset` repository.

ADR 008 limits NAO to three chains (head, shoulders, elbows), with no wrist and no
hand, with "one target per chain" orders. The robot crashes easily: nothing heavy
must run on it.

## Decision

1. **Learned gestures may move wrists and hands**, on top of the three chains of ADR
   008, because they were learned and validated on this robot. The LLM's low-level
   orders (`motion`) stay within the ADR 008 universe.
2. **The generator runs on the static drone** (`emotion_gesture.py`), not on the
   robot nor on the VPS. The robot only reports its angles, plays a trajectory
   (`ALMotion.angleInterpolation`) and stops on `/nao/stop`.
3. **Version pinned by hand**: `GESTURE_DATASET_REF` (tag or commit) in
   `emotion_gesture.env` on the drone. No automatic tracking of `main`. The files are
   checked (sha256 from `dataset.json`) and kept per version; a refused version
   leaves the previous one in service.
4. **Automatic execution** as soon as the guardian accepts the gesture, without
   manual validation.
5. **Rest and start poses**: those of the nao-emotion-motion calibration. The
   control room's motor profile must copy them; a gap of more than 0.05 rad is
   reported each time a version is loaded.
6. **After a gesture**, the robot stays stiff in the start pose. Without a new
   gesture for `GESTURE_REST_DELAY` seconds, the drone sends the return to rest,
   also validated, and the robot relaxes at the end.
7. **Gestures are made playable before the guardian**, as in the nao-emotion-motion
   interface (`playable.py`). The exported generator does it itself since
   nao-emotion-motion#62; the drone does not redo this step and refuses an older
   generator at load time, with the reason.
8. **The NAO driver arbitrates**: it refuses a gesture during a posture, a walk or
   with a chain that is too hot, waits for the chains to finish their step, then
   reserves them for the drone as long as it holds the body (`owns_body`). If the
   drone stays silent too long, the driver takes control back and brings the chains
   back to rest through the start pose.

Split:

| Where | What |
|---|---|
| `body-gateway` | `gesture` action in the prompt (emotions known to the generator), intensity, voice direction (sign inverted) |
| NAO driver (`motion_policy.py`) | refusal (posture, walk, heat), chain reservation, takeover if the drone goes silent |
| static drone (`emotion_gesture.py`) | generator version, measured posture, generation and guardian, return to rest |
| bridge on the robot (`nao_ws_bridge.py`, `trajectory` module) | angles, trajectory check (joints, hardware ranges, start posture), playback, stop, relaxation |
| control room | Robot page: version in service, « Recharger » (reload) button, gesture test |

## Consequences

- A tag (or a commit) of the dataset must be published and written into
  `emotion_gesture.env` to change the model; nothing changes on its own.
- The bridge only checks learned gestures against the hardware ranges (minus a
  margin), not against the motor profile box: the generator's guardian is the real
  safety net, the ADR 008 box only applies to `motion` orders.
- Gestures are played at the learned speed, faster than the cap speed of `motion`
  orders (0.3): the guardian bounds velocities and accelerations.
- During a learned gesture, the head no longer follows the voice; the gesture
  orients itself towards the voice that was heard.
