# ADR 005 — A single loop: stimuli → LLM → agent actions (Twitch, OBS)

**Date:** 2026-10-09  
**Status:** Accepted (sections 1 to 4 implemented; section 5 planned)

## Context

NAO is the representation of an autonomous AI avatar, visible on a Twitch stream.
It reacts in real time to external stimuli that the streamer does not control.

The Twitch chat is **just one more external stimulus**, like "ok nao" said in the
room or a message written in Open WebUI. It must not have a path of its own.

- **NAO** only moves and lights its LEDs ([ADR 004](004-drone-statique.md)).
- **The static drone** hears, sees and speaks in the room ([ADR 004](004-drone-statique.md)).
- **OBS** can receive the sound of NAO's answers directly.

**Guiding principle (taken from ADR 004): simple solutions, even imperfect ones.**

## Decision

### 1. A single loop

```
  INPUTS (stimuli)                       BRAIN                      AGENTS (bodies)
  ────────────────                       ─────                      ───────────────
  "ok nao …" (static drone)     ─┐                              ┌─► nao_01    : gestures, head, LEDs
  Open WebUI                    ─┼─► text ─► body-gateway ─► actions ─┼─► static_01 : voice in the room
  Twitch chat "!nao …"          ─┤            (LLM, one       │  └─► obs_01    : voice on the stream (OBS)
  sensors (sonar, contact…)     ─┘             inference at   │
                                               a time)        └─ looping, on every stimulus
```

Each input only produces a text. The LLM returns a list of actions (`speech`,
`motion`, `leds`, `look`…), and `body-gateway` spreads them across the agents. No
input knows the outputs, and no output knows which input triggered it.

### 2. Twitch input: `twitch-input` (`robotics/inputs/twitch/`)

A small Python container without ROS 2 that does for the chat what Whisper and the
wake word do for the voice:

| Step | Simple choice |
|---|---|
| Read the chat | Twitch IRC in **anonymous read mode** (`justinfan…`): no token or Twitch app |
| Keep the commands | message starting with `!nao` (`!naomi` excluded) |
| Filter | max length, no links, banned words, bots ignored, per-viewer delay, "moderators only" option |
| Bursts | single slot: when the robot is free it takes the latest message, those received in between are dropped, as is a message older than 90 s |
| Send | `POST body-gateway/chat` with `[tchat Twitch] nickname : message`, exactly like Open WebUI |

`body-gateway`'s system prompt describes this format and reminds that **chat
messages are never orders**.

