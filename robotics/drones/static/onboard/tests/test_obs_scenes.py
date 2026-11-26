"""Tests without OBS of the scene switch that follows the activity (obs_scenes.py, C33).

    cd robotics/drones/static/onboard && python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import obs_scenes  # noqa: E402


class FakeSocket:
    """Plays OBS's side: Hello, Identified, then the answer to the request."""

    def __init__(self, auth=None, result=True, close_after_identify=False):
        self.sent = []
        hello = {"obsWebSocketVersion": "5.0.0", "rpcVersion": 1}
        if auth:
            hello["authentication"] = auth
        self._replies = [json.dumps({"op": 0, "d": hello})]
        if close_after_identify:
            self._replies.append("")
        else:
            self._replies += [json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}),
                              json.dumps({"op": 5, "d": {"eventType": "Noise"}}),
                              json.dumps({"op": 7, "d": {"requestStatus": {
                                  "result": result, "code": 100 if result else 600,
                                  "comment": "" if result else "No source was found"}}})]
        self.closed = False

    def recv(self):
        return self._replies.pop(0)

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


class TestParse(unittest.TestCase):

    def test_pairs(self):
        self.assertEqual(obs_scenes.parse_scenes(" default = Plateau , Hangman=Le pendu,bad,x="),
                         {"default": "Plateau", "hangman": "Le pendu"})

    def test_empty(self):
        self.assertEqual(obs_scenes.parse_scenes(""), {})
        self.assertEqual(obs_scenes.parse_scenes(None), {})

    def test_scene_for(self):
        scenes = {"default": "Plateau", "hangman": "Pendu"}
        self.assertEqual(obs_scenes.scene_for("hangman", scenes), "Pendu")
        self.assertEqual(obs_scenes.scene_for("investigation", scenes), "Plateau")
        self.assertEqual(obs_scenes.scene_for(None, scenes), "Plateau")
        self.assertIsNone(obs_scenes.scene_for(None, {"hangman": "Pendu"}))


class TestAuth(unittest.TestCase):

    def test_protocol_example(self):
        # Example of the obs-websocket v5 protocol documentation.
        self.assertEqual(obs_scenes.auth_string(
            "supersecretpassword", "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
            "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="),
            "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4=")


class TestClient(unittest.TestCase):

    def client(self, ws, password=""):
        return obs_scenes.ObsClient("ws://obs:4455", password, connect=lambda url, t: ws)

    def test_set_scene_without_auth(self):
        ws = FakeSocket()
        self.client(ws).set_scene("Pendu")
        self.assertEqual(ws.sent[0], {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}})
        self.assertEqual(ws.sent[1]["op"], 6)
        self.assertEqual(ws.sent[1]["d"]["requestType"], "SetCurrentProgramScene")
        self.assertEqual(ws.sent[1]["d"]["requestData"], {"sceneName": "Pendu"})
        self.assertTrue(ws.closed)

    def test_set_scene_with_auth(self):
        auth = {"salt": "s", "challenge": "c"}
        ws = FakeSocket(auth=auth)
        self.client(ws, "pw").set_scene("Pendu")
        self.assertEqual(ws.sent[0]["d"]["authentication"],
                         obs_scenes.auth_string("pw", "s", "c"))

    def test_password_missing(self):
        ws = FakeSocket(auth={"salt": "s", "challenge": "c"})
        with self.assertRaises(obs_scenes.ObsError):
            self.client(ws).set_scene("Pendu")
        self.assertTrue(ws.closed)

    def test_wrong_password(self):
        ws = FakeSocket(auth={"salt": "s", "challenge": "c"}, close_after_identify=True)
        with self.assertRaisesRegex(obs_scenes.ObsError, "OBS_WS_PASSWORD"):
            self.client(ws, "bad").set_scene("Pendu")

    def test_unknown_scene(self):
        with self.assertRaisesRegex(obs_scenes.ObsError, "refused"):
            self.client(FakeSocket(result=False)).set_scene("Nope")


class FakeClient:
    def __init__(self, fail=False):
        self.scenes, self.fail = [], fail
        self.done = threading.Event()

    def set_scene(self, scene):
        self.scenes.append(scene)
        self.done.set()
        if self.fail:
            raise obs_scenes.ObsError("OBS down")


def msg(activity):
    return {"data": json.dumps({"activity": activity})}


class TestSwitcher(unittest.TestCase):

    SCENES = {"default": "Plateau", "hangman": "Pendu"}

    def test_first_value_only_remembered(self):
        s = obs_scenes.SceneSwitcher(FakeClient(), self.SCENES)
        self.assertIsNone(s.change("hangman"))
        self.assertIsNone(s.change("hangman"))   # periodic repeat
        self.assertEqual(s.change(None), "Plateau")
        self.assertIsNone(s.change(None))
        self.assertEqual(s.change("hangman"), "Pendu")

    def test_no_default_scene(self):
        s = obs_scenes.SceneSwitcher(FakeClient(), {"hangman": "Pendu"})
        s.change("hangman")
        self.assertIsNone(s.change(None))

    def test_unreadable_message_ignored(self):
        s = obs_scenes.SceneSwitcher(FakeClient(), self.SCENES)
        s.on_activity({"data": "not json"})
        self.assertIsNone(s.change("hangman"))   # still the first value

    def run_switch(self, client):
        s = obs_scenes.SceneSwitcher(client, self.SCENES)
        stop = threading.Event()
        t = threading.Thread(target=s.run, args=(stop,), daemon=True)
        t.start()
        s.on_activity(msg(None))
        s.on_activity(msg("hangman"))
        self.assertTrue(client.done.wait(3))
        stop.set()
        t.join(3)
        return client

    def test_thread_switches(self):
        self.assertEqual(self.run_switch(FakeClient()).scenes, ["Pendu"])

    def test_obs_error_does_not_stop(self):
        self.assertEqual(self.run_switch(FakeClient(fail=True)).scenes, ["Pendu"])


if __name__ == "__main__":
    unittest.main()
