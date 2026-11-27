"""Tests of the story preparation (format, import, segmentation, LLM passes), without network.

    cd robotics/regie && python3 -m unittest discover -s tests -v
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

REGIE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REGIE))

from storyteller import annotate, book, prepare, prerecording, segment  # noqa: E402

HEADER = {"title": "Voyage", "author": "Jules Verne", "year": 1864, "chapters": 2,
          "source": "v.txt", "profile": "stream", "timed": False, "voices": {}}
TEXT = {"seg": 1, "chapter": 1, "text": "Bonjour.", "emotion": "calm"}


class TestPrerecording(unittest.TestCase):

    def test_round_trip(self):
        entries = [TEXT, {"sound": "waves", "theme": "vagues"},
                   {**TEXT, "seg": 2, "speaker": "m1", "energy": "high", "pause_after_s": 1,
                    "gesture": {"emotion": "joyful", "intensity": 0.4}, "at_ms": 500}]
        self.assertEqual(prerecording.loads(prerecording.dumps(HEADER, entries)), (HEADER, entries))

    def test_refused(self):
        for bad in ({**TEXT, "emotion": "happy"}, {**TEXT, "speaker": "lidenbrock"},
                    {**TEXT, "speech": "x"}, {**TEXT, "pause_after_s": 4},
                    {"seg": 1, "chapter": 1, "emotion": "calm"}, {"sound": "x"},
                    {**TEXT, "gesture": {"emotion": "calm"}}, {**TEXT, "at_ms": -1}):
            with self.assertRaises(ValueError, msg=bad):
                prerecording.dumps(HEADER, [bad])
        with self.assertRaises(ValueError):
            prerecording.dumps({**HEADER, "chapters": 0}, [])
        with self.assertRaisesRegex(ValueError, "line 2"):
            prerecording.loads(json.dumps(HEADER) + "\nnot json\n")

    def test_schema_matches_the_code(self):
        schema = json.loads((REGIE / "storyteller" / "prerecording.schema.json").read_text())
        defs = schema["$defs"]
        self.assertEqual(tuple(defs["header"]["properties"]), prerecording.HEADER_FIELDS)
        self.assertEqual(set(defs["text"]["properties"]), set(prerecording.TEXT_FIELDS))
        self.assertEqual(set(defs["sound"]["properties"]), set(prerecording.SOUND_FIELDS))
        self.assertEqual(tuple(defs["text"]["properties"]["emotion"]["enum"]), prerecording.EMOTIONS)
        self.assertEqual(tuple(defs["text"]["properties"]["speaker"]["enum"]), prerecording.SPEAKERS)
        self.assertEqual(tuple(defs["text"]["properties"]["energy"]["enum"]), prerecording.ENERGIES)

    def test_timed_example_is_valid(self):
        header, entries = prerecording.load_story(REGIE / "storyteller/examples/timed-test")
        self.assertTrue(header["timed"])
        self.assertEqual(len([e for e in entries if "sound" not in e]), 4)


GUTENBERG = """The Project Gutenberg eBook\r
*** START OF THE PROJECT GUTENBERG EBOOK VOYAGE ***\r
TABLE\r
\r
I\r
II\r
\r
I\r
\r
Le 24 mai 1863, un dimanche, mon oncle, le professeur Lidenbrock, revint\r
précipitamment vers sa petite maison[1].\r
\r
La bonne Marthe dut se croire fort en retard, car le dîner commençait à peine\r
à chanter sur le fourneau de la cuisine, _naturellement_.\r
\r
* * *\r
\r
II\r
\r
« Axel, suis-moi ! » Je n'eus pas le temps de répondre. Le professeur était déjà\r
dans son cabinet, et il m'attendait avec une impatience que je connaissais bien.\r
M. Lidenbrock n'était pas un méchant homme, mais il ne souffrait aucun délai de\r
la part de ceux qui l'entouraient.\r
\r
*** END OF THE PROJECT GUTENBERG EBOOK VOYAGE ***\r
Licence.\r
"""


class TestImport(unittest.TestCase):

    def test_clean_and_chapters(self):
        text = book.clean(GUTENBERG)
        self.assertNotIn("Gutenberg", text)
        self.assertNotIn("[1]", text)
        self.assertNotIn("_", text)
        chapters = book.chapters(text)
        self.assertEqual([c["title"] for c in chapters], ["I", "II"])
        self.assertEqual(len(chapters[0]["paragraphs"]), 2)
        self.assertTrue(chapters[0]["paragraphs"][0].endswith("vers sa petite maison."))

    def test_headings_and_html(self):
        html = ("<html><body><h2>CHAPITRE PREMIER</h2><p>Il pleuvait.<sup>2</sup></p>"
                "<h2>Chapitre 2</h2><p>" + "Il neigeait beaucoup. " * 12 + "</p></body></html>")
        text = book.clean(html)
        self.assertNotIn("<", text)
        self.assertEqual(book.chapters(text, book.DEFAULT_CHAPTER_RULE)[-1]["chapter"], 1)
        self.assertEqual(book.chapters("Une seule page, sans titre.")[0]["paragraphs"],
                         ["Une seule page, sans titre."])
        with self.assertRaises(ValueError):
            book.chapters("\n\n")

    def test_a_roman_looking_word_is_not_a_heading(self):
        self.assertEqual(len(book.chapters("CIVIL\n\nMIDI\n\nIl pleuvait.")), 1)

    def test_latin1(self):
        self.assertEqual(book.decode("été".encode("cp1252")), "été")


class TestSegmentation(unittest.TestCase):

    def test_sentences(self):
        self.assertEqual(
            segment.sentences("M. Lidenbrock et J. Verne partirent. « Venez ! » dit-il. "
                              "Attendez… Quoi ? Rien !"),
            ["M. Lidenbrock et J. Verne partirent.", "« Venez ! » dit-il.", "Attendez…",
             "Quoi ?", "Rien !"])

    def test_closing_quote_stays_with_its_sentence(self):
        self.assertEqual(segment.sentences("« Suis-moi ! » Je restai là."),
                         ["« Suis-moi ! »", "Je restai là."])

    def test_text_is_never_altered(self):
        chapters = book.chapters(book.clean(GUTENBERG))
        for chapter in chapters:
            said = " ".join(s["text"] for s in segment.segment_chapter(chapter))
            self.assertEqual(said, " ".join(chapter["paragraphs"]))

    def test_numbers_run_through_the_book_and_paragraphs_stay_apart(self):
        parts = segment.segment_book(book.chapters(book.clean(GUTENBERG)))
        numbers = [s["seg"] for part in parts for s in part]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertEqual({s["paragraph"] for s in parts[0]}, {1, 2})
        self.assertEqual(parts[1][0]["kind"], "dialogue")
        self.assertTrue(all(len(s["text"]) <= segment.HARD_MAX_CHARS for p in parts for s in p))

    def test_long_sentence_is_cut(self):
        sentence = "a " * 120 + "; " + "b " * 120
        cut = segment._cut_long(sentence.strip() + ".")
        self.assertEqual(len(cut), 2)
        self.assertTrue(cut[0].endswith(";"))


SHEET = [{"id": "lidenbrock", "name": "Lidenbrock", "aliases": ["mon oncle"],
          "description": "Un savant pressé.", "voice": "m2"}]


def annotation(seg, **extra):
    return {"seg": seg, "speaker": "narrator", "emotion": "calm", "energy": "medium",
            "gesture": None, "pause_after_s": 0.3, **extra}


class FakeClient:
    provider, model = "fake", "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, messages, temperature, schema=None, max_tokens=None):
        self.calls.append((messages, schema))
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)


SEGMENTS = [{"seg": n, "chapter": 1, "paragraph": 1, "kind": "narration", "text": f"Phrase {n}."}
            for n in range(1, 7)]
SPEAKERS = ["narrator", "lidenbrock"]


class TestAnnotate(unittest.TestCase):

    def errors(self, annotations, previous=None):
        return annotate.director_errors({"segments": annotations}, SEGMENTS, SPEAKERS, previous)

    def test_rules(self):
        good = [annotation(n) for n in range(1, 7)]
        self.assertEqual(self.errors(good), [])
        self.assertTrue(self.errors(good[:-1]))                          # missing
        self.assertTrue(self.errors([good[1], good[0], *good[2:]]))      # reordered
        self.assertTrue(self.errors(good + [annotation(7)]))             # added
        for bad in ({"text": "réécrit"}, {"speech": "x"}, {"speaker": "inconnu"},
                    {"emotion": "happy"}, {"pause_after_s": 9}):
            self.assertTrue(self.errors([annotation(1, **bad), *good[1:]]), bad)

    def test_gesture_spacing(self):
        gesture = {"emotion": "joyful", "intensity": 0.5}
        spaced = [annotation(n, gesture=gesture if n in (1, 5) else None) for n in range(1, 7)]
        self.assertEqual(self.errors(spaced), [])
        close = [annotation(n, gesture=gesture if n in (1, 3) else None) for n in range(1, 7)]
        self.assertTrue(self.errors(close))
        self.assertTrue(self.errors(spaced, previous=-1))   # too close to the block before

    def test_retry_once_with_the_errors(self):
        client = FakeClient("pas du JSON", {"summary": "s", "segments": [annotation(n) for n in range(1, 7)]})
        result = annotate.direct_chapter(client, "Voyage", SHEET, SEGMENTS, log=lambda *_: None)
        self.assertEqual(len(result["segments"]), 6)
        self.assertIn("Réponse refusée", client.calls[1][0][-1]["content"])
        self.assertIn("lidenbrock", client.calls[0][1]["properties"]["segments"]["items"]
                      ["properties"]["speaker"]["enum"])
        with self.assertRaises(annotate.AnnotationError):
            annotate.direct_chapter(FakeClient("x", "y"), "Voyage", SHEET, SEGMENTS,
                                    log=lambda *_: None)

    def test_long_chapter_in_blocks(self):
        segments = [{**SEGMENTS[0], "seg": n} for n in range(1, annotate.BLOCK_SEGMENTS + 3)]
        first = segments[:annotate.BLOCK_SEGMENTS]
        client = FakeClient(
            {"summary": "A", "segments": [annotation(s["seg"]) for s in first]},
            {"summary": "B", "segments": [annotation(s["seg"]) for s in segments[len(first):]]})
        result = annotate.direct_chapter(client, "V", SHEET, segments, log=lambda *_: None)
        self.assertEqual((result["summary"], len(result["segments"])), ("A B", len(segments)))
        self.assertIn("Derniers segments", client.calls[1][0][1]["content"])

    def test_known_characters_are_kept(self):
        self.assertEqual(annotate.character_errors({"characters": SHEET}, SHEET), [])
        self.assertTrue(annotate.character_errors({"characters": []}, SHEET))
        self.assertTrue(annotate.character_errors(
            {"characters": [{**SHEET[0], "voice": "m9"}]}, []))
        self.assertTrue(annotate.character_errors(
            {"characters": [{**SHEET[0], "voice": "m1"}]}, SHEET))

    def test_merge_gives_voice_slots(self):
        entries = annotate.merge(SEGMENTS[:2], [annotation(1, speaker="lidenbrock"), annotation(2)],
                                 SHEET)
        self.assertEqual([e["speaker"] for e in entries], ["m2", "narrator"])
        self.assertEqual(entries[0]["text"], "Phrase 1.")
        self.assertNotIn("gesture", entries[0])

    def test_brain_env(self):
        env = {"BRAIN_PROVIDER": "ollama", "BRAIN_URL": "http://o", "BRAIN_MODEL": "small",
               "BRAIN_API_KEY": "k", "BRAIN_TIMEOUT": "60"}
        self.assertEqual(annotate.brain_env({**env, "STORY_BRAIN_MODEL": "big"}),
                         {"BRAIN_PROVIDER": "ollama", "BRAIN_URL": "http://o", "BRAIN_MODEL": "big",
                          "BRAIN_API_KEY": "k", "BRAIN_TIMEOUT": "600"})
        other = annotate.brain_env({**env, "STORY_BRAIN_PROVIDER": "anthropic",
                                    "STORY_BRAIN_TIMEOUT": "900"})
        self.assertEqual(other, {"BRAIN_PROVIDER": "anthropic", "BRAIN_TIMEOUT": "900"})


class TestPrepare(unittest.TestCase):

    def test_chapter_to_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = pathlib.Path(tmp, "v.txt"), pathlib.Path(tmp, "story")
            source.write_text(GUTENBERG, encoding="utf-8")
            count = len(segment.segment_book(book.chapters(book.clean(GUTENBERG)))[0])
            client = FakeClient({"characters": SHEET},
                                {"summary": "s", "segments": [annotation(n) for n in range(1, count + 1)]})
            prepare.prepare(source, out, "Voyage", "Jules Verne", 1864, client=client,
                            log=lambda *_: None)
            header, entries = prerecording.load_story(out)
            self.assertEqual((header["chapters"], header["year"], len(entries)), (2, 1864, count))
            self.assertTrue(os.path.exists(out / "chapters" / "01.segments.json"))
            with self.assertRaises(ValueError):
                prepare.prepare(source, out, "V", "J", None, chapter=3, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
