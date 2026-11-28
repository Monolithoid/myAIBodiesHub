"""Books of the stories page: import, description, preparation.  No network.

    cd robotics/regie && python3 -m unittest discover -s tests -v
"""

import base64
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest

REGIE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REGIE))

from storyteller import prerecording, web  # noqa: E402

CHAPTER = "Le professeur entra dans la maison sans dire un mot. " * 5
BOOK = f"I\n\n{CHAPTER}\n\nII\n\n{CHAPTER}\n"
META = {"title": "Voyage", "author": "Jules Verne", "year": 1864}


class FakeClient:
    provider, model = "fake", "fake"

    def __init__(self):
        self.hold = None      # a callable run before answering

    def chat(self, messages, temperature, schema=None, max_tokens=None):
        if self.hold:
            self.hold()
        if "characters" in schema["properties"]:
            return json.dumps({"characters": []})
        numbers = [int(line[1:line.index("]")]) for line in messages[1]["content"].splitlines()
                   if line.startswith("[")]
        return json.dumps({"summary": "s", "segments": [
            {"seg": n, "speaker": "narrator", "emotion": "calm", "energy": "medium",
             "gesture": None, "pause_after_s": 0.3, "sound": None} for n in numbers]})


class Base(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.client = FakeClient()
        self.routes = web.StoryRoutes({"STORIES_DIR": tmp.name}, llm=lambda: self.client)

    def post(self, path, **body):
        return self.routes.post("/stories/" + path, body)

    def add(self, text=BOOK, **extra):
        content = base64.b64encode(text.encode()).decode()
        return self.post("import", **{**META, "story": "voyage", "content": content,
                                      "filename": "../v.txt", **extra})

    def wait(self):
        end = time.monotonic() + 5
        while self.routes.preparation.state()["running"]:
            self.assertLess(time.monotonic(), end)
            time.sleep(0.01)
        return self.routes.preparation.state()

    def prepare(self, chapter=1, **extra):
        code, answer = self.post("prepare", story="voyage", chapter=chapter, **extra)
        if code == 200:
            self.assertEqual(self.wait()["error"], "")
        return code, answer


class TestImport(Base):

    def test_chapters_listed_then_prepared(self):
        code, answer = self.add()
        self.assertEqual(code, 200, answer)
        self.assertEqual([(c["chapter"], c["segments"], c["prepared"]) for c in answer["chapters"]],
                         [(1, 2, False), (2, 2, False)])
        self.assertEqual(json.loads((self.root / "voyage" / "book.json").read_text())["source"],
                         "v.txt")
        self.assertEqual(self.routes.get("/stories/list", {})[1], {"stories": []})
        self.assertEqual(self.prepare(2)[0], 200)
        _, book = self.routes.get("/stories/book", {"story": ["voyage"]})
        self.assertEqual([c["prepared"] for c in book["chapters"]], [False, True])
        self.assertEqual(self.routes.get("/stories/list", {})[1]["stories"][0]["passages"], 2)
        self.assertEqual(prerecording.load_story(self.root / "voyage")[1][0]["seg"], 3)

    def test_refused(self):
        for extra in ({"story": "../x"}, {"title": " "}, {"year": "1864"}, {"content": "??"},
                      {"content": ""}, {"rule": "("}):
            self.assertEqual(self.add(**extra)[0], 409, extra)
        self.assertEqual(list(self.root.glob("*/book.json")), [])

    def test_imported_again_only_before_the_first_script(self):
        self.add()
        code, answer = self.add(rule="^II$")
        self.assertEqual((code, len(answer["chapters"])), (200, 1))
        self.assertEqual(len(list((self.root / "voyage" / "chapters").glob("*.segments.json"))), 1)
        self.prepare()
        self.assertEqual(self.add()[0], 409)

    def test_description_corrected_in_the_scripts(self):
        self.add()
        self.prepare()
        code, answer = self.post("book", story="voyage", **{**META, "year": 1867})
        self.assertEqual((code, answer["year"]), (200, 1867))
        self.assertEqual(prerecording.load_story(self.root / "voyage")[0]["year"], 1867)
        self.assertEqual(self.post("book", story="inconnu", **META)[0], 409)


class TestPreparation(Base):

    def test_one_at_a_time_and_the_book_is_locked(self):
        self.add()
        release = threading.Event()
        self.client.hold = lambda: release.wait(5)
        self.assertEqual(self.post("prepare", story="voyage", chapter=1)[0], 200)
        self.assertEqual(self.post("prepare", story="voyage", chapter=2)[0], 409)
        self.assertEqual(self.post("book", story="voyage", **META)[0], 409)
        self.assertEqual(self.add()[0], 409)
        release.set()
        self.assertEqual(self.wait()["error"], "")

    def test_a_script_is_overwritten_only_when_asked(self):
        self.add()
        self.prepare()
        self.assertEqual(self.prepare()[0], 409)
        self.assertEqual(self.prepare(overwrite=True)[0], 200)
        for chapter in (0, 3, "1", None):
            self.assertEqual(self.prepare(chapter)[0], 409, chapter)

    def test_failure_reported(self):
        self.add()

        def fail():
            raise RuntimeError("quota")

        self.client.hold = fail
        self.post("prepare", story="voyage", chapter=1)
        state = self.wait()
        self.assertIn("quota", state["error"])
        self.assertFalse(pathlib.Path(self.root, "voyage", "chapters",
                                      "01.script.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
