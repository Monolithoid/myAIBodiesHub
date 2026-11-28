"""Tests of the story player with fake bodies and a fake game-master.

    cd robotics/regie && python3 -m unittest discover -s tests -v
"""

import pathlib
import sys
import tempfile
import threading
import time
import unittest

REGIE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REGIE))

from storyteller import player, prerecording, web  # noqa: E402

HEADER = {"title": "Voyage", "author": "Jules Verne", "year": 1864, "chapters": 3}


def passage(seg, chapter, **extra):
    return {"seg": seg, "chapter": chapter, "text": f"t{seg}", "emotion": "calm",
            "pause_after_s": 0, **extra}


# Chapter 1: t1-t3, chapter 2: t4-t6, chapter 3: t7.
ENTRIES = [passage(n, (n + 2) // 3) for n in range(1, 8)]


class Bodies:
    """A voice: sentences queue, each is "spoken" until finish() is called."""

    def __init__(self):
        self.voices = False
        self._queue = 0
        self.said = []
        self.orders = []
        self.fail = False
        self.busy = 0
        self._speaking = False
        self._cond = threading.Condition()

    def perform(self, order):
        with self._cond:
            if self.fail:
                raise OSError("down")
            if self.busy:
                self.busy -= 1
                return None
            self.orders.append(order)
            self.said.append(order["actions"][0]["text"])
            if self._speaking:
                self._queue += 1
            self._speaking = True
            self._cond.notify_all()
            return {"voices": self.voices}

    def speaking(self):
        with self._cond:
            return self._speaking, self._queue == 0

    def wait(self, count):
        with self._cond:
            if not self._cond.wait_for(lambda: len(self.said) >= count, timeout=3):
                raise AssertionError(f"only said {self.said}")
        return self.said[count - 1]

    def finish(self):
        """The sentence being said ends; a queued one starts."""
        with self._cond:
            if self._queue:
                self._queue -= 1
            else:
                self._speaking = False


class Activity:
    def __init__(self):
        self.running = False
        self.refused = False
        self.releases = 0
        self.views = []

    def take(self, view):
        if self.refused:
            raise player.PlayerError("Une partie est déjà en cours")
        self.running = True
        self.views.append(view)

    def update(self, view):
        self.views.append(view)

    def release(self):
        self.running = False
        self.releases += 1

    def held(self):
        return self.running


def until(check, message="condition not reached"):
    end = time.monotonic() + 3
    while not check():
        if time.monotonic() > end:
            raise AssertionError(message)
        time.sleep(0.005)


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.write("voyage", HEADER, ENTRIES)
        self.bodies, self.activity = Bodies(), Activity()
        self.player = self.make()
        self.addCleanup(self.end)

    def write(self, name, header, entries):
        (self.root / name).mkdir()
        (self.root / name / "01.script.jsonl").write_text(prerecording.dumps(header, entries))

    def make(self):
        made = player.Player(player.Library(self.root),
                             player.Bookmarks(str(self.root / "bookmarks.db")),
                             self.bodies, self.activity)
        made.SPEAKING_POLL_S = made.BUSY_RETRY_S = 0.005
        return made

    def end(self):
        self.bodies.fail = True
        self.player.stop()
        self.bodies._queue = 0
        self.bodies.finish()
        if self.player._worker:
            self.player._worker.join(3)

    def hear(self, *texts):
        """Lets the sentence being said end, then expects the next ones, one by one."""
        for text in texts:
            count = len(self.bodies.said)
            self.bodies.finish()
            self.assertEqual(self.bodies.wait(count + 1), text)

    def start(self):
        self.player.play("voyage")
        self.assertEqual(self.bodies.wait(1), "Je vous lis Voyage, de Jules Verne, chapitre 1.")

    def finish_to(self, status):
        self.bodies.finish()
        until(lambda: self.player.state()["status"] == status, f"not {status}")


class TestReading(Base):

    def test_whole_story_in_order_then_activity_given_back(self):
        self.start()
        self.assertTrue(self.activity.running)
        self.hear("t1", "t2", "t3", "Chapitre 2.", "t4", "t5", "t6", "Chapitre 3.", "t7")
        self.finish_to("stopped")
        until(lambda: not self.activity.running)
        self.assertEqual([v["chapter"] for v in self.activity.views], [1, 2, 3])
        self.assertEqual(self.player.state()["seg"], 1)     # next time from the start

    def test_perform_order(self):
        self.write("g", HEADER, [passage(1, 1, emotion="angry", energy="high",
                                         gesture={"emotion": "angry", "intensity": 0.7})])
        self.player.play("g")
        self.bodies.wait(1)
        self.hear("t1")
        self.assertEqual(self.bodies.orders[1], {
            "source": "story", "emotion": "angry", "energy": "high",
            "actions": [{"type": "speech", "text": "t1"},
                        {"type": "gesture", "emotion": "angry", "intensity": 0.7}]})

    def test_a_character_has_its_voice_the_narrator_none(self):
        header = {**HEADER, "voices": {"m1": "fr-CA-JeanNeural"}}
        self.write("v", header, [passage(1, 1, speaker="m1"), passage(2, 1, speaker="narrator")])
        self.player.play("v")
        self.bodies.wait(1)
        self.hear("t1", "t2")
        self.assertEqual(self.bodies.orders[1]["actions"][0], {
            "type": "speech", "text": "t1", "voice": "fr-CA-JeanNeural",
            "rate": "+0%", "pitch": "+0Hz"})
        self.assertEqual(self.bodies.orders[0]["actions"][0].keys(), {"type", "text"})
        self.assertEqual(self.bodies.orders[2]["actions"][0].keys(), {"type", "text"})

    def test_no_edit_while_reading(self):
        self.start()
        with self.assertRaises(player.PlayerError):
            self.player.edit(self.player.state()["story"], lambda: self.fail("edited"))
        self.assertEqual(self.player.edit("other", lambda: 1), 1)

    def test_pause_waits_for_the_end_of_the_passage(self):
        self.start()
        self.hear("t1")
        state = self.player.pause()
        self.assertEqual((state["status"], state["pending"]["command"]), ("playing", "paused"))
        self.finish_to("paused")
        time.sleep(0.05)
        self.assertEqual(len(self.bodies.said), 2)
        self.assertTrue(self.activity.running)              # no game can start meanwhile
        self.player.play()
        self.assertEqual(self.bodies.wait(3), "t2")         # no resume sentence

    def test_play_withdraws_a_pending_pause(self):
        self.start()
        self.player.pause()
        self.assertIsNone(self.player.play()["pending"])
        self.hear("t1", "t2")

    def test_pause_during_the_resume_sentence(self):
        self.start()
        self.assertEqual(self.player.pause()["status"], "playing")
        self.finish_to("paused")
        self.player.play()
        self.assertEqual(self.bodies.wait(2), "t1")

    def test_stop_waits_then_bookmarks_and_releases(self):
        self.start()
        self.hear("t1", "t2")
        self.assertEqual(self.player.stop()["status"], "playing")
        self.finish_to("stopped")
        until(lambda: not self.activity.running)
        self.player._worker.join(3)
        self.player = self.make()                            # a restart keeps the bookmark
        self.player.play("voyage")
        self.assertEqual(self.bodies.wait(4), "Je vous lis Voyage, de Jules Verne, chapitre 1.")
        self.hear("t3")

    def test_seeks_add_up_and_apply_after_the_passage(self):
        self.start()
        self.hear("t1")
        for _ in range(3):
            state = self.player.seek("passage", 1)
        self.assertEqual(state["pending"]["target"], {"chapter": 2, "passage": 1})
        self.assertEqual(state["text"], "t1")                # still being said
        self.hear("Chapitre 2.", "t4")

    def test_no_move_forward_at_the_end_does_not_repeat(self):
        self.player.seek("chapter", 1, "voyage")
        self.player.seek("chapter", 1)
        self.player.play("voyage")
        self.bodies.wait(1)
        self.hear("t7")
        self.assertIsNone(self.player.seek("chapter", 1)["pending"])
        self.assertIsNone(self.player.seek("passage", 1)["pending"])
        self.finish_to("stopped")
        self.assertEqual(self.bodies.said.count("t7"), 1)

    def test_only_whole_deltas(self):
        for delta in (1.0, True, 2):
            with self.assertRaises(player.PlayerError):
                self.player.seek("passage", delta, "voyage")

    def test_lost_answer_of_game_master_gives_the_activity_back(self):
        def take(view):
            self.activity.running = True
            raise player.PlayerError("game-master injoignable")
        self.activity.take = take
        with self.assertRaises(player.PlayerError):
            self.player.play("voyage")
        self.assertFalse(self.activity.running)

    def test_replay_and_chapter_seeks(self):
        self.start()
        self.hear("t1", "t2")
        self.player.seek("passage", -1)
        self.hear("t1", "t2")
        self.player.seek("chapter", -1)                      # start of this chapter
        self.hear("t1")
        self.player.seek("chapter", 1)
        self.player.seek("chapter", 1)
        self.hear("Chapitre 3.", "t7")
        self.player.seek("chapter", -1)                      # at a chapter start: the previous one
        self.hear("Chapitre 2.", "t4")

    def test_seek_while_paused_moves_at_once(self):
        self.start()
        self.player.pause()
        self.finish_to("paused")
        state = self.player.seek("chapter", 1)
        self.assertEqual((state["seg"], state["pending"]), (4, None))
        self.player.play()
        self.assertEqual(self.bodies.wait(2), "Chapitre 2.")

    def test_seek_while_stopped_moves_the_bookmark(self):
        state = self.player.seek("chapter", 1, "voyage")
        self.assertEqual((state["status"], state["seg"]), ("stopped", 4))
        self.assertEqual(self.player.seek("passage", 1, "voyage")["seg"], 5)
        self.assertEqual(self.make().state("voyage")["seg"], 5)
        self.assertEqual(self.bodies.said, [])

    def test_seek_limits(self):
        story = player.Library(self.root).load("voyage")
        self.assertEqual(story.seek(0, "passage", -1), 0)
        self.assertEqual(story.seek(6, "passage", 1), 6)
        self.assertEqual(story.seek(0, "chapter", -1), 0)
        self.assertEqual(story.seek(6, "chapter", 1), 6)
        self.assertEqual(story.seek(4, "chapter", -1), 3)

    def test_stop_while_stopped_frees_a_stuck_activity(self):
        self.activity.running = True
        self.player.stop()
        self.assertFalse(self.activity.running)

    def test_stop_while_paused(self):
        self.start()
        self.player.pause()
        self.finish_to("paused")
        self.assertEqual(self.player.stop()["status"], "stopped")
        until(lambda: not self.activity.running)

    def test_refused_activity(self):
        self.activity.refused = True
        with self.assertRaisesRegex(player.PlayerError, "déjà en cours"):
            self.player.play("voyage")
        self.assertEqual(self.player.state()["status"], "stopped")
        self.assertEqual(self.bodies.said, [])

    def test_errors_give_the_activity_back(self):
        self.start()
        self.hear("t1")
        self.bodies.fail = True
        self.finish_to("stopped")
        until(lambda: not self.activity.running)
        state = self.player.state()
        self.assertIn("injoignable", state["error"])
        self.assertEqual(state["seg"], 2)                    # t2 was not heard

    def test_unexpected_error_gives_the_activity_back(self):
        self.bodies.perform = lambda order: 1 / 0
        self.player.play("voyage")
        until(lambda: self.activity.releases == 1)
        self.assertIn("ZeroDivisionError", self.player.state()["error"])

    def test_busy_body_gateway_is_retried(self):
        self.bodies.busy = 3
        self.start()

    def test_activity_taken_away_stops_the_reading(self):
        self.start()
        self.activity.running = False                        # "!stop"
        self.finish_to("stopped")
        self.assertEqual(self.activity.releases, 0)
        self.assertEqual(len(self.bodies.said), 1)

    def test_unknown_story(self):
        for story in ("", "nope", "../voyage"):
            with self.assertRaises(player.PlayerError):
                self.player.play(story)

    def test_sounds_are_skipped_and_files_listed(self):
        self.write("s", HEADER, [{"sound": "x", "theme": "vent"}, passage(1, 1)])
        self.assertEqual({s["id"]: s["passages"] for s in self.player.library.list()},
                         {"s": 1, "voyage": 7})


class TestSentAhead(Base):
    """Voice bodies connected: the next passage leaves while the current one is said."""

    def setUp(self):
        super().setUp()
        self.bodies.voices = True

    def begin(self):
        """The resume sentence, then t1 with t2 already sent behind it."""
        self.start()
        self.hear("t1")
        self.bodies.wait(3)
        self.assertEqual(self.bodies.said[2], "t2")

    def heard(self, count):
        """The current sentence ends; waits for `count` sentences sent in all."""
        self.bodies.finish()
        self.bodies.wait(count)

    def test_chain_and_bookmark(self):
        self.begin()
        self.assertEqual(self.player.state()["seg"], 1)
        self.heard(4)                                    # t2 starts, t3 leaves
        until(lambda: self.player.state()["seg"] == 2)
        self.assertEqual(self.bodies.said[1:], ["t1", "t2", "t3"])
        self.assertEqual(player.Bookmarks(str(self.root / "bookmarks.db"))
                         .index(self.player.library.load("voyage")), 1)

    def test_nothing_ahead_across_a_chapter_then_the_end(self):
        self.begin()
        self.heard(4)
        self.bodies.finish()                             # t3 starts, chapter 2 follows
        time.sleep(0.05)
        self.assertEqual(len(self.bodies.said), 4)
        self.heard(5)
        self.assertEqual(self.bodies.said[4], "Chapitre 2.")

    def test_late_pause_waits_one_more_passage(self):
        self.begin()
        self.assertEqual(self.player.pause()["pending"]["command"], "paused")
        self.bodies.finish()                             # t2, already sent, is said
        until(lambda: self.player.state()["seg"] == 2)
        self.assertEqual(self.player.state()["status"], "playing")
        self.finish_to("paused")
        self.assertEqual(len(self.bodies.said), 3)       # nothing sent behind a command
        self.assertEqual(self.player.state()["seg"], 3)

    def test_late_next_does_not_repeat_the_passage_sent(self):
        self.begin()
        self.player.seek("passage", 1)
        self.heard(4)
        self.assertEqual(self.bodies.said[1:], ["t1", "t2", "t3"])
        self.assertIsNone(self.player.state()["pending"])

    def test_late_seek_applies_after_the_passage_sent(self):
        self.begin()
        self.player.seek("chapter", 1)
        self.bodies.finish()
        until(lambda: self.player.state()["seg"] == 2)
        self.heard(4)
        self.assertEqual(self.bodies.said[3], "Chapitre 2.")

    def test_command_before_the_next_one_is_sent(self):
        self.start()
        self.player.pause()                              # during the resume sentence
        self.finish_to("paused")
        self.player.play()
        self.bodies.wait(3)                              # t1, then t2 behind it
        self.assertEqual(self.bodies.said[1:], ["t1", "t2"])

    def test_long_pause_and_timed_text_are_not_sent_ahead(self):
        self.write("p", HEADER, [passage(1, 1, pause_after_s=1.5), passage(2, 1),
                                 passage(3, 1, at_ms=0)])
        self.player.play("p")
        self.bodies.wait(1)
        self.hear("t1")
        time.sleep(0.05)
        self.assertEqual(len(self.bodies.said), 2)
        self.hear("t2")
        time.sleep(0.05)
        self.assertEqual(len(self.bodies.said), 3)

    def test_gesture_is_not_sent_ahead(self):
        self.write("g", HEADER, [passage(1, 1), passage(2, 1, gesture={"emotion": "calm",
                                                                      "intensity": 0.5})])
        self.player.play("g")
        self.bodies.wait(1)
        self.hear("t1")
        time.sleep(0.05)
        self.assertEqual(len(self.bodies.said), 2)
        self.hear("t2")

    def test_only_the_early_sentence_says_ahead(self):
        self.begin()
        self.assertEqual([o.get("ahead", False) for o in self.bodies.orders], [False, False, True])

    def test_refused_ahead_is_said_afterwards(self):
        perform = self.bodies.perform
        self.bodies.perform = lambda order: None if order.get("ahead") else perform(order)
        self.start()
        self.hear("t1")
        time.sleep(0.05)
        self.assertEqual(len(self.bodies.said), 2)
        self.hear("t2")


class TestTimed(Base):

    def test_waits_for_the_instant_and_pauses_the_clock(self):
        self.write("timed", {**HEADER, "timed": True},
                   [passage(1, 1, at_ms=0), passage(2, 1, at_ms=300)])
        self.player.play("timed")
        self.bodies.wait(1)
        started = time.monotonic()
        self.hear("t1")
        self.bodies.finish()
        time.sleep(0.05)
        self.player.pause()
        time.sleep(0.4)
        self.assertEqual(len(self.bodies.said), 2)           # the clock stood still
        self.player.play()
        self.assertEqual(self.bodies.wait(3), "t2")
        self.assertGreater(time.monotonic() - started, 0.65)

    def test_seek_moves_the_clock(self):
        self.write("timed", {**HEADER, "timed": True},
                   [passage(1, 1, at_ms=0), passage(2, 1, at_ms=60000), passage(3, 1, at_ms=90000)])
        self.player.play("timed")
        self.bodies.wait(1)
        self.hear("t1")
        self.bodies.finish()
        until(lambda: self.player.state()["seg"] == 2 and not self.player._saying)
        self.player.seek("passage", 1)
        self.assertEqual(self.bodies.wait(3), "t3")

    def test_seek_to_an_untimed_passage_takes_the_instant_before(self):
        self.write("timed", {**HEADER, "timed": True},
                   [passage(1, 1, at_ms=0), passage(2, 1, at_ms=60000), passage(3, 1),
                    passage(4, 1, at_ms=60100)])
        self.player.seek("passage", 1, "timed")
        self.player.seek("passage", 1, "timed")
        self.player.play("timed")
        self.bodies.wait(1)
        self.hear("t3", "t4")

    def test_clock_starts_over_without_an_instant_before(self):
        self.write("a", {**HEADER, "timed": True}, [passage(1, 1, at_ms=60000)])
        self.write("b", {**HEADER, "timed": True}, [passage(1, 1), passage(2, 1, at_ms=100)])
        self.player.clock.start(60000)
        self.player.play("b")
        self.bodies.wait(1)
        self.hear("t1", "t2")
        self.assertLess(self.player.state()["lag_ms"], 1000)

    def test_clock(self):
        now = [10.0]
        clock = player.Clock(lambda: now[0])
        clock.start(500)
        now[0] = 11.0
        clock.pause()
        now[0] = 20.0
        self.assertEqual(clock.ms(), 1500)
        clock.resume()
        now[0] = 21.0
        self.assertEqual(clock.ms(), 2500)


class TestRoutes(Base):

    def test_routes(self):
        routes = web.StoryRoutes(env={"STORIES_DIR": str(self.root)})
        routes._player = self.player
        self.assertEqual(routes.get("/stories/list", {})[1]["stories"][0]["id"], "voyage")
        code, state = routes.post("/stories/seek",
                                  {"story": "voyage", "unit": "passage", "delta": 1})
        self.assertEqual((code, state["seg"]), (200, 2))
        self.assertEqual(routes.post("/stories/seek", {"unit": "page", "delta": 1})[0], 409)
        self.assertEqual(routes.post("/stories/play", {"story": "voyage"})[1]["status"], "playing")
        self.assertEqual(routes.post("/stories/nope", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
