"""Tests sans réseau de speech_services (durée d'un MP3).

    cd robotics/shared && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speech_services  # noqa: E402


class TestVoice(unittest.TestCase):

    def test_say_options(self):
        data = '{"text": "a", "voice": "fr-FR-HenriNeural", "rate": "-5%", "pitch": "+2Hz"}'
        self.assertEqual(speech_services.say_options(data),
                         {"voice": "fr-FR-HenriNeural", "rate": "-5%", "pitch": "+2Hz"})
        for data in ("bonjour", "", "[1]", '{"text": "a", "sync": "s-1"}',
                     '{"text": "a", "voice": "male", "rate": "fast", "pitch": 2}'):
            self.assertEqual(speech_services.say_options(data), {}, data)

    def test_say_text_never_reads_the_json_aloud(self):
        self.assertEqual(speech_services.say_text('{"text": "a", "voice": "male"}'), "a")
        for data in ("bonjour", "42", '{"x": 1}'):
            self.assertEqual(speech_services.say_text(data), data)

    def test_failed_voice_falls_back_to_the_default(self):
        calls = []

        def synth(text, voice, **prosody):
            calls.append((voice, prosody))
            if voice != "default":
                raise RuntimeError("no audio")
            return b"mp3"

        with mock.patch.object(speech_services, "_synthesize", synth):
            options = {"voice": "fr-FR-HenriNeural", "rate": "-5%"}
            self.assertEqual(speech_services.synthesize_mp3("a", "default", options), b"mp3")
        self.assertEqual(calls, [("fr-FR-HenriNeural", {"rate": "-5%"}), ("default", {})])


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
