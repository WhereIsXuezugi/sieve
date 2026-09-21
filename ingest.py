"""Catalogue ingestion and background scoring.

Everything expensive happens here, off the request path. A homepage render is
pure SQLite; the network only gets touched by `sync` and `score_pending`, which
run on a timer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import channels as channel_policy
from . import interests as interest_store
from . import scoring
from .community import CommunityData
from .config import Config
from .db import Database
from .invidious import Invidious, InvidiousUnavailable, normalise_video

log = logging.getLogger("sieve.ingest")


class Ingestor:
    def __init__(self, cfg: Config, db: Database, api: Invidious, community: CommunityData):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.community = community
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sync = 0
        self.status = "idle"

    # -- catalogue ---------------------------------------------------------

    def sync(self, deep: bool = False) -> dict[str, int]:
        counts = {"subscriptions": 0, "trending": 0, "search": 0, "playlists": 0}
        self.status = "syncing"
        try:
            for row in self.db.query("SELECT channel_id, name FROM subscriptions"):
                try:
                    videos = self.api.channel_videos(row["channel_id"])
                except InvidiousUnavailable:
                    break
                except Exception as exc:
                    log.warning("channel %s failed: %s", row["channel_id"], exc)
                    continue
                self.db.upsert_videos(normalise_video(v) for v in videos[:30])
                counts["subscriptions"] += len(videos[:30])

            try:
                trending = self.api.trending()
                self.db.upsert_videos(normalise_video(v) for v in trending[:60])
                counts["trending"] = len(trending[:60])
            except Exception as exc:
                log.info("trending unavailable: %s", exc)

            if deep:
                positive, _ = interest_store.interest_vector(self.db)
                from . import textutil as T
                for term in T.top_terms(positive, 8):
                    try:
                        found = self.api.search(term, sort_by="upload_date")
                    except Exception:
                        continue
                    self.db.upsert_videos(normalise_video(v) for v in found[:20])
                    counts["search"] += len(found[:20])

            for row in self.db.query("SELECT id FROM playlists"):
                try:
                    data = self.api.playlist(row["id"])
                except Exception:
                    continue
                videos = data.get("videos") or []
                self.db.upsert_videos(normalise_video(v) for v in videos)
                self.db.execute(
                    "UPDATE playlists SET title = ?, video_ids = ?, updated_at = ? WHERE id = ?",
                    (data.get("title", ""), json.dumps([v.get("videoId") for v in videos if v.get("videoId")]),
                     int(time.time()), row["id"]),
                )
                counts["playlists"] += len(videos)
        finally:
            self.last_sync = int(time.time())
            self.status = "idle"
        return counts

    # -- scoring -----------------------------------------------------------

    def score_pending(self, limit: int | None = None, fetch_transcripts: bool | None = None) -> int:
        """Score videos that have no current scorecard."""
        limit = limit or self.cfg.score_batch
        transcripts = self.cfg.use_transcripts if fetch_transcripts is None else fetch_transcripts
        rows = self.db.query(
            "SELECT v.* FROM videos v LEFT JOIN scores s ON s.video_id = v.id "
            "WHERE s.video_id IS NULL OR s.version < ? ORDER BY v.published DESC LIMIT ?",
            (scoring.SCORER_VERSION, limit),
        )
        if not rows:
            return 0

        ids = [r["id"] for r in rows]
        settings = self.db.get_setting("settings", {}) or {}
        from .config import resolve_settings
        resolved = resolve_settings(settings)
        try:
            self.community.enrich(ids, resolved)
        except Exception as exc:
            log.debug("community enrichment skipped: %s", exc)
        branding = self.community.branding_map(ids)
        segments = self.community.segments_map(ids)

        written = []
        for row in rows:
            video = dict(row)
            transcript = video.get("transcript") or ""
            if transcripts and not transcript and video.get("caption_langs") not in (None, "[]"):
                transcript = self.api.captions(video["id"])
                if transcript:
                    self.db.execute(
                        "UPDATE videos SET transcript = ? WHERE id = ?", (transcript, video["id"])
                    )
            seg = segments.get(video["id"], {})
            extra = {
                "dearrow_retitled": 1.0 if branding.get(video["id"], {}).get("title") else 0.0,
                "sponsor_ratio": seg.get("sponsor_ratio", 0.0),
                "filler_ratio": seg.get("filler_ratio", 0.0),
            }
            card = scoring.score_video(video, transcript, extra)
            written.append(scoring.card_to_row(card))

        self.db.executemany(scoring.INSERT_SCORE, written)
        return len(written)

    def rescore_all(self) -> int:
        self.db.execute("UPDATE scores SET version = 0")
        total = 0
        while True:
            done = self.score_pending(limit=200, fetch_transcripts=False)
            total += done
            if done == 0:
                break
        return total

    # -- worker ------------------------------------------------------------

    def start(self, interval: int = 900) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(5):
                try:
                    scored = self.score_pending()
                    if scored == 0:
                        if time.time() - self.last_sync > interval:
                            self.sync()
                            channel_policy.recompute_affinity(self.db)
                            channel_policy.recompute_quality(self.db)
                            interest_store.derive_from_history(self.db)
                        self._stop.wait(30)
                except Exception as exc:
                    log.exception("ingest worker error: %s", exc)
                    self._stop.wait(60)

        self._thread = threading.Thread(target=loop, name="sieve-ingest", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # -- single video ------------------------------------------------------

    def ensure_video(self, video_id: str) -> dict | None:
        row = self.db.get_video(video_id)
        if row is None:
            try:
                self.db.upsert_video(normalise_video(self.api.video(video_id)))
            except Exception as exc:
                log.warning("could not fetch %s: %s", video_id, exc)
                return None
            row = self.db.get_video(video_id)
        if row is not None and self.db.one("SELECT 1 FROM scores WHERE video_id = ?", (video_id,)) is None:
            self.score_pending(limit=1)
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Importers
# --------------------------------------------------------------------------


def import_subscriptions(db: Database, payload: Any) -> int:
    """Accept Invidious, NewPipe, FreeTube or OPML-derived subscription lists."""
    entries: list[tuple[str, str]] = []

    if isinstance(payload, dict):
        if "subscriptions" in payload:  # Invidious / NewPipe export
            for item in payload["subscriptions"]:
                if isinstance(item, str):
                    entries.append((item, ""))
                elif isinstance(item, dict):
                    url = item.get("url", "")
                    cid = item.get("id") or item.get("channelId") or _channel_from_url(url)
                    if cid:
                        entries.append((cid, item.get("name") or item.get("author") or ""))
        elif "profiles" in payload:  # FreeTube
            for profile in payload.get("profiles", []):
                for sub in profile.get("subscriptions", []):
                    if sub.get("id"):
                        entries.append((sub["id"], sub.get("name", "")))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                entries.append((item, ""))
            elif isinstance(item, dict):
                cid = item.get("id") or item.get("channelId") or _channel_from_url(item.get("url", ""))
                if cid:
                    entries.append((cid, item.get("name") or item.get("author") or ""))

    now = int(time.time())
    db.executemany(
        "INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?,?,1.0,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), subscriptions.name)",
        [(cid, name, now) for cid, name in entries if cid],
    )
    return len(entries)


def import_history(db: Database, payload: Any, api: Invidious | None = None) -> int:
    """Import watch history.

    Accepts Invidious' `watch_history` export (a list of video ids), and Google
    Takeout's `watch-history.json` (which carries timestamps but no progress —
    those rows are recorded as a conservative 0.6 completion so they inform the
    model without pretending to precision we do not have).
    """
    events: list[tuple[str, int, float]] = []

    if isinstance(payload, dict) and "watch_history" in payload:
        now = int(time.time())
        for index, vid in enumerate(payload["watch_history"]):
            events.append((vid, now - index * 600, 0.6))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                events.append((item, int(time.time()), 0.6))
                continue
            if not isinstance(item, dict):
                continue
            vid = item.get("videoId") or item.get("id") or _video_from_url(
                item.get("titleUrl") or item.get("url") or ""
            )
            if not vid:
                continue
            when = item.get("time") or item.get("watched_at") or ""
            events.append((vid, _parse_time(when), float(item.get("progress", 0.6))))

    known = set(db.get_videos([e[0] for e in events]))
    rows = []
    for vid, when, progress in events:
        rows.append((vid, when, progress, 0, "import"))
        if vid not in known and api is not None:
            try:
                db.upsert_video(normalise_video(api.video(vid)))
                known.add(vid)
            except Exception:
                pass

    db.executemany(
        "INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,?,?,?)", rows
    )
    return len(rows)


def import_playlist(db: Database, api: Invidious, playlist_id: str) -> int:
    data = api.playlist(playlist_id)
    videos = data.get("videos") or []
    db.upsert_videos(normalise_video(v) for v in videos)
    db.execute(
        "INSERT INTO playlists(id, title, video_ids, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, video_ids=excluded.video_ids, "
        "updated_at=excluded.updated_at",
        (playlist_id, data.get("title", ""),
         json.dumps([v.get("videoId") for v in videos if v.get("videoId")]), int(time.time())),
    )
    return len(videos)


def _channel_from_url(url: str) -> str:
    if not url:
        return ""
    for marker in ("/channel/", "/c/", "/user/"):
        if marker in url:
            return url.split(marker, 1)[1].split("/")[0].split("?")[0]
    return ""


def _video_from_url(url: str) -> str:
    if "v=" in url:
        return url.split("v=", 1)[1].split("&")[0]
    if "youtu.be/" in url:
        return url.split("youtu.be/", 1)[1].split("?")[0]
    return ""


def _parse_time(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(time.mktime(time.strptime(value[:26], fmt)))
            except ValueError:
                continue
    return int(time.time())
