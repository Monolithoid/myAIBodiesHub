"""Tests sans réseau ni robot du service de gestes appris (emotion_gesture.py, ADR 009).

Le vrai générateur vient de nao-emotion-motion-dataset ; ici une petite bibliothèque
factice avec la même interface (GestureGenerator, Gesture, _min_jerk) suffit.

    cd robotics/drones/static/onboard && python3 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emotion_gesture as eg  # noqa: E402

FAKE_LIB = '''
import json

class _Report(object):
    def __init__(self, ok, why=""):
        self.ok, self.why = ok, why
    def summary(self):
        return self.why

class _Guardian(object):
    start_tolerance, rest_velocity, max_acceleration = 0.05, 0.3, 30.0
    def __init__(self, poses):
        self.poses = poses
    def pose(self, name):
        return dict(self.poses[name])
    def check_rest_transition(self, t, joints, q, start_state=None, time_offset=0.0):
        return _Report(True)

class Gesture(object):
    def __init__(self, joints, times, positions, report, gesture_report, segments, inputs, notes, durations,
                 adaptation=None, raw_report=None):
        self.joints, self.times, self.positions, self.report = joints, times, positions, report
        self.notes, self.adaptation = notes, adaptation
    @property
    def ok(self):
        return self.report.ok
    @property
    def duration(self):
        return self.times[-1] - self.times[0]
    def angle_interpolation(self, rate_hz=None):
        idx = range(1, len(self.times))
        return (list(self.joints), [[self.positions[i][k] for i in idx] for k in range(len(self.joints))],
                [[self.times[i] for i in idx] for _ in self.joints])

def _min_jerk(q0, q1, v, a):
    return {"t": [0.0, 1.0, 2.0], "q": [list(q0), [(x + y) / 2 for x, y in zip(q0, q1)], list(q1)]}

class GestureGenerator(object):
    def __init__(self, data):
        self.emotions = data["emotions"]
        self.groups = {"arms": ["LShoulderPitch"], "head": ["HeadYaw"], "hands": []}
        self.models = {"arms": 1, "head": 1}
        self.guardian = _Guardian(data["poses"])
        self.refuse = set(data.get("refuse", []))
        self.info = {"models": {"arms": {"run": "arms-1"}}, "calibration": {"calibration_version": "cal-1"}}
        self.calls = []
    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f))
    def generate(self, emotion, intensity, event=None, current_pose=None, from_rest=False, return_to="start"):
        self.calls.append((emotion, intensity, event, dict(current_pose or {}), from_rest, return_to))
        joints = ["LShoulderPitch", "HeadYaw"]
        start = self.guardian.pose("start")
        q0 = [current_pose[j] for j in joints]
        rows = [q0, [x + 0.1 for x in q0], [start[j] for j in joints]]
        ok = emotion not in self.refuse
        return Gesture(joints, [0.0, 0.5, 1.0], rows, _Report(ok, "" if ok else "velocity"), None, [], {},
                       ["note"], {}, {"changed": False})
'''

POSES = {"rest": {"LShoulderPitch": 1.4, "HeadYaw": 0.0}, "start": {"LShoulderPitch": 0.8, "HeadYaw": 0.0}}


def _sha(b):
    return hashlib.sha256(b).hexdigest()


class FakeBridge(object):
    """Rosbridge factice ; le robot répond à /nao/read_angles avec self.pose."""
    connected = True

    def __init__(self):
        self.subs, self.out = {}, []
        self.pose = dict(POSES["rest"])

    def advertise(self, topic, msg_type):
        pass

    def subscribe(self, topic, msg_type, cb):
        self.subs[topic] = cb

    def publish(self, topic, msg):
        data = json.loads(msg["data"])
        self.out.append((topic, data))
        if topic == "/nao/read_angles":
            self.subs["/nao/joint_angles"]({"data": json.dumps({"id": data["id"], "angles": self.pose})})

    def robot(self, gid, state):
        self.subs["/nao/trajectory_state"]({"data": json.dumps({"id": gid, "state": state})})

    def states(self):
        return [(d["id"], d["state"]) for t, d in self.out if t == "/nao/emotion_gesture_state"]

    def last_state(self):
        return [d for t, d in self.out if t == "/nao/emotion_gesture_state"][-1]

    def trajectories(self):
        return [d for t, d in self.out if t == "/nao/trajectory"]


class Base(unittest.TestCase):
    data = {"emotions": ["joie", "peur"], "poses": POSES}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.files = {"generator/nao_gesture.py": FAKE_LIB.encode(),
                      "generator/nao_gesture.json": json.dumps(self.data).encode()}
        self.gets = []

    def tearDown(self):
        shutil.rmtree(self.dir)

    def get(self, url):
        self.gets.append(url)
        path = url.split("/v1/", 1)[1]
        if path == "dataset.json":
            return json.dumps({"files": dict((p, _sha(b)) for p, b in self.files.items())}).encode()
        return self.files[path]

    def service(self, rest_delay=3.0):
        self.now = [100.0]
        self.bridge = FakeBridge()
        s = eg.GestureService(self.bridge, {"repo": "o/r", "ref": "v1", "cache_dir": self.dir,
                                            "rest_delay": rest_delay, "rate_hz": 25.0},
                              clock=lambda: self.now[0])
        s.setup()
        self.assertTrue(s.load(fetch=lambda repo, ref, cache: eg.fetch_generator(repo, ref, cache, get=self.get)))
        return s


class TestFetch(Base):

    def test_checked_then_cached(self):
        folder = eg.fetch_generator("o/r", "v1", self.dir, get=self.get)
        self.assertEqual(sorted(os.listdir(folder)), ["dataset.json", "nao_gesture.json", "nao_gesture.py"])
        self.assertEqual(self.gets[0], "https://raw.githubusercontent.com/o/r/v1/dataset.json")
        n = len(self.gets)
        eg.fetch_generator("o/r", "v1", self.dir, get=self.get)
        self.assertEqual(len(self.gets), n)   # version déjà en cache : pas de réseau

    def test_wrong_sha_refused(self):
        real = self.get

        def tampered(url):
            out = real(url)
            return out + b"#" if url.endswith("nao_gesture.py") else out
        with self.assertRaises(eg.GestureError):
            eg.fetch_generator("o/r", "v1", self.dir, get=tampered)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "v1")))

    def test_ref_from_the_env_file(self):
        env = os.path.join(self.dir, "emotion_gesture.env")
        with open(env, "w") as f:
            f.write("GESTURE_DATASET_REF=v2026-11-18\n")
        os.environ["GESTURE_ENV_FILE"] = env
        try:
            self.assertEqual(eg.read_settings()["ref"], "v2026-11-18")
            with open(env, "w") as f:
                f.write("GESTURE_DATASET_REF=../etc\n")
            with self.assertRaises(eg.GestureError):
                eg.read_settings()
        finally:
            del os.environ["GESTURE_ENV_FILE"]


class TestService(Base):

    def test_gesture_from_rest_then_back_to_rest(self):
        s = self.service()
        s.on_request({"id": "a", "emotion": "joie", "intensity": 0.7, "event": {"azimuth_deg": 30}})
        s.step()
        (traj,) = self.bridge.trajectories()
        self.assertEqual(traj["id"], "a")
        self.assertFalse(traj["relax_end"])          # reste raide à la pose de départ
        self.assertEqual(traj["start"], POSES["rest"])
        call = s.gen.calls[-1]
        self.assertEqual(call[2], {"azimuth_deg": 30})
        self.assertTrue(call[4])                     # mesuré au repos
        self.assertEqual(call[5], "start")
        self.assertTrue(self.bridge.last_state()["owns_body"])     # le driver laisse la main
        self.bridge.robot("a", "playing")
        self.assertFalse(self.bridge.last_state()["at_rest"])
        s.step()                                     # geste en cours : rien d'autre
        self.assertEqual(len(self.bridge.trajectories()), 1)
        self.bridge.robot("a", "done")
        self.bridge.pose = dict(POSES["start"])
        self.now[0] += 1.0
        s.step()                                     # délai pas encore écoulé
        self.assertEqual(len(self.bridge.trajectories()), 1)
        self.now[0] += 3.0
        s.step()
        rest = self.bridge.trajectories()[-1]
        self.assertTrue(rest["relax_end"])
        self.assertEqual(rest["angles"][0][-1], POSES["rest"]["LShoulderPitch"])
        self.bridge.robot(rest["id"], "playing")
        self.assertTrue(self.bridge.last_state()["owns_body"])
        self.bridge.robot(rest["id"], "done")
        self.assertFalse(self.bridge.last_state()["owns_body"])    # rendu au driver, au repos
        self.assertTrue(self.bridge.last_state()["at_rest"])
        self.now[0] += 10
        s.step()                                     # déjà au repos : plus rien
        self.assertEqual(len(self.bridge.trajectories()), 2)

    def test_new_gesture_chains_without_rest(self):
        s = self.service()
        s.on_request({"id": "a", "emotion": "joie", "intensity": 0.5})
        s.step()
        s.on_request({"id": "b", "emotion": "peur", "intensity": 0.5})
        s.on_request({"id": "c", "emotion": "joie", "intensity": 0.9})   # remplace b
        self.bridge.robot("a", "done")
        self.bridge.pose = dict(POSES["start"])
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a", "c"])
        self.assertIn(("b", "replaced"), self.bridge.states())
        self.assertFalse(s.gen.calls[-1][4])          # pas au repos : depuis la posture mesurée

    def test_guardian_refusal_sends_nothing(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, refuse=["peur"])).encode()
        s = self.service()
        s.on_request({"id": "a", "emotion": "peur", "intensity": 0.5})
        s.step()
        self.assertEqual(self.bridge.trajectories(), [])
        self.assertIn(("a", "refused"), self.bridge.states())

    def test_version_refused_keeps_the_old_one(self):
        s = self.service()
        old = s.gen
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, emotions=[])).encode()
        s.settings = dict(s.settings, ref="v1", cache_dir=os.path.join(self.dir, "other"))
        self.assertFalse(s.load(fetch=lambda repo, ref, cache: eg.fetch_generator(repo, ref, cache, get=self.get)))
        self.assertIs(s.gen, old)
        self.assertEqual(self.bridge.states()[-1], (None, "version_refused"))


if __name__ == "__main__":
    unittest.main()
