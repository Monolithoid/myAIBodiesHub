#!/usr/bin/env bash
# setup-vps.sh — VPS preparation before deploying with Coolify
#
# This script only covers what Coolify does not handle:
#   - Host volume tree (/opt/ia/)
#   - Firewall UFW
#   - bitterbot.json configuration (gateway token)
#   - rclone + systemd services (cloud sync)
#
# What Coolify handles (do not duplicate here):
#   - Docker installation
#   - docker compose pull + up -d (Deploy button)
#   - Environment variables (Coolify UI)
#   - Updates (Redeploy button)
#
# Usage: sudo bash setup-vps.sh
# Requirements: Ubuntu 22.04+ with root access, Coolify already installed

set -euo pipefail

# ============================================================
# CONFIGURATION
# ============================================================

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="/opt/ia"
BITTERBOT_CONFIG_SRC="${REPO_DIR}/core/bitterbot/bitterbot.json"
BITTERBOT_CONFIG_DST="${DATA_DIR}/bitterbot/config/bitterbot.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ============================================================
# HELPERS
# ============================================================

log()    { echo -e "${GREEN}[✓]${NC} $*"; }
info()   { echo -e "${CYAN}[→]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
error()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
section(){ echo -e "\n${CYAN}══ $* ══${NC}"; }

require_root() {
    [[ $EUID -eq 0 ]] || error "This script must be run as root: sudo bash $0"
}

ask() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [${default}] : ")" value
        echo "${value:-$default}"
    else
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
        echo "$value"
    fi
}

# ============================================================
# STEP 1 — Data tree
# ============================================================

