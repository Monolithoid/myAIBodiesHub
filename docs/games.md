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
  catalog ("on peut jouer au pendu ?") while nothing runs; during a mystery, a full
  accusation, a recap or resume request, or a question naming a card.
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
- **Lives per player**: each player (NAO included) has `HANGMAN_MAX_ERRORS` errors (7);
  then they are hanged and may no longer play this game, the others go on.
- End: word found, or every viewer **still in the chat** is hanged. A viewer is in the
  chat when they wrote there (any message, not only `!nao`) or played within
  `PRESENCE_MIN` minutes: twitch-input posts the logins it sees to game-master
  (`/seen`, kept in memory only). As long as one viewer present is not hanged, the game
  goes on; a hanged viewer's leaving can end it (checked every second).
  Points: 1 per letter revealed, 3 + the letters still hidden for the word.
- Overlay: the word (`R _ _ _ _ T _ _ _ X`), the definition, the letters tried (wrong
  ones struck out), the wrong words proposed, the gallows of the last player who erred
  (their errors), the players hanged; it stays on screen a little while after the end with the solution.

## Murder mystery

- Start: "!nao on fait une enquête dans un château écossais ?" (the setting asked for
  is required). The case has 6 suspects (a motive each), 6 blunt weapons and 6 places.
- **Preparation**, two hidden LLM calls: (1) introduction, victim, cards and motives,
  checked by the code, which then **draws the solution**; (2) the story of the murder
  and 15 clues, one per card outside the solution, also checked (each clue names its
  card and no card of the solution). One single attempt: if a check fails, or the
  setting is refused, NAO says it does not suit and no game starts (the delay
  `MYSTERY_GEN_COOLDOWN_S` only counts from a case that started). A preparation with
  no answer is dropped after 5 minutes.
- NAO reads the introduction; the overlay shows the cards.
- **Questions** ("!nao où était le jardinier ?"): the hidden **archivist** call reads
  the cards and motives (not the story) and answers with a card id or "aucune", nothing
  else is used. The code
  picks the clue: the card's own clue when it is innocent and still open; the clue of
  another open card, as a witness's account, when it is in the solution or already
  crossed out, and now and then for an innocent one. The card cleared is crossed out
  and NAO's speech ends with the clue, word for word. With no clue, NAO may add a
  harmless detail (kept with the case so it stays consistent, unless it names a card)
  or say it does not know.
- **Accusation** at any time: "!nao j'accuse X avec Y dans Z". The three are required.
  A wrong one and that viewer may not accuse again in this case (questions still
  allowed). A right one wins: NAO reads the story of what happened.
- **End** also when every card but the solution is crossed out, or when every viewer
  present in the chat has accused wrongly (checked at a wrong accusation only).
  Leaving the chat never ends a case.
- **NAO does not play**, never gets the file or the solution, and answers "I don't
  know" to any question about who is involved.
- **Over several streams**: the case is saved after each change and survives restarts.
  It stays the running activity (no other game) until solved or cancelled; "on reprend
  l'enquête ?" or a recap request retells where it stands.
- Points: 5 for the right accusation, 1 per clue obtained (control room settings).
- Preparing a case needs a capable model: the 15 clues must pass the checks at once.
- Cost: two LLM calls to prepare a case; for each question, the usual two (move, NAO's
  reaction) plus the archivist's.
- Overlay: introduction, victim, "Enquête en cours", the 3 x 6 grid with cleared cards
  crossed out, the latest clues, the last investigator and who may no longer accuse;
  at the end the solution is highlighted for a minute.

## Hidden LLM calls

When a game needs the LLM for something NAO must not see, game-master's answer holds
an `ask` (`{"id", "purpose", "system", "user"}`). body-gateway runs that call outside
NAO's prompt and posts the text to `POST /answer`; only its purpose goes to `/events`.
The mystery's preparation and archivist use the same path.

## Services

