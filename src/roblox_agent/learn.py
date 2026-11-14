"""Learning from your clicks: which kinds of items are worth your time.

Every click on a check-up item is a free label:

* **Fixed** (``done``): it was real and worth acting on.
* **Fix worked / didn't work**: a few days after "Fixed", the numbers show
  whether it recovered. Either way the problem was real.
* **Snooze**: real, but not urgent for you right now.
* **Not a problem** (``wrong``): a false alarm.

Two things adapt from these labels, both on your computer only:

1. **The evidence bar** (``checks.load``): a metric you often mark as a
   false alarm needs stronger evidence before it is flagged at all.
2. **The ranking** (this module): items like the ones you act on move up the
   top 3, items like the ones you dismiss move down. It learns per metric
   (e.g. "crash rate") and per kind of check (sudden change, slow slide,
   rule-of-thumb limit), because people often care about one and not the
   other ("I never act on slow slides").

Safety rules: the ranking never hides an item, never changes the status
("Act now" stays "Act now"), and starts neutral. Each label moves it only a
little, so one stray click can't bury a real problem. Old labels (over
180 days) stop counting, because games and priorities change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import catalog

DAYS = 180
PRIOR = 2.0                    # pretend 2 "acted" + 2 "dismissed" to start neutral
LOW, HIGH = 0.6, 1.4           # one level (metric or kind) moves the score at most this far
CLAMP = (0.5, 1.5)             # both levels together
REAL = {"done": 1.0, "worked": 1.0, "failed": 1.0}
NOT = {"wrong": 1.0, "snooze": 0.5}
KIND_NAMES = {"sudden": "sudden changes", "trend": "slow slides", "budget": "rule-of-thumb limits"}


def kind_of(key: str) -> str:
    """The kind of check behind a finding key ('metric|split|dir[|trend|budget]')."""
    parts = key.split("|")
    return parts[3] if len(parts) > 3 and parts[3] in ("trend", "budget") else "sudden"


def _factor(real: float, dismissed: float) -> float:
    u = (real + PRIOR) / (real + dismissed + 2 * PRIOR)   # 0.5 when nothing is known
    return LOW + (HIGH - LOW) * u


@dataclass
class Ranking:
    by_metric: dict[str, tuple[float, float]] = field(default_factory=dict)  # metric -> (real, dismissed)
    by_kind: dict[str, tuple[float, float]] = field(default_factory=dict)

    def factor(self, metric: str, kind: str) -> float:
        f = 1.0
        if metric in self.by_metric:
            f *= _factor(*self.by_metric[metric])
        if kind in self.by_kind:
            f *= _factor(*self.by_kind[kind])
        return max(CLAMP[0], min(CLAMP[1], f))

    def lines(self) -> list[str]:
        """What it learned, in plain words (only things that moved the ranking)."""
        up, down = [], []
        for m, (r, d) in sorted(self.by_metric.items()):
            f = _factor(r, d)
            name = catalog.get(m).label
            if f >= 1.1:
                up.append(name)
            elif f <= 0.9:
                down.append(name)
        for k, (r, d) in sorted(self.by_kind.items()):
            f = _factor(r, d)
            if f >= 1.1:
                up.append(KIND_NAMES.get(k, k))
            elif f <= 0.9:
                down.append(KIND_NAMES.get(k, k))
        out = []
        if up:
            out.append("Ranked higher because you usually act on them: " + ", ".join(up) + ".")
        if down:
            out.append("Ranked lower because you often dismiss or snooze them: " + ", ".join(down) + ".")
        return out


def ranking(conn: sqlite3.Connection, uid: int, today: date | None = None) -> Ranking:
    since = ((today or datetime.now(timezone.utc).date()) - timedelta(days=DAYS)).isoformat()
    rk = Ranking()
    metric_tally: dict[str, list[float]] = {}
    kind_tally: dict[str, list[float]] = {}
    for r in conn.execute("SELECT metric, kind, action, COUNT(*) AS n FROM feedback WHERE universe_id=? AND day>=?"
                          " GROUP BY metric, kind, action", (uid, since)):
        real, dismissed = REAL.get(r["action"], 0.0) * r["n"], NOT.get(r["action"], 0.0) * r["n"]
        for tally, k in ((metric_tally, r["metric"]), (kind_tally, r["kind"])):
            if not k:
                continue  # labels from before v0.5 don't know their kind
            t = tally.setdefault(k, [0.0, 0.0])
            t[0] += real
            t[1] += dismissed
    rk.by_metric = {k: (v[0], v[1]) for k, v in metric_tally.items()}
    rk.by_kind = {k: (v[0], v[1]) for k, v in kind_tally.items()}
    return rk


def report(conn: sqlite3.Connection, uid: int) -> str:
    """Everything it has learned about this game, in plain words (CLI 'learned' and the app's Settings)."""
    from . import changes, checks, fixes
    out = ["From your clicks (Fixed, Snooze, Not a problem):", checks.describe(conn, uid), "",
           "From the changes you shipped:", changes.track_record(conn, uid)]
    out += [f"- {line}" for line in changes.lessons(conn, uid)]
    out += ["", "From fixes you marked done:", *fixes.summary(conn, uid)]
    return "\n".join(out)


def record(conn: sqlite3.Connection, uid: int, key: str, action: str, day: str) -> None:
    """Store one label. Call commit yourself."""
    from . import db
    metric, split = key.split("|")[:2]
    conn.execute("INSERT INTO feedback (universe_id, metric, split, action, day, created_at, kind)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)", (uid, metric, split, action, day, db.utcnow(), kind_of(key)))
