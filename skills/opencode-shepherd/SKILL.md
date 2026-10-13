---
name: opencode-shepherd
description: "Use when monitoring or controlling opencode sessions."
---

# opencode-shepherd

`ocshepherd` is a dependency-free CLI that talks to the local opencode server
(the same API the TUI and web UI use) to watch sessions and act on their needs:
status, alerts when a session stops or asks something, and replies.

## When to Use

- The user asks about their opencode sessions ("what's my session doing?",
  "did it finish?", "is anything waiting on me?").
- An alert reports a session stopped / asked a question / requests a
  permission, and the user tells you how to answer it.
- You need to set up or debug the watch pipeline (cron, systemd, notifications).

## Prerequisite check

```bash
ocshepherd ping     # auto-discovers URL + password; prints the active session count
```

If `ocshepherd` is not installed:

```bash
pipx install git+https://github.com/okazakee/opencode-shepherd
```

## Core commands

```bash
ocshepherd list                    # recent sessions (RUNNING / child flags)
ocshepherd tail <frag> 20          # last messages of a session
ocshepherd send <frag> "text..."   # steer into the current turn (--queue to wait)
ocshepherd forms <frag>            # pending questions
ocshepherd form-reply <frag> --answer q0="value"
ocshepherd perms <frag>            # pending permission requests
ocshepherd perm <frag> once|always|reject [--message "..."]
ocshepherd resolve <frag>          # disambiguate a session by id/title fragment
```

`<frag>` = session id fragment (case-insensitive substring) or title substring.
Ambiguous -> prints candidates, exit 2. No match -> exit 3.

## Watch mode (notifications)

```bash
ocshepherd watch                                          # one-shot; silent unless something new
ocshepherd watch --loop 120                               # long-running
ssh box ocshepherd snapshot | ocshepherd watch --stdin    # remote over SSH
```

Sinks: stdout (default), `--telegram-token` + `--telegram-chat`, or
`--notify-cmd` (alert text on stdin; works with ntfy, webhooks, mail...).
Dedupe state lives in `~/.local/state/ocshepherd/watch-state.json` (`--state` to override).

Alert semantics (why silence is the norm):

- ⏸️ turn finished / ❓ pending question / 🔐 pending permission / ⚠️ failure
  or repeated connectivity loss.
- A session waiting on a background shell is NOT reported: opencode resumes
  it automatically when the shell exits. Do not treat it as blocked.
- Sub-sessions only alert for questions/permissions, never for "turn finished"
  (their parent reports that).
- First run seeds silently; however, pending questions/permissions found on
  the first run are reported.

## Credentials

Auto-discovered, in order — URL: `--url` → `$OCSHEPHERD_URL` →
`opencode service status` → `http://127.0.0.1:4096`. Password: `--password` →
`$OCSHEPHERD_PASSWORD` → `~/.config/opencode/service.json` →
`$OPENCODE_SERVER_PASSWORD`. Username defaults to `opencode`.
Never print the password; never bind the opencode server beyond localhost.

## Pitfalls

- `send` steers by default: the message is delivered immediately, even
  mid-turn. Use `--queue` to deliver after the current turn ends. Pick per
  the user's intent.
- `perm once/always` and question answers are real approvals — relay exactly
  what the user decided, nothing more.
- Prefer `resolve` before acting when the fragment could be ambiguous.
