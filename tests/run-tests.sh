#!/usr/bin/env bash
# run-tests.sh — Test launcher for the WebUI → NAO chain
#
# Usage:
#   bash tests/run-tests.sh                          # lists the available suites
#   bash tests/run-tests.sh <suite> [<suite> ...]    # runs the given suites
#   bash tests/run-tests.sh all                      # runs all suites (without --nao)
#
# Examples:
#   bash tests/run-tests.sh chain
#   bash tests/run-tests.sh nao:say nao:leds
#   bash tests/run-tests.sh nao:move --nao
#   bash tests/run-tests.sh nao:posture nao:motion nao:move --nao
#   bash tests/run-tests.sh all

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ─── Help / suite list ──────────────────────────────────────────────────────
_usage() {
    echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║   run-tests.sh — Test suite launcher                        ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${CYAN}Usage:${NC}"
    echo -e "    bash tests/run-tests.sh <suite> [<suite> ...] [--nao]"
    echo -e "    bash tests/run-tests.sh all"
    echo ""
    echo -e "  ${CYAN}Options:${NC}"
    echo -e "    ${GREEN}--nao${NC}   Enables the physical NAO tests for the nao:* suites"
    echo ""
    echo -e "  ${CYAN}Suites — infrastructure:${NC}"
    echo ""
    echo -e "    ${GREEN}chain${NC}"
    echo -e "        Links WebUI → Ollama → naoqi-bridge (NAO can be off)"
    echo -e "        Includes link 8: __ping__ pipe through the Open WebUI API"
    echo -e "        (needs WEBUI_API_KEY set in the environment)"
    echo ""
    echo -e "    ${GREEN}diag${NC}"
    echo -e "        NAO connection diagnostics (network, qi SDK, port 9559)"
    echo ""
    echo -e "  ${CYAN}Suites — NAOqi capabilities (mock by default, --nao for physical):${NC}"
    echo ""
    echo -e "    ${GREEN}nao:health${NC}       /health — bridge connection state"
    echo -e "    ${GREEN}nao:battery${NC}      /battery — battery level"
    echo -e "    ${GREEN}nao:say${NC}          /say — speech synthesis (ALTextToSpeech)"
    echo -e "    ${GREEN}nao:leds${NC}         /leds — LED control (ALLeds.fadeRGB)"
    echo -e "    ${GREEN}nao:posture${NC}      /posture — postures (Stand, Crouch, Sit…)"
    echo -e "    ${GREEN}nao:motion${NC}       /motion — head joints (HeadYaw, HeadPitch)"
    echo -e "    ${GREEN}nao:move${NC}         /move + /move/stop — locomotion"
    echo -e "    ${GREEN}nao:events${NC}       /events — SSE stream (bumper, sonar, battery)"
    echo -e "    ${GREEN}nao:robustness${NC}   error cases and invalid endpoints"
    echo -e "    ${GREEN}nao:speaking${NC}     \"I'm speaking\" animation (eyes, head, arms) of the emotive NAO"
    echo -e "    ${GREEN}nao:all${NC}          all the nao:* sections above"
    echo ""
    echo -e "  ${CYAN}Shortcuts:${NC}"
    echo ""
    echo -e "    ${GREEN}all${NC}   chain + nao:all + diag  (without --nao)"
    echo ""
    echo -e "  ${CYAN}Examples:${NC}"
    echo -e "    bash tests/run-tests.sh chain"
    echo -e "    bash tests/run-tests.sh nao:say nao:leds"
    echo -e "    bash tests/run-tests.sh nao:move --nao"
    echo -e "    bash tests/run-tests.sh nao:posture nao:motion nao:move --nao"
    echo -e "    bash tests/run-tests.sh all"
    echo ""
}

# ─── Suite handling ────────────────────────────────────────────────────────────
SUITES_RUN=()
SUITES_FAILED=()
PHYSICAL=0

# Sections NAO individuelles disponibles
NAO_SECTIONS=(health battery say leds posture motion move events robustness)

_run_suite() {
    local suite="$1"
    local script args label nao_section

    case "$suite" in
        chain)
            script="$SCRIPT_DIR/test-chain.sh"
            args=()
            label="chain — WebUI → NAO links"
            ;;
        diag)
            script="$SCRIPT_DIR/diag-nao-connection.sh"
            args=()
            label="diag — NAO connection diagnostics"
            ;;
        nao:all)
            # Expanded upstream — should not get here
            for s in "${NAO_SECTIONS[@]}"; do _run_suite "nao:$s"; done
            return
            ;;
        nao:health|nao:battery|nao:say|nao:leds|nao:posture|nao:motion|nao:move|nao:events|nao:robustness|nao:speaking)
            nao_section="${suite#nao:}"
            script="$SCRIPT_DIR/test-nao-capabilities.sh"
            args=("$nao_section")
            [ "$PHYSICAL" -eq 1 ] && args=(--nao "$nao_section")
            label="$suite$([ "$PHYSICAL" -eq 1 ] && echo ' (--nao)' || echo ' (mock)')"
            ;;
        *)
            echo -e "${RED}Unknown suite: '$suite'${NC}" >&2
            echo -e "Run ${CYAN}bash tests/run-tests.sh${NC} without arguments to see the available suites." >&2
            SUITES_RUN+=("${RED}✗${NC} $suite (inconnue)")
            SUITES_FAILED+=("$suite")
            return 1
            ;;
    esac

    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  ▶  $label${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    if bash "$script" "${args[@]}"; then
        SUITES_RUN+=("${GREEN}✓${NC} $suite")
    else
        SUITES_RUN+=("${RED}✗${NC} $suite")
        SUITES_FAILED+=("$suite")
    fi
}

# ─── Entry point ──────────────────────────────────────────────────────────────
if [ $# -eq 0 ]; then
    _usage
    exit 0
fi

# Parsing: separates --nao from the suite names
RAW_SUITES=()
for arg in "$@"; do
    case "$arg" in
        --nao) PHYSICAL=1 ;;
        *)     RAW_SUITES+=("$arg") ;;
    esac
done

# Expansion of "all" and "nao:all"
EXPANDED=()
for arg in "${RAW_SUITES[@]}"; do
    case "$arg" in
        all)
            EXPANDED+=(chain)
            for s in "${NAO_SECTIONS[@]}"; do EXPANDED+=("nao:$s"); done
            EXPANDED+=(diag)
            ;;
        nao:all)
            for s in "${NAO_SECTIONS[@]}"; do EXPANDED+=("nao:$s"); done
            ;;
        *)
            EXPANDED+=("$arg")
            ;;
    esac
done

# Deduplication (order kept)
SEEN=()
DEDUPED=()
for s in "${EXPANDED[@]}"; do
    already=0
    for seen in "${SEEN[@]:-}"; do [ "$seen" = "$s" ] && already=1 && break; done
    if [ "$already" -eq 0 ]; then
        DEDUPED+=("$s"); SEEN+=("$s")
    fi
done

# Lancement
for suite in "${DEDUPED[@]}"; do
    _run_suite "$suite"
done

# ─── Overall summary──────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   OVERALL SUMMARY                                            ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
for line in "${SUITES_RUN[@]}"; do
    echo -e "    $line"
done
echo ""

if [ ${#SUITES_FAILED[@]} -eq 0 ]; then
    echo -e "  ${GREEN}All suites passed.${NC}"
    exit 0
else
    echo -e "  ${RED}${#SUITES_FAILED[@]} suite(s) failing: ${SUITES_FAILED[*]}${NC}"
    exit 1
fi
