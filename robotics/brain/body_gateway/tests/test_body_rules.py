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
        text = body_rules.gestures_label(["joy", "fear"])
        self.assertIn("joy, fear", text)
        self.assertIn('"gesture"', text)

    def test_order(self):
        self.assertEqual(body_rules.gesture_order({"emotion": "joy", "intensity": 1.4}, ["joy"]),
                         {"emotion": "joy", "intensity": 1.0})
        self.assertIsNone(body_rules.gesture_order({"emotion": "anger"}, ["joy"]))
        self.assertIsNone(body_rules.gesture_order({}, ["joy"]))
        # Sans intensité : celle de l'énergie de la réponse.
        self.assertEqual(body_rules.gesture_order({"emotion": "joy"}, ["joy"], "high")["intensity"], 0.7)
        self.assertEqual(body_rules.gesture_order({"emotion": "joy", "intensity": "x"}, [])["intensity"], 0.5)

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
               {"body": "nao_01", "type": "gesture", "emotion": "joy", "intensity": 0.6}]

    def test_speech_seconds(self):
        self.assertEqual(body_rules.speech_seconds(""), 0.0)
        short = body_rules.speech_seconds("Oui.")
        long = body_rules.speech_seconds("Bonjour, je suis vraiment très content de te voir aujourd'hui.")
        self.assertLess(short, 1.0)
        self.assertTrue(3.0 < long < 6.0, long)

    def test_sync_when_room_speaker_and_gesture(self):
        sync = body_rules.speech_gesture_sync(self.ACTIONS, REGISTRY, self.CONNECTED, ["joy"], "s-1")
        self.assertEqual(sync["id"], "s-1")
        self.assertEqual(sync["speech_s"], sync["speech_s_first"])
        self.assertGreater(sync["speech_s"], 1.0)

    def test_several_sentences(self):
        actions = self.ACTIONS + [{"body": "static_01", "type": "speech", "text": "Et toi ?"}]
        sync = body_rules.speech_gesture_sync(actions, REGISTRY, self.CONNECTED, ["joy"], "s-1")
        self.assertGreater(sync["speech_s"], sync["speech_s_first"])

    def test_sync_with_obs_voice_only(self):
        registry = ["nao_01", "obs_01"]
        actions = [dict(self.ACTIONS[0], body="obs_01"), self.ACTIONS[1]]
        sync = body_rules.speech_gesture_sync(actions, registry, {"nao_01": True, "obs_01": True},
                                              ["joy"], "s-1")
        self.assertEqual(sync["id"], "s-1")

    def test_no_sync_without_gesture_or_static_voice(self):
        self.assertIsNone(body_rules.speech_gesture_sync(self.ACTIONS[:1], REGISTRY, self.CONNECTED, [], "s"))
        self.assertIsNone(body_rules.speech_gesture_sync(self.ACTIONS, REGISTRY, self.CONNECTED, ["fear"], "s"))
        self.assertIsNone(body_rules.speech_gesture_sync(
            self.ACTIONS, REGISTRY, {"nao_01": True, "static_01": False}, ["joy"], "s"))



class TestGesturesOnly(unittest.TestCase):
    """LLM_MOTION=0: NAO only moves through learned gestures."""

    def test_nao_label(self):
        text = body_rules.describe_bodies(REGISTRY, "static_01", motion=False)
        self.assertIn("nao_01 : gestes appris", text)
        self.assertNotIn("motion", text)

    def test_gestures_label(self):
        text = body_rules.gestures_label(["joy"], motion=False)
        self.assertIn("joy", text)
        self.assertNotIn('"motion"', text)
        self.assertIn("NAO ne bouge pas", body_rules.gestures_label([], motion=False))
        self.assertIn('"motion"', body_rules.gestures_label(["joy"]))    # default unchanged


class TestFirstGestureOnly(unittest.TestCase):
    def test_keeps_the_first_gesture_and_everything_else(self):
        actions = [{"type": "speech", "text": "a"}, {"type": "gesture", "emotion": "joy"},
                   {"type": "leds", "color": "blue"}, {"type": "gesture", "emotion": "fear"},
                   {"type": "speech", "text": "b"}]
        self.assertEqual(body_rules.first_gesture_only(actions),
                         [actions[0], actions[1], actions[2], actions[4]])

    def test_unchanged_with_one_or_no_gesture(self):
        one = [{"type": "speech", "text": "a"}, {"type": "gesture", "emotion": "joy"}]
        self.assertEqual(body_rules.first_gesture_only(one), one)
        self.assertEqual(body_rules.first_gesture_only(one[:1]), one[:1])


