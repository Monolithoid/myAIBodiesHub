#!/usr/bin/env bash
# test-chain.sh — Tests each link of the WebUI → NAO chain
#
# Usage (on the VPS, from the repository root):
#   bash tests/test-chain.sh
#
# Requirements: docker, curl available.
# NAO can be off — naoqi-bridge then runs in mock mode.
#
# Tested chain:
#
#   open-webui (nao_pipe.py)
#     │  POST http://chat-relay:8095/chat   [ai-network]
#     ▼
#   chat-relay (Caddy)
#     │  reverse-proxy → host-gateway:8095
#     ▼
#   body-gateway  (host network, port 8095)
#     │  POST http://localhost:11434/v1/chat/completions
#     ▼
#   ollama  (ai-network, port 11434 exposed on the host)
#
#   body-gateway → publishes /bodies/nao_01/actuators/* (ROS2 DDS domain 42)
#     ▼
#   nao-core  (host network, DDS 42 — naoqi-driver + cognitive-renderer + motion-engine)
#     │  POST http://localhost:8090/say|motion|leds|…
#     ▼
#   naoqi-bridge  (ai-network, port 127.0.0.1:8090 exposed on the host)
#     │  qi SDK → tcp://NAO_IP:9559   [MOCK if NAO is off]
#     ▼
#   NAO (off → mock response)
#
#   Link 9 — simulated static drone (tests/fake-static-drone.py):
#   fake rosbridge messages /drones/static_01/* → nao-core/static-driver
#   → whisper-asr → body-gateway → ollama → /drones/static_01/audio_out

set -uo pipefail

# ─── Colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS=0
FAIL=0

_pass() { echo -e "  ${GREEN}✓ PASS${NC}  $1"; PASS=$((PASS + 1)); }
_fail() { echo -e "  ${RED}✗ FAIL${NC}  $1"; FAIL=$((FAIL + 1)); }
_info() { echo -e "        ${CYAN}$1${NC}"; }
_head() { echo -e "\n${YELLOW}┌─ LINK $1 ─ $2${NC}"; }
_skip() { echo -e "  ${BLUE}⊘ SKIP${NC}  $1"; }

