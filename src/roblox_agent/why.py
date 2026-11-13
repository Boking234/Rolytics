"""Why are fewer (or more) people playing?

Player numbers can only fall for two reasons: fewer new players arrive, or
the players you have come back less. This module splits a change in daily
players into those two parts, the way growth teams do with "growth
accounting" (new + returning vs lost; Amplitude calls its version Lifecycle,
investors call the summary number the quick ratio), then lists the clues
that point at a cause.

The split uses only numbers Roblox already gives per day:

    players today = new players today + returning players today
    come-back rate = returning players today / all players yesterday

In a steady game, players = new players / (1 - come-back rate). So a drop in
players is the sum of the drop in new players and the rise in (1 - come-back
rate), on a percent scale, and each part's share of the drop can be named.

The same sum gives the "needed" line: at today's come-back rate, how many
new players a day it takes to hold the old level, next to how many arrive.
(Investors' "quick ratio" is these two numbers divided: under 1 shrinks.)
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import catalog, decline, detect, priors
from .detect import Point

STEADY = 0.05

CLUES = ("ForwardD1Retention", "ForwardD7Retention", "ForwardD30Retention", "AverageSessionLengthMinutes",
         "DauMauStickiness", "UniqueUsersWithImpressions", "EndToEndCVR", "ClientCrashRate15m", "ClientFpsP10")
PLAIN = {
    "UniqueUsersWithImpressions": "fewer people are shown the game",
    "EndToEndCVR": "fewer of the people who see it start playing (icon, thumbnail, title)",
    "ForwardD1Retention": "the first session got worse",
    "ForwardD7Retention": "players run out of reasons to return in the first week",
    "ForwardD30Retention": "long-term players are leaving",
    "AverageSessionLengthMinutes": "sessions got shorter",
    "ClientCrashRate15m": "crashes went up",
    "ClientFpsP10": "the game runs slower",
}


@dataclass
class Why:
    end: date
    now: float
    before: float
    rel: float
    start: date | None = None          # when the change began, if clear
    shape: str = ""
    cause: str = ""                    # new | return | both | none | unknown
    new_rel: float | None = None       # change in new players a day
    stay_now: float | None = None      # come-back rate
    stay_before: float | None = None
    new_share: float | None = None     # share of the drop explained by new players
    new_now: float | None = None       # new players a day now
    new_needed: float | None = None    # new players a day needed to hold the old level
    clues: list[str] = field(default_factory=list)
    device: str = ""
    changes: list[str] = field(default_factory=list)
    lifecycle: str = ""
    bench: str = ""

    def main_cause(self) -> str:
        if self.cause == "new":
            return (f"fewer new players arriving ({self.new_rel * 100:+.0f}% new players a day); "
                    f"existing players come back about as often as before")
        if self.cause == "return":
            return (f"players coming back less (each day {self.stay_now * 100:.0f}% of yesterday's players "
                    f"return, was {self.stay_before * 100:.0f}%); new players are "
                    f"{'steady' if abs(self.new_rel or 0) < STEADY else f'{self.new_rel * 100:+.0f}%'}")
        if self.cause == "both":
            return (f"both fewer new players ({self.new_rel * 100:+.0f}%, about {self.new_share * 100:.0f}% of "
                    f"the drop) and players coming back less ({self.stay_now * 100:.0f}% return each day, "
                    f"was {self.stay_before * 100:.0f}%)")
        return ""

    def text(self) -> str:
        word = "down" if self.rel < 0 else "up"
        head = f"Players: {self.now:,.0f} a day over the last 2 weeks"
        if abs(self.rel) < STEADY:
            head += f", about the same as before ({self.rel * 100:+.0f}%)."
        else:
            ref = (f"the 4 weeks before {self.start:%b} {self.start.day}" if self.start
                   else "the 4 weeks before that")
            head += f", {word} {abs(self.rel) * 100:.0f}% vs {ref} ({self.before:,.0f})."
            if self.start:
                head += (" It dropped at once." if self.shape == "step" and self.rel < 0 else
                         " It has been sliding since then." if self.rel < 0 else "")
        out = [head]
        cause = self.main_cause()
        if cause:
            out.append("Main cause: " + cause + ".")
        elif self.cause == "none" and self.new_rel is not None:
            out.append(f"New players {self.new_rel * 100:+.0f}%; {self.stay_now * 100:.0f}% of yesterday's "
                       f"players return each day (was {self.stay_before * 100:.0f}%).")
        elif self.cause == "unknown":
            out.append("Can't split new vs returning players yet (no 'new players' numbers stored).")
        if self.new_needed and self.rel <= -STEADY and self.new_now is not None:
            out.append(f"To hold {self.before:,.0f} players a day at today's come-back rate the game needs about "
                       f"{self.new_needed:,.0f} new players a day; it gets {self.new_now:,.0f}.")
        if self.clues:
            out.append("Clues: " + "; ".join(self.clues) + ".")
        if self.device:
            out.append(self.device)
        if self.changes:
            out.append("Shipped around then: " + "; ".join(self.changes) + ".")
        if self.lifecycle:
            out.append(self.lifecycle)
        if self.bench:
            out.append(self.bench)
        return "\n".join(out)


_mean = decline._avg


def _stay(dau: list[Point], new: list[Point], lo: date, hi: date) -> tuple[float, float] | None:
    """(come-back rate, new players a day) averaged over a window."""
    d = {p.day: p.value for p in dau}
    n = {p.day: p.value for p in new}
    rates, news = [], []
    day = lo
    while day <= hi:
        y = day - timedelta(days=1)
        if day in d and day in n and d.get(y):
            rates.append((d[day] - n[day]) / d[y])
            news.append(n[day])
        day += timedelta(days=1)
    if len(rates) < 5:
        return None
    return sum(rates) / len(rates), sum(news) / len(news)


def explain(conn: sqlite3.Connection, uid: int) -> Why | None:
    dau = detect.load_series(conn, uid, "DailyActiveUsers", "")
    win = decline.windows(dau)
    if not win:
        return None
    now_lo, end, ref_lo, ref_hi, tp = win
    now, before = _mean(dau, now_lo, end), _mean(dau, ref_lo, ref_hi)
    if not now or not before:
        return None
    w = Why(end, now, before, (now - before) / before)
    if tp and abs(w.rel) >= STEADY:
        w.start, w.shape = tp

    new = detect.load_series(conn, uid, "DailyActiveUsers", "IsNewUser=true")
    a, b = _stay(dau, new, now_lo, end), _stay(dau, new, ref_lo, ref_hi)
    if a and b and b[1] > 0 and a[1] > 0 and a[0] < 0.999 and b[0] < 0.999:
        (w.stay_now, new_now), (w.stay_before, new_before) = a, b
        w.new_rel = (new_now - new_before) / new_before
        c_new = math.log(new_now / new_before)
        c_stay = -math.log((1 - w.stay_now) / (1 - w.stay_before))
        w.new_now, w.new_needed = new_now, (1 - w.stay_now) * before
        sign = -1 if w.rel < 0 else 1
        if abs(w.rel) < STEADY:
            w.cause = "none"
        else:
            p_new, p_stay = max(0.0, sign * c_new), max(0.0, sign * c_stay)
            if p_new + p_stay <= 0:
                w.cause = "none"
            else:
                w.new_share = p_new / (p_new + p_stay)
                kinds = ("new", "return", "both")
                w.cause = kinds[0] if w.new_share >= 0.65 else kinds[1] if w.new_share <= 0.35 else kinds[2]
                if w.rel > 0:
                    w.cause = "none"  # growth: the numbers are shown, no "cause of the drop"
    else:
        w.cause = "unknown"

    # Clues: what else moved over the same two windows. Bad news first, 4 at most.
    ranked: list[tuple[bool, str]] = []
    for name in CLUES:
        spec = catalog.get(name)
        pts = detect.load_series(conn, uid, name, "")
        x, y = _mean(pts, now_lo, end), _mean(pts, ref_lo, ref_hi)
        if x is None or not y:
            continue
        rel = (x - y) / abs(y)
        if abs(rel) < max(STEADY, spec.min_rel / 2) or abs(x - y) < spec.min_abs / 2:
            continue
        from .checkup import fmt_change, fmt_value  # local import: checkup imports this module
        text = f"{spec.label} {fmt_value(spec, x)} vs {fmt_value(spec, y)} ({fmt_change(spec, x, y, rel)})"
        bad = (rel > 0) != spec.higher_is_better
        if bad and w.rel < 0 and name in PLAIN:
            text += f": {PLAIN[name]}"
        ranked.append((not (bad and w.rel < 0), text))
    w.clues = [t for _, t in sorted(ranked, key=lambda x: x[0])][:4]

    # Device: where is the loss?
    if w.rel <= -STEADY:
        losses = {}
        for (key,) in conn.execute("SELECT DISTINCT breakdown_key FROM data_points WHERE universe_id=?"
                                   " AND metric='DailyActiveUsers' AND breakdown_key LIKE 'Platform=%'", (uid,)):
            pts = detect.load_series(conn, uid, "DailyActiveUsers", key)
            x, y = _mean(pts, now_lo, end), _mean(pts, ref_lo, ref_hi)
            if x is not None and y:
                losses[key.split("=", 1)[1]] = (y - x, (x - y) / y)
        total = sum(v for v, _ in losses.values() if v > 0)
        if total > 0 and len(losses) > 1:
            name, (loss, rel) = max(losses.items(), key=lambda kv: kv[1][0])
            if loss / total >= 0.6:
                w.device = f"Device: {loss / total * 100:.0f}% of the loss is on {name} ({rel * 100:+.0f}%)."
            else:
                w.device = "Device: the loss is spread across devices, so it is not one device's bug."

    if w.start:
        for c in conn.execute("SELECT id, title, shipped_on FROM changes WHERE universe_id=? AND shipped_on"
                              " BETWEEN ? AND ? ORDER BY shipped_on",
                              (uid, (w.start - timedelta(days=10)).isoformat(),
                               (w.start + timedelta(days=3)).isoformat())):
            day = date.fromisoformat(c["shipped_on"])
            w.changes.append(f"#{c['id']} '{c['title']}' ({day:%b} {day.day})")

    life = decline.lifecycle(conn, uid)
    if life:
        w.lifecycle = life.text()
    d1 = detect.load_series(conn, uid, "ForwardD1Retention", "")
    d1_now = _mean(d1, now_lo, end)
    if d1_now is not None:
        b = priors.benchmark("ForwardD1Retention", now, d1_now)
        if b:
            w.bench = (f"Day 1 retention {d1_now * 100:.0f}% is {b} for user-made games this size (rough guide "
                       "from public Fortnite island data; the Roblox dashboard's benchmark is the better one).")
    return w


def short(conn: sqlite3.Connection, uid: int) -> str:
    """One clause for the daily check-up item."""
    w = explain(conn, uid)
    if not w or w.rel > -STEADY or w.cause in ("", "none", "unknown"):
        return ""
    if w.cause == "new":
        return f"Mostly fewer new players ({w.new_rel * 100:+.0f}%)"
    if w.cause == "return":
        return f"Mostly players coming back less ({w.stay_now * 100:.0f}% return daily, was {w.stay_before * 100:.0f}%)"
    return "Both fewer new players and players coming back less"


def report(conn: sqlite3.Connection, uid: int) -> str:
    w = explain(conn, uid)
    return w.text() if w else "Not enough history yet to explain player numbers (needs about 4 weeks)."
