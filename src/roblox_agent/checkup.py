"""The daily check-up: one status and the 3 things that matter most today.

All the math runs here, on your computer, for free. An AI agent only ever
receives the short text this produces (well under 1,000 tokens), never the
raw data.

How it works
------------
1. ``detect`` checks every metric, for the whole game and per device, for
   sudden changes, slow slides and broken rule-of-thumb limits.
2. Findings that tell one story are merged: a crash spike that is really a
   phone crash spike is reported once, on phones; "players down" and "visits
   down" become one item with an "Also:" note.
3. Each finding gets a rough size in players or Robux and a confidence.
4. Findings are ranked (issues first) by how much the metric matters x how
   big the change is x how many players it reaches x confidence. Problems
   that have been shown for several days sink a little so new news can surface,
   but never disappear while they last.
5. The status is "Act now" only for a large, confident problem in crashes,
   players, retention or money. Any other problem gives "Watch".
6. Each problem gets its likely cause (``causes``: device mix, a wave of new
   players, discovery, or a change you shipped) and one thing to try first
   (``fixes``). Items like the ones you usually act on rank a little higher,
   ones you usually dismiss a little lower (``learn``).

The check-up remembers what it showed (``findings`` table), so it can say
"day 3" or "still there after you marked it fixed", and you (or your agent)
can mark an item done, snooze it, or say it was wrong.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import catalog, causes, checks, db, detect, fixes, learn
from .detect import Signal

NSS = detect.NSS
STATUSES = ("Act now", "Watch", "All good", "No data")
SUDDEN = ("spike", "shift")
CONF_FACTOR = {"high": 1.0, "medium": 0.75, "low": 0.5}
KIND_FACTOR = {"spike": 1.0, "shift": 1.0, "trend": 0.7, "level": 0.7, "budget": 0.5}
SLOW = ("trend", "level")
FAMILY_HEAD = ("DailyActiveUsers", "ForwardD1Retention", "DailyRevenue", "ClientCrashRate15m")
REPEAT_DAMPING = 0.6      # after 3 days on the list, an ongoing item sinks a little
LEARNING_DAYS = 24        # days of history before the checks are fully tuned
DEFAULT_REACH = 0.25
SNOOZE_DAYS = 7
WRONG_DAYS = 30
DONE_GRACE_DAYS = 3       # a fix needs a few days to show in the numbers
MIN_AFFECTED = 5          # players a day; smaller problems are counted, not shown


# ---------------------------------------------------------------- format ---

def _num(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.3g}"


def fmt_value(spec: catalog.MetricSpec, v: float) -> str:
    if spec.rate:
        pct = v * 100
        return f"{pct:.2f}%" if abs(pct) < 1 else f"{pct:.1f}%"
    if spec.count:
        return f"{v:.0f}" if v >= 10 or v == int(v) else f"{v:.1f}"
    if spec.unit in ("players", "visits", "people") and abs(v) >= 20:
        return f"{v:,.0f}"
    unit = {"fps": " fps", "MB": " MB", "min": " min", "Robux": " Robux"}.get(spec.unit, "")
    return _num(v) + unit


def fmt_change(spec: catalog.MetricSpec, value: float, usual: float, rel: float) -> str:
    if spec.rate:
        return f"{(value - usual) * 100:+.1f} pts"
    if rel >= 1.0:
        return f"{value / usual:.1f}x" if usual else "new"
    return f"{rel * 100:+.0f}%"


def _day(d: date) -> str:
    return f"{d:%b} {d.day}"


# --------------------------------------------------------------- finding ---

@dataclass
class Finding:
    sig: Signal
    key: str
    reach: float = 1.0
    score: float = 0.0
    impact: str = ""
    note: str = ""            # e.g. "whole game +40%" or "mostly on Phone"
    also: list[str] = field(default_factory=list)
    age: int = 0              # earlier days it was already shown
    reopened: bool = False
    severity: str = "watch"   # act | watch | good
    affected: float | None = None  # rough players per day, when it can be estimated
    corroborated: bool = False     # a device finding the whole-game numbers back up
    why: str = ""                  # for player declines: new vs returning players
    cause: causes.Cause | None = None
    fix_id: str = ""
    fix: str = ""                  # one thing to try first (problems only)
    learned: float = 1.0           # ranking factor learned from your clicks

    # Short names used by the report, the self-test and older code.
    metric = property(lambda self: self.sig.metric)
    split = property(lambda self: self.sig.split)
    good = property(lambda self: self.sig.good)
    label = property(lambda self: self.sig.spec.label)
    day = property(lambda self: self.sig.day.isoformat())

    def short(self) -> str:
        s = self.sig
        if s.kind == "budget":
            side = "under" if s.rel < 0 else "over"
            return f"{s.spec.label}{_where(s.split)} {side} the {fmt_value(s.spec, s.usual)} limit"
        return f"{s.spec.label}{_where(s.split)} {fmt_change(s.spec, s.value, s.usual, s.rel)}"

    def headline(self) -> str:
        """The finding without the GOOD/ISSUE tag, for people."""
        s, spec = self.sig, self.sig.spec
        where = _where(s.split)
        val, usual = fmt_value(spec, s.value), fmt_value(spec, s.usual)
        chg = fmt_change(spec, s.value, s.usual, s.rel)
        if s.kind == "trend":
            word = "climbing" if s.rel > 0 else "sliding"
            text = f"{spec.label}{where} {word} for {s.days} weeks: {val} vs {usual} ({chg})"
            if s.detail:
                text += f", {s.detail}"
        elif s.kind == "level":
            word = "up" if s.rel > 0 else "down"
            if s.detail:  # a clear start day
                text = (f"{spec.label}{where} {word} and staying {word}: {val} over the last 2 weeks vs {usual} "
                        f"before ({chg}), {s.detail}")
            else:
                text = (f"{spec.label}{where} {word} over the last 4 weeks: {val} vs {usual} the 4 weeks "
                        f"before ({chg})")
        elif s.kind == "budget":
            side = "below" if s.rel < 0 else "above"
            text = (f"{spec.label}{where} {val}, {side} the {usual} rule of thumb on {s.days} of the "
                    f"last 7 days")
        else:
            when = f"on {_day(s.day)}" if s.days == 1 else f"for {s.days} days since {_day(s.since)}"
            text = f"{spec.label}{where} {val} vs usual {usual} ({chg}) {when}"
        parts = [text]
        if self.note:
            parts.append(self.note)
        if self.why:
            parts.append(self.why)
        if self.cause:
            parts.append(self.cause.text)
        if self.impact:
            parts.append(self.impact[0].upper() + self.impact[1:])
        tail = f"Confidence {s.confidence}" + (", early estimate" if s.provisional else "")
        if self.reopened:
            tail += ", still there after you marked it fixed"
        elif self.age >= 1:
            tail += f", day {self.age + 1} on this list"
        parts.append(tail)
        if self.also:
            parts.append("Also: " + "; ".join(self.also[:2]))
        if self.fix:
            parts.append("Try: " + self.fix)
        return ". ".join(parts) + "."

    def line(self) -> str:
        tag = "GOOD" if self.good else "ISSUE"
        return f"{tag}: {self.headline()}"


@dataclass
class Checkup:
    universe_id: int
    status: str
    day: str | None
    top: list[Finding]
    checked: int
    notes: list[str]
    extra: str = ""          # e.g. the weekly check-up, when it's due

    def text(self) -> str:
        """Compact plain text. This is all an AI agent needs to see."""
        day = _day(date.fromisoformat(self.day)) if self.day else "-"
        lines = [f"Roblox game check-up, universe {self.universe_id}, data through {day}: {self.status}"]
        if self.top:
            lines += [f"{i}. {f.line()}" for i, f in enumerate(self.top, 1)]
        elif self.checked:
            lines.append(f"No unusual changes across {self.checked} metrics.")
        lines += self.notes
        if self.extra:
            lines += ["", self.extra]
        return "\n".join(lines)


# ------------------------------------------------------------- the check ---

def _series_keys(conn: sqlite3.Connection, uid: int, spec: catalog.MetricSpec) -> list[str]:
    keys = [""]
    for dims in spec.breakdowns:
        if dims:
            keys += [r[0] for r in conn.execute(
                "SELECT DISTINCT breakdown_key FROM data_points WHERE universe_id=? AND metric=?"
                " AND breakdown_key LIKE ? ORDER BY breakdown_key", (uid, spec.name, dims[0] + "=%"))]
    return keys


def _players(conn: sqlite3.Connection, uid: int) -> dict[str, float]:
    """Typical daily players over the last week, whole game and per split."""
    out = {}
    for key in _series_keys(conn, uid, catalog.get("DailyActiveUsers")):
        pts = detect.load_series(conn, uid, "DailyActiveUsers", key)[-7:]
        if pts:
            vals = sorted(p.value for p in pts)
            out[key] = vals[len(vals) // 2]
    return out


def _impact(sig: Signal, players: dict[str, float]) -> tuple[str, float | None]:
    total = players.get("", 0)
    dau = players.get(sig.split) or (total * DEFAULT_REACH if sig.split else total)
    new = players.get("IsNewUser=true") or total * 0.35
    d = sig.diff
    mf = "more" if d > 0 else "fewer"
    n = None
    if sig.metric == "DailyActiveUsers":
        n, text = abs(d), "{n} {mf} players a day"
    elif sig.metric == "Visits":
        n, text = abs(d), "{n} {mf} visits a day"
    elif sig.metric == "ClientCrashRate15m" and dau:
        n, text = abs(d) * dau, "roughly {n} {mf} players hitting crashes a day"
    elif sig.metric == "ForwardD1Retention" and new:
        share = (players.get(sig.split, 0) / total) if sig.split and total else 1.0
        n, text = abs(d) * new * share, "roughly {n} {mf} new players coming back the next day"
    elif sig.metric == "ForwardD7Retention" and new:
        n, text = abs(d) * new, "roughly {n} {mf} new players still playing a week later"
    elif sig.metric == "PayingUsersCVR" and dau:
        n, text = abs(d) * dau, "roughly {n} {mf} buyers a day"
    elif sig.metric == "DailyRevenue":
        n, text = abs(d), "{n} Robux a day " + ("more" if d > 0 else "less")
    if n is None:
        return "", None
    if sig.metric == "DailyRevenue":
        return (text.format(n=f"{n:,.0f}", mf=mf) if n >= 1 else ""), None
    return (text.format(n=f"{n:,.0f}", mf=mf) if n >= 1 else ""), n


def _key(sig: Signal) -> str:
    k = f"{sig.metric}|{sig.split}|{'up' if sig.rel > 0 else 'down'}"
    kind = "trend" if sig.kind in SLOW else sig.kind  # one slow story per metric, whichever check saw it
    return k + ("|" + kind if kind in ("trend", "budget") else "")


def _split_name(split: str) -> str:
    dim, _, val = split.partition("=")
    if dim == "IsNewUser":
        return "new players" if val == "true" else "returning players"
    return val


def _where(split: str) -> str:
    if not split:
        return ""
    if split.startswith("IsNewUser="):
        return " for " + _split_name(split)
    return " on " + _split_name(split)


def _merge_splits(sigs: list[Signal], players: dict[str, float],
                  series: dict[tuple[str, str], list[detect.Point]] | None = None) -> list[Finding]:
    """One finding per metric story: whole game, or the one device that explains it."""
    out: list[Finding] = []
    by_metric: dict[str, list[Signal]] = {}
    for s in sigs:
        by_metric.setdefault(s.metric, []).append(s)
    total = players.get("") or 0

    def reach(split: str) -> float:
        if not split or not total:
            return 1.0 if not split else DEFAULT_REACH
        return min(1.0, (players.get(split) or total * DEFAULT_REACH) / total)

    series_keys: dict[str, list[str]] = {}
    for m, key in series or {}:
        if key.startswith("Platform="):
            series_keys.setdefault(m, []).append(key)
    for metric, group in by_metric.items():
        series_keys.setdefault(metric, [])
        sudden = [s for s in group if s.kind in SUDDEN]
        whole = next((s for s in sudden if not s.split), None)
        splits = [s for s in sudden if s.split.startswith("Platform=")]
        if whole:
            # Which device does it come from? Measure every device over the same days.
            per = {}
            for (m, key), pts in (series or {}).items():
                if m == metric and key.startswith("Platform="):
                    sig = detect.sudden(whole.spec, pts, key, force_days=whole.days)
                    if sig and (sig.rel > 0) == (whole.rel > 0) and reach(key) >= 0.1:
                        per[key] = sig
            for s in splits:
                if (s.rel > 0) == (whole.rel > 0):
                    per[s.split] = s
            f = Finding(whole, _key(whole))
            if per:
                # Which device holds most of the extra (or missing) amount? Rates are
                # weighed by how many players each device has.
                def excess(s: Signal, metric: str = metric) -> float:
                    size = 1.0 if metric == "DailyActiveUsers" else (players.get(s.split) or 0)
                    return abs(s.value - s.usual) * size
                total_excess = sum(excess(s) for s in per.values()) or 1.0
                top_split = max(per.values(), key=excess)
                share = excess(top_split) / total_excess
                only_one = len(per) == 1 and len(series_keys[metric]) > 1  # the other devices didn't move
                if only_one or share >= 0.6:
                    if top_split.confidence != "low" and abs(top_split.rel) >= abs(whole.rel):
                        # The whole game backs it up, so it's at least as sure as the whole-game finding.
                        order = ("low", "medium", "high")
                        top_split.confidence = max(top_split.confidence, whole.confidence, key=order.index)
                        f = Finding(top_split, _key(top_split), corroborated=True)
                        f.note = f"Whole game {fmt_change(whole.spec, whole.value, whole.usual, whole.rel)}"
                    else:
                        f.note = (f"Mostly on {_split_name(top_split.split)} "
                                  f"({fmt_change(top_split.spec, top_split.value, top_split.usual, top_split.rel)})")
            out.append(f)
        else:
            # Only one device moved. Many device series are checked, so a lone
            # device needs stronger evidence than the whole game does.
            # Unless the whole game leans the same way over the same days.
            whole_pts = (series or {}).get((group[0].metric, ""))
            for s in splits + [s for s in sudden if s.split.startswith("IsNewUser=")]:
                lean = detect.sudden(s.spec, whole_pts, "", force_days=s.days) if whole_pts else None
                backed = lean is not None and (lean.rel > 0) == (s.rel > 0) and abs(lean.z) >= 3
                if not backed:
                    s.confidence = {"high": "medium", "medium": "low", "low": "low"}[s.confidence]
                f = Finding(s, _key(s), corroborated=backed)
                if backed:
                    f.note = f"Whole game {fmt_change(lean.spec, lean.value, lean.usual, lean.rel)}"
                out.append(f)
        slow = [s for s in group if s.kind in SLOW]
        if slow and not sudden:  # the 12-week slide and the 4-week comparison tell one story: keep the bigger
            top_slow = max(slow, key=lambda s: abs(s.rel))
            out.append(Finding(top_slow, _key(top_slow)))
        budgets = [s for s in group if s.kind == "budget"]
        if budgets and not sudden:  # only the worst one per metric, and only if nothing sudden says it already
            worst = max(budgets, key=lambda s: abs(s.rel))
            out.append(Finding(worst, _key(worst)))
    for f in out:
        f.reach = reach(f.split)
    return out


def _score(f: Finding) -> float:
    s = f.sig
    damp = REPEAT_DAMPING if f.age >= 3 and not f.reopened else 1.0
    return (s.spec.weight * min(abs(s.rel), 2.0) * math.sqrt(f.reach) * CONF_FACTOR[s.confidence]
            * KIND_FACTOR[s.kind] * damp)


def _severity(f: Finding, total_players: float) -> str:
    s = f.sig
    if s.good:
        return "good"
    if s.kind not in SUDDEN or s.spec.weight < 2 or f.reach < 0.1 or s.learning:
        return "watch"
    share = 0.005 if s.spec.weight >= 3 else 0.01
    if f.affected is not None and f.affected < max(5, share * total_players):
        return "watch"  # real but small in players: no need to drop everything
    if s.confidence == "high" and abs(s.rel) >= 0.25:
        return "act"
    if (s.confidence == "medium" and abs(s.rel) >= 0.5 and s.spec.weight >= 3
            and (not s.split or f.corroborated)):
        return "act"  # e.g. crashes doubled on an early estimate: don't wait a day
    return "watch"


def _group_families(findings: list[Finding]) -> list[Finding]:
    """Keep the strongest finding per family and direction; mention the rest under 'Also'."""
    best: dict[tuple[str, bool], Finding] = {}
    # The family's main number leads when scores are close ("players down", not "peak players down").
    for f in sorted(findings, key=lambda f: -f.score * (1.5 if f.metric in FAMILY_HEAD else 1.0)):
        k = (f.sig.spec.family, f.good)
        if k in best:
            best[k].also.append(f.short())
        else:
            best[k] = f
    return list(best.values())


def _merge_discovery(findings: list[Finding]) -> list[Finding]:
    """A players drop explained by discovery tells one story with the discovery numbers: show it once."""
    head = next((f for f in findings if f.cause and f.cause.kind == "discovery"), None)
    if not head:
        return findings
    rest = []
    for f in findings:
        if f is not head and not f.good and f.sig.spec.family == "discovery":
            head.also.insert(0, f.short())
        else:
            rest.append(f)
    return rest


def _journal(conn: sqlite3.Connection, uid: int) -> dict[str, sqlite3.Row]:
    return {r["key"]: r for r in conn.execute("SELECT * FROM findings WHERE universe_id=?", (uid,))}


def run(conn: sqlite3.Connection, universe_id: int, today: date | None = None, record: bool = True,
        weekly: bool | None = False) -> Checkup:
    """Run the check-up. ``record`` remembers what was shown (for 'day 3', mark done, weekly).

    ``weekly``: True = add the weekly check-up, None = add it on the first
    check-up of a new week, False = never.
    """
    today = today or datetime.now(timezone.utc).date()
    players = _players(conn, universe_id)
    dau = {p.day: p.value for p in detect.load_series(conn, universe_id, "DailyActiveUsers", "")}
    series: dict[tuple[str, str], list[detect.Point]] = {}
    sigs: list[Signal] = []
    checked = 0
    latest: date | None = None
    history_days = None
    tuning = checks.load(conn, universe_id, today)
    rank = learn.ranking(conn, universe_id, today)
    active = tuning.active()
    for spec in catalog.DEFAULT_METRICS:
        if spec.context:
            continue  # only used to explain other findings
        for key in _series_keys(conn, universe_id, spec):
            pts = detect.load_series(conn, universe_id, spec.name, key)
            if not pts:
                continue
            if key == "":
                checked += 1
                latest = max(latest or pts[-1].day, pts[-1].day)
                if spec.name == "DailyActiveUsers":
                    history_days = len(pts)
            series[(spec.name, key)] = pts
            ctx = checks.Context(spec, key, pts, exposure=dau if spec.count and not key else None,
                                 size=players.get(key) or (players.get("") if not key else None),
                                 strict=tuning.strict(spec.name, key), limits=tuning.limits)
            for chk in active:
                if chk.whole_game_only and key:
                    continue  # slow changes: whole game only (fewer false alarms)
                sig = chk.run(ctx)
                if sig:
                    sigs.append(sig)

    findings = _merge_splits(sigs, players, series)
    journal = _journal(conn, universe_id)
    data_day = latest.isoformat() if latest else None
    visible: list[Finding] = []
    for f in findings:
        row = journal.get(f.key)
        if row:
            until = row["state_until"]
            if row["state"] in ("snoozed", "wrong", "done") and until and data_day and until >= data_day:
                continue
            f.reopened = row["state"] == "done"
            f.age = row["days_shown"] - (1 if row["last_shown"] == data_day else 0)
        f.impact, f.affected = _impact(f.sig, players)
        prefer: tuple[str, ...] = ()
        if f.metric == "DailyActiveUsers" and not f.split and not f.good and f.sig.kind != "spike":
            from . import why as why_mod  # local import: why uses this module's helpers
            w = why_mod.explain(conn, universe_id)
            f.why = why_mod.short(conn, universe_id, w)
            prefer = {"new": ("thumbnail", "reach"), "return": ("content",)}.get(w.cause if w else "", ())
        f.cause = causes.find(conn, universe_id, f.sig, series)
        f.learned = rank.factor(f.metric, learn.kind_of(f.key))
        f.score = _score(f) * f.learned
        f.severity = _severity(f, players.get("", 0))
        if f.cause and f.cause.kind == "mix":
            if f.note.startswith("Mostly on"):
                f.note = ""  # the mix explanation replaces "which device"
            if f.severity == "act":
                f.severity = "watch"  # no device got worse: worth a look, not dropping everything
        if not f.good:
            kind = "sudden" if f.sig.kind in SUDDEN else "slow"
            f.fix_id, f.fix = fixes.suggest(conn, universe_id, f.metric, f.split, f.cause, kind, prefer)
        visible.append(f)

    # "Low" confidence means it may well be noise: counted, not shown (unless the
    # game is new and only very big changes can be judged at all).
    # Tiny problems (under MIN_AFFECTED players a day) are counted, not shown.
    shown_ok = [f for f in visible if (f.sig.confidence != "low" or f.sig.learning)
                and (f.good or f.affected is None or f.affected >= MIN_AFFECTED)]
    unsure = len(visible) - len(shown_ok)
    grouped = _merge_discovery(_group_families(shown_ok))
    issues = sorted((f for f in grouped if not f.good), key=lambda f: -f.score)
    wins = sorted((f for f in grouped if f.good), key=lambda f: -f.score)
    top = (issues + wins)[:3]

    notes: list[str] = []
    if not checked:
        status = "No data"
        notes.append("No data yet: run 'roblox-agent pull'.")
    elif any(f.severity == "act" for f in issues):
        status = "Act now"
    elif issues:
        status = "Watch"
    else:
        status = "All good"
    if latest and (today - latest).days > 3:
        notes.append(f"Data is {(today - latest).days} days old: run 'roblox-agent pull'.")
    if history_days is not None and history_days < LEARNING_DAYS:
        notes.append(f"Still learning your game's normal ({history_days} days of history). "
                     "Until then only very big changes are flagged.")
    hidden = len(grouped) - len(top) + unsure
    if hidden > 0:
        notes.append(f"{hidden} smaller or uncertain change(s) not shown.")

    from . import changes  # local import: changes uses this module's helpers
    notes += changes.due_notes(conn, universe_id, latest, record=record)
    notes += fixes.due_notes(conn, universe_id, latest, record=record)

    result = Checkup(universe_id, status, data_day, top, checked, notes)
    if weekly is None and record and latest:
        weekly = _new_week(conn, universe_id, latest)
    if record and data_day:
        _remember(conn, universe_id, data_day, result, visible)
    if weekly and latest:
        from . import weekly as weekly_mod
        result.extra = weekly_mod.build(conn, universe_id, latest).text()
        if record:  # keep the stored text complete
            conn.execute("UPDATE checkups SET text=? WHERE universe_id=? AND data_day=?",
                         (result.text(), universe_id, data_day))
            conn.commit()
    return result


def _new_week(conn: sqlite3.Connection, uid: int, latest: date) -> bool:
    """True on the first check-up of a new week (and not the very first check-up ever)."""
    days = [date.fromisoformat(r["data_day"]) for r in
            conn.execute("SELECT data_day FROM checkups WHERE universe_id=?", (uid,))]
    if not days or latest in days:
        return False
    week = latest.isocalendar()[:2]
    return not any(d.isocalendar()[:2] == week for d in days)


def _remember(conn: sqlite3.Connection, uid: int, data_day: str, result: Checkup,
              visible: list[Finding]) -> None:
    """Save what was found and shown today. Safe to call many times a day."""
    now = db.utcnow()
    shown = {f.key for f in result.top}
    old = _journal(conn, uid)
    for f in visible:
        row = old.get(f.key)
        is_shown = f.key in shown
        if row is None or row["last_day"] < (date.fromisoformat(data_day) - timedelta(days=3)).isoformat():
            # New, or back after a break: a fresh episode.
            first, days_shown, last_shown = f.sig.since.isoformat(), 0, None
            state, until = (row["state"], row["state_until"]) if row and row["state"] == "wrong" else ("open", None)
        else:
            first, days_shown, last_shown = row["first_day"], row["days_shown"], row["last_shown"]
            state, until = row["state"], row["state_until"]
        if is_shown and last_shown != data_day:
            days_shown += 1
            last_shown = data_day
        if is_shown and state != "open" and (until or "") < data_day:
            state, until = "open", None  # a snooze or 'fixed' grace period ran out and it's still there
        peak = f.sig.rel if row is None or abs(f.sig.rel) > abs(row["peak_rel"] or 0) else row["peak_rel"]
        conn.execute(
            "INSERT OR REPLACE INTO findings (universe_id, key, metric, split, good, first_day, last_day,"
            " days_shown, last_shown, state, state_until, peak_rel, last_line, updated_at, fix)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, f.key, f.metric, f.split, int(f.good), first, max(data_day, row["last_day"] if row else ""),
             days_shown, last_shown, state, until, peak, f.headline(), now, f.fix_id or None))
    conn.execute(
        "INSERT OR REPLACE INTO checkups (universe_id, data_day, status, top_keys, text, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (uid, data_day, result.status, json.dumps([f.key for f in result.top]), result.text(), now))
    conn.commit()


# -------------------------------------------------------------- feedback ---

ACTIONS = {"done": DONE_GRACE_DAYS, "snooze": SNOOZE_DAYS, "wrong": WRONG_DAYS, "open": 0}


def mark(conn: sqlite3.Connection, uid: int, item: int | str, action: str) -> str:
    """Mark item N of the latest check-up: done (fixed), snooze (hide 7 days), wrong (false alarm), open."""
    if action not in ACTIONS:
        return f"Unknown action '{action}'. Use: done, snooze, wrong or open."
    row = conn.execute("SELECT * FROM checkups WHERE universe_id=? ORDER BY data_day DESC LIMIT 1",
                       (uid,)).fetchone()
    if not row:
        return "No check-up yet. Run one first."
    keys = json.loads(row["top_keys"])
    try:
        n = int(item)
        if n < 1:
            raise IndexError
        key = keys[n - 1]
    except (ValueError, IndexError):
        return f"Pick an item number from 1 to {len(keys)}." if keys else "The latest check-up had no items."
    day = date.fromisoformat(row["data_day"])
    until = (day + timedelta(days=ACTIONS[action])).isoformat() if action != "open" else None
    state = {"snooze": "snoozed"}.get(action, action)
    # Same metric and device, any kind (sudden change, slide, limit): it's one problem to the user.
    prefix = "|".join(key.split("|")[:2]) + "|"
    conn.execute("UPDATE findings SET state=?, state_until=?, updated_at=? WHERE universe_id=?"
                 " AND substr(key, 1, ?) = ?", (state, until, db.utcnow(), uid, len(prefix), prefix))
    metric, split = key.split("|")[:2]
    if action in ("done", "wrong", "snooze"):  # remembered for good: this is how the checks adapt to your game
        learn.record(conn, uid, key, action, row["data_day"])
    if action == "done":
        fixes.start(conn, uid, key, row["data_day"])
    conn.commit()
    name = catalog.get(metric).label + _where(split)
    wait = fixes.wait_days(metric, key)
    return {"done": f"Marked '{name}' as fixed. I'll check in about {wait} days that it really is.",
            "snooze": f"Snoozed '{name}' for {SNOOZE_DAYS} days.",
            "wrong": f"Noted that '{name}' was a false alarm. Hidden for {WRONG_DAYS} days, and it will need "
                     "stronger evidence from now on.",
            "open": f"'{name}' is back on the list."}[action]
