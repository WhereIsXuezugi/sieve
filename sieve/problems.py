"""Backend problems, shown in the web app instead of only in the terminal.

Each problem has a kind, a plain explanation, the raw technical message (shown
under "Technical details"), help links, and when it last happened. The same
kind happening again updates the entry rather than piling up. Shown as a
dismissible banner for a few hours, and on the pages it affects.
"""

from __future__ import annotations

import time
from typing import Any

from .db import Database

KEY = "problems"
SHOW_FOR = 6 * 3600

EXPLAIN = {
    "youtube_bot_check": {
        "title": "YouTube is asking Sieve to sign in",
        "text": ("YouTube wanted proof that Sieve is not a bot, so it did not send the video details or "
                 "file that was asked for. Everything that does not need yt-dlp keeps working. Add your "
                 "YouTube cookies under Controls, Source to fix it."),
        "links": [("How to pass cookies to yt-dlp",
                   "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"),
                  ("Exporting YouTube cookies",
                   "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies")],
        "fix": "/settings#youtube-sign-in",
    },
    "sponsorblock_timeout": {
        "title": "SponsorBlock did not answer in time",
        "text": ("Sponsor segments could not be looked up because the SponsorBlock server took too long. "
                 "Videos still load and play normally; they just will not skip sponsors or be ranked "
                 "by sponsor time until SponsorBlock answers again."),
        "links": [("SponsorBlock status", "https://status.sponsor.ajay.app/")],
    },
    "sponsorblock_error": {
        "title": "SponsorBlock data is unavailable",
        "text": ("Sponsor segments could not be looked up. Videos still load and play normally."),
        "links": [],
    },
    "dearrow_timeout": {
        "title": "DeArrow did not answer in time",
        "text": ("Community titles and thumbnails could not be looked up. Videos show their original "
                 "titles meanwhile."),
        "links": [],
    },
    "video_unavailable": {
        "title": "A video's details could not be retrieved",
        "text": "Sieve could not load this video's details from YouTube or Invidious.",
        "links": [],
    },
}


def record(db: Database, kind: str, detail: str, video_id: str = "") -> None:
    items = [p for p in (db.get_setting(KEY, []) or []) if p.get("kind") != kind]
    items.insert(0, {"kind": kind, "detail": str(detail)[:600], "video_id": video_id,
                     "at": int(time.time()), "count": 1 + next(
                         (p.get("count", 0) for p in (db.get_setting(KEY, []) or []) if p.get("kind") == kind), 0)})
    db.set_setting(KEY, items[:12])


def clear(db: Database, kind: str) -> None:
    items = db.get_setting(KEY, []) or []
    if any(p.get("kind") == kind for p in items):
        db.set_setting(KEY, [p for p in items if p.get("kind") != kind])


def recent(db: Database, within: int = SHOW_FOR) -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for p in db.get_setting(KEY, []) or []:
        if now - p.get("at", 0) > within or p.get("kind") not in EXPLAIN:
            continue
        out.append({**p, **EXPLAIN[p["kind"]]})
    return out


def latest(db: Database, kind: str, within: int = SHOW_FOR) -> dict[str, Any] | None:
    return next((p for p in recent(db, within) if p["kind"] == kind), None)


def classify_community(service: str, message: str) -> str:
    timeout = "timed out" in message.lower() or "timeout" in message.lower()
    if service == "sponsorblock":
        return "sponsorblock_timeout" if timeout else "sponsorblock_error"
    return "dearrow_timeout"
