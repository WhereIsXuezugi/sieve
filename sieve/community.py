"""DeArrow and SponsorBlock integration.

Both are existing, mature, crowd-sourced open data projects from the
SponsorBlock team, and both expose a public API. Sieve does not attempt to
detect clickbait titles or sponsor reads on its own when a human-voted answer
already exists — it just asks.

Privacy note: both services support lookup by the first four hex characters of
``sha256(videoID)``. The server returns every video sharing that prefix — a few
hundred out of billions — so it never learns which one you wanted. Sieve always
uses the prefix endpoints, and because one prefix request covers many videos it
is also the cheaper option: a cold 36-video homepage costs at most 36 prefix
requests and usually far fewer, since popular videos cluster in the cache.

APIs used
    GET {sponsorblock_url}/api/skipSegments/{prefix}?categories=[...]
    GET {dearrow_url}/api/branding/{prefix}
    GET {dearrow_thumbnail_url}/api/v1/getThumbnail?videoID=..&time=..
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from .config import Config
from .db import Database

log = logging.getLogger("sieve.community")

SPONSOR_CATEGORIES = [
    "sponsor", "selfpromo", "interaction", "intro", "outro", "preview",
    "music_offtopic", "filler", "exclusive_access", "poi_highlight",
]


def hash_prefix(video_id: str, length: int = 4) -> str:
    return hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:length]


class CommunityData:
    """Shared client for the two sponsor.ajay.app services."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self._client = httpx.Client(
            timeout=cfg.request_timeout,
            follow_redirects=True,
            headers={"User-Agent": "sieve/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    # -- prefix bookkeeping ------------------------------------------------

    def _prefix_is_fresh(self, service: str, prefix: str) -> bool:
        row = self.db.one(
            "SELECT fetched_at FROM hash_prefix_log WHERE service = ? AND prefix = ?",
            (service, prefix),
        )
        return bool(row) and row["fetched_at"] > time.time() - self.cfg.community_ttl

    def _mark_prefix(self, service: str, prefix: str) -> None:
        self.db.execute(
            "INSERT INTO hash_prefix_log(service, prefix, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(service, prefix) DO UPDATE SET fetched_at = excluded.fetched_at",
            (service, prefix, int(time.time())),
        )

    def _pending_prefixes(self, service: str, video_ids: Iterable[str]) -> list[str]:
        prefixes = {hash_prefix(v) for v in video_ids if v}
        return sorted(p for p in prefixes if not self._prefix_is_fresh(service, p))

    # -- SponsorBlock ------------------------------------------------------

    def fetch_segments(self, video_ids: Sequence[str], categories: Sequence[str] | None = None) -> int:
        cats = list(categories or SPONSOR_CATEGORIES)
        fetched = 0
        for prefix in self._pending_prefixes("sponsorblock", video_ids):
            url = f"{self.cfg.sponsorblock_url.rstrip('/')}/api/skipSegments/{prefix}"
            params = {"categories": json.dumps(cats), "actionTypes": json.dumps(["skip", "mute", "full"])}
            try:
                response = self._client.get(url, params=params)
                if response.status_code == 404:
                    self._mark_prefix("sponsorblock", prefix)
                    continue
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                log.warning("sponsorblock prefix %s failed: %s", prefix, exc)
                continue
            rows = []
            for entry in payload if isinstance(payload, list) else []:
                vid = entry.get("videoID")
                segments = entry.get("segments") or []
                if not vid or not segments:
                    continue
                rows.append(_segment_row(vid, segments))
                fetched += 1
            self.db.executemany(
                "INSERT INTO sponsor_segments(video_id, segments, sponsor_ratio, filler_ratio, "
                "selfpromo_ratio, has_exclusive_access, fetched_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET segments=excluded.segments, "
                "sponsor_ratio=excluded.sponsor_ratio, filler_ratio=excluded.filler_ratio, "
                "selfpromo_ratio=excluded.selfpromo_ratio, "
                "has_exclusive_access=excluded.has_exclusive_access, fetched_at=excluded.fetched_at",
                rows,
            )
            self._mark_prefix("sponsorblock", prefix)
        return fetched

    def segments_for(self, video_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM sponsor_segments WHERE video_id = ?", (video_id,))
        if row is None:
            return {"segments": [], "sponsor_ratio": 0.0, "filler_ratio": 0.0,
                    "selfpromo_ratio": 0.0, "has_exclusive_access": 0}
        return {
            "segments": json.loads(row["segments"]),
            "sponsor_ratio": row["sponsor_ratio"],
            "filler_ratio": row["filler_ratio"],
            "selfpromo_ratio": row["selfpromo_ratio"],
            "has_exclusive_access": row["has_exclusive_access"],
        }

    def segments_map(self, video_ids: Sequence[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        ids = list(video_ids)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for row in self.db.query(
                f"SELECT * FROM sponsor_segments WHERE video_id IN ({marks})", chunk
            ):
                out[row["video_id"]] = {
                    "sponsor_ratio": row["sponsor_ratio"],
                    "filler_ratio": row["filler_ratio"],
                    "selfpromo_ratio": row["selfpromo_ratio"],
                    "has_exclusive_access": row["has_exclusive_access"],
                }
        return out

    # -- DeArrow -----------------------------------------------------------

    def fetch_branding(self, video_ids: Sequence[str], min_votes: int = 0) -> int:
        fetched = 0
        for prefix in self._pending_prefixes("dearrow", video_ids):
            url = f"{self.cfg.dearrow_url.rstrip('/')}/api/branding/{prefix}"
            try:
                response = self._client.get(url)
                if response.status_code == 404:
                    self._mark_prefix("dearrow", prefix)
                    continue
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                log.warning("dearrow prefix %s failed: %s", prefix, exc)
                continue
            rows = []
            for vid, entry in (payload or {}).items():
                row = _branding_row(vid, entry, min_votes)
                if row:
                    rows.append(row)
                    fetched += 1
            self.db.executemany(
                "INSERT INTO dearrow(video_id, title, title_votes, title_locked, thumb_time, "
                "thumb_original, fetched_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, "
                "title_votes=excluded.title_votes, title_locked=excluded.title_locked, "
                "thumb_time=excluded.thumb_time, thumb_original=excluded.thumb_original, "
                "fetched_at=excluded.fetched_at",
                rows,
            )
            self._mark_prefix("dearrow", prefix)
        return fetched

    def branding_map(self, video_ids: Sequence[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        ids = list(video_ids)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for row in self.db.query(f"SELECT * FROM dearrow WHERE video_id IN ({marks})", chunk):
                out[row["video_id"]] = {
                    "title": row["title"],
                    "votes": row["title_votes"],
                    "locked": row["title_locked"],
                    "thumb_time": row["thumb_time"],
                }
        return out

    def thumbnail_url(self, video_id: str, time_seconds: float | None) -> str:
        base = self.cfg.dearrow_thumbnail_url.rstrip("/")
        if time_seconds is None:
            return f"{base}/api/v1/getThumbnail?videoID={video_id}"
        return f"{base}/api/v1/getThumbnail?videoID={video_id}&time={time_seconds}"

    # -- one call the rest of the app uses --------------------------------

    def enrich(self, video_ids: Sequence[str], settings: dict) -> None:
        """Warm both caches for a batch of videos, honouring user settings."""
        ids = [v for v in video_ids if v]
        if not ids:
            return
        sb = settings.get("sponsorblock", {})
        da = settings.get("dearrow", {})
        if sb.get("enabled"):
            try:
                self.fetch_segments(ids, sb.get("categories"))
            except Exception as exc:
                log.warning("sponsorblock enrichment failed: %s", exc)
        if da.get("enabled"):
            try:
                self.fetch_branding(ids, int(da.get("min_votes") or 0))
            except Exception as exc:
                log.warning("dearrow enrichment failed: %s", exc)


def _segment_row(video_id: str, segments: list[dict]) -> tuple:
    duration = 0.0
    for seg in segments:
        duration = max(duration, float(seg.get("videoDuration") or 0))
    covered: dict[str, float] = {}
    keep = []
    exclusive = 0
    for seg in segments:
        category = seg.get("category", "")
        bounds = seg.get("segment") or [0, 0]
        try:
            start, end = float(bounds[0]), float(bounds[1])
        except (TypeError, ValueError, IndexError):
            continue
        length = max(0.0, end - start)
        covered[category] = covered.get(category, 0.0) + length
        if category == "exclusive_access":
            exclusive = 1
        keep.append({
            "category": category,
            "action": seg.get("actionType", "skip"),
            "start": round(start, 2),
            "end": round(end, 2),
            "votes": seg.get("votes", 0),
            "locked": seg.get("locked", 0),
            "uuid": seg.get("UUID", ""),
        })

    def ratio(category: str) -> float:
        if duration <= 0:
            return 0.0
        return round(min(1.0, covered.get(category, 0.0) / duration), 4)

    return (
        video_id,
        json.dumps(keep),
        ratio("sponsor"),
        ratio("filler"),
        ratio("selfpromo"),
        exclusive,
        int(time.time()),
    )


def _branding_row(video_id: str, entry: dict, min_votes: int) -> tuple | None:
    titles = [t for t in (entry.get("titles") or []) if not t.get("original")]
    thumbs = [t for t in (entry.get("thumbnails") or []) if not t.get("original")]
    title, votes, locked = "", 0, 0
    if titles:
        best = max(titles, key=lambda t: (t.get("locked", 0), t.get("votes", 0)))
        if best.get("votes", 0) >= min_votes or best.get("locked"):
            title = (best.get("title") or "").strip()
            votes = int(best.get("votes") or 0)
            locked = int(bool(best.get("locked")))
    thumb_time = None
    thumb_original = 1
    if thumbs:
        best_thumb = max(thumbs, key=lambda t: (t.get("locked", 0), t.get("votes", 0)))
        if best_thumb.get("timestamp") is not None:
            thumb_time = float(best_thumb["timestamp"])
            thumb_original = 0
    if not title and thumb_time is None:
        return None
    return (video_id, title, votes, locked, thumb_time, thumb_original, int(time.time()))
