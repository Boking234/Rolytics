"""Likely causes: why did this number move?

"Crash rate up 40%" is a symptom. This module looks for the most common
causes, using only numbers already on your computer, and names the best
supported one in a single short clause:

1. **Player mix** (``mix``). Phones crash more and run slower than PCs.
   If many more of today's players are on phones, the whole-game crash rate
   rises even though no device got worse. That is not a bug, and the fix is
   different (make the game lighter for that device, not hunt a new error).
   It is measured directly: the whole-game value is re-computed with the old
   per-device values and today's device shares. If that alone explains most
   of the move, the player mix is the cause.
2. **A wave of new players** (``traffic``). A video, an ad or a front-page
   boost brings many first-time players, who come back and spend less than
   regulars. Retention, conversion and session length then dip for a while.
   Measured as: new players a day (and how many people Roblox showed the game
   to) up a lot over the same days.
3. **Fewer people finding the game** (``discovery``). For a drop in players:
   is Roblox showing the game to fewer people, or are fewer of the people who
   see it pressing play? The first is reach, the second is the icon,
   thumbnail or title.
4. **Something you shipped** (``change``). A change you logged that shipped
   just before it started. Timing is a clue, not proof, so it is listed after
   the measured causes. If your past changes of the same kind moved the same
   number, it says so.

Roblox does not give a per-source breakdown of players (search, home page,
ads, friends) in the analytics API this tool uses, so "where the new players
came from" is inferred from new-player and impression numbers only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from . import catalog, decline, detect

MIX_SHARE = 0.6          # the device mix must explain at least this share of the move
MIX_MIN_SHIFT = 0.04     # ...and one device's share of players must move by 4 points or more
WAVE = 0.30              # new players up 30% or more over the same days
DISCOVERY = 0.15         # impressions or play rate down 15% or more
BASE_DAYS = 28
WAVE_HURTS = ("ForwardD1Retention", "ForwardD7Retention", "PayingUsersCVR", "AverageSessionLengthMinutes",
              "EndToEndCVR", "ClientFpsP10", "ServerFrameRateAvg")
PLAYER_METRICS = ("DailyActiveUsers", "Visits", "PeakConcurrentPlayers")


@dataclass
class Cause:
    kind: str            # mix | traffic | discovery | change
    text: str            # one short clause, already in plain words
    change_id: int | None = None
    device: str = ""     # for mix: the device whose share grew


def _mean(pts: list[detect.Point], lo: date, hi: date) -> float | None:
    vals = [p.value for p in pts if lo <= p.day <= hi]
    return sum(vals) / len(vals) if vals else None


def _windows(sig: detect.Signal) -> tuple[date, date, date, date]:
    """(now from, now to, before from, before to) for a finding."""
    lo = sig.since
    if sig.kind in ("trend", "level") or (sig.day - lo).days > 27:
        lo = sig.day - timedelta(days=13)
    return lo, sig.day, lo - timedelta(days=BASE_DAYS), lo - timedelta(days=1)


def _rel(pts: list[detect.Point] | None, win: tuple[date, date, date, date]) -> float | None:
    if not pts:
        return None
    a, b = _mean(pts, win[0], win[1]), _mean(pts, win[2], win[3])
    if a is None or not b:
        return None
    return (a - b) / abs(b)


def mix(conn: sqlite3.Connection, uid: int, sig: detect.Signal,
        series: dict[tuple[str, str], list[detect.Point]] | None = None) -> Cause | None:
    """Did the whole-game number move only because the device mix changed?"""
    spec = sig.spec
    if sig.split or spec.count or spec.name in PLAYER_METRICS or ("Platform",) not in spec.breakdowns:
        return None

    def load(metric: str, key: str) -> list[detect.Point]:
        if series is not None and (metric, key) in series:
            return series[(metric, key)]
        return detect.load_series(conn, uid, metric, key)

    win = _windows(sig)
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT breakdown_key FROM data_points WHERE universe_id=? AND metric=? AND breakdown_key"
        " LIKE 'Platform=%'", (uid, spec.name))]
    old_w, new_w, old_v, new_v = {}, {}, {}, {}
    for key in keys:
        players = load("DailyActiveUsers", key)
        vals = load(spec.name, key)
        po, pn = _mean(players, win[2], win[3]), _mean(players, win[0], win[1])
        vo, vn = _mean(vals, win[2], win[3]), _mean(vals, win[0], win[1])
        if None in (po, pn, vo, vn):
            continue
        old_w[key], new_w[key], old_v[key], new_v[key] = po, pn, vo, vn
    if len(old_w) < 2:
        return None
    to, tn = sum(old_w.values()), sum(new_w.values())
    if not to or not tn:
        return None
    old = sum(old_w[k] / to * old_v[k] for k in old_w)
    now = sum(new_w[k] / tn * new_v[k] for k in old_w)
    mix_only = sum(new_w[k] / tn * old_v[k] for k in old_w)
    move = now - old
    if not move or (move > 0) != (sig.rel > 0):
        return None
    share = (mix_only - old) / move
    if share < MIX_SHARE:
        return None
    grew = max(old_w, key=lambda k: new_w[k] / tn - old_w[k] / to)
    s_old, s_new = old_w[grew] / to, new_w[grew] / tn
    if s_new - s_old < MIX_MIN_SHIFT:
        return None
    device = grew.split("=", 1)[1]
    plural = {"Phone": "phones", "Tablet": "tablets", "Computer": "computers", "Console": "consoles"}.get(
        device, device)
    return Cause("mix", f"Likely cause: more players on {plural} ({s_old * 100:.0f}% to {s_new * 100:.0f}% of "
                        f"players), where this is normally {'higher' if old_v[grew] > old else 'lower'}; each "
                        "device on its own looks normal, so it is the player mix, not a new bug", device=device)


def traffic(conn: sqlite3.Connection, uid: int, sig: detect.Signal) -> Cause | None:
    """A wave of first-time players (worse retention and spending) or fewer people finding the game."""
    win = _windows(sig)
    new = _rel(detect.load_series(conn, uid, "DailyActiveUsers", "IsNewUser=true"), win)
    seen = _rel(detect.load_series(conn, uid, "UniqueUsersWithImpressions", ""), win)
    if not sig.good and sig.metric in WAVE_HURTS and new is not None and new >= WAVE:
        extra = f", and Roblox showed the game to {seen * 100:+.0f}% people" if seen and seen >= WAVE else ""
        return Cause("traffic", f"Likely cause: a wave of new players ({new * 100:+.0f}% new players a day over "
                                f"the same days{extra}); first-time visitors come back and spend less, so this "
                                "may ease as the wave passes")
    if not sig.good and sig.metric in PLAYER_METRICS and new is not None and new <= -DISCOVERY:
        play = _rel(detect.load_series(conn, uid, "EndToEndCVR", ""), win)
        if seen is not None and seen <= -DISCOVERY and (play is None or play > seen):
            return Cause("discovery", f"Likely cause: Roblox is showing the game to fewer people "
                                      f"({seen * 100:+.0f}%), so fewer new players arrive ({new * 100:+.0f}%)")
        if play is not None and play <= -DISCOVERY:
            return Cause("discovery", f"Likely cause: fewer of the people who see the game press play "
                                      f"({play * 100:+.0f}%), so fewer new players arrive ({new * 100:+.0f}%)")
    return None


def change(conn: sqlite3.Connection, uid: int, sig: detect.Signal,
           pts: list[detect.Point] | None = None) -> Cause | None:
    """A logged change that shipped just before the problem started."""
    if sig.kind in ("trend", "level"):
        tp = decline.turning_point(pts or detect.load_series(conn, uid, sig.metric, sig.split), sig.spec)
        if not tp:
            return None
        start, lo, hi = tp[0], tp[0] - timedelta(days=10), tp[0] + timedelta(days=3)
    elif sig.kind == "budget":
        return None
    else:
        start, lo, hi = sig.since, sig.since - timedelta(days=3), sig.since
    rows = conn.execute("SELECT * FROM changes WHERE universe_id=? AND shipped_on BETWEEN ? AND ?"
                        " ORDER BY shipped_on DESC, id DESC", (uid, lo.isoformat(), hi.isoformat())).fetchall()
    if not rows:
        return None
    row = rows[0]
    shipped = date.fromisoformat(row["shipped_on"])
    gap = (start - shipped).days
    when = ("the same day it shipped" if gap <= 0 else "the day after it shipped" if gap == 1
            else f"{gap} days after it shipped")
    text = f"Likely cause: change #{row['id']} '{row['title']}', it started {when}"
    from . import changes  # local import: changes imports checkup, which imports this module
    lesson = changes.similar_effect(conn, uid, row, sig.metric, worse=not sig.good)
    if lesson:
        text += f" ({lesson})"
    if len(rows) > 1:
        text += f"; #{rows[1]['id']} shipped around then too"
    return Cause("change", text, change_id=row["id"])


def find(conn: sqlite3.Connection, uid: int, sig: detect.Signal,
         series: dict[tuple[str, str], list[detect.Point]] | None = None) -> Cause | None:
    """The best supported cause for one finding, or None. Measured causes come before timing."""
    pts = (series or {}).get((sig.metric, sig.split))
    for fn in (lambda: mix(conn, uid, sig, series), lambda: traffic(conn, uid, sig),
               lambda: change(conn, uid, sig, pts)):
        c = fn()
        if c:
            return c
    return None


def label(metric: str) -> str:
    return catalog.get(metric).label
