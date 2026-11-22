"""Tests without network of the Twitch whispering account (twitch_auth.py).

    cd robotics/regie && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import twitch_auth as ta  # noqa: E402

REDIRECT = "https://regie.example.com/twitch/callback"


class FakeTwitch:
    """Answers the Twitch endpoints; records every call."""

    def __init__(self):
        self.calls = []
        self.token_status = 200
        self.scopes = [ta.SCOPE]
        self.validate_status = 200
        self.whisper_status = [204]
        self.issued = 0

    def __call__(self, method, url, *, params=None, form=None, json_body=None, headers=None, timeout=10):
        self.calls.append((method, url, params, form, json_body, headers))
        if url == ta.TOKEN_URL:
            if self.token_status != 200:
                return self.token_status, {"message": "Invalid refresh token"}
            self.issued += 1
            return 200, {"access_token": f"acc{self.issued}", "refresh_token": f"ref{self.issued}",
                         "expires_in": 14000}
        if url == ta.VALIDATE_URL:
            if self.validate_status != 200:
                return self.validate_status, {"message": "invalid access token"}
            return 200, {"login": "naobot", "user_id": "42", "scopes": self.scopes, "expires_in": 14000}
        if url == ta.WHISPERS_URL:
            return self.whisper_status.pop(0) if len(self.whisper_status) > 1 else self.whisper_status[0], {}
        if url == ta.REVOKE_URL:
            return 200, {}
        return 404, {}

    def urls(self):
        return [c[1] for c in self.calls]


class TestRedirectUri(unittest.TestCase):

    def test_normalized(self):
        expected = "https://regia.example.com/twitch/callback"
        for value in ("regia.example.com", "regia.example.com/", "https://regia.example.com",
                      "https://regia.example.com/", " https://regia.example.com/twitch/callback ",
                      "https://https://regia.example.com//twitch/callback",
                      "HTTPS://regia.example.com//twitch/callback/"):
            self.assertEqual(ta.redirect_uri(value), expected, value)

    def test_scheme_and_port_kept(self):
        self.assertEqual(ta.redirect_uri("localhost:8097", "http"), "http://localhost:8097/twitch/callback")
        self.assertEqual(ta.redirect_uri("http://localhost:8097/"), "http://localhost:8097/twitch/callback")
        self.assertEqual(ta.redirect_uri(""), "")


class TestTwitchAuth(unittest.TestCase):

    def setUp(self):
        self.now = [1000.0]
        self.fake = FakeTwitch()
        self.changes = []
        self.auth = ta.TwitchAuth("cid", "secret", on_change=self.changes.append,
                                  http=self.fake, clock=lambda: self.now[0], log=lambda _: None)

    def login(self):
        url = self.auth.login_url(REDIRECT)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.assertTrue(self.auth.known_state(state))
        return self.auth.finish_login(state, "code123")

    def test_not_connected_by_default(self):
        self.assertTrue(self.auth.configured)
        self.assertFalse(self.auth.connected)
        self.assertEqual(self.auth.whisper("7", "hi")[0], 503)
        self.assertFalse(ta.TwitchAuth("", "").configured)

    def test_login_url(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.auth.login_url(REDIRECT)).query)
        self.assertEqual(query["scope"], [ta.SCOPE])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertEqual(query["client_id"], ["cid"])
        self.assertEqual(query["force_verify"], ["true"])

    def test_login_then_whisper(self):
        self.assertTrue(self.login())
        self.assertEqual(self.auth.login, "naobot")
        self.assertTrue(self.changes[-1]["connected"])
        token_call = self.fake.calls[0]
        self.assertEqual(token_call[3]["redirect_uri"], REDIRECT)
        self.assertEqual(token_call[3]["grant_type"], "authorization_code")
        self.assertEqual(self.auth.whisper("7", "hello"), (200, ""))
        method, url, params, _, body, headers = self.fake.calls[-1]
        self.assertEqual(params, {"from_user_id": "42", "to_user_id": "7"})
        self.assertEqual(body, {"message": "hello"})
        self.assertEqual(headers["Authorization"], "Bearer acc1")

    def test_unknown_or_expired_state(self):
        self.assertFalse(self.auth.known_state("forged"))
        url = self.auth.login_url(REDIRECT)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.now[0] += ta.STATE_TTL_S + 1
        self.assertFalse(self.auth.known_state(state))
        self.assertFalse(self.auth.finish_login(state, "code"))
        self.assertNotIn(ta.TOKEN_URL, self.fake.urls())

    def test_state_used_once(self):
        url = self.auth.login_url(REDIRECT)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.assertTrue(self.auth.finish_login(state, "code"))
        self.assertFalse(self.auth.known_state(state))

    def test_denied_on_twitch(self):
        url = self.auth.login_url(REDIRECT)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.assertFalse(self.auth.finish_login(state, "", "access_denied"))
        self.assertIn("access_denied", self.auth.error)

    def test_missing_scope(self):
        self.fake.scopes = ["chat:read"]
        self.assertFalse(self.login())
        self.assertIn(ta.SCOPE, self.auth.error)

    def test_refresh_before_expiry(self):
        self.login()
        self.now[0] += 14000 - ta.REFRESH_EARLY_S + 1
        self.auth.maintain()
        self.assertEqual(self.auth._access, "acc2")
        refresh = [c for c in self.fake.calls if c[3] and c[3].get("grant_type") == "refresh_token"]
        self.assertEqual(refresh[0][3]["refresh_token"], "ref1")
        self.assertTrue(self.auth.connected)

    def test_hourly_validation(self):
        self.login()
        count = self.fake.urls().count(ta.VALIDATE_URL)
        self.now[0] += 60
        self.auth.maintain()
        self.assertEqual(self.fake.urls().count(ta.VALIDATE_URL), count)
        self.now[0] += ta.VALIDATE_EVERY_S
        self.auth.maintain()
        self.assertEqual(self.fake.urls().count(ta.VALIDATE_URL), count + 1)

    def test_refresh_failure_disconnects(self):
        self.login()
        self.fake.token_status = 400
        self.now[0] += 14000
        self.auth.maintain()
        self.assertFalse(self.auth.connected)
        self.assertFalse(self.changes[-1]["connected"])
        self.assertTrue(self.changes[-1]["error"])
        self.assertEqual(self.auth.whisper("7", "hi")[0], 503)

    def test_whisper_401_refreshes_once(self):
        self.login()
        self.fake.whisper_status = [401, 204]
        self.assertEqual(self.auth.whisper("7", "hi"), (200, ""))
        self.assertEqual(self.fake.calls[-1][5]["Authorization"], "Bearer acc2")

    def test_whisper_error_passed_on(self):
        self.login()
        self.fake.whisper_status = [403]
        self.assertEqual(self.auth.whisper("7", "hi")[0], 502)

    def test_logout(self):
        self.login()
        self.auth.logout()
        self.assertFalse(self.auth.connected)
        self.assertIn(ta.REVOKE_URL, self.fake.urls())
        self.assertFalse(self.changes[-1]["connected"])


if __name__ == "__main__":
    unittest.main()
