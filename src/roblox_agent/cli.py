"""Command line.

Most people only ever need:
    roblox-agent            today's check-up (sets things up the first time)
    roblox-agent view       the same, as a page with charts
    roblox-agent connect    pick the AI agent to use (Claude, Codex, ...)
    roblox-agent app        the desktop app (setup screens, inbox, settings)
    roblox-agent mark 1 done        tell it item 1 is fixed (or: snooze, wrong)
    roblox-agent change "name"      log a game change so it gets judged later
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import (
    __version__,
    catalog,
    changes,
    checks,
    checkup,
    config,
    connect,
    db,
    ingest,
    report,
    selftest,
    weekly,
    why,
)
from .api import AnalyticsClient, ApiError, AuthError
from .demo import FakeRoblox

DEMO_UNIVERSE = 1
STALE_HOURS = 20
MAX_BACKFILL_DAYS = 730  # Roblox only allows daily data for ranges up to 2 years


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _client(cfg: config.Config) -> AnalyticsClient:
    if not cfg.universe_id or not cfg.api_key:
        raise SystemExit("Not set up yet. Run:  roblox-agent setup")
    return AnalyticsClient(api_key=cfg.api_key, universe_id=cfg.universe_id)


def _test_connection(client: AnalyticsClient, quiet: bool = False) -> bool:
    end = ingest.today_utc()
    start = end - timedelta(days=7)
    try:
        op = client.query_metric("DailyActiveUsers", "OneDay", ingest._iso(start), ingest._iso(end))
    except ApiError as e:
        _say(f"  Not connected: {e}")
        return False
    points = [dp for s in op.get("response", {}).get("values", []) for dp in s.get("dataPoints", [])]
    if quiet:
        _say(f"  Connected. Roblox sent {len(points)} days of player counts.")
        return True
    _say(f"Connected. Roblox returned {len(points)} days of daily active users:")
    for dp in points:
        note = report.STATUS_NOTE.get(dp.get("status", ""), "")
        _say(f"  {dp['time'][:10]}  {report.fmt(dp.get('value'))}  {note}")
    return True


def _stale(conn, uid: int) -> bool:
    row = conn.execute("SELECT MAX(started_at) AS t FROM pull_runs WHERE universe_id=? AND status!='failed'",
                       (uid,)).fetchone()
    if not row or not row["t"]:
        return True
    last = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - last > timedelta(hours=STALE_HOURS)


def _do_pull(client: AnalyticsClient, conn, metrics=None, backfill_days: int = ingest.DEFAULT_BACKFILL_DAYS,
             verbose: bool = False) -> bool:
    first = conn.execute("SELECT COUNT(*) FROM data_points WHERE universe_id=?", (client.universe_id,)).fetchone()[0] == 0
    if first:
        _say("Downloading your game's history (first time only, a few minutes)...")
    else:
        _say("Getting the latest numbers from Roblox...")
    jobs = [0]
    total = sum(len(s.breakdowns) for s in (metrics or catalog.DEFAULT_METRICS))

    def progress(msg: str) -> None:
        if verbose:
            _say(msg)
        elif not msg.startswith("  "):
            jobs[0] += 1
            print(f"\r  {jobs[0]}/{total}", end="", flush=True)

    try:
        result = ingest.pull(client, conn, metrics or catalog.DEFAULT_METRICS, backfill_days, progress=progress)
    except AuthError as e:
        _say(f"\nStopped: {e}")
        return False
    if not verbose:
        print("\r", end="")
    if result.errors:
        _say(f"  {len(result.errors)} item(s) skipped (details: roblox-agent pull --details).")
        if verbose:
            for e in result.errors:
                _say("  - " + e)
    return result.status != "failed"


def _print_checkup(conn, uid: int, record: bool = True, weekly_too: bool | None = None) -> None:
    c = checkup.run(conn, uid, record=record, weekly=weekly_too)
    _say()
    _say(c.text())
    _say()
    if c.top and record:
        _say("More: roblox-agent view   ·   Fixed it? roblox-agent mark 1 done")
    else:
        _say("More: roblox-agent view")


# ------------------------------------------------------------ commands ---

def cmd_home(args: argparse.Namespace) -> int:
    cfg = config.load()
    if not cfg.universe_id or not cfg.api_key:
        _say("Welcome! Let's connect your game. This takes about 2 minutes.\n")
        return cmd_setup(args)
    conn = db.connect(config.db_path())
    if _stale(conn, cfg.universe_id):
        _do_pull(_client(cfg), conn)
    _print_checkup(conn, cfg.universe_id)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = config.load()
    if os.environ.get(config.KEY_ENV):
        _say(f"Note: the {config.KEY_ENV} environment variable is set, so it will be used instead of "
             "the key you save here.\n")

    _say("Step 1 of 3: your game")
    _say("  Open create.roblox.com/dashboard/creations, click your game, and copy the page link.")
    while True:
        hint = f" [{cfg.universe_id}]" if cfg.universe_id else ""
        raw = _ask(f"  Paste the link or Universe ID{hint}: ").strip()
        if not raw and cfg.universe_id:
            uid = cfg.universe_id
            break
        try:
            uid = config.parse_universe_id(raw)
            break
        except ValueError as e:
            _say("  " + str(e))
            if not raw:
                return 1

    _say("\nStep 2 of 3: your API key")
    key = None
    if cfg.api_key and _ask("  A key is already saved. Keep it? [Y/n]: ").strip().lower() != "n":
        key = cfg.api_key
    while key is None:
        _say("  Guide: docs/SETUP_GUIDE.md, step 4. Only tick 'universe.analytics:read'.")
        _say("  Paste the key and press Enter. (It stays hidden while you paste. That's normal.)")
        typed = getpass.getpass("  API key: ").strip()
        if not typed:
            return 1
        if len(typed) < 20 or any(c.isspace() for c in typed):
            _say("  That doesn't look like a full key. Use 'Copy Key to Clipboard' on Roblox and paste again.")
            continue
        key = typed
    client = AnalyticsClient(api_key=key, universe_id=uid)
    _say("  Checking with Roblox...")
    if not _test_connection(client, quiet=True):
        _say("  Nothing was saved. Check the key has your game and 'universe.analytics:read', then try again.")
        return 1
    config.save_universe_id(uid)
    if key != cfg.api_key:
        config.save_api_key(key)
    _say("  Saved on this computer only.")

    conn = db.connect(config.db_path())
    _do_pull(client, conn)

    _say("\nStep 3 of 3: your AI helper (optional)")
    _choose_agent(skip_allowed=True)

    _print_checkup(conn, uid)
    return 0


def _choose_agent(skip_allowed: bool = False, choice: str | None = None) -> int:
    if choice is None:
        _say("  Which AI agent should read your check-ups?")
        for i, a in enumerate(connect.AGENTS, 1):
            _say(f"   {i}. {a.label}")
        if skip_allowed:
            _say("   Enter = skip for now (run 'roblox-agent connect' later)")
        choice = _ask("  Number: ").strip()
    if not choice:
        return 0
    agent = connect.BY_KEY.get(choice)
    if agent is None and choice.isdigit() and 1 <= int(choice) <= len(connect.AGENTS):
        agent = connect.AGENTS[int(choice) - 1]
    if agent is None:
        _say("  Unknown choice. Options: " + ", ".join(a.key for a in connect.AGENTS))
        return 1
    ok, msg = connect.connect(agent)
    _say("  " + msg.replace("\n", "\n  "))
    if ok and agent.kind != "manual":
        config.save_setting("agent", agent.key)
        _say(f"  Then ask {agent.label}: \"Run my Roblox daily check-up\".")
    return 0 if ok else 1


def cmd_connect(args: argparse.Namespace) -> int:
    return _choose_agent(choice=args.agent)


def cmd_check(args: argparse.Namespace) -> int:
    return 0 if _test_connection(_client(config.load())) else 1


def _pick_metrics(names: str | None) -> list[catalog.MetricSpec]:
    if not names:
        return list(catalog.DEFAULT_METRICS)
    return [catalog.get(n.strip()) for n in names.split(",") if n.strip()]


def cmd_pull(args: argparse.Namespace) -> int:
    cfg = config.load()
    client = _client(cfg)
    conn = db.connect(config.db_path())
    days = max(1, min(args.backfill_days, MAX_BACKFILL_DAYS))
    ok = _do_pull(client, conn, _pick_metrics(args.metrics), days, verbose=args.details)
    _say("Done." if ok else "Pull failed.")
    return 0 if ok else 1


def _open_db(demo: bool):
    if demo:
        return db.connect(config.db_path(demo=True)), DEMO_UNIVERSE
    cfg = config.load()
    if not cfg.universe_id:
        raise SystemExit("Not set up yet. Run:  roblox-agent setup   (or try:  roblox-agent demo)")
    return db.connect(config.db_path()), cfg.universe_id


def cmd_show(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    _say(report.text_summary(conn, uid))
    return 0


def _write_view(conn, uid: int, out: Path, open_it: bool) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.html_report(conn, uid, checkup.run(conn, uid)), encoding="utf-8")
    _say(f"Page saved to {out}")
    if open_it:
        webbrowser.open(out.resolve().as_uri())


def cmd_view(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    out = Path(args.out) if args.out else config.home_dir() / ("demo-report.html" if args.demo else "report.html")
    _write_view(conn, uid, out, not args.no_open)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    out = Path(args.out) if args.out else config.home_dir() / "analytics.csv"
    n = report.export_csv(conn, uid, out)
    _say(f"Wrote {n} rows to {out}. It opens in Excel or Google Sheets.")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    path = config.db_path(demo=True)
    if path.exists():
        path.unlink()
    conn = db.connect(path)
    client = AnalyticsClient(api_key="demo-key", universe_id=DEMO_UNIVERSE, transport=FakeRoblox(incident=True),
                             sleep=lambda _s: None)
    _say("Practice mode: made-up numbers, no internet, no key.")
    ingest.pull(client, conn, backfill_days=120)
    shipped = ingest.today_utc() - timedelta(days=37)
    changes.log(conn, DEMO_UNIVERSE, "New icon and thumbnail (example)",
                dict(zip(changes.FACTOR_KEYS, (3, 1, 0, 0, 0, 0, 1))), shipped, "DailyActiveUsers")
    _print_checkup(conn, DEMO_UNIVERSE, weekly_too=True)
    _write_view(conn, DEMO_UNIVERSE, config.home_dir() / "demo-report.html", not args.no_open)
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from .app import main as app_main
    return app_main.run(demo=args.demo, browser=args.browser, background=args.background, port=args.port)


def cmd_mark(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    msg = checkup.mark(conn, uid, args.item, args.action)
    _say(msg)
    return 0 if not msg.startswith(("Unknown", "Pick", "No check-up", "The latest")) else 1


def _ask_score(key: str, question: str, options: tuple[str, ...]) -> int:
    _say(f"\n  {question}")
    for i, o in enumerate(options):
        _say(f"   {i} = {o}")
    while True:
        raw = _ask("  0-3: ").strip()
        if raw in ("0", "1", "2", "3"):
            return int(raw)
        if not raw:
            raise SystemExit("Cancelled. Nothing was logged.")
        _say("  Type 0, 1, 2 or 3.")


def cmd_change(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    if args.scores:
        parts = [p.strip() for p in args.scores.split(",")]
        if len(parts) != len(changes.FACTOR_KEYS):
            raise ValueError(f"--scores needs {len(changes.FACTOR_KEYS)} numbers, e.g. 2,1,1,0,0,1,0")
        scores = dict(zip(changes.FACTOR_KEYS, (int(p) for p in parts)))
    else:
        _say(f"How heavy is '{args.title}'? 7 quick questions (Enter to cancel).")
        scores = {key: _ask_score(key, q, opts) for key, q, opts in changes.FACTORS}
    shipped = date.fromisoformat(args.shipped) if args.shipped else None
    _say(changes.log(conn, uid, args.title, scores, shipped, args.target))
    return 0


def cmd_week(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    latest = conn.execute("SELECT MAX(substr(bucket_time,1,10)) FROM data_points WHERE universe_id=?",
                          (uid,)).fetchone()[0]
    if not latest:
        _say("No data yet. Run:  roblox-agent")
        return 1
    _say(weekly.build(conn, uid, date.fromisoformat(latest)).text())
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    _say(why.report(conn, uid))
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    if args.what == "sensitivity":
        if args.value not in checks.SENSITIVITY:
            _say("Use: roblox-agent tune sensitivity low|normal|high")
            return 1
        db.set_setting(conn, "sensitivity", args.value)
    elif args.what == "limit":
        # roblox-agent tune limit ClientFpsP10 min 25   (or: ... off)
        own = db.get_setting(conn, "limits", {}) or {}
        parts = [args.value, *args.more]
        if len(parts) == 2 and parts[1] == "off":
            own[parts[0]] = None
        elif len(parts) == 3 and parts[1] in ("min", "max"):
            own[parts[0]] = [parts[1], float(parts[2])]
        else:
            _say("Use: roblox-agent tune limit ClientFpsP10 min 25   (or: tune limit ClientFpsP10 off)")
            return 1
        db.set_setting(conn, "limits", own)
    elif args.what in ("off", "on"):
        if args.value not in checks.REGISTRY:
            _say("Checks: " + ", ".join(checks.REGISTRY))
            return 1
        off = set(db.get_setting(conn, "checks_off", []) or [])
        (off.add if args.what == "off" else off.discard)(args.value)
        db.set_setting(conn, "checks_off", sorted(off))
    _say(checks.describe(conn, uid))
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    if args.mine:
        conn, uid = _open_db(False)
        _say("Planting known problems into a copy of your game's history...")
        _say(selftest.run_on_own(conn, uid))
        return 0
    saved = selftest.saved_scorecard() or {}
    standard = args.save or args.seeds is None  # the standard run can be compared with the saved scorecard
    seeds = saved.get("seeds", 2) if standard else args.seeds
    calm = saved.get("calm_runs", 45) if standard else args.calm
    _say("Planting known problems in pretend games and checking they're found (about a minute)...")
    res = selftest.run_all(seeds=seeds, calm_runs=calm)
    _say(selftest.summary(res).split("\n\n", 1)[1])
    card = selftest.scorecard(res)
    _say("")
    _say(selftest.card_text(card, saved if standard and saved else None))
    if args.save:
        selftest.save_scorecard(card, seeds, calm)
        _say(f"Saved as the new scorecard: {selftest.SCORECARD}")
    return 0


def cmd_learned(args: argparse.Namespace) -> int:
    conn, uid = _open_db(args.demo)
    from . import learn
    _say(learn.report(conn, uid))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import mcp_server
    mcp_server.serve()
    return 0


def cmd_forget_key(args: argparse.Namespace) -> int:
    _say("API key deleted from this computer." if config.forget_api_key() else "No saved API key found.")
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    cfg = config.load()
    _say(f"Data folder : {config.home_dir()}")
    _say(f"Database    : {config.db_path()}")
    _say(f"Universe ID : {cfg.universe_id or 'not set'}")
    _say(f"API key     : {'saved' if cfg.api_key else 'not saved'} (never shown)")
    _say(f"AI agent    : {cfg.agent or 'not connected'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="roblox-agent",
        description="Daily check-up for your Roblox game. Run with no command to see today's top 3.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")
    p.set_defaults(func=cmd_home)

    sub.add_parser("setup", help="connect your game (and an AI agent)").set_defaults(func=cmd_setup)
    sp = sub.add_parser("view", help="open the check-up with charts in your browser")
    sp.add_argument("--demo", action="store_true", help="use the practice data")
    sp.add_argument("--out", help="where to save the page")
    sp.add_argument("--no-open", action="store_true", help="don't open the browser")
    sp.set_defaults(func=cmd_view)
    cp = sub.add_parser("connect", help="choose the AI agent to use (Claude, Codex, Cursor...)")
    cp.add_argument("agent", nargs="?", choices=[a.key for a in connect.AGENTS], help="skip the menu")
    cp.set_defaults(func=cmd_connect)
    mp = sub.add_parser("mark", help="mark item N of today's check-up: done, snooze or wrong")
    mp.add_argument("item", type=int)
    mp.add_argument("action", choices=list(checkup.ACTIONS))
    mp.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    mp.set_defaults(func=cmd_mark)
    chp = sub.add_parser("change", help="log a game change you shipped, so it gets judged later")
    chp.add_argument("title", help="short name, e.g. \"Shorter tutorial\"")
    chp.add_argument("--shipped", help="date it went live, YYYY-MM-DD (default today)")
    chp.add_argument("--target", help="metric it should move, e.g. ForwardD1Retention")
    chp.add_argument("--scores", help="skip the questions: 7 numbers 0-3 (reach,systems,spread,"
                                      "economy,paid,progress,undo)")
    chp.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    chp.set_defaults(func=cmd_change)
    ap = sub.add_parser("app", help="open the desktop app (setup, today's check-up, inbox, settings)")
    ap.add_argument("--demo", action="store_true", help="practice mode with made-up numbers")
    ap.add_argument("--browser", action="store_true", help="open in your browser instead of a window")
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    ap.set_defaults(func=cmd_app)
    dp = sub.add_parser("demo", help="try it with made-up numbers (no key needed)")
    dp.add_argument("--no-open", action="store_true")
    dp.set_defaults(func=cmd_demo)

    pp = sub.add_parser("pull", help="(advanced) download the latest data now")
    pp.add_argument("--backfill-days", type=int, default=ingest.DEFAULT_BACKFILL_DAYS,
                    help="how much history to keep (max 730, default 730)")
    pp.add_argument("--metrics", help="comma-separated metric names")
    pp.add_argument("--details", action="store_true", help="show every step")
    pp.set_defaults(func=cmd_pull)
    for name, fn, helptext in (("show", cmd_show, "(advanced) table of all stored metrics"),
                               ("export", cmd_export, "(advanced) save everything as a CSV spreadsheet")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--demo", action="store_true")
        if name == "export":
            sp.add_argument("--out")
        sp.set_defaults(func=fn)
    wp = sub.add_parser("week", help="(advanced) show the weekly check-up now")
    wp.add_argument("--demo", action="store_true")
    wp.set_defaults(func=cmd_week)
    yp = sub.add_parser("why", help="why player numbers are rising or falling")
    yp.add_argument("--demo", action="store_true", help="use the practice data")
    yp.set_defaults(func=cmd_why)
    tp = sub.add_parser("tune", help="(advanced) show or change how sensitive the checks are")
    tp.add_argument("what", nargs="?", choices=["sensitivity", "limit", "off", "on"])
    tp.add_argument("value", nargs="?")
    tp.add_argument("more", nargs="*")
    tp.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    tp.set_defaults(func=cmd_tune)
    stp = sub.add_parser("selftest", help="(advanced) prove the checks find planted problems")
    stp.add_argument("--mine", action="store_true", help="plant problems into a copy of your own game's data")
    stp.add_argument("--save", action="store_true", help=argparse.SUPPRESS)  # developers: new scorecard
    stp.add_argument("--seeds", type=int, default=None, help=argparse.SUPPRESS)
    stp.add_argument("--calm", type=int, default=30, help=argparse.SUPPRESS)
    stp.set_defaults(func=cmd_selftest)
    lp = sub.add_parser("learned", help="what it has learned about your game (clicks, changes, fixes)")
    lp.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    lp.set_defaults(func=cmd_learned)
    sub.add_parser("check", help="(advanced) test the API key").set_defaults(func=cmd_check)
    sub.add_parser("where", help="(advanced) where data and settings live").set_defaults(func=cmd_where)
    sub.add_parser("forget-key", help="(advanced) delete the saved API key").set_defaults(func=cmd_forget_key)
    sub.add_parser("mcp", help="(advanced) run the AI agent server; agents start this themselves"
                   ).set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows consoles: never crash on an unusual character
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _say("\nCancelled.")
        return 130
    except (ApiError, OSError, ValueError) as e:
        # Friendly one-line error, with the key scrubbed just in case.
        _say(f"\nSomething went wrong: {config.redact(str(e), config.load().api_key)}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
