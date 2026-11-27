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
| `STORY_BRAIN_EFFORT` | reasoning effort (Anthropic, OpenAI `gpt-6*`): `low` to `max` |

The preparation runs once per book. With OpenAI, a low-cost setting to start with:

```
STORY_BRAIN_PROVIDER=openai
STORY_BRAIN_MODEL=gpt-6-luna
STORY_BRAIN_EFFORT=max
STORY_BRAIN_TIMEOUT=1800
```

`gpt-6.1-terra` or `gpt-6.2-astra` cost more and are the next steps if the annotations
disappoint. The key is the one of `BRAIN_API_KEY` when the chat already uses OpenAI.
Set `STORY_BRAIN_EFFORT` whenever `BRAIN_EFFORT` is set, or the chat's effort is used.

## Prepare a chapter

The source is a plain text or HTML file (for example from Project Gutenberg), in
`/opt/ia/nao/stories/` on the host (`/data/stories` in the container):

```bash
docker exec regie python3 -m storyteller.prepare /data/stories/book.txt \
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
| `chapters/01.segments.json` | frozen segments |
| `characters.json` | characters and their voice slot (`f1`-`f3`, `m1`-`m3`) |
| `chapters/01.annotations.json` | summary and annotations of the LLM |
| `chapters/01.script.jsonl` | the pre-recording; may be edited by hand |

## Pre-recording

JSON Lines, schema in
[`prerecording.schema.json`](../robotics/regie/storyteller/prerecording.schema.json).
A header line `{title, author, year, chapters, source, profile, timed, voices}`, then
one line per text: `text`, `emotion`, `seg`, `chapter`, and optionally `speaker`,
`energy`, `gesture`, `pause_after_s`, `at_ms` (milliseconds from the start of the
story; the text waits for that instant). Entries `{sound, theme, file?}` are accepted
and not played yet; voices per speaker are not applied yet either.

A hand-written timed example:

```bash
docker exec regie cp -r /app/storyteller/examples/timed-test /data/stories/
```

## Read it

Control room → **Histoires** (`/stories?token=$REGIE_TOKEN`): pick the story, then
⏮ ⏪ ▶ ⏸ ⏹ ⏩ ⏭.

- ▶ takes the `story` activity of game-master, says "Je vous lis {title}, de
  {author}, chapitre {n}" and reads from the bookmark. After a pause: no sentence.
- While reading, every command waits for the end of the passage; seeks add up.
  Paused or stopped, a seek moves at once.
- ⏸ keeps the activity. ⏹ saves the bookmark and gives it back; when already stopped,
  it frees a `story` activity left behind.
- ⏮ goes to the start of the chapter, then to the previous one.

One activity at a time: no game starts during a story and the other way round. The
OBS scene can follow it with `story=<scene>` in `OBS_SCENES`
([obs-overlays.md](obs-overlays.md) §3). A moderator's `!stop` or "Annuler le jeu"
also stops the reading after the current passage. An error of body-gateway stops the
reading and gives the activity back.

Chat messages and "ok nao" are still answered between two passages; a dedicated story
mode is not there yet.
