"""
annotate — the two LLM passes that prepare a chapter, and the merge into the pre-recording.

  1. characters sheet (grows chapter after chapter);
  2. director: annotations by segment number only, never the text; it may propose a
     noise (gunshot, rockfall...) played after a segment, as a theme without a file.

The answers are constrained by a JSON schema, then checked by this code; an
invalid answer is asked again once with the list of errors, then AnnotationError
("to be reviewed").  The text said by NAO comes from the segmentation, never
from the LLM.

LLM: STORY_BRAIN_PROVIDER / _MODEL / _URL / _API_KEY / _TIMEOUT / _EFFORT, each one falling
back on BRAIN_* when empty (brain_client.py, shared with body-gateway).
"""

import json
import pathlib
import re
import unicodedata

from storyteller import prerecording

PROMPTS = pathlib.Path(__file__).with_name("prompts")
GESTURE_SPACING = 4       # at most one gesture every N segments
SOUND_SPACING = 5         # at most one sound every N segments
MAX_THEME = 40            # characters of a sound's theme
BLOCK_SEGMENTS = 250      # a longer chapter is annotated in blocks
CONTEXT_SEGMENTS = 10     # already annotated segments recalled to the next block
MAX_TOKENS = 32000        # annotations of a whole block
TEMPERATURE = 0.3
NARRATOR = "narrator"
_BRAIN_KEYS = ("PROVIDER", "URL", "MODEL", "API_KEY", "TIMEOUT", "EFFORT")
DEFAULT_TIMEOUT = "600"


class AnnotationError(Exception):
    pass


def brain_env(env) -> dict:
    """BRAIN_* settings of the preparation, for brain_client.BrainClient.from_env.

    STORY_BRAIN_* wins.  When the story provider differs from the chat's, its
    URL, model, key and effort are not inherited: they belong to the other provider.
    """
    def read(name):
        return str(env.get(name) or "").strip()

    chat = read("BRAIN_PROVIDER").lower() or "ollama"
    provider = read("STORY_BRAIN_PROVIDER").lower() or chat
    result = {}
    for key in _BRAIN_KEYS:
        value = read(f"STORY_BRAIN_{key}")
        if not value and (provider == chat or key == "TIMEOUT"):
            value = read(f"BRAIN_{key}")
        if value:
            result[f"BRAIN_{key}"] = value
    result["BRAIN_PROVIDER"] = provider
    # A whole chapter takes far longer than a chat answer.
    if not read("STORY_BRAIN_TIMEOUT"):
        result["BRAIN_TIMEOUT"] = DEFAULT_TIMEOUT
    return result


