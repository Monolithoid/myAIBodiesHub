#!/usr/bin/env bash
# setup-ubuntu.sh — Installs the static drone on an Ubuntu (laptop) PC
# with a webcam and a microphone. Automates pc-portable-ubuntu.md, steps 1 to 6
# (and 9, 10 as options).
#
# By default, nothing starts on its own and sleep is left alone: the drone is
# started by hand when needed (non-dedicated PC). On a PC dedicated to the
# drone, add --service and --no-sleep.
#
# Idempotent: it can be rerun as often as you like; it only redoes what is
# missing and does not rewrite settings already made in .env.
#
# Usage (as the drone user, not as root):
#   bash robotics/drones/static/onboard/setup-ubuntu.sh [options]
#
# Options:
#   --audio        Ask again for the default microphone and speaker
#   --service      Automatic start at boot (user service, step 9)
#   --no-sleep     No more sleep, even with the lid closed (step 10)
#   -h, --help     This help
#
# Without a terminal (e.g. via ssh -T), values can be passed through the
# environment: ROSBRIDGE_URL, ROSBRIDGE_TOKEN, CAMERA_INDEX, CAMERA_HFOV.
# They are then written to .env.
#
# The NAO relay (ADR 006) is installed afterwards with nao-relay/setup-ubuntu.sh.

set -euo pipefail

ONBOARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup-lib.sh
. "${ONBOARD_DIR}/setup-lib.sh"

ENV_FILE="${ONBOARD_DIR}/.env"
VENV_DIR="${ONBOARD_DIR}/.venv"
UNIT_SRC="${ONBOARD_DIR}/static-drone-user.service"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_DST="${UNIT_DIR}/static-drone.service"
LOGIND_DROPIN="/etc/systemd/logind.conf.d/50-static-drone.conf"
SLEEP_TARGETS=(sleep.target suspend.target hibernate.target hybrid-sleep.target)

APT_PACKAGES=(git python3-venv libportaudio2 libusb-1.0-0 mpg123
              alsa-utils v4l-utils pulseaudio-utils)

OPT_AUDIO=0
OPT_SERVICE=0
OPT_POWER=0
for arg in "$@"; do
    case "$arg" in
        --audio)      OPT_AUDIO=1 ;;
        --service)    OPT_SERVICE=1 ;;
        --no-sleep)   OPT_POWER=1 ;;
        -h|--help)    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            error "Unknown option: $arg (see --help)" ;;
    esac
done

# What changed and requires restarting the service at the end
NEED_RESTART=0
ENV_CREATED=0

# ============================================================
# 1. Paquets
# ============================================================

install_packages() {
    section "Packages"
    local missing=()
    for p in "${APT_PACKAGES[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" \
            || missing+=("$p")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log "Already installed: ${APT_PACKAGES[*]}"
        return
    fi
    info "Installation : ${missing[*]}"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    log "Packages installed"
}

# ============================================================
# 2. Code : environnement virtuel Python
# ============================================================

setup_venv() {
    section "Python environment"
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        info "Creating ${VENV_DIR}"
        python3 -m venv "$VENV_DIR"
        NEED_RESTART=1
    else
        log "venv present: ${VENV_DIR}"
    fi
    local before after
    before="$("${VENV_DIR}/bin/pip" freeze 2>/dev/null | md5sum)"
    "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check -r "${ONBOARD_DIR}/requirements.txt"
    # openWakeWord without its dependencies (see requirements-wakeword.txt)
    "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check --no-deps \
        -r "${ONBOARD_DIR}/requirements-wakeword.txt"
    after="$("${VENV_DIR}/bin/pip" freeze 2>/dev/null | md5sum)"
    if [[ "$before" != "$after" ]]; then
        NEED_RESTART=1
        log "Dependencies installed"
    else
        log "Dependencies up to date"
    fi
    # Shared openWakeWord models and hey_jarvis (to try WAKE_MODE=local
    # without an "ok nao" model): downloaded once.
    if "${VENV_DIR}/bin/python" -c \
        "import openwakeword.utils as u; u.download_models(['hey_jarvis'])" >/dev/null 2>&1; then
        log "openWakeWord models present"
    else
        warn "Unable to download the openWakeWord models (retried at startup with WAKE_MODE=local)"
    fi
    if [[ -f "${ONBOARD_DIR}/models/ok_nao.onnx" ]]; then
        log "\"ok nao\" model: models/ok_nao.onnx"
    else
        info "No models/ok_nao.onnx: WAKE_MODE=local requires training it (models/README.md)"
    fi
}

# ============================================================
# 3. Permissions: audio and video groups
# ============================================================

setup_groups() {
    section "audio / video groups"
    local g missing=()
    for g in audio video; do
        id -nG "$USER" | tr ' ' '\n' | grep -qx "$g" || missing+=("$g")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log "$USER is already in audio and video"
        return
    fi
    sudo usermod -aG "$(IFS=,; echo "${missing[*]}")" "$USER"
    warn "$USER added to ${missing[*]}: log out and back in (or reboot)"
}

