# AI stack

Services that make up the core of the artificial intelligence: LLM engine, chat interface, workflow orchestrator, vector memory and autonomous agent.

---

## Overview

```
User (browser)
  └─► chat.$DOMAIN (Coolify/Traefik)
        └─► Open WebUI :8080
              ├─► Ollama :11434  (LLM inference)
              └─► ChromaDB :8000 (RAG / vector memory)

Developer (SSH tunnel)
  └─► n8n :5678          (automations)
  └─► Bitterbot :19001   (autonomous agent)
```

All these services run in `ai-network`, except Bitterbot, which is isolated in `bitterbot-net` (internet access blocked at the Docker level — only Ollama is reachable).

---

## Ollama — LLM engine

**Image:** `ollama/ollama:latest`  
**Port:** 11434 (exposed through Traefik with a mandatory API key)

| Variable | Description |
|---|---|
| `OLLAMA_API_KEYS` | API key (`openssl rand -hex 32`) |
| `OLLAMA_KEEP_ALIVE` | Keeps the model in memory (`24h`) |
| `OLLAMA_NUM_PARALLEL` | Concurrent requests (`2`) |
| `OLLAMA_NUM_THREAD` | CPU threads allocated (`8`) |

**Volume:** `/opt/ia/ollama` → AI models (10–20 GB depending on the model)

**Download a model:**
```bash
docker exec -it ollama ollama pull qwen3:1.7b   # body-gateway's default model
```

**Change the model used by body-gateway:** environment variable `BRAIN_MODEL` in Coolify.

---

## Brain provider (body-gateway)

`body-gateway` can call three LLM providers. The choice is made with the
`BRAIN_PROVIDER` variable, in Coolify. The code is in
`robotics/brain/body_gateway/brain_client.py`. Whatever the provider, the LLM must
answer in strict JSON, which is then read by `_parse_json()`.

| `BRAIN_PROVIDER` | Call | Default `BRAIN_URL` | Default `BRAIN_MODEL` |
|---|---|---|---|
| `ollama` (default) | `POST {BRAIN_URL}/v1/chat/completions` streaming, with `"options": {"think": false}` | `http://localhost:11434` | `qwen3:1.7b` |
| `openai` | the same endpoint, without the `options` field | `https://api.openai.com` | `gpt-4o-mini` |
| `anthropic` | official `anthropic` SDK, `messages.stream()` | SDK URL (`https://api.anthropic.com`) | `claude-haiku-4-5` |

| Variable | Description |
|---|---|
| `BRAIN_PROVIDER` | `ollama`, `openai` or `anthropic` |
| `BRAIN_URL` | Leave empty to use the provider's default |
| `BRAIN_MODEL` | Leave empty to use the provider's default |
| `BRAIN_API_KEY` | Provider key. Without a value, `OLLAMA_API_KEYS` is used: for `openai` or `anthropic` it must therefore be set (`sk-...` from platform.openai.com, `sk-ant-...` from console.anthropic.com) |
| `BRAIN_TIMEOUT` | Maximum delay in seconds between two chunks of the stream (default 120, 60 in the compose file) |
| `BRAIN_EFFORT` | Anthropic only, optional: `low`, `medium`, `high`… Nothing is sent if the variable is empty |

Without `BRAIN_PROVIDER`, nothing changes: local Ollama and `qwen3:1.7b`, as before.

A ChatGPT or Claude.ai subscription does not give access to the API. An API key,
billed by usage, is required.

### NAO's personality

The system prompt starts with NAO's personality: who it is, its past, its way of
speaking. It is read from `/opt/ia/nao/persona/personnalite.md` on the VPS (mounted
read-only in body-gateway, variable `BRAIN_PERSONA_FILE`).

- The file is reread on every LLM call: a change applies to the next message,
  without restarting body-gateway.
- Missing or empty file: body-gateway uses the default text, `DEFAULT_PERSONA` in
  `robotics/brain/body_gateway/persona.py`.
- Free text, in French (the robot speaks French), in the second person
  ("Tu es NAO…"). A few paragraphs are enough: it is sent on every call and counts
  towards the tokens.
- The technical frame (strict JSON, possible actions, Twitch chat rules) stays in
  the code, after the personality: the file cannot replace it.
- The dream mode prompt does not use the personality.

