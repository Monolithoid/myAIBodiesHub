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
    constraints, min_duration_s, max_duration_s = [], 0.3, 15.0
    def pose(self, name):
        return dict(self.poses[name])
    def allowed_range(self, joint):
        return [-2.0, 2.0]
    def velocity_limit(self, joint):
        return 5.0
    def acceleration_limit(self, joint):
        return 50.0
    def check_rest_transition(self, t, joints, q, start_state=None, time_offset=0.0):
        return _Report(True)

class Gesture(object):
    def __init__(self, joints, times, positions, report, gesture_report, segments, inputs, notes, durations):
        self.joints, self.times, self.positions, self.report = joints, times, positions, report
        self.notes = notes
        self.inputs = inputs
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

def rest_segment(guardian, q0, q1, joints):
    seg = _min_jerk(q0, q1, None, None)
    seg["t"] = [x / 5 for x in seg["t"]]        # factory limits: much shorter
    return seg

class GestureGenerator(object):
    def __init__(self, data):
        self.emotions = data["emotions"]
        self.groups = {"arms": ["LShoulderPitch"], "head": ["HeadYaw"], "hands": []}
        self.models = {"arms": 1, "head": 1}
        self.guardian = _Guardian(data["poses"])
        self.refuse = set(data.get("refuse", []))
        self.refuse_with_event = set(data.get("refuse_with_event", []))
        if not data.get("old"):
            self.adapt_generated = True   # exporté avec nao-emotion-motion#62 ou plus récent
        self.info = {"models": {"arms": {"run": "arms-1"}}, "calibration": {"calibration_version": "cal-1"}}
        self.calls = []
        self.kwargs = []
        if data.get("cvae"):
            self.latent_dims = {"arms": 4, "head": 4}
        self.refuse_drawn = set(data.get("refuse_drawn", []))
    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f))
    def _plan(self, model, x, variation, seed):
        return [[0.0]], 1.0
    def gesture(self, emotion, intensity, event=None, variation=0.0, seed=None):
        return ["LShoulderPitch"], [0.0, 1.0], [[0.8], [0.9]], {"notes": [], "durations": {}}
    def generate(self, emotion, intensity, event=None, current_pose=None, from_rest=False, return_to="start",
                 variation=0.0, seed=None, **kw):
        self.calls.append((emotion, intensity, event, dict(current_pose or {}), from_rest, return_to))
        self.kwargs.append(dict(kw, event=event, seed=seed, variation=variation))
        self.gesture(emotion, intensity, event, variation, seed)
        joints = ["LShoulderPitch", "HeadYaw"]
        start = self.guardian.pose("start")
        q0 = [current_pose[j] for j in joints]
        rows = [q0, [x + 0.1 for x in q0], [start[j] for j in joints]]
        ok = emotion not in self.refuse and not (event and emotion in self.refuse_with_event)
        ok = ok and not (emotion in self.refuse_drawn and kw.get("temperature") != 0.0)
        inputs = {}
        if getattr(self, "latent_dims", None):
            t = kw.get("temperature", 1.0)
            inputs = {"temperature": t, "latent": {"arms": [0.0] * 4 if t == 0.0 else [(seed or 0) % 7, 0.0, 0.0, 0.0]}}
        times, sequence = [0.0, 0.5, 1.0], None
        if kw.get("duration_s"):
            # one movement of the requested length, capped like the real guardian
            d = min(float(kw["duration_s"]), self.guardian.max_duration_s)
            times = [0.0, d / 2, d]
            sequence = {"draws": int(d) + 1, "time_scale": 1.1, "duration_s": d}
        g = Gesture(joints, times, rows, _Report(ok, "" if ok else "velocity"), None, [], inputs,
                    ["note"], {})
        g.sequence = sequence
        return g
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
        data = json.loads(msg.get("data", "{}"))
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
    data = {"emotions": ["joy", "fear"], "poses": POSES}

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

    def service(self, rest_delay=3.0, **settings):
        self.now = [100.0]
        self.slept = []
        self.bridge = FakeBridge()
        s = eg.GestureService(self.bridge, dict({"repo": "o/r", "ref": "v1", "cache_dir": self.dir,
                                                 "rest_delay": rest_delay, "rate_hz": 25.0}, **settings),
                              clock=lambda: self.now[0], wall=lambda: 1000.0,
                              sleep=self.slept.append)
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

    def test_any_posture_before_the_start_pose_counts_as_rest(self):
        s = self.service()
        for pose, from_rest in (({"LShoulderPitch": 1.1, "HeadYaw": 0.5}, True),    # head turned
                                ({"LShoulderPitch": 0.83, "HeadYaw": 0.0}, False)):  # within tolerance
            self.bridge.pose = dict(pose)
            s.play({"id": "p", "emotion": "joy", "intensity": 0.5})
            self.assertIs(s.gen.calls[-1][4], from_rest)

    def test_gesture_from_rest_then_back_to_rest(self):
        s = self.service()
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.7, "event": {"azimuth_deg": 30}})
        s.step()
        (traj,) = self.bridge.trajectories()
        self.assertEqual(traj["id"], "a")
        self.assertFalse(traj["relax_end"])          # reste raide à la pose de départ
        self.assertEqual(traj["start"], POSES["rest"])
        call = s.gen.calls[-1]
        self.assertEqual(call[2], {"azimuth_deg": 30})
        self.assertTrue(call[4])                     # pas à la pose de départ
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

    def _back_to_rest(self, s):
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.7})
        s.step()
        self.bridge.robot("a", "done")
        self.bridge.pose = dict(POSES["start"])
        self.now[0] += 4.0
        s.step()
        return self.bridge.trajectories()[-1]

    def test_back_to_rest_uses_the_generator_rest_segment(self):
        rest = self._back_to_rest(self.service())
        self.assertTrue(rest["relax_end"])
        self.assertAlmostEqual(rest["times"][0][-1], 0.4)

    def test_back_to_rest_with_an_older_generator(self):
        s = self.service()
        del s.lib.rest_segment                       # exported before rest_segment()
        rest = self._back_to_rest(s)
        self.assertTrue(rest["relax_end"])
        self.assertAlmostEqual(rest["times"][0][-1], 2.0)

    def test_new_gesture_chains_without_rest(self):
        s = self.service()
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5})
        s.step()
        s.on_request({"id": "b", "emotion": "fear", "intensity": 0.5})
        s.on_request({"id": "c", "emotion": "joy", "intensity": 0.9})   # remplace b
        self.bridge.robot("a", "done")
        self.bridge.pose = dict(POSES["start"])
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a", "c"])
        self.assertIn(("b", "replaced"), self.bridge.states())
        self.assertFalse(s.gen.calls[-1][4])          # pas au repos : depuis la posture mesurée

    def _playing_a_then_b_asked(self, refuse_b=False, **b):
        s = self.service(speech_wait_s=0.0)          # sentence duration: the estimate
        if refuse_b:
            s.gen.refuse.add("fear")
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5})
        s.step()
        self.bridge.robot("a", "playing")
        self.bridge.pose = dict(POSES["rest"])       # never read while a plays
        s.on_request(dict({"id": "b", "emotion": "fear", "intensity": 0.5}, **b))
        reads = sum(1 for t, _ in self.bridge.out if t == "/nao/read_angles")
        self.calls = len(s.gen.calls)
        s.step()                                     # a still playing: b prepared now
        return s, reads

    def reads(self):
        return sum(1 for t, _ in self.bridge.out if t == "/nao/read_angles")

    def test_next_gesture_prepared_while_the_current_one_plays(self):
        s, reads = self._playing_a_then_b_asked(sync={"id": "s-b", "speech_s": 2.0, "speech_s_first": 2.0})
        self.assertEqual(s.gen.calls[-1][3], POSES["start"])   # from the start pose, where a ends
        self.assertFalse(s.gen.calls[-1][4])
        self.assertEqual(s.gen.kwargs[-1]["duration_s"], 2.0)
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a"])
        s.step()                                     # prepared once
        self.assertEqual(len(s.gen.calls), self.calls + 1)
        self.bridge.robot("a", "done")
        s.step()                                     # sent at once: no measure, no generation
        traj = self.bridge.trajectories()[-1]
        self.assertEqual((traj["id"], traj["start"]), ("b", POSES["start"]))
        self.assertEqual((self.reads(), len(s.gen.calls)), (reads, self.calls + 1))
        self.assertTrue(self.bridge.last_state()["prepared_ahead"])
        self.assertIn("s-b", [d.get("sync") for t, d in self.bridge.out if t == eg.SYNC_TOPIC])

    def test_prepared_gesture_replaced_by_a_newer_request(self):
        s, _ = self._playing_a_then_b_asked()
        s.on_request({"id": "c", "emotion": "joy", "intensity": 0.9})
        s.step()                                     # c prepared in turn
        self.bridge.robot("a", "done")
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a", "c"])
        self.assertIn(("b", "replaced"), self.bridge.states())
        self.assertTrue(self.bridge.last_state()["prepared_ahead"])

    def test_prepared_gesture_made_again_when_the_current_one_does_not_end_at_start(self):
        s, reads = self._playing_a_then_b_asked()
        self.bridge.pose = {"LShoulderPitch": 1.1, "HeadYaw": 0.2}
        self.bridge.robot("a", "stopped")
        s.step()
        self.assertEqual(self.bridge.trajectories()[-1]["start"], self.bridge.pose)
        self.assertEqual(self.reads(), reads + 1)
        self.assertTrue(s.gen.calls[-1][4])           # from the measured posture
        self.assertFalse(self.bridge.last_state()["prepared_ahead"])

    def test_refused_ahead_is_not_played_later(self):
        s, _ = self._playing_a_then_b_asked(refuse_b=True)
        self.assertIn(("b", "refused"), self.bridge.states())
        self.bridge.robot("a", "done")
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a"])

    def test_gesture_stops_the_return_to_rest(self):
        s = self.service()
        rest = self._back_to_rest(s)
        self.bridge.robot(rest["id"], "playing")
        s.on_request({"id": "b", "emotion": "fear", "intensity": 0.5})
        s.step()
        self.assertEqual(sum(1 for t, _ in self.bridge.out if t == "/nao/stop"), 1)
        s.step()                                     # waits for "stopped"
        self.assertEqual(sum(1 for t, _ in self.bridge.out if t == "/nao/stop"), 1)
        self.bridge.pose = dict(POSES["start"])      # halfway: posture held
        self.bridge.robot(rest["id"], "stopped")
        s.step()
        self.assertEqual(self.bridge.trajectories()[-1]["id"], "b")
        self.assertFalse(s.gen.calls[-1][4])          # from the measured posture, not from rest

    def test_reflex_does_not_stop_the_return_to_rest(self):
        s = self.service()
        rest = self._back_to_rest(s)
        self.bridge.robot(rest["id"], "playing")
        s.on_request({"id": "r", "emotion": "fear", "intensity": 0.3, "reflex": True})
        s.step()
        self.assertFalse(any(t == "/nao/stop" for t, _ in self.bridge.out))

    def test_old_generator_refused_with_reason(self):
        s = self.service()
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, old=True)).encode()
        s.settings = dict(s.settings, ref="v1")
        shutil.rmtree(os.path.join(self.dir, "v1"))
        self.assertFalse(s.load(fetch=lambda repo, ref, cache: eg.fetch_generator(repo, ref, cache, get=self.get)))
        last = self.bridge.last_state()
        self.assertEqual(last["state"], "version_refused")
        self.assertIn("trop ancien", last["reason"])
        self.assertIsNotNone(s.gen)                  # l'ancienne version reste en service

    def test_retry_without_event(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, refuse_with_event=["joy"])).encode()
        s = self.service()
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5, "event": {"azimuth_deg": 20}})
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a"])
        self.assertIsNone(s.gen.calls[-1][2])
        sent = [d for t, d in self.bridge.out if t == "/nao/emotion_gesture_state" and d["state"] == "sent"]
        self.assertEqual(sent[-1]["retry"], "sans événement")

    def _sent(self):
        return [d for t, d in self.bridge.out if t == "/nao/emotion_gesture_state" and d["state"] == "sent"]

    def test_cvae_draws_a_new_gesture_each_time(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, cvae=True)).encode()
        s = self.service(latent_temperature=0.8)
        seeds = []
        for gid in ("a", "b"):
            s.on_request({"id": gid, "emotion": "joy", "intensity": 0.5})
            s.step()
            kw = s.gen.kwargs[-1]
            self.assertEqual(kw["temperature"], 0.8)
            seeds.append(kw["seed"])
            s.on_trajectory_state({"id": gid, "state": "done"})
        self.assertNotEqual(seeds[0], seeds[1])
        sent = self._sent()
        self.assertEqual([d["id"] for d in sent], ["a", "b"])
        self.assertEqual(sent[-1]["temperature"], 0.8)
        self.assertIsNone(sent[-1]["retry"])
        self.assertIn("arms", sent[-1]["latent"])

    def test_cvae_default_temperature_is_the_generator_one(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, cvae=True)).encode()
        s = self.service()
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5})
        s.step()
        self.assertNotIn("temperature", {k: v for k, v in s.gen.kwargs[-1].items() if k != "event"})
        self.assertIsNotNone(s.gen.kwargs[-1]["seed"])

    def test_cvae_refused_draws_fall_back_to_the_central_gesture(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, cvae=True, refuse_drawn=["fear"])).encode()
        s = self.service()
        s.on_request({"id": "a", "emotion": "fear", "intensity": 0.5, "event": {"azimuth_deg": 20}})
        s.step()
        tried = s.gen.kwargs[-4:]
        self.assertEqual([k["event"] for k in tried], [{"azimuth_deg": 20}, None, {"azimuth_deg": 20},
                                                       {"azimuth_deg": 20}])
        self.assertEqual(tried[0]["seed"], tried[1]["seed"])
        self.assertNotEqual(tried[0]["seed"], tried[2]["seed"])
        self.assertEqual(tried[3]["temperature"], 0.0)
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a"])
        self.assertEqual(self._sent()[-1]["retry"], "geste central")

    def test_plain_generator_keeps_the_former_tries(self):
        s = self.service(latent_temperature=0.8)
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5})
        s.step()
        self.assertEqual(s.gen.kwargs[-1], {"event": None, "seed": None, "variation": 0.0})

    def test_guardian_refusal_sends_nothing(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, refuse=["fear"])).encode()
        s = self.service()
        s.on_request({"id": "a", "emotion": "fear", "intensity": 0.5})
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


