"""Tests without network of the OBS voice's speech ↔ gesture sync (obs_agent.py).

    cd robotics/drones/obs && python3 -m unittest discover -s tests -v
"""

import base64
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import obs_agent as oa  # noqa: E402


class TestParse(unittest.TestCase):

    def test_say(self):
        self.assertEqual(oa.parse_say("bonjour"), "bonjour")
        self.assertEqual(oa.parse_say(json.dumps({"text": "bonjour", "sync": "s-1"})), "bonjour")

    def test_audio_out(self):
        b64 = base64.b64encode(b"mp3").decode()
        self.assertEqual(oa.parse_audio_out(b64), (b"mp3", ""))
        self.assertEqual(oa.parse_audio_out(json.dumps({"mp3": b64, "sync": "s-1"})), (b"mp3", "s-1"))
        self.assertEqual(oa.parse_audio_out("{bad"), (b"", ""))


class TestSpeechSync(unittest.TestCase):

    def setUp(self):
        self.now = [50.0]
        self.slept = []

    def sync(self, offset_s=0.0, max_wait_s=0.5):
        return oa.SpeechSync(max_wait_s, offset_s, clock=lambda: self.now[0], sleep=self.slept.append)

    def test_waits_in_s_plus_obs_offset(self):
        s = self.sync(offset_s=0.2)
        s.on_message({"kind": "go", "sync": "s-1", "at": 1234.0, "in_s": 1.5})
        self.assertIn("geste", s.wait("s-1"))
        self.assertEqual(len(self.slept), 1)
        self.assertAlmostEqual(self.slept[0], 1.7)

    def test_released_now(self):
        s = self.sync()
        s.on_message({"kind": "go", "sync": "s-1", "at": 0, "in_s": 0})
        s.wait("s-1")
        self.assertEqual(self.slept, [])

    def test_go_while_waiting(self):
        s = oa.SpeechSync(5.0)
        threading.Timer(0.05, s.on_message, args=({"kind": "go", "sync": "s-2", "in_s": 0},)).start()
        self.assertIn("geste", s.wait("s-2"))

    def test_timeout(self):
        self.assertIn("attente dépassée", oa.SpeechSync(0.05).wait("absent"))


if __name__ == "__main__":
    unittest.main()
