#!/usr/bin/env bash
# run.sh — Runs the NAO relay in the foreground, without a systemd service
# (Ctrl+C to stop it). Reads its configuration from the drone's .env, as
# nao-relay.service would.
#
# Usage : bash robotics/drones/static/onboard/nao-relay/run.sh

set -euo pipefail

RELAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONBOARD_DIR="$(dirname "$RELAY_DIR")"
# shellcheck source=SCRIPTDIR/../setup-lib.sh
. "${ONBOARD_DIR}/setup-lib.sh"

ENV_FILE="${ONBOARD_DIR}/.env"
[[ -f "$ENV_FILE" ]] || error "$ENV_FILE absent : lancer d'abord nao-relay/setup-ubuntu.sh"
command -v caddy >/dev/null || error "Caddy absent : lancer d'abord nao-relay/setup-ubuntu.sh"

# Only the .env values count (an empty NAO_IP= line refuses everything)
for key in NAO_RELAY_UPSTREAM ROSBRIDGE_TOKEN NAO_IP NAO_RELAY_PORT; do
    unset "$key"
    if env_has "$ENV_FILE" "$key"; then
        export "$key=$(env_get "$ENV_FILE" "$key")"
    fi
done

exec caddy run --adapter caddyfile --config "${RELAY_DIR}/Caddyfile"
