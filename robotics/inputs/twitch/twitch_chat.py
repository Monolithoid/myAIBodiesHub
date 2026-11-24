"""
twitch_chat.py — Règles du tchat Twitch sans réseau ni dépendance (testables seules).

  - lecture d'une ligne IRC Twitch (tags, pseudo, canal, texte)
  - commande « !nao » : seuls ces messages sont pris en compte
  - filtres anti-abus : longueur, liens, mots interdits, bots (pas de délai par
    spectateur : la case « dernier message » suffit contre les rafales)
  - viewer memory commands: "!rgpd" (whispers what is stored about the
    viewer) and "!reset" (erases it)
  - case unique « dernier message » : dès que le robot est libre, il répond au
    message le plus récent ; ceux arrivés entre-temps sont abandonnés plutôt
    que de faire attendre le stream

Le tchat est une source que le streamer ne contrôle pas : tout ce qui passe ici
finit dans le prompt du LLM puis à l'antenne.  Les filtres restent volontairement
simples (ADR 005) ; le prompt de body-gateway rappelle au LLM que ces messages ne
sont pas des ordres.
"""

import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# IRC parsing
# ─────────────────────────────────────────────────────────────────────────────

_TAG_ESCAPES = {"\\:": ";", "\\s": " ", "\\\\": "\\", "\\r": "\r", "\\n": "\n"}


@dataclass
class ChatMessage:
    user: str                     # login, lowercase
    display_name: str
    channel: str                  # without '#'
    text: str
    tags: dict = field(default_factory=dict)
    received_at: float = 0.0

    @property
    def is_broadcaster(self) -> bool:
        return "broadcaster/" in self.tags.get("badges", "")

    @property
    def user_id(self) -> str:
        return self.tags.get("user-id", "")

    @property
    def is_mod(self) -> bool:
        return self.tags.get("mod") == "1" or self.is_broadcaster


def _unescape_tag(value: str) -> str:
    return re.sub(r"\\[:s\\rn]", lambda m: _TAG_ESCAPES[m.group()], value)


