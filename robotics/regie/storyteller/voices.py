"""
voices — the voice of each character.

A speaker is the narrator (NAO's own voice: nothing is sent) or one of six slots
(f1-f3, m1-m3) chosen by the LLM in the character sheet.  At the review, the human
picks the edge-tts voice behind each slot (header "voices" of the scripts, kept in
voices.json for the next chapters) and may move a character to another slot.
"""

import json
import os
import pathlib

from storyteller import prerecording

NARRATOR = "narrator"
CHOICES_FILE = "voices.json"

CHOICES = {   # French voices of edge-tts, except the narrator's default one
    "fr-FR-VivienneMultilingualNeural": "Vivienne (France, femme)",
    "fr-FR-EloiseNeural": "Éloïse (France, enfant)",
    "fr-BE-CharlineNeural": "Charline (Belgique, femme)",
    "fr-CA-SylvieNeural": "Sylvie (Québec, femme)",
    "fr-CH-ArianeNeural": "Ariane (Suisse, femme)",
    "fr-FR-HenriNeural": "Henri (France, homme)",
    "fr-FR-RemyMultilingualNeural": "Rémy (France, homme)",
    "fr-BE-GerardNeural": "Gérard (Belgique, homme)",
    "fr-CA-AntoineNeural": "Antoine (Québec, homme)",
    "fr-CA-JeanNeural": "Jean (Québec, homme)",
    "fr-CA-ThierryNeural": "Thierry (Québec, homme)",
    "fr-CH-FabriceNeural": "Fabrice (Suisse, homme)",
}

# Default voice of each slot, with a slight variation of rate and pitch.
SLOTS = {
    "f1": ("fr-FR-VivienneMultilingualNeural", "+0%", "+0Hz"),
    "f2": ("fr-FR-EloiseNeural", "+4%", "+2Hz"),
    "f3": ("fr-BE-CharlineNeural", "-4%", "-3Hz"),
    "m1": ("fr-FR-HenriNeural", "+0%", "+0Hz"),
    "m2": ("fr-FR-RemyMultilingualNeural", "+4%", "+2Hz"),
    "m3": ("fr-BE-GerardNeural", "-5%", "-5Hz"),
}


def speech_options(header: dict, speaker: str | None) -> dict:
    """{"voice", "rate", "pitch"} of a speech action; {} for the narrator."""
    if speaker not in SLOTS:
        return {}
    voice, rate, pitch = SLOTS[speaker]
    return {"voice": header.get("voices", {}).get(speaker) or voice, "rate": rate, "pitch": pitch}


def _read(path: pathlib.Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: pathlib.Path, content: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _characters(directory: pathlib.Path) -> list[dict]:
    return _read(directory / "characters.json", {"characters": []})["characters"]


def casting(directory) -> dict:
    """What the review shows: the characters, the voice of each slot, the voices offered."""
    directory = pathlib.Path(directory)
    header, _ = prerecording.load_story(directory)
    chosen = {**header.get("voices", {}), **_read(directory / CHOICES_FILE, {})}
    return {
        "characters": [{"id": c["id"], "name": c["name"], "voice": c["voice"],
                        "description": c.get("description", "")}
                       for c in _characters(directory) if c["id"] != NARRATOR],
        "voices": {slot: chosen.get(slot) or default[0] for slot, default in SLOTS.items()},
        "choices": [{"id": k, "label": v} for k, v in CHOICES.items()],
    }


def save(directory, slots: dict, chosen: dict) -> dict:
    """slots: {character id: slot}; chosen: {slot: voice}.  Rewrites the scripts.

    Only the "speaker" of a moved character's passages changes (found through the
    annotations), so a script edited by hand keeps its other changes.
    """
    directory = pathlib.Path(directory)
    characters = _characters(directory)
    old = {c["id"]: c["voice"] for c in characters if c["id"] != NARRATOR}
    if not isinstance(slots, dict) or any(
            k not in old or v not in prerecording.VOICE_SLOTS for k, v in slots.items()):
        raise ValueError("personnage ou emplacement inconnu")
    current = casting(directory)["voices"]
    if not isinstance(chosen, dict) or any(
            slot not in SLOTS or not isinstance(v, str)
            or (v not in CHOICES and v != current[slot]) for slot, v in chosen.items()):
        raise ValueError("emplacement ou voix inconnu")
    voices = {**current, **chosen}
    moved = {k: v for k, v in slots.items() if v != old[k]}

    rewritten = []      # every file is checked before the first one is written
    for path in prerecording.script_files(directory):
        header, entries = prerecording.loads(path.read_text(encoding="utf-8"))
        annotations = _read(path.with_name(path.name.replace(".script.jsonl", ".annotations.json")),
                            {"segments": []})
        who = {a["seg"]: a["speaker"] for a in annotations["segments"]}
        for entry in entries:
            character = who.get(entry.get("seg"))
            if "sound" not in entry and character in moved \
                    and entry.get("speaker") == old[character]:
                entry["speaker"] = moved[character]
        header["voices"] = voices
        rewritten.append((path, prerecording.dumps(header, entries)))
    for path, content in rewritten:
        _write(path, content)
    _write(directory / CHOICES_FILE, _json(voices))
    if moved:
        for c in characters:
            c["voice"] = moved.get(c["id"], c["voice"])
        _write(directory / "characters.json", _json({"characters": characters}))
    return casting(directory)
