# Container overview

Summary view of all the project's containers, their roles and how they exchange information.

---

## Summary table

| Name | Description & role | Incoming containers → information & format | Outgoing containers → information & format |
|---|---|---|---|
| **ollama** | Local LLM inference engine. Serves language models (e.g. `qwen2.5:7b`) through an OpenAI-compatible REST API. | `open-webui` → completion requests (HTTP/JSON, OpenAI chat format); `n8n` → inference requests (HTTP/JSON); `body-gateway` → system prompts + messages (HTTP/JSON, OpenAI chat format) | `open-webui` ← generated tokens (HTTP/JSON, SSE streaming); `n8n` ← generated text (HTTP/JSON); `body-gateway` ← LLM response (HTTP/JSON) |
| **open-webui** | Web interface for chat and agent management. Google OAuth authentication. Publicly reachable through Traefik at `chat.$DOMAIN`. | `ollama` ← generated tokens (HTTP/JSON SSE); `chromadb` ← relevant vector chunks (HTTP/JSON); `chat-relay` ← body-gateway responses (HTTP/JSON) | `ollama` → completion requests (HTTP/JSON); `chromadb` → texts to index or similarity queries (HTTP/JSON); `chat-relay` → user messages (HTTP/JSON) |
| **n8n** | Automation workflow orchestrator. Reachable only through an SSH tunnel. | `ollama` ← generated text (HTTP/JSON) | `ollama` → inference requests (HTTP/JSON) |
| **chromadb** | Vector database for RAG memory (Retrieval-Augmented Generation). Never exposed publicly. | `open-webui` → texts to vectorise or similarity queries (HTTP/JSON, ChromaDB protocol) | `open-webui` ← matching vectors / chunks (HTTP/JSON) |
| **whisper-asr** | Speech transcription (Speech-to-Text) service based on OpenAI Whisper. Default language: French. | `nao-core` → audio files captured by the NAO's or the static drone's microphone (HTTP multipart/form-data, WAV) | `nao-core` ← transcribed text (HTTP/JSON) |
| **naoqi-bridge** | HTTP bridge between the Docker ecosystem and the NAOqi SDK (Python 2.7). Runs in mock mode if the robot is not connected. | `nao-core` → motor commands / status requests (HTTP/JSON) | `nao-core` ← robot status, confirmations (HTTP/JSON); physical NAO ← qi SDK commands (local network, binary NAOqi protocol) |
| **nao-core** | ROS 2 (Humble) container grouping three nodes (four processes): `naoqi_driver` (topics ↔ NAO bridge, audio → Whisper, speech → edge-tts), `cognitive_renderer` (cognitive state → LEDs / head) and `static_driver` (bridge `/drones/{id}/*` ↔ `/bodies/{id}/*` for the static drone, same Whisper and edge-tts through `robotics/shared/speech_services.py`; started a second time with `OBS_DRONE_ID` for the stream voice, ADR 005). | `whisper-asr` ← transcribed text (HTTP/JSON); `naoqi-bridge` ← robot status (HTTP/JSON); ROS 2 DDS ← commands published by body-gateway, `/nao/*` and `/drones/{id}/*` topics coming from rosbridge | `whisper-asr` → WAV audio (HTTP multipart); `naoqi-bridge` → commands and requests (HTTP/JSON); ROS 2 DDS → sensor data `/bodies/{id}/sensors/*` (speech, voice direction, image); ROS 2 DDS → `/nao/*` and `/drones/{id}/audio_out` (MP3) to rosbridge |
| **body-gateway** | ROS 2 (Humble) node acting as the central brain. A single loop for all inputs (voice, Open WebUI, Twitch chat, sensors — ADR 005). Routes LLM inferences to the physical bodies (ROS 2) with a table per action type (speech → all connected voices: static drone and OBS voice, otherwise NAO; gestures / head / LEDs → NAO), turns the NAO's head towards the voice, handles dream cycles (2am–6am) and exposes an HTTP chat endpoint. | ROS 2 DDS ← body sensor data (`/bodies/{id}/sensors/*`, ROS 2 topics); `ollama` ← LLM responses (HTTP/JSON); `chat-relay` → texts from Open WebUI and `twitch-input` (HTTP/JSON, `POST /chat`) | `ollama` → system prompts + history (HTTP/JSON, OpenAI chat format); ROS 2 DDS → motor / speech commands (ROS 2 topics); ROS 2 DDS → cognitive state (`/cognitive_state` topic); `open-webui` / `twitch-input` via `chat-relay` ← chat responses (HTTP/JSON, port 8095) |
| **ros2-bridge** | rosbridge server (ROS 2 Humble). Exposes the ROS 2 bus (DDS domain 42) over WebSocket for external clients. | NAO / static drone / obs-agent / external clients ← wrapped ROS 2 messages (WebSocket, rosbridge JSON protocol) | ROS 2 DDS → messages translated to the internal nodes (ROS 2 topics) |
| **rosbridge-proxy** | Caddy reverse proxy. Bridges the outside (TLS through Traefik) and the ros2-bridge in host mode. | Traefik → incoming WebSocket connections (WSS 443, WS 80 for the NAO); requires `Authorization: Bearer $ROSBRIDGE_TOKEN` | `ros2-bridge` (host) ← WebSocket connections (WS, port 9090) |
| **chat-relay** | Lightweight Caddy reverse proxy. Makes body-gateway (host network) reachable from ai-network for open-webui and twitch-input. | `open-webui`, `twitch-input` → HTTP chat requests (HTTP/JSON, port 8095) | `body-gateway` (host) ← relayed HTTP requests (HTTP/JSON, port 8095) |
| **twitch-input** | Twitch chat input (ADR 005). Reads the chat over anonymous IRC, keeps `!nao …` messages, filters them (length, links, banned words, per-viewer delay, moderators-only option) and sends them one by one to the brain. Refuses to start without `TWITCH_CHANNEL`. | Twitch → chat messages (IRC over TLS, `irc.chat.twitch.tv:6697`); `chat-relay` ← brain response (HTTP/JSON, ignored) | `chat-relay` → `{"text": "[tchat Twitch] nickname : message", "robot_id"}` (HTTP/JSON, `POST /chat`) |
| **obs-agent** | NAO's voice on the stream (ADR 005): body `obs_01` with the `speech` capability. A rosbridge client like the static drone; serves the overlay `https://<obs-agent domain>/overlay?token=$OBS_TOKEN` (domain chosen in Coolify), to open as an OBS browser source. Only "connected" while the overlay is open. | `rosbridge-proxy` ← `/drones/obs_01/audio_out` (base64 MP3) and `/bodies/obs_01/actuators/say` (subtitle); OBS → overlay opening (HTTPS through Traefik) | `rosbridge-proxy` → `/drones/obs_01/status` (heartbeat) and `/drones/obs_01/speaking` (Bool); OBS ← HTML overlay + audio (OBS mixer) |
| **game-master** | Games with the stream's viewers (ADR 010, [games.md](games.md)): settles each move by code and returns the public state (never the solution); one activity at a time; scores in SQLite; OBS overlay `https://<game-master domain>/overlay?token=$GAME_TOKEN`. Refuses to start without `GAME_TOKEN`. | `body-gateway` → `POST /start`, `/move`, `GET /state` (HTTP/JSON, `127.0.0.1:8098`); `regie`, `twitch-input` → `POST /stop` | `body-gateway` ← announced events (`POST /game-event` through `chat-relay`); OBS ← overlay (HTML + SSE) |
| **regie** | Control room (ADR 007): page `https://<regie domain>/?token=$REGIE_TOKEN` (domain chosen in Coolify) showing live each input, the LLM decision and the actions routed to the bodies. Read-only rosbridge client. | `rosbridge-proxy` ← `/events` (JSON published by body-gateway), `/cognitive_state`, `/emotion_state` | browser ← HTML page + SSE stream (HTTPS through Traefik) |
| **bitterbot-gateway** *(disabled)* | Autonomous agent with long-term memory (SQLite) and a dream engine. Disabled since ADR 003 in favour of direct Ollama calls from body-gateway. | `ollama` ← generated text (HTTP/JSON) | `ollama` → inference requests (HTTP/JSON) |

