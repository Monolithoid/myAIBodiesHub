# Static drone on an Ubuntu laptop

First version of the static drone: an **Ubuntu laptop** (22.04 or 24.04, desktop)
with a **webcam** and a **microphone**, until the Raspberry Pi of
[ADR 004](../../../../docs/decisions/004-drone-statique.md). The program is the same
([README](README.md)); this page only lists what changes on a laptop.

What differs from a Raspberry Pi:

| Topic | On the laptop |
|---|---|
| Sound | Ubuntu goes through PipeWire (24.04) or PulseAudio (22.04). The simplest is to pick the microphone and speaker in **Settings > Sound** and let the program use the default input and output. |
| Camera | The built-in camera often takes `/dev/video0` (and `video1`); a USB webcam then shows up as `/dev/video2`. `CAMERA_INDEX` accepts a name (`C920`) so as not to depend on the order. |
| Sound direction | One microphone (or the built-in one) → direction from **the image** (motion in the webcam). The built-in stereo microphone cannot be used for `loudest_mic`: its two capsules are too close. |
| Echo | Speaker and microphone are right next to each other: listening is cut while the drone speaks (default behaviour, without ReSpeaker). |
| Automatic start | systemd **user** service (`static-drone-user.service`), to get access to the session's sound. The system service `static-drone.service` does not have it. |
| Power | Prevent sleep, including with the lid closed. |

## Automatic installation

The `setup-ubuntu.sh` script does steps 1 to 6 below. It is idempotent: it can be
rerun safely, it only redoes what is missing and keeps the values already set in
`.env`.

```bash
cd ~
git clone https://github.com/Monolithoid/ia-agent.git
bash ia-agent/robotics/drones/static/onboard/setup-ubuntu.sh
```

Run it as the drone user, **without sudo** (the script asks for it when needed). On
the first run, it asks for the rosbridge URL, the token, the webcam, the microphone
and the speaker. After being added to the `audio` and `video` groups, log out and
back in.

By default, nothing starts on its own and sleep is left alone: the drone is started
by hand (step 7) when needed, which suits a PC also used for other things. On a PC
dedicated to the drone:

| Option | Effect |
|---|---|
| `--service` | Automatic start at boot (step 9) |
| `--no-sleep` | No more sleep, even with the lid closed (step 10). The lid is only ignored after a reboot: the script does not restart `systemd-logind`, which would close the graphical session. |
| `--audio` | Choose the microphone and speaker again |

What it does not do: fine tuning (step 8).

Then, for the NAO relay: `bash ia-agent/robotics/drones/static/onboard/nao-relay/setup-ubuntu.sh`
(step 11).

The steps below detail what the script does, for a manual installation or for
troubleshooting.

## 1. Packages

```bash
sudo apt update
sudo apt install -y git python3-venv libportaudio2 libusb-1.0-0 mpg123 \
  alsa-utils v4l-utils pulseaudio-utils
```

`pulseaudio-utils` provides `pactl`, which also works with PipeWire.

## 2. Code

```bash
cd ~
git clone https://github.com/Monolithoid/ia-agent.git
cd ia-agent/robotics/drones/static/onboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps -r requirements-wakeword.txt
```

Ubuntu 24.04 forbids `pip install` outside a virtual environment: always go through
`.venv`.

## 3. Permissions

```bash
sudo usermod -aG audio,video $USER
```

Log out and back in. The groups are used when the service runs without an open
session (step 9).

## 4. Choose the microphone and the speaker

Plug in the webcam and the microphone, then in **Settings > Sound** choose:

- **Input**: the USB microphone (or the webcam's, or the built-in one);
- **Output**: the desired speaker.

Same thing on the command line:

```bash
pactl list short sources                # inputs (ignore the ".monitor" ones)
pactl set-default-source <name>         # e.g. alsa_input.usb-…-00.mono-fallback
pactl list short sinks                  # outputs
pactl set-default-sink <name>
```

Leave `AUDIO_INPUT_DEVICE` empty in `.env`: the program opens the `default` input,
which follows this setting, and PipeWire converts to 16 kHz.

Check:

```bash
.venv/bin/python -m sounddevice         # "default" and "pipewire"/"pulse" show up
arecord -d 3 -f S16_LE -r 16000 /tmp/t.wav && aplay /tmp/t.wav
mpg123 -q some_file.mp3                 # speaker
```

If the recording is silent: `alsamixer`, F6 to choose the card, F4 for the inputs,
raise the level and enable capture (space bar).

> Opening a USB microphone directly (`AUDIO_INPUT_DEVICE=hw:…` or its "hw" index)
> also works but bypasses PipeWire: the microphone may be reported "busy", and it
> often refuses 16 kHz. The program then falls back to the microphone's native rate
> (log: `16000 Hz refused … : 48000 Hz used`).

## 5. Choose the webcam

```bash
v4l2-ctl --list-devices
```

Example on a laptop with a USB webcam:

```
Integrated Camera: Integrated C (usb-0000:00:14.0-8):
        /dev/video0
        /dev/video1
HD Pro Webcam C920 (usb-0000:00:14.0-1):
        /dev/video2
        /dev/video3
```

Each camera has two nodes; the first one gives the image. In `.env`, either:

```bash
CAMERA_INDEX=C920                       # partial name (recommended)
CAMERA_INDEX=2                          # index
CAMERA_INDEX=/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_XXXX-video-index0
```

At startup, the log shows `Camera: HD Pro Webcam C920`. Set `CAMERA_HFOV` to the
webcam's horizontal field of view (data sheet; about 70° for a C920, 60 to 80° for a
built-in camera).

