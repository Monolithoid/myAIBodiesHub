# Deployment chain test

Link-by-link diagnostic script to validate the full **WebUI → NAO** chain. Every test can run with the robot switched off: `naoqi-bridge` works in mock mode when NAO is not connected.

---

## Running the tests

```bash
# From the repository root, on the VPS
bash tests/test-chain.sh
```

The output shows `✓ PASS`, `✗ FAIL` or `⊘ SKIP` for each check. The script exits with a non-zero code if at least one link fails.

---

## Tested chain

```
open-webui (nao_pipe.py)
  │  POST http://chat-relay:8095/chat     ← Link 1 (ai-network)
  ▼
chat-relay (Caddy)
  │  reverse-proxy → <host gateway>:8095   ← Link 2
  ▼
body-gateway  (host network, :8095)
  │  POST http://localhost:11434/v1/chat/completions   ← Link 3
  ▼
ollama  (:11434, port exposed on the host)
  │  → JSON response { thoughts, emotion, actions }     ← Link 3b
  ▼
body-gateway  (publishes on ROS 2 DDS domain 42)
  │  /bodies/nao_01/actuators/*          ← Link 4
  ▼
nao-core/naoqi_driver  (host network, DDS 42)
  │  POST http://localhost:8090/say|motion|leds|…     ← Link 5
  ▼
naoqi-bridge  (:8090, exposed on 127.0.0.1)
  │  qi SDK → tcp://NAO_IP:9559          ← Link 6
  ▼
NAO  (off → mock response)
```

Link 9, simulated static drone ([ADR 004](decisions/004-drone-statique.md)):

```
tests/fake-static-drone.py  (fake drone, WebSocket ws://localhost:9090)
  │  /drones/static_01/status, image, speaking, audio
  ▼
ros2-bridge → nao-core/static_driver → whisper-asr
  ▼
body-gateway  (wake word, direction, NAO head, speech routing)
  ▼
nao-core/static_driver → edge-tts → /drones/static_01/audio_out
```