---

## Data flows per use case

### Voice conversation with the NAO robot

```
NAO microphone
  └─► nao-core/naoqi_driver (PCM audio)
        └─► whisper-asr (WAV → HTTP multipart)
              └─► nao-core/naoqi_driver (transcribed text ← JSON)
                    └─► body-gateway (ROS 2 topic /bodies/{id}/sensors/speech)
                          └─► ollama (prompt → HTTP/JSON)
                                └─► body-gateway (response ← JSON)
                                      ├─► nao-core/naoqi_driver (speech command → ROS 2 topic)
                                      │     └─► physical NAO (TTS through naoqi-bridge)
                                      └─► nao-core/cognitive_renderer (cognitive state → /cognitive_state topic)
                                            └─► physical NAO (LEDs / animations)
```

### Voice conversation through the static drone (ADR 004)

```
Static drone microphones (Raspberry Pi / PC)
  └─► rosbridge-proxy → ros2-bridge (/drones/{id}/audio: WAV + angle, JSON)
        └─► nao-core/static_driver
              ├─► body-gateway (/bodies/{id}/sensors/sound_direction)
              │     └─► nao-core/naoqi_driver (HeadYaw towards the voice) → NAO
              └─► whisper-asr (WAV → HTTP multipart)
                    └─► body-gateway (/bodies/{id}/sensors/speech, wake word "ok nao")
                          └─► ollama (prompt + "[voix : 30° à gauche]")
                                └─► body-gateway
                                      ├─► nao-core/static_driver (speech → edge-tts)
                                      │     └─► /drones/{id}/audio_out (MP3) → drone speaker
                                      └─► nao-core/naoqi_driver (gestures, LEDs) → NAO
```

