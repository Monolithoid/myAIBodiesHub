"""
hangman — the hangman game (ADR 010).  The code decides, the LLM speaks.

A French common noun is drawn from words/hangman_fr.json (Lexique 3.83, CC BY-SA 4.0).
Right after the draw, a hidden LLM call writes a short definition; the code
checks it does not give the word away, otherwise the game goes on without it.
The code reads the definition word for word and the overlay shows it, but NAO's
LLM never gets it (it would guess the word): only the masked word, the letters
tried and the errors.

Rules:
  - letters compared without accents (é = e), first come first served;
  - a viewer may propose the whole word at any time: a wrong word costs an error,
    and that viewer may not propose a word again in this game;
  - won when every letter is found or the word is proposed; lost after
    HANGMAN_MAX_ERRORS errors (7 by default);
  - points: 1 per letter revealed, 3 + letters still hidden for the word;
  - NAO plays too, with its own points: letters when a viewer asks it, and the
    word when it says it in a sentence (move "said", checked by the code), at
    any time; HANGMAN_NAO_MIN_FOUND (e.g. 0.5) makes it wait until that share of
    the word is found, its sentence being replaced by a teasing one before.
"""

import json
import os
import random
import re
import unicodedata
import uuid

from game_core import Event, Game, Player

WORDS_PATH = os.environ.get("HANGMAN_WORDS",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "words", "hangman_fr.json"))
MAX_ERRORS = int(os.environ.get("HANGMAN_MAX_ERRORS", "7"))
MIN_LETTERS = int(os.environ.get("HANGMAN_MIN_LETTERS", "6"))
# NAO's own guess counts once this share of the word is found; 0 (default) = at
# any time, NAO's sentence is never changed.
NAO_MIN_FOUND = float(os.environ.get("HANGMAN_NAO_MIN_FOUND", "0"))
DEFINITION_MAX_CHARS = 200

LEVELS = {"facile": "easy", "moyen": "medium", "difficile": "hard",
          "easy": "easy", "medium": "medium", "hard": "hard"}
LEVEL_NAMES = {"easy": "facile", "medium": "moyen", "hard": "difficile"}
WORD_BONUS = 3

_words_cache: dict | None = None


def plain(text: str) -> str:
    """Lowercase, no accents, œ → oe: what the viewers type and guess."""
    text = text.lower().replace("œ", "oe").replace("æ", "ae")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def load_words(path: str | None = None) -> dict:
    """{"easy": [...], "medium": [...], "hard": [...]}, words of at least MIN_LETTERS letters."""
    global _words_cache
    if path is None and _words_cache is not None:
        return _words_cache
    with open(path or WORDS_PATH, encoding="utf-8") as f:
        data = json.load(f)["levels"]
    words = {level: [w for w in data.get(level, []) if len(plain(w)) >= MIN_LETTERS]
             for level in ("easy", "medium", "hard")}
    if path is None:
        _words_cache = words
    return words


def leaks(definition: str, word: str) -> bool:
    """True when the definition gives the word away (the word, its stem, its letters)."""
    text = plain(definition)
    target = plain(word)
    stem = target[:max(4, len(target) - 3)]
    tokens = re.findall(r"[a-z]+", text)
    if any(t.startswith(stem) for t in tokens):
        return True
    # Spelled out ("a-r-a-i-g…") or with spaces.
    return target in re.sub(r"[^a-z]", "", text)


