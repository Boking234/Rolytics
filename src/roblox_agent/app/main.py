"""Start the desktop app: local server, daily scheduler, window and tray icon.

    roblox-agent app              the app (its own window if pywebview is installed, else your browser)
    roblox-agent app --demo       practice mode with made-up numbers (separate folder, no real key)
    roblox-agent app --browser    always use your browser
    roblox-agent app --background tray icon only (used when Windows starts)

The window (pywebview) and tray icon (pystray + Pillow) are optional extras that
the Windows download includes. Without them the app opens in your browser and
keeps running in this terminal until you press Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from .. import __version__, config

APP_NAME = "Roblox Analytics Agent"


def _lock_path() -> Path:
    return config.home_dir() / "app.lock"


def _already_running() -> dict | None:
    """If the app is already open, return how to reach it."""
    p = _lock_path()
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
        req = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}/api/show", data=b"{}", method="POST",
                                     headers={"X-Token": info["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                return info
    except Exception:
        return None
    return None


def _write_lock(port: int, token: str) -> None:
    p = _lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the token is private to you
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)


def _make_icon_image(size: int = 64):
    from PIL import Image, ImageDraw  # optional extra
    k = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2 * k, 2 * k, 62 * k, 62 * k), radius=14 * k, fill=(37, 99, 235, 255))
    for i, h in enumerate((18, 30, 24, 40)):
        x = 12 + i * 11
        d.rounded_rectangle((x * k, (52 - h) * k, (x + 7) * k, 52 * k), radius=2 * k, fill=(255, 255, 255, 255))
    return img


def run(demo: bool = False, browser: bool = False, background: bool = False, port: int = 0) -> int:
    if demo:  # practice mode never touches your real settings or key, and starts fresh each time
        home = Path(tempfile.gettempdir()) / "roblox-agent-demo-app"
        shutil.rmtree(home, ignore_errors=True)
        os.environ[config.HOME_ENV] = str(home)
        os.environ.pop(config.KEY_ENV, None)
    from .server import App, start
    from .service import Service

    if not demo and _already_running():
        print("The app is already open; brought it to the front.")
        return 0

    service = Service(demo=demo)
    app = App(service)
    httpd = start(app, port)
    url = f"http://127.0.0.1:{app.port}/"
    app.on_show = lambda: webbrowser.open(url)  # replaced by the window, if there is one
    if not demo:
        _write_lock(app.port, app.token)
    service.start_scheduler()
    threading.Timer(5, service.tick).start()  # catch up soon after launch (e.g. the PC was off)

    tray = _start_tray(service, app, url)
    if tray:
        service.notifier = lambda title, body: tray.notify(body, title)

    try:
        if not browser and _open_window(app, url, hidden=background, tray=tray):
            return 0
        if not background:
            webbrowser.open(url)
        print(f"{APP_NAME} is running at {url}  (press Ctrl+C to stop)")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        service.stop()
        httpd.shutdown()
        if tray:
            tray.stop()
        if not demo:
            try:
                _lock_path().unlink()
            except OSError:
                pass


def _start_tray(service, app, url: str):
    try:
        import pystray  # optional extra
        image = _make_icon_image()
    except Exception:
        return None

    def open_app(icon=None, item=None):
        app.on_show()

    def check_now(icon=None, item=None):
        service.run_now()

    def toggle_pause(icon=None, item=None):
        service.update_settings(paused=not service.settings().get("paused"))

    def quit_app(icon=None, item=None):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open", open_app, default=True),
        pystray.MenuItem("Check now", check_now),
        pystray.MenuItem("Pause daily reports", toggle_pause, checked=lambda item: bool(service.settings().get("paused"))),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("roblox-analytics-agent", image, APP_NAME, menu)
    try:
        icon.run_detached()
    except Exception:
        return None
    return icon


def _open_window(app, url: str, hidden: bool, tray) -> bool:
    """Show the app in its own window. Returns False if pywebview isn't available."""
    try:
        import webview  # optional extra (pywebview)
    except Exception:
        return False
    window = webview.create_window(APP_NAME, url, width=980, height=760, min_size=(420, 520), hidden=hidden)

    def show():
        window.show()
        window.restore()

    app.on_show = show

    if tray:  # closing the window keeps the app (and the daily report) running in the tray
        def on_closing():
            window.hide()
            return False
        window.events.closing += on_closing
    webview.start()
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="roblox-agent app", description=f"{APP_NAME} {__version__}")
    p.add_argument("--demo", action="store_true", help="practice mode with made-up numbers")
    p.add_argument("--browser", action="store_true", help="open in your browser instead of a window")
    p.add_argument("--background", action="store_true", help="start in the tray without a window")
    p.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    return run(demo=a.demo, browser=a.browser, background=a.background, port=a.port)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
