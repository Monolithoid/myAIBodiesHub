#!/usr/bin/env python3
"""
twitch_input — Twitch chat as one more input of the brain (ADR 005).

Like "ok nao" heard by the static drone or a message typed in Open WebUI, a
"!nao …" chat message only becomes a text sent to body-gateway /chat.  The
brain answers with a list of actions for the bodies: gestures and LEDs on the
NAO, speech on every connected voice (room speaker, OBS stream voice).  This
service knows nothing about the outputs.

    Twitch chat (IRC, anonymous read)
      │  only "!nao …" messages, filtered (twitch_chat.ChatFilter)
      ▼
    LatestQueue ── latest message only ──► body-gateway POST /chat
    (single slot: when the robot is ready it takes the newest message,
     those received in between are dropped)
      ▲                                       │
      └── robot ready ◄── GET /speaking ◄─────┘  polled until the answer has started
                                                  (NEXT_MESSAGE_AT=end: until it is spoken)
                                               "[tchat Twitch] pseudo : message"

Environment variables:
  TWITCH_CHANNEL        channel to read (required), e.g. "monolithoid"
  BODY_GATEWAY_URL      default http://chat-relay:8095
  ROBOT_ID              body named in /chat, default "nao_01"
  COMMAND_PREFIX        default "!nao"
  MAX_CHARS             longest question kept, default 200
  MAX_AGE_S             older messages are dropped, default 90
  POLL_S                seconds between two GET /speaking, default 0.3
  SPEAKING_WAIT_MAX_S   longest wait for the start (or end) of an answer, default 120
  NEXT_MESSAGE_AT       start (default) = the next message goes to the brain as soon as
                        NAO starts saying the answer, so the next answer is ready when
                        this one ends (the static drone plays it right after, without
                        going back to rest); end = only once the answer has been spoken
  BLOCKLIST             comma-separated forbidden words, default empty
  IGNORED_USERS         comma-separated logins, default "nightbot,streamelements,moobot"
  MODS_ONLY             1 = only moderators and the broadcaster, default 0
  REGIE_URL             control room that sends the "!rgpd" whispers, default http://regie:8097
  REGIE_TOKEN           its page secret (optional; empty = "!rgpd" is only logged)

The Twitch account that whispers is connected in the control room ("Connecter
Twitch" button, once per start): this service holds no Twitch token.  Not
connected = "!rgpd" is off, everything else works.

Viewer memory (body-gateway viewers.py): every "!nao" message carries the
viewer's login, name and question, body-gateway keeps a one-sentence profile
and the last 5 messages of each viewer with NAO's answer; messages dropped by
the single slot are sent to POST /viewers/missed and kept as missed.  Two chat commands, handled at once (never dropped by the queue):
  !rgpd    whispers the viewer what is stored about them (never in public chat),
           one whisper for the profile then one per message
  !reset   erases it
"""

import os
import signal
import socket
import ssl
import sys
import threading
import time
from urllib.parse import quote

import requests

import twitch_chat


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


