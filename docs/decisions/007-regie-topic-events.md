# ADR 007 — Control room: body-gateway publishes its streams on the ROS topic `/events`

**Date:** 2026-10-26  
**Status:** Accepted

## Context

During a stream, we want to see live what passes between the components of the
[ADR 005](005-avatar-twitch.md) loop: which input arrived (Open WebUI, Twitch chat,
"ok nao", sensor, dream), what the LLM decided, and to which bodies each action was
sent (NAO, static drone, OBS voice). Today, this requires reading the
`body-gateway` logs.

Constraints: **open-source, self-hostable** tools only, and the ADR 005
architecture (`body-gateway` abstracts the LLM, ROS abstracts the bodies).

## Decision

### 1. A standard ROS topic: `/events`

`body-gateway` is the only component that sees the whole loop. It publishes on
`/events` (`std_msgs/String`, one JSON object per message):

| `kind` | When | Fields |
|---|---|---|
| `input` | an input triggers the LLM | `source` (`webui`, `twitch`, `voice`, `sensor`, `dream`), `body`, `text`, `author` |
| `ignored` | an input is dropped | `source`, `body`, `text`, `reason` (`wake_word`, `busy`) |
| `wake` | "ok nao" alone was heard, the next sentence will go to the LLM | `source`, `body`, `text`; `detector` (`local`), `model` and `score` when the static drone detected it itself |
| `decision` | the LLM answered | `thoughts`, `emotion`, `energy`, `actions`, `duration_s` |
| `action` | an action is routed | `type`, `requested`, `targets`, `connected`, `detail` |
| `error` | LLM or processing error | `message` |
| `bodies` | a body (dis)connects, and every 60 s | `connected` `{body: bool}`, `state` |

All carry `v` (format version), `t` (Unix time) and `id`: the input, the decision
and the actions of one interaction share the same `id`. The format is defined and
tested in `robotics/brain/body_gateway/events.py`.

The topic is a ROS topic like any other: `ros2 topic echo /events`, rqt or
[Lichtblick](https://github.com/lichtblick-suite/lichtblick) (open source, through
rosbridge) read it without changing anything in the core.

### 2. A home-made control room page: `regie` (`robotics/regie/`)

A small Python container, built on the `obs-agent` model:

- it connects **outbound** to `rosbridge-proxy` with `ROSBRIDGE_TOKEN`, and only
  subscribes (`/events`, `/cognitive_state`, `/emotion_state`);
- it keeps the last 300 events, for a page opened midway;
- it serves `https://regie.$DOMAIN/?token=$REGIE_TOKEN`: an inputs → body-gateway →
  bodies diagram animated on each event, the brain state, and the list of
  interactions (input, LLM thought, actions and targeted bodies, disconnected bodies
  in orange, ignored inputs) with a filter by source;
- the page receives the events as Server-Sent Events, in HTML and JavaScript with no
  external library.

The browser cannot send the `Authorization` header required by `rosbridge-proxy`
on a WebSocket: that is why the page goes through this container instead of talking
to rosbridge directly.

## Alternatives rejected

| Option | Why |
|---|---|
| Foxglove Studio | Proprietary tool since v2: rejected, open source only |
| Lichtblick alone | Sees the bodies' topics, but not the input or the LLM decision without `/events`; can still be plugged in alongside the control room |
| Grafana + Loki / OpenTelemetry | Robust for history, but several more services on the VPS for a live monitoring need |
| Each component publishes its own events | Text inputs do not go through ROS (ADR 005); `body-gateway` already sees everything |
| The page talks to rosbridge directly | No `Authorization` header possible from a browser; rosbridge would have to be opened without a token |

## What is lost

- **History**: only the last 300 events are kept, in memory. A restart of `regie`
  clears them.
- **Room privacy**: the static drone hears everything; sentences without the wake
  word appear in the control room (`ignored`). The page is protected by
  `REGIE_TOKEN`.

## What is kept

- The **outbound connection pattern** of ADR 002 / 004 / 005.
- A **standard ROS** format: the visualisation tool stays interchangeable.

---

← [All decisions](../../README.md#architecture-decisions-adr) · [ADR 005](005-avatar-twitch.md)