Without a connected static drone, speech goes back to the NAO as before.

### Twitch chat and stream voice (ADR 005)

```
Twitch chat "!nao …"
  └─► twitch-input (filters, one message at a time)
        └─► chat-relay → body-gateway (POST /chat, "[tchat Twitch] nickname : message")
              └─► ollama → list of actions
                    ├─► nao-core/static_driver (DRONE_ID=obs_01, edge-tts)
                    │     └─► /drones/obs_01/audio_out (MP3) → rosbridge → obs-agent
                    │           └─► overlay open in OBS → OBS mixer → stream
                    ├─► nao-core/static_driver (static_01) → room speaker
                    └─► nao-core/naoqi_driver (gestures, LEDs) → NAO
```

Speech goes out on every connected voice, whatever the input: an answer to
"ok nao" is also heard on the stream if the overlay is open.

### Games with the viewers (ADR 010)

```
Twitch chat "!nao je propose le E"
  └─► twitch-input (+ chat badges) → chat-relay → body-gateway (POST /chat)
        ├─► LLM: the message becomes {"type": "game_move", ...}
        ├─► game-master (127.0.0.1:8098, POST /move): settled by code → event + public state
        │     └─► OBS overlay updated (SSE)
        └─► LLM: NAO's reaction to the event → voices, gesture, LEDs
control room "Annuler le jeu" / "!stop" (moderator) → game-master POST /stop
  └─► chat-relay → body-gateway POST /game-event → NAO announces it
```

### User chat through a browser

```
Browser
  └─► open-webui (HTTPS / Traefik)
        ├─► ollama (inference → HTTP/JSON SSE)
        ├─► chromadb (RAG → HTTP/JSON)
        └─► chat-relay → body-gateway (physical chat → HTTP/JSON :8095)
```

### NAO / static drone → rosbridge connection (external command input)

```
NAO, static drone or obs-agent (outbound WebSocket)
  └─► Traefik (WS :80 for the NAO, WSS :443)
        └─► rosbridge-proxy (Caddy :80, checks the token)
              └─► ros2-bridge (host :9090)
                    └─► ROS 2 DDS (domain 42)
                          └─► nao-core / body-gateway
```

`obs-agent` runs on the VPS: it reaches `rosbridge-proxy:80` directly over
`robotics-net`, without going through Traefik, but with the same token.

---

## Docker networks

| Network | Type | Member containers | Internet access |
|---|---|---|---|
| `ai-network` | bridge | ollama, open-webui, n8n, chromadb, naoqi-bridge, whisper-asr, chat-relay, twitch-input, game-master | yes |
| `bitterbot-net` | bridge (internal) | *(reserved for bitterbot-gateway)* | no |
| `robotics-net` | bridge | rosbridge-proxy, naoqi-bridge, obs-agent, regie, twitch-input, game-master | yes |
| host | — | ros2-bridge, nao-core, body-gateway | through the host |