```bash
mkdir -p /opt/ia/nao/persona
nano /opt/ia/nao/persona/personnalite.md
```

### Anthropic parameters

What is sent depends on the model. The rules below come from the Anthropic documentation of October 2026.

- **temperature**: only sent to models that accept it (Haiku 4.5, Sonnet 4.5 / 4.6,
  Opus 4.x up to 4.6). Opus 4.7 and later, Sonnet 5 / 5.5 and Haiku 5.5 return a 400
  error with a non-default temperature. For these models, and for any unknown model,
  the API default applies.
- **thinking**: the parameter is never sent. Haiku 4.5 then answers without
  thinking, which is equivalent to Ollama's `think: false`. On Opus 5.5 and Sonnet
  5.5, thinking cannot be disabled (`disabled` returns a 400). To limit latency, set
  `BRAIN_EFFORT=low`.
- **effort**: Haiku 4.5 refuses it, hence it is only sent if `BRAIN_EFFORT` is set.
  With Opus 5.5 and Haiku 5.5, the default effort is `medium`.
- **structured outputs** (`output_config.format`): not used. The format of the
  `actions` varies with their type, and a strict schema would require freezing each
  variant. The JSON instruction in the system prompt and `_parse_json()` are enough.
- A refusal by the model (`stop_reason: "refusal"`) is treated as an error: the
  robot falls back to its backup response.

A more recent model, `claude-haiku-5-5`, costs $0.10 / $0.50 per million tokens,
against $1 / $5 for Haiku 4.5. Its thinking is on by default: try it with
`BRAIN_EFFORT=low`.

### Cost

The dream loop (2am – 6am, one tick every 300 s) makes at most **48 calls per
night**. Counting about 1,000 input tokens (system prompt and sensor context) and
300 output tokens per call, the order of magnitude is:

| Model | Input / output price (per million tokens) | Per call | Per night (48) | Per month |
|---|---|---|---|---|
| `claude-haiku-4-5` | $1 / $5 | ≈ $0.0025 | ≈ $0.12 | ≈ $3.60 |
| `claude-haiku-5-5` | $0.10 / $0.50 | ≈ $0.00025 | ≈ $0.012 | ≈ $0.36 |
| `claude-opus-5-5` | $4 / $20, plus thinking tokens | ≈ $0.01 or more | ≈ $0.50 or more | ≈ $15 or more |

Daytime conversations (chat, voice, sonar, bumper) add to this total, one call per
interaction. These figures are estimates, not measurements: the actual cost is shown
in the provider's console.

### Privacy

With `openai` or `anthropic`, **the data leaves the VPS**. Each call sends the
provider the system prompt, the user's message (the chat text or the transcription
of what was said to the robot) and the sensor context (battery, sonar, voice
direction, motor temperatures). The data is then subject to the provider's retention
policy. With `ollama`, everything stays on the VPS.

---

## Open WebUI — chat interface

**Image:** `ghcr.io/open-webui/open-webui:main`  
**Port:** 3000 → 8080 (exposed through Traefik)  
**URL:** `https://chat.$DOMAIN`

Access restricted to allowed Google accounts through SSO (OIDC). Open sign-ups are disabled.

| Variable | Description |
|---|---|
| `WEBUI_SECRET_KEY` | JWT secret for sessions |
| `WEBUI_ADMIN_EMAIL` | Administrator account email |
| `OAUTH_CLIENT_ID` | Google Client ID (console.cloud.google.com) |
| `OAUTH_CLIENT_SECRET` | Google secret |
| `OLLAMA_API_KEY` | Same value as `OLLAMA_API_KEYS` |

**Volume:** `/opt/ia/webui` → agents, users, conversation history

**OAuth callback URI to declare in the Google Cloud Console:**
```
https://chat.$DOMAIN/oauth/oidc/callback
```

### NAO Brain — WebUI chat as the robotic brain's interface

The `NAO Brain` Pipe (`robotics/brain/webui/nao_pipe.py`) connects the Open WebUI chat to the **same full pipeline** as speaking directly to NAO.

Each chat message is sent to the brain (`BRAIN_PROVIDER` / `BRAIN_MODEL`), without a wake word: "ok nao" is only for voice. The brain answers in natural language AND drives its physical bodies at the same time (NAO, room and stream voices, any body in `BODY_REGISTRY`) through ROS 2. The Twitch chat (`twitch-input`, [ADR 005](decisions/005-avatar-twitch.md)) arrives through the same `/chat` endpoint.

