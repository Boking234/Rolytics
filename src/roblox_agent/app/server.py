"""The app's local web server: serves the page and answers its requests.

Safety:
* Listens on 127.0.0.1 only, so other computers can't reach it.
* Every /api/ request must carry the random token made at launch (other
  websites open in your browser can't read it), and the Host header must be
  127.0.0.1 or localhost (blocks "DNS rebinding" tricks).
* Request bodies are never logged.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from .. import checkup, config, db, report
from .service import Service

MAX_BODY = 64 * 1024
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".md": "text/plain; charset=utf-8",
         ".png": "image/png", ".ico": "image/x-icon"}


def _web(name: str) -> bytes:
    return resources.files("roblox_agent.app").joinpath("web", name).read_bytes()


def _web_names() -> set[str]:
    return {p.name for p in resources.files("roblox_agent.app").joinpath("web").iterdir() if p.is_file()}


class App:
    def __init__(self, service: Service, token: str | None = None):
        self.service = service
        self.token = token or secrets.token_urlsafe(24)
        self.port = 0
        self.on_show = lambda: None  # set by the window layer (second launch brings the window forward)

    # Each route: (method, path) -> function(body) -> json-able
    def route(self, method: str, path: str, body: dict) -> Any:
        s = self.service
        b = body
        routes = {
            ("GET", "/api/state"): lambda: s.state(),
            ("GET", "/api/today"): lambda: s.today(),
            ("GET", "/api/inbox"): lambda: s.inbox(),
            ("GET", "/api/agents"): lambda: s.agents(),
            ("GET", "/api/export-settings"): lambda: s.export_settings(),
            ("POST", "/api/terms"): lambda: s.accept_terms(bool(b.get("agree")), str(b.get("age", "")),
                                                          bool(b.get("guardian_ok"))),
            ("POST", "/api/sharing"): lambda: s.set_sharing(bool(b.get("on"))),
            ("POST", "/api/key"): lambda: s.check_key(str(b.get("key", ""))),
            ("POST", "/api/key/keep"): lambda: s.keep_saved_key(),
            ("POST", "/api/key/forget"): lambda: s.forget_key(),
            ("POST", "/api/game"): lambda: s.connect_game(str(b.get("link", ""))),
            ("POST", "/api/details"): lambda: s.save_details(str(b.get("genre", "")), str(b.get("systems", "")),
                                                            b.get("report_time"), b.get("quiet_start"),
                                                            b.get("quiet_end")),
            ("POST", "/api/agent"): lambda: s.connect_agent(str(b.get("key", ""))),
            ("POST", "/api/discord"): lambda: s.set_discord(str(b.get("choice", ""))),
            ("POST", "/api/finish"): lambda: s.finish_setup(b.get("start_with_windows")),
            ("POST", "/api/run"): lambda: s.run_now(),
            ("POST", "/api/mark"): lambda: s.mark(str(b.get("key", "")), str(b.get("action", ""))),
            ("POST", "/api/read"): lambda: s.mark_read(b.get("id")),
            ("POST", "/api/settings"): lambda: s.update_settings(**(b.get("changes") or {})),
            ("POST", "/api/open"): lambda: s.open_url(str(b.get("url", ""))),
            ("POST", "/api/open-report"): lambda: (s.opener(f"http://127.0.0.1:{self.port}/report"), {"ok": True})[1],
            ("POST", "/api/show"): lambda: (self.on_show(), {"ok": True})[1],
        }
        fn = routes.get((method, path))
        if fn is None:
            raise KeyError(path)
        return fn()

    def full_report(self) -> str:
        cfg = config.load()
        if not cfg.universe_id:
            return "<p>Not set up yet.</p>"
        conn = db.connect(config.db_path())
        try:
            return report.html_report(conn, cfg.universe_id, checkup.run(conn, cfg.universe_id, record=False))
        finally:
            conn.close()


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "roblox-agent-app"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: https://*.rbxcdn.com; "
                             "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def _handle(self, method: str) -> None:
            if not self._host_ok():
                return self._send(403, b"Forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/"):
                if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                    return self._json(403, {"ok": False, "message": "Wrong token."})
                body: dict = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        return self._json(413, {"ok": False, "message": "Too big."})
                    try:
                        body = json.loads(self.rfile.read(n) or b"{}")
                    except ValueError:
                        return self._json(400, {"ok": False, "message": "Bad request."})
                    if not isinstance(body, dict):
                        body = {}
                try:
                    return self._json(200, app.route(method, path, body))
                except KeyError:
                    return self._json(404, {"ok": False, "message": "Not found."})
                except Exception as e:  # never leak the key in an error
                    msg = config.redact(f"{type(e).__name__}: {e}", config.load().api_key)
                    return self._json(500, {"ok": False, "message": msg})
            if path == "/report":  # other sites can open it but can't read it (same-origin rule)
                return self._send(200, app.full_report().encode(), TYPES[".html"])
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            if name not in _web_names():
                return self._send(404, b"Not found", "text/plain")
            data = _web(name)
            if name == "index.html":
                data = data.replace(b"{{TOKEN}}", app.token.encode())
            ext = name[name.rfind("."):]
            self._send(200, data, TYPES.get(ext, "application/octet-stream"))

    return Handler


def start(app: App, port: int = 0) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
    return httpd
