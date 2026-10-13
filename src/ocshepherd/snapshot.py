"""Build a read-only snapshot of the opencode server state.

The snapshot is the shared currency between `ocshepherd snapshot` (produced
where the opencode server runs) and `ocshepherd watch` (which diffs successive
snapshots and emits alerts). It is plain JSON, so it can be piped over SSH.

Per session, the snapshot captures:
  - identity: id, parent (for sub-sessions), title, working directory
  - liveness: active flag, last update time
  - pending interactions: question forms, permission requests (full payload)
  - the tail of the conversation: last message type (+ outcome for turns),
    the latest assistant text snippet, and background shell status
    (a turn that ended while a background shell is still running will resume
    by itself when that shell exits).
"""

from __future__ import annotations

import time

DEFAULT_WINDOW_HOURS = 12
DEFAULT_MAX_SESSIONS = 30
MESSAGE_LOOKBACK = 6


def _texts_of(msg):
    out = []
    for c in msg.get("content") or []:
        if c.get("type") == "text" and c.get("text"):
            out.append(c["text"])
    return out


def _shell_ids_of(msg):
    ids = []
    md0 = msg.get("metadata")
    if isinstance(md0, dict) and md0.get("shellID"):
        ids.append(md0["shellID"])
    for c in msg.get("content") or []:
        if c.get("type") != "tool":
            continue
        state = c.get("state") if isinstance(c.get("state"), dict) else {}
        for md in (c.get("metadata"), state.get("metadata")):
            if isinstance(md, dict) and md.get("shellID"):
                ids.append(md["shellID"])
    return ids


def _form_item(f):
    fields = []
    for fd in f.get("fields") or []:
        item = {
            "key": fd.get("key"),
            "type": fd.get("type"),
            "title": fd.get("title") or "",
        }
        if fd.get("description"):
            item["description"] = fd["description"][:300]
        if fd.get("options"):
            item["options"] = [
                {"value": o.get("value"), "label": o.get("label")}
                for o in fd["options"]
            ]
        if "default" in fd:
            item["default"] = fd.get("default")
        fields.append(item)
    return {"id": f.get("id"), "title": f.get("title") or "", "fields": fields}


def _perm_item(p):
    return {
        "id": p.get("id"),
        "action": p.get("action") or "",
        "resources": [str(r)[:200] for r in (p.get("resources") or [])[:4]],
        "message": (p.get("message") or "")[:300],
    }


def build_snapshot(client,
                   window_hours=DEFAULT_WINDOW_HOURS,
                   max_sessions=DEFAULT_MAX_SESSIONS,
                   message_lookback=MESSAGE_LOOKBACK):
    now = int(time.time() * 1000)
    window_ms = window_hours * 3600 * 1000

    active = client.active()
    sessions = client.sessions(limit=60)
    have = set(s.get("id") for s in sessions)
    for sid in active:
        if sid not in have:
            s = client.session(sid)
            if s:
                sessions.insert(0, s)

    selected = {}
    for s in sessions:
        sid = s.get("id")
        if not sid or sid in selected:
            continue
        updated = (s.get("time") or {}).get("updated") or 0
        if sid in active or (now - updated) <= window_ms:
            selected[sid] = s
    ordered = sorted(
        selected.values(),
        key=lambda s: (s.get("time") or {}).get("updated") or 0,
        reverse=True,
    )[:max_sessions]

    out = []
    for s in ordered:
        sid = s["id"]
        directory = (s.get("location") or {}).get("directory") or ""
        item = {
            "id": sid,
            "parent": s.get("parentID") or "",
            "title": (s.get("title") or "").strip(),
            "dir": directory,
            "updated": (s.get("time") or {}).get("updated") or 0,
            "idle_ts": (s.get("time") or {}).get("idle") or 0,
            "active": sid in active,
            "outcome": s.get("outcome") or "",
        }
        forms = client.pending_form(sid)
        if forms:
            item["form"] = [_form_item(f) for f in forms]
        perms = client.pending_permissions(sid)
        if perms:
            item["perm"] = [_perm_item(p) for p in perms]

        msgs = client.messages(sid, limit=message_lookback)
        last = msgs[0] if msgs else None
        tail = {}
        if last:
            tail["type"] = last.get("type") or ""
            tail["ts"] = (last.get("time") or {}).get("created") or 0
            if last.get("outcome"):
                tail["outcome"] = last["outcome"]
            for m in msgs:
                if m.get("type") == "assistant":
                    text = " ".join(_texts_of(m)).strip()
                    if text:
                        tail["text"] = text[:500]
                    break
            shell_ids = []
            for m in msgs[:5]:
                shell_ids.extend(_shell_ids_of(m))
            if shell_ids:
                shell_ids = list(dict.fromkeys(shell_ids))
                running = []
                for shell_id in shell_ids:
                    try:
                        info = client.shell(shell_id, directory=directory)
                    except Exception:
                        info = {}
                    if info.get("status") == "running":
                        running.append(shell_id)
                tail["shells"] = {"ids": shell_ids, "running": running}
        item["last"] = tail
        out.append(item)

    return {
        "ts": now,
        "url": client.url,
        "active": sorted(active.keys()),
        "sessions": out,
    }
