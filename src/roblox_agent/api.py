"""Client for the Roblox Analytics Query API.

Docs: https://create.roblox.com/docs/cloud/guides/analytics

Uses only the Python standard library, so there is nothing extra to install.
The API key is sent in the ``x-api-key`` header and is never printed, logged,
or included in any error message.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://apis.roblox.com/analytics-query-api/"
USER_AGENT = "roblox-analytics-agent (open source; local tool)"

# Errors worth retrying after a pause (server hiccups).
RETRYABLE_STATUS = {500, 502, 503, 504}


class ApiError(Exception):
    """A problem talking to Roblox. Messages are safe to show (no key)."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class AuthError(ApiError):
    """The key is wrong, revoked, or lacks universe.analytics:read for this game."""


class BudgetError(ApiError):
    """Roblox said the query asked for too many data points (429 / code 3000)."""


class BadRequestError(ApiError):
    """Roblox rejected the query (400 / code 2001), e.g. unsupported granularity."""


@dataclass
class Response:
    status: int
    body: dict[str, Any]


# A transport takes (method, url, headers, json_body) and returns a Response.
# Tests swap in a fake transport; real runs use urllib.
Transport = Callable[[str, str, dict[str, str], "dict[str, Any] | None"], Response]


def urllib_transport(method: str, url: str, headers: dict[str, str],
                     body: dict[str, Any] | None, timeout: float = 60.0) -> Response:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return Response(resp.status, _parse_json(raw))
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return Response(e.code, _parse_json(raw))
    except urllib.error.URLError as e:
        raise ApiError(f"Could not reach Roblox ({e.reason}). Check your internet connection.") from None


def _parse_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_text": raw[:500].decode(errors="replace")}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


@dataclass
class AnalyticsClient:
    api_key: str = field(repr=False)
    universe_id: int
    transport: Transport = urllib_transport
    max_retries: int = 4
    poll_interval: float = 2.0
    max_poll_seconds: float = 600.0
    sleep: Callable[[float], None] = time.sleep

    def __repr__(self) -> str:  # never show the key
        return f"AnalyticsClient(universe_id={self.universe_id}, api_key=***)"

    # ---- public -------------------------------------------------------
    def query_metric(self, metric: str, granularity: str, start: str, end: str,
                     breakdown: list[str] | None = None,
                     filters: list[dict[str, Any]] | None = None,
                     limit: int | None = None) -> dict[str, Any]:
        """Run one metrics query and return the finished operation body.

        Waits for long-running (202) operations by polling.
        """
        body: dict[str, Any] = {
            "metric": metric,
            "granularity": granularity,
            "startTime": start,
            "endTime": end,
        }
        if breakdown:
            body["breakdown"] = list(breakdown)
        if filters:
            body["filter"] = filters
        if limit:
            body["limit"] = limit
        url = f"{BASE_URL}v1/universes/{self.universe_id}/metrics"
        op = self._request("POST", url, body)
        return self._wait(op)

    # ---- internals ----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _request(self, method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            resp = self.transport(method, url, self._headers(), body)
            if resp.status in (200, 202):
                return resp.body
            if resp.status in RETRYABLE_STATUS and attempt < self.max_retries:
                self.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if resp.status == 429 and _error_code(resp.body) != 3000 and attempt < self.max_retries:
                # Plain rate limiting: slow down and retry.
                self.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise _to_error(resp)
        raise ApiError("Roblox kept failing; try again later.")  # pragma: no cover

    def _wait(self, op: dict[str, Any]) -> dict[str, Any]:
        waited = 0.0
        while not op.get("done", False):
            path = op.get("path")
            if not path:
                raise ApiError("Roblox returned an unfinished query with no path to check.")
            if waited >= self.max_poll_seconds:
                raise ApiError("Roblox took too long to answer this query; try a shorter date range.")
            self.sleep(self.poll_interval)
            waited += self.poll_interval
            op = self._request("GET", BASE_URL + path.lstrip("/"), None)
        if "error" in op:
            err = op["error"] or {}
            code = err.get("code")
            msg = err.get("message", "unknown error")
            if code == 3000:
                raise BudgetError(f"Query too big: {msg}", code=code)
            raise BadRequestError(f"Roblox rejected the query: {msg}", code=code)
        return op


def _error_code(body: dict[str, Any]) -> int | None:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("code")
    return body.get("code") if isinstance(body, dict) else None


def _error_message(body: dict[str, Any]) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    for key in ("message", "_text"):
        if isinstance(body, dict) and body.get(key):
            return str(body[key])
    return ""


def _to_error(resp: Response) -> ApiError:
    code = _error_code(resp.body)
    msg = _error_message(resp.body)
    if resp.status in (401, 403):
        return AuthError(
            "Roblox did not accept the API key. Check that it is copied fully, is not expired, "
            "has this game under Access Permissions, and has the universe.analytics:read permission.",
            status=resp.status)
    if resp.status == 404:
        return ApiError("Roblox could not find that game or query. Check the Universe ID.", status=404)
    if resp.status == 429 and code == 3000:
        return BudgetError(f"Query too big: {msg}", status=429, code=code)
    if resp.status == 400:
        return BadRequestError(f"Roblox rejected the query: {msg}", status=400, code=code)
    return ApiError(f"Roblox error {resp.status}: {msg}".strip(), status=resp.status, code=code)
