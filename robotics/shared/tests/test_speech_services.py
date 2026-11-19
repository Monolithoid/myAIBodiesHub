"""Tests sans réseau de speech_services (durée d'un MP3).

    cd robotics/shared && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speech_services  # noqa: E402


def _frames(n, version=2, bitrate_idx=6, rate_idx=1):
    """n trames MPEG Layer III vides (par défaut MPEG-2, 48 kbit/s, 24 kHz, comme edge-tts)."""
    h = (0x7FF << 21) | (version << 19) | (1 << 17) | (1 << 16) | (bitrate_idx << 12) | (rate_idx << 10)
    length = (72 if version != 3 else 144) * 48000 // 24000
    return (h.to_bytes(4, "big") + b"\0" * (length - 4)) * n


class TestMp3Duration(unittest.TestCase):

    def test_edge_tts_format(self):
        self.assertAlmostEqual(speech_services.mp3_duration(_frames(125)), 125 * 576 / 24000.0)

    def test_id3_tag_skipped(self):
        tag = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\xff" * 10
        self.assertAlmostEqual(speech_services.mp3_duration(tag + _frames(10)), 0.24)

    def test_not_mp3(self):
        self.assertIsNone(speech_services.mp3_duration(b""))
        self.assertIsNone(speech_services.mp3_duration(b"pas un mp3 du tout"))


if __name__ == "__main__":
    unittest.main()
