#!/usr/bin/env bash
# setup-ubuntu.sh — Installs the NAO relay (ADR 006) on the Ubuntu static
# drone: the NAO talks ws:// to the drone, Caddy relays to the VPS
# over wss:// and adds the token. Automates nao-relay/README.md, steps 1 to 3.
#
# By default, the relay does not start on its own: it is started by hand with
# nao-relay/run.sh when needed (non-dedicated PC). --service installs the
# nao-relay service, started at boot.
#
# Idempotent: it can be rerun as often as you like; it only redoes what is
# missing and only restarts the service if its configuration changed.
#
# Requirements: the drone is installed (../setup-ubuntu.sh), its .env contains
# ROSBRIDGE_TOKEN and ROSBRIDGE_URL (or DOMAIN).
#
# Usage (as the drone user, not as root):
#   bash robotics/drones/static/onboard/nao-relay/setup-ubuntu.sh [--service]
#
# Without a terminal, NAO_RELAY_UPSTREAM, NAO_IP and NAO_RELAY_PORT can be
# passed through the environment; they are then written to the drone's .env.

set -euo pipefail

RELAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONBOARD_DIR="$(dirname "$RELAY_DIR")"
# shellcheck source=SCRIPTDIR/../setup-lib.sh
. "${ONBOARD_DIR}/setup-lib.sh"

ENV_FILE="${ONBOARD_DIR}/.env"
CADDYFILE="${RELAY_DIR}/Caddyfile"
UNIT_SRC="${RELAY_DIR}/nao-relay.service"
UNIT_DST="/etc/systemd/system/nao-relay.service"
LOCAL_NETS=(192.168.0.0/16 172.16.0.0/12 10.0.0.0/8)

CADDY_KEYRING="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
CADDY_LIST="/etc/apt/sources.list.d/caddy-stable.list"

OPT_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --service) OPT_SERVICE=1 ;;
        -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         error "Unknown option: $arg (see --help)" ;;
    esac
done

is_placeholder() {
    local v="$1"
    [[ -z "$v" || "$v" == "change-me" || "$v" == *example.com* ]]
}

# ============================================================
# Configuration (drone .env)
# ============================================================

# Derives rosbridge's https:// address from ROSBRIDGE_URL or DOMAIN
guess_upstream() {
    local url domain
    url="$(env_get "$ENV_FILE" ROSBRIDGE_URL)"
    domain="$(env_get "$ENV_FILE" DOMAIN)"
    if ! is_placeholder "$url"; then
        url="${url/#wss:\/\//https://}"
        url="${url/#ws:\/\//http://}"
        # Caddy wants a host, not a path
        echo "$url" | sed -E 's#^(https?://[^/]+).*#\1#'
    elif ! is_placeholder "$domain"; then
        echo "https://rosbridge.${domain}"
    fi
}

