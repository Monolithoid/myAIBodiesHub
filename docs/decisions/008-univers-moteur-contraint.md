# ADR 008 — Constrained motor universe: motor profile calibrated from the control room

**Date:** 2026-10-29  
**Status:** Accepted

## Context

NAOqi's motor management crashes as soon as a movement is out of the ordinary,
without a clear reason. Until now a gesture was a list of target angles: presets
(`wave` set both shoulders to -1.0), free angles from `motion_engine` at 20 Hz, arm
poses from `cognitive_renderer`, return to the launch pose. Nothing guaranteed that
these angles and paths were safe for this robot.

## Decision

We arbitrarily define a stable, strongly constrained motor universe.

1. **NAO stays seated.** No posture change, no walking.
2. **Three chains**: the head, the left arm and the right arm (shoulder and elbow,
   no wrist and no hand). No dancing, no waving with the hand.
3. **One motor profile per robot**, `nao_motor_profile.json` on the robot: for each
   chain, a rest position, a start position, a range per joint (which contains the
   start) and a speed.
4. **Every gesture goes rest → start → target → start → rest.** A new order during a
   gesture chains target → start → new target. The start → target path stays within
   the range, since the range is a box that contains the start.
5. **The LLM gives a low-level order**: the position of one or more joints, within
   the ranges shown to it. It knows neither the rest nor the start pose.
6. **Calibration is done from the control room** (`/robot` page), chain by chain:
   stiffness cut to place the chain by hand, capture of the rest and start poses,
   recording of the ranges, speed, validation replay, then writing. It runs on the
   robot, without the VPS latency.
7. **As many NAOqi components as possible are turned off** (fall manager, collision
   avoidance, breathing, idle posture, awareness…), and each can be turned back on
   from the control room. The fall manager and the arms' collision avoidance are a
   likely source of crashes.

Split, in line with "the robot is a puppet":

| Where | What |
|---|---|
| NAO driver (`motion_policy.py`) | sequences per chain, grouping of orders, return to rest, heat |
| bridge on the robot (`nao_ws_bridge.py`) | profile, last-resort bounds (profile box), rest at startup, calibration, components |
| `body-gateway` | ranges in the prompt, low-level orders |
| control room | Robot page: calibration and components (the only page that writes) |

## Consequences

- Without a profile, NAO does not move: each chain must be calibrated once.
- `motion_engine` and the arms of `cognitive_renderer` are off by default
  (`MOTION_ENGINE=1`, `RENDERER_ARMS=1` to bring them back).
- Less spontaneity: variety comes from the LLM's choice of targets. Tactile feedback
  (unlocking a movement, saying "good" or "not good") and learning from
  demonstration can be added later on this foundation.

Update 2026-11: `motion_engine` removed from the code and the `nao-core` image (see PR #…).
