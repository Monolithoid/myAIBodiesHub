# Architecture — Unified personal AI stack

## Vision

A single AI with modular capabilities.  
Splitting into containers is a scalability decision, not a functional separation.

## Infrastructure

- **Ubuntu VPS** — ~24 GB RAM, no GPU, managed through Coolify
- **All services** run in Docker containers
- **A single `docker-compose.yml`** at the root of the monorepo

## Monorepo layout

```
ia-agent/
├── core/
│   └── bitterbot/           → Dockerfile + config of the Bitterbot agent
├── robotics/
│   ├── brain/               → Generic brain, independent of the drone type
│   │   ├── body_gateway/    → ROS 2 brain: LLM ↔ robotic bodies
│   │   ├── ros2_bridge/     → rosbridge_server WebSocket Dockerfile
│   │   └── webui/           → Open WebUI pipe (nao_pipe.py)
│   ├── inputs/
│   │   └── twitch/          → Twitch chat input "!nao" → body-gateway (ADR 005)
│   ├── shared/              → Shared classes (RobotBridgeBase)
│   └── drones/
│       ├── nao/
│       │   ├── onboard/     → nao_ws_bridge.py (script run ON the robot)
│       │   ├── bridge/      → Flask HTTP bridge to NAO (naoqi-bridge container)
│       │   ├── driver/      → ROS 2 node: /bodies/*/actuators/* → /nao/*
│       │   ├── cognitive_renderer/ → ROS 2 node: cognitive state → LEDs / head
│       │   └── core/        → Dockerfile + entrypoint of the nao-core container
│       ├── static/          → Static drone (ADR 004): onboard/ (Raspberry Pi / PC) + driver/ (VPS ROS 2 node)
│       ├── obs/             → NAO's voice on the stream: OBS overlay (ADR 005)
│       └── quadcopter/      → Reserved
├── docs/
│   ├── architecture.md      → this file
│   └── decisions/           → ADR (Architecture Decision Records)
├── tests/                   → test-chain.sh, NAO diagnostics, fake static drone
├── .github/workflows/       → GitHub Actions CI/CD
├── setup-vps.sh             → VPS preparation
└── docker-compose.yml       → full composition
```

## Related documentation

- [Container overview](containers-overview.md) — detailed table of the exchanges between containers
- [AI stack](ia-stack.md) — configuration of each AI service
- [AI / ROS 2 / NAO bridge](bridge-ia-ros2-nao.md) — robotics layer
- [ADR 001](decisions/001-ros2-over-ros1.md) · [ADR 002](decisions/002-nao-websocket-bridge.md) · [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md) · [ADR 004](decisions/004-drone-statique.md) · [ADR 005](decisions/005-avatar-twitch.md)

---

## Docker services

| Container | Role | Port | Access |
|---|---|---|---|
| `ollama` | LLM engine | 11434 | Public (API key) |
| `open-webui` | Chat / agents interface | 3000→8080 | Public (Google SSO) |
| `n8n` | Workflow orchestrator | 5678 | SSH tunnel |
| `chromadb` | Vector memory | 8000 | Internal only |
| `whisper-asr` | Speech transcription (STT) | 9000 | Internal only |
| `naoqi-bridge` | Python 2.7 HTTP bridge → NAOqi SDK | 8090 | Internal (host) |
| `nao-core` | ROS 2 Humble: naoqi_driver + cognitive_renderer + static_driver (×2: static drone and OBS voice) | — | host network |
| `body-gateway` | ROS 2 brain: LLM ↔ robotic bodies | 8095 | host network |
| `ros2-bridge` | rosbridge_server WebSocket | 9090 | host network |
| `rosbridge-proxy` | Caddy proxy: Traefik → ros2-bridge | 80 | robotics-net |
| `chat-relay` | Caddy proxy: ai-network → body-gateway | 8095 | ai-network |
| `twitch-input` | Twitch chat input "!nao" → body-gateway | — | ai-network |
| `obs-agent` | NAO's voice on the stream (OBS overlay, body `obs_01`) | 8096 | Public (`obs.$DOMAIN`, token) |
| `regie` | Control room: live `/events` stream (inputs, LLM decisions, actions) | 8097 | Public (`regie.$DOMAIN`, token) |
| `game-master` | Games with the viewers: the code decides, the LLM speaks (ADR 010) | 8098 | Public overlay (`games.$DOMAIN`, token); API on 127.0.0.1 for body-gateway |
| `bitterbot-gateway` *(disabled)* | AI agent with long-term memory | 19001 | SSH tunnel |

