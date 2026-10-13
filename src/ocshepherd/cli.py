"""Command-line interface for opencode-shepherd."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .client import Client, ShepherdError, format_ms
from .config import coerce, load_config, save_config
from .snapshot import build_snapshot
from .watch import (
    DEFAULT_STATE,
    check,
    clear_fails,
    fail_alert,
    format_alerts,
    load_state,
    notify_cmd,
    notify_stdout,
    notify_telegram,
    run_events,
    save_state,
)

EPILOG = """examples:
  ocshepherd ping
  ocshepherd list
  ocshepherd tail auth-refactor 15
  ocshepherd send auth-refactor "also add a test for the empty case"
  ocshepherd watch                        # one-shot check (cron/systemd)
  ocshepherd watch --events               # event-driven (long-running, light)
  ocshepherd watch --loop 120             # polling loop
  ocshepherd config set remind_after_minutes 30
  ocshepherd new --dir ~/work/proj --title "notes" --prompt "summarise the repo"
  ocshepherd abort auth-refactor          # interrupt a running session
  ocshepherd close auth-refactor --yes    # delete a session
  ssh devbox ocshepherd snapshot | ocshepherd watch --stdin   # remote, piped
"""


def add_client_args(p):
    p.add_argument("--url", help="opencode server URL (default: auto-detect)")
    p.add_argument("--password", help="basic auth password (default: auto-detect)")
    p.add_argument("--username", help="basic auth username (default: opencode)")
    p.add_argument("--timeout", type=int, default=20, help="request timeout in seconds")


def make_client(args):
    return Client(
        url=args.url,
        password=args.password,
        username=args.username,
        timeout=args.timeout,
    )


def die(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def resolve_or_die(client, fragment):
    (session, candidates), _sessions = client.resolve(fragment)
    if session:
        return session
    if candidates:
        print("Ambiguous fragment %r — candidates:" % fragment, file=sys.stderr)
        for s in candidates[:10]:
            print("  %s  %s" % (s.get("id"), s.get("title") or ""), file=sys.stderr)
        sys.exit(2)
    print("No session matches %r." % fragment, file=sys.stderr)
    sys.exit(3)


def age_minutes(ms):
    if not ms:
        return "?"
    return int((time.time() * 1000 - ms) / 60000)


# -- subcommands -----------------------------------------------------------

def cmd_ping(args):
    client = make_client(args)
    print("server: %s" % client.url)
    try:
        info = client.info()
        if info:
            print("info: %s" % json.dumps(info)[:300])
    except ShepherdError:
        pass
    active = client.active()
    sessions = client.sessions(limit=1)
    print("active sessions: %d" % len(active))
    print("reachable: yes")


def cmd_snapshot(args):
    client = make_client(args)
    snap = build_snapshot(
        client,
        window_hours=args.window_hours,
        max_sessions=args.max_sessions,
    )
    if args.pretty:
        print(json.dumps(snap, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(snap, separators=(",", ":"), ensure_ascii=False))


def cmd_list(args):
    client = make_client(args)
    sessions = client.sessions(limit=args.limit)
    active = client.active()
    if not sessions:
        print("no sessions")
        return
    for s in sessions:
        sid = s.get("id") or ""
        flags = []
        if sid in active:
            flags.append("RUNNING")
        if s.get("parentID"):
            flags.append("child")
        flagstr = ("[%s]" % ",".join(flags)) if flags else ""
        print("%s%-14s %4dm  %s" % (
            sid[:24].ljust(24), flagstr, age_minutes((s.get("time") or {}).get("updated")),
            s.get("title") or "",
        ))


def cmd_tail(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    sid = session["id"]
    messages = client.messages(sid, limit=args.count, order="desc")
    messages.reverse()
    print("== %s  %r ==" % (sid, session.get("title") or ""))
    for m in messages:
        ts = (m.get("time") or {}).get("created") or 0
        bits = []
        typ = m.get("type")
        if typ in ("assistant", "system", "synthetic"):
            for c in m.get("content") or []:
                if c.get("type") == "text" and c.get("text"):
                    bits.append(c["text"].strip())
                elif c.get("type") == "tool":
                    meta = c.get("metadata") or {}
                    status = meta.get("status") or (c.get("state") or {}).get("status") or ""
                    bits.append("[tool:%s%s]" % (c.get("name"), (":" + status) if status else ""))
        elif typ == "user":
            bits.append((m.get("payload") or {}).get("text") or m.get("text") or "")
        elif typ == "idle":
            bits.append("outcome=%s" % m.get("outcome"))
        print("%s [%s] %s" % (format_ms(ts), typ, " ".join(b for b in bits if b)[:500]))

    perms = client.pending_permissions(sid)
    forms = client.pending_form(sid)
    if perms:
        print(json.dumps({"pending_permissions": perms}, indent=1, ensure_ascii=False))
    if forms:
        print(json.dumps({"pending_forms": forms}, indent=1, ensure_ascii=False))


def cmd_send(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif args.stdin:
        text = sys.stdin.read()
    else:
        text = " ".join(args.text)
    text = text.strip("\n")
    if not text:
        die("nothing to send (empty message)")
    delivery = "queue" if args.queue else None
    data = client.send(session["id"], text, delivery=delivery)
    print("sent to %s (%r) delivery=%s id=%s" % (
        session["id"], session.get("title") or "",
        (data or {}).get("delivery") or "default", (data or {}).get("id") or "?",
    ))


def cmd_forms(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    forms = client.pending_form(session["id"])
    if not forms:
        print("no pending questions")
        return
    print(json.dumps(forms, indent=1, ensure_ascii=False))


def cmd_form_reply(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    forms = client.pending_form(session["id"])
    if not forms:
        die("no pending question form on %s" % session["id"])
    form = forms[0]
    answer = {}
    if args.json:
        answer = json.loads(args.json)
    for pair in args.answer or []:
        key, _, value = pair.partition("=")
        if not _:
            die("--answer expects key=value, got %r" % pair)
        answer[key] = value
    if not answer:
        print("pending form %s — provide --answer key=value (or --json):" % form.get("id"))
        print(json.dumps(form, indent=1, ensure_ascii=False))
        return
    result = client.answer_form(session["id"], form["id"], answer)
    print("answered form %s: %s" % (form.get("id"), json.dumps(answer, ensure_ascii=False)))


def cmd_perms(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    perms = client.pending_permissions(session["id"])
    if not perms:
        print("no pending permissions")
        return
    print(json.dumps(perms, indent=1, ensure_ascii=False))


def cmd_perm(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    perms = client.pending_permissions(session["id"])
    if not perms:
        die("no pending permission on %s" % session["id"])
    request = perms[0]
    result = client.reply_permission(
        session["id"], request["id"], args.decision, message=args.message,
    )
    print("permission %s -> %s (action: %s)" % (
        request["id"], args.decision, request.get("action") or "?"))


def cmd_resolve(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    print("%s  %s" % (session["id"], session.get("title") or ""))


def _load_settings(args):
    """Merge settings: CLI flag > env (via parser defaults) > config file > default."""
    cfg = load_config()

    def pick(key, attr):
        value = getattr(args, attr, None)
        if value is not None:
            return value
        return cfg.get(key)

    return {
        "enabled": bool(cfg.get("enabled", True)),
        "remind": pick("remind_after_minutes", "remind_after_minutes"),
        "snippet": pick("snippet", "snippet") or 220,
        "state": pick("state", "state") or str(DEFAULT_STATE),
        "telegram_token": pick("telegram_token", "telegram_token"),
        "telegram_chat": pick("telegram_chat", "telegram_chat"),
        "notify_cmd": pick("notify_cmd", "notify_cmd"),
        "resync_minutes": pick("resync_minutes", "resync_minutes") or 30,
        "min_interval_seconds": pick("min_interval_seconds", "min_interval") or 15,
        "max_fails": getattr(args, "max_fails", None) or 3,
        "fail_cooldown_hours": getattr(args, "fail_cooldown_hours", None) or 6,
    }


def _emit(text, settings, quiet=False):
    if not text:
        return
    if not quiet:
        notify_stdout(text)
    if settings.get("telegram_token") and settings.get("telegram_chat"):
        notify_telegram(text, settings["telegram_token"], settings["telegram_chat"])
    if settings.get("notify_cmd"):
        notify_cmd(text, settings["notify_cmd"])


def cmd_watch(args):
    if args.events:
        if args.stdin or args.snapshot or args.loop:
            die("--events cannot be combined with --stdin/--snapshot/--loop")
        settings = _load_settings(args)
        try:
            run_events(
                make_client(args),
                lambda: _load_settings(args),
                lambda text: _emit(text, settings, args.quiet),
            )
        except KeyboardInterrupt:
            pass
        return

    if args.loop and (args.stdin or args.snapshot):
        die("--loop cannot be combined with --stdin/--snapshot (snapshot would go stale)")

    def run_once():
        settings = _load_settings(args)  # fresh each run: config edits apply live
        state = load_state(settings["state"])
        alerts = []
        try:
            if args.snapshot:
                with open(args.snapshot) as f:
                    snap = json.load(f)
            elif args.stdin:
                raw = sys.stdin.read()
                if not raw.strip():
                    raise ValueError("no snapshot data on stdin")
                snap = json.loads(raw)
            else:
                snap = build_snapshot(make_client(args))
            if "sessions" not in snap:
                raise ValueError("invalid snapshot (missing 'sessions')")
            clear_fails(state)
            alerts = check(
                snap, state,
                snippet=settings["snippet"],
                remind_after_minutes=settings["remind"],
                enabled=settings["enabled"],
            )
        except SystemExit:
            raise
        except Exception as e:
            alert = fail_alert(
                state, e,
                max_fails=settings["max_fails"],
                cooldown_hours=settings["fail_cooldown_hours"],
            )
            if alert:
                alerts = [alert]
        save_state(settings["state"], state)
        _emit(format_alerts(alerts), settings, args.quiet)

    if args.loop:
        try:
            while True:
                run_once()
                time.sleep(args.loop)
        except KeyboardInterrupt:
            pass
    else:
        run_once()


def cmd_config(args):
    cfg = load_config()
    op = getattr(args, "op", None) or "list"
    if op == "list":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    elif op == "get":
        if not args.key:
            die("usage: ocshepherd config get <key>")
        print(json.dumps(cfg.get(args.key)))
    elif op == "set":
        if not args.key or args.value is None:
            die("usage: ocshepherd config set <key> <value>")
        cfg[args.key] = coerce(args.value)
        save_config(cfg)
        print("%s = %s" % (args.key, json.dumps(cfg[args.key])))
    elif op == "unset":
        if not args.key:
            die("usage: ocshepherd config unset <key>")
        cfg.pop(args.key, None)
        save_config(cfg)
        print("unset %s" % args.key)
    else:
        die("unknown config op: %s" % op)


def cmd_new(args):
    client = make_client(args)
    directory = args.dir or os.getcwd()
    data = client.create_session(title=args.title, directory=directory, agent=args.agent)
    sid = data.get("id")
    if not sid:
        die("could not create session: %s" % json.dumps(data)[:300])
    print("new session: %s  %r  (%s)" % (sid, data.get("title") or "", directory))
    if args.prompt:
        sent = client.send(sid, args.prompt)
        print("prompt sent: id=%s delivery=%s" % (
            (sent or {}).get("id"), (sent or {}).get("delivery")))


def cmd_close(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    if not args.yes:
        print("would delete: %s  %r — re-run with --yes to confirm" % (
            session["id"], session.get("title") or ""))
        return
    client.delete_session(session["id"])
    print("deleted %s  %r" % (session["id"], session.get("title") or ""))


def cmd_abort(args):
    client = make_client(args)
    session = resolve_or_die(client, args.fragment)
    result = client.interrupt_session(session["id"])
    print(("interrupt %s -> %s" % (session["id"], json.dumps(result)))[:220])


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ocshepherd",
        description="Watch over opencode sessions and respond when they need you.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="ocshepherd %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ping", help="check connectivity to the opencode server")
    add_client_args(p)
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("snapshot", help="print a JSON snapshot of the sessions")
    add_client_args(p)
    p.add_argument("--window-hours", type=float, default=12)
    p.add_argument("--max-sessions", type=int, default=30)
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("list", help="list recent sessions")
    add_client_args(p)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("tail", help="show the last messages of a session")
    add_client_args(p)
    p.add_argument("fragment", help="session id (or fragment) or title fragment")
    p.add_argument("count", nargs="?", type=int, default=10)
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("send", help="send a message to a session")
    add_client_args(p)
    p.add_argument("fragment", help="session id (or fragment) or title fragment")
    p.add_argument("text", nargs="*", help="message text (or use --stdin/--file)")
    p.add_argument("--stdin", action="store_true", help="read the message from stdin")
    p.add_argument("--file", help="read the message from a file")
    p.add_argument("--queue", action="store_true",
                   help="queue the message instead of steering it into the current turn")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("forms", help="list pending question forms of a session")
    add_client_args(p)
    p.add_argument("fragment")
    p.set_defaults(func=cmd_forms)

    p = sub.add_parser("form-reply", help="answer a pending question form")
    add_client_args(p)
    p.add_argument("fragment")
    p.add_argument("--answer", action="append", metavar="KEY=VALUE",
                   help="answer field (repeatable)")
    p.add_argument("--json", help="answer object as JSON")
    p.set_defaults(func=cmd_form_reply)

    p = sub.add_parser("perms", help="list pending permission requests of a session")
    add_client_args(p)
    p.add_argument("fragment")
    p.set_defaults(func=cmd_perms)

    p = sub.add_parser("perm", help="reply to the first pending permission request")
    add_client_args(p)
    p.add_argument("fragment")
    p.add_argument("decision", choices=["once", "always", "reject"])
    p.add_argument("--message", help="optional message to the agent")
    p.set_defaults(func=cmd_perm)

    p = sub.add_parser("resolve", help="resolve a session fragment to a session id")
    add_client_args(p)
    p.add_argument("fragment")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser(
        "watch",
        help="check sessions and notify about new events (one-shot, --loop or --events)",
    )
    add_client_args(p)
    p.add_argument("--events", action="store_true",
                   help="event-driven mode: stream the opencode event bus and check on turn/permission/form events")
    p.add_argument("--stdin", action="store_true", help="read the snapshot from stdin")
    p.add_argument("--snapshot", help="read the snapshot from a file")
    p.add_argument("--state", default=None, help="state file path (overrides config)")
    p.add_argument("--snippet", type=int, default=None, help="max chars of last-message snippets")
    p.add_argument("--remind-after-minutes", type=int, default=None,
                   help="re-notify every N minutes while an item is still waiting")
    p.add_argument("--resync-minutes", type=int, default=None,
                   help="--events: full-check safety net interval (default 30)")
    p.add_argument("--min-interval", type=int, default=None,
                   help="--events: minimum seconds between checks (default 15)")
    p.add_argument("--loop", type=int, metavar="SECONDS", help="keep running, checking every N seconds")
    p.add_argument("--telegram-token", default=os.environ.get("OCSHEPHERD_TELEGRAM_TOKEN"))
    p.add_argument("--telegram-chat", default=os.environ.get("OCSHEPHERD_TELEGRAM_CHAT"))
    p.add_argument("--notify-cmd", help="shell command to pipe the alert text into")
    p.add_argument("--quiet", action="store_true", help="do not print alerts to stdout")
    p.add_argument("--max-fails", type=int, default=3,
                   help="consecutive failed runs before a connectivity alert")
    p.add_argument("--fail-cooldown-hours", type=float, default=6)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("config", help="show or change persistent settings")
    p.add_argument("op", nargs="?", default="list", choices=["list", "get", "set", "unset"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("new", help="create a new session")
    add_client_args(p)
    p.add_argument("--dir", help="working directory (default: current)")
    p.add_argument("--title", help="session title")
    p.add_argument("--agent", help="agent to use")
    p.add_argument("--prompt", help="send this first message after creating")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("close", help="delete a session (needs --yes)")
    add_client_args(p)
    p.add_argument("fragment")
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("abort", help="interrupt a running session")
    add_client_args(p)
    p.add_argument("fragment")
    p.set_defaults(func=cmd_abort)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ShepherdError as e:
        die("error: %s" % e)
    except KeyboardInterrupt:
        sys.exit(130)
    return 0


if __name__ == "__main__":
    sys.exit(main())
