"""Watch successive snapshots and emit alerts only for NEW events.

Alert semantics (deliberately conservative — silence is the norm):

  ❓  a session has a pending question form (the agent used the `question` tool)
  🔐  a session has a pending permission request
  ⏸️  a top-level session ended its turn and is waiting for input
  ⚠️  a top-level session ended its turn with outcome failed/interrupted,
      or the opencode server has been unreachable for several runs

Sessions waiting on a background shell are NOT reported: opencode resumes
them automatically when the shell exits. Sub-sessions never produce
"turn finished" alerts (their parent does); they only alert when they ask
something or request a permission.

State keeps track of already-reported events (by form/permission id and by
idle timestamp:outcome) so the same event is never reported twice. The first
run only seeds the state (existing idle turns are not reported), and
pending questions/permissions found on the first run ARE reported.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

STATE_VERSION = 1
DEFAULT_STATE = (
    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    / "ocshepherd"
    / "watch-state.json"
)
FRESH_GRACE_MS = 60_000
SNIPPET = 220
TELEGRAM_CHUNK = 3900


# -- state -----------------------------------------------------------------

def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("version", STATE_VERSION)
    st.setdefault("seen", {})
    st.setdefault("idle", {})
    st.setdefault("fails", 0)
    st.setdefault("fail_alert_ts", 0)
    st.setdefault("seeded", False)
    return st


def save_state(path, st):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def prune(st, now_ms):
    cutoff = now_ms - 14 * 86400000
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
    st["idle"] = {k: v for k, v in st["idle"].items() if v.get("seen", 0) >= cutoff}


# -- alerts ----------------------------------------------------------------

def fmt_form(form):
    parts = []
    title = (form.get("title") or "").strip()
    if title:
        parts.append(title)
    for fd in form.get("fields") or []:
        label = fd.get("title") or fd.get("key") or "?"
        line = "  • %s" % label
        options = fd.get("options") or []
        if options:
            line += " → " + " / ".join(
                str(o.get("label") or o.get("value")) for o in options
            )
        elif fd.get("type") not in (None, "string", "boolean"):
            line += " (%s)" % fd.get("type")
        if fd.get("default") not in (None, ""):
            line += " [default: %s]" % fd["default"]
        parts.append(line)
    return "\n".join(parts)[:600]


def check(snapshot, state, now_ms=None, snippet=SNIPPET):
    """Diff a snapshot against `state` (updated in place); return new alerts."""
    now = now_ms or snapshot.get("ts") or int(time.time() * 1000)
    seeding = not state.get("seeded")
    alerts = []

    for s in snapshot.get("sessions") or []:
        sid = s.get("id") or ""
        title = s.get("title") or "(untitled)"
        sub = " (sub-session)" if s.get("parent") else ""

        for form in s.get("form") or []:
            key = "form:" + str(form.get("id"))
            if key not in state["seen"]:
                state["seen"][key] = now
                alerts.append('❓ "%s"%s asks:\n%s' % (title, sub, fmt_form(form)))

        for perm in s.get("perm") or []:
            key = "perm:" + str(perm.get("id"))
            if key not in state["seen"]:
                state["seen"][key] = now
                resources = " | ".join(perm.get("resources") or [])[:180]
                line = '🔐 "%s"%s — permission requested: %s — %s' % (
                    title, sub, perm.get("action") or "?", resources,
                )
                if perm.get("message"):
                    line += "\n    %s" % perm["message"][:200]
                alerts.append(line)

        if s.get("parent") or s.get("active"):
            continue
        if s.get("form") or s.get("perm"):
            continue  # already surfaced above
        last = s.get("last") or {}
        if last.get("type") != "idle":
            continue
        if (last.get("shells") or {}).get("running"):
            continue  # waiting on a background job; will resume by itself
        ts = last.get("ts") or 0
        if now - ts < FRESH_GRACE_MS:
            continue  # transition may still be settling; re-check next run
        key = "%s:%s" % (ts, last.get("outcome"))
        current = state["idle"].get(sid)
        if current and current.get("k") == key:
            current["seen"] = now
            continue
        state["idle"][sid] = {"k": key, "seen": now}
        if seeding:
            continue
        text = (last.get("text") or "").strip()
        if last.get("outcome") in ("failed", "interrupted"):
            alerts.append('⚠️ "%s" — turn ended (%s).\n    %s' % (
                title, last.get("outcome"), text[:snippet]))
        else:
            tail = ("\n    Last: “%s”" % text[:snippet]) if text else ""
            alerts.append('⏸️ "%s" — turn finished, waiting for input.%s' % (title, tail))

    state["seeded"] = True
    prune(state, now)
    return alerts


def format_alerts(alerts, header="🐑 opencode-shepherd"):
    if not alerts:
        return ""
    body = "\n\n".join(alerts)
    footer = ("\n\n↩️ Reply with what to send back "
              "(or use `ocshepherd send` / `perm` / `form-reply`).")
    return "%s:\n\n%s%s" % (header, body, footer)


def fail_alert(state, message, max_fails=3, cooldown_hours=6, now_ms=None):
    """Count a failed run; return an alert string after max_fails consecutive
    failures (rate-limited to once per cooldown)."""
    now = now_ms or int(time.time() * 1000)
    state["fails"] = state.get("fails", 0) + 1
    if (state["fails"] >= max_fails
            and now - state.get("fail_alert_ts", 0) > cooldown_hours * 3600 * 1000):
        state["fail_alert_ts"] = now
        return "⚠️ opencode-shepherd: cannot reach the opencode server (%s)." % str(message)[:200]
    return None


def clear_fails(state):
    state["fails"] = 0


# -- notification sinks ----------------------------------------------------

def notify_stdout(text):
    if text:
        print(text)


def notify_telegram(text, token, chat_id):
    if not text:
        return
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    chunks = [text[i:i + TELEGRAM_CHUNK] for i in range(0, len(text), TELEGRAM_CHUNK)]
    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()


def notify_cmd(text, command):
    if not text:
        return
    subprocess.run(command, shell=True, input=text.encode(), timeout=60)
