"""How many requests Sieve makes to YouTube or Invidious, and a limit on them.

A *pull* is one request that actually leaves the machine: one channel's
uploads, one search, one playlist, one video's details, one caption track.
Answers from the local cache cost nothing and are not counted, so a sync that
re-reads a hundred channels inside the cache lifetime spends nothing.

Every pull is written to the `pulls` table — when, what kind, and whether you
asked for it or Sieve did it by itself — so the Controls page can show what
the limit is spending on. The limit is a rolling window: "300 in any 24
hours", not "300 per calendar day", so there is no midnight burst.

By default the limit binds only automatic work (background syncs, discovery,
scoring's caption requests, history backfill). What you ask for — Sync now, a
Sift, importing a playlist — goes through, and is still counted, unless you
tick "count what I ask for too".
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any

from .config import PULL_LIMITS, PULL_WINDOWS, resolve_settings
from .db import Database
from .invidious import UpstreamUnavailable


class PullLimitReached(UpstreamUnavailable):
    """The pull limit is spent. A subclass of UpstreamUnavailable so every
    caller that already copes with "nothing answered" copes with this too —
    but the upstream chooser re-raises it rather than trying the next backend,
    since the limit covers every backend."""


KIND_NOUNS = {
    "channel": ("channel feed", "channel feeds"), "search": ("search", "searches"),
    "playlist": ("playlist", "playlists"), "video": ("video lookup", "video lookups"),
    "captions": ("caption track", "caption tracks"), "trending": ("trending list", "trending lists"),
    "popular": ("popular list", "popular lists"), "resolve": ("handle lookup", "handle lookups"),
}


def spent_on(by_kind: dict[str, int]) -> str:
    """{"channel": 32, "search": 1} -> "32 channel feeds and 1 search"."""
    parts = []
    for kind, n in by_kind.items():
        one, many = KIND_NOUNS.get(kind, ("other request", "other requests"))
        parts.append(f"{n} {one if n == 1 else many}")
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


def window_label(minutes: int) -> str:
    for value, label in PULL_WINDOWS:
        if value == minutes:
            return label
    if minutes % 1440 == 0:
        return f"{minutes // 1440} days"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def _clamp(key: str, value: Any) -> int:
    low, high = PULL_LIMITS[key]
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return low


class PullBudget:
    """Counts pulls and enforces the limit. One per Upstream."""

    def __init__(self, db: Database):
        self.db = db
        self._local = threading.local()
        self._lock = threading.Lock()

    # -- who is asking --------------------------------------------------------

    @contextmanager
    def automatic(self):
        """Everything inside this block is Sieve acting by itself."""
        previous = getattr(self._local, "automatic", False)
        self._local.automatic = True
        try:
            yield
        finally:
            self._local.automatic = previous

    @property
    def is_automatic(self) -> bool:
        return bool(getattr(self._local, "automatic", False))

    # -- settings -------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        pull = resolve_settings(self.db.get_setting("settings", {}) or {}).get("pull", {})
        return {
            "enabled": bool(pull.get("limit_enabled", False)),
            "count": _clamp("limit_count", pull.get("limit_count", 300)),
            "window": _clamp("limit_window", pull.get("limit_window", 1440)),
            "manual": bool(pull.get("limit_manual", False)),
        }

    # -- accounting -----------------------------------------------------------

    def used(self, window_minutes: int, now: float | None = None) -> int:
        since = int((now or time.time()) - window_minutes * 60)
        return int(self.db.scalar("SELECT COUNT(*) FROM pulls WHERE at > ?", (since,), 0))

    def binds(self, limit: dict[str, Any] | None = None) -> bool:
        """Whether the limit applies to the current caller."""
        limit = limit or self.settings()
        return limit["enabled"] and (self.is_automatic or limit["manual"])

    def remaining(self) -> int | None:
        """Pulls left for the current caller; None when unlimited."""
        limit = self.settings()
        if not self.binds(limit):
            return None
        return max(0, limit["count"] - self.used(limit["window"]))

    def tight(self) -> bool:
        """Under half the limit left for the current caller. Optional work —
        captions for scoring, backfilling imported history, yt-dlp's second
        lookup per channel — waits, so new videos always have budget."""
        limit = self.settings()
        if not self.binds(limit):
            return False
        return limit["count"] - self.used(limit["window"]) < limit["count"] / 2

    def spend(self, kind: str) -> None:
        """Record one pull, or refuse it. Called right before a request goes out."""
        with self._lock:
            limit = self.settings()
            if self.binds(limit):
                used = self.used(limit["window"])
                if used >= limit["count"]:
                    raise PullLimitReached(
                        f"request limit reached ({used} of {limit['count']} per "
                        f"{window_label(limit['window'])}); {self._next_free_in(limit)}")
            self.db.execute("INSERT INTO pulls(at, kind, automatic) VALUES(?,?,?)",
                            (int(time.time()), kind[:24], int(self.is_automatic)))

    def _next_free_at(self, limit: dict[str, Any]) -> int | None:
        """When the oldest pull inside the window ages out, freeing one."""
        since = int(time.time() - limit["window"] * 60)
        oldest = self.db.scalar(
            "SELECT at FROM pulls WHERE at > ? ORDER BY at ASC LIMIT 1 OFFSET ?",
            (since, max(0, self.used(limit["window"]) - limit["count"])), None)
        return int(oldest + limit["window"] * 60) if oldest else None

    def _next_free_in(self, limit: dict[str, Any]) -> str:
        at = self._next_free_at(limit)
        if at is None:
            return "next one available now"
        return "next one in " + _span(max(0, at - int(time.time())))

    # -- reporting ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        limit = self.settings()
        used = self.used(limit["window"])
        by_kind = {
            row["kind"]: row["n"] for row in self.db.query(
                "SELECT kind, COUNT(*) AS n FROM pulls WHERE at > ? GROUP BY kind ORDER BY n DESC",
                (int(time.time() - limit["window"] * 60),))
        }
        automatic = int(self.db.scalar(
            "SELECT COUNT(*) FROM pulls WHERE at > ? AND automatic = 1",
            (int(time.time() - limit["window"] * 60),), 0))
        exhausted = limit["enabled"] and used >= limit["count"]
        return {
            "enabled": limit["enabled"],
            "limit": limit["count"],
            "window_minutes": limit["window"],
            "window": window_label(limit["window"]),
            "counts_manual": limit["manual"],
            "used": used,
            "automatic": automatic,
            "manual": used - automatic,
            "remaining": max(0, limit["count"] - used) if limit["enabled"] else None,
            "exhausted": exhausted,
            "next_free_at": self._next_free_at(limit) if exhausted else None,
            "by_kind": by_kind,
            "spent_on": spent_on(by_kind),
            "last_hour": self.used(60),
            "last_day": self.used(1440),
        }

    def summary(self) -> str:
        s = self.status()
        if not s["enabled"]:
            return f"No limit. {s['last_hour']} requests in the last hour, {s['last_day']} today."
        line = f"{s['used']} of {s['limit']} requests used this {s['window']}"
        if s["exhausted"]:
            line += f"; {self._next_free_in(self.settings())}"
        return line + "."


def _span(seconds: int) -> str:
    if seconds < 90:
        return f"{max(1, seconds)} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    if seconds < 2 * 86400:
        return f"{round(seconds / 3600)} hours"
    return f"{round(seconds / 86400)} days"
