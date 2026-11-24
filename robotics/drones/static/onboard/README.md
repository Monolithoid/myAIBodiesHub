# Static drone — onboard program

Python 3 program that turns a small Linux computer placed in the room into the
robot's **ears, eyes and voice** ([ADR 004](../../../../docs/decisions/004-drone-statique.md)).
It opens an **outbound** WebSocket connection to rosbridge on the VPS, like the NAO
([ADR 002](../../../../docs/decisions/002-nao-websocket-bridge.md)): no port to open
at home.

| File | Role |
|---|---|
| `static_drone.py` | Main program |
| `direction.py` | Sound direction computation (numpy only, testable without hardware) |
| `wakeword.py` | Local wake word with openWakeWord (`WAKE_MODE=local`) |
| [`models/`](models/README.md) | "ok nao" model: where to put it, how to train it (Colab notebook) |
| `tests/test_direction.py` | Unit tests for `direction.py` |
| `.env.example` | Example configuration, to copy to `.env` |
| `requirements.txt` | Python dependencies |
| `requirements-wakeword.txt` | openWakeWord, to install with `--no-deps` |
| `static-drone.service` | systemd service, for automatic start later |
| `static-drone-user.service` | systemd user service, for a desktop Ubuntu PC (access to the session's sound) |
| `tests/test_camera.py` | Unit tests for the camera choice and the audio sample rate |
| `tests/test_wakeword.py` | Unit tests for the wake word detector (without openWakeWord) |
| [`pc-portable-ubuntu.md`](pc-portable-ubuntu.md) | **Installation on an Ubuntu laptop** (webcam + microphone) |
| `setup-ubuntu.sh` | Automatic installation on Ubuntu (idempotent), see [pc-portable-ubuntu.md](pc-portable-ubuntu.md#automatic-installation) |
| `setup-lib.sh` | Functions shared by the installation scripts |
| `tmux.sh` | Runs the NAO relay, `static_drone.py` and `emotion_gesture.py` in one tmux session, panes stacked vertically: `bash tmux.sh [--kill] [session-name]` (default `static-drone`; `--kill` stops it) |
| `nao-relay/` | NAO relay: the NAO goes through the drone to reach the VPS ([installation](nao-relay/README.md), [ADR 006](../../../../docs/decisions/006-nao-via-drone-statique.md)) |
| `emotion_gesture.py` | Learned NAO gestures: generates the emotional gestures on the drone and sends them to the robot (see [Learned gestures](#learned-nao-gestures)) |
| `emotion_gesture.env.example` | Generator version to use, to copy to `emotion_gesture.env` |
| `emotion-gesture-user.service` | systemd user service for the gesture generator |
| `tests/test_emotion_gesture.py` | Tests for the gesture service (no network, no robot) |
| `tests/test_speech_sync.py` | Tests for the sentence held back until the gesture starts |

## Topics

`{id}` is `DRONE_ID` (default `static_01`).

| Topic | Direction | Type | Content |
|---|---|---|---|
| `/drones/{id}/status` | → VPS | `std_msgs/String` | JSON every 30 s: `capabilities`, `mics`, `respeaker`, `aec`, `direction_mode`, `wake_mode`, `camera_hfov_deg`, `speaking`, `uptime_s` |
| `/drones/{id}/audio` | → VPS | `std_msgs/String` | JSON `{"wav": base64, "azimuth_deg": angle or null, "direction_source": "respeaker\|loudest_mic\|image\|none"}`, plus `"wake": "local"` and `"wake_score"` for the sentence following a wake word detected on the drone |
| `/drones/{id}/wake` | → VPS | `std_msgs/String` | `WAKE_MODE=local`: JSON `{"model", "score", "threshold"}` as soon as "ok nao" is heard |
| `/drones/{id}/audio_out` | ← VPS | `std_msgs/String` | base64 MP3 (like `/nao/audio_out`), or JSON `{"mp3", "sync", "speech_s"}` for a sentence waiting for a NAO gesture ([synchronisation](#speech--gesture-synchronisation)) |
| `/speech_gesture_sync` | ← drone, VPS | `std_msgs/String` | `{"kind": "go", "sync", "at", "in_s"}`: start time of sentence `sync` (0 = right away); `in_s` = the same as a delay, for the OBS voice |
| `/drones/{id}/speaking` | → VPS | `std_msgs/Bool` | `true` at the start of playback, `false` at the end |
| `/drones/{id}/event` | → VPS, gesture service | `std_msgs/String` | `{"kind": "sound"\|"motion", "azimuth_deg"}`: a sound segment heard, or a movement starting after `MOTION_EVENT_QUIET_S` quiet seconds ([reflex](#reflex)) |
| `/drones/{id}/image` | → VPS | `std_msgs/String` | JSON `{"jpeg": base64, "width", "height", "reason": "periodic\|request"}` |
| `/drones/{id}/image_request` | ← VPS | `std_msgs/Empty` | Requests an image right away |

Angles are in degrees: **0 = camera axis, positive = to the right**, between -180
and 180.

## Learned NAO gestures

`emotion_gesture.py` runs, on the drone, the gesture generator learned by
[nao-emotion-motion](https://github.com/Monolithoid/nao-emotion-motion). The robot
only plays the trajectory it receives (`trajectory` module of `nao_ws_bridge.py`),
nothing heavy runs on the NAO.

**Model version.** The generator (`generator/nao_gesture.py` +
`generator/nao_gesture.json`) comes from the public `nao-emotion-motion-dataset`
repository, at the version written **by hand** in `emotion_gesture.env`:

```bash
cp emotion_gesture.env.example emotion_gesture.env
# GESTURE_DATASET_REF=v0.0.4 by default; change it for another tag or a full commit
```

The service never follows `main` on its own. The files are checked (sha256 from
`dataset.json`) and cached per version. To change the model: edit
`GESTURE_DATASET_REF`, then publish `{"op": "reload"}` on
`/nao/emotion_gesture_control` (control room button) or restart the service. If the
new version is refused (invalid files, no gesture accepted from the start pose), the
old one stays in service.

**Required generator.** The generator must make gestures playable before the
guardian, like the nao-emotion-motion interface (`playable.py`): this is the case
for exports made with nao-emotion-motion#62 or later. The drone does not redo this
step. An older dataset version is refused at load time, with the reason in
`/nao/emotion_gesture_state` (visible in the control room), and the old version
stays in service. A gesture refused by the guardian is retried without the event,
then with a small variation (the `retry` field says so); otherwise nothing is
played.

**Starting it.**

```bash
python3 emotion_gesture.py                 # by hand
cp emotion-gesture-user.service ~/.config/systemd/user/
systemctl --user enable --now emotion-gesture-user
```

The rosbridge connection is read from the drone's `.env`. Only numpy is needed
(already in `requirements.txt`).

**Topics.**

| Topic | Direction | Content |
|---|---|---|
| `/nao/emotion_gesture` | ← driver, control room | `{"id", "emotion", "intensity", "event", "sync"}`; `sync` = `{"id", "speech_s", "speech_s_first"}` when a sentence goes with the gesture; `event` = `null` or `{"azimuth_deg", "elevation_deg", "distance_m"}` in the robot's torso frame (azimuth positive = to the left, the opposite of the drone) |
| `/nao/emotion_gesture_control` | ← control room | `{"op": "reload"}`: rereads `emotion_gesture.env`; `{"op": "speech_offset", "ms": 150}`: speech / gesture offset, live |
| `/speech_gesture_sync` | ↔ | `{"kind": "speech", "sync", "speech_s"}` ← VPS (measured sentence duration); `{"kind": "go", "sync", "at", "in_s"}` → drone speaker and OBS voice |
| `/nao/read_angles` | → robot | `{"id"}`: asks for the measured posture |
| `/nao/joint_angles` | ← robot | `{"id", "angles": {joint: rad}}` |
| `/nao/trajectory` | → robot | `{"id", "names", "angles", "times", "start", "relax_end"}` |
| `/nao/trajectory_state` | ← robot | `{"id", "state": "playing\|done\|stopped\|refused\|error", "reason"}` |
| `/nao/emotion_gesture_state` | → VPS | state of each gesture (`sent`, `refused`, `replaced`, `robot_playing`, `robot_done`…), the version in service (`version_loaded`, `version_refused`), and `owns_body` / `at_rest`: the NAO driver does not move the head or the arms while `owns_body` is true |

After a gesture, the robot stays stiff in the start pose. Without a new gesture for
`GESTURE_REST_DELAY` seconds (3 by default), the drone sends the return to rest,
also validated by the guardian, and the robot relaxes at the end. That segment is
built by the generator itself (`rest_segment()`), so it moves as fast as the
dataset version allows: about 0.4 s at the factory limits (dataset exported with
`rest_velocity_rad_s: factory`), about 5.6 s with older versions. A request that
arrives during a gesture waits for it to finish; a more recent request replaces the
one that was waiting.

### Speech / gesture synchronisation

Before, the sentence went out as soon as the speech synthesis was ready, while the
gesture still had to go through the NAO driver, the posture measurement, the
generation and above all the robot's move to its start pose (more than 5 s from
rest): speech left well before the gesture, which also lasted a different time.

When an LLM answer contains a sentence spoken by the drone and a gesture,
`body_gateway` gives them the same id (`sync`):

```
body_gateway ── say {"text", "sync"} ──► static_driver_node: edge-tts, measured MP3 duration
             │                              ├─ /speech_gesture_sync {"kind": "speech", "speech_s"}
             │                              └─ /drones/{id}/audio_out {"mp3", "sync"} ─► static_drone.py
             │                                                                (sentence held back)
             └─ gesture {"…", "sync"} ──► NAO driver ──► emotion_gesture.py
                                          1. waits for the measured sentence duration
                                          2. generate(..., duration_s=that duration): one movement
                                          3. /nao/trajectory → robot
                                             /speech_gesture_sync {"kind": "go", "at"} ─► the sentence starts
```

1. **Duration**: the drone asks the generator for one movement as long as the speech
   (`generate(..., duration_s=speech_s)`). The generator blends several learned
   gestures into it and fits their tempo within a margin set by the intensity; the
   `gesture` segment lasts about the speech, the moves from and back to the start pose
   come around it, and the whole is capped at the guardian maximum duration. The drone
   no longer stretches or tightens anything: the movement is played as generated. The
   `sent` state reports `draws` (learned gestures blended) and `time_scale` (the
   generator's tempo factor). `GESTURE_MATCH_SPEECH=0` asks for a single learned gesture
   instead. The generator must accept `duration_s` (dataset `v0.0.4` or later).
2. **Start**: the sentence starts at the beginning of the expressive part, plus
   `GESTURE_SPEECH_OFFSET_MS` (`emotion_gesture.env`): **positive = speech after the
   gesture, negative = before**. It makes up for what the drone cannot see (network to
   the robot, `angleInterpolation` start-up, speaker latency): tune it by eye and ear,
   live from the control room (Robot page, « décalage parole / geste »), then copy it
   into `emotion_gesture.env` to keep it.
3. **Without a gesture** (refused by the driver or the guardian, replaced, service
   stopped): the sentence starts right away, or after `SPEECH_SYNC_MAX_WAIT_S` seconds
   (drone `.env`, 4 by default).

The stream voice (OBS overlay: voice and caption) waits for the gesture the same
way: `obs_agent` holds the sentence until the same start, plus `OBS_SPEECH_OFFSET_MS`
(its own environment, 0 by default) to match the camera latency on the stream. As its
clock is not the drone's, it uses `in_s` (delay from the moment the message is sent).
`SPEECH_GESTURE_SYNC=0` in `body_gateway`'s environment turns the whole mechanism off.

### Variety (CVAE generator)

When the pinned generator has CVAE models (`latent_dims` in `nao_gesture.py`), each
gesture draws a new latent code: the same emotion and intensity give a different
learned gesture every time, for LLM gestures and reflexes alike. If the guardian
refuses the draw, the service tries the same draw without the event, another draw,
then the central gesture. `GESTURE_LATENT_TEMPERATURE` (`emotion_gesture.env`) sets
how far the draws go: empty = the generator's own default, `0` = always the central
gesture. The draw (`latent`, `temperature`) is reported in `/nao/emotion_gesture_state`.

### Reflex

The gesture service also reacts, without the LLM, to what happens in front of
the NAO. `static_drone.py` publishes each sound segment it hears and each
movement the camera starts to see on `/drones/{id}/event`, with its direction.
When the robot is at rest and calm for `GESTURE_REFLEX_COOLDOWN_S` seconds, the
service asks the NAO driver (`/bodies/{nao}/actuators/gesture`, `"reflex": true`)
for a short gesture of the generator toward the event: `GESTURE_REFLEX_SOUND`
(`surprise:0.3`) or `GESTURE_REFLEX_MOTION` (`calm:0.3`). The driver checks heat,
posture and walk as for any gesture.

- A gesture asked by the LLM stops a reflex in progress (`/nao/stop`) and starts
  from the measured posture; a reflex never replaces nor delays an LLM gesture.
- With `LLM_MOTION=0` on the VPS (`body_gateway`), the LLM no longer sends
  `motion` / `look` and `body_gateway` no longer turns the head toward each voice:
  NAO only moves through learned gestures, by reflex or asked by the LLM (which
  gets the voice direction as its `event`). In `nao-core` the same variable
  (`RAW_MOTION` of the NAO driver) drops every other joint order and stiffness
  change, cognitive_renderer's head animations included; the LEDs stay.
- `GESTURE_REFLEX=0` turns the reflex off.

## Sound direction

Fixed rule from ADR 004, chosen at startup according to the hardware:

1. **ReSpeaker USB 4 mics** plugged in → `respeaker`: average of the `DOAANGLE`
   values read during speech, plus `DOA_OFFSET`.
2. Otherwise, **at least 2 microphones** declared in `MICS` → `loudest_mic`: angle of
   the microphone with the highest energy during speech.
3. Otherwise, **webcam** → `image`: mean x of the pixels that moved during speech,
   `angle = (x / width − 0.5) × CAMERA_HFOV`.
4. Otherwise → `none` (`azimuth_deg` is `null`).

If the method gives nothing for a sentence (no motion, for example), the message is
still sent with `direction_source: "none"`.

## Hardware

- An **Ubuntu PC** (22.04 or later) or a **Raspberry Pi 5** running Raspberry Pi OS
  64-bit (Bookworm).
- To hear, one of the two:
  - a **ReSpeaker USB Mic Array v2.0** (4 mics, built-in speech detection and
    direction);
  - one or more **USB microphones**. For loudest-mic direction, the microphones must
    show up as **channels of the same input** (USB stereo mic, small 2-input audio
    interface…).
- A **USB webcam**.
- A **speaker**: on the ReSpeaker's jack output (echo cancellation), or on the audio
  output / a USB speaker.

## Step-by-step installation

On an **Ubuntu laptop** with a webcam and a microphone, follow
[pc-portable-ubuntu.md](pc-portable-ubuntu.md) instead (microphone and webcam
choice, user service, sleep). The `setup-ubuntu.sh` script does that installation
automatically.

### 1. System

On the Pi, install Raspberry Pi OS 64-bit with Raspberry Pi Imager and enable SSH.
Then, on the Pi or the Ubuntu PC:

```bash
sudo apt update
sudo apt install -y git python3-venv libportaudio2 libusb-1.0-0 mpg123 alsa-utils
```

### 2. Code

```bash
cd ~
git clone https://github.com/Monolithoid/ia-agent.git
cd ia-agent/robotics/drones/static/onboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps -r requirements-wakeword.txt
```

openWakeWord is installed without its dependencies (already in `requirements.txt`):
otherwise pip asks for `tflite-runtime`, which cannot be found for Python 3.12
(Ubuntu 24.04), and falls back to an old version.

Raspberry Pi OS forbids `pip install` outside a virtual environment: use `.venv` as
above.

### 3. Hardware permissions

```bash
sudo usermod -aG audio,video,plugdev $USER
```

For the program to read the ReSpeaker without `sudo`, add a udev rule (USB IDs
`2886:0018`, those of Seeed's `tuning.py`):

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="0018", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/60-respeaker.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Log out and back in (or reboot) for the groups to apply.

### 4. Check the hardware

```bash
lsusb                                   # the ReSpeaker shows up as 2886:0018
arecord -l                              # audio inputs
aplay -l                                # audio outputs
.venv/bin/python -m sounddevice         # inputs seen by the program
ls /dev/video*                          # webcam
mpg123 -q some_file.mp3                 # speaker test
```

The ReSpeaker exists with two firmwares: 1 channel (processed audio) or 6 channels
(channel 0 processed, channels 1 to 4 raw, channel 5 playback). Both work: the
program records channel 0.

### 5. Configure

```bash
cp .env.example .env
nano .env
```

To fill in at least:

- `ROSBRIDGE_URL` (e.g. `wss://rosbridge.mydomain.com`) or `DOMAIN`;
- `ROSBRIDGE_TOKEN`: the same value as on the VPS.

Depending on the hardware:

- **ReSpeaker**: nothing to do, it is detected. If the speaker is on its jack output,
  set `RESPEAKER_AEC=1` and send playback to it, for example
  `PLAYER_CMD=mpg123 -q -a hw:ArrayUAC10,0 -` (card name given by `aplay -l`).
- **Several microphones**: `MICS=gauche:-60,droite:60` (one `name:angle` per channel,
  in channel order) and, if needed, `AUDIO_INPUT_DEVICE` with the name or index shown
  by `python -m sounddevice`. If the input refuses `SAMPLE_RATE` (16 kHz), its
  default rate is used.
- **Camera**: `CAMERA_INDEX` = index, path (`/dev/video2`) or partial name shown by
  `v4l2-ctl --list-devices`; `CAMERA_HFOV` = horizontal field of view of the webcam
  (default 66°).

### 6. Run by hand

```bash
.venv/bin/python static_drone.py
```

The log must show the chosen direction method, then `Connected to wss://…`. Speak in
front of the microphones: an `Audio sent: … angle … (…)` line appears for each
sentence. `Ctrl+C` to stop.

For more details: `LOG_LEVEL=DEBUG .venv/bin/python static_drone.py`.

### 7. Tuning

- **Volume threshold** (without ReSpeaker): run with `LOG_LEVEL=DEBUG`, stay silent,
  then speak, and set `RMS_THRESHOLD` between the two. Too low: background noise
  triggers recording; too high: sentences are cut.
- **Orientation**: stand right in front of the camera and speak. The angle must be
  close to 0. Otherwise, adjust `DOA_OFFSET` by the observed gap. If the angle
  increases when you move to the left, set `DOA_INVERT=1` (ReSpeaker).
- **Echo**: without echo cancellation, listening is cut during playback and
  `PLAYBACK_TAIL_S` seconds after.

### "Ok nao": on the drone or on the VPS

`WAKE_MODE` in `.env` chooses where the wake word is recognised.

**`WAKE_MODE=local` (recommended)**: like a smart speaker, the drone listens
continuously and recognises "ok nao" itself with
[openWakeWord](https://github.com/dscripka/openWakeWord). It keeps the last
`WAKE_PRE_ROLL_S` seconds (0.5 s) in memory: on the wake word, recording starts from
there, so as not to cut "ok nao quelle heure…" said in one go. It then waits for the
start of the sentence (up to `WAKE_LISTEN_S`, 5 s) and stops after `SILENCE_S` of
silence. Only that sentence goes to the VPS, marked `"wake": "local"`: body-gateway
does not filter it again. Faster (Whisper only transcribes questions) and more
reliable (Whisper no longer has to recognise an isolated "ok nao").

- Model: `WAKE_MODEL=ok_nao` loads `models/ok_nao.onnx`, to train once
  ([models/README.md](models/README.md), Colab notebook). To try before that:
  `WAKE_MODEL=hey_jarvis` and say "hey Jarvis".
- Threshold: `WAKE_THRESHOLD` (0.5). With `LOG_LEVEL=DEBUG`, the log shows the max
  score of each second (`Max score for "ok_nao" over 1 s: 0.83`): set it between the
  score of the room noise and that of "ok nao".
- Load: a few milliseconds of computation every 80 ms, on a PC as on a Raspberry Pi 5.

**`WAKE_MODE=server` (default, no model)**: the drone sends every sentence to the
VPS, Whisper transcribes it, then body-gateway looks for "ok nao" in it. The most
reliable is then to say it all in one go: « ok nao, quelle heure est-il ? ». In two
steps, wait for `j'écoute` before the question. If Whisper writes the wake word
differently ("Ok, now", …), add that spelling to `WAKE_ALIASES` on the VPS side
(body-gateway).

The control room shows what happened:

| In the control room | Meaning |
|---|---|
| `mot de réveil, j'écoute : ok nao sur le drone (ok_nao, score 0.83)` | `local`: the drone heard "ok nao", the next sentence goes to the LLM |
| `mot de réveil, j'écoute : …` | `server`: "ok nao" alone, the next sentence (within 10 s) goes to the LLM |
| voice card with the question | the question went to the LLM |
| `ignoré (sans mot de réveil) : …` | `server`: Whisper did not recognise "ok nao": the text says what it understood |
| nothing | the drone sent nothing: see its log (`Too short, ignored`, `Nothing heard after the wake word`, score under the threshold) |

The result arrives after `SILENCE_S` of silence plus the Whisper time on the VPS
(shown in the static-driver log: `STT [static_01] (1.8 s)`).

### 8. Automatic start (later)

On a desktop Ubuntu PC, use the user service instead
([pc-portable-ubuntu.md, step 9](pc-portable-ubuntu.md#9-automatic-start)).

On the Pi, adapt `User` and the paths in `static-drone.service`, then:

```bash
sudo cp static-drone.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now static-drone
journalctl -u static-drone -f
```

### 9. NAO relay (ADR 006)

The NAO cannot do TLS: it sends its requests to the drone, which relays them to the
VPS over `wss://` and adds the token (Caddy relay). See
[nao-relay/README.md](nao-relay/README.md).

## Tests

Without hardware, with numpy only (`test_wakeword.py` also needs scipy):

```bash
python3 -m unittest discover -s tests -v
```

## Known limitations

- While offline, audio and image messages are lost (no queue); reconnection is
  automatic (1 s, then up to `RECONNECT_MAX_S`).
- Image-based direction follows **motion**, not necessarily the person speaking, and
  stays bounded to the camera's field.
- `loudest_mic` only handles one multi-channel audio input, not several separate USB
  microphones.
- On the VPS side, `body-gateway` does not know the `static` body yet (see the
  consequences of ADR 004).