create_directories() {
    section "Directory tree /opt/ia"
    local dirs=(
        "${DATA_DIR}/ollama"
        "${DATA_DIR}/webui"
        "${DATA_DIR}/n8n"
        "${DATA_DIR}/chroma"
        "${DATA_DIR}/nao/naoqi-sdk"
        "${DATA_DIR}/whisper"
        "${DATA_DIR}/bitterbot/config"
        "${DATA_DIR}/bitterbot/workspace"
        "${DATA_DIR}/sync/onedrive"
        "${DATA_DIR}/sync/gdrive"
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
        log "Created: $d"
    done
    # uid 1000 = node user in the Bitterbot container
    chown -R 1000:1000 "${DATA_DIR}/bitterbot"
    log "Permissions bitterbot : 1000:1000"
}

# ============================================================
# STEP 2 — UFW firewall
# ============================================================

configure_firewall() {
    section "Firewall UFW"
    if ! command -v ufw &>/dev/null; then
        apt-get install -y -q ufw
    fi

    ufw allow 22    comment "SSH"
    ufw allow 80    comment "HTTP (Coolify / Traefik)"
    ufw allow 443   comment "HTTPS (Coolify / Traefik)"
    ufw allow 8000  comment "Coolify UI"

    # body-gateway: allowed from the Docker bridge subnet (chat-relay → host:8095)
    # Docker typically creates bridges in 172.16-31.x.x or 10.x.x.x depending on the daemon
    ufw allow from 172.16.0.0/12 to any port 8095 comment "body-gateway depuis Docker bridge (ai-network)"
    ufw allow from 10.0.0.0/8    to any port 8095 comment "body-gateway depuis Docker bridge (ai-network, alt)"

    # rosbridge: only from rosbridge-proxy (Docker bridge), which checks the token.
    # The NAO goes through the static drone's relay (ADR 006), then Traefik over wss://.
    ufw allow from 172.16.0.0/12 to any port 9090 comment "rosbridge depuis rosbridge-proxy (Docker bridge)"
    ufw allow from 10.0.0.0/8    to any port 9090 comment "rosbridge depuis rosbridge-proxy (Docker bridge, alt)"

    # Internal ports — not exposed publicly
    ufw deny 11434  comment "Ollama (reverse proxy uniquement)"
    ufw deny 3000   comment "Open WebUI (reverse proxy uniquement)"
    ufw deny 5678   comment "n8n (SSH tunnel uniquement)"
    ufw deny 9090   comment "rosbridge (via rosbridge-proxy + jeton uniquement)"
    ufw deny 8095   comment "body-gateway (Docker bridge only, see allow rules above)"

    ufw --force enable
    log "UFW enabled."
    ufw status verbose
}

# ============================================================
# STEP 3 — Bitterbot configuration
# ============================================================

configure_bitterbot() {
    section "Configuration Bitterbot"

    if [[ -f "$BITTERBOT_CONFIG_DST" ]]; then
        warn "bitterbot.json already present — kept unchanged."
        return
    fi

    [[ -f "$BITTERBOT_CONFIG_SRC" ]] || error "Template introuvable : ${BITTERBOT_CONFIG_SRC}"

    # Generate the gateway token and print it to enter it in Coolify
    local token
    token=$(openssl rand -hex 32)

    cp "$BITTERBOT_CONFIG_SRC" "$BITTERBOT_CONFIG_DST"
    sed -i "s/CHANGE_THIS_BITTERBOT_TOKEN/${token}/g" "$BITTERBOT_CONFIG_DST"
    chown 1000:1000 "$BITTERBOT_CONFIG_DST"

    log "bitterbot.json configured."
    echo ""
    echo -e "${YELLOW}══ ACTION REQUIRED IN COOLIFY ══${NC}"
    echo -e "  Variable : ${CYAN}BITTERBOT_GATEWAY_TOKEN${NC}"
    echo -e "  Valeur   : ${GREEN}${token}${NC}"
    echo ""
    warn "Copy this value into Coolify → Environment Variables before deploying."
    echo ""
    echo -e "  Variable : ${CYAN}ROSBRIDGE_TOKEN${NC} (also to give to the NAO: --token)"
    echo -e "  Valeur   : ${GREEN}$(openssl rand -hex 32)${NC}"
    echo ""
    echo -e "  Variable : ${CYAN}OBS_TOKEN${NC} (OBS overlay secret: https://obs.DOMAIN/overlay?token=…)"
    echo -e "  Valeur   : ${GREEN}$(openssl rand -hex 32)${NC}"
    echo ""
    echo -e "  Variable : ${CYAN}REGIE_TOKEN${NC} (control room secret: https://regie.DOMAIN/?token=…)"
    echo -e "  Valeur   : ${GREEN}$(openssl rand -hex 32)${NC}"
    echo ""
    echo -e "  Variable : ${CYAN}GAME_TOKEN${NC} (games with the viewers: https://games.DOMAIN/overlay?token=…)"
    echo -e "  Valeur   : ${GREEN}$(openssl rand -hex 32)${NC}"
    echo ""
    echo -e "  Variable : ${CYAN}TWITCH_CHANNEL${NC} (Twitch channel whose chat NAO reads)"
}

# ============================================================
# STEP 4 — rclone + systemd services (optional cloud sync)
# ============================================================

setup_rclone() {
    section "rclone — synchronisation cloud (optionnel)"

    local install_rclone
    install_rclone=$(ask "Install rclone for OneDrive/GDrive sync? (y/n)" "y")
    [[ "$install_rclone" =~ ^[YyOo]$ ]] || { info "rclone skipped."; return; }

    if ! command -v rclone &>/dev/null; then
        info "Installing rclone…"
        curl -fsSL https://rclone.org/install.sh | bash
        log "rclone installed."
    else
        log "rclone already installed: $(rclone --version | head -1)"
    fi

    echo ""
    warn "rclone's OAuth configuration needs a browser."
    warn "Do the configuration FROM YOUR LOCAL MACHINE, then copy the file:"
    warn "  rclone config   # on your local machine"
    warn "  scp ~/.config/rclone/rclone.conf root@VPS_IP:/root/.config/rclone/rclone.conf"
    echo ""

    local setup_timers
    setup_timers=$(ask "Create the systemd sync services (rclone.conf already copied?) (y/n)" "n")
    [[ "$setup_timers" =~ ^[YyOo]$ ]] || return

    _create_rclone_service "onedrive" "onedrive:" "${DATA_DIR}/sync/onedrive"
    _create_rclone_service "gdrive"   "gdrive:"   "${DATA_DIR}/sync/gdrive"

    systemctl daemon-reload
    systemctl enable --now rclone-onedrive.timer rclone-gdrive.timer
    log "rclone timers enabled (sync every 15 min)."
    systemctl list-timers --all | grep rclone
}

_create_rclone_service() {
    local name="$1" remote="$2" dest="$3"

    cat > "/etc/systemd/system/rclone-${name}.service" <<EOF
[Unit]
Description=rclone sync — ${remote} → ${dest}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/rclone sync ${remote} ${dest} \
    --log-file=/var/log/rclone-${name}.log --log-level=INFO \
    --transfers=4 --checkers=8 --retries=3
EOF

    cat > "/etc/systemd/system/rclone-${name}.timer" <<EOF
[Unit]
Description=Sync ${name} every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Unit=rclone-${name}.service

[Install]
WantedBy=timers.target
EOF

    log "systemd service created: rclone-${name}"
}

# ============================================================
# MAIN
# ============================================================

main() {
    require_root

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║   AI stack — VPS preparation             ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
    echo ""

    create_directories
    configure_firewall
    configure_bitterbot
    setup_rclone

    echo ""
    echo -e "${GREEN}VPS preparation complete.${NC}"
    echo ""
    echo -e "${CYAN}Next steps in Coolify:${NC}"
    echo "  1. Create the "IA" project → New Resource → Docker Compose → this repository"
    echo "  2. Enter all the environment variables (see docs/coolify-vps.md § 5)"
    echo "  3. Click Deploy"
    echo ""
    echo -e "${CYAN}After the first Coolify deployment:${NC}"
    echo "  → Download an Ollama model:"
    echo "    docker exec -it ollama ollama pull qwen3:1.7b"
    echo ""
    echo "  → Deploy the NAO bridge (if a robot is present):"
    echo "    scp robotics/drones/nao/onboard/nao_ws_bridge.py nao@NAO_IP:/home/nao/"
    echo "    then on the NAO: python nao_ws_bridge.py --vps ws://DRONE_IP:9090/"
    echo "    (static drone relay: robotics/drones/static/onboard/nao-relay/)"
}

main "$@"
