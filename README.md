# 🐑 opencode-shepherd

**Keep an eye on your [opencode](https://opencode.ai) sessions from anywhere — know when they stop, ask a question, or need a permission, and answer them without a UI.**

A tiny, dependency-free Python CLI (standard library only). Works locally, over SSH, and as a [Hermes](https://github.com/NousResearch/hermes-agent) plugin.

```text
   sessions on your dev box              you, anywhere
  ┌────────────────────────┐          ┌────────────────────────────┐
  │ opencode server (v2)   │   ssh    │ ocshepherd watch           │
  │ (same API as the TUI)  │ ◄──────► │   → Telegram / terminal    │
  └────────────────────────┘          │ ocshepherd send / perm / … │
                                      └────────────────────────────┘
```

## Why

Long agent sessions behave like a slow colleague: they finish a step and wait, they ask before doing something risky, they block on a question. If opencode runs on another machine, you find out too late — and a notification you can't reply to doesn't help. `ocshepherd` does both halves:

1. **Watch** — tells you only when something **new** needs attention.
2. **Reply** — send messages, answer questions, allow or deny permissions — from wherever you are.

## Install

```bash
pipx install git+https://github.com/okazakee/opencode-shepherd   # recommended
# or
pip install git+https://github.com/okazakee/opencode-shepherd
```

- Requires **Python ≥ 3.9** and a running **opencode v2** (`opencode serve`,
  the background service, or a TUI — tested with v2.0.28).
- Credentials are found automatically (see [Good to know](#good-to-know)).

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
| ⚠️ | a turn failed — or the server has been unreachable for several checks |

```bash
# cron: local machine
*/2 * * * * /usr/local/bin/ocshepherd watch

# cron: Telegram notifications, silent stdout
*/2 * * * * OCSHEPHERD_TELEGRAM_TOKEN=... OCSHEPHERD_TELEGRAM_CHAT=... /usr/local/bin/ocshepherd watch --quiet

# cron: remote machine over SSH — snapshot remotely, diff + notify locally
*/2 * * * * ssh devbox ocshepherd snapshot | ocshepherd watch --stdin --quiet
```

- Notifications go to **stdout**, **Telegram** (`--telegram-token/--telegram-chat`), or **any command** (`--notify-cmd`, alert text on stdin — ntfy, webhooks, mail…).
- `--loop 120` keeps it running (for a systemd service without a timer).
- Examples in [`examples/`](examples/): crontab, systemd unit + timer, ntfy wrapper.
- It knows the difference between "waiting for you" and "waiting for a background job" — the latter is never reported, because opencode resumes those sessions by itself.

## Reply to a session

```bash
ocshepherd send fix-auth "text..."          # delivered immediately (steer)
ocshepherd send fix-auth "text..." --queue  # delivered after the current turn
ocshepherd send fix-auth --file prompt.md   # multiline
cat prompt.md | ocshepherd send fix-auth --stdin

ocshepherd forms fix-auth                   # pending questions
ocshepherd form-reply fix-auth --answer q0="PD connected"

ocshepherd perms fix-auth                   # pending permissions
ocshepherd perm fix-auth once               # once | always | reject

ocshepherd resolve fix-auth                 # what does this fragment match?
```

Sessions are addressed by a **fragment**: part of the session id or part of the title (case-insensitive). Ambiguous fragments print the candidates instead of guessing.

## Over SSH (remote machines)

Snapshots are plain JSON, so you can pipe them straight over SSH — nothing exposed, no new ports:

```bash
ssh devbox ocshepherd snapshot | ocshepherd watch --stdin   # watch from here
ssh devbox ocshepherd list                                  # or act remotely
ssh devbox ocshepherd send fix-auth "text..."
```

## Using it with Hermes (plugin)

This repo is also a portable **Agent Plugin** (skills-only): it teaches a [Hermes](https://github.com/NousResearch/hermes-agent) agent how to use `ocshepherd`, so your agent can watch, summarise and answer sessions for you.

```bash
hermes plugins install okazakee/opencode-shepherd
```

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
| `watch` | the checks that produce notifications |

## Good to know

- **Credentials** are auto-discovered: URL from `$OCSHEPHERD_URL` → `opencode service status` → `http://127.0.0.1:4096`; password from `$OCSHEPHERD_PASSWORD` → `~/.config/opencode/service.json` → `$OPENCODE_SERVER_PASSWORD`; username defaults to `opencode`. Override with `--url/--password/--username`.
- **steer vs queue**: `send` steers by default (delivered even mid-turn); `--queue` waits for the current turn to end.
- **Repeat safety**: alerts are deduplicated by event id, and the first `watch` run only seeds state — no old noise.
- **API caveats**: built and tested against the opencode **v2** server API (spec at `http://<host>:<port>/doc`). The API is young; if your version is newer, check the spec after upgrades. The v1 server API is not supported. Escape hatch: `opencode api GET /api/session`.

## Security

- Nothing listens and nothing is exposed: the tool only calls the opencode server over HTTP (or runs over your existing SSH).
- Credentials are never printed or logged. On shared machines prefer the env var or `service.json` over `--password` (visible in the process list).
- `perm`/`form-reply` are real approvals — treat them like clicking approve in the TUI.

## Related tools

- `opencode web` / `opencode pair` / `opencode attach` — full interactive UIs; use them to drive sessions by hand.
- [opencode-notify](https://github.com/kdcokenny/opencode-notify) — OS notifications on the machine running opencode.
- `@opencode-ai/sdk` — the official TypeScript SDK for JS/TS integrations.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).
