# ADR 004 — Raspberry Pi static drone for the senses, NAO reduced to emotional rendering

**Date:** 2026-10-08  
**Status:** Accepted

## Context

The NAO is limited as a sensory organ: video capture, audio capture and audio
output are of poor quality. Its lower motors (legs) are already disabled. It
remains expressive, however: head, arms and LEDs are enough to convey an emotional
state.

Another source is therefore needed to hear, see and speak in the room, without
making the architecture heavier.

**Guiding principle: simple solutions, even imperfect ones.** A building block that
works right away is worth more than a precise one that will need calibrating.

## Decision

### 1. Split of roles

- **NAO** keeps **only the motor emotional rendering**: head, arms, LEDs.
- **New drone type `static`**: a **Raspberry Pi 5** placed in the same room handles
  **audio capture**, **video capture** and **audio output**.
- The Pi connects **outbound** to the VPS through rosbridge, on the same principle
  as the NAO ([ADR 002](002-nao-websocket-bridge.md)), with the `ROSBRIDGE_TOKEN`
  token (`Authorization: Bearer` header, checked by `rosbridge-proxy`, PR #102).

### 2. Tree

```
robotics/
├── brain/            → shared by all drones (body_gateway, ros2_bridge, webui)
└── drones/
    ├── nao/          → motor emotional rendering
    ├── static/       → Raspberry Pi 5: microphones, camera, speaker
    └── quadcopter/   → reserved
```

### 3. Pi / VPS work split

| Raspberry Pi (light) | VPS (heavy) |
|---|---|
| Audio and video capture | Transcription (Whisper STT) |
| Speech detection | Speech synthesis (edge-tts) |
| Sound direction | LLM |
| Audio playback | Image analysis |

### 4. Sound direction

Common origin: **the camera axis = angle 0**. The method is a fixed rule, chosen
according to the connected hardware:

```
if ReSpeaker USB 4 mics    → DOAANGLE angle read from the device
                             (360°, ~10-20° accuracy, echo cancellation if the
                              speaker is plugged into the ReSpeaker's jack output)
else if n ≥ 2 mics         → angle of the loudest microphone
                             (angles declared in the config, e.g. MICS="gauche:-60,droite:60")
else (1 mic)               → angle of the motion in the image
                             (difference with the previous image, mean x of the changed pixels:
                              angle = (x / width − 0.5) × field_of_view)
```

An optional `DOA_OFFSET` corrects the sensor's real orientation relative to the
camera.

## Alternatives rejected

| Option | Why |
|---|---|
| Home-made GCC-PHAT / SRP-PHAT computation | Too complex for the gain: the ReSpeaker already provides an angle, and the "loudest mic" case is enough otherwise |
| Audio-vision fusion with learned calibration | Calibration to maintain, marginal gain for a single room |
| Active speaker detection from the lips | Heavy model, depends on lighting and framing, too complex for the gain |

## What is lost

- **NAO sensors**: its microphones, camera and speaker are no longer used. The
  `/nao/audio` and `/nao/audio_out` topics eventually become obsolete in favour of
  the `static` drone's topics.
- **Direction accuracy**: with a single microphone, **motion** is followed, not the
  speaker. The camera's field (~70°) bounds the measurable angle, and only one
  source is followed at a time.
- **Physical autonomy**: perception depends on a second device in the room, no
  longer a single robot.

## What is kept

- The **NAO as an expressive body**: head, arms, LEDs (`cognitive_renderer`,
  `motion_engine`).
- The **outbound connection pattern** of ADR 002: no inbound firewall rule on the
  robot or the Pi.
- The **shared brain** (`robotics/brain/`), independent of the drone type.
- **Heavy processing on the VPS**: the Pi can be replaced by any small computer.

## Consequences

- A new body identifier `static` is to be added to `body-gateway`'s
  `BODY_REGISTRY`, with its own topics (incoming audio, image, direction angle,
  outgoing audio).
- The Pi needs `ROSBRIDGE_TOKEN` in its configuration, like the NAO.
- The Pi configuration declares the hardware: presence of a ReSpeaker, `MICS` list,
  camera field of view, `DOA_OFFSET`.
- No code ships with this ADR; `robotics/drones/static/` will be filled in a later
  piece of work.

---

← [All decisions](../../README.md#architecture-decisions-adr) · [Architecture](../architecture.md) · [NAO robot](../nao.md)
