#!/usr/bin/env bash
# test-nao-capabilities.sh — Tests the NAOqi capabilities through naoqi-bridge
#
# Usage:
#   bash tests/test-nao-capabilities.sh [--nao] [<section> ...]
#
# Without a section: runs all sections.
# With sections: runs only the ones given.
#
# Options:
#   --nao     Enables the physical tests (NAO must be on and connected)
#             Default: mock mode only
#   --emotive NAO in emotive-only mode (ADR 004, docs/nao.md): it does not
#             speak, the speech tests are skipped (SKIP). Same as
#             NAO_EMOTIVE=1 in the environment.
#
# Sections:
#   health        /health — connection state
#   battery       /battery — battery level
#   say           /say — speech synthesis (ALTextToSpeech)
#   leds          /leds — LED control (ALLeds.fadeRGB)
#   posture       /posture — predefined postures (ALRobotPosture)
#   motion        /motion — head joints (ALMotion)
#   move          /move + /move/stop — movement (ALMotion.moveToward)
#   events        /events — SSE stream (bumper, sonar, battery)
#   robustness    error cases and invalid endpoints
#   stiffness     /stiffness — head / arm stiffness
#   emotions      emotion rendering (speech + LEDs + head + arms)
#   speaking      NAO's "I'm speaking" animation when the static drone speaks

set -uo pipefail

# ─── Argument parsing ─────────────────────────────────────────────────────────
PHYSICAL=0
EMOTIVE="${NAO_EMOTIVE:-0}"
SECTIONS=()

for arg in "$@"; do
    case "$arg" in
        --nao) PHYSICAL=1 ;;
        --emotive) EMOTIVE=1 ;;
        health|battery|say|leds|posture|motion|move|events|robustness|stiffness|emotions|speaking)
            SECTIONS+=("$arg") ;;
        *)
            echo "Argument inconnu : '$arg'" >&2
            echo "Sections valides : health battery say leds posture motion move events robustness stiffness emotions speaking" >&2
            exit 1
            ;;
    esac
done

