"""Tests sans robot du module trajectory (gestes appris, ADR 009) : le drone
statique génère et valide le geste, le robot lit ses angles et le joue.

    cd robotics/drones/nao/onboard && python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_motion_limits import BridgeTest, FakeMotion  # noqa: E402


class _Post(object):
    def __init__(self, motion):
        self.motion = motion

    def angleInterpolation(self, names, angles, times, absolute):
        self.motion.calls.append(("post.angleInterpolation", list(names), angles, times, absolute))
        return 42


class TrajMotion(FakeMotion):
    def __init__(self):
        FakeMotion.__init__(self)
        self.post = _Post(self)
        self.on_wait = None

    def wait(self, task, timeout):
        self.calls.append(("wait", task))
        if self.on_wait:
            self.on_wait()


GESTURE = {"id": "g1", "names": ["HeadYaw", "LWristYaw", "LHand"],
           "angles": [[0.1, 0.2], [0.3, 0.0], [0.5, 0.8]],
           "times": [[0.5, 1.0], [0.5, 1.0], [0.5, 1.0]],
           "start": {"HeadYaw": 0.0, "LWristYaw": 0.0, "LHand": 0.3},
           "relax_end": True}


class TestTrajectory(BridgeTest):

    def setUp(self):
        BridgeTest.setUp(self)
        self.proxy = TrajMotion()
        self.proxy.pose = {"HeadYaw": 0.0, "LWristYaw": 0.01, "LHand": 0.3}
        self.bridge = self.make()

    def send(self, payload):
        self.bridge._dispatch("/nao/trajectory", {"data": json.dumps(payload)})

    def states(self):
        return [(p["state"], p.get("reason")) for t, p in self.sent if t == "/nao/trajectory_state"]

    def test_read_angles(self):
        self.bridge._dispatch("/nao/read_angles", {"data": '{"id": "r1"}'})
        (topic, msg), = self.sent
        self.assertEqual((topic, msg["id"]), ("/nao/joint_angles", "r1"))
        self.assertEqual(msg["angles"]["LHand"], 0.3)
        self.assertNotIn("LKneePitch", msg["angles"])

    def test_played_at_once_then_relaxed(self):
        self.send(GESTURE)
        self.assertEqual([s for s, _ in self.states()], ["playing", "done"])
        played = [c for c in self.proxy.calls if c[0] == "post.angleInterpolation"]
        self.assertEqual(played, [("post.angleInterpolation", GESTURE["names"], GESTURE["angles"],
                                   GESTURE["times"], True)])
        # raideur posée sur place avant, coupée à la fin (geste terminé au repos)
        self.assertIn(("stiffness", "Head", 1.0), self.proxy.calls)
        self.assertIn(("stiffness", "LArm", 1.0), self.proxy.calls)
        self.assertEqual(self.bridge._limp, {"head", "larm"})

    def test_stays_stiff_without_relax(self):
        self.send(dict(GESTURE, relax_end=False))
        self.assertEqual(self.bridge._limp, set())
        self.assertEqual(self.bridge._stiffness_enabled, {"head", "larm"})

    def test_refusals(self):
        cases = [dict(GESTURE, names=["LKneePitch", "LWristYaw", "LHand"]),
                 dict(GESTURE, angles=[[0.1, 9.0], [0.3, 0.0], [0.5, 0.8]]),
                 dict(GESTURE, times=[[1.0, 0.5], [0.5, 1.0], [0.5, 1.0]]),
                 dict(GESTURE, start={"HeadYaw": 0.4}),
                 {"id": "x"}]
        for c in cases:
            self.send(c)
        self.assertEqual([s for s, _ in self.states()], ["refused"] * len(cases))
        self.assertFalse([c for c in self.proxy.calls if c[0] == "post.angleInterpolation"])

    def test_stop_holds_the_posture(self):
        self.proxy.on_wait = lambda: self.bridge._dispatch("/nao/stop", {})
        self.send(GESTURE)
        self.assertIn(("stop", 42), self.proxy.calls)
        self.assertEqual([s for s, _ in self.states()], ["playing", "stopped"])
        self.assertEqual(self.bridge._limp, set())   # pas relâché après un arrêt

    def test_module_off(self):
        bridge = self.make(modules=["led"])
        bridge._dispatch("/nao/trajectory", {"data": json.dumps(GESTURE)})
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