# ============================================================
# 4. Default microphone and speaker (PipeWire / PulseAudio)
# ============================================================

# choose_default sources|sinks: shows the list and offers to choose one
choose_default() {
    local kind="$1" label="$2" current list choice name i=0
    if [[ "$kind" == sources ]]; then
        current="$(pactl get-default-source 2>/dev/null || true)"
        list="$(pactl list short sources | awk '$2 !~ /\.monitor$/ { print $2 }')"
    else
        current="$(pactl get-default-sink 2>/dev/null || true)"
        list="$(pactl list short sinks | awk '{ print $2 }')"
    fi
    [[ -n "$list" ]] || { warn "No ${label} found"; return; }
    echo "  ${label} disponibles :"
    while read -r name; do
        i=$((i + 1))
        if [[ "$name" == "$current" ]]; then
            echo "    $i) $name  (actuel)"
        else
            echo "    $i) $name"
        fi
    done <<< "$list"
    choice="$(ask "Number of the ${label} (Enter = keep the current one)" "")"
    [[ -n "$choice" ]] || { log "${label}: ${current:-default}"; return; }
    name="$(sed -n "${choice}p" <<< "$list")"
    [[ -n "$name" ]] || { warn "Invalid number, ${label} unchanged"; return; }
    if [[ "$kind" == sources ]]; then
        pactl set-default-source "$name"
    else
        pactl set-default-sink "$name"
    fi
    log "${label} : $name"
}

setup_audio() {
    section "Microphone and speaker"
    if ! pactl info >/dev/null 2>&1; then
        warn "Sound server unreachable (no session?). Choose later, in the session:"
        warn "  pactl set-default-source <microphone> ; pactl set-default-sink <speaker>"
        return
    fi
    if [[ $OPT_AUDIO -eq 0 && $ENV_CREATED -eq 0 ]] || ! is_interactive; then
        log "Microphone: $(pactl get-default-source 2>/dev/null || echo '?')"
        log "Speaker: $(pactl get-default-sink 2>/dev/null || echo '?')"
        info "To change: rerun with --audio, or Settings > Sound"
        return
    fi
    choose_default sources "microphone"
    choose_default sinks "speaker"
}

# ============================================================
# 5 and 6. Webcam and .env
# ============================================================

is_placeholder() {
    local v="$1"
    [[ -z "$v" || "$v" == "change-me" || "$v" == *example.com* ]]
}