class TestSpeechSync(Base):
    """Geste et phrase d'une même réponse : durée calée, départ commun, décalage réglable."""

    SYNC = {"id": "s-1", "speech_s": 2.0, "speech_s_first": 2.0}

    def go(self):
        return [d for t, d in self.bridge.out if t == eg.SYNC_TOPIC and d.get("kind") == "go"]

    def test_generator_asked_for_the_measured_speech_duration(self):
        s = self.service(speech_offset_ms=200.0, speech_wait_s=0.5)
        self.bridge.subs[eg.SYNC_TOPIC]({"data": json.dumps({"kind": "speech", "sync": "s-1", "speech_s": 3.0})})
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5, "sync": self.SYNC})
        s.step()
        self.assertEqual(s.gen.kwargs[-1]["duration_s"], 3.0)
        (traj,) = self.bridge.trajectories()
        self.assertAlmostEqual(traj["times"][0][-1], 3.0)          # the generator's length, as is
        (go,) = self.go()
        self.assertEqual(go["sync"], "s-1")
        self.assertAlmostEqual(go["at"], 1000.2)                    # start of the gesture + 200 ms
        self.assertAlmostEqual(go["in_s"], 0.2)                     # same, as a delay (OBS voice)
        sent = [d for t, d in self.bridge.out if t == "/nao/emotion_gesture_state" and d["state"] == "sent"][-1]
        self.assertEqual(sent["speech_s"], 3.0)
        self.assertEqual(sent["draws"], 4)
        self.assertEqual(sent["time_scale"], 1.1)
        self.assertEqual(sent["duration_s"], 3.0)
        self.assertEqual(self.slept, [])

    def test_estimate_used_when_measure_missing(self):
        s = self.service(speech_wait_s=0.0)
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5, "sync": self.SYNC})
        s.step()
        self.assertEqual(s.gen.kwargs[-1]["duration_s"], 2.0)
        self.assertAlmostEqual(self.bridge.trajectories()[0]["times"][0][-1], 2.0)

    def test_measured_first_sentence_plus_estimated_rest(self):
        s = self.service(speech_wait_s=0.5)
        s.on_sync({"kind": "speech", "sync": "s-1", "speech_s": 2.5})
        self.assertEqual(s.speech_seconds({"id": "s-1", "speech_s": 5.0, "speech_s_first": 2.0}), 5.5)

    def test_drone_never_stretches_the_gesture(self):
        # The generator caps at its guardian maximum (15 s): the drone plays it as is.
        s = self.service(speech_wait_s=0.0)
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5,
                      "sync": {"id": "s-1", "speech_s": 20.0}})
        s.step()
        self.assertEqual(s.gen.kwargs[-1]["duration_s"], 20.0)
        self.assertAlmostEqual(self.bridge.trajectories()[0]["times"][0][-1], 15.0)

    def test_every_try_gets_the_duration(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, cvae=True, refuse_drawn=["fear"])).encode()
        s = self.service(speech_wait_s=0.0)
        s.on_request({"id": "a", "emotion": "fear", "intensity": 0.5, "sync": self.SYNC})
        s.step()
        self.assertEqual([k["duration_s"] for k in s.gen.kwargs[-3:]], [2.0, 2.0, 2.0])
        self.assertEqual(s.gen.kwargs[-1]["temperature"], 0.0)

    def test_match_speech_off_keeps_a_single_gesture(self):
        s = self.service(speech_wait_s=0.0, match_speech=False)
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5, "sync": self.SYNC})
        s.step()
        self.assertNotIn("duration_s", s.gen.kwargs[-1])
        self.assertAlmostEqual(self.bridge.trajectories()[0]["times"][0][-1], 1.0)
        self.assertEqual(len(self.go()), 1)

    def test_no_duration_without_speech(self):
        s = self.service()
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5})
        s.step()
        self.assertNotIn("duration_s", s.gen.kwargs[-1])

    def test_negative_offset_delays_the_gesture(self):
        s = self.service(speech_wait_s=0.0, speech_offset_ms=-300.0)
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5, "sync": self.SYNC})
        s.step()
        self.assertEqual(self.slept, [0.3])     # pas de passage à la pose de départ ici
        self.assertAlmostEqual(self.go()[0]["at"], 1000.0)   # parole tout de suite, geste 0,3 s après

    def test_offset_set_live(self):
        s = self.service()
        s.on_control({"op": "speech_offset", "ms": 120})
        self.assertEqual(s.settings["speech_offset_ms"], 120.0)
        s.on_control({"op": "speech_offset", "ms": 99999})
        self.assertEqual(s.settings["speech_offset_ms"], 10000.0)

    def test_refused_gesture_releases_the_speech(self):
        self.files["generator/nao_gesture.json"] = json.dumps(dict(self.data, refuse=["fear"])).encode()
        s = self.service(speech_wait_s=0.0)
        s.on_request({"id": "a", "emotion": "fear", "intensity": 0.5, "sync": self.SYNC})
        s.step()
        self.assertEqual(self.go(), [{"kind": "go", "sync": "s-1", "at": 0, "in_s": 0,
                                      "reason": "geste refusé"}])

    def test_replaced_gesture_releases_its_speech(self):
        s = self.service(speech_wait_s=0.0)
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5, "sync": self.SYNC})
        s.on_request({"id": "b", "emotion": "joy", "intensity": 0.5})
        self.assertEqual(self.go()[0]["sync"], "s-1")
        self.assertEqual(self.go()[0]["at"], 0)