> **n8n** is in `ai-network` and can call `body-gateway` directly, but it is **not** part of the chat chain described here — see [Link 7](#link-7--n8n-outside-the-direct-chain).

---

## Requirements

- `docker` available on the VPS
- The target services started (Deploy in Coolify; `docker ps` to check)
- `curl` and `python3` installed (present by default on Ubuntu)
- NAO can be off for every link except 6 (mock expected)

---

## Links

### Link 0 — Docker containers

Checks that the required containers are in the `running` state.

| Container | Role | Required |
|---|---|---|
| `open-webui` | Chat interface | Yes |
| `chat-relay` | Caddy proxy WebUI ↔ body-gateway | Yes |
| `body-gateway` | ROS 2 brain + HTTP endpoint | Yes |
| `naoqi-bridge` | HTTP bridge ↔ NAOqi SDK | Yes |
| `nao-core` | naoqi_driver + cognitive_renderer + static_driver | Yes |
| `ollama` | LLM engine | Recommended |
| `chromadb` | Vector memory | Optional |
| `n8n` | Workflow orchestration | Optional |
| `whisper-asr` | Speech transcription | Optional |
| `ros2-bridge` | rosbridge WebSocket | Optional |

> See [ia-stack.md](ia-stack.md) for the full description of each AI service and [bridge-ia-ros2-nao.md](bridge-ia-ros2-nao.md) for the robotics services.

---

### Link 1 — open-webui → chat-relay (ai-network)

**What is tested:** the `nao_pipe.py` Pipe installed in Open WebUI calls `POST http://chat-relay:8095/chat`. This test checks that DNS resolution works and that the TCP port is reachable from the `open-webui` container on the `ai-network` network.

**Success:** `chat-relay` answers HTTP 400 (`{"error":"missing text"}`) — body-gateway receives the request and rejects an empty body. This proves the proxy is crossed correctly.

**Reference:** [ia-stack.md — NAO Brain](ia-stack.md#nao-brain--webui-chat-as-the-robotic-brains-interface)

```bash
# Equivalent manual test (from the open-webui container)
docker exec open-webui curl -sf -X POST http://chat-relay:8095/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"","robot_id":"nao_01"}'
# Expected: {"error":"missing text"} or code 400
```

---

### Link 2 — chat-relay → body-gateway (host gateway :8095)

**What is tested:** the `chat-relay` Caddy proxies requests to port 8095 of the host gateway (IP read at startup from `/proc/net/route`), where `body-gateway` listens in `network_mode: host`. This is also the path used by `twitch-input`. This link validates that the `ai-network → host network` crossing works.

**Success:** `POST http://localhost:8095/chat` answers from the host, and the same request from `open-webui` through `chat-relay` gives the same result.

**Reference:** [ia-stack.md — Docker networks](ia-stack.md#docker-networks)

```bash
# Manual test from the host (body-gateway in network_mode: host)
curl -sf -X POST http://localhost:8095/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"","robot_id":"nao_01"}'
# Expected: {"error":"missing text"}
```

---

### Link 3 — body-gateway → Ollama (localhost:11434)

**What is tested:** `body-gateway` runs in `network_mode: host` and calls Ollama on `http://localhost:11434` (Ollama exposes its port 11434 on the host). Two sub-tests:

1. `GET /api/tags` — Ollama answers and the model configured in `BRAIN_MODEL` is present
2. `POST /v1/chat/completions` — the OpenAI-compatible endpoint answers with a `choices` field

**Success:** Ollama answers, the model is listed, and `/v1/chat/completions` returns a valid response.

**Reference:** [ia-stack.md — Ollama](ia-stack.md#ollama--llm-engine) · [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md)

```bash
# Check the available models
curl -sf http://localhost:11434/api/tags | python3 -c \
  "import json,sys; [print(m['name']) for m in json.load(sys.stdin)['models']]"

# Direct inference test (adjust the model if needed)
curl -sf -X POST http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3:1.7b","messages":[{"role":"user","content":"ok"}],"stream":false}'
```

---

### Link 3b — Full inference through body-gateway

**What is tested:** a real text going through `body-gateway` to Ollama and back — the same path as an "ok nao" message from the chat. The response must contain the `speech`, `emotion`, `thoughts` and `actions` fields.

**Success:** `{"speech":"...","emotion":"...","thoughts":"...","actions":[...]}` — Ollama generated structured JSON and body-gateway parsed it correctly.

**Duration:** up to 60 s (LLM inference on CPU).

```bash
curl -sf --max-time 60 -X POST http://localhost:8095/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"bonjour","robot_id":"nao_01"}'
```

---

### Link 4 — body-gateway → ROS 2 (DDS domain 42)

**What is tested:** after an inference, `body-gateway` publishes on the ROS 2 topics `/cognitive_state`, `/emotion_state` and `/bodies/nao_01/actuators/*`. This test checks that the DDS bus is active and that the topics are visible from `naoqi-driver`.

**Success:** `ros2 topic list` returns `/cognitive_state` and at least one `/bodies/…` topic.

**Reference:** [bridge-ia-ros2-nao.md — ROS 2 topics](bridge-ia-ros2-nao.md#ros-2-topics--complete-reference)

```bash
# List the active topics (DDS domain 42)
docker exec nao-core bash -c \
  "source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 ros2 topic list"

# Check that there is a publisher on /cognitive_state
docker exec nao-core bash -c \
  "source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 ros2 topic info /cognitive_state"
```

---

### Link 5 — nao-core → naoqi-bridge (localhost:8090)

**What is tested:** `nao-core/naoqi_driver` receives the ROS 2 topics and translates them into HTTP calls to `naoqi-bridge` on `localhost:8090`. This link validates that the Flask bridge is started, reachable, and answers commands.

**Success:** `/health` returns `{"ok": true}`, `/say` and `/leds` return `{"ok": true}` (in mock or real mode).

**Reference:** [nao.md — Scripts overview](nao.md#scripts-overview)

```bash
# Bridge health
curl -sf http://localhost:8090/health

# say test (mock if NAO is off)
curl -sf -X POST http://localhost:8090/say \
  -H "Content-Type: application/json" \
  -d '{"text":"test"}'

# LEDs test (mock if NAO is off)
curl -sf -X POST http://localhost:8090/leds \
  -H "Content-Type: application/json" \
  -d '{"group":"AllLeds","r":0.0,"g":1.0,"b":0.0,"duration":0.5}'
```

---

### Link 6 — naoqi-bridge in mock mode (NAO off)

**What is tested:** with NAO off, `naoqi-bridge` must run in **mock mode**: it answers `{"ok": true}` to every command but prints them in its logs instead of sending them to the robot. The `connected` field in `/health` must be `false`.

**Success:** `{"ok": true, "connected": false}` and the logs contain the word `MOCK`.

**Reference:** [nao.md — Troubleshooting](nao.md#troubleshooting)

```bash
# Check the connection state
curl -sf http://localhost:8090/health
# Expected: {"ok": true, "nao_ip": "...", "connected": false}

# Check mock mode in the logs
docker logs --tail=50 naoqi-bridge 2>&1 | grep -i mock
# Expected: "[naoqi_bridge] MOCK mode active — no NAO connection."
```

When NAO is switched back on and reconnected, rerun this link: `connected` will become `true`.

---

### Link 7 — n8n (outside the direct chain)

**What is tested:** n8n is in `ai-network` and its port 5678 is bound to `127.0.0.1`. This link simply checks that it is started.

> **Important:** n8n is **not** in the direct chat chain. The route `nao_pipe.py → chat-relay → body-gateway` does not go through n8n. n8n is a **parallel** orchestration tool: it can trigger actions on `body-gateway` through its own workflows, independently of the WebUI chat.

**Reference:** [ia-stack.md — n8n](ia-stack.md#n8n--workflow-orchestrator)

```bash
# Access through an SSH tunnel only
ssh -L 5678:localhost:5678 $VPS_USER@$DOMAIN
# then open http://localhost:5678
```

### Link 9 — Simulated static drone (fake rosbridge messages)

Runs `tests/fake-static-drone.py`, which plays the role of the Raspberry Pi: it
connects to rosbridge like `static_drone.py` and checks each relay. No hardware is
needed, but `whisper-asr`, Ollama and edge-tts internet access are required.

| Step | Sent by the fake drone | Expected |
|---|---|---|
| 1 | `/drones/static_01/status` | `/bodies/static_01/connected` = `true` |
| 2 | `/drones/static_01/speaking` = `true` then `false` | `/bodies/static_01/sensors/speaking` = `true`, NAO eyes animated (`/naoqi/leds` `FaceLeds`), then back to the profile (`AllLeds`) |
| 3 | `/drones/static_01/image` | `/bodies/static_01/sensors/image` (same JSON) |
| 4 | `/bodies/static_01/actuators/say` « OK Nao, bonjour… » | MP3 on `/drones/static_01/audio_out` |
| 5 | `/drones/static_01/audio` (that MP3, angle -30°) | `sound_direction`, `HeadYaw` on `/bodies/nao_01/actuators/motion`, text on `/bodies/static_01/sensors/speech` |
| 6 | — | LLM answer spoken on `/drones/static_01/audio_out` |

```bash
# On its own, without the rest of test-chain.sh
python3 tests/fake-static-drone.py
# Through the public path (Traefik + rosbridge-proxy), with the token
python3 tests/fake-static-drone.py --url wss://rosbridge.$DOMAIN --token "$ROSBRIDGE_TOKEN"
```

If a real `static_01` drone is plugged in during the test, it speaks the test
sentences and the NAO turns its head.

---

## Expected results (NAO off)

| Link | Expected result |
|---|---|
| 0 | All required containers `running` |
| 1 | HTTP 400 `missing text` from open-webui to chat-relay |
| 2 | HTTP 400 `missing text` from the host on :8095 |
| 3 | Ollama answers, `BRAIN_MODEL` model listed, `/v1/chat/completions` OK |
| 3b | JSON `{speech, emotion, thoughts, actions}` returned |
| 4 | `/cognitive_state` and `/bodies/nao_01/…` visible on DDS 42 |
| 5 | `naoqi-bridge /health` and `/say` return `{"ok": true}` |
| 6 | `connected: false` in `/health`, `MOCK` lines in the logs |
| 7 | n8n reachable on localhost:5678 (if started) |
| 9 | All fake static drone steps `PASS` (if edge-tts is unreachable, step 4 fails and steps 5 and 6 are skipped) |

---

## Troubleshooting

| Symptom | Link | Likely cause | Fix |
|---|---|---|---|
| `container X: NOT running` | 0 | Service not started | `docker start <service>` (or Redeploy in Coolify) |
| `Could not resolve host: chat-relay` | 1 | open-webui outside `ai-network` | Check `networks:` in `docker-compose.yml` |
| `Connection refused` on :8095 from open-webui | 1–2 | chat-relay not started | `docker start chat-relay` (or Redeploy in Coolify) |
| `Connection refused` on localhost:8095 | 2 | body-gateway crashed | `docker logs body-gateway` |
| `model not found` in Ollama | 3 | Model not downloaded | `docker exec -it ollama ollama pull qwen3:1.7b` |
| Timeout on `/v1/chat/completions` | 3 | Model not loaded or CPU saturated | Wait for the initial load (~30 s) |
| JSON parse error in body-gateway | 3b | Ollama does not return strict JSON | Check the `_AWAKE_SYSTEM` system prompt in `body_gateway.py` |
| ROS 2 topics missing | 4 | Wrong DDS domain or body-gateway/nao-core crashed | `docker logs body-gateway`; `docker logs nao-core`; check `ROS_DOMAIN_ID=42` |
| `Connection refused` on localhost:8090 | 5 | naoqi-bridge not started | `docker start naoqi-bridge` (or Redeploy in Coolify) |
| `connected: true` with NAO off | 6 | Old TCP connection not closed | Restart `naoqi-bridge` |
| `/bodies/static_01/connected` never seen | 9 | static-driver not started | `docker logs nao-core`: is there a `static-driver(…)` line for static_01? Is `STATIC_DRONE_ID` empty? |
| No `HeadYaw` | 9 | `static_01` missing from `BODY_REGISTRY` | Check body-gateway's environment |
| No spoken answer | 9 | Wake word missing from the transcription, or slow LLM | `docker logs body-gateway` (wake-word-missing message) |
| No `MOCK` line in the logs | 6 | The qi SDK was found and NAO_IP is set | Normal if NAO was on at startup |
