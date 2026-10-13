"""HTTP client for the opencode server (v2 API), stdlib-only.

Auth model (matches `opencode serve`/`opencode web`): HTTP Basic with
username `opencode` (override with OPENCODE_SERVER_USERNAME) and a password
(OPENCODE_SERVER_PASSWORD for manually started servers; the background
service writes its own generated password to ~/.config/opencode/service.json).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:4096"
SERVICE_JSON = Path.home() / ".config" / "opencode" / "service.json"


class ShepherdError(Exception):
    """Raised when the opencode server cannot be reached or returns an error."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def discover_url() -> str:
    """Resolve the opencode server URL: env > `opencode service status` > default."""
    for env in ("OCSHEPHERD_URL", "OPENCODE_SERVER_URL"):
        v = os.environ.get(env)
        if v:
            return v.rstrip("/")
    if shutil.which("opencode"):
        try:
            out = subprocess.run(
                ["opencode", "service", "status"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            m = re.search(r"https?://\S+", out)
            if m:
                return m.group(0).rstrip("/")
        except Exception:
            pass
    return DEFAULT_URL


def discover_password():
    """Resolve the service password: explicit > OCSHEPHERD_PASSWORD >
    service.json (background service) > OPENCODE_SERVER_PASSWORD."""
    v = os.environ.get("OCSHEPHERD_PASSWORD")
    if v:
        return v
    try:
        with open(SERVICE_JSON) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("password"):
            return str(data["password"])
    except Exception:
        pass
    return os.environ.get("OPENCODE_SERVER_PASSWORD")


def match_session(sessions, fragment):
    """Match a session by id (exact or substring) or title substring.

    Returns (session, candidates): exactly one of the two is set.
    candidates is a (possibly empty) list of near matches when ambiguous/not found.
    """
    frag = fragment.lower()
    exact = [s for s in sessions if (s.get("id") or "").lower() == frag]
    if exact:
        return exact[0], None
    by_id = [s for s in sessions if frag in (s.get("id") or "").lower()]
    if len(by_id) == 1:
        return by_id[0], None
    if len(by_id) > 1:
        return None, by_id
    by_title = [s for s in sessions if frag in (s.get("title") or "").lower()]
    if len(by_title) == 1:
        return by_title[0], None
    if len(by_title) > 1:
        return None, by_title
    return None, []


class Client:
    """Minimal client for the opencode v2 HTTP API."""

    def __init__(self, url=None, password=None, username=None,
                 api_prefix="/api", timeout=20):
        self.url = (url or discover_url()).rstrip("/")
        self.username = (
            username
            or os.environ.get("OCSHEPHERD_USERNAME")
            or os.environ.get("OPENCODE_SERVER_USERNAME")
            or "opencode"
        )
        self.password = password if password is not None else discover_password()
        self.api_prefix = api_prefix.rstrip("/")
        self.timeout = timeout

    # -- low level ---------------------------------------------------------

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.password:
            token = base64.b64encode(
                ("%s:%s" % (self.username, self.password)).encode()
            ).decode()
            headers["Authorization"] = "Basic " + token
        return headers

    def request(self, method, path, params=None, data=None):
        if not path.startswith("/"):
            path = "/" + path
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        url = "%s%s%s%s" % (self.url, self.api_prefix, path, query)
        headers = self._headers()
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise ShepherdError(
                "HTTP %s for %s %s: %s" % (e.code, method, url, detail),
                status=e.code, body=detail,
            )
        except urllib.error.URLError as e:
            raise ShepherdError("Cannot reach opencode server at %s: %s" % (self.url, e.reason))

    # -- reads -------------------------------------------------------------

    def info(self):
        d = self.request("GET", "/info") or {}
        return d.get("data") if isinstance(d, dict) else d

    def active(self):
        d = self.request("GET", "/session/active") or {}
        return d.get("data") or {}

    def sessions(self, limit=60, order="desc"):
        d = self.request("GET", "/session", params={"limit": limit, "order": order}) or {}
        return d.get("data") or []

    def session(self, sid):
        d = self.request("GET", "/session/%s" % sid) or {}
        return d.get("data") or {}

    def messages(self, sid, limit=10, order="desc"):
        d = self.request(
            "GET", "/session/%s/message" % sid, params={"limit": limit, "order": order}
        ) or {}
        return d.get("data") or []

    def pending_form(self, sid):
        d = self.request("GET", "/session/%s/form" % sid) or {}
        return d.get("data") or []

    def pending_permissions(self, sid):
        d = self.request("GET", "/session/%s/permission" % sid) or {}
        return d.get("data") or []

    def shell(self, shell_id, directory=None):
        params = {"location[directory]": directory} if directory else None
        d = self.request("GET", "/shell/%s" % shell_id, params=params) or {}
        return d.get("data") or {}

    def resolve(self, fragment):
        """Resolve a session fragment against the recent session list."""
        sessions = self.sessions(limit=80)
        return match_session(sessions, fragment), sessions

    # -- writes ------------------------------------------------------------

    def send(self, sid, text, delivery=None):
        body = {"text": text}
        if delivery:
            body["delivery"] = delivery
        d = self.request("POST", "/session/%s/prompt" % sid, data=body) or {}
        return d.get("data") or {}

    def reply_permission(self, sid, request_id, decision, message=None):
        body = {"decision": decision}
        if message:
            body["message"] = message
        return self.request(
            "POST", "/session/%s/permission/%s/reply" % (sid, request_id), data=body
        )

    def answer_form(self, sid, form_id, answer):
        return self.request(
            "POST", "/session/%s/form/%s/reply" % (sid, form_id),
            data={"answer": answer},
        )


def format_ms(ms):
    if not ms:
        return "?"
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000.0))