setup_env() {
    section "Configuration (.env)"
    if [[ ! -f "$ENV_FILE" ]]; then
        cp "${ONBOARD_DIR}/.env.example" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        ENV_CREATED=1
        # Laptop settings (pc-portable-ubuntu.md, step 6)
        env_set "$ENV_FILE" RESPEAKER 0
        env_set "$ENV_FILE" MICS ""
        env_set "$ENV_FILE" CAMERA_HFOV 70
        log "Created from .env.example: $ENV_FILE"
    else
        log "Already exists: $ENV_FILE (values kept)"
    fi
    local before
    before="$(md5sum < "$ENV_FILE")"

    # Values passed through the environment: they take precedence
    local key
    for key in ROSBRIDGE_URL ROSBRIDGE_TOKEN CAMERA_INDEX CAMERA_HFOV; do
        if [[ -n "${!key:-}" ]]; then
            env_set "$ENV_FILE" "$key" "${!key}"
            log "$key taken from the environment"
        fi
    done

    # VPS connection: only asked for as long as it is not set
    local url domain token
    url="$(env_get "$ENV_FILE" ROSBRIDGE_URL)"
    domain="$(env_get "$ENV_FILE" DOMAIN)"
    if is_placeholder "$url" && is_placeholder "$domain"; then
        url="$(ask "VPS rosbridge URL (e.g. wss://rosbridge.mydomain.com)" "")"
        if [[ -n "$url" ]]; then
            env_set "$ENV_FILE" ROSBRIDGE_URL "$url"
        else
            warn "ROSBRIDGE_URL to set in $ENV_FILE"
        fi
    fi
    token="$(env_get "$ENV_FILE" ROSBRIDGE_TOKEN)"
    if is_placeholder "$token"; then
        token="$(ask_secret "ROSBRIDGE_TOKEN (same value as on the VPS)")"
        if [[ -n "$token" ]]; then
            env_set "$ENV_FILE" ROSBRIDGE_TOKEN "$token"
        else
            warn "ROSBRIDGE_TOKEN to set in $ENV_FILE"
        fi
    fi

    # Webcam: only asked for on the first run
    if [[ $ENV_CREATED -eq 1 && -z "${CAMERA_INDEX:-}" ]] && is_interactive; then
        echo "  Cameras (the first /dev/video of each camera gives the image):"
        v4l2-ctl --list-devices 2>/dev/null | sed 's/^/    /' || warn "No camera found"
        local cam hfov
        cam="$(ask "CAMERA_INDEX: partial name (e.g. C920), index or path" "0")"
        env_set "$ENV_FILE" CAMERA_INDEX "$cam"
        hfov="$(ask "CAMERA_HFOV: horizontal field of view in degrees" "70")"
        env_set "$ENV_FILE" CAMERA_HFOV "$hfov"
    fi
    log "Camera: CAMERA_INDEX=$(env_get "$ENV_FILE" CAMERA_INDEX), CAMERA_HFOV=$(env_get "$ENV_FILE" CAMERA_HFOV)"

    [[ "$(md5sum < "$ENV_FILE")" == "$before" ]] || NEED_RESTART=1
}

env_ready() {
    local url domain token
    url="$(env_get "$ENV_FILE" ROSBRIDGE_URL)"
    domain="$(env_get "$ENV_FILE" DOMAIN)"
    token="$(env_get "$ENV_FILE" ROSBRIDGE_TOKEN)"
    { ! is_placeholder "$url" || ! is_placeholder "$domain"; } && ! is_placeholder "$token"
}

# ============================================================
# 9. Automatic start (user service)
# ============================================================

setup_service() {
    section "Automatic start"
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        warn "user systemd unreachable: rerun from a session of $USER (desktop or ssh)"
        return
    fi

    # The repository file assumes ~/ia-agent: put the real path in it
    local tmp
    tmp="$(mktemp)"
    sed "s|%h/ia-agent/robotics/drones/static/onboard|${ONBOARD_DIR}|g" "$UNIT_SRC" > "$tmp"
    mkdir -p "$UNIT_DIR"
    if install_if_changed "$tmp" "$UNIT_DST"; then
        systemctl --user daemon-reload
        NEED_RESTART=1
        log "Service installed: $UNIT_DST"
    else
        log "Service up to date: $UNIT_DST"
    fi
    rm -f "$tmp"

    if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
        sudo loginctl enable-linger "$USER"
        log "Linger enabled: the service starts at boot, without an open session"
    else
        log "Linger already enabled"
    fi

    systemctl --user enable -q static-drone
    if ! env_ready; then
        warn "Service enabled but not started: fill in ROSBRIDGE_URL and ROSBRIDGE_TOKEN in .env, then rerun this script"
        return
    fi
    # Also restarts after a change to .env made by hand
    if [[ $NEED_RESTART -eq 1 ]] || is_stale static-drone --user "$ENV_FILE" "$UNIT_DST"; then
        systemctl --user restart static-drone
        log "Service (re)started"
    else
        log "Service already running, nothing changed"
    fi
}

# ============================================================
# 10. No sleep
# ============================================================

setup_power() {
    section "Sleep"
    local t to_mask=()
    for t in "${SLEEP_TARGETS[@]}"; do
        [[ "$(systemctl is-enabled "$t" 2>/dev/null || true)" == "masked" ]] || to_mask+=("$t")
    done
    if [[ ${#to_mask[@]} -gt 0 ]]; then
        sudo systemctl mask "${to_mask[@]}"
        log "Sleep disabled (${to_mask[*]})"
    else
        log "Sleep already disabled"
    fi

    # Lid closed: do nothing. A separate file rather than editing
    # logind.conf: simpler to review and undo (delete the file).
    local tmp
    tmp="$(mktemp)"
    cat > "$tmp" <<'EOF'
# Static drone (setup-ubuntu.sh): the laptop stays on with the lid closed.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
    sudo mkdir -p "$(dirname "$LOGIND_DROPIN")"
    if install_if_changed "$tmp" "$LOGIND_DROPIN" sudo; then
        # Restarting systemd-logind would close the graphical session: wait for
        # the next reboot.
        warn "Closed lid ignored from the next reboot on ($LOGIND_DROPIN)"
    else
        log "Closed lid already ignored"
    fi
    rm -f "$tmp"
    info "Keep the laptop on mains power. With the lid closed, use a USB webcam and microphone."
}

# ============================================================
# MAIN
# ============================================================

main() {
    echo -e "${CYAN}Static drone — Ubuntu installation${NC}  (${ONBOARD_DIR})"
    require_user_with_sudo
    require_ubuntu

    install_packages
    setup_venv
    setup_groups
    setup_env
    setup_audio
    if [[ $OPT_SERVICE -eq 1 ]]; then setup_service; fi
    if [[ $OPT_POWER -eq 1 ]]; then setup_power; fi

    section "Done"
    if [[ $OPT_SERVICE -eq 1 ]]; then
        info "Log: journalctl --user -u static-drone -f"
    else
        info "Run by hand: cd ${ONBOARD_DIR} && .venv/bin/python static_drone.py"
        if [[ -f "$UNIT_DST" ]]; then
            warn "An automatic start installed earlier is still in place."
            warn "To remove it: systemctl --user disable --now static-drone"
        fi
    fi
    info "NAO relay (ADR 006): bash ${ONBOARD_DIR}/nao-relay/setup-ubuntu.sh"
}

main
