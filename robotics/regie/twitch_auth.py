"""
twitch_auth — the Twitch account that whispers the "!rgpd" answers, connected
from the control room (OAuth authorization code flow, scope user:manage:whispers).

No token in the environment: at each start of the system, the "Connecter
Twitch" button of the régie page logs in with the account that sends the
whispers.  The access token and the refresh token stay in memory only: a
background loop (maintain) refreshes the access token before it expires and
validates it every hour, as Twitch requires.  When the régie stops, the
connection is gone and has to be made again.

twitch-input holds no Twitch token: it asks the régie to whisper
(POST /twitch/whisper).  Not connected = only "!rgpd" is off, the rest works.

Only the standard library: the HTTP calls go through `http_call`, replaced by a
fake in the tests.
"""

import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL     = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL  = "https://id.twitch.tv/oauth2/validate"
REVOKE_URL    = "https://id.twitch.tv/oauth2/revoke"
WHISPERS_URL  = "https://api.twitch.tv/helix/whispers"
SCOPE = "user:manage:whispers"

STATE_TTL_S    = 600      # time allowed to log in on Twitch after the click
REFRESH_EARLY_S = 600     # refresh the access token this long before it expires
VALIDATE_EVERY_S = 3600   # Twitch asks apps to validate their tokens every hour


CALLBACK_PATH = "/twitch/callback"


def redirect_uri(value: str, default_scheme: str = "https") -> str:
    """Twitch redirect URL from a domain or a URL, however it was typed:
    "regia.example.com", "https://regia.example.com/", even
    "https://https://regia.example.com//twitch/callback"
    → "https://regia.example.com/twitch/callback"."""
    rest, scheme = value.strip(), default_scheme
    while True:
        head, sep, tail = rest.partition("://")
        if not sep or head.lower() not in ("http", "https"):
            break
        scheme, rest = head.lower(), tail
    parts = [p for p in rest.split("/") if p]
    if not parts:
        return ""
    path = "/" + "/".join(parts[1:])
    if not path.rstrip("/").endswith(CALLBACK_PATH):
        path = path.rstrip("/") + CALLBACK_PATH
    return f"{scheme}://{parts[0]}{path}"


