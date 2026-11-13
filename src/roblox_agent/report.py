"""Turn the local database into things a person can read: a text summary,
a CSV export, and a one-page HTML report with charts. All offline."""

from __future__ import annotations

import csv
import html
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import catalog

STATUS_NOTE = {
    "Projected": "estimate, may change",
    "NotStatisticallySignificant": "too few players to trust",
}


@dataclass
class MetricSummary:
    metric: str
    label: str
    unit: str
    dashboard_page: str
    points: int
    first_day: str | None
    last_day: str | None
    last_value: float | None
    last_status: str | None
    avg_7d: float | None
    avg_prev_7d: float | None
    not_significant: int

    @property
    def change_pct(self) -> float | None:
        if self.avg_7d is None or not self.avg_prev_7d:
            return None
        return (self.avg_7d - self.avg_prev_7d) / abs(self.avg_prev_7d) * 100


def fmt(v: float | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:,.1f}"
    return f"{v:.4g}"


def _series(conn: sqlite3.Connection, universe_id: int, metric: str, key: str = "") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bucket_time, value, status FROM data_points WHERE universe_id=? AND metric=?"
        " AND granularity='OneDay' AND breakdown_key=? ORDER BY bucket_time",
        (universe_id, metric, key)).fetchall()


def _avg(rows: list[sqlite3.Row]) -> float | None:
    vals = [r["value"] for r in rows if r["value"] is not None
            and r["status"] != "NotStatisticallySignificant"]
    return sum(vals) / len(vals) if vals else None


def stored_metrics(conn: sqlite3.Connection, universe_id: int) -> list[str]:
    known = [m.name for m in catalog.DEFAULT_METRICS]
    rows = conn.execute("SELECT DISTINCT metric FROM data_points WHERE universe_id=?", (universe_id,))
    found = {r["metric"] for r in rows}
    return [m for m in known if m in found] + sorted(found - set(known))


def summarize(conn: sqlite3.Connection, universe_id: int) -> list[MetricSummary]:
    out = []
    for name in stored_metrics(conn, universe_id):
        spec = catalog.get(name)
        rows = _series(conn, universe_id, name)
        last = rows[-1] if rows else None
        out.append(MetricSummary(
            metric=name, label=spec.label, unit=spec.unit, dashboard_page=spec.dashboard_page,
            points=len(rows),
            first_day=rows[0]["bucket_time"][:10] if rows else None,
            last_day=last["bucket_time"][:10] if last else None,
            last_value=last["value"] if last else None,
            last_status=last["status"] if last else None,
            avg_7d=_avg(rows[-7:]), avg_prev_7d=_avg(rows[-14:-7]),
            not_significant=sum(1 for r in rows if r["status"] == "NotStatisticallySignificant"),
        ))
    return out


def text_summary(conn: sqlite3.Connection, universe_id: int) -> str:
    items = summarize(conn, universe_id)
    if not items:
        return "No data yet. Run:  roblox-agent pull"
    lines = [f"Game (Universe ID) {universe_id}", ""]
    header = f"{'Metric':34} {'Latest day':11} {'Latest':>12} {'7-day avg':>12} {'vs prev 7d':>10}  Note"
    lines += [header, "-" * len(header)]
    for s in items:
        change = "-" if s.change_pct is None else f"{s.change_pct:+.1f}%"
        note = STATUS_NOTE.get(s.last_status or "", "")
        lines.append(f"{s.label[:34]:34} {s.last_day or '-':11} {fmt(s.last_value):>12} "
                     f"{fmt(s.avg_7d):>12} {change:>10}  {note}")
    total = sum(s.points for s in items)
    nss = sum(s.not_significant for s in items)
    lines += ["", f"{total} daily values stored (whole-game series)."]
    if total:
        share = nss / total * 100
        lines.append(f"{share:.0f}% are marked 'too few players to trust' by Roblox.")
        if share > 30:
            lines.append("That's high: checks in later stages will skip those days, so expect fewer alerts.")
    last_run = conn.execute("SELECT * FROM pull_runs WHERE universe_id=? ORDER BY id DESC LIMIT 1",
                            (universe_id,)).fetchone()
    if last_run:
        lines.append(f"Last pull: {last_run['started_at']} ({last_run['status']}, "
                     f"{last_run['points_saved']} values saved).")
    return "\n".join(lines)


