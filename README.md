# 🐑 opencode-shepherd

**Watch over [opencode](https://opencode.ai) sessions from anywhere — know when they stop, ask a question, or request a permission, and answer them.**
A dependency-free CLI (Python stdlib only) that talks to the opencode server API — locally, from cron/scripts, or over SSH.

---

## Why

Agentic coding sessions run for a long time. They stop silently when a turn ends, block on questions ("Remove PD for Phase B?"), and pause for permission requests. If opencode is running on your dev box and you're somewhere else, a notification alone is not enough: you want to **act**.

`ocshepherd` gives you both sides:

- **watch** — alerts you (stdout / Telegram / any command) only when something *new* happens: a session finished its turn, asked a question, or requests a permission. No duplicates, no noise. It understands when a session is waiting on a *background job* and stays silent because opencode will resume it by itself.
- **act** — send messages into sessions (steer into the current turn or queue for after it), answer the question forms, allow/deny permission requests — from any machine, with no UI and nothing exposed.

## How it works

```text
    machine running opencode                          anywhere (VPS, CI, laptop)
 ┌───────────────────────────────┐   ssh + stdout   ┌───────────────────────────────┐
 │  opencode serve (v2 API)      │ ◄──── pipe ───── │  ocshepherd watch --stdin     │
 │  127.0.0.1:<port> basic auth  │                  │   → Telegram / stdout / cmd   │
 │        ▲                      │                  │                               │
 │        │ HTTP (local)         │                  │  ocshepherd send / perm /     │
 │  ocshepherd snapshot          │ ◄──────────────► │             form-reply        │
 └───────────────────────────────┘      ssh         └───────────────────────────────┘
```

- opencode exposes its sessions through the server API (the same one the TUI and the web UI use). `ocshepherd` is just another API client.
- A **snapshot** is plain JSON: sessions, pending questions/permissions, last activity, background-job status. Because it is JSON, it pipes cleanly over SSH — the diffing/notifying side can live on a completely different machine.
- Nothing is opened to the network: the server stays on `127.0.0.1`, and the tool never listens on any port.

## Requirements

- **opencode v2** running (`opencode serve`, the background service, or the TUI that embeds a server). Tested with **v2.0.28**. The older v1 server API is different and not supported.
- **Python ≥ 3.9** — standard library only, nothing to compile.
- For remote use: SSH access to the machine running opencode (and `ocshepherd` installed there — or just use the remote `snapshot` pipe).

## Install

```bash
# pipx (recommended)
pipx install git+https://github.com/okazakee/opencode-shepherd

# or pip
pip install git+https://github.com/okazakee/opencode-shepherd

# or from a checkout, no install:
PYTHONPATH=src python3 -m ocshepherd --help
```

## Credentials & discovery

The tool needs the server URL and password, and finds both automatically:

| What | Resolution order |
|---|---|
| URL | `--url` → `$OCSHEPHERD_URL` → `opencode service status` → `http://127.0.0.1:4096` |
| Password | `--password` → `$OCSHEPHERD_PASSWORD` → `~/.config/opencode/service.json` → `$OPENCODE_SERVER_PASSWORD` |
| Username | `--username` → `$OCSHEPHERD_USERNAME` → `$OPENCODE_SERVER_USERNAME` → `opencode` |

(`service.json` is where the opencode background service stores its generated password; `OPENCODE_SERVER_PASSWORD` is the one you set yourself when starting `opencode serve` manually.)

## Quick start

```bash
ocshepherd ping                      # is the server reachable?
ocshepherd list                      # recent sessions, RUNNING/child flags
ocshepherd tail fix-auth 20          # last messages of a session (id or title fragment)
ocshepherd send fix-auth "also add a test for the empty case"
ocshepherd watch                     # one-shot check: exits silently if nothing new
```

## Watching (cron, systemd, anywhere)

`watch` is a one-shot check — schedule it (every 2 minutes is a good default) and it stays quiet unless something new needs attention:

```bash
# local machine
*/2 * * * * /usr/local/bin/ocshepherd watch

# with Telegram notifications, silent stdout
*/2 * * * * OCSHEPHERD_TELEGRAM_TOKEN=... OCSHEPHERD_TELEGRAM_CHAT=... /usr/local/bin/ocshepherd watch --quiet

# remote machine over SSH: snapshot remotely, diff + notify locally
*/2 * * * * ssh devbox ocshepherd snapshot | ocshepherd watch --stdin --quiet
```

- `--loop 120` keeps it running in the foreground (for a systemd service without a timer).
- More examples in [`examples/`](examples/): crontab lines, a systemd service + timer, an `ntfy.sh` notify wrapper.
- State (what has already been reported) lives in `~/.local/state/ocshepherd/watch-state.json`; override with `--state`. The first run only seeds the state; pending questions/permissions found on the first run *are* reported.

### What triggers an alert

| Alert | Meaning |
|---|---|
| ⏸️ | a top-level session **finished its turn** and is waiting for input |
| ❓ | a session has a **pending question** (the agent used the `question` tool) |
| 🔐 | a session has a **pending permission request** |
| ⚠️ | a turn ended with outcome **failed/interrupted**, or the server has been unreachable for 3 consecutive runs |

Deliberately silent: sessions **waiting on a background shell** (opencode resumes them automatically when it exits), and sub-sessions finishing (their parent will report).

### Notification sinks

- **stdout** (default; combine with cron mail, or a pipe of your choice)
- **Telegram** — `--telegram-token` + `--telegram-chat` (or `OCSHEPHERD_TELEGRAM_TOKEN` / `OCSHEPHERD_TELEGRAM_CHAT`)
- **any command** — `--notify-cmd 'my-script'`: the alert text is piped to its stdin (ntfy, webhooks, mail, …)
- `--quiet` suppresses stdout when you only want one of the above.

## Responding to a session

```bash
ocshepherd send fix-auth "text..."        # steer (delivered immediately, even mid-turn)
ocshepherd send fix-auth "text..." --queue  # deliver after the current turn ends
ocshepherd send fix-auth --file prompt.md   # multiline from a file
cat prompt.md | ocshepherd send fix-auth --stdin

ocshepherd forms fix-auth                  # inspect pending questions
ocshepherd form-reply fix-auth --answer q0="PD connected"

ocshepherd perms fix-auth                  # inspect pending permissions
ocshepherd perm fix-auth once              # once | always | reject  (--message ...)

ocshepherd resolve fix-auth                # what does this fragment match?
```

Session **fragments** match either a session id (substring, case-insensitive) or a title substring (case-insensitive). Ambiguous fragments print the candidates and exit with code 2.

Remote use is the same commands over SSH:

```bash
ssh devbox ocshepherd list
ssh devbox ocshepherd send fix-auth "text..."
```

## Security notes

- The tool never listens on a port and never proxies anything: it only makes HTTP calls to the local server (or, in remote setups, runs over your existing SSH).
- Credentials are read from the environment or from the same file opencode itself uses; they are never printed or logged. Avoid `--password` on shared machines (visible in the process list) — prefer the env var or `service.json`.
- `perm once/always` and question answers are real approvals. Treat them with the same care as clicking approve in the TUI.

## Compatibility & caveats

- Built against the **opencode v2 HTTP API** (the spec is served at `http://<host>:<port>/doc`). Tested with v2.0.28. The API is young: expect occasional breaking changes — pin your opencode version if you rely on this in automation, and check the spec after upgrades.
- Escape hatch: the opencode CLI can call its own server — `opencode api GET /api/session`.
- Not affiliated with the opencode project.

## Related tools

- `opencode web` / `opencode pair` / `opencode attach` — full interactive web/TUI clients; use them when you want to *drive* sessions manually with a UI (mind exposure if you bind beyond localhost).
- [opencode-notify](https://github.com/kdcokenny/opencode-notify) — native OS notifications on the machine running opencode; great when you're near that machine.
- `@opencode-ai/sdk` — official TypeScript SDK, if you're building JS/TS integrations.
- **ocshepherd** — headless, scriptable *watch + relay*: notifications you can act on, from anywhere, in about 400 lines of Python.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).
