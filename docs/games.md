# Games with the viewers (game-master)

NAO plays small games with the stream's viewers. Decisions are in
[ADR 010](decisions/010-game-master.md): **the code decides, the LLM speaks**.

## How a move flows

```
Twitch "!nao je propose le E"
  └─► twitch-input ── POST /chat (viewer, chat badges) ──► body-gateway
        1. LLM call: the prompt holds the running game's public state and its moves;
           the answer contains {"type": "game_move", "move": {"type": "letter", "letter": "e"}}
        2. POST game-master /move ──► settled by the code ──► event + public state
        3. LLM call: NAO's reaction to the settled event (voice, gesture, LEDs)
           (the answer of step 1 is not played)
  OBS ◄── game-master /overlay (updated as soon as the move is settled)
```

- **Everything goes through `!nao`** in natural language: starting a game ("on joue
  au pendu ?") and playing it. A game only starts from a message that is a question.
- **One activity at a time**: while a game runs, no other game (or story) can start.
- **First come, first served**: moves are settled in the order they reach the brain.
- **NAO never sees the solution**: game-master only sends the public state.
- **NAO never writes in the chat**: results are said by voice and shown on the overlay.
- **Recap**: a viewer who arrives mid-game can ask NAO where the game stands.
- **Stop**: "Annuler le jeu" button in the control room, or `!stop` from a moderator
  in the chat; NAO announces it.
- When the LLM is late, NAO says a canned sentence given by game-master.
- When the LLM forgets the game action, body-gateway infers it in unambiguous cases
  only: a single letter ("e", "la lettre e") or a single word as long as the hidden one
  ("grenier", "c'est grenier") while a hangman runs, or a question naming a game of the
  catalog ("on peut jouer au pendu ?") while nothing runs.
- **NAO plays too**, with its own points in the same ranking (login `nao`): it plays a
  move when a viewer asks it ("à toi NAO", action `"by": "nao"`). Its LLM never gets
  the solution, nor the hangman definition (the code reads it word for word, the
  overlay shows it), but it may find the word from the letters found: while a game
  runs, body-gateway asks game-master (`/check`) about each sentence, and one that
  holds the solution is played as NAO's guess (move `said`). NAO then wins and
  reacts as a show-off, at any time by default (`HANGMAN_NAO_MIN_FOUND` can make it
  wait until part of the word is found).
- **Ranking** on the overlay: points of every game played (NAO included), top 3
  during a game, top 5 at the end.

## Hangman

- Start: "!nao on joue au pendu ?" (optionally "facile", "moyen", "difficile"; medium
  by default). A French common noun of 6 to 12 letters is drawn from
  [`robotics/games/words/hangman_fr.json`](../robotics/games/words/README.md)
  (Lexique 3.83, CC BY-SA 4.0).
- Right after the draw, a **hidden LLM call** writes a short definition. The code
  checks it gives nothing away (the word, its stem, its letters spelled out); otherwise
  the game goes on without a definition. NAO reads it when the game starts and with
  the recap: the code adds it to the speech, NAO's LLM never sees it.
- Moves: a letter ("!nao je propose le E"), or the whole word at any time. Letters are
  compared without accents. A wrong word costs an error, and that viewer may no longer
  propose a word in this game (letters still allowed).
- End: word found, or `HANGMAN_MAX_ERRORS` errors (7). Points: 1 per letter revealed,
  3 + the letters still hidden for the word.
- Overlay: the word (`R _ _ _ _ T _ _ _ X`), the definition, the letters tried (wrong
  ones struck out), the gallows drawn step by step with the errors; it stays on screen
  a little while after the end with the solution.

### Hidden LLM calls

When a game needs the LLM for something NAO must not see, game-master's answer holds
an `ask` (`{"id", "purpose", "system", "user"}`). body-gateway runs that call outside
NAO's prompt and posts the text to `POST /answer`; only its purpose goes to `/events`.
The mystery's archivist will use the same path.

## Services

| Service | Role in the games |
|---|---|
| `game-master` (`robotics/games/`) | rules, state, who may start, per-viewer delay, scores (SQLite), OBS overlay |
| `body-gateway` (`game_link.py`) | games in the prompt, forwards `start_game` / `game_move`, NAO's reaction; `POST /game-event` for events game-master announces itself |
| `twitch-input` | forwards the chat badges (`roles`); `!stop` for moderators |
| `regie` | "Annuler le jeu" button; game events in the feed (`game` kind) |

## game-master API

Token: `Authorization: Bearer $GAME_TOKEN` (or `?token=`).

| Route | Body | Answer |
|---|---|---|
| `GET /ping` | — (no token) | `{"ok": true}` |
| `GET /state` | — | `{"activity": null \| {...}, "games": [...], "last_event"}` |
| `POST /start` | `{"game", "player", "message", "options"}` | `{"event", "state"}` |
| `POST /move` | `{"move", "player"}` | `{"event", "state"}` |
| `POST /stop` | `{"player"}` | `{"event", "state"}`; NAO announces it |
| `POST /answer` | `{"id", "text", "player"}` | `{"event", "state"}`: text of a hidden LLM call (`""` = failed) |
| `POST /check` | `{"text"}` | `{"leak"}`: NAO's sentence holds the running game's solution (then played as its guess) |
| `POST /reveal` | `{"version"}` | `{"revealed"}`: NAO has said the result, the overlay may show the state of that version |
| `GET /settings` | — | `{"values", "schema"}`: settings of the control room |
| `POST /settings` | `{key: value, …}` | `{"values"}`; 400 `{"error"}` when a value is refused (nothing saved) |
| `GET /scores?game=&n=` | — | `{"game", "top"}`: ranking |
| `POST /scores/reset` | `{"game"}` | ranking back to zero (every game without `game`) |
| `GET /overlay?token=…` | — | OBS page (Server-Sent Events from `/overlay/stream`) |

**No spoiler on the overlay**: every state carries a `version`. game-master holds
each new state; body-gateway posts `/reveal` once NAO has finished saying its
reaction, so the overlay never shows a result before NAO says it. A state nobody
reveals is shown after `OVERLAY_HOLD_S`.

`player` is `{"login", "name", "roles", "source"}`; `roles` among `broadcaster`,
`moderator`, `vip`, `subscriber`. A source other than `twitch` (Open WebUI, control
room) is the streamer. An `event` is `{"kind", "text", "say", "ok", "points", "mood", "read"}`:
`text` is the fact given to the LLM, `say` the canned sentence, `read` a text NAO
says word for word after its reaction (the hangman definition): body-gateway adds
it to the speech, since the LLM may drop it.

## Control room page

`/jeux?token=$REGIE_TOKEN` on the control room (link "Jeux : réglages et classement"
on its main page): the running game with a cancel button, the ranking (top 10, reset
with a confirmation) and the settings. The control room relays these calls to
game-master (`GET/POST /settings`, `GET /scores`, `POST /scores/reset`), so
`GAME_TOKEN` never reaches the browser.

Settings changed there are kept in game-master's SQLite base (`/data`) and override
the variables below, which only give the defaults. Who may start and the delay
between two moves apply at once; the hangman settings at the next game.

## Configuration

| Variable | Service | Default | Meaning |
|---|---|---|---|
| `GAME_TOKEN` | game-master, body-gateway, twitch-input, regie | — | API and overlay secret (game-master refuses to start without it; empty elsewhere = games off) |
| `GAME_START_ROLES` | game-master | `everyone` | who may start a game: `everyone`, or a comma list among `moderator`, `vip`, `subscriber` (the streamer always may) |
| `MOVE_COOLDOWN_S` | game-master | `0` | per-viewer delay between two moves |
| `HANGMAN_MAX_ERRORS` | game-master | `7` | errors before the hangman game is lost |
| `HANGMAN_MIN_LETTERS` | game-master | `6` | shortest hangman word (the list holds 6 to 12 letters) |
| `GAMES_DB_PATH` | game-master | `/data/games.db` | scores (volume `/opt/ia/nao/games`) |
| `OVERLAY_HOLD_S` | game-master | `30` | longest a new state waits for `/reveal` before the overlay shows it (`0` = at once) |
| `GAME_REVEAL_MAX_S` | body-gateway | `25` | longest body-gateway waits for NAO to finish speaking before revealing |
| `HANGMAN_NAO_MIN_FOUND` | game-master | `0` | share of the word found before NAO's own guess counts (`0` = at any time); before, its sentence holding the word is replaced by a teasing one |
| `GAME_MASTER_URL` | body-gateway | empty (compose: `http://127.0.0.1:8098`) | empty = no games in the prompt |

Overlay in OBS: give game-master a domain in Coolify (container port 8098, e.g.
`https://games.$DOMAIN`), then add a browser source
`https://games.$DOMAIN/overlay?token=$GAME_TOKEN` (transparent background).

## Adding a game

A game is a `game_core.Game` subclass registered in `game_master.GAMES`:

- `id`, `name`, `how_to_play` (the moves and their JSON form, in French, for the LLM),
  `start_help` (start options, in French);
- `start(player, options)` and `on_move(move, player)` return an `Event`;
- `summary()` (public state for the LLM), `public_view()` (overlay), `is_over()`;
- optional: `recap()`, `on_tick(now)` (an event announced by NAO), `pending_ask()` /
  `on_answer()` (hidden LLM call).

The overlay has a default view (`view.big`, `view.lines`); a game may add its own
renderer in `overlay.html` (`views[<id>]`).

## Tests

```bash
cd robotics/games && python3 -m unittest discover -s tests -v
cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
cd robotics/inputs/twitch && python3 -m unittest discover -s tests -v
```