## Docker networks

```
ai-network (bridge, internet)
  └── ollama, open-webui, n8n, chromadb, whisper-asr, naoqi-bridge, chat-relay, twitch-input, game-master

bitterbot-net (bridge, internal — no internet access)
  └── (reserved for bitterbot-gateway — see ADR 003)

robotics-net (bridge, internet)
  └── rosbridge-proxy, naoqi-bridge, obs-agent, regie, twitch-input, game-master

host network
  └── nao-core, body-gateway, ros2-bridge  (ROS 2 DDS domain 42)
```

## NAO robotics architecture

All inputs follow the same loop (see [ADR 005](decisions/005-avatar-twitch.md)):
the "ok nao" voice, Open WebUI, Twitch chat and sensors produce a text;
`body-gateway` calls the LLM and spreads the list of actions across the bodies.

```
Open WebUI (chat)          twitch-input ("!nao …" from Twitch chat)
  │  POST http://chat-relay:8095/chat  ◄──┘
  ▼
chat-relay (Caddy, ai-network → host)
  │
  ▼
body-gateway (ROS 2 Humble, host network, :8095)
  │  POST http://localhost:11434/v1/chat/completions
  ├─► ollama (LLM)
  │
  │  ROS 2 DDS domain 42
  ├─► /cognitive_state → nao-core/cognitive_renderer (LEDs + head)
  ├─► /bodies/{static_01,obs_01}/actuators/say → nao-core/static_driver (edge-tts)
  │         └─► /drones/{id}/audio_out → room speaker / OBS overlay (obs-agent)
  └─► /bodies/{id}/actuators/* → nao-core/naoqi_driver
                                        │  HTTP localhost:8090
                                        ▼
                                  naoqi-bridge (Python 2.7)
                                        │  qi SDK (mock if NAO absent)
                                        ▼
                                  physical NAO

NAO robot
  └── nao_ws_bridge.py (Python 2.7)
        └─► outbound WebSocket ws://DRONE_IP:9090 (local network, no token)
              └─► static drone relay (Caddy, ADR 006): wss://rosbridge.$DOMAIN + token
                    └─► rosbridge-proxy (Caddy) → ros2-bridge (host :9090)
                    └─► ROS 2 DDS domain 42
```

### ROS 2 topics

| Topic | Type | Direction | NAO action |
|---|---|---|---|
| `/nao/speech` | `std_msgs/String` | VPS → NAO | `ALTextToSpeech.say()` |
| `/nao/audio_out` | `std_msgs/String` (base64 MP3) | VPS → NAO | `ALAudioPlayer.playFile()` |
| `/nao/cmd_vel` | `geometry_msgs/Twist` | VPS → NAO | `ALMotion.moveToward()` |
| `/nao/gesture` | `std_msgs/String` | VPS → NAO | `ALRobotPosture.goToPosture()` |
| `/nao/leds` | `std_msgs/String` (JSON) | VPS → NAO | `ALLeds.fadeRGB()` |
| `/nao/motion` | `std_msgs/String` (JSON) | VPS → NAO | `ALMotion.angleInterpolationWithSpeed()` |
| `/nao/stop` | `std_msgs/Empty` | VPS → NAO | `ALMotion.stopMove()` |
| `/nao/status` | `std_msgs/String` (JSON) | NAO → VPS | Battery (30 s heartbeat) |
| `/nao/audio` | `std_msgs/String` (base64 WAV) | NAO → VPS | Captured speech (wake word) |

## Drone extensibility

The rosbridge architecture extends directly to drones.
PX4, DJI (ROS 2 SDK) and Parrot (Anafi SDK) all have ROS 2 drivers.
All it takes is a bridge script on the drone side and a new ID in `BODY_REGISTRY` (the ID prefix gives the type: `static…`, `obs…`, otherwise NAO — see `body_rules.py`).

First case: the Raspberry Pi 5 static drone (audio capture, video, audio output), which leaves NAO with emotional rendering only — see [ADR 004](decisions/004-drone-statique.md). Second case: the stream voice `obs_01`, a "speech" body played in the OBS mixer — see [ADR 005](decisions/005-avatar-twitch.md).