TWITCH_CHANNEL   = os.environ.get("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
BODY_GATEWAY_URL = os.environ.get("BODY_GATEWAY_URL", "http://chat-relay:8095").rstrip("/")
ROBOT_ID         = os.environ.get("ROBOT_ID", "nao_01")
COMMAND_PREFIX   = os.environ.get("COMMAND_PREFIX", "!nao")
MAX_CHARS        = int(os.environ.get("MAX_CHARS", "200"))
MAX_AGE_S        = float(os.environ.get("MAX_AGE_S", "90"))
POLL_S           = float(os.environ.get("POLL_S", "0.3"))
SPEAKING_WAIT_MAX_S = float(os.environ.get("SPEAKING_WAIT_MAX_S", "120"))
NEXT_MESSAGE_AT  = "end" if os.environ.get("NEXT_MESSAGE_AT", "start").strip().lower() == "end" else "start"
BLOCKLIST        = _env_list("BLOCKLIST")
IGNORED_USERS    = _env_list("IGNORED_USERS", "nightbot,streamelements,moobot")
MODS_ONLY        = os.environ.get("MODS_ONLY", "0") == "1"
REGIE_URL        = os.environ.get("REGIE_URL", "http://regie:8097").rstrip("/")
REGIE_TOKEN      = os.environ.get("REGIE_TOKEN", "").strip()

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697


def log(text: str):
    print(f"[twitch-input] {text}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Twitch IRC reader (anonymous, read-only)
# ─────────────────────────────────────────────────────────────────────────────

def irc_loop(chat_filter: twitch_chat.ChatFilter, inbox: twitch_chat.LatestQueue):
    backoff = 2
    while True:
        try:
            raw = socket.create_connection((TWITCH_IRC_HOST, TWITCH_IRC_PORT), timeout=360)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=TWITCH_IRC_HOST)
            send = lambda s: sock.sendall((s + "\r\n").encode())  # noqa: E731
            send("CAP REQ :twitch.tv/tags")
            # "justinfan…" is Twitch's anonymous read-only login: no token needed.
            send(f"NICK justinfan{int(time.time()) % 100000}")
            send(f"JOIN #{TWITCH_CHANNEL}")
            log(f"connecté au tchat #{TWITCH_CHANNEL}")
            backoff = 2
            for line in sock.makefile("r", encoding="utf-8", errors="replace"):
                if line.startswith("PING"):
                    send("PONG" + line[4:].rstrip("\r\n"))
                    continue
                if " RECONNECT" in line[:80]:
                    log("Twitch demande une reconnexion")
                    break
                msg = twitch_chat.parse_privmsg(line)
                if msg is None:
                    continue
                command = twitch_chat.viewer_command(msg.text)
                if command and msg.user not in chat_filter.ignored_users:
                    threading.Thread(target=viewer_command, args=(command, msg), daemon=True).start()
                    continue
                question, reason = chat_filter.check(msg)
                if question is None:
                    if reason != "pas une commande":
                        log(f"ignoré ({reason}) : {msg.user}")
                    continue
                replaced = inbox.push(msg, question)
                log(f"reçu de {msg.display_name} : {question[:80]!r}")
                if replaced is not None:
                    log(f"abandonné (plus récent reçu) : {replaced[0].display_name}")
            sock.close()
        except OSError as e:
            log(f"tchat déconnecté : {e} — nouvel essai dans {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ─────────────────────────────────────────────────────────────────────────────
# Worker: as soon as the robot is ready, the latest message → brain
# ─────────────────────────────────────────────────────────────────────────────

def worker_loop(inbox: twitch_chat.LatestQueue):
    while True:
        item = inbox.pop()
        if item is None:
            continue
        msg, question = item
        try:
            # Same entry point as Open WebUI: the brain dispatches the actions itself.
            resp = requests.post(
                f"{BODY_GATEWAY_URL}/chat",
                json={
                    "text": twitch_chat.brain_prompt(msg, question),
                    "robot_id": ROBOT_ID,
                    # body-gateway keeps a short profile per viewer (viewers.py).
                    "viewer": {"login": msg.user, "name": msg.display_name, "message": question},
                },
                timeout=180,
            )
            resp.raise_for_status()
            result = resp.json()
        except (requests.RequestException, ValueError) as e:
            log(f"body-gateway en erreur : {e}")
        else:
            if result.get("error") == "busy":
                log("cerveau occupé — message abandonné")
            else:
                log(f"réponse à {msg.display_name} : {(result.get('speech') or '')[:80]!r}")
        # The robot is ready once the answer has started (or been spoken), not after a fixed pause.
        wait_for_answer(NEXT_MESSAGE_AT)


# ─────────────────────────────────────────────────────────────────────────────
# Viewer memory: !rgpd (whisper what is stored) and !reset (erase it)
# ─────────────────────────────────────────────────────────────────────────────

def viewer_command(command: str, msg: twitch_chat.ChatMessage):
    try:
        if command == "reset":
            resp = requests.post(f"{BODY_GATEWAY_URL}/viewers/reset",
                                 json={"login": msg.user}, timeout=10)
            resp.raise_for_status()
            log(f"!reset from {msg.display_name}: memory erased ({resp.json().get('deleted')})")
        elif command == "rgpd":
            resp = requests.get(f"{BODY_GATEWAY_URL}/viewers/{quote(msg.user)}", timeout=10)
            resp.raise_for_status()
            for i, text in enumerate(twitch_chat.rgpd_texts(resp.json())):
                if i:
                    time.sleep(0.5)   # Twitch: at most 3 whispers per second
                if not send_whisper(msg, text):
                    break
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        log(f"!{command} from {msg.display_name} failed: {e}")


def report_missed(msg: twitch_chat.ChatMessage, question: str):
    """A dropped message stays in the viewer's memory as missed (no answer)."""
    threading.Thread(target=_post_missed, args=(msg, question), daemon=True).start()


def _post_missed(msg: twitch_chat.ChatMessage, question: str):
    try:
        requests.post(f"{BODY_GATEWAY_URL}/viewers/missed", timeout=10, json={
            "login": msg.user, "name": msg.display_name, "message": question,
        }).raise_for_status()
    except requests.RequestException as e:
        log(f"missed message from {msg.display_name} not stored: {e}")


def send_whisper(msg: twitch_chat.ChatMessage, text: str) -> bool:
    """Private message, sent by the control room with the Twitch account
    connected there: the profile never goes to public chat."""
    if not REGIE_TOKEN:
        log(f"!rgpd from {msg.display_name}: REGIE_TOKEN empty, nothing sent")
        return False
    if not msg.user_id:
        log(f"!rgpd from {msg.display_name}: unknown Twitch user id, nothing sent")
        return False
    resp = requests.post(
        f"{REGIE_URL}/twitch/whisper", params={"token": REGIE_TOKEN},
        json={"to_user_id": msg.user_id, "message": text}, timeout=15,
    )
    if resp.status_code == 503:
        log(f"!rgpd from {msg.display_name}: Twitch not connected in the control room, nothing sent")
        return False
    if resp.status_code != 200:
        log(f"!rgpd: whisper to {msg.display_name} refused ({resp.status_code}) {resp.text[:200]}")
        return False
    log(f"!rgpd: whisper sent to {msg.display_name}")
    return True


def wait_for_answer(at: str = "start"):
    """Poll body-gateway GET /speaking until the last answer has started, or been spoken."""
    deadline = time.monotonic() + SPEAKING_WAIT_MAX_S
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{BODY_GATEWAY_URL}/speaking", timeout=5)
            resp.raise_for_status()
            if twitch_chat.answer_ready(resp.json(), at):
                return
        except (requests.RequestException, ValueError) as e:
            log(f"état « parle » inconnu ({e}) — message suivant sans attendre")
            return
        time.sleep(POLL_S)
    log(f"réponse ni commencée ni finie après {SPEAKING_WAIT_MAX_S:.0f}s — message suivant"
        if at == "start" else
        f"toujours en train de parler après {SPEAKING_WAIT_MAX_S:.0f}s — message suivant")


def main():
    # PID 1 in the container: without a handler, the kernel drops SIGTERM and
    # `docker stop` waits for its timeout before sending SIGKILL.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not TWITCH_CHANNEL:
        log("ERREUR : TWITCH_CHANNEL est obligatoire")
        sys.exit(1)
    chat_filter = twitch_chat.ChatFilter(
        prefix=COMMAND_PREFIX, max_chars=MAX_CHARS,
        blocklist=BLOCKLIST, ignored_users=IGNORED_USERS, mods_only=MODS_ONLY,
    )
    inbox = twitch_chat.LatestQueue(max_age_s=MAX_AGE_S, on_drop=report_missed)
    threading.Thread(target=irc_loop, args=(chat_filter, inbox), daemon=True).start()
    log(f"tchat #{TWITCH_CHANNEL} → {BODY_GATEWAY_URL}/chat")
    worker_loop(inbox)


if __name__ == "__main__":
    main()