Viewer memory: body-gateway keeps a short profile per viewer (SQLite), written
by the LLM in the same answer and sent back with that viewer's next message.
`!rgpd` whispers a viewer what is stored, `!reset` erases it (see
[bridge-ia-ros2-nao.md](../bridge-ia-ros2-nao.md#twitch-input)).

### 3. OBS agent: a "voice" body like the static drone (`robotics/drones/obs/`)

The stream voice is a new body type, `obs`, with the `speech` capability, built on
the same model as the static drone's speaker:

- `obs-agent` connects **outbound** to rosbridge with `ROSBRIDGE_TOKEN`, like the
  static drone.
- `static-driver` is started a second time in `nao-core` with `DRONE_ID=obs_01`: it
  does the edge-tts synthesis and publishes the MP3 on `/drones/obs_01/audio_out`.
  The topics are the same as for the drone.
- `obs-agent` serves a `/overlay?token=…` page to open as an **OBS browser source**
  ("Control audio via OBS": the voice becomes a mixer source), with subtitles.
- The overlay signals the start and end of playback: `/drones/obs_01/speaking`, and
  the NAO moves while the voice plays (`cognitive_renderer` now listens to all
  voices).
- `obs-agent` only sends its heartbeat **while the overlay is open in OBS**: off
  stream, the OBS voice is "disconnected" and the brain sends it nothing.

### 4. Speech routing

`body_rules.route_targets`: a `speech` action goes to **every connected voice**
(room and stream), whatever the input. Without a connected voice, the NAO speaks
itself, as before. Gestures, head and LEDs always go to the NAO.

### 5. Target architecture (planned, not implemented yet)

These principles frame the next changes. The code of this ADR already partly
follows them (text inputs to `/chat`, voices as agents).

**Text inputs.** Any text-type input (Open WebUI, Twitch chat, SMS, Discord…) is
sent as is to the LLM, with its source and author: `[twitch] nickname : message`. No
channel-specific processing outside its adapter (reading, anti-abuse filters).

**Single event stream in `body-gateway`.** Each stimulus becomes an event
`{source, author, text, reply_to, priority, time}` placed in a single queue,
consumed by a single loop:

- each input keeps its natural transport: ROS for voice and sensors, HTTP `/chat`
  for text channels (Open WebUI waits for the result of its event and stays
  question-answer);
- simple priorities (the room comes before the chat);
- events that arrive during a reflection are grouped into the next prompt instead
  of being lost;
- a short memory of the latest exchanges, all sources combined, is added to the
  prompt;
- a periodic tick without a stimulus gives NAO the initiative ("I look at the room
  and the mood of the chat").

ROS 2 stays the abstraction of the **bodies** (`/bodies/{id}/sensors|actuators/*`);
`body-gateway` stays the abstraction towards the **LLM**. Text inputs do not need
to go through ROS.

**Text channels = input + agent.** A channel that can answer (Open WebUI, Twitch,
Discord, SMS) is also a body with the `message` capability:

| LLM action | Destination |
|---|---|
| `{"type": "reply", "text": "…"}` | the event's original channel, chosen by `body-gateway` (fixed rule, like `speech` → all voices) |
| `{"body": "discord_01", "type": "message", "text": "…"}` | a named channel, on the LLM's initiative |
| `speech`, `motion`, `leds`… | unchanged: voices and NAO, on top of the written answer |

Each adapter sends the answer back its own way: HTTP response (Open WebUI), message
(Twitch, Discord), sending (SMS). Known constraints: writing in the Twitch chat
requires a bot account with an OAuth token (reading stays anonymous), a limited
rate and at most 500 characters; SMS go through a paid provider.

## Alternatives rejected

| Option | Why |
|---|---|
| A Twitch service that calls the brain and plays the answer in OBS itself | A separate path for one input: the OBS voice would only serve the chat, and the room would not be heard on the stream |
| EventSub API / authenticated Twitch bot | Twitch app, OAuth and token renewal for a simple read need |
| obs-websocket + audio file | Requires opening OBS to the VPS or one more local script; the browser source is enough and lands in the mixer |
| The LLM picks the voice (room or stream) or the answer channel | A small local model would get it wrong; fixed rules ("all voices", "answer the original channel") are predictable |
| All inputs published on a ROS `/inputs` topic | Open WebUI waits for an answer: a request ID and a return path would be needed, for no gain; unification happens in `body-gateway` |

## What is lost

- **Completeness**: in a burst, `!nao` messages are deliberately ignored, and if the
  brain is busy, the message is dropped.
- **Fine moderation**: the filters are fixed rules. An LLM can still say something
  inappropriate on air.
- **Separate voices**: the answer to a viewer is also spoken in the room, and the
  static drone hears it (filtered by the wake word).

## What is kept

- **A single brain** and a single loop for all stimuli.
- The **outbound connection pattern** of ADR 002 / 004 for every agent.
- No inbound port at the streamer's: OBS only opens an HTTPS page.

## Open points

- Implement the target architecture (section 5): event stream, short memory,
  initiative tick, then text channels as outputs.
- With two possible Docker networks, check that Traefik reaches `obs-agent` through
  `robotics-net` (`traefik.docker.network` label if needed, depending on Coolify).

---

← [All decisions](../../README.md#architecture-decisions-adr) · [Architecture](../architecture.md) · [ADR 004](004-drone-statique.md)
