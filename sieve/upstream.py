"""Which backend Sieve gets videos from.

    invidious   only your Invidious instances
    youtube     only YouTube directly (RSS feeds, plus yt-dlp when installed)
    auto        Invidious when it answers, YouTube when it does not (default)

The choice is a user setting (Controls, Getting videos), read on every call, so
changing it takes effect immediately. Both clients return Invidious-shaped
data, so nothing above this module knows which one answered.

In `auto`, an Invidious failure opens a short circuit breaker: for the next ten
minutes calls go straight to YouTube instead of paying every instance's timeout
on every channel of a sync.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .config import Config, resolve_settings
from .db import Database
from .invidious import Invidious, NotFound, UpstreamUnavailable
from .pulls import PullBudget, PullLimitReached
from .youtube import YouTube, ytdlp_available

BACKENDS = ("auto", "invidious", "youtube")
BREAKER_SECONDS = 600
HEALTH_SECONDS = 300


class Upstream:
    def __init__(self, cfg: Config, db: Database, youtube: YouTube | None = None,
                 invidious: Invidious | None = None):
        self.cfg = cfg
        self.db = db
        self.invidious = invidious or Invidious(cfg, db)
        self.youtube = youtube or YouTube(cfg, db)
        self._down_until = 0.0
        self._health: tuple[float, bool] | None = None
        self._health_lock = threading.Lock()
        self.last_backend = ""
        self.last_error = ""
        # Every request either client makes is counted, and may be refused,
        # here. Cache hits never reach it.
        self.budget = PullBudget(db)
        self.invidious.on_request = self.budget.spend
        self.youtube.on_request = self.budget.spend
        self.youtube.frugal = self.budget.tight

    def automatic(self):
        """`with api.automatic():` marks calls as Sieve acting by itself,
        which is what the pull limit binds by default."""
        return self.budget.automatic()

    # -- choosing -------------------------------------------------------------

    def setting(self) -> str:
        stored = self.db.get_setting("settings", {}) or {}
        choice = resolve_settings(stored).get("source", {}).get("backend", "auto")
        return choice if choice in BACKENDS else "auto"

    def _clients(self) -> list:
        choice = self.setting()
        if choice == "invidious":
            return [self.invidious]
        if choice == "youtube":
            return [self.youtube]
        if time.time() < self._down_until:
            return [self.youtube]
        # Ask the (cached, uncounted) health check before spending a pull on
        # an instance that is not there: with the shipped default pointing at
        # a local port, most people without Invidious paid for a failed
        # request before every fallback.
        if not self.invidious_healthy():
            return [self.youtube]
        return [self.invidious, self.youtube]

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        errors = []
        clients = self._clients()
        for client in clients:
            try:
                result = getattr(client, method)(*args, **kwargs)
            except (PullLimitReached, NotFound):
                # The limit covers every backend, and "no such channel" is an
                # answer, not an outage: neither is a reason to try the next.
                if client is self.invidious:
                    self._down_until = 0.0
                raise
            except UpstreamUnavailable as exc:
                errors.append(f"{client.name}: {exc}")
                if client is self.invidious and len(clients) > 1:
                    self._down_until = time.time() + BREAKER_SECONDS
                    self._health = (time.time(), False)
                continue
            self.last_backend = client.name
            if client is self.invidious:
                self._down_until = 0.0
                self._health = (time.time(), True)
            return result
        self.last_error = "; ".join(errors)
        raise UpstreamUnavailable(self.last_error or "no backend available")

    # -- the same interface as each client -----------------------------------

    def video(self, video_id: str) -> dict:
        return self._call("video", video_id)

    def channel(self, channel_id: str) -> dict:
        return self._call("channel", channel_id)

    def resolve_handle(self, handle: str) -> str:
        return self._call("resolve_handle", handle)

    def channel_videos(self, channel_id: str, sort: str = "newest") -> list[dict]:
        return self._call("channel_videos", channel_id, sort)

    def search(self, query: str, **params: Any) -> list[dict]:
        return self._call("search", query, **params)

    def trending(self, region: str = "US", category: str = "") -> list[dict]:
        return self._call("trending", region, category)

    def popular(self) -> list[dict]:
        return self._call("popular")

    def playlist(self, playlist_id: str) -> dict:
        return self._call("playlist", playlist_id)

    def captions(self, video_id: str, lang: str = "en") -> str:
        # Invidious reports a missing track as "", not as a failure, so there
        # is nothing to fall back from; use whichever backend is current.
        for client in self._clients():
            try:
                text = client.captions(video_id, lang)
            except UpstreamUnavailable:
                # The pull limit, or yt-dlp failing: score from metadata.
                return ""
            if text:
                return text
        return ""

    def get(self, path: str, params: dict | None = None, ttl: int | None = None) -> Any:
        """Raw Invidious API access, for callers that need it (the doctor)."""
        return self.invidious.get(path, params, ttl)

    # -- thumbnails and status ------------------------------------------------

    def invidious_healthy(self) -> bool:
        """Cached for five minutes; one caller probes while the rest wait."""
        cached = self._health
        if cached and time.time() - cached[0] < HEALTH_SECONDS:
            return cached[1]
        with self._health_lock:
            cached = self._health
            if cached and time.time() - cached[0] < HEALTH_SECONDS:
                return cached[1]
            healthy = self.invidious.reachable(timeout=2.0)
            self._health = (time.time(), healthy)
            return healthy

    def thumbnail_url(self, video_id: str) -> str:
        """Invidious' proxy when that is the backend and it answers, so the
        browser never talks to Google; YouTube's image host otherwise."""
        choice = self.setting()
        if choice == "invidious":
            return self.invidious.thumbnail_url(video_id)
        if choice == "auto" and time.time() >= self._down_until and self.invidious_healthy():
            return self.invidious.thumbnail_url(video_id)
        return self.youtube.thumbnail_url(video_id)

    def status(self) -> dict[str, Any]:
        return {
            "setting": self.setting(),
            "last_backend": self.last_backend or None,
            "invidious_skipped_until": int(self._down_until) if time.time() < self._down_until else None,
            # Installed *and* enabled: use_ytdlp=false turns it off even when
            # installed, and reporting it as available sent wrong advice.
            "yt_dlp": self.youtube.has_ytdlp,
            "yt_dlp_installed": ytdlp_available(),
            "last_error": self.last_error or None,
            # Whether search works right now: through a live Invidious, or
            # through yt-dlp. YouTube's feeds cannot search.
            "can_search": self.can_search(),
        }

    def can_search(self) -> bool:
        return True   # YouTube's results page needs neither yt-dlp nor Invidious

    def close(self) -> None:
        self.invidious.close()
        self.youtube.close()
