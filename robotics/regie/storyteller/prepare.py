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


def prepare(source: pathlib.Path, out: pathlib.Path, title: str, author: str, year: int | None,
            chapter: int = 1, rule: str = book.DEFAULT_CHAPTER_RULE, client=None, log=print):
    """client None: import and segmentation only.  Returns the files' base path of the chapter."""
    text = book.clean(book.decode(source.read_bytes()))
    chapters = book.chapters(text, rule)
    if not 1 <= chapter <= len(chapters):
        raise ValueError(f"chapter {chapter}: the book has {len(chapters)}")
    segments = segment.segment_book(chapters)[chapter - 1]
    base = out / "chapters" / f"{chapter:02d}"
    _write(out / "source.txt", text)
    _write(out / "book.json", {"title": title, "author": author, "year": year,
                               "chapters": len(chapters), "source": source.name})
    _write(base.with_suffix(".segments.json"), segments)
    chars = sum(len(s["text"]) for s in segments)
    log(f"{len(chapters)} chapter(s); chapter {chapter}: {len(segments)} segments, "
        f"{chars} characters, about {chars / 14 / 60:.0f} min of speech")
    if client is None:
        return base
    started = time.monotonic()
    sheet = _read(out / "characters.json", {"characters": []})["characters"]
    characters = annotate.update_characters(client, title, sheet, segments, log)
    _write(out / "characters.json", {"characters": characters})
    previous = _read(out / "chapters" / f"{chapter - 1:02d}.annotations.json", {})
    result = annotate.direct_chapter(client, title, characters, segments,
                                     previous.get("summary", ""), log)
    _write(base.with_suffix(".annotations.json"), {"chapter": chapter, **result})
    header = {"title": title, "author": author, "year": year, "chapters": len(chapters),
              "source": source.name, "profile": "stream", "timed": False,
              "voices": _read(out / voices.CHOICES_FILE, {})}
    entries = annotate.merge(segments, result["segments"], characters)
    _write(base.with_suffix(".script.jsonl"), prerecording.dumps(header, entries))
    log(f"{base.with_suffix('.script.jsonl')} written in {time.monotonic() - started:.0f} s "
        f"({client.provider} / {client.model})")
    return base


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
