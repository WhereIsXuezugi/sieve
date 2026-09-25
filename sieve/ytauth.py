"""When YouTube asks yt-dlp to prove it is not a bot.

YouTube sometimes answers yt-dlp with "Sign in to confirm you're not a bot",
especially from servers, VPNs and addresses that make many requests. The fix
yt-dlp documents is to pass the cookies of a signed-in YouTube session. Sieve
can use either:

- a cookies.txt file you upload (Controls, Source) — works everywhere,
  including on a phone or a headless server. Stored beside the database as
  `youtube-cookies.txt`, readable only by you, never copied into backups;
- the cookies of a browser on the same computer (`source.cookies_browser`).

When a bot check happens, Sieve stops asking yt-dlp for 30 minutes — asking
again at once is how an address gets flagged for longer — and carries on
without it: RSS feeds, YouTube's results and watch pages, oEmbed. Controls,
Source says what happened and what to do.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .db import Database
from .invidious import UpstreamUnavailable

COOLDOWN_SECONDS = 30 * 60
BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge", "opera", "vivaldi", "safari")
# Only the bot check pauses yt-dlp: "Sign in to confirm your age" concerns one
# age-restricted video, not the whole address. "not a bot" matches both the
# straight and the curly apostrophe YouTube uses ("you're" / "you’re").
_SIGNS = ("not a bot",)


class BotCheck(UpstreamUnavailable):
    """YouTube wants a signed-in session before yt-dlp may continue."""


def is_bot_check(message: str) -> bool:
    lowered = (message or "").lower()
    return any(sign in lowered for sign in _SIGNS)


def cookies_file(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir).expanduser() / "youtube-cookies.txt"


def save_cookies(data_dir: str | os.PathLike, content: bytes) -> Path:
    text = content.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    # Netscape cookie format: seven tab-separated fields per line.
    good = [line for line in lines if len(line.split("\t")) >= 7]
    if not good:
        raise ValueError("that is not a cookies.txt file (Netscape format, as the browser "
                         "extensions and yt-dlp export it)")
    if not any("youtube.com" in line.split("\t")[0] for line in good):
        raise ValueError("the file has no youtube.com cookies; export them while on youtube.com")
    path = cookies_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.startswith("# Netscape") else "# Netscape HTTP Cookie File\n" + text)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def remove_cookies(data_dir: str | os.PathLike) -> bool:
    path = cookies_file(data_dir)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def ytdlp_options(db: Database, data_dir: str | os.PathLike) -> dict[str, Any]:
    """What to add to every yt-dlp call so it signs in, if it can."""
    path = cookies_file(data_dir)
    if path.exists():
        return {"cookiefile": str(path)}
    from .config import resolve_settings

    browser = (resolve_settings(db.get_setting("settings", {}) or {}).get("source", {})
               .get("cookies_browser") or "")
    if browser in BROWSERS:
        return {"cookiesfrombrowser": (browser,)}
    return {}


def record(db: Database, message: str, signed_in: bool, video_id: str = "") -> None:
    db.set_setting("ytdlp_bot_check", {"at": int(time.time()), "message": message[:300],
                                       "signed_in": signed_in})
    from . import problems

    problems.record(db, "youtube_bot_check", message, video_id)


def clear(db: Database) -> None:
    if db.get_setting("ytdlp_bot_check", None):
        db.set_setting("ytdlp_bot_check", {})
        from . import problems

        problems.clear(db, "youtube_bot_check")


def status(db: Database) -> dict[str, Any]:
    state = db.get_setting("ytdlp_bot_check", {}) or {}
    at = int(state.get("at") or 0)
    return {"at": at or None, "signed_in": bool(state.get("signed_in")),
            "blocked": bool(at) and time.time() - at < COOLDOWN_SECONDS,
            "retry_at": at + COOLDOWN_SECONDS if at else None}


def advice(signed_in: bool) -> str:
    if signed_in:
        return ("YouTube asked Sieve to prove it is not a bot, even with your cookies. They may have "
                "expired: export fresh ones under Controls, Source.")
    return ("YouTube asked Sieve to prove it is not a bot. Add your YouTube cookies under Controls, "
            "Source, then try again.")
