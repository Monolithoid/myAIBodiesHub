"""Tests sans réseau de la phrase retenue jusqu'au départ du geste (static_drone.py).

    cd robotics/drones/static/onboard && python3 -m unittest discover -s tests -v
"""

import base64
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import static_drone as sd  # noqa: E402


class TestParseAudioOut(unittest.TestCase):

    def test_plain_base64(self):
        self.assertEqual(sd.parse_audio_out(base64.b64encode(b"mp3").decode()), (b"mp3", ""))

    def test_json_with_sync(self):
        data = json.dumps({"mp3": base64.b64encode(b"mp3").decode(), "sync": "s-1", "speech_s": 2.0})
        self.assertEqual(sd.parse_audio_out(data), (b"mp3", "s-1"))

    def test_garbage(self):
        self.assertEqual(sd.parse_audio_out("{pas du json"), (b"", ""))
        self.assertEqual(sd.parse_audio_out(""), (b"", ""))


class TestSpeechSync(unittest.TestCase):

    def setUp(self):
        self.now = [1000.0]
        self.slept = []
        self.sync = sd.SpeechSync(0.5, clock=lambda: self.now[0], sleep=self.slept.append)

    def test_waits_until_the_gesture_starts(self):
        self.sync.on_message({"kind": "go", "sync": "s-1", "at": 1001.5})
        self.assertIn("signal", self.sync.wait("s-1"))
        self.assertEqual(self.slept, [1.5])

    def test_go_now(self):
        self.sync.on_message({"kind": "go", "sync": "s-1", "at": 0, "reason": "geste refusé"})
        self.sync.wait("s-1")
        self.assertEqual(self.slept, [])

    def test_go_arriving_while_waiting(self):
        sync = sd.SpeechSync(5.0)
        threading.Timer(0.05, sync.on_message, args=({"kind": "go", "sync": "s-2", "at": 0},)).start()
        self.assertIn("signal", sync.wait("s-2"))

    def test_timeout_says_it_anyway(self):
        sync = sd.SpeechSync(0.05)
        self.assertIn("attente dépassée", sync.wait("absent"))

    def test_other_messages_ignored(self):
        self.sync.on_message({"kind": "speech", "sync": "s-1", "speech_s": 2.0})
        self.assertEqual(self.sync._go, {})


if __name__ == "__main__":
    unittest.main()