class TestSpeakingTracker(unittest.TestCase):

    def test_idle_at_start(self):
        self.assertFalse(body_rules.SpeakingTracker().busy(now=0))

    def test_voice_busy_until_it_stops_speaking(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        t.said("static_01", "bonjour", now=0)
        self.assertTrue(t.busy(now=1))          # synthesis: not started yet
        t.heard("static_01", True, now=2)
        self.assertTrue(t.busy(now=30))         # still speaking after the grace
        t.heard("static_01", False, now=31)
        self.assertFalse(t.busy(now=31))

    def test_two_sentences_wait_for_the_second(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        t.said("static_01", "un", now=0)
        t.said("static_01", "deux", now=0)
        t.heard("static_01", True, now=1)
        t.heard("static_01", False, now=2)
        self.assertTrue(t.busy(now=2))          # second sentence not started yet
        t.heard("static_01", True, now=2.1)
        t.heard("static_01", False, now=3)
        self.assertFalse(t.busy(now=3))

    def test_two_voices(self):
        t = body_rules.SpeakingTracker()
        t.said("static_01", "salut", now=0)
        t.said("obs_01", "salut", now=0)
        t.heard("static_01", True, now=1)
        t.heard("obs_01", True, now=1)
        t.heard("static_01", False, now=3)
        self.assertTrue(t.busy(now=3))
        t.heard("obs_01", False, now=4)
        self.assertFalse(t.busy(now=4))

    def test_started_when_the_first_sentence_of_the_answer_starts(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        t.answer()
        t.said("static_01", "un", now=0)
        t.said("static_01", "deux", now=0)
        self.assertFalse(t.started(now=1))      # synthesis, gesture not started
        t.heard("static_01", True, now=2)
        self.assertTrue(t.started(now=2))       # second sentence still pending: started anyway
        self.assertTrue(t.busy(now=2))

    def test_next_answer_not_started_while_the_previous_one_plays(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        t.answer()
        t.said("static_01", "réponse N", now=0)
        t.heard("static_01", True, now=1)
        t.answer()
        t.said("static_01", "réponse N+1", now=3)   # waits behind N on the speaker
        self.assertFalse(t.started(now=4))
        t.heard("static_01", False, now=5)
        t.heard("static_01", True, now=5.5)
        self.assertTrue(t.started(now=5.5))

    def test_started_on_every_voice(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        t.answer()
        t.said("static_01", "salut", now=0)
        t.said("obs_01", "salut", now=0)
        t.heard("static_01", True, now=1)
        self.assertFalse(t.started(now=1))
        t.heard("obs_01", True, now=1.5)
        self.assertTrue(t.started(now=1.5))

    def test_started_when_given_up_or_spoken_by_the_nao(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        t.answer()
        t.said("static_01", "bonjour", now=0)
        self.assertTrue(t.started(now=11))      # never started: given up
        t.answer()
        t.said("nao_01", "bonjour", now=20)     # no speaking signal: at once
        self.assertTrue(t.started(now=20))

    def test_answer_without_speech(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        self.assertTrue(t.started(now=0))
        t.answer()
        t.said("static_01", "un", now=0)
        t.answer()                              # LEDs only
        self.assertFalse(t.started(now=1))      # older sentence not started yet
        t.heard("static_01", True, now=2)
        self.assertTrue(t.started(now=2))

    def test_voice_that_never_starts_is_given_up(self):
        t = body_rules.SpeakingTracker(start_grace_s=10)
        t.said("static_01", "bonjour", now=0)
        self.assertTrue(t.busy(now=9))
        self.assertFalse(t.busy(now=11))

    def test_missing_false_is_given_up(self):
        t = body_rules.SpeakingTracker(max_speaking_s=60)
        t.heard("static_01", True, now=0)
        self.assertTrue(t.busy(now=59))
        self.assertFalse(t.busy(now=61))

    def test_nao_alone_uses_estimated_duration(self):
        t = body_rules.SpeakingTracker()
        text = "Bonjour à tous, merci d'être là ce soir."
        t.said("nao_01", text, now=0)
        d = body_rules.speech_seconds(text)
        self.assertTrue(t.busy(now=d - 0.1))
        self.assertFalse(t.busy(now=d + 0.1))



class TestQueuedSentence(unittest.TestCase):

    def test_waiting_behind_a_long_sentence_is_not_late(self):
        tracker = body_rules.SpeakingTracker(start_grace_s=10, max_speaking_s=120)
        tracker.answer()
        tracker.said("static_01", "longue", now=0)
        tracker.heard("static_01", True, now=1)
        tracker.answer()
        tracker.said("static_01", "suivante", now=2)
        self.assertFalse(tracker.started(now=30))
        tracker.heard("static_01", False, now=31)
        self.assertFalse(tracker.started(now=40))
        tracker.heard("static_01", True, now=40.5)
        self.assertTrue(tracker.started(now=41))

    def test_still_given_up_when_it_never_starts(self):
        tracker = body_rules.SpeakingTracker(start_grace_s=10)
        tracker.said("static_01", "a", now=0)
        tracker.heard("static_01", True, now=1)
        tracker.said("static_01", "b", now=2)
        tracker.heard("static_01", False, now=30)
        self.assertTrue(tracker.busy(now=39))
        self.assertFalse(tracker.busy(now=41))


class TestPerform(unittest.TestCase):

    def test_defaults_and_gesture_mapping(self):
        order = body_rules.perform_request(
            {"actions": [{"type": "speech", "text": "Il pleut."},
                         {"type": "gesture", "emotion": "scared", "intensity": 0.5}],
             "emotion": "scared", "energy": "low"}, REGISTRY, ["fear", "joy", "calm"])
        self.assertEqual(order["actions"][0]["body"], "nao_01")
        self.assertEqual(order["actions"][1]["emotion"], "fear")
        self.assertEqual((order["emotion"], order["energy"]), ("uncertain", "low"))

    def test_every_story_emotion_has_a_learned_gesture(self):
        learned = {"joy", "sadness", "anger", "fear", "surprise", "calm"}
        for emotion in ("neutral", "calm", "curious", "joyful", "excited", "uncertain",
                        "worried", "scared", "angry", "sad"):
            self.assertIn(body_rules.STORY_GESTURES[emotion], learned)

    def test_refused(self):
        for body in (None, {}, {"actions": []}, {"actions": ["x"]},
                     {"actions": [{"type": "move", "x": 1}]},
                     {"actions": [{"type": "speech", "text": " "}]}):
            with self.assertRaises(ValueError, msg=body):
                body_rules.perform_request(body, REGISTRY, [])

if __name__ == "__main__":
    unittest.main()
