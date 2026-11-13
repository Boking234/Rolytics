"""Command line: ``roblox-agent <command>``. Run ``roblox-agent --help``."""

from __future__ import annotations

import argparse
import getpass
import sys
import webbrowser
from datetime import timedelta
from pathlib import Path

from . import __version__, catalog, config, db, ingest, report
from .api import AnalyticsClient, ApiError, AuthError
from .demo import FakeRoblox

DEMO_UNIVERSE = 1


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _client(cfg: config.Config) -> AnalyticsClient:
    if not cfg.universe_id or not cfg.api_key:
        raise SystemExit("Not set up yet. Run:  roblox-agent setup")
    return AnalyticsClient(api_key=cfg.api_key, universe_id=cfg.universe_id)


def _test_connection(client: AnalyticsClient) -> bool:
    end = ingest.today_utc()
    start = end - timedelta(days=7)
    try:
        op = client.query_metric("DailyActiveUsers", "OneDay", ingest._iso(start), ingest._iso(end))
    except AuthError as e:
        _say(f"Connection failed: {e}")
        return False
    except ApiError as e:
        _say(f"Connection failed: {e}")
        return False
    points = [dp for s in op.get("response", {}).get("values", []) for dp in s.get("dataPoints", [])]
    _say(f"Connected. Roblox returned {len(points)} days of daily active users:")
    for dp in points:
        note = report.STATUS_NOTE.get(dp.get("status", ""), "")
        _say(f"  {dp['time'][:10]}  {report.fmt(dp.get('value'))}  {note}")
    return True


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = config.load()
    _say("Setup. Your answers are saved only on this computer, in " + str(config.home_dir()))
    _say()
    while True:
        prompt = "Universe ID (or paste your game's Creator Dashboard link)"
        if cfg.universe_id:
            prompt += f" [{cfg.universe_id}]"
        raw = input(prompt + ": ").strip()
        if not raw and cfg.universe_id:
            uid = cfg.universe_id
            break
        try:
            uid = config.parse_universe_id(raw)
            break
        except ValueError as e:
            _say(str(e))
    config.save_universe_id(uid)
    _say()
    if cfg.api_key and input("An API key is already saved. Replace it? [y/N]: ").strip().lower() != "y":
        key = cfg.api_key
    else:
        _say("Paste your API key. It will not show on screen while you paste; press Enter after.")
        key = getpass.getpass("API key: ").strip()
        config.save_api_key(key)
    _say("Saved. Testing the connection...")
    ok = _test_connection(AnalyticsClient(api_key=key, universe_id=uid))
    if ok:
        _say("\nAll set. Next run:  roblox-agent pull")
    return 0 if ok else 1


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
    _say(f"Pulling analytics for Universe ID {client.universe_id}. The first run backfills "
         f"{args.backfill_days} days and can take a few minutes.")
    try:
        result = ingest.pull(client, conn, _pick_metrics(args.metrics), args.backfill_days, progress=_say)
    except AuthError as e:
        _say(f"\nStopped: {e}")
        return 1
    _say()
    _say(f"Done ({result.status}). Saved {result.points} values to {config.db_path()}")
    if result.errors:
        _say(f"{len(result.errors)} item(s) were skipped:")
        for e in result.errors:
            _say("  - " + config.redact(e, cfg.api_key))
    _say("See it with:  roblox-agent view   (or roblox-agent show)")
    return 0 if result.status != "failed" else 1


def _open_db(demo: bool) -> tuple[object, int]:
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
    out.write_text(report.html_report(conn, uid), encoding="utf-8")
    _say(f"Report saved to {out}")
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
    client = AnalyticsClient(api_key="demo-key", universe_id=DEMO_UNIVERSE, transport=FakeRoblox(),
                             sleep=lambda _s: None)
    _say("Making a practice database with pretend numbers (no internet, no key needed)...")
    result = ingest.pull(client, conn, backfill_days=120)
    _say(f"Saved {result.points} pretend values.\n")
    _say(report.text_summary(conn, DEMO_UNIVERSE))
    _say()
    _write_view(conn, DEMO_UNIVERSE, config.home_dir() / "demo-report.html", not args.no_open)
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
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roblox-agent",
                                description="Pull your Roblox game's analytics into a local database.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="save your Universe ID and API key, then test them").set_defaults(func=cmd_setup)
    sub.add_parser("check", help="test the connection to Roblox").set_defaults(func=cmd_check)

    pp = sub.add_parser("pull", help="download the latest analytics into the local database")
    pp.add_argument("--backfill-days", type=int, default=ingest.DEFAULT_BACKFILL_DAYS,
                    help="how far back to go on the first run (default 365)")
    pp.add_argument("--metrics", help="comma-separated metric names (default: the standard set)")
    pp.set_defaults(func=cmd_pull)

    for name, fn, helptext in (("show", cmd_show, "print a summary of the stored data"),
                               ("view", cmd_view, "open a report page with charts in your browser"),
                               ("export", cmd_export, "save all stored data as a CSV spreadsheet")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--demo", action="store_true", help="use the practice database")
        if name in ("view", "export"):
            sp.add_argument("--out", help="where to save the file")
        if name == "view":
            sp.add_argument("--no-open", action="store_true", help="don't open the browser")
        sp.set_defaults(func=fn)

    dp = sub.add_parser("demo", help="try everything with pretend data (no key needed)")
    dp.add_argument("--no-open", action="store_true")
    dp.set_defaults(func=cmd_demo)
    sub.add_parser("forget-key", help="delete the saved API key").set_defaults(func=cmd_forget_key)
    sub.add_parser("where", help="show where your data and settings are").set_defaults(func=cmd_where)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _say("\nCancelled.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
