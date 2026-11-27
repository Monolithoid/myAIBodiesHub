"""
prerecording — the one file the player knows, whatever the source of the story.

JSON Lines (schema: prerecording.schema.json).  The first line is the header,
each next line is a text to say, or a sound played between two texts:

  {"title", "author", "year", "chapters", "source", "profile", "timed", "voices"}
  {"seg", "chapter", "text", "emotion",
   "speaker"?, "energy"?, "gesture"?, "pause_after_s"?, "at_ms"?}
  {"sound", "theme", "file"?, "seg"?, "chapter"?, "at_ms"?}

A story is a directory holding one or more "*.script.jsonl" files (one per
chapter), in the directory itself or in its "chapters" sub-directory.
"""

import json
import pathlib

EMOTIONS = ("neutral", "calm", "curious", "joyful", "excited",
            "uncertain", "worried", "scared", "angry", "sad")
ENERGIES = ("very-low", "low", "medium", "high", "very-high")
VOICE_SLOTS = ("f1", "f2", "f3", "m1", "m2", "m3")
SPEAKERS = ("narrator",) + VOICE_SLOTS
MAX_PAUSE_S = 3.0
DEFAULT_PAUSE_S = 0.3
SCRIPT_GLOB = "*.script.jsonl"

HEADER_FIELDS = ("title", "author", "year", "chapters", "source", "profile", "timed", "voices")
TEXT_FIELDS = ("seg", "chapter", "text", "emotion", "speaker", "energy", "gesture",
               "pause_after_s", "at_ms")
SOUND_FIELDS = ("sound", "theme", "file", "seg", "chapter", "at_ms")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def header_errors(header) -> list[str]:
    if not isinstance(header, dict):
        return ["header: not an object"]
    errors = [f"header: unknown field {k!r}" for k in header if k not in HEADER_FIELDS]
    for key in ("title", "author"):
        if not isinstance(header.get(key), str) or not header[key].strip():
            errors.append(f"header: {key!r} must be a non-empty string")
    if header.get("year") is not None and not _is_int(header["year"]):
        errors.append("header: 'year' must be an integer or null")
    if not _is_int(header.get("chapters")) or header["chapters"] < 1:
        errors.append("header: 'chapters' must be a positive integer")
    if not isinstance(header.get("timed", False), bool):
        errors.append("header: 'timed' must be true or false")
    voices = header.get("voices", {})
    if not isinstance(voices, dict) or any(
            slot not in VOICE_SLOTS or not isinstance(v, str) for slot, v in voices.items()):
        errors.append("header: 'voices' maps f1-f3 / m1-m3 to a voice name")
    return errors


def entry_errors(entry) -> list[str]:
    if not isinstance(entry, dict):
        return ["not an object"]
    errors = []
    at_ms = entry.get("at_ms")
    if "at_ms" in entry and (not _is_int(at_ms) or at_ms < 0):
        errors.append("'at_ms' must be a non-negative integer")
    for key in ("seg", "chapter"):
        if key in entry and not _is_int(entry[key]):
            errors.append(f"{key!r} must be an integer")
    if "sound" in entry:
        errors += [f"unknown field {k!r}" for k in entry if k not in SOUND_FIELDS]
        if not isinstance(entry.get("theme"), str) or not entry["theme"].strip():
            errors.append("a sound needs a 'theme'")
        return errors
    errors += [f"unknown field {k!r}" for k in entry if k not in TEXT_FIELDS]
    errors += [f"missing {k!r}" for k in ("seg", "chapter") if k not in entry]
    if not isinstance(entry.get("text"), str) or not entry["text"].strip():
        errors.append("'text' must be a non-empty string")
    if entry.get("emotion") not in EMOTIONS:
        errors.append(f"'emotion' must be one of {', '.join(EMOTIONS)}")
    if "speaker" in entry and entry["speaker"] not in SPEAKERS:
        errors.append(f"'speaker' must be one of {', '.join(SPEAKERS)}")
    if "energy" in entry and entry["energy"] not in ENERGIES:
        errors.append(f"'energy' must be one of {', '.join(ENERGIES)}")
    if entry.get("gesture") is not None:
        errors += gesture_errors(entry["gesture"])
    pause = entry.get("pause_after_s")
    if "pause_after_s" in entry and (not _is_number(pause) or not 0 <= pause <= MAX_PAUSE_S):
        errors.append(f"'pause_after_s' must be between 0 and {MAX_PAUSE_S:g}")
    return errors


def gesture_errors(gesture) -> list[str]:
    if not isinstance(gesture, dict) or set(gesture) != {"emotion", "intensity"}:
        return ["'gesture' must be null or {\"emotion\", \"intensity\"}"]
    errors = []
    if gesture["emotion"] not in EMOTIONS:
        errors.append("gesture 'emotion' not in the list")
    if not _is_number(gesture["intensity"]) or not 0 <= gesture["intensity"] <= 1:
        errors.append("gesture 'intensity' must be between 0 and 1")
    return errors


def dumps(header: dict, entries: list) -> str:
    """The file's content; ValueError if anything is invalid."""
    errors = header_errors(header)
    for i, entry in enumerate(entries):
        errors += [f"entry {i + 1}: {e}" for e in entry_errors(entry)]
    if errors:
        raise ValueError("; ".join(errors[:20]))
    return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in [header, *entries])


def loads(content: str) -> tuple[dict, list]:
    """(header, entries) of one file; ValueError naming the bad line."""
    lines = [(n, line) for n, line in enumerate(content.splitlines(), 1) if line.strip()]
    if not lines:
        raise ValueError("empty file")
    parsed = []
    for n, line in lines:
        try:
            value = json.loads(line)
        except ValueError as e:
            raise ValueError(f"line {n}: invalid JSON ({e})") from None
        errors = header_errors(value) if not parsed else entry_errors(value)
        if errors:
            raise ValueError(f"line {n}: {'; '.join(errors)}")
        parsed.append(value)
    return parsed[0], parsed[1:]


def script_files(directory) -> list[pathlib.Path]:
    directory = pathlib.Path(directory)
    return sorted(directory.glob(SCRIPT_GLOB)) + sorted((directory / "chapters").glob(SCRIPT_GLOB))


def load_story(directory) -> tuple[dict, list]:
    """(header, entries of every script file in file order); ValueError if none or invalid."""
    files = script_files(directory)
    if not files:
        raise ValueError(f"no {SCRIPT_GLOB} file in {directory}")
    header, entries = None, []
    for path in files:
        try:
            file_header, file_entries = loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"{path.name}: {e}") from None
        header = header or file_header
        entries += file_entries
    return header, entries
