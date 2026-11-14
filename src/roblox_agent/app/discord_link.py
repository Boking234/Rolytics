"""Discord DMs: the hook for stage (e). Nothing here talks to Discord yet.

The plan (Part 4e): a small cloud relay holds the bot token and sends DMs. This app
will only ever send it the finished short report text (never raw data, never the
Roblox key). Until that relay exists:

* ``AVAILABLE`` is False, so the setup screen shows "coming in the next update"
  and lets people ask to be reminded.
* ``mirror()`` is called for every new inbox item and does nothing.

Stage (e) fills these in: ``link()`` opens Discord's "Add to My Apps" page with the
install ID, and ``mirror()`` posts the item to the relay, falling back to the desktop
notification if the DM fails.
"""

from __future__ import annotations

AVAILABLE = False
DM_LIMIT = 2000  # Discord message size limit; reports must be cut to fit


def status(settings: dict) -> str:
    """One of: blocked (under 13), linked, remind (wants it when ready), skipped, unset."""
    if settings.get("age") == "under13":
        return "blocked"
    if settings.get("discord_user"):
        return "linked"
    return settings.get("discord") or "unset"


def dm_text(title: str, body: str) -> str:
    """The text a DM would carry: short, under Discord's limit."""
    text = f"**{title}**\n{body}"
    tail = "\n...(more in the app)"
    return text if len(text) <= DM_LIMIT else text[: DM_LIMIT - len(tail)].rstrip() + tail


def mirror(item: dict, settings: dict) -> bool:
    """Send an inbox item as a DM. Returns True if sent. Stage (e) implements this."""
    if not AVAILABLE or status(settings) != "linked":
        return False
    raise NotImplementedError  # pragma: no cover - stage (e)