# Default: all sections
if [ ${#SECTIONS[@]} -eq 0 ]; then
    SECTIONS=(health battery say leds posture motion move events robustness stiffness emotions speaking)
fi

# ─── Configuration ────────────────────────────────────────────────────────────
BRIDGE="http://localhost:8090"
TIMEOUT=10

# ─── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0

_pass()  { echo -e "  ${GREEN}✓ PASS${NC}  $1"; PASS=$((PASS + 1)); }
_fail()  { echo -e "  ${RED}✗ FAIL${NC}  $1"; FAIL=$((FAIL + 1)); }
_skip()  { echo -e "  ${BLUE}⊘ SKIP${NC}  $1"; SKIP=$((SKIP + 1)); }
_info()  { echo -e "        ${CYAN}$1${NC}"; }
_head()  { echo -e "\n${YELLOW}┌─ $1 ─────────────────────────────────────${NC}"; }
_sub()   { echo -e "  ${MAGENTA}▸${NC} $1"; }
_warn()  { echo -e "  ${YELLOW}⚠${NC}  $1"; }

_post() {
    local desc="$1" body="$2" endpoint="$3" out
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}${endpoint}" \
          -H "Content-Type: application/json" -d "$body" 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "$desc"; _info "$out"
    else
        _fail "$desc"; [ -n "$out" ] && _info "$(echo "$out" | head -5)"
    fi
}

_get() {
    local desc="$1" endpoint="$2" pattern="${3:-}" out
    out=$(curl -s --max-time "$TIMEOUT" "${BRIDGE}${endpoint}" 2>&1)
    if [ -z "$pattern" ] || echo "$out" | grep -qE "$pattern"; then
        _pass "$desc"; _info "$out"
    else
        _fail "$desc (pattern '$pattern' absent)"; _info "$out"
    fi
}

# ─── Sections ─────────────────────────────────────────────────────────────────

_section_health() {
    _head "0 — Requirements / health"

    _get "naoqi-bridge /health answers" "/health" '"ok"'

    if [ "$PHYSICAL" -eq 1 ]; then
        HEALTH=$(curl -sf --max-time 5 "${BRIDGE}/health" 2>/dev/null || echo "{}")
        CONNECTED=$(echo "$HEALTH" | python3 -c \
            "import json,sys; d=json.load(sys.stdin); print('true' if d.get('connected') else 'false')" \
            2>/dev/null || echo "false")
        if [ "$CONNECTED" = "true" ]; then
            _info "NAO connected — physical NAO mode active"
        else
            _fail "connected=false — NAO not connected, cannot run --nao"
            _info "$HEALTH"
            exit 1
        fi
    else
        _info "Mock mode — simulated commands (rerun with --nao when NAO is on)"
    fi
}

_section_battery() {
    _head "1 — Batterie  (GET /battery)"
    _sub "Reading the charge level"
    _get "GET /battery → level returned" "/battery" '"level"'

    if [ "$PHYSICAL" -eq 1 ]; then
        LEVEL=$(curl -sf --max-time 5 "${BRIDGE}/battery" 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('level','?'))" 2>/dev/null)
        _info "Niveau batterie : ${LEVEL}%"
    fi
}

_section_say() {
    _head "2 — Speech synthesis  (POST /say)"

    if [ "$EMOTIVE" = "1" ]; then
        _skip "Emotive-only NAO: speech module disabled, the static drone speaks"
        return
    fi

    _sub "Empty text → error 400"
    out=$(curl -s --max-time 5 -X POST "${BRIDGE}/say" \
          -H "Content-Type: application/json" -d '{"text":""}' 2>&1)
    if echo "$out" | grep -q '"error"'; then
        _pass "/say texte vide → {error} attendu"; _info "$out"
    else
        _fail "/say texte vide devrait retourner {error}"; _info "$out"
    fi

    _sub "Phrase courte"
    _post "/say 'Bonjour'" '{"text":"Bonjour"}' "/say"

    _sub "Sentence with accents (UTF-8)"
    _post "/say UTF-8 — accents" '{"text":"Je suis prêt à t'\''aider"}' "/say"

    _sub "Long sentence (> 50 chars)"
    _post "/say phrase longue" \
        '{"text":"Je suis le robot NAO. Je peux parler, bouger et interagir avec vous."}' "/say"

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Numbers and punctuation"
        _post "/say chiffres + ponctuation" \
            '{"text":"Deux plus deux font quatre. Est-ce correct ?"}' "/say"

        _sub "Question (intonation)"
        _post "/say question" '{"text":"Comment allez-vous ?"}' "/say"
    fi
}

_section_leds() {
    _head "3 — LEDs  (POST /leds)"

    _sub "AllLeds rouge"
    _post "AllLeds → rouge" \
        '{"group":"AllLeds","r":1.0,"g":0.0,"b":0.0,"duration":0.5}' "/leds"

    _sub "FaceLeds vert"
    _post "FaceLeds → vert" \
        '{"group":"FaceLeds","r":0.0,"g":1.0,"b":0.0,"duration":0.5}' "/leds"

    _sub "EarLeds bleu"
    _post "EarLeds → bleu" \
        '{"group":"EarLeds","r":0.0,"g":0.0,"b":1.0,"duration":0.5}' "/leds"

    _sub "ChestLeds blanc"
    _post "ChestLeds → blanc" \
        '{"group":"ChestLeds","r":1.0,"g":1.0,"b":1.0,"duration":0.5}' "/leds"

    _sub "AllLeds extinction (noir)"
    _post "AllLeds → extinction" \
        '{"group":"AllLeds","r":0.0,"g":0.0,"b":0.0,"duration":1.0}' "/leds"

    _sub "Short duration (0.1s flash)"
    _post "AllLeds flash rapide" \
        '{"group":"AllLeds","r":1.0,"g":1.0,"b":0.0,"duration":0.1}' "/leds"

    _sub "Long duration (3s fade)"
    _post "FaceLeds fondu lent 3s" \
        '{"group":"FaceLeds","r":0.5,"g":0.0,"b":0.5,"duration":3.0}' "/leds"

    _sub "RGB values at the limit (1.0, 1.0, 1.0)"
    _post "AllLeds valeurs max" \
        '{"group":"AllLeds","r":1.0,"g":1.0,"b":1.0,"duration":0.5}' "/leds"

    _sub "RGB values at 0 (all off)"
    _post "AllLeds valeurs min" \
        '{"group":"AllLeds","r":0.0,"g":0.0,"b":0.0,"duration":0.5}' "/leds"
}

_section_posture() {
    _head "4 — Postures  (POST /posture)"

    _sub "Posture Stand (debout)"
    _post "posture Stand" '{"posture":"Stand","speed":0.5}' "/posture"

    _sub "Posture Crouch (accroupi)"
    _post "posture Crouch" '{"posture":"Crouch","speed":0.5}' "/posture"

    _sub "Posture StandInit (init standard)"
    _post "posture StandInit" '{"posture":"StandInit","speed":0.5}' "/posture"

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Posture Sit (assis)"
        _post "posture Sit" '{"posture":"Sit","speed":0.3}' "/posture"

        _sub "SitRelax posture (seated relaxed)"
        _post "posture SitRelax" '{"posture":"SitRelax","speed":0.3}' "/posture"

        _sub "LyingBelly posture (lying on the belly)"
        _post "posture LyingBelly (lent)" '{"posture":"LyingBelly","speed":0.2}' "/posture"

        _sub "LyingBack posture (lying on the back)"
        _post "posture LyingBack (lent)" '{"posture":"LyingBack","speed":0.2}' "/posture"

        _sub "Retour Stand vitesse rapide (0.8)"
        _post "posture Stand rapide" '{"posture":"Stand","speed":0.8}' "/posture"

        _sub "Retour Stand vitesse lente (0.1)"
        _post "posture Stand lent" '{"posture":"Stand","speed":0.1}' "/posture"
    else
        _skip "Sit/Lying postures skipped in mock mode (--nao to test)"
    fi
}

_section_motion() {
    _head "5 — Head joints  (POST /motion)"
    # HeadYaw [-2.09, 2.09] rad  |  HeadPitch [-0.67, 0.51] rad

    _sub "HeadYaw → 0 (centre)"
    _post "HeadYaw centre (0 rad)" \
        '{"joint":"HeadYaw","angle":0.0,"speed":0.2}' "/motion"

    _sub "HeadYaw → gauche (+0.5 rad ≈ 29°)"
    _post "HeadYaw gauche (+0.5 rad)" \
        '{"joint":"HeadYaw","angle":0.5,"speed":0.2}' "/motion"

    _sub "HeadYaw → droite (-0.5 rad ≈ -29°)"
    _post "HeadYaw droite (-0.5 rad)" \
        '{"joint":"HeadYaw","angle":-0.5,"speed":0.2}' "/motion"

    _sub "HeadYaw → max gauche (+2.0 rad ≈ 115°)"
    _post "HeadYaw max gauche (+2.0 rad)" \
        '{"joint":"HeadYaw","angle":2.0,"speed":0.15}' "/motion"

    _sub "HeadYaw → max droite (-2.0 rad ≈ -115°)"
    _post "HeadYaw max droite (-2.0 rad)" \
        '{"joint":"HeadYaw","angle":-2.0,"speed":0.15}' "/motion"

    _sub "HeadPitch → 0 (horizontal)"
    _post "HeadPitch horizontal (0 rad)" \
        '{"joint":"HeadPitch","angle":0.0,"speed":0.2}' "/motion"

    _sub "HeadPitch → bas (+0.4 rad ≈ 23°)"
    _post "HeadPitch bas (+0.4 rad)" \
        '{"joint":"HeadPitch","angle":0.4,"speed":0.2}' "/motion"

    _sub "HeadPitch → haut (-0.5 rad ≈ -29°)"
    _post "HeadPitch haut (-0.5 rad)" \
        '{"joint":"HeadPitch","angle":-0.5,"speed":0.2}' "/motion"

    _sub "Vitesse interpolation lente (0.05)"
    _post "HeadYaw vitesse 0.05 (lent)" \
        '{"joint":"HeadYaw","angle":0.0,"speed":0.05}' "/motion"

    _sub "Vitesse interpolation rapide (0.5)"
    _post "HeadYaw vitesse 0.5 (rapide)" \
        '{"joint":"HeadYaw","angle":0.0,"speed":0.5}' "/motion"
}

_section_move() {
    _head "6 — Movement  (POST /move  +  POST /move/stop)"

    _sub "Immediate stop (no prior movement)"
    _post "/move/stop without context" '{}' "/move/stop"

    _sub "Avancer (x=0.3 m/s)"
    _post "/move avancer x=0.3" '{"x":0.3,"y":0.0,"theta":0.0}' "/move"

    _sub "Stop after moving forward"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Reculer (x=-0.3 m/s)"
    _post "/move reculer x=-0.3" '{"x":-0.3,"y":0.0,"theta":0.0}' "/move"

    _sub "Stop"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Strafe gauche (y=0.2 m/s)"
    _post "/move strafe gauche y=0.2" '{"x":0.0,"y":0.2,"theta":0.0}' "/move"

    _sub "Stop"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Strafe droite (y=-0.2 m/s)"
    _post "/move strafe droite y=-0.2" '{"x":0.0,"y":-0.2,"theta":0.0}' "/move"

    _sub "Stop"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Rotation gauche (theta=0.5 rad/s)"
    _post "/move rotation gauche theta=0.5" '{"x":0.0,"y":0.0,"theta":0.5}' "/move"

    _sub "Stop"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Rotation droite (theta=-0.5 rad/s)"
    _post "/move rotation droite theta=-0.5" '{"x":0.0,"y":0.0,"theta":-0.5}' "/move"

    _sub "Final stop"
    _post "/move/stop final" '{}' "/move/stop"

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Combined movement (forward + rotation)"
        _post "/move combined x=0.2 theta=0.3" '{"x":0.2,"y":0.0,"theta":0.3}' "/move"
        sleep 2
        _post "/move/stop after combined movement" '{}' "/move/stop"
    else
        _skip "combined movement skipped in mock mode (--nao to test)"
    fi
}

_section_events() {
    _head "7 — Flux SSE  (GET /events)"

    _sub "SSE connection and keepalive reception"
    out=$(curl -s --max-time 20 -N -H "Accept: text/event-stream" \
          "${BRIDGE}/events" 2>&1 | head -5 || true)
    if echo "$out" | grep -qE "data:|keepalive"; then
        _pass "/events SSE stream active (keepalive or data received)"; _info "$out"
    else
        _fail "/events SSE stream: no data received in 20 s"
        [ -n "$out" ] && _info "$out"
    fi

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Battery available in the stream (30 s poll — waits 35 s)"
        _warn "This test waits up to 35 s (battery polling)"
        out=$(curl -s --max-time 40 -N -H "Accept: text/event-stream" \
              "${BRIDGE}/events" 2>&1 \
              | grep -m1 '"topic":"battery"' || true)
        if [ -n "$out" ]; then
            _pass "/events battery topic received"; _info "$out"
        else
            _fail "/events topic battery absent en 40 s"
        fi
    else
        _skip "SSE battery test skipped (mock mode)"
    fi
}

_section_robustness() {
    _head "8 — Robustness / error cases"

    _sub "/say without a JSON body"
    code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
           -X POST "${BRIDGE}/say" 2>&1)
    if echo "$code" | grep -qE "^[45]"; then
        _pass "/say without body → HTTP $code (expected error)"
    else
        _fail "/say without body should return 4xx or 5xx (got: $code)"
    fi

    _sub "/motion with an invalid joint"
    out=$(curl -s --max-time 10 -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"JointInexistant","angle":0.5,"speed":0.1}' 2>&1)
    if [ "$PHYSICAL" -eq 1 ]; then
        if echo "$out" | grep -q '"error"'; then
            _pass "/motion invalid joint → NAOqi error returned (correct)"
        else
            _fail "/motion invalid joint → NAOqi should have rejected this joint"
        fi
    else
        if echo "$out" | grep -q '"ok".*true'; then
            _pass "/motion invalid joint → mock returns ok:true (normal)"
        else
            _fail "/motion invalid joint → unexpected mock response"
        fi
    fi
    _info "$out"

    _sub "/posture with an invalid name"
    out=$(curl -s --max-time 10 -X POST "${BRIDGE}/posture" \
          -H "Content-Type: application/json" \
          -d '{"posture":"PostureInexistante","speed":0.5}' 2>&1)
    if [ "$PHYSICAL" -eq 1 ]; then
        if echo "$out" | grep -q '"error"'; then
            _pass "/posture invalid → NAOqi error returned (correct)"
        else
            _fail "/posture invalid → NAOqi should have rejected this posture"
        fi
    else
        if echo "$out" | grep -q '"ok".*true'; then
            _pass "/posture invalid → mock returns ok:true (normal)"
        else
            _fail "/posture invalid → unexpected mock response"
        fi
    fi
    _info "$out"

    _sub "Endpoint inexistant → 404"
    code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
           "${BRIDGE}/inexistant" 2>&1)
    if [ "$code" = "404" ]; then
        _pass "GET /inexistant → 404"
    else
        _fail "GET /inexistant devrait retourner 404 (obtenu: $code)"
    fi
}

