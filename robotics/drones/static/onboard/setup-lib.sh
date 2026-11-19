# shellcheck shell=bash
# Functions shared by setup-ubuntu.sh and nao-relay/setup-ubuntu.sh.
# This file is sourced, not executed.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()    { echo -e "${GREEN}[✓]${NC} $*"; }
info()   { echo -e "${CYAN}[→]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
error()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
section(){ echo -e "\n${CYAN}══ $* ══${NC}"; }

# The script runs as a normal user and calls sudo when needed:
# the venv, the .env and the services belong to the drone user.
require_user_with_sudo() {
    [[ $EUID -ne 0 ]] || error "Run without sudo, as the drone user: bash $0"
    command -v sudo >/dev/null || error "sudo is required"
    sudo -v || error "sudo refused"
}

require_ubuntu() {
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if [[ "${ID:-}" != "ubuntu" ]]; then
            warn "System ${PRETTY_NAME:-unknown}: script meant for Ubuntu 22.04 / 24.04"
        fi
    fi
}

is_interactive() { [[ -t 0 ]]; }

# ask "Question" [default] → answer on stdout (default if no terminal)
ask() {
    local prompt="$1" default="${2:-}" value=""
    if is_interactive; then
        if [[ -n "$default" ]]; then
            read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [${default}] : ")" value
        else
            read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
        fi
    fi
    echo "${value:-$default}"
}

ask_secret() {
    local prompt="$1" value=""
    if is_interactive; then
        read -rsp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
        echo >&2
    fi
    echo "$value"
}

# ── .env file (KEY=value, one per line) ─────────────────────────────────────
# The .env is never sourced: some values (PLAYER_CMD=mpg123 -q -) are not
# valid bash.

# env_get FILE KEY → last uncommented value (empty if missing)
env_get() {
    local file="$1" key="$2"
    [[ -f "$file" ]] || return 0
    awk -v k="$key" '
        index($0, k "=") == 1 { v = substr($0, length(k) + 2); found = 1 }
        END { if (found) print v }
    ' "$file"
}

# env_has FILE KEY → true if an uncommented KEY= line exists
env_has() {
    [[ -f "$1" ]] && grep -q "^$2=" "$1"
}

# env_set FILE KEY VALUE: replaces the KEY= line, otherwise uncomments the
# first #KEY= line, otherwise appends at the end. Leaves the rest alone.
env_set() {
    local file="$1" key="$2" value="$3" tmp
    tmp="$(mktemp)"
    KEY="$key" VALUE="$value" awk '
        BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"] }
        {
            line[NR] = $0
            if (!exact && index($0, k "=") == 1) exact = NR
            if (!comm && index($0, "#" k "=") == 1) comm = NR
        }
        END {
            target = exact ? exact : comm
            for (i = 1; i <= NR; i++) {
                if (i == target) print k "=" v
                else if (index(line[i], k "=") == 1) continue   # doublons
                else print line[i]
            }
            if (!target) print k "=" v
        }
    ' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

# install_if_changed SOURCE DESTINATION [sudo] → true (0) if the file changed
install_if_changed() {
    local src="$1" dst="$2" use_sudo="${3:-}"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        return 1
    fi
    if [[ -n "$use_sudo" ]]; then
        sudo install -m 644 "$src" "$dst"
    else
        install -m 644 "$src" "$dst"
    fi
    return 0
}

# is_stale SERVICE [--user] FILE… → true if the service is not running, or if
# one of the files was changed after it started (setting to apply).
is_stale() {
    local unit="$1"; shift
    local sc=(systemctl)
    if [[ "${1:-}" == "--user" ]]; then sc=(systemctl --user); shift; fi
    "${sc[@]}" is-active -q "$unit" || return 0
    local started f
    started="$(date -d "$("${sc[@]}" show "$unit" -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0)"
    for f in "$@"; do
        [[ -e "$f" && "$(stat -c %Y "$f")" -gt "$started" ]] && return 0
    done
    return 1
}