```
comment tu vas ?
dis bonjour et fais une révérence
mets les LEDs en violet et assieds-toi
```

**Flow:**

```
WebUI chat  ──"[message]"──►  nao_pipe.py (Pipe)
                                            │
                              POST http://chat-relay:8095/chat
                                            │
                                     body-gateway (_infer_sync)
                                            │
                                  LLM (BRAIN_MODEL)
                                            │
                               JSON { thoughts, emotion, actions }
                                            │
                    ┌───────────────────────┘
                    │
              ROS 2 topics
              ├─ /bodies/{static_01,obs_01}/actuators/say → connected voices
              │    (otherwise /bodies/nao_01/actuators/say: NAO speaks)
              ├─ /bodies/nao_01/actuators/motion → gesture
              └─ /bodies/nao_01/actuators/leds   → LEDs
                    │
              JSON response returned to the chat
              (speech + actions shown)
```

**Installing the Pipe (once, admin account):**

1. `https://chat.$DOMAIN` → **Workspace → Functions → (+)**
2. Paste the content of `robotics/brain/webui/nao_pipe.py`
3. Select the **Pipe** type → Save as **NAO Brain**
4. The **NAO Brain** model appears in the chat's model list

**Open WebUI background tasks** (title, tags, follow-up suggestions, search queries): Open WebUI hands them to the chat's model, so to the Pipe. Since version 0.8.0, the Pipe recognises them (`__task__` argument, or a prompt starting with `### Task:`) and answers them itself with a neutral value: they never reach `body-gateway`, NAO neither speaks nor moves on them and they no longer show up in the control room. To get real titles and tags, pick an Ollama model in **Admin Settings → Interface → Task Model (external models)**.

The Pipe talks to `body-gateway` through `chat-relay:8095` (valve `body_gateway_url`): `chat-relay` is a Caddy proxy on `ai-network` that relays to port 8095 of the host, where `body-gateway` listens in `network_mode: host`.

---

## n8n — workflow orchestrator

**Image:** `n8nio/n8n:latest`  
**Port:** 5678 (bound to `127.0.0.1` — not exposed publicly)  
**Access:** SSH tunnel only

```bash
ssh -L 5678:localhost:5678 $VPS_USER@$DOMAIN
# then open http://localhost:5678
```

| Variable | Description |
|---|---|
| `N8N_BASIC_AUTH_PASSWORD` | Interface password |

**Volume:** `/opt/ia/n8n` → workflow definitions

**Typical uses:**
- Trigger actions on the NAO robot through `body-gateway` (POST localhost:8095/chat)
- Chain LLM calls through Ollama
- Automate tasks on a schedule or an event

---

## ChromaDB — vector memory

**Image:** `chromadb/chroma:latest`  
**Port:** 8000 (internal only — never exposed)

Used by Open WebUI for RAG (Retrieval-Augmented Generation). Stores the embeddings of uploaded documents.

**Volume:** `/opt/ia/chroma` → persistent vector index

---

## Bitterbot — autonomous AI agent (disabled)

> See [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md) for the full context.

Bitterbot is commented out in `docker-compose.yml`. `body-gateway` calls **the
provider chosen** by `BRAIN_PROVIDER` (local Ollama by default, `qwen3:1.7b` — see
[Brain provider](#brain-provider-body-gateway)).

What is lost without Bitterbot: SQLite long-term memory, dream engine.  
What is kept: the whole physical control pipeline and the WebUI chat.

**To re-enable Bitterbot:**

1. Uncomment `bitterbot-gateway` in `docker-compose.yml`
2. In `body-gateway`, change: `BRAIN_URL=http://localhost:19001`
3. Redeploy

---

## Docker networks

```
ai-network (bridge, internet)
  └── ollama, open-webui, n8n, chromadb, whisper-asr, naoqi-bridge, chat-relay, twitch-input

bitterbot-net (bridge, internal — no internet access)
  └── (reserved for Bitterbot — see ADR 003)

robotics-net (bridge, internet)
  └── rosbridge-proxy, naoqi-bridge, obs-agent
```