def export_csv(conn: sqlite3.Connection, universe_id: int, path: Path) -> int:
    rows = conn.execute(
        "SELECT metric, breakdown_key, substr(bucket_time,1,10) AS day, value, status, updated_at"
        " FROM data_points WHERE universe_id=? ORDER BY metric, breakdown_key, bucket_time",
        (universe_id,)).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric", "split_by", "day", "value", "roblox_status", "last_updated"])
        for r in rows:
            w.writerow([r["metric"], r["breakdown_key"] or "whole game", r["day"], r["value"],
                        r["status"] or "", r["updated_at"]])
    return len(rows)


# ---------------------------------------------------------------- HTML ---

def _sparkline(rows: list[sqlite3.Row], width: int = 560, height: int = 120) -> str:
    pts = [(i, r["value"], r["status"]) for i, r in enumerate(rows) if r["value"] is not None]
    if len(pts) < 2:
        return '<p class="muted">Not enough days to draw a chart yet.</p>'
    vals = [v for _, v, _ in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = len(rows) - 1 or 1
    pad = 6

    def xy(i: int, v: float) -> tuple[float, float]:
        return (pad + i / n * (width - 2 * pad),
                height - pad - (v - lo) / span * (height - 2 * pad))

    path = " ".join(("M" if k == 0 else "L") + "{:.1f},{:.1f}".format(*xy(i, v)) for k, (i, v, _) in enumerate(pts))
    dots = "".join(
        '<circle cx="{:.1f}" cy="{:.1f}" r="3" class="dot-{}"><title>{}</title></circle>'.format(*xy(i, v), "nss" if s == "NotStatisticallySignificant" else "proj",
           html.escape(STATUS_NOTE.get(s, "")))
        for i, v, s in pts if s in STATUS_NOTE)
    return (f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
            f'aria-label="trend from {fmt(lo)} to {fmt(hi)}">'
            f'<path d="{path}" class="line"/>{dots}</svg>'
            f'<div class="axis"><span>{rows[0]["bucket_time"][:10]}</span>'
            f'<span>low {fmt(lo)} · high {fmt(hi)}</span><span>{rows[-1]["bucket_time"][:10]}</span></div>')


def _breakdown_table(conn: sqlite3.Connection, universe_id: int, metric: str, day: str | None) -> str:
    if not day:
        return ""
    rows = conn.execute(
        "SELECT breakdown_key, value, status FROM data_points WHERE universe_id=? AND metric=?"
        " AND granularity='OneDay' AND breakdown_key!='' AND substr(bucket_time,1,10)=? ORDER BY value DESC",
        (universe_id, metric, day)).fetchall()
    if not rows:
        return ""
    body = "".join(
        f"<tr><td>{html.escape(r['breakdown_key'].replace('=', ': '))}</td><td>{fmt(r['value'])}</td>"
        f"<td class='muted'>{html.escape(STATUS_NOTE.get(r['status'] or '', ''))}</td></tr>" for r in rows)
    return f"<details><summary>Split on {day}</summary><table>{body}</table></details>"


def html_report(conn: sqlite3.Connection, universe_id: int, check=None, days: int = 90) -> str:
    """One calm page: today's status and top 3 first, everything else folded away."""
    from . import checkup as _checkup  # local import avoids a cycle
    check = check or _checkup.run(conn, universe_id)
    tone = {"All good": "good", "Watch": "watch", "Act now": "act"}.get(check.status, "none")
    items = "".join(
        f'<li class="{"win" if f.good else "issue"}"><span class="tag">{"Good" if f.good else "Issue"}</span>'
        f"{html.escape(f.line().split(': ', 1)[1])}</li>" for f in check.top)
    if not items and check.checked:
        items = f'<li class="win"><span class="tag">Calm</span>No unusual changes across {check.checked} metrics.</li>'
    notes = "".join(f'<p class="muted">{html.escape(n)}</p>' for n in check.notes)

    cards = []
    for s in summarize(conn, universe_id):
        rows = _series(conn, universe_id, s.metric)[-days:]
        recent = "".join(
            f"<tr><td>{r['bucket_time'][:10]}</td><td>{fmt(r['value'])}</td>"
            f"<td class='muted'>{html.escape(STATUS_NOTE.get(r['status'] or '', ''))}</td></tr>"
            for r in reversed(rows[-14:]))
        change = "" if s.change_pct is None else f"{s.change_pct:+.1f}% vs last week"
        cards.append(f"""
<section class="card">
  <h3>{html.escape(s.label)}</h3>
  <p class="big">{fmt(s.last_value)} <span class="unit">{html.escape(s.unit)}</span></p>
  <p class="muted">{s.last_day} · {change}</p>
  {_sparkline(rows)}
  <details><summary>Last 14 days</summary><table>{recent}</table></details>
  {_breakdown_table(conn, universe_id, s.metric, s.last_day)}
  <p class="muted small">Dashboard: {html.escape(s.dashboard_page or 'Analytics')} · {html.escape(s.metric)}</p>
</section>""")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    grid = "".join(cards) or "<p>No data yet. Run <code>roblox-agent</code> first.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Game check-up</title>
<style>
:root {{ --bg:#f6f6f4; --card:#fff; --ink:#1b1b1d; --muted:#6b6b70; --line:#2f6fde; --rule:#e4e4e0;
  --good:#1f8a4c; --watch:#b7791f; --act:#c23b3b; --none:#6b6b70; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#131315; --card:#1d1d20; --ink:#ececee; --muted:#9a9aa0;
  --line:#6ea0ff; --rule:#2c2c30; --good:#4cc47f; --watch:#f0b040; --act:#ff7a7a; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:32px 16px 64px; }}
.status {{ font-size:13px; letter-spacing:.06em; text-transform:uppercase; color:var(--{tone}); font-weight:600; margin:0; }}
h1 {{ font-size:30px; margin:4px 0 4px; }}
.top {{ list-style:none; padding:0; margin:20px 0 8px; }}
.top li {{ background:var(--card); border:1px solid var(--rule); border-left:4px solid var(--good); border-radius:10px;
  padding:12px 14px; margin-bottom:10px; }}
.top li.issue {{ border-left-color:var(--act); }}
.tag {{ display:inline-block; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); margin-right:8px; }}
.muted {{ color:var(--muted); font-size:13px; margin:4px 0; }} .small {{ font-size:11px; }}
details.all > summary {{ margin-top:28px; font-weight:600; cursor:pointer; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; margin-top:12px; }}
.card {{ background:var(--card); border:1px solid var(--rule); border-radius:10px; padding:14px; }}
h3 {{ margin:0; font-size:14px; font-weight:600; }}
.big {{ font-size:24px; margin:6px 0 0; font-weight:600; }} .unit {{ font-size:13px; color:var(--muted); font-weight:400; }}
.chart {{ width:100%; height:auto; margin-top:8px; }} .line {{ fill:none; stroke:var(--line); stroke-width:2; }}
.dot-proj {{ fill:var(--watch); }} .dot-nss {{ fill:var(--act); }}
.axis {{ display:flex; justify-content:space-between; font-size:11px; color:var(--muted); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:6px; }}
td {{ padding:3px 4px; border-bottom:1px solid var(--rule); }} summary {{ cursor:pointer; margin-top:8px; font-size:13px; }}
</style></head><body><main>
<p class="status">{html.escape(check.status)}</p>
<h1>Today's check-up</h1>
<p class="muted">Data through {html.escape(check.day or '-')} (UTC) · game {universe_id}</p>
<ul class="top">{items}</ul>
{notes}
<details class="all"><summary>All metrics</summary>
<p class="muted">Orange dots: Roblox estimate, may change. Red dots: too few players to trust. Made {generated} on your computer.</p>
<div class="grid">{grid}</div></details>
</main></body></html>"""
