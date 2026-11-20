#!/usr/bin/env bash
# tmux.sh — Starts the whole static drone in one tmux session, in the
# foreground and without systemd: one pane per program, stacked vertically.
#
#   ┌──────────────────────────┐
#   │ relay   nao-relay/run.sh │
#   ├──────────────────────────┤
#   │ static  static_drone.py  │
#   ├──────────────────────────┤
#   │ gesture emotion_gesture  │
#   └──────────────────────────┘
#
# Usage : bash robotics/drones/static/onboard/tmux.sh [session-name]
#   session-name : tmux session name (default: $TMUX_SESSION or "static-drone")
#
# If the session already exists, it is reattached instead of being recreated.
# To stop everything: tmux kill-session -t <session-name>
# A pane stays open after its program exits (see its last lines), and
# "Ctrl+B :respawn-pane -k" restarts it.

set -euo pipefail

ONBOARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/setup-lib.sh
. "${ONBOARD_DIR}/setup-lib.sh"

SESSION="${1:-${TMUX_SESSION:-static-drone}}"
PYTHON="${ONBOARD_DIR}/.venv/bin/python"

command -v tmux >/dev/null || error "tmux absent : sudo apt install -y tmux"
[[ -x "$PYTHON" ]] || error "$PYTHON absent : lancer d'abord setup-ubuntu.sh"
[[ -f "${ONBOARD_DIR}/.env" ]] || error "${ONBOARD_DIR}/.env absent : cp .env.example .env"
[[ -f "${ONBOARD_DIR}/emotion_gesture.env" ]] \
    || warn "emotion_gesture.env absent : cp emotion_gesture.env.example emotion_gesture.env"

# Attach (or switch, from inside tmux) to an existing session
attach() {
    if [[ -n "${TMUX:-}" ]]; then
        exec tmux switch-client -t "$SESSION"
    else
        exec tmux attach-session -t "$SESSION"
    fi
}

if tmux has-session -t "=$SESSION" 2>/dev/null; then
    info "Session tmux « $SESSION » déjà lancée : rattachement"
    attach
fi

# The systemd services would run a second copy of the same programs
for unit in static-drone emotion-gesture; do
    if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
        warn "Service $unit actif : systemctl --user stop $unit (sinon doublon)"
    fi
done
if systemctl is-active --quiet nao-relay 2>/dev/null; then
    warn "Service nao-relay actif : sudo systemctl stop nao-relay (sinon port déjà pris)"
fi

# Each pane runs its program and drops into a shell when it exits, so the
# error stays readable.
pane_cmd() { printf '%s; echo; echo "[%s terminé, code $?]"; exec bash' "$1" "$2"; }

tmux new-session -d -s "$SESSION" -n drone -c "$ONBOARD_DIR" \
    "$(pane_cmd "bash nao-relay/run.sh" relay)"
tmux split-window -v -t "$SESSION:drone" -c "$ONBOARD_DIR" \
    "$(pane_cmd "PYTHONUNBUFFERED=1 '$PYTHON' static_drone.py" static)"
tmux split-window -v -t "$SESSION:drone" -c "$ONBOARD_DIR" \
    "$(pane_cmd "PYTHONUNBUFFERED=1 '$PYTHON' emotion_gesture.py" gesture)"
tmux select-layout -t "$SESSION:drone" even-vertical

# Pane titles shown in each border, session name in the status bar
tmux select-pane -t "$SESSION:drone.0" -T relay
tmux select-pane -t "$SESSION:drone.1" -T static
tmux select-pane -t "$SESSION:drone.2" -T gesture
tmux set-option -t "$SESSION" -w pane-border-status top
tmux set-option -t "$SESSION" -w pane-border-format ' #{pane_index}: #{pane_title} '
tmux set-option -t "$SESSION" status-left "[#{session_name}] "
tmux set-option -t "$SESSION" status-left-length 40
tmux select-pane -t "$SESSION:drone.1"

log "Session tmux « $SESSION » lancée (relay, static, gesture)"
attach
