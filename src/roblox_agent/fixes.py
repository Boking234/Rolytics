"""Suggest a likely fix, then check whether it worked.

Each problem on the check-up gets one short "Try:" line: the first thing
most Roblox developers would check for that symptom, on that device, given
the likely cause (see ``causes``). It is a starting point, not a diagnosis;
your AI agent can turn it into concrete steps for your game.

When you mark an item **Fixed**, the tool remembers what the problem looked
like (the normal level before it, and the level while it lasted) and checks
again a few days later:

* back to normal (at most a third of the problem left): "the fix worked",
* most of it still there: "the fix didn't work yet", the item comes back, and
  the next suggestion for that symptom is shown instead,
* in between: "partly".

The result is also a label for learning (see ``learn``): a fix that worked or
failed proves the problem was real. Suggestions that worked in your game
before are shown first next time; ones that failed are skipped.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from . import catalog, db, detect

MOBILE = ("Phone", "Tablet")
WORKED = 0.35        # at most this share of the problem left: fixed
FAILED = 0.70        # at least this share left: not fixed
GIVE_UP_DAYS = 21    # no data this long after the check day: result unknown
BEFORE_DAYS = 28


@dataclass(frozen=True)
class Fix:
    id: str
    metrics: tuple[str, ...]
    text: str
    devices: tuple[str, ...] = ()   # () = any device / whole game
    when: str = "any"               # sudden | slow | any: what kind of problem it fits


PLAYBOOK: tuple[Fix, ...] = (
    # crashes
    Fix("crash-lowend", ("ClientCrashRate15m",), "test on a low-end phone and look for big meshes, textures or "
        "sounds added lately (phones run out of memory first)", MOBILE),
    Fix("crash-errors", ("ClientCrashRate15m",), "open the Developer Console (F9) on a live server and look for "
        "errors that started with your last update", when="sudden"),
    Fix("crash-memory", ("ClientCrashRate15m", "ClientMemoryUsageP90", "OomUnexpectedExits"),
        "check memory over a 30-minute session for a leak: parts, sounds or connections never cleaned up"),
    # speed
    Fix("fps-mobile", ("ClientFpsP10",), "play the newest area on a phone at graphics level 1; cut part count, "
        "shadows and particle effects there", MOBILE),
    Fix("fps-profiler", ("ClientFpsP10",), "use the MicroProfiler (Ctrl+F6) in the busiest area to find the slow "
        "script or effect"),
    Fix("mem-assets", ("ClientMemoryUsageP90", "OomUnexpectedExits"), "check which new assets are largest "
        "(Developer Console > Memory) and stream or shrink them"),
    Fix("server-scripts", ("ServerCrashCount", "ServerFrameRateAvg"), "check server memory and heavy server "
        "scripts (loops, physics) in the Developer Console's server tab"),
    # players
    Fix("join-test", ("DailyActiveUsers", "Visits", "PeakConcurrentPlayers"), "do a fresh join on a phone and a "
        "PC: make sure the game loads and the first minute works", when="sudden"),
    Fix("thumbnail", ("DailyActiveUsers", "Visits", "EndToEndCVR"), "test a new icon or thumbnail (Roblox has "
        "built-in thumbnail tests) and check the title still says what the game is"),
    Fix("reach", ("DailyActiveUsers", "Visits", "UniqueUsersWithImpressions"), "give Roblox a reason to show the "
        "game again: an update or event, and consider a small sponsored ad test", when="slow"),
    Fix("content", ("DailyActiveUsers", "AverageSessionLengthMinutes", "ForwardD7Retention"), "add something new "
        "to return for (an event, a goal, a weekly reward); regulars may have run out of things to do", when="slow"),
    # first session and retention
    Fix("first-5-min", ("ForwardD1Retention", "AverageSessionLengthMinutes"), "play the first 5 minutes as a new "
        "player: is the tutorial or first goal broken, slower or more confusing than before?"),
    Fix("come-back", ("ForwardD1Retention", "ForwardD7Retention"), "give new players a clear reason to come back "
        "tomorrow (a daily reward, an unfinished goal)"),
    Fix("early-exit", ("AverageSessionLengthMinutes",), "look for the point where players leave: a hard level, a "
        "long wait, or a system that stopped working"),
    # money
    Fix("shop-test", ("DailyRevenue", "PayingUsersCVR"), "buy every product yourself on a phone and a PC: a "
        "broken shop button or purchase prompt is the most common cause", when="sudden"),
    Fix("prices", ("DailyRevenue", "PayingUsersCVR"), "check what changed in the shop: prices, item order, "
        "or a free way to get what used to be paid"),
)
BY_ID = {f.id: f for f in PLAYBOOK}
CAUSE_FIX = {
    "change": "roll back or switch off change #{id} for a day; if the number recovers, the change is the cause",
    "mix": "make the game lighter on {device} (now a bigger share of players) instead of hunting a new bug",
    "traffic": "make the first 5 minutes clearer for newcomers; check Acquisition on the Creator Dashboard to "
               "see where they came from",
    "discovery": "",  # covered by the metric's own list (thumbnail, reach)
}


def _history(conn: sqlite3.Connection, uid: int, metric: str, split: str) -> tuple[set[str], set[str]]:
    """(fixes that worked, fixes that failed) for this symptom in this game, last 6 months."""
    worked, failed = set(), set()
    for r in conn.execute("SELECT fix, result FROM fix_tries WHERE universe_id=? AND metric=? AND split=?"
                          " AND result IS NOT NULL AND created_at >= date('now', '-180 days')",
                          (uid, metric, split)):
        (worked if r["result"] == "worked" else failed if r["result"] == "failed" else set()).add(r["fix"])
    return worked, failed


def suggest(conn: sqlite3.Connection, uid: int, metric: str, split: str, cause=None, kind: str = "sudden",
            prefer: tuple[str, ...] = ()) -> tuple[str, str]:
    """(fix id, short text) for a problem, or ('', '') if there's nothing useful to say.

    ``kind``: 'sudden' or 'slow' (slides, drops that stayed, limits). ``prefer``:
    fix ids that fit what else is known (e.g. players coming back less -> content).
    """
    worked, failed = _history(conn, uid, metric, split)
    if cause is not None and CAUSE_FIX.get(cause.kind):
        fid = f"cause-{cause.kind}"
        if fid not in failed:
            text = CAUSE_FIX[cause.kind].format(id=cause.change_id, device=_plural(cause.device))
            return fid, text
    device = split.split("=", 1)[1] if split.startswith("Platform=") else ""
    options = [f for f in PLAYBOOK if metric in f.metrics and (not f.devices or device in f.devices)
               and f.when in ("any", kind)]
    # Proven in this game first, then what the other numbers point to, then device-specific ones.
    options.sort(key=lambda f: (f.id not in worked, f.id not in prefer, not (device and f.devices)))
    for f in options:
        if f.id not in failed:
            return f.id, f.text + (" (this worked for you before)" if f.id in worked else "")
    return "", ""


def _plural(device: str) -> str:
    return {"Phone": "phones", "Tablet": "tablets", "Computer": "computers", "Console": "consoles"}.get(
        device, device or "that device")


# ------------------------------------------------------------- fix check ---

def wait_days(metric: str, key: str) -> int:
    if metric == "ForwardD7Retention":
        return 10    # a week's cohort has to finish its week first
    if metric == "ForwardD1Retention":
        return 4
    if key.endswith("|trend"):
        return 14    # slow problems recover slowly
    return 3


def start(conn: sqlite3.Connection, uid: int, key: str, data_day: str) -> None:
    """Remember what the problem looked like when it was marked fixed. Call commit yourself."""
    row = conn.execute("SELECT * FROM findings WHERE universe_id=? AND key=?", (uid, key)).fetchone()
    if not row or row["good"]:
        return
    metric, split = row["metric"], row["split"]
    pts = detect.load_series(conn, uid, metric, split)
    first, marked = date.fromisoformat(row["first_day"]), date.fromisoformat(data_day)
    before = [p.value for p in pts if first - timedelta(days=BEFORE_DAYS) <= p.day < first]
    during = [p.value for p in pts if first <= p.day <= marked]
    if len(before) < 7 or not during:
        return
    usual, bad = median(before), sum(during) / len(during)
    if bad == usual:
        return
    check_on = marked + timedelta(days=wait_days(metric, key))
    conn.execute("INSERT INTO fix_tries (universe_id, key, metric, split, fix, marked_day, check_on, usual, bad,"
                 " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (uid, key, metric, split, row["fix"] or "", data_day, check_on.isoformat(), usual, bad,
                  db.utcnow()))


def _verdict(usual: float, bad: float, after: float) -> str:
    left = (after - usual) / (bad - usual)
    if left <= WORKED:
        return "worked"
    if left >= FAILED:
        return "failed"
    return "partly"


def due_notes(conn: sqlite3.Connection, uid: int, latest: date | None, record: bool = True) -> list[str]:
    """Check fixes whose wait is over, once. One short line each."""
    if not latest:
        return []
    from . import learn
    from .checkup import _where, fmt_value
    notes = []
    rows = conn.execute("SELECT * FROM fix_tries WHERE universe_id=? AND announced=0 AND check_on<=? ORDER BY id",
                        (uid, latest.isoformat())).fetchall()
    for r in rows:
        spec = catalog.get(r["metric"])
        marked = date.fromisoformat(r["marked_day"])
        pts = [p for p in detect.load_series(conn, uid, r["metric"], r["split"]) if p.day > marked]
        name = spec.label + _where(r["split"])
        if len(pts) < 2:
            if (latest - date.fromisoformat(r["check_on"])).days < GIVE_UP_DAYS:
                continue  # not enough new data yet: try again tomorrow
            result, after = "unknown", None
        else:
            recent = pts[-3:]
            after = sum(p.value for p in recent) / len(recent)
            result = _verdict(r["usual"], r["bad"], after)
        if result == "unknown":
            line = f"Fix check: {name}: no new data to tell whether the fix worked."
        else:
            now, usual, was = (fmt_value(spec, v) for v in (after, r["usual"], r["bad"]))
            line = {"worked": f"Fix check: {name} is back to {now} (normal {usual}, was {was}): the fix worked.",
                    "partly": f"Fix check: {name} is {now} (normal {usual}, was {was}): partly fixed.",
                    "failed": f"Fix check: {name} is still {now} (normal {usual}, was {was}): the fix didn't "
                              "work yet, so it's back on the list."}[result]
        notes.append(line)
        if record:
            conn.execute("UPDATE fix_tries SET after=?, result=?, announced=1 WHERE id=?", (after, result, r["id"]))
            if result in ("worked", "failed"):
                learn.record(conn, uid, r["key"], result, latest.isoformat())
            if result == "failed":  # show it again now, not when the grace period happens to end
                prefix = "|".join(r["key"].split("|")[:2]) + "|"
                conn.execute("UPDATE findings SET state='open', state_until=NULL WHERE universe_id=? AND"
                             " substr(key, 1, ?) = ? AND state='done'", (uid, len(prefix), prefix))
    if record and rows:
        conn.commit()
    return notes


def summary(conn: sqlite3.Connection, uid: int) -> list[str]:
    """Which suggestions worked in this game, for 'roblox-agent learned'."""
    rows = conn.execute("SELECT fix, result, COUNT(*) AS n FROM fix_tries WHERE universe_id=? AND result IS NOT NULL"
                        " GROUP BY fix, result", (uid,)).fetchall()
    if not rows:
        return ["No fixes checked yet. Mark an item Fixed and it checks a few days later whether it worked."]
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["result"]] = tally.get(r["result"], 0) + r["n"]
    out = [(f"Fixes checked: {tally.get('worked', 0)} worked, {tally.get('partly', 0)} partly, "
            f"{tally.get('failed', 0)} didn't work.")]
    good = sorted({r["fix"] for r in rows if r["result"] == "worked" and r["fix"] in BY_ID})
    if good:
        out.append("Suggestions that worked in your game: " + "; ".join(BY_ID[g].text.split(":")[0].split(",")[0] for g in good)
                   + ".")
    return out