class _G(object):
    def __init__(self, times, segments):
        self.times, self.segments = times, segments


class TestExpressiveWindow(unittest.TestCase):
    SEGS = [("rest_to_start", 0.0, 1.0), ("start_to_gesture", 1.0, 1.5), ("gesture", 1.5, 2.5),
            ("gesture_to_start", 2.5, 3.0)]

    def test_from_start_to_gesture_to_back_at_start(self):
        self.assertEqual(eg.expressive_window(_G([0.0, 3.0], list(self.SEGS))), (1.0, 3.0))

    def test_whole_movement_without_segments(self):
        self.assertEqual(eg.expressive_window(_G([0.0, 2.0], [])), (0.0, 2.0))

    def test_start_pose_inside_the_gesture(self):
        # dataset v0.0.6 or later: no transition segments, the gesture starts and ends
        # at the start pose; only the move from rest comes before it
        self.assertEqual(eg.expressive_window(_G([0.0, 3.0], [("gesture", 0.0, 3.0)])), (0.0, 3.0))
        self.assertEqual(eg.expressive_window(
            _G([0.0, 3.4], [("rest_to_start", 0.0, 0.4), ("gesture", 0.4, 3.4)])), (0.4, 3.4))


class TestReflex(Base):
    """What the drone perceives (sound, motion) -> short gesture, through the NAO driver."""

    REFLEX = {"reflex": True, "reflex_cooldown_s": 20.0,
              "reflex_gestures": {"sound": ("fear", 0.3), "motion": ("joy", 0.2)}}

    def asked(self):
        return [d for t, d in self.bridge.out if t == "/bodies/nao_01/actuators/gesture"]

    def test_sound_asks_a_reflex_through_the_driver(self):
        s = self.service(**self.REFLEX)
        self.now[0] += 100
        s.on_event({"kind": "sound", "azimuth_deg": 30.0})
        (order,) = self.asked()
        self.assertEqual(order, {"emotion": "fear", "intensity": 0.3, "reflex": True,
                                 "event": {"azimuth_deg": -30.0, "elevation_deg": 0.0}})
        s.on_event({"kind": "motion", "azimuth_deg": None})          # cooldown
        self.assertEqual(len(self.asked()), 1)
        self.now[0] += 25
        s.on_event({"kind": "motion", "azimuth_deg": None})
        self.assertEqual(self.asked()[-1], {"emotion": "joy", "intensity": 0.2, "reflex": True})

    def test_no_reflex_when_off_unknown_or_busy(self):
        s = self.service(**dict(self.REFLEX, reflex=False))
        self.now[0] += 100
        s.on_event({"kind": "sound", "azimuth_deg": 0.0})
        s = self.service(**dict(self.REFLEX, reflex_gestures={"sound": ("inconnue", 0.3)}))
        self.now[0] += 100
        s.on_event({"kind": "sound", "azimuth_deg": 0.0})
        s = self.service(**self.REFLEX)
        self.now[0] += 100
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.5})     # asked by the LLM
        s.on_event({"kind": "sound", "azimuth_deg": 0.0})
        self.assertEqual(self.asked(), [])

    def test_llm_gesture_stops_a_reflex(self):
        s = self.service(**self.REFLEX)
        s.on_request({"id": "r", "emotion": "fear", "intensity": 0.3, "reflex": True})
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["r"])
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.6})
        s.step()
        self.assertEqual(sum(1 for t, _ in self.bridge.out if t == "/nao/stop"), 1)
        s.step()                                                     # waits for "stopped"
        self.assertEqual(sum(1 for t, _ in self.bridge.out if t == "/nao/stop"), 1)
        self.bridge.robot("r", "stopped")
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["r", "a"])

    def test_reflex_never_replaces_an_llm_gesture(self):
        s = self.service(**self.REFLEX)
        s.on_request({"id": "a", "emotion": "joy", "intensity": 0.6})
        s.on_request({"id": "r", "emotion": "fear", "intensity": 0.3, "reflex": True})
        self.assertIn(("r", "ignored"), self.bridge.states())
        s.step()
        self.assertEqual([t["id"] for t in self.bridge.trajectories()], ["a"])

    def test_parse_reflex(self):
        self.assertEqual(eg.parse_reflex("surprise:0.4"), ("surprise", 0.4))
        self.assertEqual(eg.parse_reflex("calm"), ("calm", 0.3))

    def test_parse_temperature(self):
        self.assertEqual(eg.parse_temperature("0.8"), 0.8)
        self.assertEqual(eg.parse_temperature("0"), 0.0)
        for bad in ("", " ", "x", "-1", "nan", "inf"):
            self.assertIsNone(eg.parse_temperature(bad))
        self.assertIsNone(eg.parse_reflex(""))
        self.assertIsNone(eg.parse_reflex("off"))


if __name__ == "__main__":
    unittest.main()
