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
| `GET /overlay?token=…` | — | OBS page (Server-Sent Events from `/overlay/stream`) |

`player` is `{"login", "name", "roles", "source"}`; `roles` among `broadcaster`,
`moderator`, `vip`, `subscriber`. A source other than `twitch` (Open WebUI, control
room) is the streamer. An `event` is `{"kind", "text", "say", "ok", "points"}`:
`text` is the fact given to the LLM, `say` the canned sentence.

## Configuration

| Variable | Service | Default | Meaning |
|---|---|---|---|
| `GAME_TOKEN` | game-master, body-gateway, twitch-input, regie | — | API and overlay secret (game-master refuses to start without it; empty elsewhere = games off) |
| `GAME_START_ROLES` | game-master | `everyone` | who may start a game: `everyone`, or a comma list among `moderator`, `vip`, `subscriber` (the streamer always may) |
| `MOVE_COOLDOWN_S` | game-master | `0` | per-viewer delay between two moves |
| `GAMES_DB_PATH` | game-master | `/data/games.db` | scores (volume `/opt/ia/nao/games`) |
| `GAME_MASTER_URL` | body-gateway | empty (compose: `http://127.0.0.1:8098`) | empty = no games in the prompt |

Overlay in OBS: give game-master a domain in Coolify (container port 8098, e.g.
`https://games.$DOMAIN`), then add a browser source
`https://games.$DOMAIN/overlay?token=$GAME_TOKEN` (transparent background).

## Adding a game

A game is a `game_core.Game` subclass registered in `game_master.GAMES`:

- `id`, `name`, `how_to_play` (the moves and their JSON form, in French, for the LLM);
- `start(player, options)` and `on_move(move, player)` return an `Event`;
- `summary()` (public state for the LLM), `public_view()` (overlay), `is_over()`;
- optional: `recap()`, `on_tick(now)` (an event announced by NAO).

The overlay has a default view (`view.big`, `view.lines`); a game may add its own
renderer in `overlay.html` (`views[<id>]`).

## Tests

```bash
cd robotics/games && python3 -m unittest discover -s tests -v
cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
cd robotics/inputs/twitch && python3 -m unittest discover -s tests -v
```
