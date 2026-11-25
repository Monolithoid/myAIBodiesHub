#!/usr/bin/env python3
"""
Build hangman_fr.json, the hangman word list, from the Lexique 3.83 database.

Lexique (www.lexique.org, B. New & C. Pallier) is licensed under CC BY-SA 4.0:
the generated list is a derived work under the same license (see README.md).

Kept: common nouns in their base form (lemma, not plural), whose most frequent
use is as a noun, 6 to 12 letters once accents are removed, letters only, not
in blocklist_fr.txt, frequent enough to be known.  Difficulty from the
frequency per million words (max of films and books):

    easy    >= 50        medium  10 to 50        hard   3 to 10

Usage (Lexique383.tsv from http://www.lexique.org/databases/Lexique383/Lexique383.zip):

    python3 build_hangman_words.py /path/to/Lexique383.tsv
"""

import csv
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_LETTERS, MAX_LETTERS = 6, 12
LEVELS = (("easy", 50.0), ("medium", 10.0), ("hard", 3.0))


def letters(word: str) -> str:
    """'Œuvre' → 'oeuvre', 'éléphant' → 'elephant': what the viewers guess."""
    word = word.lower().replace("œ", "oe").replace("æ", "ae")
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))


def load_blocklist(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        return {letters(line.strip()) for line in f if line.strip() and not line.startswith("#")}


def build(tsv_path: str, blocklist: set[str]) -> dict:
    nouns: dict[str, float] = {}       # word → frequency as a noun
    other: dict[str, float] = {}       # word → highest frequency in another category
    with open(tsv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            word = row["ortho"]
            freq = max(float(row["freqfilms2"] or 0), float(row["freqlivres"] or 0))
            if row["cgram"] == "NOM" and row["lemme"] == word and row["nombre"] != "p":
                nouns[word] = max(freq, nouns.get(word, 0.0))
            elif row["cgram"] != "NOM":
                other[word] = max(freq, other.get(word, 0.0))
    levels: dict[str, list[str]] = {name: [] for name, _ in LEVELS}
    for word, freq in nouns.items():
        plain = letters(word)
        if not re.fullmatch(rf"[a-z]{{{MIN_LETTERS},{MAX_LETTERS}}}", plain):
            continue
        if plain in blocklist or other.get(word, 0.0) >= freq:
            continue   # sensitive, or mostly used as an adjective / verb ("nouveau", "tendre")
        for name, threshold in LEVELS:
            if freq >= threshold:
                levels[name].append(word)
                break
    return {
        "source": "Lexique 3.83, www.lexique.org (B. New, C. Pallier), CC BY-SA 4.0",
        "license": "CC BY-SA 4.0",
        "letters": [MIN_LETTERS, MAX_LETTERS],
        "levels": {name: sorted(words) for name, words in levels.items()},
    }


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    data = build(sys.argv[1], load_blocklist(os.path.join(HERE, "blocklist_fr.txt")))
    out = os.path.join(HERE, "hangman_fr.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(out, {name: len(words) for name, words in data["levels"].items()})


if __name__ == "__main__":
    main()