_section_stiffness() {
    _head "9 — Stiffness (motor stiffness control)"

    _sub "Enable head stiffness only"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
          -H "Content-Type: application/json" \
          -d '{"groups":["head"],"value":1.0}' 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "/stiffness head 1.0 → ok"
    else
        _fail "/stiffness head 1.0 → unexpected response"
    fi
    _info "$out"

    _sub "Activer stiffness bras gauche"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
          -H "Content-Type: application/json" \
          -d '{"groups":["larm"],"value":1.0}' 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "/stiffness larm 1.0 → ok"
    else
        _fail "/stiffness larm 1.0 → unexpected response"
    fi
    _info "$out"

    _sub "Activer stiffness bras droit"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
          -H "Content-Type: application/json" \
          -d '{"groups":["rarm"],"value":1.0}' 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "/stiffness rarm 1.0 → ok"
    else
        _fail "/stiffness rarm 1.0 → unexpected response"
    fi
    _info "$out"

    _sub "Enable head + arms in a single request"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
          -H "Content-Type: application/json" \
          -d '{"groups":["head","larm","rarm"],"value":1.0}' 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "/stiffness head+larm+rarm 1.0 → ok"
    else
        _fail "/stiffness head+larm+rarm 1.0 → unexpected response"
    fi
    _info "$out"

    _sub "Disable head + arms stiffness"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
          -H "Content-Type: application/json" \
          -d '{"groups":["head","larm","rarm"],"value":0.0}' 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "/stiffness head+larm+rarm 0.0 → ok"
    else
        _fail "/stiffness head+larm+rarm 0.0 → unexpected response"
    fi
    _info "$out"

    _sub "Invalid group → warning (no fatal error)"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
          -H "Content-Type: application/json" \
          -d '{"groups":["inconnu"],"value":1.0}' 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "/stiffness unknown group → ok with warning"
    else
        _fail "/stiffness unknown group → unexpected response"
    fi
    _info "$out"

    _sub "Without a JSON body → 4xx/5xx error"
    code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
           -X POST "${BRIDGE}/stiffness" 2>&1)
    if echo "$code" | grep -qE "^[245]"; then
        _pass "/stiffness without body → HTTP $code (handled)"
    else
        _fail "/stiffness without body → unexpected response (HTTP $code)"
    fi
}

