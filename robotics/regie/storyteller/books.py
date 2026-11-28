"""
books — the books of the stories page: import, description, preparation of a chapter.

A book is a story directory with a book.json (prepare.import_book).  It can be read
once at least one chapter is prepared.
"""

import base64
import binascii
import json
import os
import pathlib
import re
import threading

from storyteller import annotate, book, prepare, prerecording

STORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
LOG_LINES = 20


def _directory(root, story_id) -> pathlib.Path:
    if not isinstance(story_id, str) or not STORY_ID.fullmatch(story_id):
        raise ValueError("identifiant : lettres sans accent, chiffres, « . », « _ » ou « - »")
    return pathlib.Path(root) / story_id


def _meta(directory: pathlib.Path) -> dict:
    try:
        return json.loads((directory / "book.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"livre inconnu : {directory.name!r}") from None


def _description(body: dict) -> dict:
    title, author = str(body.get("title") or "").strip(), str(body.get("author") or "").strip()
    year = body.get("year")
    if not title or not author:
        raise ValueError("le titre et l'auteur sont obligatoires")
    if year is not None and (type(year) is not int or not -3000 <= year <= 3000):
        raise ValueError("année : un nombre, ou vide")
    return {"title": title, "author": author, "year": year}


def _script(directory: pathlib.Path, chapter: int) -> pathlib.Path:
    return directory / "chapters" / f"{chapter:02d}.script.jsonl"


def listing(root) -> list[dict]:
    found = []
    for path in sorted(pathlib.Path(root).glob("*/book.json")):
        if not STORY_ID.fullmatch(path.parent.name):
            continue
        try:
            meta = _meta(path.parent)
            found.append({"id": path.parent.name, "title": meta["title"],
                          "author": meta["author"]})
        except (ValueError, KeyError, TypeError):
            continue
    return found


def describe(root, story_id: str) -> dict:
    directory = _directory(root, story_id)
    meta = _meta(directory)
    rule = meta.get("chapter_rule") or book.DEFAULT_CHAPTER_RULE
    _, chapters, segments = prepare.cut((directory / "source.txt").read_bytes(), rule)
    return {"id": directory.name, "title": meta["title"], "author": meta["author"],
            "year": meta.get("year"), "chapters": [
                {"chapter": c["chapter"], "title": c["title"], "segments": len(part),
                 "minutes": round(sum(len(s["text"]) for s in part) / 14 / 60),
                 "prepared": _script(directory, c["chapter"]).exists()}
                for c, part in zip(chapters, segments)]}


def add(root, body: dict) -> dict:
    """Import.  A book with a prepared chapter is never replaced: its scripts would not
    match the new segments."""
    directory = _directory(root, body.get("story"))
    meta = _description(body)
    if prerecording.script_files(directory):
        raise ValueError(f"« {directory.name} » a déjà un chapitre préparé : "
                         "choisis un autre identifiant")
    try:
        raw = base64.b64decode(str(body.get("content") or ""), validate=True)
    except binascii.Error:
        raise ValueError("fichier illisible") from None
    if not raw:
        raise ValueError("choisis un fichier (texte ou HTML)")
    rule = str(body.get("rule") or "").strip() or book.DEFAULT_CHAPTER_RULE
    try:
        re.compile(rule)
    except re.error as e:
        raise ValueError(f"expression des titres de chapitre invalide : {e}") from None
    source = os.path.basename(str(body.get("filename") or "book.txt"))
    prepare.import_book(raw, directory, source=source, rule=rule, **meta)
    return describe(root, directory.name)


def update(root, body: dict) -> dict:
    """Title, author and year, in book.json and in the header of every script."""
    directory = _directory(root, body.get("story"))
    meta = {**_meta(directory), **_description(body)}
    rewritten = []      # every file is checked before the first one is written
    for path in prerecording.script_files(directory):
        header, entries = prerecording.loads(path.read_text(encoding="utf-8"))
        header.update({k: meta[k] for k in ("title", "author", "year")})
        rewritten.append((path, prerecording.dumps(header, entries)))
    for path, content in rewritten:
        prepare._write(path, content)
    prepare._write(directory / "book.json", meta)
    return describe(root, directory.name)


def llm_client(env):
    import brain_client   # needs requests and anthropic

    return brain_client.BrainClient.from_env(annotate.brain_env(env))


class Preparation:
    """One chapter at a time, in the background (several minutes with a real LLM)."""

    def __init__(self, llm, log=lambda text: None):
        self._llm = llm
        self._log = log
        self._lock = threading.Lock()
        self._job = {"running": False, "story": "", "chapter": 0, "log": [], "error": ""}

    def state(self) -> dict:
        with self._lock:
            return {**self._job, "log": list(self._job["log"])}

    def busy(self, story_id: str) -> bool:
        with self._lock:
            return self._job["running"] and self._job["story"] == story_id

    def start(self, root, story_id: str, chapter, overwrite: bool = False):
        directory = _directory(root, story_id)
        meta = _meta(directory)
        if type(chapter) is not int or not 1 <= chapter <= meta["chapters"]:
            raise ValueError("chapitre inconnu")
        if _script(directory, chapter).exists() and not overwrite:
            raise ValueError("chapitre déjà préparé (le préparer à nouveau écrase son script)")
        with self._lock:
            if self._job["running"]:
                raise ValueError("une préparation est déjà en cours")
            self._job = {"running": True, "story": directory.name, "chapter": chapter,
                         "log": [], "error": ""}
        try:
            threading.Thread(target=self._run, args=(directory, chapter), daemon=True).start()
        except RuntimeError as e:
            self._end(f"préparation impossible à lancer : {e}")

    def _note(self, text: str):
        self._log(f"story preparation: {text}")
        with self._lock:
            self._job["log"] = [*self._job["log"], str(text)][-LOG_LINES:]

    def _end(self, error: str = ""):
        with self._lock:
            self._job.update(running=False, error=error)

    def _run(self, directory: pathlib.Path, chapter: int):
        error = ""
        try:
            prepare.annotate_chapter(directory, chapter, self._llm(), self._note)
        except Exception as e:     # file, network, API and refused answers of the LLM
            error = f"{type(e).__name__} : {e}"
            self._log(f"story preparation failed: {error}")
        finally:
            self._end(error)