class Hangman(Game):
    id = "hangman"
    name = "Pendu"
    how_to_play = ('une lettre : {"type": "letter", "letter": "e"} ; '
                   'le mot entier, à tout moment de la partie (un message fait d\'un seul mot '
                   'est une proposition de mot entier) : {"type": "word", "word": "<mot proposé>"}')
    start_help = '"options": {"difficulty": "facile|moyen|difficile"} (moyen si rien n\'est demandé)'

    def __init__(self, words: dict | None = None, rng: random.Random | None = None,
                 max_errors: int = MAX_ERRORS, nao_min_found: float = NAO_MIN_FOUND):
        self.nao_min_found = nao_min_found
        self._words = words
        self._rng = rng or random.Random()
        self.max_errors = max_errors
        self.min_letters = 0                # extra filter set from the control room (configure)
        self.word = ""
        self.level = "medium"
        self.tried: list[str] = []          # letters, in the order they were played
        self.errors = 0
        self.no_word: set[str] = set()      # logins who proposed a wrong word
        self.definition = ""
        self._ask: dict | None = None
        self._over = False
        self._started_by = ""

    # ── Public state ────────────────────────────────────────────────────────

    @property
    def target(self) -> str:
        return plain(self.word)

    def mask(self) -> str:
        """'R _ _ _ _ T _ _ _ X': found letters in capitals, the others hidden.

        Once the game is over (won or lost), the whole word shows on the overlay."""
        return " ".join(c.upper() if self._over or c in self.tried else "_" for c in self.target)

    def hidden_count(self) -> int:
        return sum(1 for c in self.target if c not in self.tried)

    def wrong_letters(self) -> list[str]:
        return [c for c in self.tried if c not in self.target]

    def public_view(self) -> dict:
        return {
            "mask": self.mask(), "letters": len(self.target),
            "tried": [c.upper() for c in self.tried],
            "wrong": [c.upper() for c in self.wrong_letters()],
            "errors": self.errors, "max_errors": self.max_errors,
            "definition": self.definition, "level": LEVEL_NAMES[self.level],
        }

    def summary(self) -> str:
        parts = [f"mot de {len(self.target)} lettres : {self.mask()}",
                 f"erreurs : {self.errors}/{self.max_errors}"]
        if self.tried:
            parts.append("lettres essayées : " + ", ".join(c.upper() for c in self.tried))
        wrong = self.wrong_letters()
        if wrong:
            parts.append("lettres absentes : " + ", ".join(c.upper() for c in wrong))
        # No definition here: this goes to the LLM, which would guess the word from it.
        return " ; ".join(parts)

    def recap(self) -> str:
        return (f"partie de pendu de niveau {LEVEL_NAMES[self.level]}, lancée par "
                f"{self._started_by} ; {self.summary()}")

    def is_over(self) -> bool:
        return self._over

    def clue(self) -> str:
        return f"Indice : {self.definition}" if self.definition else ""

    def leaks(self, text: str) -> bool:
        # NAO said the word (it can guess it from the letters found, like a viewer).
        return bool(self.word) and not self._over and leaks(text, self.word)

    # ── Start and definition (hidden LLM call) ──────────────────────────────

    def configure(self, settings: dict):
        self.max_errors = int(settings.get("hangman_max_errors", self.max_errors))
        self.min_letters = int(settings.get("hangman_min_letters", self.min_letters))
        self.nao_min_found = float(settings.get("hangman_nao_min_found", self.nao_min_found))

    def start(self, player: Player, options: dict) -> Event:
        words = self._words if self._words is not None else load_words()
        self.level = LEVELS.get(str((options or {}).get("difficulty", "")).lower(), "medium")
        words = {level: [w for w in pool if len(plain(w)) >= self.min_letters]
                 for level, pool in words.items()}
        pool = words.get(self.level) or [w for level in words.values() for w in level]
        if not pool:
            return Event("no_words", "La liste de mots du pendu est vide.",
                         "Je n'ai pas de mot à te faire deviner.", ok=False)
        self.word = self._rng.choice(pool)
        self._started_by = player.label()
        self._ask = {
            "id": uuid.uuid4().hex[:8], "purpose": "définition du mot du pendu",
            "system": ("Tu écris des définitions de dictionnaire très courtes pour un jeu du pendu. "
                       "Réponds UNIQUEMENT par la définition, en une phrase de 15 mots au plus, "
                       "sans jamais écrire le mot lui-même, ni un mot de la même famille, ni ses "
                       "lettres, ni son nombre de lettres."),
            "user": f"Mot : {self.word}",
        }
        return self._started_event(waiting=True)

    def pending_ask(self) -> dict | None:
        return self._ask

    def on_answer(self, ask_id: str, text: str) -> Event:
        self._ask = None
        definition = " ".join(str(text).strip().strip('"«»').split())
        if definition and len(definition) <= DEFINITION_MAX_CHARS and not leaks(definition, self.word):
            self.definition = definition
        return self._started_event()

    def _started_event(self, waiting: bool = False) -> Event:
        n = len(self.target)
        text = (f"{self._started_by} lance une partie de pendu, niveau {LEVEL_NAMES[self.level]} : "
                f"un mot de {n} lettres, {self.max_errors} erreurs permises.")
        say = f"C'est parti pour un pendu ! Un mot de {n} lettres."
        read = ""
        if self.definition:
            # Read word for word by the code: the LLM never sees the definition.
            read = self.clue()
            text += " Un indice (une définition) va être lu aux spectateurs."
        elif not waiting:
            text += " Pas de définition pour ce mot."
        return Event("started", text, say, mood="enthousiasme", read=read)

    # ── Moves ───────────────────────────────────────────────────────────────

    def on_move(self, move: dict, player: Player) -> Event:
        kind = move.get("type")
        if kind == "letter":
            return self._letter(str(move.get("letter", "")), player)
        if kind == "word":
            return self._word(str(move.get("word", "")), player)
        if kind == "said" and player.is_nao:
            # NAO said the word in a sentence (body-gateway asked /check): its own guess.
            if leaks(str(move.get("text", "")), self.word):
                found = 1 - self.hidden_count() / max(1, len(self.target))
                if found < self.nao_min_found:
                    # Too early: NAO keeps its idea to itself, its sentence is not said.
                    return Event("nao_holds", "NAO a une idée du mot mais laisse chercher.",
                                 self._rng.choice([
                                     "J'ai bien une petite idée, mais je vous laisse chercher !",
                                     "Je crois savoir… mais je ne dis rien, à vous de jouer !",
                                     "Hé hé, j'ai ma petite idée. Allez, encore quelques lettres !"]),
                                 ok=False, mood="frimeur")
                return self._won(player, WORD_BONUS + self.hidden_count(), f"{player.label()} dit le mot")
            return Event("invalid", "NAO n'a pas dit le mot.", ok=False)
        return Event("invalid", "Ce coup n'existe pas au pendu : on propose une lettre ou le mot entier.",
                     "Je n'ai pas compris ta proposition.", ok=False)

    def _letter(self, raw: str, player: Player) -> Event:
        letter = plain(raw.strip())
        if not re.fullmatch(r"[a-z]", letter):
            return Event("invalid", f"« {raw} » n'est pas une lettre.",
                         "Il me faut une seule lettre.", ok=False)
        if letter in self.tried:
            return Event("already", f"La lettre {letter.upper()} a déjà été proposée.",
                         f"Le {letter.upper()} a déjà été joué !", ok=False)
        self.tried.append(letter)
        count = self.target.count(letter)
        who = player.label()
        if count:
            if self.hidden_count() == 0:
                return self._won(player, count + WORD_BONUS,
                                 f"{who} propose le {letter.upper()}, qui complète le mot")
            return Event("good",
                         f"{who} propose le {letter.upper()} : il est dans le mot ({count} fois).",
                         self._rng.choice([f"Bien vu {who}, le {letter.upper()} y est !",
                                           f"Oui ! Le {letter.upper()} est dans le mot.",
                                           f"Bravo {who}, il y a bien un {letter.upper()}."]),
                         points={player.login: count} if player.login else {}, mood="joie")
        return self._error(f"{who} propose le {letter.upper()} : il n'est pas dans le mot.",
                           self._rng.choice([f"Raté, pas de {letter.upper()} !",
                                             f"Aïe, le {letter.upper()} n'y est pas.",
                                             f"Pas de {letter.upper()}, désolé {who}."]))

    def _word(self, raw: str, player: Player) -> Event:
        guess = re.sub(r"[^a-z]", "", plain(raw))
        who = player.label()
        if not guess:
            return Event("invalid", "Aucun mot proposé.", "Quel mot proposes-tu ?", ok=False)
        if player.login and player.login in self.no_word:
            return Event("word_refused",
                         f"{who} s'est déjà trompé de mot : il ne peut plus proposer de mot, "
                         "seulement des lettres.",
                         f"Désolé {who}, plus de mot pour toi, seulement des lettres.", ok=False)
        if guess == self.target:
            return self._won(player, WORD_BONUS + self.hidden_count(), f"{who} trouve le mot")
        if player.login:
            self.no_word.add(player.login)
        return self._error(f"{who} propose le mot « {raw.strip()} » : ce n'est pas le bon. "
                           f"{who} ne peut plus proposer de mot.",
                           self._rng.choice([f"Non {who}, ce n'est pas ça !",
                                             "Mauvais mot, ça coûte une erreur.",
                                             f"Raté {who}, et plus de mot pour toi."]))

    def _error(self, text: str, say: str) -> Event:
        self.errors += 1
        if self.errors >= self.max_errors:
            self._over = True
            return Event("lost", f"{text} C'est la {self.errors}e erreur : la partie est perdue, "
                                 f"le pendu est complet. Le mot était « {self.word} ».",
                         f"Perdu ! Le mot était {self.word}.", mood="déception")
        left = self.max_errors - self.errors
        return Event("bad", f"{text} Erreur {self.errors}/{self.max_errors}, encore {left} permises.",
                     say, mood="suspense" if left <= 2 else "déception")

    def _won(self, player: Player, points: int, how: str) -> Event:
        self._over = True
        text = (f"{how} : « {self.word} » ! La partie est gagnée avec {self.errors} erreur(s). "
                f"{player.label()} marque {points} points.")
        if player.is_nao:
            return Event("won", text + " C'est toi, NAO, qui as trouvé le mot avant les spectateurs : "
                                       "réagis en frimeur, fier et taquin.",
                         f"Hé hé, c'était {self.word} ! Trop facile pour moi !",
                         points={player.login: points}, mood="frimeur")
        return Event("won", text, f"Gagné ! Le mot était {self.word}, bravo {player.label()} !",
                     points={player.login: points} if player.login else {}, mood="joie")