# An emotion's speech: skipped when the NAO is in emotive-only mode.
_emotion_say() {
    local emotion="$1" body="$2" out
    if [ "$EMOTIVE" = "1" ]; then
        _skip "$emotion: speech (emotive-only NAO)"
        return
    fi
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/say" \
          -H "Content-Type: application/json" -d "$body" 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "$emotion: speech" || _fail "$emotion: speech"
}

_section_emotions() {
    _head "10 — Emotion rendering (speech + LEDs + head + arms)"

    # Requirement: enable head + arms stiffness
    _sub "Requirement: enable head + arms stiffness"
    curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
         -H "Content-Type: application/json" \
         -d '{"groups":["head","larm","rarm"],"value":1.0}' > /dev/null

    _sub "EXCITED emotion: speech + white LEDs + looking up + arms raised"
    # 1. LEDs blanches vives
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/leds" \
          -H "Content-Type: application/json" \
          -d '{"group":"AllLeds","r":1.0,"g":1.0,"b":1.0,"duration":2.0}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "excited: LEDs blanches" || _fail "excited: LEDs blanches"
    # 2. Head up (HeadPitch = -0.30)
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"HeadPitch","angle":-0.30,"speed":0.15}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "excited: head up" || _fail "excited: head up"
    # 3. Arms raised (ShoulderPitch ~0.4)
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"LShoulderPitch","angle":0.4,"speed":0.12}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "excited: left arm raised" || _fail "excited: left arm raised"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"RShoulderPitch","angle":0.4,"speed":0.12}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "excited: right arm raised" || _fail "excited: right arm raised"
    # 4. Speech
    _emotion_say "excited" '{"text":"Super ! Je suis vraiment excite !"}'

    _sub "CURIOUS emotion: speech + blue LEDs + tilted head + arms at rest"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/leds" \
          -H "Content-Type: application/json" \
          -d '{"group":"AllLeds","r":0.0,"g":0.0,"b":1.0,"duration":2.0}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "curious: LEDs bleues" || _fail "curious: LEDs bleues"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"HeadPitch","angle":-0.20,"speed":0.04}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "curious: head tilted" || _fail "curious: head tilted"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"LShoulderPitch","angle":1.50,"speed":0.06}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "curious: bras gauche repos" || _fail "curious: bras gauche repos"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"RShoulderPitch","angle":1.50,"speed":0.06}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "curious: bras droit repos" || _fail "curious: bras droit repos"
    _emotion_say "curious" '{"text":"Hmm, je me pose des questions..."}'

    _sub "UNCERTAIN emotion: speech + yellow LEDs + slow yaw + shrug"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/leds" \
          -H "Content-Type: application/json" \
          -d '{"group":"AllLeds","r":0.8,"g":0.6,"b":0.0,"duration":2.0}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "uncertain: LEDs jaunes" || _fail "uncertain: LEDs jaunes"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"HeadYaw","angle":0.18,"speed":0.04}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "uncertain: head yaw" || _fail "uncertain: head yaw"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"LElbowRoll","angle":-0.80,"speed":0.07}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "uncertain: bras gauche haussement" || _fail "uncertain: bras gauche haussement"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"RElbowRoll","angle":0.80,"speed":0.07}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "uncertain: bras droit haussement" || _fail "uncertain: bras droit haussement"
    _emotion_say "uncertain" '{"text":"Je ne suis pas sur de comprendre..."}'

    _sub "NEUTRAL emotion: speech + dim LEDs + head straight + arms at rest"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/leds" \
          -H "Content-Type: application/json" \
          -d '{"group":"AllLeds","r":0.05,"g":0.05,"b":0.10,"duration":2.0}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "neutral: LEDs sombres" || _fail "neutral: LEDs sombres"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"HeadPitch","angle":0.0,"speed":0.10}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "neutral: head straight" || _fail "neutral: head straight"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"LShoulderPitch","angle":1.50,"speed":0.06}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "neutral: bras gauche repos" || _fail "neutral: bras gauche repos"
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"RShoulderPitch","angle":1.50,"speed":0.06}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "neutral: bras droit repos" || _fail "neutral: bras droit repos"
    _emotion_say "neutral" '{"text":"Bonjour. Comment puis-je vous aider ?"}'

    # End: put the arms back at rest and disable stiffness
    _sub "Back to rest + arm stiffness disabled"
    curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
         -H "Content-Type: application/json" \
         -d '{"joint":"HeadPitch","angle":0.0,"speed":0.10}' > /dev/null
    curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/motion" \
         -H "Content-Type: application/json" \
         -d '{"joint":"HeadYaw","angle":0.0,"speed":0.10}' > /dev/null
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
          -H "Content-Type: application/json" \
          -d '{"groups":["larm","rarm"],"value":0.0}' 2>&1)
    [[ "$out" =~ '"ok"'.*'true' ]] && _pass "back to rest OK" || _fail "back to rest failed"
}

