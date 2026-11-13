"""Where settings live, and how the API key is kept safe.

Everything is stored in your home folder, NOT in the project folder, so the
key can never be committed to GitHub by accident:

    ~/.roblox-agent/config.json   game settings (Universe ID)
    ~/.roblox-agent/secrets.env   your API key (readable only by you)
    ~/.roblox-agent/analytics.db  the data

Set ROBLOX_AGENT_HOME to use a different folder. If the environment variable
ROBLOX_API_KEY is set, it is used instead of secrets.env.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

KEY_ENV = "ROBLOX_API_KEY"
HOME_ENV = "ROBLOX_AGENT_HOME"


def home_dir() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".roblox-agent")


def config_path() -> Path:
    return home_dir() / "config.json"


def secrets_path() -> Path:
    return home_dir() / "secrets.env"


def db_path(demo: bool = False) -> Path:
    return home_dir() / ("demo.db" if demo else "analytics.db")


@dataclass
class Config:
    universe_id: int | None = None
    api_key: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"Config(universe_id={self.universe_id}, api_key={'***' if self.api_key else None})"


def parse_universe_id(text: str) -> int:
    """Accept a bare number or a Creator Dashboard link and return the Universe ID.

    Dashboard links look like
    https://create.roblox.com/dashboard/creations/experiences/1234567890/overview
    """
    text = (text or "").strip()
    m = re.search(r"/experiences/(\d+)", text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    raise ValueError(
        "That doesn't look like a Universe ID. Paste the number from your game's Creator Dashboard "
        "overview page (or the page's link).")


def load() -> Config:
    cfg = Config()
    p = config_path()
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        if data.get("universe_id"):
            cfg.universe_id = int(data["universe_id"])
    cfg.api_key = os.environ.get(KEY_ENV) or _read_secret()
    return cfg


def save_universe_id(universe_id: int) -> None:
    home_dir().mkdir(parents=True, exist_ok=True)
    p = config_path()
    data = json.loads(p.read_text(encoding="utf-8") or "{}") if p.exists() else {}
    data["universe_id"] = int(universe_id)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def save_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("The API key was empty.")
    if any(c.isspace() for c in key):
        raise ValueError("The API key has spaces or line breaks in it; copy it again.")
    home_dir().mkdir(parents=True, exist_ok=True)
    p = secrets_path()
    # Create the file readable/writable by the owner only, then write.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{KEY_ENV}={key}\n")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - e.g. some Windows setups
        pass


def forget_api_key() -> bool:
    p = secrets_path()
    if p.exists():
        p.unlink()
        return True
    return False


def _read_secret() -> str | None:
    p = secrets_path()
    if not p.exists():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith(KEY_ENV + "="):
            return line.split("=", 1)[1].strip() or None
    return None


def redact(text: str, secret: str | None) -> str:
    """Remove the key from any text before it is shown."""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text