| Service | Role in the games |
|---|---|
| `game-master` (`robotics/games/`) | rules, state, who may start, per-viewer delay, scores (SQLite), OBS overlay |
| `body-gateway` (`game_link.py`) | games in the prompt, forwards `start_game` / `game_move`, NAO's reaction; `POST /game-event` for events game-master announces itself |
| `twitch-input` | forwards the chat badges (`roles`); `!stop` for moderators; logins seen in the chat (`/seen`) |
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
| `POST /note` | `{"text"}` | `{"kept"}`: NAO's reaction to an event with `keep`, saved by the running game |
| `POST /seen` | `{"logins"}` | `{"ok"}`: viewers who just wrote in the chat (twitch-input, every `PRESENCE_EVERY_S`) |
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
room) is the streamer. An `event` is `{"kind", "text", "say", "ok", "points", "mood", "read", "read_as", "keep"}`:
`text` is the fact given to the LLM, `say` the canned sentence, `read` a text NAO
says word for word after its reaction (the hangman definition, a mystery's clue):
body-gateway adds it to the speech, since the LLM may drop it. `read_as` names it for
the LLM; `keep` asks body-gateway to post NAO's reaction to `/note`.

## Control room page

`/jeux?token=$REGIE_TOKEN` on the control room (link "Jeux : réglages et classement"
on its main page): the running game with a cancel button, the ranking (top 10, reset
with a confirmation) and the settings. The control room relays these calls to
game-master (`GET/POST /settings`, `GET /scores`, `POST /scores/reset`), so
`GAME_TOKEN` never reaches the browser.

Settings changed there are kept in game-master's SQLite base (`/data`) and override
the variables below, which only give the defaults. Who may start, the delay between
two moves and the presence apply at once; a game's settings at its next game. The
ranking and its reset follow the game selected on the page. Cancelling a mystery
deletes its saved case.

## Configuration

| Variable | Service | Default | Meaning |
|---|---|---|---|
| `GAME_TOKEN` | game-master, body-gateway, twitch-input, regie | — | API and overlay secret (game-master refuses to start without it; empty elsewhere = games off) |
| `GAME_START_ROLES` | game-master | `everyone` | who may start a game: `everyone`, or a comma list among `moderator`, `vip`, `subscriber` (the streamer always may) |
| `MOVE_COOLDOWN_S` | game-master | `0` | per-viewer delay between two moves |
| `HANGMAN_MAX_ERRORS` | game-master | `7` | errors of one player before they are hanged |
| `PRESENCE_MIN` | game-master | `10` | a viewer who wrote in the chat (or played) within these minutes is still there |
| `PRESENCE_EVERY_S` | twitch-input | `30` | how often the logins seen in the chat go to game-master |
| `HANGMAN_MIN_LETTERS` | game-master | `6` | shortest hangman word (the list holds 6 to 12 letters) |
| `MYSTERY_WIN_POINTS` | game-master | `5` | points of a right accusation |
| `MYSTERY_CLUE_POINTS` | game-master | `1` | points of a clue obtained |
| `MYSTERY_TESTIMONY_RATE` | game-master | `0.25` | share of questions on an innocent card answered by a witness about another card |
| `MYSTERY_GEN_COOLDOWN_S` | game-master | `0` | delay between two mysteries, counted from a case that started |
| `GAMES_DB_PATH` | game-master | `/data/games.db` | scores, settings and saved mystery (volume `/opt/ia/nao/games`) |
| `OVERLAY_HOLD_S` | game-master | `30` | longest a new state waits for `/reveal` before the overlay shows it (`0` = at once) |
| `GAME_REVEAL_MAX_S` | body-gateway | `25` | longest body-gateway waits for NAO to finish speaking before revealing |
| `HANGMAN_NAO_MIN_FOUND` | game-master | `0` | share of the word found before NAO's own guess counts (`0` = at any time); before, its sentence holding the word is replaced by a teasing one |
| `GAME_MASTER_URL` | body-gateway | empty (compose: `http://127.0.0.1:8098`) | empty = no games in the prompt |

Overlay in OBS: give game-master a domain in Coolify (container port 8098, e.g.
`https://games.$DOMAIN`), then add a browser source
`https://games.$DOMAIN/overlay?token=$GAME_TOKEN` (transparent background).
Position, size and colours of the panel: [obs-overlays.md](obs-overlays.md).

## Adding a game

A game is a `game_core.Game` subclass registered in `game_master.GAMES`:

- `id`, `name`, `how_to_play` (the moves and their JSON form, in French, for the LLM),
  `start_help` (start options, in French);
- `start(player, options)` and `on_move(move, player)` return an `Event`;
- `summary()` (public state for the LLM), `public_view()` (overlay), `is_over()`;
- optional: `recap()`, `on_tick(now)` (an event announced by NAO), `pending_ask()` /
  `on_answer()` (hidden LLM call);
- a long game sets `persistent = True` and implements `save()` / `restore(state)`
  (plus `is_ready()` / `is_aborted()` when it needs preparing).

The overlay has a default view (`view.big`, `view.lines`); a game may add its own
renderer in `overlay.html` (`views[<id>]`), with its own class and the panel's CSS
variables for its colours ([obs-overlays.md](obs-overlays.md)).

## Tests

```bash
cd robotics/games && python3 -m unittest discover -s tests -v
cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
cd robotics/inputs/twitch && python3 -m unittest discover -s tests -v
```
