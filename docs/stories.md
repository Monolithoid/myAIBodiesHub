# Stories: NAO reads a book on stream

NAO reads and acts a public domain book during a stream. The book is prepared once,
offline, into a **pre-recording**; reading it needs no LLM. Today: preparation of one
chapter from the command line, and reading from the control room.

```
book.txt ─ import, segmentation (code) ─ characters sheet + director (LLM) ─► NN.script.jsonl
NN.script.jsonl ─ player (regie) ─ POST /perform ─► body-gateway ─► voices, gesture
```

The text NAO says is cut by the code and frozen; the LLM only returns annotations by
segment number (speaker, emotion, energy, gesture, pause). An answer that misses,
adds or reorders a segment is refused, asked again once, then the chapter is left to
be reviewed.

## LLM of the preparation

Set on the `regie` service; each variable falls back on `BRAIN_*` when empty.

| Variable | Meaning |
|---|---|
| `STORY_BRAIN_PROVIDER` | `ollama`, `openai` or `anthropic` |
| `STORY_BRAIN_MODEL` | model name |
| `STORY_BRAIN_URL` | API URL; from the `regie` container a local Ollama is `http://ollama:11434` |
| `STORY_BRAIN_API_KEY` | API key |
| `STORY_BRAIN_TIMEOUT` | longest silence of the API in seconds, default 600 |
| `STORY_BRAIN_EFFORT` | reasoning effort (Anthropic, OpenAI `gpt-6*`): `low`, `medium`, `high`, `xhigh`; the values accepted depend on the model (`gpt-6-luna` refuses `max`) |

The preparation runs once per book. With OpenAI, a low-cost setting to start with:

```
STORY_BRAIN_PROVIDER=openai
STORY_BRAIN_MODEL=gpt-6-luna
STORY_BRAIN_EFFORT=xhigh
STORY_BRAIN_TIMEOUT=1800
```

`gpt-6.1-terra` or `gpt-6.2-astra` cost more and are the next steps if the annotations
disappoint. The key is the one of `BRAIN_API_KEY` when the chat already uses OpenAI.
Set `STORY_BRAIN_EFFORT` whenever `BRAIN_EFFORT` is set, or the chat's effort is used.

## Import and prepare from the page

Control room → **Histoires**, section **Livres**:

1. **Importer un livre…**: the file (plain text or HTML, about 9 MB at most), title,
   author, year of first publication, identifier (the story's directory). The chapters
   found are listed with their number of passages and estimated length. If the split is
   wrong, import again with another "Titres de chapitre" regular expression (possible
   as long as no chapter is prepared).
2. **Préparer** on a chapter calls the LLM in the background (one chapter at a time,
   several minutes with some models). Prepare the chapters in order: the character
   sheet grows and the previous summary is recalled. "Préparer à nouveau" overwrites
   the script, hand edits included.
3. Title, author and year can be corrected at any time while the story is stopped; the
   scripts are updated. Once a chapter is prepared, the story can be read.

The state of a preparation is kept in memory: restarting `regie` interrupts it.

## Prepare a chapter from the command line

The source is a plain text or HTML file (for example from Project Gutenberg), in
`/opt/ia/nao/stories/` on the host (`/data/stories` in the container):

```bash
# Coolify adds a suffix to container names
REGIE=$(docker ps --filter name=regie --format '{{.Names}}' | head -1)
docker exec "$REGIE" python3 -m storyteller.prepare /data/stories/book.txt \
    --out /data/stories/my-book --title "Voyage au centre de la Terre" \
    --author "Jules Verne" --year 1864 --chapter 1 --segment-only
```

Check the number of chapters and `chapters/01.segments.json`, then run the same
command without `--segment-only`. Chapter headings are detected as `CHAPTER …` /
`CHAPITRE …` or a Roman number alone on its line; `--chapter-rule` takes another
regular expression.

| File in the story directory | Content |
|---|---|
| `book.json`, `source.txt` | description and cleaned text |
| `chapters/NN.segments.json` | frozen segments, one file per chapter |
| `characters.json` | characters and their voice slot (`f1`-`f3`, `m1`-`m3`) |
| `voices.json` | voice chosen for each slot (written by the stories page) |
| `chapters/01.annotations.json` | summary and annotations of the LLM |
| `chapters/01.script.jsonl` | the pre-recording; may be edited by hand |

## Pre-recording

JSON Lines, schema in
[`prerecording.schema.json`](../robotics/regie/storyteller/prerecording.schema.json).
A header line `{title, author, year, chapters, source, profile, timed, voices}`, then
one line per text: `text`, `emotion`, `seg`, `chapter`, and optionally `speaker`,
`energy`, `gesture`, `pause_after_s`, `at_ms` (milliseconds from the start of the
story; the text waits for that instant). Entries `{sound, theme, file?}` are noises
played after the text before them (see **Sounds**).

The director may propose a sound after a segment that tells it: a short noise only
(gunshot, rockfall, explosion, thunder), never music or a lasting ambience, at most one
every 5 segments. It is written right after its text as
`{"sound": "coup-de-feu", "theme": "coup de feu", "seg", "chapter"}`, without a file.
A chapter prepared before this has no proposed sound: prepare it again to get some.

A hand-written timed example:

```bash
docker exec "$REGIE" cp -r /app/storyteller/examples/timed-test /data/stories/
```

## Voices

The narrator keeps NAO's voice (`TTS_VOICE` of the drivers). Every other `speaker` is
a slot, `f1`-`f3` or `m1`-`m3`, chosen by the LLM for each character. Each slot has a
default French edge-tts voice with a slight rate and pitch of its own. Two characters
in the same slot sound the same.

In **Personnages et voix**, at the bottom of the stories page, move a character to
another slot or pick the voice behind a slot, then save (only while the story is
stopped). The scripts are rewritten: their header `voices`, and the `speaker` of the
moved characters' passages; other hand edits are kept. Chapters prepared later use
the same voices.

`POST /perform` takes `voice`, `rate` and `pitch` on a speech action; a voice that
fails falls back to NAO's voice. Redeploy `body-gateway`, `regie`, `nao-core` (and
the static drone if it should use them): an older driver ignores them.

