"""
segment — chapter paragraphs → segments of narration or dialogue, without any LLM.

A segment is a short sentence or a group of short sentences (about 5 to 15 s of
speech), never across two paragraphs.  Its text is frozen here: the LLM only
annotates segments by number.
"""

import re

TARGET_MIN_CHARS = 70    # shorter sentences are grouped with their neighbour
TARGET_MAX_CHARS = 210   # a group never grows beyond this
HARD_MAX_CHARS = 320     # a longer sentence is cut at ";" or ":"

ABBREVIATIONS = {"m", "mm", "mme", "mmes", "mlle", "mlles", "mgr", "dr", "pr", "st", "ste",
                 "cf", "av", "fig", "chap", "vol", "mr", "mrs", "no"}
_CLOSERS = "»”\"')]"
# End of sentence: punctuation, closing quotes (with the French space), then a space.
_END = re.compile(r"[.!?…]+(?:\s?[" + re.escape(_CLOSERS) + r"])*(?=\s)")
_DIALOGUE = re.compile(r"^(?:—|–|―|--|«|“|\"|- )")


def _ends_with_abbreviation(before: str) -> bool:
    word = re.search(r"([^\W\d_]+)$", before)
    if not word:
        return False
    word = word.group(1)
    # "M. Lidenbrock", an initial ("J. Verne").
    return word.lower() in ABBREVIATIONS or (len(word) == 1 and word.isupper())


def sentences(paragraph: str) -> list[str]:
    found, start = [], 0
    for match in _END.finditer(paragraph):
        rest = paragraph[match.end():].lstrip()
        if not rest or rest[0].islower() or rest[0] in ",;:":
            continue   # "« Venez ! » dit-il."
        if match.group().startswith(".") and not match.group().startswith("..") \
                and _ends_with_abbreviation(paragraph[:match.start()]):
            continue
        # A lone closing quote belongs to the sentence it closes.
        piece = paragraph[start:match.end()].strip()
        if not any(c.isalnum() for c in piece):
            continue
        found.append(piece)
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        if found and not any(c.isalnum() for c in tail):
            found[-1] = f"{found[-1]} {tail}"
        else:
            found.append(tail)
    return found


def _cut_long(sentence: str) -> list[str]:
    if len(sentence) <= HARD_MAX_CHARS:
        return [sentence]
    cuts = [m.end() for m in re.finditer(r"\s?[;:](?=\s)", sentence)]
    cuts = [c for c in cuts if TARGET_MIN_CHARS <= c <= len(sentence) - TARGET_MIN_CHARS]
    if not cuts:
        return [sentence]
    middle = min(cuts, key=lambda c: abs(c - len(sentence) / 2))
    return _cut_long(sentence[:middle].strip()) + _cut_long(sentence[middle:].strip())


def group(pieces: list[str]) -> list[str]:
    grouped: list[str] = []
    for piece in pieces:
        if grouped and min(len(grouped[-1]), len(piece)) < TARGET_MIN_CHARS \
                and len(grouped[-1]) + len(piece) + 1 <= TARGET_MAX_CHARS:
            grouped[-1] = f"{grouped[-1]} {piece}"
        else:
            grouped.append(piece)
    return grouped


def segment_chapter(chapter: dict, first_seg: int = 1) -> list[dict]:
    """[{"seg", "chapter", "paragraph", "kind", "text"}] numbered from first_seg."""
    segments = []
    for number, paragraph in enumerate(chapter["paragraphs"], 1):
        kind = "dialogue" if _DIALOGUE.match(paragraph) else "narration"
        pieces = [cut for s in sentences(paragraph) for cut in _cut_long(s)]
        for text in group(pieces):
            segments.append({"seg": first_seg + len(segments), "chapter": chapter["chapter"],
                             "paragraph": number, "kind": kind, "text": text})
    return segments


def segment_book(chapters: list[dict]) -> list[list[dict]]:
    """Segments of every chapter; numbers run on through the whole book."""
    result, next_seg = [], 1
    for chapter in chapters:
        segments = segment_chapter(chapter, next_seg)
        next_seg += len(segments)
        result.append(segments)
    return result
