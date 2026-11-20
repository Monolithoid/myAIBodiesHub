# Coolify & VPS

Deploying the stack on an Ubuntu VPS with Docker Compose managed by Coolify.

---

## 1. VPS requirements

Connect over SSH, clone this repository, then create the persistent data tree:

```bash
git clone https://github.com/monolithoid/ia-agent.git /opt/ia-agent
cd /opt/ia-agent
```

```bash
# Main stack
mkdir -p /opt/ia/ollama /opt/ia/webui /opt/ia/n8n /opt/ia/chroma

# Robotics: NAOqi SDK (to extract by hand) and Whisper models
mkdir -p /opt/ia/nao/naoqi-sdk /opt/ia/whisper
# Extract pynaoqi-python2.7-2.1.4.*-linux64.tar.gz (SoftBank portal) into /opt/ia/nao/naoqi-sdk/

# Bitterbot
mkdir -p /opt/ia/bitterbot/config /opt/ia/bitterbot/workspace
mkdir -p /opt/ia/sync/onedrive /opt/ia/sync/gdrive
chown -R 1000:1000 /opt/ia/bitterbot   # node uid in the container

# Copy the Bitterbot config template
cp core/bitterbot/bitterbot.json /opt/ia/bitterbot/config/bitterbot.json
# Edit this file: set BITTERBOT_GATEWAY_TOKEN (same value as in Coolify)
```

A `setup-vps.sh` script at the root of the repository automates these steps (run it from the clone):

```bash
sudo bash /opt/ia-agent/setup-vps.sh
```

---

## 2. Firewall (UFW)

```bash
ufw allow 22
ufw allow 80
ufw allow 443
ufw allow from 172.16.0.0/12 to any port 8095  # chat-relay (Docker bridge) → body-gateway
ufw allow from 10.0.0.0/8 to any port 8095     # same, other Docker range
ufw allow from 172.16.0.0/12 to any port 9090  # rosbridge-proxy (Docker bridge) → ros2-bridge
ufw allow from 10.0.0.0/8 to any port 9090     # same, other Docker range
ufw deny 9090   # rosbridge is only reachable through Traefik + rosbridge-proxy (token)
ufw deny 11434
ufw deny 3000
ufw deny 5678
ufw deny 8095   # body-gateway is only reachable from the Docker networks (chat-relay)
ufw enable
```

`setup-vps.sh` applies these rules (and also opens port 8000 of the Coolify interface).

> Port 19001 (Bitterbot) is not exposed directly. Port 9090 (rosbridge) is no longer exposed: everything goes through `rosbridge-proxy`, which requires the header `Authorization: Bearer $ROSBRIDGE_TOKEN`. All clients use `wss://rosbridge.$DOMAIN` (443); the NAO (Python 2.7, no TLS) goes through the static drone's relay ([ADR 006](decisions/006-nao-via-drone-statique.md)), there is no more plain-HTTP rosbridge. If an old rule `ufw allow from NAO_PUBLIC_IP to any port 9090` exists, remove it with `ufw delete`.

---

## 3. Domains and DNS

Public domains are chosen in Coolify: resource **"IA"** → each service → **Domains**
field. Put the container port at the end of the URL (it is not public, it only tells
Traefik which container port to send the traffic to). The names below are examples:

| Service | Domains field in Coolify (example) | Role |
|---|---|---|
| `open-webui` | `https://chat.$DOMAIN:8080` | Open WebUI |
| `ollama` | `https://ollama.$DOMAIN:11434` | Ollama API |
| `rosbridge-proxy` | `https://rosbridge.$DOMAIN:80` | rosbridge (static drone, NAO relay); TLS only |
| `obs-agent` | `https://obs.$DOMAIN:8096` | OBS overlay, stream voice ([ADR 005](decisions/005-avatar-twitch.md)) |
| `regie` | `https://regie.$DOMAIN:8097` | Live view of the streams ([ADR 007](decisions/007-regie-topic-events.md)) |

Each chosen domain must have an A record pointing to the VPS IP. The `rosbridge-proxy`
domain is the one to put in `ROSBRIDGE_URL` (`wss://…`) and `NAO_RELAY_UPSTREAM`
(`https://…`) on the static drone.

---

## 4. Setting up the project in Coolify

1. Coolify → **Projects** → create the **"IA"** project
2. **New Resource** → **Docker Compose** → point to this Git repository
3. Leave the compose path as `docker-compose.yml` (root)
4. Go to **Environment Variables** and add all the variables below

---

## 5. Environment variables