setup_env() {
    section "Configuration (drone .env)"
    [[ -f "$ENV_FILE" ]] \
        || error "$ENV_FILE missing: install the drone first (bash ${ONBOARD_DIR}/setup-ubuntu.sh)"
    is_placeholder "$(env_get "$ENV_FILE" ROSBRIDGE_TOKEN)" \
        && error "ROSBRIDGE_TOKEN to set in $ENV_FILE (same value as on the VPS)"

    local key
    for key in NAO_RELAY_UPSTREAM NAO_IP NAO_RELAY_PORT; do
        if [[ -n "${!key:-}" ]]; then
            env_set "$ENV_FILE" "$key" "${!key}"
            log "$key taken from the environment"
        fi
    done

    local upstream guess
    upstream="$(env_get "$ENV_FILE" NAO_RELAY_UPSTREAM)"
    if is_placeholder "$upstream"; then
        guess="$(guess_upstream)"
        upstream="$(ask "VPS rosbridge over https:// (same host as ROSBRIDGE_URL)" "$guess")"
        [[ -n "$upstream" ]] || error "NAO_RELAY_UPSTREAM to set in $ENV_FILE"
        env_set "$ENV_FILE" NAO_RELAY_UPSTREAM "$upstream"
    fi
    [[ "$upstream" == https://* ]] \
        || warn "NAO_RELAY_UPSTREAM=$upstream is not https://: the token would be sent unencrypted"
    log "Relaying to: $upstream"

    if env_has "$ENV_FILE" NAO_IP && [[ -z "$(env_get "$ENV_FILE" NAO_IP)" ]]; then
        warn "NAO_IP= is empty in $ENV_FILE: the relay will refuse everyone."
        warn "Comment it out (#NAO_IP=) to accept the local network, or set the NAO's address."
    elif env_has "$ENV_FILE" NAO_IP; then
        log "Accepted addresses: $(env_get "$ENV_FILE" NAO_IP)"
    else
        log "Accepted addresses: the whole local network"
    fi
}

relay_port() {
    local p
    p="$(env_get "$ENV_FILE" NAO_RELAY_PORT)"
    echo "${p:-9090}"
}

# ============================================================
# Caddy
# ============================================================

install_caddy() {
    section "Caddy"
    if command -v caddy >/dev/null; then
        log "Caddy present: $(caddy version | cut -d' ' -f1)"
    else
        local candidate
        candidate="$(apt-cache policy caddy 2>/dev/null | awk '/Candidate:/ { print $2 }')"
        if [[ -z "$candidate" || "$candidate" == "(none)" ]]; then
            # Ubuntu 22.04: no caddy in the repositories, official repository
            info "Adding Caddy's official repository"
            sudo apt-get update -qq
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gpg
            if [[ ! -s "$CADDY_KEYRING" ]]; then
                curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
                    | sudo gpg --dearmor -o "$CADDY_KEYRING"
            fi
            if [[ ! -s "$CADDY_LIST" ]]; then
                curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
                    | sudo tee "$CADDY_LIST" >/dev/null
            fi
        fi
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
        log "Caddy installed: $(caddy version | cut -d' ' -f1)"
    fi

    # The package starts a default Caddy (port 80): nao-relay replaces it
    if systemctl is-enabled -q caddy 2>/dev/null || systemctl is-active -q caddy 2>/dev/null; then
        sudo systemctl disable --now caddy
        log "Default caddy service stopped and disabled"
    else
        log "Default caddy service already disabled"
    fi

    command -v curl >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl
}

# Checks the Caddyfile with the .env values, as systemd will
validate_caddyfile() {
    local vars=() key
    for key in NAO_RELAY_UPSTREAM ROSBRIDGE_TOKEN NAO_IP NAO_RELAY_PORT; do
        env_has "$ENV_FILE" "$key" && vars+=("$key=$(env_get "$ENV_FILE" "$key")")
    done
    if env -i HOME="$HOME" PATH="$PATH" "${vars[@]}" \
        caddy validate --adapter caddyfile --config "$CADDYFILE" >/dev/null 2>&1; then
        log "Caddyfile valide"
    else
        env -i HOME="$HOME" PATH="$PATH" "${vars[@]}" \
            caddy validate --adapter caddyfile --config "$CADDYFILE" || true
        error "Caddyfile refused with the .env values (see above)"
    fi
}

# ============================================================
# Service nao-relay
# ============================================================

setup_service() {
    section "nao-relay service"

    # The repository file assumes User=pi and ~pi/ia-agent: put the real
    local tmp changed=0
    tmp="$(mktemp)"
    sed -e "s|^User=.*|User=${USER}|" \
        -e "s|/home/pi/ia-agent/robotics/drones/static/onboard|${ONBOARD_DIR}|g" \
        "$UNIT_SRC" > "$tmp"
    if install_if_changed "$tmp" "$UNIT_DST" sudo; then
        sudo systemctl daemon-reload
        changed=1
        log "Service installed: $UNIT_DST"
    else
        log "Service up to date: $UNIT_DST"
    fi
    rm -f "$tmp"

    sudo systemctl enable -q nao-relay
    if [[ $changed -eq 1 ]] || is_stale nao-relay "$ENV_FILE" "$CADDYFILE" "$UNIT_DST"; then
        sudo systemctl restart nao-relay
        log "Relay (re)started"
    else
        log "Relay already running, nothing changed"
    fi
}

# ============================================================
# Firewall (only if active)
# ============================================================

setup_firewall() {
    section "Firewall"
    if ! command -v ufw >/dev/null || ! sudo ufw status | grep -q "^Status: active"; then
        log "ufw inactive: nothing to open"
        return
    fi
    local port sources src
    port="$(relay_port)"
    if env_has "$ENV_FILE" NAO_IP && [[ -n "$(env_get "$ENV_FILE" NAO_IP)" ]]; then
        read -ra sources <<< "$(env_get "$ENV_FILE" NAO_IP)"
    else
        sources=("${LOCAL_NETS[@]}")
    fi
    for src in "${sources[@]}"; do
        # ufw ignores a rule that already exists
        sudo ufw allow from "$src" to any port "$port" proto tcp >/dev/null
    done
    log "Port $port open to: ${sources[*]}"
}

# ============================================================
# Check
# ============================================================

check_relay() {
    section "Check"
    local port code temp_pid=""
    port="$(relay_port)"
    if ! systemctl is-active -q nao-relay 2>/dev/null; then
        # No service: relay started for the test, then stopped
        bash "${RELAY_DIR}/run.sh" >/dev/null 2>&1 &
        temp_pid=$!
    fi
    for _ in 1 2 3 4 5; do
        code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
            -H "Connection: Upgrade" -H "Upgrade: websocket" \
            -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
            "http://127.0.0.1:${port}/" || true)"
        [[ "$code" == "000" ]] || break
        sleep 1
    done
    if [[ -n "$temp_pid" ]]; then
        kill "$temp_pid" 2>/dev/null || true
        wait "$temp_pid" 2>/dev/null || true
    fi
    case "$code" in
        101) log "The relay and the VPS answer (101 Switching Protocols)" ;;
        401) warn "401: token refused by the VPS (ROSBRIDGE_TOKEN differs from the VPS one)" ;;
        403) warn "403: address refused by the relay (see NAO_IP)" ;;
        502) warn "502: the drone cannot reach NAO_RELAY_UPSTREAM" ;;
        000) warn "The relay does not answer on port $port (port already taken? run run.sh to see the error)" ;;
        *)   warn "Unexpected response: $code" ;;
    esac
}

