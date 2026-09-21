"""Invidious API client.

Sieve never scrapes YouTube directly: every piece of catalogue data comes from
an Invidious instance's public JSON API, which is also what the upstream project
already maintains and keeps working. Point `instances` at your own instance and
this layer is a couple of hundred lines of HTTP plumbing.

Responses are cached in SQLite rather than Redis. On a single-user instance the
cache is a few megabytes and the read latency difference is irrelevant, so a
second daemon would be pure operational overhead.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .config import Config
from .db import Database

log = logging.getLogger("sieve.invidious")


class Invidious:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.instances = list(cfg.instances)
        self._current = 0
        self._client = httpx.Client(
            timeout=cfg.request_timeout,
            follow_redirects=True,
            headers={"User-Agent": "sieve/0.1 (+https://github.com/whereixuezugi/sieve)"},
        )

    # -- plumbing ----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def _cached(self, key: str) -> Any | None:
        row = self.db.one(
            "SELECT body FROM http_cache WHERE url = ? AND expires_at > ?",
            (key, int(time.time())),
        )
        if row is None:
            return None
        try:
            return json.loads(row["body"])
        except json.JSONDecodeError:
            return None

    def _store(self, key: str, value: Any, ttl: int) -> None:
        now = int(time.time())
        self.db.execute(
            "INSERT INTO http_cache(url, body, fetched_at, expires_at) VALUES(?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET body=excluded.body, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at",
            (key, json.dumps(value), now, now + ttl),
        )

    def get(self, path: str, params: dict | None = None, ttl: int | None = None) -> Any:
        ttl = self.cfg.cache_ttl if ttl is None else ttl
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        key = f"inv:{path}?{query}"
        if ttl > 0:
            hit = self._cached(key)
            if hit is not None:
                return hit

        last_error: Exception | None = None
        for offset in range(len(self.instances)):
            index = (self._current + offset) % len(self.instances)
            base = self.instances[index].rstrip("/")
            try:
                response = self._client.get(f"{base}{path}", params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                last_error = exc
                log.warning("instance %s failed for %s: %s", base, path, exc)
                continue
            self._current = index
            if ttl > 0:
                self._store(key, data, ttl)
            return data
        raise InvidiousUnavailable(f"no instance answered {path}: {last_error}")

    # -- endpoints ---------------------------------------------------------

    def video(self, video_id: str) -> dict:
        return self.get(f"/api/v1/videos/{video_id}", ttl=self.cfg.catalog_ttl)

    def channel(self, channel_id: str) -> dict:
        return self.get(f"/api/v1/channels/{channel_id}", ttl=self.cfg.catalog_ttl)

    def channel_videos(self, channel_id: str, sort: str = "newest") -> list[dict]:
        data = self.get(f"/api/v1/channels/{channel_id}/videos", {"sort_by": sort})
        return _videos_of(data)

    def search(self, query: str, **params: Any) -> list[dict]:
        args = {"q": query, "type": "video", **params}
        return _videos_of(self.get("/api/v1/search", args))

    def trending(self, region: str = "US", category: str = "") -> list[dict]:
        params: dict[str, Any] = {"region": region}
        if category:
            params["type"] = category
        return _videos_of(self.get("/api/v1/trending", params))

    def popular(self) -> list[dict]:
        return _videos_of(self.get("/api/v1/popular"))

    def playlist(self, playlist_id: str) -> dict:
        return self.get(f"/api/v1/playlists/{playlist_id}")

    def captions(self, video_id: str, lang: str = "en") -> str:
        """Fetch a caption track as plain text.

        This is why Sieve does not ship Whisper. Transcribing a 40-minute video
        locally costs minutes of CPU; asking Invidious for the caption track
        YouTube already has costs one request. Videos with no captions simply
        get scored from metadata alone, with lower confidence.
        """
        base = self.instances[self._current].rstrip("/")
        url = f"{base}/api/v1/captions/{video_id}"
        key = f"cap:{video_id}:{lang}"
        hit = self._cached(key)
        if hit is not None:
            return hit
        try:
            response = self._client.get(url, params={"label": lang, "lang": lang})
            response.raise_for_status()
            text = _strip_vtt(response.text)[: self.cfg.transcript_max_chars]
        except Exception as exc:
            log.debug("captions unavailable for %s: %s", video_id, exc)
            text = ""
        self._store(key, text, self.cfg.catalog_ttl)
        return text


class InvidiousUnavailable(RuntimeError):
    pass


def _videos_of(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict) and item.get("type") != "channel"]
    if isinstance(data, dict):
        for key in ("videos", "latestVideos", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _strip_vtt(text: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if "-->" in line or line.isdigit():
            continue
        line = line.replace("&nbsp;", " ")
        # YouTube's rolling captions repeat each line; dedupe adjacent repeats.
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(seen) > 4000:
            break
    return " ".join(lines)


def normalise_video(raw: dict) -> dict:
    """Map an Invidious video object onto our column names."""
    vid = raw.get("videoId") or raw.get("id") or ""
    duration = int(raw.get("lengthSeconds") or 0)
    published = int(raw.get("published") or 0)
    return {
        "id": vid,
        "title": raw.get("title") or "",
        "author": raw.get("author") or "",
        "author_id": raw.get("authorId") or "",
        "published": published,
        "duration": duration,
        "views": int(raw.get("viewCount") or 0),
        "likes": int(raw.get("likeCount") or 0),
        "description": (raw.get("description") or raw.get("descriptionHtml") or "")[:6000],
        "keywords": raw.get("keywords") or [],
        "genre": raw.get("genre") or "",
        "is_live": int(bool(raw.get("liveNow"))),
        "is_upcoming": int(bool(raw.get("isUpcoming"))),
        "family_safe": int(bool(raw.get("isFamilyFriendly", True))),
        "sub_count": int(raw.get("subCountText_num") or raw.get("subCount") or 0),
        "caption_langs": [c.get("languageCode", "") for c in (raw.get("captions") or [])],
    }
