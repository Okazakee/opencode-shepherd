# 🐑 opencode-shepherd

**Keep an eye on your [opencode](https://opencode.ai) sessions from anywhere: know when they stop, ask a question or need a permission, and answer them without a UI.**

A plugin for [Hermes](https://github.com/NousResearch/hermes-agent) with a tiny, dependency-free Python CLI (standard library only) underneath. Works locally, over SSH, and completely on its own.

```text
   sessions on your dev box              you, anywhere
  ┌────────────────────────┐          ┌────────────────────────────┐
  │ opencode server (v2)   │   ssh    │ ocshepherd watch           │
  │ (same API as the TUI)  │ ◄──────► │   → Telegram / terminal    │
  └────────────────────────┘          │ ocshepherd send / perm / … │
                                      └────────────────────────────┘
```

## Why

Long agent sessions behave like a slow colleague: they finish a step and wait, they ask before doing something risky, they block on a question. If opencode runs on another machine you find out too late, and a notification you can't reply to doesn't help. `ocshepherd` does both halves:

1. **Watch**: tells you only when something **new** needs attention.
2. **Reply**: send messages, answer questions, allow or deny permissions, from wherever you are.

## Install

As a Hermes plugin (skills-only: Hermes loads no Python from it; your agent learns how to set up and use the CLI for you):

```bash
hermes plugins install okazakee/opencode-shepherd
```

The standalone CLI (source in [`cli/`](cli/)) goes on the machine that can reach the opencode server:

```bash
pipx install "git+https://github.com/okazakee/opencode-shepherd#subdirectory=cli"   # recommended
# or
python3 -m pip install --user "git+https://github.com/okazakee/opencode-shepherd#subdirectory=cli"
```

- Requires **Python ≥ 3.9** and a running **opencode v2** (`opencode serve`, the background service, or a TUI; tested with v2.0.28).
- Credentials: URL auto-detected, password via env or flag (see [Good to know](#good-to-know)).

## 30-second tour

```bash
ocshepherd ping                 # is the server reachable?
ocshepherd list                 # your recent sessions
ocshepherd tail fix-auth 20     # what happened lately in a session
ocshepherd send fix-auth "also add a test for the empty case"
ocshepherd watch                # one check; silent if nothing new
```

## Get notified (`watch`)

Schedule `watch` (every couple of minutes is a good default); it stays quiet unless something new happens.

| Alert | Meaning |
|---|---|
| ⏸️ | a session finished its turn and waits for input |
| ❓ | a session is asking a question |
| 🔐 | a session requests a permission |
| ⚠️ | a turn failed, or the server has been unreachable for several checks |

```bash
# cron: local machine
*/2 * * * * /usr/local/bin/ocshepherd watch

# cron: Telegram notifications, silent stdout
*/2 * * * * OCSHEPHERD_TELEGRAM_TOKEN=... OCSHEPHERD_TELEGRAM_CHAT=... /usr/local/bin/ocshepherd watch --quiet

# cron: remote machine over SSH (snapshot remotely, diff + notify locally)
*/2 * * * * ssh devbox ocshepherd snapshot | ocshepherd watch --stdin --quiet
```

- Notifications go to **stdout**, **Telegram** (`--telegram-token/--telegram-chat`), or **any command** (`--notify-cmd`, alert text on stdin; works with ntfy, webhooks, mail...).
- `--loop 120` keeps it running (polling), or use **event-driven mode**: `ocshepherd watch --events` streams the opencode event bus and only runs a check when something actually happens (turn boundaries, errors, permissions, questions), plus a periodic safety-net resync. Lighter and faster than polling.
- Examples in [`examples/`](examples/): crontab, systemd unit + timer, event-daemon unit, ntfy wrapper.
- It knows the difference between "waiting for you" and "waiting for a background job": the latter is never reported, because opencode resumes those sessions by itself.

### Reminders

Don't want a question to sit unanswered for hours? Set a reminder interval and `watch` will re-notify while an item is still waiting:

```bash
ocshepherd config set remind_after_minutes 30   # reminders every 30 minutes while waiting
```

### Pause / resume notifications

```bash
ocshepherd config set enabled false   # pause: events are recorded silently
ocshepherd config set enabled true    # resume: one recap of what's waiting right now
```

## Configuration

Settings live in `~/.config/ocshepherd/config.json` (every key optional, written with mode `0600`) and are read live, no restart needed:

```bash
ocshepherd config list
ocshepherd config set telegram_token 123:ABC
ocshepherd config unset remind_after_minutes
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | master switch; while off, events are recorded silently, with a recap when re-enabled |
| `remind_after_minutes` | `null` | re-notify every N minutes while an item is still waiting |
| `snippet` | `220` | max chars of last-message snippets |
| `state` | XDG state | dedupe state file path |
| `telegram_token` / `telegram_chat` | `null` | default Telegram sink |
| `notify_cmd` | `null` | command receiving alerts on stdin |
| `resync_minutes` | `30` | `--events`: full-check safety net interval |
| `min_interval_seconds` | `15` | `--events`: minimum gap between checks |

## Reply to a session

```bash
ocshepherd send fix-auth "text..."          # delivered immediately (steer)
ocshepherd send fix-auth "text..." --queue  # delivered after the current turn
ocshepherd send fix-auth --file prompt.md   # multiline
cat prompt.md | ocshepherd send fix-auth --stdin

ocshepherd forms fix-auth                   # pending questions
ocshepherd form-reply fix-auth --answer q0="yes"

ocshepherd perms fix-auth                   # pending permissions
ocshepherd perm fix-auth once               # once | always | reject

ocshepherd resolve fix-auth                 # what does this fragment match?
```

Sessions are addressed by a **fragment**: part of the session id or part of the title (case-insensitive). Ambiguous fragments print the candidates instead of guessing.

## Sessions (create, abort, close)

```bash
ocshepherd new --dir ~/work/proj --title "notes" --prompt "summarise this repo"
ocshepherd abort fix-auth        # interrupt a running turn
ocshepherd close fix-auth        # show what would be deleted
ocshepherd close fix-auth --yes  # actually delete the session (and its children)
```

## Over SSH (remote machines)

Snapshots are plain JSON, so you can pipe them straight over SSH, with nothing exposed and no new ports:

```bash
ssh devbox ocshepherd snapshot | ocshepherd watch --stdin   # watch from here
ssh devbox ocshepherd list                                  # or act remotely
ssh devbox ocshepherd send fix-auth "text..."
```

## How the Hermes plugin works

The plugin is skills-only (no Python): it ships the `opencode-shepherd` skill, which teaches a Hermes agent how to install `ocshepherd` where opencode runs, how to set up watching and notifications, and how to reply to sessions on your behalf. The CLI itself stays a standalone distribution (source under [`cli/`](cli/)) that works with or without Hermes.

## All commands

| Command | What it does |
|---|---|
| `ping` | check connectivity + server info |
| `list` | recent sessions (`RUNNING` / `child` flags) |
| `tail <frag> [n]` | last messages of a session |
| `send <frag> ...` | send a message (`--queue`, `--file`, `--stdin`) |
| `forms <frag>` / `form-reply <frag>` | inspect / answer pending questions |
| `perms <frag>` / `perm <frag> ...` | inspect / reply to permission requests |
| `resolve <frag>` | resolve a fragment to a session |
| `snapshot` | full JSON snapshot (for piping / your own tooling) |
| `watch` | the checks that produce notifications (`--events` for event-driven) |
| `config` | show / change persistent settings (`enabled`, reminders, sinks) |
| `new` / `abort` / `close` | create / interrupt / delete sessions |

## Good to know

- **Credentials** are auto-discovered: URL from `$OCSHEPHERD_URL` → `opencode service status` → `http://127.0.0.1:4096`; password from `$OCSHEPHERD_PASSWORD` → `$OPENCODE_SERVER_PASSWORD`; username defaults to `opencode`. Override with `--url/--password/--username`. Scheduled watchers should set the password in an environment file (see [`examples/systemd/`](examples/systemd/)).
- **steer vs queue**: `send` steers by default (delivered even mid-turn); `--queue` waits for the current turn to end.
- **Repeat safety**: alerts are deduplicated by event id, and the first `watch` run only seeds state, so no old noise.
- **API caveats**: built and tested against the opencode **v2** server API (spec at `http://<host>:<port>/doc`). The API is young; if your version is newer, check the spec after upgrades. The v1 server API is not supported. Escape hatch: `opencode api GET /api/session`.

## Security

- Nothing listens and nothing is exposed: the tool only calls the opencode server over HTTP (or runs over your existing SSH).
- If you configure a notification sink (Telegram, `notify_cmd`), the tool makes network calls to that service. Nothing else leaves the machine.
- Credentials are never printed or logged. On shared machines prefer the env var over `--password` (visible in the process list).
- `perm`/`form-reply` are real approvals: treat them like clicking approve in the TUI.

## Related tools

- `opencode web` / `opencode pair` / `opencode attach`: full interactive UIs; use them to drive sessions by hand.
- [opencode-notify](https://github.com/kdcokenny/opencode-notify): OS notifications on the machine running opencode.
- `@opencode-ai/sdk`: the official TypeScript SDK for JS/TS integrations.

## Development

The CLI package lives in [`cli/`](cli/) (`pyproject.toml`, `src/`, `tests/`).

```bash
python -m unittest discover -s cli/tests -v
```

## License

MIT. See [LICENSE](LICENSE).
