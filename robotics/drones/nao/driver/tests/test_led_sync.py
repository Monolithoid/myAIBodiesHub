"""Tests without ROS 2 of the LEDs held until their sentence starts (led_sync.py).

    cd robotics/drones/nao/driver && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from led_sync import LedSync  # noqa: E402

RED = {"group": "AllLeds", "r": 1.0, "g": 0.0, "b": 0.0, "duration": 0.5}
BLUE = {"group": "AllLeds", "r": 0.0, "g": 0.0, "b": 1.0, "duration": 0.5}


class TestLedSync(unittest.TestCase):

    def test_held_until_the_go_of_its_sentence(self):
        s = LedSync()
        s.hold("s-1", RED, now=0)
        self.assertIsNone(s.go({"kind": "speech", "sync": "s-1", "speech_s": 3}, now=1))
        self.assertIsNone(s.go({"kind": "go", "sync": "s-2", "in_s": 0}, now=1))
        self.assertEqual(s.go({"kind": "go", "sync": "s-1", "at": 12.4, "in_s": 0.4}, now=5),
                         (RED, 0.4))
        self.assertEqual(len(s), 0)
        self.assertIsNone(s.go({"kind": "go", "sync": "s-1", "in_s": 0}, now=6))   # once

    def test_refused_gesture_lights_at_once(self):
        s = LedSync()
        s.hold("s-1", RED, now=0)
        self.assertEqual(s.go({"kind": "go", "sync": "s-1", "at": 0, "reason": "x"}, now=1), (RED, 0.0))

    def test_latest_order_per_sentence(self):
        s = LedSync()
        s.hold("s-1", RED, now=0)
        s.hold("s-1", BLUE, now=0)
        self.assertEqual(s.go({"kind": "go", "sync": "s-1"}, now=1)[0], BLUE)

    def test_shown_anyway_without_a_go(self):
        s = LedSync(max_wait_s=20)
        s.hold("s-1", RED, now=0)
        s.hold("s-2", BLUE, now=15)
        self.assertEqual(s.expired(now=19), [])
        self.assertEqual(s.expired(now=20), [RED])
        self.assertEqual(s.expired(now=40), [BLUE])
        self.assertEqual(len(s), 0)

    def test_delay_bounded(self):
        s = LedSync(max_wait_s=20)
        s.hold("s-1", RED, now=0)
        self.assertEqual(s.go({"kind": "go", "sync": "s-1", "in_s": 99}, now=1)[1], 20.0)


if __name__ == "__main__":
    unittest.main()
