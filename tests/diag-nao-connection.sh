#!/usr/bin/env bash
# diag-nao-connection.sh — Diagnoses the NAO connection chain
#
# Checks each link:
#   NAO → rosbridge → /nao/status → naoqi-driver → POST /connected → naoqi-bridge /health
#
# Usage (on the VPS, from the repository root):
#   bash tests/diag-nao-connection.sh

set -uo pipefail

BRIDGE="http://localhost:8090"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

_ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
_fail() { echo -e "  ${RED}✗${NC}  $1"; }
_info() { echo -e "      ${CYAN}$1${NC}"; }
_head() { echo -e "\n${YELLOW}── $1${NC}"; }

echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   diag-nao-connection.sh — NAO connection diagnostics       ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

# ── 1. Running containers ─────────────────────────────────────────────────────
_head "1. Docker containers"
for svc in naoqi-bridge naoqi-driver ros2-bridge rosbridge-proxy; do
    status=$(docker inspect --format='{{.State.Status}}' "$svc" 2>/dev/null || echo "absent")
    if [ "$status" = "running" ]; then
        _ok "$svc → running"
    else
        _fail "$svc → $status"
    fi
done

# ── 2. naoqi-bridge: /connected endpoint present ───────────────────────────
_head "2. naoqi-bridge — endpoint POST /connected"
code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
       -X POST "${BRIDGE}/connected" \
       -H "Content-Type: application/json" \
       -d '{"connected":false}' 2>/dev/null || echo "000")
if [ "$code" = "200" ]; then
    _ok "POST /connected answers HTTP 200 → code deployed"
else
    _fail "POST /connected → HTTP $code (image not rebuilt?)"
    _info "Update: Redeploy in Coolify (new naoqi-bridge image)"
fi

# ── 3. /nao/status published on the ROS bus ────────────────────────────────────
_head "3. Topic ROS /nao/status (heartbeat NAO → VPS)"
if docker exec naoqi-driver which ros2 >/dev/null 2>&1; then
    msg=$(docker exec naoqi-driver bash -c \
          "source /opt/ros/humble/setup.bash && \
           ROS_DOMAIN_ID=42 ros2 topic echo /nao/status std_msgs/msg/String \
           --once --timeout 10" 2>/dev/null | grep "data:" || true)
    if [ -n "$msg" ]; then
        _ok "/nao/status received: $msg"
    else
        _fail "/nao/status: no message in 10 s"
        _info "The NAO sends no heartbeat — check nao_ws_bridge.py on the robot"
        _info "and that the drone relay is reachable from the NAO (ws://DRONE_IP:9090, ADR 006)"
    fi
else
    _fail "ros2 not found in naoqi-driver"
fi

# ── 4. naoqi-driver: recent logs around the connection ─────────────────────
_head "4. naoqi-driver logs (last 20 lines)"
docker logs --tail 20 naoqi-driver 2>/dev/null | while IFS= read -r line; do
    _info "$line"
done

# ── 5. naoqi-bridge: inject connected=true by hand ──────────────────────────
_head "5. Manual connected=true injection → /health check"
inject=$(curl -s --max-time 5 -X POST "${BRIDGE}/connected" \
         -H "Content-Type: application/json" \
         -d '{"connected":true}' 2>/dev/null || echo "{}")
health=$(curl -s --max-time 5 "${BRIDGE}/health" 2>/dev/null || echo "{}")
conn=$(echo "$health" | python3 -c \
       "import json,sys; print(json.load(sys.stdin).get('connected','?'))" 2>/dev/null || echo "?")
if [ "$conn" = "True" ] || [ "$conn" = "true" ]; then
    _ok "/health connected=true after injection → endpoint operational"
    _info "If naoqi-driver does not call it automatically, check steps 3 and 4"
else
    _fail "/health still connected=$conn after injection"
    _info "inject: $inject"
    _info "health: $health"
fi
# Set back to false so as not to pollute the real state
curl -s --max-time 3 -X POST "${BRIDGE}/connected" \
     -H "Content-Type: application/json" \
     -d '{"connected":false}' >/dev/null 2>&1 || true

# ── 6. naoqi-bridge logs (NAO connection) ────────────────────────────────────
_head "6. naoqi-bridge logs (last 20 lines)"
docker logs --tail 20 naoqi-bridge 2>/dev/null | while IFS= read -r line; do
    _info "$line"
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}── Update if needed${NC}"
echo -e "  ${CYAN}Coolify → IA resource → Redeploy (the images are built by GitHub Actions)${NC}"