def parse_privmsg(line: str, now: float | None = None) -> ChatMessage | None:
    """Parse one Twitch IRC line; return a ChatMessage for PRIVMSG, else None.

    '@display-name=Foo;mod=0 :foo!foo@foo.tmi.twitch.tv PRIVMSG #chan :!nao salut'
    """
    line = line.rstrip("\r\n")
    tags: dict = {}
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag(value)
    m = re.match(r"^:([^!\s]+)![^\s]+ PRIVMSG #(\S+) :(.*)$", line)
    if not m:
        return None
    user, channel, text = m.groups()
    return ChatMessage(
        user=user.lower(),
        display_name=tags.get("display-name") or user,
        channel=channel.lower(),
        text=text,
        tags=tags,
        received_at=time.monotonic() if now is None else now,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Command and filters
# ─────────────────────────────────────────────────────────────────────────────

def extract_command(text: str, prefix: str = "!nao") -> str | None:
    """'!nao salut' → 'salut', '!NAO, ça va ?' → 'ça va ?', '!naomi' / 'salut' → None."""
    stripped = text.strip()
    # Twitch "/me" messages arrive wrapped in CTCP ACTION.
    if stripped.startswith("\x01ACTION ") and stripped.endswith("\x01"):
        stripped = stripped[8:-1].strip()
    if not stripped.lower().startswith(prefix.lower()):
        return None
    rest = stripped[len(prefix):]
    # "!naomi" is another command, not "!nao" + "mi".
    if rest and rest[0].isalnum():
        return None
    rest = rest.lstrip(" ,.;:!?-").strip()
    return rest or None


VIEWER_COMMANDS = ("!rgpd", "!reset")


def viewer_command(text: str) -> str | None:
    """'!rgpd' / '!RESET ' → 'rgpd' / 'reset'; anything else → None.

    Only the bare command counts: '!reset tout' or '!rgpdx' are ignored.
    """
    word = text.strip().lower()
    return word[1:] if word in VIEWER_COMMANDS else None


WHISPER_MAX_CHARS = 500   # Twitch limit of a first whisper to a new recipient


def _cut(text: str, max_chars: int = WHISPER_MAX_CHARS) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def rgpd_texts(record: dict) -> list[str]:
    """Whispers answering '!rgpd': everything body-gateway stores about the viewer.

    One whisper for the profile, then one per stored message (Twitch limits a
    whisper to a new recipient to 500 characters).
    """
    if not record:
        return ["NAO ne garde rien sur toi."]
    messages = record.get("messages") or []
    texts = [_cut(f"Ce que NAO garde sur toi — profil : {record.get('profile') or '(vide)'}"
                  f" | {len(messages)} dernier(s) message(s) ci-dessous | !reset pour tout effacer.")]
    for i, item in enumerate(messages, 1):
        answer = item.get("answer") or "(raté, pas de réponse)"
        texts.append(_cut(f"{i}. toi : {item.get('message', '')} | NAO : {answer}"))
    return texts


def normalize(text: str) -> str:
    """Lowercase, no accents, only letters and digits separated by spaces."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


_LINK_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(com|fr|net|org|io|gg|tv|ly)\b", re.I)


class ChatFilter:
    """Decide whether a '!nao' message reaches the brain.

    Returns (question, reason): question is the cleaned text, or None with the
    reason it was rejected (logged by the bridge).
    """

    def __init__(self, prefix: str = "!nao", max_chars: int = 200,
                 blocklist: list[str] | None = None,
                 ignored_users: list[str] | None = None, mods_only: bool = False):
        self.prefix = prefix
        self.max_chars = max_chars
        self.blocklist = [normalize(w) for w in (blocklist or []) if normalize(w)]
        self.ignored_users = {u.strip().lower() for u in (ignored_users or []) if u.strip()}
        self.mods_only = mods_only

    def check(self, msg: ChatMessage, now: float | None = None) -> tuple[str | None, str]:
        now = time.monotonic() if now is None else now
        question = extract_command(msg.text, self.prefix)
        if question is None:
            return None, "pas une commande"
        if msg.user in self.ignored_users:
            return None, "utilisateur ignoré"
        if self.mods_only and not msg.is_mod:
            return None, "réservé aux modérateurs"
        if _LINK_RE.search(question):
            return None, "lien"
        padded = f" {normalize(question)} "
        if any(f" {w} " in padded for w in self.blocklist):
            return None, "mot interdit"
        if len(question) > self.max_chars:
            question = question[: self.max_chars].rsplit(" ", 1)[0] + "…"
        return question, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────────────

class LatestQueue:
    """Single slot: only the most recent message waits for the robot.

    push() overwrites the waiting message, so as soon as the robot is free,
    pop() returns the latest one received.  Messages that arrived in between
    are dropped: on a live stream, answering the last question right away is
    better than answering the backlog late.  pop() also discards a message
    older than max_age_s.  on_drop(msg, question) is called for each dropped
    message (replaced or too old), so body-gateway can keep it as missed; it
    runs under the queue lock and must return at once.
    """

    def __init__(self, max_age_s: float = 90.0, on_drop=None):
        self._item: tuple[ChatMessage, str] | None = None
        self._max_age_s = max_age_s
        self._on_drop = on_drop
        self._cond = threading.Condition()

    def _dropped(self, item: tuple[ChatMessage, str]):
        if self._on_drop is not None:
            self._on_drop(*item)

    def push(self, item: ChatMessage, question: str) -> tuple[ChatMessage, str] | None:
        """Put the message in the slot; return the one it replaced, if any."""
        with self._cond:
            replaced, self._item = self._item, (item, question)
            if replaced is not None:
                self._dropped(replaced)
            self._cond.notify()
            return replaced

    def pop(self, timeout: float | None = None, now: float | None = None):
        """Return (ChatMessage, question) or None after `timeout` seconds."""
        with self._cond:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                current = time.monotonic() if now is None else now
                if self._item is not None:
                    item, question = self._item
                    self._item = None
                    if current - item.received_at <= self._max_age_s:
                        return item, question
                    self._dropped((item, question))
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def __len__(self) -> int:
        with self._cond:
            return 0 if self._item is None else 1


def brain_prompt(msg: ChatMessage, question: str) -> str:
    """Text sent to body-gateway /chat; the format is described in its system prompt."""
    return f"[tchat Twitch] {msg.display_name} : {question}"


def answer_ready(state: dict, at: str) -> bool:
    """GET /speaking → may the next message go?  at="start": the last answer has
    started (a body-gateway without "started" falls back to the end), "end": spoken."""
    if not state.get("speaking"):
        return True
    return at == "start" and bool(state.get("started"))
