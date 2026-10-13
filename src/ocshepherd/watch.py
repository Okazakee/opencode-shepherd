"""Watch successive snapshots and emit alerts only for NEW events.

Alert semantics (deliberately conservative — silence is the norm):

  ❓  a session has a pending question form (the agent used the `question` tool)
  🔐  a session has a pending permission request
  ⏸️  a top-level session ended its turn and is waiting for input
  ⚠️  a top-level session ended its turn with outcome failed/interrupted,
      or the opencode server has been unreachable for several runs
  ⏰  a reminder: an item above is still waiting after `remind_after_minutes`
  ▶️  recap after re-enabling notifications: what is waiting right now

Sessions waiting on a background shell are NOT reported: opencode resumes
them automatically when the shell exits. Sub-sessions never produce
"turn finished" alerts (their parent does); they only alert when they ask
something or request a permission.

State (dedupe by form/permission id and by idle timestamp:outcome) guarantees
the same event is never reported twice. The first run only seeds the idle
state (existing idle turns are not reported; pending questions/permissions
ARE reported). While notifications are disabled (`ocshepherd config set
enabled false`) events are recorded silently; when re-enabled, a one-time
recap lists what is waiting.

Event-driven mode (`run_events`): instead of periodic polling, a background
SSE connection to the opencode event bus wakes an evaluation on turn and
permission/form events, plus a periodic full resync as a safety net.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .client import EventStream
from .snapshot import build_snapshot

STATE_VERSION = 2
DEFAULT_STATE = (
    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    / "ocshepherd"
    / "watch-state.json"
)
FRESH_GRACE_MS = 60_000
SNIPPET = 220
TELEGRAM_CHUNK = 3900
DEBOUNCE_SECONDS = 2
FOLLOWUP_SECONDS = 65  # FRESH_GRACE + margin

# Event types that wake an evaluation in --events mode (prefix match).
TRIGGER_PREFIXES = (
    "session.idle",
    "session.error",
    "session.execution.failed",
    "session.execution.interrupted",
    "session.execution.succeeded",
    "session.compaction.failed",
    "permission.",
    "form.",
    "session.form.",
)
RESYNC_TRIGGER = "server.connected"
DISCONNECT = "_disconnect"


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
    st.setdefault("open", {})
    st.setdefault("was_enabled", None)
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


# -- formatting ------------------------------------------------------------

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


def _form_payload(s, form):
    return {
        "kind": "form",
        "sid": s.get("id"),
        "title": s.get("title") or "(untitled)",
        "sub": " (sub-session)" if s.get("parent") else "",
        "body": fmt_form(form),
    }


def _perm_payload(s, perm):
    resources = " | ".join(perm.get("resources") or [])[:180]
    body = "%s — %s" % (perm.get("action") or "?", resources)
    if perm.get("message"):
        body += "\n    %s" % perm["message"][:200]
    return {
        "kind": "perm",
        "sid": s.get("id"),
        "title": s.get("title") or "(untitled)",
        "sub": " (sub-session)" if s.get("parent") else "",
        "body": body,
    }


def _initial_line(payload):
    kind = payload["kind"]
    title = payload["title"]
    sub = payload.get("sub", "")
    body = payload.get("body", "")
    if kind == "form":
        return '❓ "%s"%s asks:\n%s' % (title, sub, body)
    if kind == "perm":
        return '🔐 "%s"%s — permission requested: %s' % (title, sub, body)
    if kind == "idle":
        tail = ("\n    Last: “%s”" % body[:220]) if body else ""
        return '⏸️ "%s" — turn finished, waiting for input.%s' % (title, tail)
    if kind == "idle-error":
        return '⚠️ "%s" — turn ended (%s).\n    %s' % (title, payload.get("outcome"), body[:220])
    return title


def _reminder_line(payload, now):
    since = payload.get("since") or payload.get("last") or now
    mins = max(1, int((now - since) / 60000))
    title = payload.get("title") or ""
    body = payload.get("body") or ""
    if payload.get("kind") == "idle":
        tail = ("\n    Last: “%s”" % body[:160]) if body else ""
        return '⏰ Still waiting (%dm): "%s" — turn finished.%s' % (mins, title, tail)
    return '⏰ Still waiting (%dm): "%s"\n%s' % (mins, title, body)


def _summary_line(payload, now):
    since = payload.get("since") or payload.get("last") or now
    mins = max(0, int((now - since) / 60000))
    labels = {"form": "question", "perm": "permission",
              "idle": "waiting", "idle-error": "error"}
    return '• [%s] "%s" (%dm)' % (
        labels.get(payload.get("kind"), payload.get("kind")),
        payload.get("title") or "", mins,
    )


def format_alerts(alerts, header="🐑 opencode-shepherd"):
    if not alerts:
        return ""
    body = "\n\n".join(alerts)
    footer = ("\n\n↩️ Reply with what to send back "
              "(or use `ocshepherd send` / `perm` / `form-reply`).")
    return "%s:\n\n%s%s" % (header, body, footer)


# -- the check -------------------------------------------------------------

def check(snapshot, state, now_ms=None, snippet=SNIPPET, remind_after_minutes=None,
          enabled=True, meta=None):
    """Diff a snapshot against `state` (updated in place); return new alerts.

    `meta` (optional dict) receives event-mode bookkeeping:
      deferred_idle     True when an idle transition was too fresh to alert
      next_reminder_ms  earliest due timestamp among waiting reminders
    """
    now = now_ms or snapshot.get("ts") or int(time.time() * 1000)
    seeding = not state.get("seeded")
    resumed = enabled and state.get("was_enabled") is False
    state["was_enabled"] = enabled
    alerts = []
    alerted_keys = set()
    current = {}

    for s in snapshot.get("sessions") or []:
        sid = s.get("id") or ""
        title = s.get("title") or "(untitled)"

        for form in s.get("form") or []:
            key = "form:" + str(form.get("id"))
            payload = _form_payload(s, form)
            current[key] = payload
            if key not in state["seen"]:
                state["seen"][key] = now
                if enabled:
                    alerts.append(_initial_line(payload))
                    alerted_keys.add(key)

        for perm in s.get("perm") or []:
            key = "perm:" + str(perm.get("id"))
            payload = _perm_payload(s, perm)
            current[key] = payload
            if key not in state["seen"]:
                state["seen"][key] = now
                if enabled:
                    alerts.append(_initial_line(payload))
                    alerted_keys.add(key)

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
        outcome = last.get("outcome") or ""
        payload = {
            "kind": "idle" if outcome == "succeeded" else "idle-error",
            "sid": sid,
            "title": title,
            "outcome": outcome,
            "body": (last.get("text") or "").strip(),
            "k": "%s:%s" % (ts, outcome),
        }
        current["idle:" + sid] = payload
        if now - ts < FRESH_GRACE_MS:
            if meta is not None:
                meta["deferred_idle"] = True
            continue  # not settled yet; re-check next run
        cur = state["idle"].get(sid)
        if cur and cur.get("k") == payload["k"]:
            cur["seen"] = now
            continue
        state["idle"][sid] = {"k": payload["k"], "seen": now}
        if enabled and not seeding:
            alerts.append(_initial_line(payload))
            alerted_keys.add("idle:" + sid)

    # reminders + open-item bookkeeping
    new_open = {}
    next_reminder_ms = None
    remind_ms = remind_after_minutes * 60000 if remind_after_minutes else None
    for key, payload in current.items():
        prev = state["open"].get(key)
        if prev and prev.get("k") != payload.get("k"):
            prev = None  # a new event on this session resets the clock
        entry = dict(prev) if prev else {"since": now}
        entry.update(payload)
        entry.setdefault("last", now)
        if prev and remind_ms and enabled and not seeding:
            due = entry["last"] + remind_ms
            if now >= due:
                alerts.append(_reminder_line(entry, now))
                entry["last"] = now
        if prev and remind_ms and enabled:
            due = entry["last"] + remind_ms
            if due <= now:
                due = now + 60_000  # due but suppressed (e.g. seeding); retry shortly
            if next_reminder_ms is None or due < next_reminder_ms:
                next_reminder_ms = due
        new_open[key] = entry
    state["open"] = new_open
    if meta is not None:
        meta["next_reminder_ms"] = next_reminder_ms

    # recap on the disabled -> enabled transition
    if resumed and not seeding:
        lines = [_summary_line(e, now) for key, e in new_open.items()
                 if key not in alerted_keys]
        if lines:
            alerts.insert(0, "▶️ shepherd resumed — waiting on you:\n" + "\n".join(lines))
        for entry in new_open.values():
            entry["last"] = now

    state["seeded"] = True
    prune(state, now)
    return alerts


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


# -- event-driven loop -----------------------------------------------------

def run_events(client, get_settings, emit):
    """Event-driven watch loop.

    get_settings() returns the merged settings dict (re-read periodically so
    `ocshepherd config set ...` applies live); emit(text) delivers an alert.
    """
    stream = EventStream(client)
    stream.start()
    settings = get_settings()
    last_check = 0.0
    last_resync = time.time()
    last_cfg = time.time()
    pending_since = None
    followup_at = None
    next_reminder = None
    retry_at = None

    while True:
        now = time.time()
        if now - last_cfg > 5:
            settings = get_settings()
            last_cfg = now

        ev = stream.get(timeout=1.0)
        if ev is not None:
            et = ev.get("type") or ""
            if et == DISCONNECT or et == RESYNC_TRIGGER or et.startswith(TRIGGER_PREFIXES):
                pending_since = pending_since or now

        if now - last_resync >= settings["resync_minutes"] * 60:
            pending_since = pending_since or now
        if followup_at is not None and now >= followup_at:
            pending_since = pending_since or now
            followup_at = None
        if next_reminder is not None and now >= next_reminder:
            pending_since = pending_since or now
        if retry_at is not None:
            if now >= retry_at:
                pending_since = pending_since or now
                retry_at = None
            else:
                pending_since = None  # inside the retry backoff window
                continue

        if pending_since is None:
            continue
        if now - pending_since < DEBOUNCE_SECONDS:
            continue
        if now - last_check < settings["min_interval_seconds"]:
            continue

        pending_since = None
        last_check = now
        last_resync = now
        state = load_state(settings["state"])
        meta = {}
        try:
            snap = build_snapshot(client)
            clear_fails(state)
            alerts = check(
                snap, state,
                snippet=settings["snippet"],
                remind_after_minutes=settings["remind"],
                enabled=settings["enabled"],
                meta=meta,
            )
            if meta.get("deferred_idle"):
                followup_at = time.time() + FOLLOWUP_SECONDS
            reminder_ms = meta.get("next_reminder_ms")
            next_reminder = (reminder_ms / 1000.0) if reminder_ms else None
        except Exception as e:
            alert = fail_alert(
                state, e,
                max_fails=settings.get("max_fails", 3),
                cooldown_hours=settings.get("fail_cooldown_hours", 6),
            )
            alerts = [alert] if alert else []
            next_reminder = None
            retry_at = time.time() + 30
        text = format_alerts(alerts)
        delivered = True
        if text:
            try:
                emit(text)
            except Exception as e:
                # never lose an alert to a failing sink: skip saving the
                # state so the next run re-detects the events and retries
                delivered = False
                retry_at = time.time() + 60
                print("ocshepherd: emit failed: %s" % e, file=sys.stderr)
        if delivered:
            save_state(settings["state"], state)


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
