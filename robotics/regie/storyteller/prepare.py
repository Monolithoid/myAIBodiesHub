#!/usr/bin/env python3
"""
prepare — turns a book into the pre-recording of one chapter, from the command line.

    python3 -m storyteller.prepare BOOK.txt --title "..." --author "..." --year 1864 \\
        --out /data/stories/my-book --chapter 1 [--segment-only]

Writes in --out: book.json, source.txt, chapters/NN.segments.json, characters.json,
chapters/NN.annotations.json and chapters/NN.script.jsonl.  --segment-only stops
before the LLM, to check the chapter split and the segments.
"""

import argparse
import json
import os
import pathlib
import sys
import time

from storyteller import annotate, book, prerecording, segment, voices


def _write(path: pathlib.Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read(path: pathlib.Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def cut(raw: bytes, rule: str = book.DEFAULT_CHAPTER_RULE) -> tuple[str, list, list]:
    """(cleaned text, chapters, segments of each chapter) of a book file."""
    text = book.clean(book.decode(raw))
    chapters = book.chapters(text, rule)
    return text, chapters, segment.segment_book(chapters)


def import_book(raw: bytes, out: pathlib.Path, title: str, author: str, year: int | None,
                source: str, rule: str = book.DEFAULT_CHAPTER_RULE) -> list:
    """Writes the cleaned text, the description and the segments of every chapter."""
    text, chapters, segments = cut(raw, rule)
    _write(out / "source.txt", text)
    _write(out / "book.json", {"title": title, "author": author, "year": year,
                               "chapters": len(chapters), "source": source,
                               "chapter_rule": rule})
    for old in (out / "chapters").glob("*.segments.json"):
        old.unlink()
    for number, part in enumerate(segments, 1):
        _write(out / "chapters" / f"{number:02d}.segments.json", part)
    return segments


def annotate_chapter(out: pathlib.Path, chapter: int, client, log=print) -> pathlib.Path:
    """The two LLM passes of an imported chapter, then its pre-recording."""
    base = out / "chapters" / f"{chapter:02d}"
    meta = _read(out / "book.json", None)
    if meta is None:
        raise ValueError("not an imported book: no book.json")
    if not 1 <= chapter <= meta["chapters"]:
        raise ValueError(f"chapter {chapter}: the book has {meta['chapters']}")
    segments = _read(base.with_suffix(".segments.json"), None)
    if segments is None:      # imported by an earlier version: one segments file only
        rule = meta.get("chapter_rule") or book.DEFAULT_CHAPTER_RULE
        segments = cut((out / "source.txt").read_bytes(), rule)[2][chapter - 1]
        _write(base.with_suffix(".segments.json"), segments)
    started = time.monotonic()
    sheet = _read(out / "characters.json", {"characters": []})["characters"]
    characters = annotate.update_characters(client, meta["title"], sheet, segments, log)
    _write(out / "characters.json", {"characters": characters})
    previous = _read(out / "chapters" / f"{chapter - 1:02d}.annotations.json", {})
    result = annotate.direct_chapter(client, meta["title"], characters, segments,
                                     previous.get("summary", ""), log)
    _write(base.with_suffix(".annotations.json"), {"chapter": chapter, **result})
    header = {"title": meta["title"], "author": meta["author"], "year": meta.get("year"),
              "chapters": meta["chapters"], "source": meta.get("source", ""),
              "profile": "stream", "timed": False,
              "voices": _read(out / voices.CHOICES_FILE, {})}
    entries = annotate.merge(segments, result["segments"], characters)
    _write(base.with_suffix(".script.jsonl"), prerecording.dumps(header, entries))
    log(f"{base.with_suffix('.script.jsonl')} written in {time.monotonic() - started:.0f} s "
        f"({client.provider} / {client.model})")
    return base


def prepare(source: pathlib.Path, out: pathlib.Path, title: str, author: str, year: int | None,
            chapter: int = 1, rule: str = book.DEFAULT_CHAPTER_RULE, client=None, log=print):
    """client None: import and segmentation only.  Returns the files' base path of the chapter."""
    raw = source.read_bytes()
    count = len(cut(raw, rule)[1])
    if not 1 <= chapter <= count:
        raise ValueError(f"chapter {chapter}: the book has {count}")
    segments = import_book(raw, out, title, author, year, source.name, rule)[chapter - 1]
    chars = sum(len(s["text"]) for s in segments)
    log(f"{count} chapter(s); chapter {chapter}: {len(segments)} segments, "
        f"{chars} characters, about {chars / 14 / 60:.0f} min of speech")
    if client is None:
        return out / "chapters" / f"{chapter:02d}"
    return annotate_chapter(out, chapter, client, log)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare one chapter of a book for NAO.")
    parser.add_argument("source", type=pathlib.Path, help="plain text or HTML file")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="directory of the story")
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--year", type=int, help="year of first publication")
    parser.add_argument("--chapter", type=int, default=1)
    parser.add_argument("--chapter-rule", default=book.DEFAULT_CHAPTER_RULE,
                        help="regular expression of a chapter heading line")
    parser.add_argument("--segment-only", action="store_true", help="no LLM call")
    args = parser.parse_args(argv)
    client = None
    if not args.segment_only:
        import brain_client   # needs requests and anthropic

        client = brain_client.BrainClient.from_env(annotate.brain_env(os.environ))
    try:
        prepare(args.source, args.out, args.title, args.author, args.year, args.chapter,
                args.chapter_rule, client)
    except (OSError, ValueError, annotate.AnnotationError) as e:
        sys.exit(f"preparation failed, chapter to be reviewed: {e}")
    except Exception as e:   # network and API errors of the LLM provider
        sys.exit(f"preparation failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