## Sounds

The bank is `_sounds/` in the stories directory (`/opt/ia/nao/stories/_sounds/` on the
host), shared by every story: short MP3 noises, 1 MB at most, named after the sound
they make (`coup-de-feu.mp3`). It is never listed as a story.

In **Sons**, on the stories page (while the story is stopped), each proposed sound
shows the text it follows. Pick a file of the bank, or upload an MP3: it joins the
bank under the sound's name, then is assigned (an existing file is never replaced:
pick it from the list). "Associer …" offers the bank file named after the sound.
"Retirer" takes the sound out of the script.
The section links to free sound effect banks (Pixabay, Freesound with the CC0
licence, Mixkit, La Sonothèque) and, for each sound, to a search of its theme on
Pixabay and Freesound; check each sound's licence before using it on a stream.

A sound with a file is read like a sentence, as if NAO made the noise: `POST /perform`
gets a speech action whose text is the caption `[coup de feu]` and whose `sound` is the
MP3 (base64). It goes to the same voices as NAO's speech (OBS, the static drone's
speaker, NAO when no voice is connected and its driver plays MP3), without edge-tts,
and the next text waits for its end. The OBS caption is shown in purple
(`--caption-sound-color`, [OBS overlays](obs-overlays.md)). A sound without a file, or
whose file is missing, is skipped. Redeploy `regie`, `body-gateway`, `nao-core` and
`obs-agent`: an older driver would read the caption aloud.

## Read it

Control room → **Histoires** (`/stories?token=$REGIE_TOKEN`): pick the story, then
⏮ ⏪ ▶ ⏸ ⏹ ⏩ ⏭.

- ▶ takes the `story` activity of game-master, says "Je vous lis {title}, de
  {author}, chapitre {n}" and reads from the bookmark. After a pause: no sentence.
- While reading, every command waits for the end of the passage; seeks add up.
  Paused or stopped, a seek moves at once.
- To read fluently, like the chat, the next passage is sent as soon as the current
  one starts, so its voice is ready. The bar is then slower: a command usually applies
  one passage later. Nothing is sent ahead across a chapter, before a timed text or
  one with a gesture, after a pause of 1 s or more, or without a connected voice body;
  there a command applies at the end of the current passage.
- ⏸ keeps the activity. ⏹ saves the bookmark and gives it back; when already stopped,
  it frees a `story` activity left behind.
- ⏮ goes to the start of the chapter, then to the previous one.

The games overlay (`game-master`, `/overlay`) shows the title, the author, the year
(if known) and "Chapitre n / total", one line each, and clears when the reading stops.

One activity at a time: no game starts during a story and the other way round. The
OBS scene can follow it with `story=<scene>` in `OBS_SCENES`
([obs-overlays.md](obs-overlays.md) §3). A moderator's `!stop` or "Annuler le jeu"
also stops the reading after the current passage. An error of body-gateway stops the
reading and gives the activity back.

Chat messages and "ok nao" are still answered between two passages; a dedicated story
mode is not there yet.