next_steps() {
    section "NAO side"
    local ip port
    ip="$(hostname -I | awk '{ print $1 }')"
    port="$(relay_port)"
    info "The drone must keep the same address: DHCP reservation on the router for ${ip:-this machine}."
    info "In /home/nao/autostart.sh, remove \"export ROSBRIDGE_TOKEN=...\" and run:"
    echo "    python /home/nao/nao_ws_bridge.py --vps ws://${ip:-<drone IP>}:${port}/ --provision &"
    if [[ $OPT_SERVICE -eq 1 ]]; then
        info "Relay log: journalctl -u nao-relay -f"
    else
        info "Start the relay (Ctrl+C to stop it): bash ${RELAY_DIR}/run.sh"
        if [[ -f "$UNIT_DST" ]]; then
            warn "A nao-relay service installed earlier is still in place."
            warn "To remove it: sudo systemctl disable --now nao-relay"
        fi
    fi
}

# ============================================================
# MAIN
# ============================================================

main() {
    echo -e "${CYAN}NAO relay — Ubuntu installation${NC}  (${RELAY_DIR})"
    require_user_with_sudo
    require_ubuntu

    setup_env
    install_caddy
    validate_caddyfile
    if [[ $OPT_SERVICE -eq 1 ]]; then setup_service; fi
    setup_firewall
    check_relay
    next_steps
}

main