def http_call(method: str, url: str, *, params: dict | None = None, form: dict | None = None,
              json_body: dict | None = None, headers: dict | None = None,
              timeout: float = 10) -> tuple[int, dict]:
    """(status, JSON body or {}); status 0 when Twitch could not be reached."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return 0, {"message": str(e)}
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {"message": raw[:200].decode("utf-8", "replace")}
    return status, body if isinstance(body, dict) else {}


class TwitchAuth:
    """Tokens of the whispering account, in memory only.  Thread-safe."""

    def __init__(self, client_id: str, client_secret: str, on_change=None,
                 http=http_call, clock=time.time, log=print):
        self.client_id = client_id
        self.client_secret = client_secret
        self._on_change = on_change or (lambda status: None)
        self._http = http
        self._clock = clock
        self._log = log
        self._lock = threading.RLock()
        self._states: dict[str, tuple[float, str]] = {}   # state → (deadline, redirect_uri)
        self._clear()
        self.error = ""

    # ── State ────────────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return bool(self._access)

    def _clear(self):
        self._access = ""
        self._refresh = ""
        self._expires_at = 0.0
        self._validated_at = 0.0
        self._scopes: list[str] = []
        self.login = ""
        self.user_id = ""

    def status(self) -> dict:
        with self._lock:
            return {"kind": "twitch", "t": round(self._clock(), 3), "configured": self.configured,
                    "connected": self.connected, "login": self.login, "error": self.error}

    def _changed(self):
        self._on_change(self.status())

    # ── Login / logout ───────────────────────────────────────────────────────

    def login_url(self, redirect_uri: str) -> str:
        """Twitch authorize page; the state proves the callback follows this click."""
        now = self._clock()
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._states = {s: v for s, v in self._states.items() if v[0] > now}
            self._states[state] = (now + STATE_TTL_S, redirect_uri)
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": SCOPE, "state": state,
            # Always show the Twitch page, to pick the whispering account
            # even when the browser is logged in with another one.
            "force_verify": "true",
        })

    def known_state(self, state: str) -> bool:
        with self._lock:
            entry = self._states.get(state)
            return bool(entry) and entry[0] > self._clock()

    def finish_login(self, state: str, code: str, error: str = "") -> bool:
        """Callback from Twitch: exchange the code for tokens.  False = not connected
        (the reason is in self.error).  The caller checks known_state first."""
        with self._lock:
            entry = self._states.pop(state, None)
            if not entry or entry[0] <= self._clock():
                self.error = "connexion expirée, recommence"
            elif error or not code:
                self.error = f"refusé sur Twitch ({error or 'pas de code'})"
            else:
                status, body = self._http("POST", TOKEN_URL, form={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "code": code, "grant_type": "authorization_code", "redirect_uri": entry[1],
                })
                if status == 200 and body.get("access_token"):
                    self._store(body)
                    if not self._validate():
                        self._clear()
                        self.error = "jeton refusé par Twitch juste après la connexion"
                    elif SCOPE not in self._scopes:
                        self._clear()
                        self.error = f"le scope {SCOPE} n'a pas été accordé"
                    else:
                        self.error = ""
                        self._log(f"Twitch connected as {self.login} (whispers for !rgpd)")
                else:
                    self.error = f"échange du code refusé ({status}) {body.get('message', '')}".strip()
            if self.error:
                self._log(f"Twitch login failed: {self.error}")
            self._changed()
            return self.connected

    def logout(self):
        with self._lock:
            access = self._access
            self._clear()
            self.error = ""
            self._changed()
        if access:
            self._http("POST", REVOKE_URL, form={"client_id": self.client_id, "token": access})
            self._log("Twitch disconnected: !rgpd is off")

    # ── Tokens ───────────────────────────────────────────────────────────────

    def _store(self, body: dict):
        self._access = body["access_token"]
        self._refresh = body.get("refresh_token", self._refresh)
        self._expires_at = self._clock() + float(body.get("expires_in") or 3600)

    def _validate(self) -> bool:
        """GET /oauth2/validate: account login and id, granted scopes.  False = token dead."""
        status, body = self._http("GET", VALIDATE_URL,
                                  headers={"Authorization": f"OAuth {self._access}"})
        if status != 200:
            return False
        self.login = body.get("login", "")
        self.user_id = body.get("user_id", "")
        self._scopes = body.get("scopes") or []
        self._validated_at = self._clock()
        if body.get("expires_in"):
            self._expires_at = self._clock() + float(body["expires_in"])
        return True

    def _do_refresh(self) -> bool:
        """New access token from the refresh token; failure = disconnected."""
        status, body = self._http("POST", TOKEN_URL, form={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": self._refresh,
        }) if self._refresh else (0, {"message": "pas de refresh token"})
        if status == 200 and body.get("access_token"):
            self._store(body)
            self._validated_at = self._clock()
            return True
        self._clear()
        self.error = f"session Twitch perdue ({status}), reconnecte-toi"
        self._log(f"Twitch refresh failed ({status}) {body.get('message', '')}: !rgpd is off")
        self._changed()
        return False

    def maintain(self):
        """Called every few seconds: refresh before expiry, validate every hour."""
        with self._lock:
            if not self.connected:
                return
            now = self._clock()
            if self._expires_at - now < REFRESH_EARLY_S:
                self._do_refresh()
            elif now - self._validated_at > VALIDATE_EVERY_S:
                if not self._validate():
                    self._do_refresh()

    # ── Whisper ──────────────────────────────────────────────────────────────

    def whisper(self, to_user_id: str, message: str) -> tuple[int, str]:
        """(HTTP status for twitch-input, error text).  503 = not connected."""
        for attempt in range(2):
            with self._lock:
                if not self.connected:
                    return 503, "Twitch non connecté dans la régie"
                headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self._access}"}
                sender = self.user_id
            status, body = self._http(
                "POST", WHISPERS_URL, params={"from_user_id": sender, "to_user_id": to_user_id},
                headers=headers, json_body={"message": message})
            if status == 204:
                return 200, ""
            if status == 401 and attempt == 0:
                with self._lock:
                    if not self._do_refresh():
                        return 503, "Twitch non connecté dans la régie"
                continue
            return 502, f"Twitch {status} {body.get('message', '')}".strip()
        return 502, "Twitch 401"
