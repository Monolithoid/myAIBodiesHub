"""Tests of the sound bank, the sounds of a story and their reading.

    cd robotics/regie && python3 -m unittest discover -s tests -v
"""

import base64
import json
import pathlib
import sqlite3
import sys
import unittest

REGIE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REGIE))
sys.path.insert(0, str(REGIE / "tests"))

from storyteller import player, prerecording, sounds, web  # noqa: E402
from test_player import HEADER, Base, passage  # noqa: E402

MP3 = b"ID3" + b"\x00" * 20
SHOT = {"sound": "coup-de-feu", "theme": "coup de feu", "seg": 1, "chapter": 1}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class TestBank(Base):

    def setUp(self):
        super().setUp()
        self.routes = web.StoryRoutes(env={"STORIES_DIR": str(self.root)})
        self.routes._player = self.player
        self.write("s", HEADER, [passage(1, 1), SHOT, passage(2, 1)])

    def upload(self, name, data=MP3):
        return self.routes.post("/stories/sounds/upload", {"name": name, "content": b64(data)})

    def test_upload_listen_and_never_replace(self):
        code, answer = self.upload("Coup de feu.mp3")
        self.assertEqual((code, answer["file"], answer["bank"]),
                         (200, "coup-de-feu.mp3", ["coup-de-feu.mp3"]))
        self.assertEqual(self.upload("coup-de-feu")[0], 409)            # already there
        for bad in (b"not an mp3", b"", b"ID3" + b"x" * sounds.MAX_BYTES):
            self.assertEqual(self.upload("autre", bad)[0], 409)
        code, answer = self.routes.get("/stories/sounds/file", {"name": ["coup-de-feu.mp3"]})
        self.assertEqual((code, base64.b64decode(answer["content"])), (200, MP3))
        for name in ("../s/01.script.jsonl", "absent.mp3"):
            self.assertEqual(self.routes.get("/stories/sounds/file", {"name": [name]})[0], 409)
        # The bank is not a story.
        self.assertNotIn(sounds.BANK, [s["id"] for s in self.player.library.list()])

    def test_requested_sounds_suggested_then_assigned(self):
        listing = self.routes.get("/stories/sounds", {"story": ["s"]})[1]
        self.assertEqual(listing["sounds"], [{
            "chapter": 1, "seg": 1, "theme": "coup de feu", "file": "", "missing": False,
            "suggested": "", "before": "t1"}])
        self.upload("coup-de-feu")
        listing = self.routes.get("/stories/sounds", {"story": ["s"]})[1]
        self.assertEqual(listing["sounds"][0]["suggested"], "coup-de-feu.mp3")

        assign = {"story": "s", "chapter": 1, "seg": 1}
        code, listing = self.routes.post("/stories/sound", {**assign, "file": "coup-de-feu.mp3"})
        self.assertEqual((code, listing["sounds"][0]["file"]), (200, "coup-de-feu.mp3"))
        self.assertEqual(self.routes.post("/stories/sound", {**assign, "file": "absent.mp3"})[0],
                         409)
        self.assertEqual(self.routes.post("/stories/sound", {**assign, "seg": 2, "file": ""})[0],
                         409)
        self.assertEqual(self.routes.post("/stories/sound", {**assign, "file": ""})[1]
                         ["sounds"][0]["file"], "")
        code, listing = self.routes.post("/stories/sound", {**assign, "remove": True})
        self.assertEqual((code, listing["sounds"]), (200, []))
        _, entries = prerecording.load_story(self.root / "s")
        self.assertEqual([e["text"] for e in entries], ["t1", "t2"])


class TestReadingSounds(Base):

    def setUp(self):
        super().setUp()
        sounds.bank(self.root).mkdir()
        (sounds.bank(self.root) / "coup-de-feu.mp3").write_bytes(MP3)
        self.write("s", HEADER, [passage(1, 1, emotion="scared", energy="high"),
                                 {**SHOT, "file": "coup-de-feu.mp3"},
                                 {**SHOT, "seg": 2, "theme": "explosion"},       # no file
                                 passage(2, 1), passage(3, 1)])

    def test_played_like_a_sentence_with_its_caption(self):
        self.player.play("s")
        self.bodies.wait(1)
        self.hear("t1", "[coup de feu]", "t2")
        self.assertEqual(self.bodies.orders[2], {
            "source": "story", "emotion": "scared", "energy": "high",
            "actions": [{"type": "speech", "text": "[coup de feu]", "sound": b64(MP3)}]})

    def test_a_missing_file_is_skipped(self):
        (sounds.bank(self.root) / "coup-de-feu.mp3").unlink()
        self.player.play("s")
        self.bodies.wait(1)
        self.hear("t1", "t2")

    def test_bookmark_on_the_sound(self):
        self.assertEqual(self.player.seek("passage", 1, "s")["text"], "[coup de feu]")
        self.assertEqual(self.player.seek("passage", 1, "s")["text"], "t2")
        self.assertEqual(self.player.seek("passage", -1, "s")["text"], "[coup de feu]")
        self.assertEqual(self.make().state("s")["text"], "[coup de feu]")

    def test_bookmarks_written_before_the_sounds(self):
        path = self.root / "old.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE bookmarks (story TEXT PRIMARY KEY, chapter INTEGER,"
                   " seg INTEGER, updated REAL)")
        db.execute("INSERT INTO bookmarks VALUES ('s', 1, 2, 0)")
        db.commit()
        db.close()
        marks = player.Bookmarks(str(path))
        story = self.player.library.load("s")
        self.assertEqual(story.passages[marks.index(story)]["text"], "t2")
        marks.put(story, 1)
        self.assertEqual(marks.index(story), 1)


if __name__ == "__main__":
    unittest.main()
