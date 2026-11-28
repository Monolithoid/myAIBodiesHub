"""
sounds — the sound bank and the noises a story asks for.

The bank is one directory shared by every story, "_sounds" in the stories
directory (never listed as a story): short MP3 noises, named after the sound
they make ("coup-de-feu.mp3").  The director proposes sounds while preparing a
chapter ({"sound", "theme"} entries without a file); whoever checks the story
gives each one a file of the bank, uploads one into the bank, or removes it.
A sound without a file is not played.
"""

import base64
import binascii
import pathlib
import re

from storyteller import annotate, prepare, prerecording

BANK = "_sounds"
MAX_BYTES = 1_000_000       # a short noise (body-gateway refuses more)
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,60}\.mp3")


def bank(root) -> pathlib.Path:
    return pathlib.Path(root) / BANK


def files(root) -> list[str]:
    directory = bank(root)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if _NAME.fullmatch(p.name) and p.is_file())


def _checked_name(name) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("nom de son inconnu")
    return name


def read(root, name) -> bytes:
    """The MP3 of a bank file; ValueError when there is none."""
    path = bank(root) / _checked_name(name)
    if not path.is_file():
        raise ValueError(f"son absent de la banque : {name}")
    return path.read_bytes()


def _is_mp3(data: bytes) -> bool:
    # An ID3 tag, or an MPEG audio frame sync.
    return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


def add(root, name, content) -> dict:
    """Uploads an MP3 (base64) into the bank under the name of its sound; an existing
    file is never replaced, other stories may use it."""
    stem = annotate.sound_id(pathlib.PurePath(str(name or "")).stem)
    filename = f"{stem[:60].strip('-') or 'son'}.mp3"
    try:
        data = base64.b64decode(str(content or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("fichier illisible") from None
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f"un son fait au plus {MAX_BYTES // 1000} ko")
    if not _is_mp3(data):
        raise ValueError("ce n'est pas un fichier MP3")
    path = bank(root) / filename
    if path.exists():
        raise ValueError(f"{filename} existe déjà dans la banque : choisis-le dans la liste")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return {"file": filename, "bank": files(root)}


def listing(root, directory: pathlib.Path) -> dict:
    """The sounds a story asks for, in reading order, with the text they follow and the
    bank file named after them ("suggested") when one exists."""
    available = files(root)
    found = []
    for path in prerecording.script_files(directory):
        _, entries = prerecording.loads(path.read_text(encoding="utf-8"))
        before = ""
        for entry in entries:
            if "sound" not in entry:
                before = entry["text"]
                continue
            suggested = f"{annotate.sound_id(str(entry['sound']))}.mp3"
            found.append({"chapter": entry.get("chapter"), "seg": entry.get("seg"),
                          "theme": entry["theme"], "file": entry.get("file", ""),
                          "missing": bool(entry.get("file")) and entry["file"] not in available,
                          "suggested": suggested if suggested in available else "",
                          "before": before})
    return {"sounds": found, "bank": available}


def assign(root, directory: pathlib.Path, body: dict) -> dict:
    """{"chapter", "seg", "file": bank file | "" (none), "remove"?}: the sound after that
    segment gets its file, loses it, or is taken out of the script."""
    chapter, seg = body.get("chapter"), body.get("seg")
    if type(chapter) is not int or type(seg) is not int:
        raise ValueError("chapitre et segment attendus")
    remove = body.get("remove") is True
    chosen = str(body.get("file") or "")
    if chosen and not remove:
        read(root, chosen)          # it must be in the bank
    for path in prerecording.script_files(directory):
        header, entries = prerecording.loads(path.read_text(encoding="utf-8"))
        index = next((i for i, e in enumerate(entries) if "sound" in e
                      and e.get("chapter") == chapter and e.get("seg") == seg), None)
        if index is None:
            continue
        if remove:
            del entries[index]
        elif chosen:
            entries[index]["file"] = chosen
        else:
            entries[index].pop("file", None)
        prepare._write(path, prerecording.dumps(header, entries))
        return listing(root, directory)
    raise ValueError("son introuvable dans le script")