_section_speaking() {
    _head "11 — \"I'm speaking\" animation (static drone speaking)"

    # Same frames as cognitive_renderer (speaking_animation.py), as
    # single-joint commands for naoqi-bridge's HTTP API.
    local anim_dir
    anim_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../robotics/drones/nao/cognitive_renderer" && pwd)"
    local cmds
    cmds=$(python3 - "$anim_dir" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from speaking_animation import speaking_frame
for tick in range(4):
    leds, motions = speaking_frame(tick)
    print("/leds " + json.dumps(leds))
    for m in motions:
        pairs = zip(m["joints"], m["angles"]) if "joints" in m else [(m["joint"], m["angle"])]
        for joint, angle in pairs:
            print("/motion " + json.dumps({"joint": joint, "angle": angle, "speed": m["speed"]}))
PY
    ) || { _fail "speaking_animation.py illisible"; return; }

    _sub "Requirement: enable head + arms stiffness"
    curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
         -H "Content-Type: application/json" \
         -d '{"groups":["head","larm","rarm"],"value":1.0}' > /dev/null

    _sub "Blinking eyes + nods + left then right forearm (4 frames)"
    local endpoint body
    while read -r endpoint body; do
        _post "speaking ${endpoint} $(echo "$body" | cut -c1-50)" "$body" "$endpoint"
        [ "$endpoint" = "/leds" ] && [ "$PHYSICAL" -eq 1 ] && sleep 1.5
    done <<< "$cmds"

    _sub "Back to rest"
    _post "head straight" '{"joint":"HeadPitch","angle":0.0,"speed":0.08}' "/motion"
    curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}/stiffness" \
         -H "Content-Type: application/json" \
         -d '{"groups":["larm","rarm"],"value":0.0}' > /dev/null
}

