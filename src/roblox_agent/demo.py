"""A pretend Roblox server that makes up realistic-looking numbers.

Used by ``roblox-agent demo`` (so you can see how everything works before you
have an API key) and by the automated tests. It never touches the internet.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta
from typing import Any

from .api import Response

BASES = {
    "DailyActiveUsers": 1200, "Visits": 3100, "AverageSessionLengthMinutes": 14.0,
    "ForwardD1Retention": 0.21, "ForwardD7Retention": 0.06, "DailyRevenue": 5400,
    "PayingUsersCVR": 0.018, "ClientCrashRate15m": 0.012, "ClientFpsP10": 31.0,
    "ClientMemoryUsageP90": 1450.0, "OomUnexpectedExits": 9, "PeakConcurrentPlayers": 140,
    "ServerFrameRateAvg": 58.5, "ServerCrashCount": 1,
}
SPLITS = {"Platform": {"Computer": 0.45, "Phone": 0.4, "Tablet": 0.1, "Console": 0.05},
          "IsNewUser": {"true": 0.35, "false": 0.65}}


def _noise(*parts: Any) -> float:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return h[0] / 255 - 0.5  # -0.5 .. 0.5, same every time for the same inputs


def _value(metric: str, day: datetime, split: str | None) -> float:
    base = BASES.get(metric, 100)
    weekend = 1.15 if day.weekday() >= 5 else 1.0
    trend = 1 + 0.1 * math.sin(day.toordinal() / 20)
    v = base * weekend * trend * (1 + 0.2 * _noise(metric, day.date(), split))
    if split is not None and base > 50 and metric not in ("ClientFpsP10", "ServerFrameRateAvg"):
        dim, val = split.split("=")
        v *= SPLITS.get(dim, {}).get(val, 0.1)
    return round(v, 4)


class FakeRoblox:
    """Behaves like the Analytics Query API, including slow (202) queries."""

    def __init__(self, today: datetime | None = None, slow_metrics: set[str] | None = None,
                 reject_metrics: set[str] | None = None, budget_limit_days: int | None = None,
                 valid_key: str = "demo-key", incident: bool = False):
        self.today = today
        self.slow = slow_metrics or set()
        self.reject = reject_metrics or set()
        self.budget_limit_days = budget_limit_days
        self.valid_key = valid_key
        self.incident = incident  # demo: phones start crashing much more on the latest day
        self.pending: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str],
                 body: dict[str, Any] | None) -> Response:
        self.calls.append((method, url, body))
        if headers.get("x-api-key") != self.valid_key:
            return Response(401, {})
        if method == "GET":
            op_id = url.rsplit("/", 1)[-1]
            if op_id not in self.pending:
                return Response(404, {})
            return Response(200, self.pending.pop(op_id))
        m = re.search(r"/universes/(\d+)/metrics$", url)
        if not m or body is None:
            return Response(404, {})
        uid = m.group(1)
        metric = body["metric"]
        if metric in self.reject:
            return Response(400, {"done": True, "error": {"code": 2001, "message": "Unsupported metric."}})
        start = datetime.fromisoformat(body["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(body["endTime"].replace("Z", "+00:00"))
        days = (end - start).days
        if self.budget_limit_days and days > self.budget_limit_days:
            return Response(429, {"done": True, "error": {"code": 3000, "message": "Data point budget exceeded."}})
        op = self._result(uid, body, start, days)
        if metric in self.slow:
            op_id = f"op{len(self.calls)}"
            path = f"v1/universes/{uid}/operations/metrics/{op_id}"
            op["path"] = path
            self.pending[op_id] = op
            return Response(202, {"path": path, "done": False, "metadata": {"createdTime": "x"}})
        return Response(200, op)

    def _result(self, uid: str, body: dict[str, Any], start: datetime, days: int) -> dict[str, Any]:
        metric = body["metric"]
        dims = body.get("breakdown") or []
        splits: list[str | None] = [None]
        if dims:
            splits = [f"{dims[0]}={v}" for v in SPLITS.get(dims[0], {"A": 1})]
        values = []
        for split in splits:
            points = []
            for i in range(days):
                day = start + timedelta(days=i)
                value = _value(metric, day, split)
                if (self.incident and i == days - 1 and metric == "ClientCrashRate15m"
                        and split in (None, "Platform=Phone")):
                    value = round(value * (3.0 if split else 1.8), 4)
                dp: dict[str, Any] = {"time": day.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": value}
                if i >= days - 2:
                    dp["status"] = "Projected"
                elif split and split.endswith("Console"):
                    dp["status"] = "NotStatisticallySignificant"
                points.append(dp)
            bd = [] if split is None else [{"dimension": split.split("=")[0], "value": split.split("=")[1]}]
            values.append({"breakdowns": bd, "dataPoints": points})
        return {"path": f"v1/universes/{uid}/operations/metrics/done", "done": True,
                "metadata": {"createdTime": "x"}, "response": {"values": values}}
