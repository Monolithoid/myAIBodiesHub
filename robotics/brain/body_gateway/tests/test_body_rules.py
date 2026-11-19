"""Tests sans ROS 2 des règles de body_gateway (routage, mot de réveil, direction).

    cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import body_rules  # noqa: E402

REGISTRY = ["nao_01", "static_01"]


class TestRouting(unittest.TestCase):

    def test_body_type(self):
        self.assertEqual(body_rules.body_type("static_01"), "static")
        self.assertEqual(body_rules.body_type("nao_01"), "nao")
        self.assertEqual(body_rules.body_type("robot_x"), "nao")

    def test_speech_goes_to_connected_static(self):
        connected = {"nao_01": True, "static_01": True}
        action = {"body": "nao_01", "type": "speech", "text": "bonjour"}
        self.assertEqual(body_rules.route_action(action, REGISTRY, connected), "static_01")

    def test_speech_stays_on_nao_without_static(self):
        connected = {"nao_01": True, "static_01": False}
        for body in ("nao_01", "static_01"):
            action = {"body": body, "type": "speech", "text": "bonjour"}
            self.assertEqual(body_rules.route_action(action, REGISTRY, connected), "nao_01")

    def test_nao_only_registry_unchanged(self):
        action = {"body": "nao_01", "type": "speech", "text": "bonjour"}
        self.assertEqual(body_rules.route_action(action, ["nao_01"], {}), "nao_01")
        self.assertEqual(body_rules.route_action({"type": "leds"}, ["nao_01"], {}), "nao_01")

    def test_gestures_go_to_nao(self):
        connected = {"nao_01": True, "static_01": True}
        for kind in ("motion", "leds", "move", "look"):
            action = {"body": "static_01", "type": kind}
            self.assertEqual(body_rules.route_action(action, REGISTRY, connected), "nao_01")

    def test_speech_goes_to_every_connected_voice(self):
        registry = ["nao_01", "static_01", "obs_01"]
        action = {"body": "nao_01", "type": "speech", "text": "bonjour"}
        both = {"nao_01": True, "static_01": True, "obs_01": True}
        self.assertEqual(body_rules.route_targets(action, registry, both), ["static_01", "obs_01"])
        only_obs = {"nao_01": True, "static_01": False, "obs_01": True}
        self.assertEqual(body_rules.route_targets(action, registry, only_obs), ["obs_01"])
        self.assertEqual(body_rules.speech_bodies(registry, {}, "nao_01"), ["nao_01"])
        self.assertEqual(body_rules.route_targets({"body": "obs_01", "type": "leds"},
                                                  registry, both), ["nao_01"])
        self.assertEqual(body_rules.body_type("obs_01"), "obs")

    def test_unknown_body(self):
        self.assertIsNone(body_rules.route_action({"body": "x", "type": "speech"}, REGISTRY, {}))

    def test_describe_bodies(self):
        text = body_rules.describe_bodies(REGISTRY, "static_01")
        self.assertIn("- static_01 : parler, entendre, voir", text)
        self.assertIn("- nao_01 : gestes, tête, LEDs", text)
        self.assertIn("- nao_01 : parler, gestes", body_rules.describe_bodies(["nao_01"], "nao_01"))


class TestWakeWord(unittest.TestCase):

    def setUp(self):
        self.gate = body_rules.WakeWordGate("ok nao", window_s=10)

    def test_wake_word_and_question(self):
        self.assertEqual(self.gate.filter("s", "OK Nao, quelle heure est-il ?", now=0),
                         "quelle heure est-il ?")
        self.assertEqual(self.gate.filter("s", "Okay, Nao. Tu m'entends ?", now=0),
                         "Tu m'entends ?")

    def test_without_wake_word_ignored(self):
        self.assertIsNone(self.gate.filter("s", "Il fait beau aujourd'hui.", now=0))

    def test_lone_wake_word_opens_window(self):
        self.assertIsNone(self.gate.filter("s", "Ok Nao.", now=0))
        self.assertTrue(self.gate.armed("s", now=5))
        self.assertEqual(self.gate.filter("s", "Raconte une blague", now=5), "Raconte une blague")
        # Window consumed by that sentence.
        self.assertIsNone(self.gate.filter("s", "Et une autre", now=6))

    def test_window_expires(self):
        self.gate.filter("s", "ok nao", now=0)
        self.assertIsNone(self.gate.filter("s", "Raconte une blague", now=11))

    def test_aliases(self):
        gate = body_rules.WakeWordGate("ok nao", window_s=10, aliases=["ok now", "OK no"])
        self.assertEqual(gate.filter("s", "Ok, now. Quelle heure est-il ?", now=0),
                         "Quelle heure est-il ?")
        self.assertIsNone(gate.filter("s", "Okay no.", now=0))
        self.assertTrue(gate.armed("s", now=1))
        # Without aliases, a mishearing is ignored.
        self.assertIsNone(self.gate.filter("s", "Ok, now. Quelle heure est-il ?", now=0))

    def test_strip_after_local_wake(self):
        # Speech after a wake word detected on the drone: never ignored.
        self.assertEqual(self.gate.strip("Quelle heure est-il ?"), "Quelle heure est-il ?")
        # The pre-roll may keep "ok nao": cut only at the start.
        self.assertEqual(self.gate.strip("Ok Nao, quelle heure est-il ?"), "quelle heure est-il ?")
        self.assertEqual(self.gate.strip("Ok nao."), "")
        self.assertEqual(self.gate.strip("Dis bonjour, ok nao"), "Dis bonjour, ok nao")
        self.assertFalse(self.gate.armed("s", now=0))


class TestDirection(unittest.TestCase):

    def test_label(self):
        text = body_rules.motor_ranges_label(
            {"rarm": {"RShoulderPitch": [-0.5, 1.2]}, "head": {"HeadYaw": [-0.8, 0.8]},
             "larm": {"LElbowYaw": [-1.0, 1.0]}})
        self.assertEqual(text, "- tête :\n"
                               "  HeadYaw : min -0.80 = regarde à droite, milieu +0.00, max +0.80 = regarde à gauche\n"
                               "- bras gauche :\n"
                               "  LElbowYaw : min -1.00, milieu +0.00, max +1.00\n"
                               "- bras droit :\n"
                               "  RShoulderPitch : min -0.50 = bras levé, milieu +0.35, max +1.20 = bras baissé")

    def test_not_calibrated(self):
        self.assertIn("non calibré", body_rules.motor_ranges_label({}))
        self.assertIn("non calibré", body_rules.motor_ranges_label({"head": {}}))

class TestLearnedGestures(unittest.TestCase):

    def test_label(self):
        self.assertIn("indisponibles", body_rules.gestures_label([]))
        text = body_rules.gestures_label(["joie", "peur"])
        self.assertIn("joie, peur", text)
        self.assertIn('"gesture"', text)

    def test_order(self):
        self.assertEqual(body_rules.gesture_order({"emotion": "joie", "intensity": 1.4}, ["joie"]),
                         {"emotion": "joie", "intensity": 1.0})
        self.assertIsNone(body_rules.gesture_order({"emotion": "colère"}, ["joie"]))
        self.assertIsNone(body_rules.gesture_order({}, ["joie"]))
        # Sans intensité : celle de l'énergie de la réponse.
        self.assertEqual(body_rules.gesture_order({"emotion": "joie"}, ["joie"], "high")["intensity"], 0.7)
        self.assertEqual(body_rules.gesture_order({"emotion": "joie", "intensity": "x"}, [])["intensity"], 0.5)

    def test_event_from_voice(self):
        self.assertIsNone(body_rules.gesture_event(None))
        # Drone : positif à droite ; générateur : positif à gauche du robot.
        self.assertEqual(body_rules.gesture_event(30.0), {"azimuth_deg": -30.0, "elevation_deg": 0.0})
        self.assertEqual(body_rules.gesture_event(-20.0, 5.0)["azimuth_deg"], 25.0)
        self.assertEqual(body_rules.gesture_event(-170.0)["azimuth_deg"], 90.0)

    def test_nao_routes_gestures(self):
        self.assertEqual(body_rules.route_targets({"type": "gesture", "body": "nao_01"}, REGISTRY, {}),
                         ["nao_01"])


class TestSpeechGestureSync(unittest.TestCase):
    CONNECTED = {"nao_01": True, "static_01": True}
    ACTIONS = [{"body": "static_01", "type": "speech", "text": "Bonjour, je suis content de te voir."},
               {"body": "nao_01", "type": "gesture", "emotion": "joie", "intensity": 0.6}]

    def test_speech_seconds(self):
        self.assertEqual(body_rules.speech_seconds(""), 0.0)
        short = body_rules.speech_seconds("Oui.")
        long = body_rules.speech_seconds("Bonjour, je suis vraiment très content de te voir aujourd'hui.")
        self.assertLess(short, 1.0)
        self.assertTrue(3.0 < long < 6.0, long)

    def test_sync_when_room_speaker_and_gesture(self):
        sync = body_rules.speech_gesture_sync(self.ACTIONS, REGISTRY, self.CONNECTED, ["joie"], "s-1")
        self.assertEqual(sync["id"], "s-1")
        self.assertEqual(sync["speech_s"], sync["speech_s_first"])
        self.assertGreater(sync["speech_s"], 1.0)

    def test_several_sentences(self):
        actions = self.ACTIONS + [{"body": "static_01", "type": "speech", "text": "Et toi ?"}]
        sync = body_rules.speech_gesture_sync(actions, REGISTRY, self.CONNECTED, ["joie"], "s-1")
        self.assertGreater(sync["speech_s"], sync["speech_s_first"])

    def test_sync_with_obs_voice_only(self):
        registry = ["nao_01", "obs_01"]
        actions = [dict(self.ACTIONS[0], body="obs_01"), self.ACTIONS[1]]
        sync = body_rules.speech_gesture_sync(actions, registry, {"nao_01": True, "obs_01": True},
                                              ["joie"], "s-1")
        self.assertEqual(sync["id"], "s-1")

    def test_no_sync_without_gesture_or_static_voice(self):
        self.assertIsNone(body_rules.speech_gesture_sync(self.ACTIONS[:1], REGISTRY, self.CONNECTED, [], "s"))
        self.assertIsNone(body_rules.speech_gesture_sync(self.ACTIONS, REGISTRY, self.CONNECTED, ["peur"], "s"))
        self.assertIsNone(body_rules.speech_gesture_sync(
            self.ACTIONS, REGISTRY, {"nao_01": True, "static_01": False}, ["joie"], "s"))



class TestGesturesOnly(unittest.TestCase):
    """LLM_MOTION=0: NAO only moves through learned gestures."""

    def test_nao_label(self):
        text = body_rules.describe_bodies(REGISTRY, "static_01", motion=False)
        self.assertIn("nao_01 : gestes appris", text)
        self.assertNotIn("motion", text)

    def test_gestures_label(self):
        text = body_rules.gestures_label(["joie"], motion=False)
        self.assertIn("joie", text)
        self.assertNotIn('"motion"', text)
        self.assertIn("NAO ne bouge pas", body_rules.gestures_label([], motion=False))
        self.assertIn('"motion"', body_rules.gestures_label(["joie"]))    # default unchanged


if __name__ == "__main__":
    unittest.main()