# ─── Header ───────────────────────────────────────────────────────────────────
echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   test-nao-capabilities.sh — NAOqi capabilities via bridge  ║${NC}"
if [ "$PHYSICAL" -eq 1 ]; then
echo -e "${YELLOW}║   Mode: physical NAO  (--nao)                                ║${NC}"
else
echo -e "${YELLOW}║   Mode: mock  (rerun with --nao when NAO is on)              ║${NC}"
fi
echo -e "${YELLOW}║   Sections : ${SECTIONS[*]:0:5}...$(printf '%*s' $((36 - ${#SECTIONS[*]} * 1)) '')║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

# ─── Section dispatch ─────────────────────────────────────────────────────────
for section in "${SECTIONS[@]}"; do
    case "$section" in
        health)      _section_health ;;
        battery)     _section_battery ;;
        say)         _section_say ;;
        leds)        _section_leds ;;
        posture)     _section_posture ;;
        motion)      _section_motion ;;
        move)        _section_move ;;
        events)      _section_events ;;
        robustness)  _section_robustness ;;
        stiffness)   _section_stiffness ;;
        emotions)    _section_emotions ;;
        speaking)    _section_speaking ;;
    esac
done

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   SUMMARY                                                    ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

echo -e "  ${GREEN}✓ PASS : $PASS${NC}"
echo -e "  ${RED}✗ FAIL : $FAIL${NC}"
echo -e "  ${BLUE}⊘ SKIP : $SKIP${NC}"

if [ "$PHYSICAL" -eq 0 ] && [ ${#SECTIONS[@]} -ge 5 ]; then
    echo ""
    echo -e "  ${CYAN}Mock mode — rerun with --nao when NAO is on${NC}"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
    echo -e "  ${GREEN}All NAOqi capabilities answer correctly.${NC}"
    exit 0
else
    echo -e "  ${RED}$FAIL capability(ies) failing — see the errors above.${NC}"
    exit 1
fi