All sensitive variables are injected by Coolify. No secret is committed to the repository.

| Variable | How to generate / get it |
|---|---|
| `DOMAIN` | Your root domain (e.g. `mon-domain.com`) |
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `OLLAMA_API_KEYS` | `openssl rand -hex 32` |
| `WEBUI_SECRET_KEY` | `openssl rand -hex 32` |
| `WEBUI_ADMIN_EMAIL` | Your email address |
| `OAUTH_CLIENT_ID` | [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials → OAuth 2.0 Client ID |
| `OAUTH_CLIENT_SECRET` | Same, same credential |
| `N8N_BASIC_AUTH_PASSWORD` | A strong password of your choice |
| `BITTERBOT_GATEWAY_TOKEN` | `openssl rand -hex 32` — must match `gateway.auth.token` in `bitterbot.json` |
| `ROSBRIDGE_TOKEN` | `openssl rand -hex 32` — token required by `rosbridge-proxy`; same value to give to the NAO (`nao_ws_bridge.py --token`), the static drone, `obs-agent` and `regie`. Without it, `rosbridge-proxy` refuses to start |
| `TWITCH_CHANNEL` | Channel whose chat is read (e.g. `monolithoid`). Without it, `twitch-input` refuses to start and restarts in a loop |
| `OBS_TOKEN` | `openssl rand -hex 32` — secret of the OBS overlay URL. Without it (or without `ROSBRIDGE_TOKEN`), `obs-agent` refuses to start |
| `REGIE_TOKEN` | `openssl rand -hex 32` — secret of the control room URL. Without it (or without `ROSBRIDGE_TOKEN`), `regie` refuses to start |

Optional variables (defaults in `docker-compose.yml`, whose header describes each one):

| Group | Variables |
|---|---|
| Brain ([AI stack](ia-stack.md#brain-provider-body-gateway)) | `BRAIN_PROVIDER`, `BRAIN_URL`, `BRAIN_MODEL`, `BRAIN_API_KEY`, `BRAIN_EFFORT`, `BRAIN_TIMEOUT` (personality: file `/opt/ia/nao/persona/personnalite.md`) |
| Bodies | `NAO_ROBOT_ID`, `STATIC_DRONE_ID`, `OBS_DRONE_ID` (empty = body disabled), `NAO_HEAD_YAW_OFFSET`, `NAO_SPEAKING_SYNC`, `SPEECH_GESTURE_SYNC`, `SPEECH_CHARS_PER_S`, `LLM_MOTION` |
| Voice | `WAKE_WORD`, `WAKE_WINDOW_S`, `LISTEN_DURATION`, `ASR_MODEL`, `TTS_VOICE` |
| Control room | `OBS_URL`: `obs-agent` domain (e.g. `obs.$DOMAIN`, `https://` added if missing), for the control room's "OBS : overlay voix" link |
| Twitch | `TWITCH_COMMAND_PREFIX`, `TWITCH_BLOCKLIST`, `TWITCH_MODS_ONLY` |
| Dreams | `DREAMING_HOUR_START`, `DREAMING_HOUR_END`, `DREAMING_BATTERY_THRESHOLD`, `DREAM_TICK_INTERVAL`, `TZ` |
| Open WebUI voice notifications | `WEBUI_URL`, `WEBUI_API_KEY`, `WEBUI_PIPE_MODEL`, `WEBUI_VOICE_CHAT_ID` |

**Callback URI to declare in the Google Cloud Console:**
```
https://chat.$DOMAIN/oauth/oidc/callback
```

---

## 6. Docker image CI/CD pipeline

Seven images are built automatically by GitHub Actions and published to Docker Hub. The VPS only runs `docker pull` — no build on the server.

### a) Create a Docker Hub Access Token

Docker Hub → Account Settings → **Security** → **New Access Token**
- Description: `github-actions-ia-agent`
- Permissions: `Read & Write`
- Copy the token (shown only once)

### b) Add the secrets in GitHub

GitHub → repo → **Settings** → **Secrets and variables** → **Actions**:

| Name | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | The Docker Hub token |

### c) Trigger the first builds

GitHub → **Actions**:
- Workflow **"Build & Push — Bitterbot image"** → **Run workflow**
- Workflow **"Build & Push — Robotics images"** → **Run workflow**

Each build takes 5–15 min. The images produced:
- `$DOCKERHUB_USERNAME/bitterbot:latest`
- `$DOCKERHUB_USERNAME/naoqi-bridge:latest`
- `$DOCKERHUB_USERNAME/nao-core:latest`
- `$DOCKERHUB_USERNAME/body-gateway:latest`
- `$DOCKERHUB_USERNAME/ros2-bridge:latest`
- `$DOCKERHUB_USERNAME/twitch-input:latest`
- `$DOCKERHUB_USERNAME/obs-agent:latest`
- `$DOCKERHUB_USERNAME/regie:latest`

**Automatic rebuilds:**
- Bitterbot: every Monday at 3:00 UTC
- Robotics images: on every push to `main` that changes `robotics/**`

---

## 7. Starting the stack

In Coolify → resource **"IA"** → **Deploy** button (or **Start**).

Coolify runs `docker compose pull && docker compose up -d`. The data in `/opt/ia/` is kept across restarts.

**First start — download an LLM model:**
```bash
docker exec -it ollama ollama pull qwen3:1.7b   # body-gateway's default model (BRAIN_MODEL)
```

**Stream voice in OBS** ([ADR 005](decisions/005-avatar-twitch.md)): add a **browser
source** with the URL `https://<obs-agent domain>/overlay?token=$OBS_TOKEN` and tick
**"Control audio via OBS"**. The voice becomes a mixer source, with subtitles.
The `obs_01` body is only connected (and only receives speech) while this source is open.
If OBS blocks audio autoplay (still meter, red « Son bloqué » banner in the overlay):
right-click the source → **Interact**, then click once in the page. Redo this after
every OBS restart or source reload.

**Control room** ([ADR 007](decisions/007-regie-topic-events.md)): open
`https://<regie domain>/?token=$REGIE_TOKEN` to see live each input, the LLM decision and
the actions sent to each body.

---

## 8. Updating the services

From Coolify: **Redeploy** button on the resource (or **Restart** on a service).

> Coolify deploys from its own clone of the repository (under `/data/coolify/…`), with its
> variables. Run in `/opt/ia-agent`, `docker compose …` sees neither this project nor these
> variables (`variable is not set` warnings, empty list): go through Coolify, or use
> `docker` with the container name (`docker restart obs-agent`).

---

## 9. Cloud sync — rclone

The `/opt/ia/sync/onedrive` and `/opt/ia/sync/gdrive` folders are fed by `rclone` on the VPS and mounted read-only in Bitterbot's workspace.

### Installation

```bash
curl https://rclone.org/install.sh | sudo bash
```

### Configuration (from your local machine)

OAuth authentication needs a browser — do the configuration locally, then copy it to the VPS.

**Google Drive:**
```bash
rclone config
# → n, name: gdrive, Storage: drive, leave client_id empty, scope: 1
# → browser authentication, no Shared Drive
```

**OneDrive:**
```bash
rclone config
# → n, name: onedrive, Storage: onedrive, leave client_id empty
# → browser authentication, type: onedrive, drive_id: 0
```

**Copy the config to the VPS:**
```bash
scp ~/.config/rclone/rclone.conf $VPS_USER@$DOMAIN:/root/.config/rclone/rclone.conf
```

### systemd services (sync every 15 min)

Create `/etc/systemd/system/rclone-onedrive.service`:
```ini
[Unit]
Description=rclone sync — OneDrive → /opt/ia/sync/onedrive
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/rclone sync onedrive: /opt/ia/sync/onedrive \
    --log-file=/var/log/rclone-onedrive.log --log-level=INFO \
    --transfers=4 --checkers=8 --retries=3
```

Create `/etc/systemd/system/rclone-onedrive.timer`:
```ini
[Unit]
Description=Sync OneDrive every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Unit=rclone-onedrive.service

[Install]
WantedBy=timers.target
```

Repeat for `rclone-gdrive` (replace `onedrive:` with `gdrive:` and the paths).

```bash
systemctl daemon-reload
systemctl enable --now rclone-onedrive.timer rclone-gdrive.timer
systemctl list-timers --all | grep rclone
```

---

## 10. Diagnostics

```bash
# State of all containers (not "docker compose ps", see § 8)
docker ps --format "table {{.Names}}\t{{.Status}}"

# Logs of a service
docker logs ollama -f
docker logs body-gateway -f
docker logs nao-core -f
docker logs ros2-bridge -f
docker logs twitch-input -f
docker logs obs-agent -f

# Check Bitterbot's network isolation (when re-enabled)
docker exec bitterbot-gateway curl -s --max-time 3 https://example.com
# Expected: timeout or "Could not resolve host"

# Check that Bitterbot reaches Ollama (when re-enabled)
docker exec bitterbot-gateway curl -s http://ollama:11434/api/tags | head -c 100
```