def _object(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


_STRING = {"type": "string"}


def characters_schema() -> dict:
    return _object({"characters": {"type": "array", "items": _object({
        "id": _STRING, "name": _STRING, "aliases": {"type": "array", "items": _STRING},
        "description": _STRING,
        "voice": {"type": "string", "enum": list(prerecording.VOICE_SLOTS)}})}})


def director_schema(speakers: list[str]) -> dict:
    emotion = {"type": "string", "enum": list(prerecording.EMOTIONS)}
    gesture = {"anyOf": [_object({"emotion": emotion, "intensity": {"type": "number"}}),
                         {"type": "null"}]}
    return _object({"summary": _STRING, "segments": {"type": "array", "items": _object({
        "seg": {"type": "integer"},
        "speaker": {"type": "string", "enum": speakers},
        "emotion": emotion,
        "energy": {"type": "string", "enum": list(prerecording.ENERGIES)},
        "gesture": gesture,
        "pause_after_s": {"type": "number"},
        "sound": {"anyOf": [_STRING, {"type": "null"}]}})}})


def parse_json(raw: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    found = re.search(r"\{.*\}", cleaned, re.DOTALL)
    try:
        value = json.loads(found.group() if found else cleaned)
    except ValueError as e:
        raise ValueError(f"invalid JSON ({e}); the answer may have been cut short") from None
    if not isinstance(value, dict):
        raise ValueError("the answer is not a JSON object")
    return value


def character_errors(answer: dict, known: list[dict]) -> list[str]:
    characters = answer.get("characters")
    if not isinstance(characters, list):
        return ["'characters' must be a list"]
    errors, ids = [], []
    for i, c in enumerate(characters):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) \
                or not re.fullmatch(r"[a-z0-9_-]{1,40}", c["id"]):
            errors.append(f"character {i + 1}: 'id' must be lowercase letters, digits, '-' or '_'")
            continue
        ids.append(c["id"])
        if not isinstance(c.get("name"), str) or not c["name"].strip():
            errors.append(f"{c['id']}: missing 'name'")
        if c.get("voice") not in prerecording.VOICE_SLOTS:
            errors.append(f"{c['id']}: 'voice' must be one of {', '.join(prerecording.VOICE_SLOTS)}")
    errors += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    errors += [f"known character {c['id']} is missing: keep every existing id"
               for c in known if c["id"] not in ids]
    voices = {c["id"]: c["voice"] for c in known}
    errors += [f"{c['id']}: keep the voice {voices[c['id']]}" for c in characters
               if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"] in voices
               and c.get("voice") != voices[c["id"]]]
    return errors


def director_errors(answer: dict, segments: list[dict], speakers: list[str],
                    previous_gesture: int | None = None,
                    previous_sound: int | None = None) -> list[str]:
    """Every rule the schema cannot state, and the schema's again (not every LLM obeys it)."""
    annotations = answer.get("segments")
    if not isinstance(annotations, list) or not all(isinstance(a, dict) for a in annotations):
        return ["'segments' must be a list of objects"]
    expected = [s["seg"] for s in segments]
    given = [a.get("seg") for a in annotations]
    if given != expected:
        missing = sorted(set(expected) - {g for g in given if isinstance(g, int)})
        detail = f"; missing: {missing[:20]}" if missing else ""
        return [f"exactly one object per segment, in order, from {expected[0]} to {expected[-1]} "
                f"({len(expected)} expected, {len(given)} given{detail})"]
    errors, last_gesture, last_sound = [], previous_gesture, previous_sound
    allowed = {"seg", "speaker", "emotion", "energy", "gesture", "pause_after_s", "sound"}
    for a in annotations:
        seg = a["seg"]
        errors += [f"segment {seg}: forbidden field {k!r}" for k in a if k not in allowed]
        errors += [f"segment {seg}: missing {k!r}" for k in sorted(allowed - set(a))]
        if a.get("speaker") not in speakers:
            errors.append(f"segment {seg}: unknown speaker {a.get('speaker')!r}")
        if a.get("emotion") not in prerecording.EMOTIONS:
            errors.append(f"segment {seg}: emotion {a.get('emotion')!r} not in the list")
        if a.get("energy") not in prerecording.ENERGIES:
            errors.append(f"segment {seg}: energy {a.get('energy')!r} not in the list")
        pause = a.get("pause_after_s")
        if not isinstance(pause, (int, float)) or not 0 <= pause <= prerecording.MAX_PAUSE_S:
            errors.append(f"segment {seg}: pause_after_s must be between 0 and 3")
        if a.get("gesture") is not None:
            errors += [f"segment {seg}: {e}" for e in prerecording.gesture_errors(a["gesture"])]
            if last_gesture is not None and seg - last_gesture < GESTURE_SPACING:
                errors.append(f"segment {seg}: gesture too close to the one of segment "
                              f"{last_gesture} (one every {GESTURE_SPACING} segments at most)")
            last_gesture = seg
        sound = a.get("sound")
        if sound is not None:
            if not isinstance(sound, str) or not 0 < len(sound.strip()) <= MAX_THEME:
                errors.append(f"segment {seg}: sound must be null or a theme of 1 to "
                              f"{MAX_THEME} characters")
            if last_sound is not None and seg - last_sound < SOUND_SPACING:
                errors.append(f"segment {seg}: sound too close to the one of segment "
                              f"{last_sound} (one every {SOUND_SPACING} segments at most)")
            last_sound = seg
    return errors


def ask(client, system: str, user: str, schema: dict, check, log=print) -> dict:
    """One constrained call, checked; asked again once with the errors."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    errors: list[str] = []
    for attempt in (1, 2):
        raw = client.chat(messages, TEMPERATURE, schema=schema, max_tokens=MAX_TOKENS)
        try:
            answer = parse_json(raw)
            errors = check(answer)
        except ValueError as e:
            errors = [str(e)]
        if not errors:
            return answer
        log(f"attempt {attempt}: {len(errors)} error(s): {'; '.join(errors[:5])}")
        messages = messages[:2] + [
            {"role": "assistant", "content": raw or "{}"},
            {"role": "user", "content": "Réponse refusée. Corrige ces erreurs et renvoie le JSON "
                                        "complet :\n- " + "\n- ".join(errors[:40])}]
    raise AnnotationError("; ".join(errors[:20]))


def prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8").strip()


def numbered(segments: list[dict]) -> str:
    return "\n".join(f"[{s['seg']}] {s['text']}" for s in segments)


def update_characters(client, title: str, known: list[dict], segments: list[dict],
                      log=print) -> list[dict]:
    """Pass 1: the sheet completed with this chapter."""
    user = (f"Fiche actuelle :\n{json.dumps({'characters': known}, ensure_ascii=False)}\n\n"
            f"Texte du chapitre :\n" + "\n".join(s["text"] for s in segments))
    answer = ask(client, prompt("characters.txt").format(titre=title), user, characters_schema(),
                 lambda a: character_errors(a, known), log)
    return answer["characters"]


def direct_chapter(client, title: str, characters: list[dict], segments: list[dict],
                   previous_summary: str = "", log=print) -> dict:
    """Pass 2: {"summary", "segments": [annotations]} of one chapter, in blocks if long."""
    speakers = [NARRATOR] + [c["id"] for c in characters if c["id"] != NARRATOR]
    system = prompt("director.txt").format(
        titre=title, bloc_mode=prompt("mode_text.txt"), ecart_gestes=GESTURE_SPACING,
        ecart_sons=SOUND_SPACING, longueur_theme=MAX_THEME,
        emotions=", ".join(prerecording.EMOTIONS), energies=", ".join(prerecording.ENERGIES))
    sheet = json.dumps({"characters": characters}, ensure_ascii=False)
    annotations: list[dict] = []
    summaries: list[str] = []
    for start in range(0, len(segments), BLOCK_SEGMENTS):
        block = segments[start:start + BLOCK_SEGMENTS]
        parts = [f"Fiche des personnages :\n{sheet}"]
        if previous_summary:
            parts.append(f"Résumé du chapitre précédent :\n{previous_summary}")
        if annotations:
            parts.append("Derniers segments déjà annotés (contexte, ne pas les renvoyer) :\n"
                         + "\n".join(json.dumps(a, ensure_ascii=False)
                                     for a in annotations[-CONTEXT_SEGMENTS:]))
        parts.append(f"Segments à annoter :\n{numbered(block)}")
        last_gesture = next((a["seg"] for a in reversed(annotations) if a["gesture"]), None)
        last_sound = next((a["seg"] for a in reversed(annotations) if a["sound"]), None)
        log(f"director: segments {block[0]['seg']}-{block[-1]['seg']} ({len(block)})")
        answer = ask(client, system, "\n\n".join(parts), director_schema(speakers),
                     lambda a: director_errors(a, block, speakers, last_gesture, last_sound),
                     log)
        annotations += answer["segments"]
        summaries.append(str(answer.get("summary", "")).strip())
    return {"summary": " ".join(s for s in summaries if s), "segments": annotations}


def merge(segments: list[dict], annotations: list[dict], characters: list[dict]) -> list[dict]:
    """Pre-recording entries: the frozen text with its annotations, speakers as voice slots.

    A proposed sound follows its segment, without a file until one is chosen.
    """
    voices = {c["id"]: c["voice"] for c in characters}
    by_seg = {a["seg"]: a for a in annotations}
    entries = []
    for s in segments:
        a = by_seg[s["seg"]]
        speaker = a["speaker"]
        entry = {"seg": s["seg"], "chapter": s["chapter"], "text": s["text"],
                 "emotion": a["emotion"],
                 "speaker": NARRATOR if speaker == NARRATOR else voices[speaker],
                 "energy": a["energy"], "pause_after_s": a["pause_after_s"]}
        if a.get("gesture"):
            entry["gesture"] = a["gesture"]
        entries.append(entry)
        theme = (a.get("sound") or "").strip()     # absent from annotations of older versions
        if theme:
            entries.append({"sound": sound_id(theme), "theme": theme,
                            "seg": s["seg"], "chapter": s["chapter"]})
    return entries


def sound_id(theme: str) -> str:
    """'Coup de feu' -> 'coup-de-feu': the name a sound bank can match."""
    plain = unicodedata.normalize("NFKD", theme).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "sound"
