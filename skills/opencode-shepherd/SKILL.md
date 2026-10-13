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
ocshepherd new --dir DIR [--title T] [--prompt M]   # create a session
ocshepherd abort <frag>            # interrupt a running session
ocshepherd close <frag> [--yes]    # delete a session
ocshepherd config list|get|set|unset ...            # persistent settings
```

`<frag>` = session id fragment (case-insensitive substring) or title substring.
Ambiguous -> prints candidates, exit 2. No match -> exit 3.

## Watch mode (notifications)

```bash
ocshepherd watch                                          # one-shot; silent unless something new
ocshepherd watch --loop 120                               # polling loop
ocshepherd watch --events                                 # event-driven (long-running, light)
ssh box ocshepherd snapshot | ocshepherd watch --stdin    # remote over SSH
```

Settings (optional, live-reloaded from `~/.config/ocshepherd/config.json`):
`enabled` (pause/resume notifications; on resume a recap lists what is
waiting), `remind_after_minutes` (re-notify while something waits),
`telegram_token`/`telegram_chat`, `notify_cmd`, `resync_minutes`,
`min_interval_seconds`; manage with `ocshepherd config set KEY VALUE`.

### Setting it up where opencode runs (for the agent)

The CLI must run on a machine that can reach the opencode server (localhost
or via SSH):

1. Check: `ocshepherd --version` on the host where opencode runs
   (`ssh HOST ocshepherd --version` for remote setups).
2. If missing, install it there:
   `pipx install git+https://github.com/okazakee/opencode-shepherd`
   (or `python3 -m pip install --user` as fallback); verify with `ocshepherd ping`.
3. Wire up watching:
   - remote setup: `ssh HOST ocshepherd snapshot | ocshepherd watch --stdin` (cron/timer here),
   - local setup: `ocshepherd watch` in cron, or a long-running
     `ocshepherd watch --events` under systemd (see `examples/systemd/`).
4. Pause/steer anytime: `ocshepherd config set enabled false|true`.

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

Auto-discovered, in order. URL: `--url` → `$OCSHEPHERD_URL` →
`opencode service status` → `http://127.0.0.1:4096`. Password: `--password` →
`$OCSHEPHERD_PASSWORD` → `~/.config/opencode/service.json` →
`$OPENCODE_SERVER_PASSWORD`. Username defaults to `opencode`.
Never print the password; never bind the opencode server beyond localhost.

## Pitfalls

- `send` steers by default: the message is delivered immediately, even
  mid-turn. Use `--queue` to deliver after the current turn ends. Pick per
  the user's intent.
- `perm once/always` and question answers are real approvals; relay exactly
  what the user decided, nothing more.
- Prefer `resolve` before acting when the fragment could be ambiguous.