## 6. Configure

```bash
cp .env.example .env
nano .env
```

For a laptop with one microphone and one webcam:

```bash
ROSBRIDGE_URL=wss://rosbridge.mydomain.com
ROSBRIDGE_TOKEN=…                       # same value as on the VPS
RESPEAKER=0                             # no ReSpeaker
MICS=                                   # a single microphone → direction from the image
CAMERA_INDEX=C920
CAMERA_HFOV=70
```

Everything else keeps its default value.

## 7. Run by hand

```bash
.venv/bin/python static_drone.py
```

The log must show, in order:

```
Camera: HD Pro Webcam C920
Direction: image (0 mics declared, camera: yes)
Audio input: default (1 channels, 16000 Hz)
Connected to wss://…
```

Then, for each sentence: `Audio sent: … angle … (image)`.

## 8. Tuning

- **Volume threshold**: `LOG_LEVEL=DEBUG .venv/bin/python static_drone.py`, stay
  silent then speak, and set `RMS_THRESHOLD` between the two values. The laptop fan
  counts as background noise: a USB microphone placed a bit further away helps.
- **Echo**: if the end of the drone's sentences is still heard, increase
  `PLAYBACK_TAIL_S` (e.g. `0.6`).
- **Orientation**: stand facing the webcam and move while speaking: the angle must be
  close to 0, positive to the right of the image.

## 9. Automatic start

The service runs as a **user** service to access the session's sound:

```bash
mkdir -p ~/.config/systemd/user
cp static-drone-user.service ~/.config/systemd/user/static-drone.service
systemctl --user daemon-reload
systemctl --user enable --now static-drone
sudo loginctl enable-linger $USER       # starts at boot, without opening a session
journalctl --user -u static-drone -f
```

If the repository is not in `~/ia-agent`, adapt the two paths in the file.

Without an open graphical session, PipeWire still starts thanks to `enable-linger`,
but keeps its own settings: choose the microphone and output once with
`pactl set-default-source` / `set-default-sink` (step 4), they are remembered.

## 10. The laptop must not sleep

```bash
# No automatic sleep
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Lid closed: do nothing (even on mains power or with an external screen)
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/; s/^#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/; s/^#\?HandleLidSwitchDocked=.*/HandleLidSwitchDocked=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind   # closes the graphical session: do it last
```

With the lid closed, the **built-in microphone and camera** are often cut or covered:
use a USB webcam and microphone in that case.

Keep the laptop **on mains power**. To undo: `sudo systemctl unmask
sleep.target suspend.target hibernate.target hybrid-sleep.target` and set
`HandleLidSwitch=suspend` back.

## 11. NAO relay

The NAO goes through the drone to reach the VPS
([ADR 006](../../../../docs/decisions/006-nao-via-drone-statique.md)). On the laptop,
follow [nao-relay/README.md](nao-relay/README.md) as is. The laptop must have a fixed
address on the local network (DHCP reservation) and stay awake (step 10), otherwise
the NAO loses the VPS.

`nao-relay/setup-ubuntu.sh` does this installation (Caddy, firewall if active) and
checks that the relay reaches the VPS. The relay is then started by hand with
`nao-relay/run.sh`, or at boot with `--service`.

## Troubleshooting

| Symptom | Lead |
|---|---|
| `Camera 'C920' not found` | `v4l2-ctl --list-devices`: use a word from the name shown. Check the `video` group (`groups`). |
| `Camera 0 not found` although `ls /dev/video*` shows some | Camera already in use (browser, Cheese, video call): close it. |
| `Audio input … not found` | `AUDIO_INPUT_DEVICE` matches no name from `python -m sounddevice`: empty it to use the default input. |
| Nothing is sent when speaking | Microphone muted or wrong default microphone (step 4), or `RMS_THRESHOLD` too high (step 8). |
| No sound when the drone speaks | `mpg123 -q some_file.mp3` in the same session; check the default output (`pactl get-default-sink`). As a service, see step 9. |
| The service works by hand but not at boot | `journalctl --user -u static-drone`; check `loginctl show-user $USER -p Linger` (must be `yes`). |