# Tests a command; $1 = description, the rest = command
_check() {
    local desc="$1"; shift
    local out rc
    out=$(eval "$@" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        _pass "$desc"
        [ -n "$out" ] && _info "$(echo "$out" | head -3)"
    else
        _fail "$desc"
        [ -n "$out" ] && _info "$(echo "$out" | head -5)"
    fi
}

# ─── Container resolution robust to Coolify suffixes ─────────────────────────
# Coolify renames the containers (project prefix, random suffix).
# The com.docker.compose.service=<name> label, however, is always stable.
# DNS names inside the Docker networks stay the service names
# defined in the compose file — only the container name changes.

_container() {
    # Returns the name of the first running container for this service.
    docker ps \
        --filter "label=com.docker.compose.service=$1" \
        --filter "status=running" \
        --format "{{.Names}}" | head -1
}

_container_running() {
    [ -n "$(_container "$1")" ]
}

echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   test-chain.sh — WebUI → NAO (NAO can be off)              ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# LINK 0 — Running Docker containers
# ─────────────────────────────────────────────────────────────────────────────
_head 0 "Docker containers"

for svc in open-webui chat-relay body-gateway naoqi-bridge nao-core; do
    cname=$(_container "$svc")
    if [ -n "$cname" ]; then
        _pass "service $svc : running  (→ $cname)"
    else
        _fail "service $svc: no running container found"
    fi
done

# Optional (does not block the following tests)
for svc in ollama chromadb n8n whisper-asr ros2-bridge; do
    cname=$(_container "$svc")
    if [ -n "$cname" ]; then
        _pass "service $svc : running  (→ $cname)"
    else
        _skip "service $svc: missing (optional for this test)"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
# LINK 1 — open-webui → chat-relay (ai-network network)
# ─────────────────────────────────────────────────────────────────────────────
_head 1 "open-webui → chat-relay:8095 (ai-network)"

echo -e "  ${BLUE}→${NC} Sends POST /chat from the open-webui container to chat-relay:8095"

# Resolving the open-webui container (name varies with Coolify)
_C_WEBUI=$(_container open-webui)

if [ -z "$_C_WEBUI" ]; then
    _fail "open-webui not found — links 1 and 2 skipped"
else
    # First check that network DNS works
    _check "DNS resolution of chat-relay from open-webui ($_C_WEBUI)" \
        "docker exec $_C_WEBUI getent hosts chat-relay"

    # Check that chat-relay answers (any HTTP response = connection established)
    # 2xx/4xx/5xx are accepted — a refused connection would give "000".
    _check "TCP chat-relay:8095 reachable from open-webui (any HTTP response)" \
        "docker exec $_C_WEBUI sh -c 'curl -s --max-time 5 -o /dev/null -w \"%{http_code}\" http://chat-relay:8095/chat -X POST -H \"Content-Type: application/json\" -d \"{\\\"text\\\":\\\"\\\",\\\"robot_id\\\":\\\"nao_01\\\"}\"' | grep -E '^[2-5][0-9][0-9]$'"
fi

# ─────────────────────────────────────────────────────────────────────────────
# LINK 2 — chat-relay → body-gateway (host-gateway:8095)
# ─────────────────────────────────────────────────────────────────────────────
_head 2 "chat-relay → host gateway :8095 (body-gateway)"

echo -e "  ${BLUE}→${NC} Caddy in chat-relay must reach the host on :8095"

# Without -f: body-gateway returns 400 {"error":"missing text"} for an empty text.
# curl -f would drop the body on 4xx and make the test fail wrongly.
_check "body-gateway /chat answers from the host (port 8095)" \
    "curl -s --max-time 5 -X POST http://localhost:8095/chat \
     -H 'Content-Type: application/json' \
     -d '{\"text\":\"\",\"robot_id\":\"nao_01\"}' | grep -i 'missing\|error\|speech'"

# Shows chat-relay's startup logs for diagnostics
_C_RELAY=$(_container chat-relay)
if [ -n "$_C_RELAY" ]; then
    _info "chat-relay startup logs (→ $_C_RELAY):"
    docker logs --tail=10 "$_C_RELAY" 2>&1 | while IFS= read -r line; do _info "  $line"; done
    _check "chat-relay detected the host gateway" \
        "docker logs --tail=20 $_C_RELAY 2>&1 | grep -E 'host gateway detected|gateway hôte détecté'"
fi

if [ -n "${_C_WEBUI:-}" ]; then
    # Gets the raw response to show useful diagnostics on failure
    _check "chat-relay Caddy proxies to body-gateway (from open-webui)" \
        "resp=\$(docker exec $_C_WEBUI sh -c 'curl -s --max-time 5 -X POST http://chat-relay:8095/chat \
         -H \"Content-Type: application/json\" \
         -d \"{\\\"text\\\":\\\"\\\",\\\"robot_id\\\":\\\"nao_01\\\"}\"') \
         && echo \"\$resp\" | grep -i 'missing\|error\|speech' \
         || { echo \"Unexpected response: \$resp\"; false; }"
fi

# ─────────────────────────────────────────────────────────────────────────────
# LINK 3 — body-gateway → Ollama (localhost:11434)
# ─────────────────────────────────────────────────────────────────────────────
_head 3 "body-gateway → Ollama (localhost:11434)"

echo -e "  ${BLUE}→${NC} body-gateway (mode host) appelle http://localhost:11434"

# Brain model: body-gateway's BRAIN_MODEL, otherwise its Ollama default.
MODEL="${BRAIN_MODEL:-$(docker exec body-gateway printenv BRAIN_MODEL 2>/dev/null)}"
MODEL="${MODEL:-qwen3:1.7b}"

_check "Ollama /api/tags answers" \
    "curl -sf --max-time 10 http://localhost:11434/api/tags | grep -q 'models'"

_check "Ollama lists the ${MODEL} model" \
    "curl -sf --max-time 10 http://localhost:11434/api/tags | grep -qF '\"${MODEL}\"'"

_check "Ollama /v1/chat/completions (endpoint used by body-gateway)" \
    "curl -sf --max-time 60 -X POST http://localhost:11434/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Answer only with the word: ok\"}],\"stream\":false}' \
     | grep -q 'choices'"

# ─────────────────────────────────────────────────────────────────────────────
# LINK 3b — full body-gateway /chat (real inference)
# ─────────────────────────────────────────────────────────────────────────────
_head "3b" "Full inference body-gateway → Ollama → JSON response"

echo -e "  ${BLUE}→${NC} POST /chat with a real text — body-gateway must return speech+actions"

_check "POST /chat {text: 'bonjour'} → JSON with 'speech'" \
    "curl -sf --max-time 60 -X POST http://localhost:8095/chat \
     -H 'Content-Type: application/json' \
     -d '{\"text\":\"bonjour\",\"robot_id\":\"nao_01\"}' \
     | grep -q 'speech'"

# ─────────────────────────────────────────────────────────────────────────────
# LINK 4 — body-gateway → ROS2 bus (DDS domain 42)
# ─────────────────────────────────────────────────────────────────────────────
_head 4 "body-gateway → ROS2 DDS domain 42"

echo -e "  ${BLUE}→${NC} Checks that the ROS2 topics are published"

_C_CORE=$(_container nao-core)
if [ -n "$_C_CORE" ]; then
    _check "ros2 topic list gives /cognitive_state and /bodies/* (→ $_C_CORE)" \
        "docker exec $_C_CORE bash -c \
         'source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 \
          timeout 5 ros2 topic list 2>/dev/null' \
         | grep -E '/cognitive_state|/bodies'"

    _check "/cognitive_state has an active publisher" \
        "docker exec $_C_CORE bash -c \
         'source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 \
          timeout 5 ros2 topic info /cognitive_state 2>/dev/null' \
         | grep -i 'publisher'"

    _check "/motion_engine/command subscribed by motion-engine" \
        "docker exec $_C_CORE bash -c \
         'source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 \
          timeout 5 ros2 topic info /motion_engine/command 2>/dev/null' \
         | grep -i 'subscription'"
else
    _skip "nao-core not available — ROS2 test skipped"
fi

# ─────────────────────────────────────────────────────────────────────────────
# LINK 5 — nao-core → naoqi-bridge (localhost:8090)
# ─────────────────────────────────────────────────────────────────────────────
_head 5 "nao-core → naoqi-bridge (localhost:8090)"

echo -e "  ${BLUE}→${NC} naoqi-bridge is exposed on 127.0.0.1:8090 on the host"

_check "naoqi-bridge /health answers" \
    "curl -sf --max-time 5 http://localhost:8090/health"

_check "naoqi-bridge /health : ok=true" \
    "curl -sf --max-time 5 http://localhost:8090/health | grep -q '\"ok\": *true'"

_check "naoqi-bridge /say answers (must be ok=true in mock or real mode)" \
    "curl -sf --max-time 5 -X POST http://localhost:8090/say \
     -H 'Content-Type: application/json' \
     -d '{\"text\":\"test maillon 5\"}' | grep -q 'true'"

_check "naoqi-bridge /leds answers (mock)" \
    "curl -sf --max-time 5 -X POST http://localhost:8090/leds \
     -H 'Content-Type: application/json' \
     -d '{\"group\":\"AllLeds\",\"r\":0.0,\"g\":1.0,\"b\":0.0,\"duration\":0.5}' \
     | grep -q 'true'"

# ─────────────────────────────────────────────────────────────────────────────
# LINK 6 — naoqi-bridge mock mode (NAO off)
# ─────────────────────────────────────────────────────────────────────────────
_head 6 "naoqi-bridge (NAO off → mock mode expected)"

echo -e "  ${BLUE}→${NC} ok must always be true; connected reflects NAO's real state"

_BRIDGE_CONNECTED=$(curl -sf --max-time 5 http://localhost:8090/health \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('connected',False)).lower())" 2>/dev/null || echo "unknown")

_check "naoqi-bridge ok=true (service operational)" \
    "curl -sf --max-time 5 http://localhost:8090/health \
     | python3 -c \
     \"import json,sys; d=json.load(sys.stdin); assert d.get('ok')==True, d; \
       s='ok={ok}, connected={c}'.format(ok=d['ok'],c=d.get('connected',False)); \
       print(s)\""

_C_BRIDGE=$(_container naoqi-bridge)
if [ -n "$_C_BRIDGE" ]; then
    if [ "$_BRIDGE_CONNECTED" = "true" ]; then
        _info "NAO connected → real mode (no MOCK expected in the logs)"
        _check "naoqi-bridge logs: qi connection established" \
            "docker logs --tail=50 $_C_BRIDGE 2>&1 | grep -iE 'connect|session|nao'"
    else
        _info "NAO not connected → mock mode expected in the logs"
        _check "naoqi-bridge logs contiennent MOCK (→ $_C_BRIDGE)" \
            "docker logs --tail=50 $_C_BRIDGE 2>&1 | grep -i 'mock\|MOCK'"
    fi
else
    _skip "naoqi-bridge not found — log check skipped"
fi

# ─────────────────────────────────────────────────────────────────────────────
# LINK 8 — WebUI pipe → body-gateway (__ping__ without Ollama)
# ─────────────────────────────────────────────────────────────────────────────
_head 8 "Pipe Open WebUI → body-gateway (message __ping__)"

echo -e "  ${BLUE}→${NC} Sends __ping__ through /api/chat/completions — checks that the pipe reaches body-gateway"

# Reading the environment variables (the same ones body-gateway uses)
_WEBUI_URL="${WEBUI_URL:-http://localhost:3000}"
_WEBUI_KEY="${WEBUI_API_KEY:-}"
_PIPE_MODEL="${WEBUI_PIPE_MODEL:-nao_brain}"

if [ -z "$_WEBUI_KEY" ]; then
    _skip "WEBUI_API_KEY not set — pipe test skipped (set the env variable to enable it)"
else
    # Test 1: body-gateway /ping reachable directly from the host
    _check "body-gateway GET /ping answers ok=true (host:8095)" \
        "curl -sf --max-time 5 http://localhost:8095/ping | python3 -c \
         \"import json,sys; d=json.load(sys.stdin); assert d.get('ok') == True, d; print(d)\""

    # Test 2: the pipe answers through the WebUI API with __ping__
    _check "Pipe $_PIPE_MODEL : __ping__ → body-gateway joignable" \
        "resp=\$(curl -sf --max-time 15 -X POST '$_WEBUI_URL/api/chat/completions' \
             -H 'Authorization: Bearer $_WEBUI_KEY' \
             -H 'Content-Type: application/json' \
             -d '{\"model\":\"$_PIPE_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"__ping__\"}],\"stream\":false}') \
         && echo \"\$resp\" | python3 -c \
             \"import json,sys; d=json.load(sys.stdin); \
               content=d['choices'][0]['message']['content']; \
               assert 'body-gateway joignable' in content or 'opérationnel' in content, repr(content); \
               print(content)\""
fi

# ─────────────────────────────────────────────────────────────────────────────
# LINK 9 — Simulated static drone through rosbridge (ADR 004)
# ─────────────────────────────────────────────────────────────────────────────
_head 9 "Simulated static drone → rosbridge → static-driver → body-gateway"

echo -e "  ${BLUE}→${NC} Faux drone ${STATIC_DRONE_ID:-static_01} : status, image, audio (angle -30°), parole"
_info "If a real ${STATIC_DRONE_ID:-static_01} drone is plugged in, it will speak the test sentences."

if _container_running "ros2-bridge" && _container_running "nao-core"; then
    # Direct connection to rosbridge on the host (port 9090, not filtered locally).
    _static_out=$(python3 "$(dirname "$0")/fake-static-drone.py" \
        --url "ws://localhost:9090" \
        --id "${STATIC_DRONE_ID:-static_01}" --nao "${NAO_ROBOT_ID:-nao_01}" 2>&1)
    _static_rc=$?
    echo "$_static_out" | sed 's/^/  /'
    if [ "$_static_rc" -eq 0 ]; then
        _pass "static drone path end to end"
    else
        _fail "static drone path: $_static_rc step(s) failing"
    fi
else
    _skip "ros2-bridge or nao-core missing — static drone not tested"
fi

# ─────────────────────────────────────────────────────────────────────────────
# LINK 7 — n8n reachable (bonus — n8n is not in the direct chain)
# ─────────────────────────────────────────────────────────────────────────────
_head 7 "n8n (bonus — workflow orchestration, outside the direct chain)"

echo -e "  ${BLUE}→${NC} n8n is in ai-network but is not in the direct chat chain"

# n8n has no ports: in the compose file — it is not exposed on the host.
# It is tested from open-webui, which is in the same ai-network.
# n8n has basic auth enabled → 401 is a valid response (service up).
if _container_running "n8n" && [ -n "${_C_WEBUI:-}" ]; then
    _check "n8n answers on ai-network (→ $(_container n8n))" \
        "docker exec $_C_WEBUI curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://n8n:5678/ | grep -E '200|301|302|401'"
elif _container_running "n8n"; then
    _skip "n8n running but open-webui not found to test from ai-network"
else
    _skip "n8n not started"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   SUMMARY                                                    ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

echo -e "  ${GREEN}✓ PASS : $PASS${NC}"
echo -e "  ${RED}✗ FAIL : $FAIL${NC}"

if [ "$FAIL" -eq 0 ]; then
    echo -e "\n  ${GREEN}All links are operational!${NC}"
    echo -e "  You can now switch NAO on and retest link 6."
    exit 0
else
    echo -e "\n  ${RED}$FAIL link(s) failing — see the errors above.${NC}"
    exit 1
fi
