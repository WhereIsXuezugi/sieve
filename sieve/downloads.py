"""Saving videos for offline viewing, one at a time, in the background.

Needs yt-dlp. Only single-file formats (video and sound already together) are
asked for, so no ffmpeg is needed — which matters on a phone, where there is
none. A download counts as one request against the pull limit.

Files go to `<data dir>/downloads/<video id>.<ext>` and play in Sieve's own
player, or open from the Library page.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .db import Database

log = logging.getLogger("sieve.downloads")

QUALITIES = {"360": 360, "480": 480, "720": 720, "1080": 1080}


def folder(data_dir: str) -> Path:
    path = Path(data_dir).expanduser() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def format_for(quality: str) -> str:
    height = QUALITIES.get(str(quality), 720)
    # Progressive (muxed) formats only: no ffmpeg needed to merge.
    return (f"best[height<={height}][ext=mp4][acodec!=none][vcodec!=none]/"
            f"best[height<={height}][acodec!=none][vcodec!=none]/best[acodec!=none][vcodec!=none]")


def queue(db: Database, video_id: str, quality: str = "720") -> dict[str, Any]:
    if str(quality) not in QUALITIES:
        raise ValueError(f"quality must be one of {', '.join(QUALITIES)}")
    now = int(time.time())
    db.execute(
        "INSERT INTO downloads(video_id, status, quality, created_at, updated_at) VALUES(?, 'queued', ?, ?, ?) "
        "ON CONFLICT(video_id) DO UPDATE SET status = CASE WHEN downloads.status = 'done' "
        "THEN 'done' ELSE 'queued' END, quality = excluded.quality, error = '', updated_at = excluded.updated_at",
        (video_id, str(quality), now, now))
    return dict(db.one("SELECT * FROM downloads WHERE video_id = ?", (video_id,)))


def listing(db: Database) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT d.*, v.title, v.author, v.duration FROM downloads d LEFT JOIN videos v ON v.id = d.video_id "
        "ORDER BY CASE d.status WHEN 'downloading' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, "
        "d.updated_at DESC")]


def local_file(db: Database, video_id: str) -> Path | None:
    row = db.one("SELECT path FROM downloads WHERE video_id = ? AND status = 'done'", (video_id,))
    if row and row["path"] and Path(row["path"]).exists():
        return Path(row["path"])
    return None


def remove(db: Database, video_id: str) -> bool:
    row = db.one("SELECT path FROM downloads WHERE video_id = ?", (video_id,))
    if row is None:
        return False
    if row["path"]:
        Path(row["path"]).unlink(missing_ok=True)
    db.execute("DELETE FROM downloads WHERE video_id = ?", (video_id,))
    return True


class Downloader:
    """Works through the queue on its own thread."""

    def __init__(self, db: Database, data_dir: str, budget=None, ydl_factory=None):
        self.db = db
        self.data_dir = data_dir
        self.budget = budget
        self._factory = ydl_factory
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def available(self) -> bool:
        if self._factory is not None:
            return True
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            return False
        return True

    def start(self) -> None:
        if self._thread is None:
            # Anything left "downloading" by a restart starts over.
            self.db.execute("UPDATE downloads SET status = 'queued' WHERE status = 'downloading'")
            self._thread = threading.Thread(target=self._loop, name="sieve-downloads", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._wake.wait(30)
                self._wake.clear()

    def run_once(self) -> bool:
        """Download the next queued video. False when there was none."""
        row = self.db.one("SELECT * FROM downloads WHERE status = 'queued' ORDER BY created_at LIMIT 1")
        if row is None:
            return False
        vid = row["video_id"]
        if not self.available():
            self._fail(vid, "downloading needs yt-dlp: pip install 'sieve[youtube]'")
            return True
        from . import ytauth

        paused = ytauth.status(self.db)
        if paused["blocked"]:
            # Wait out the bot-check pause rather than fail: the download
            # starts by itself once it lifts, or as soon as cookies are added.
            self._set(vid, status="queued", error=ytauth.advice(paused["signed_in"]))
            return False
        try:
            if self.budget is not None:
                self.budget.spend("download")
        except Exception as exc:        # the pull limit: try again later
            self._set(vid, status="queued", error=str(exc))
            return False
        self._set(vid, status="downloading", progress=0.0, error="")
        target = folder(self.data_dir)

        def hook(d: dict) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    self._set(vid, progress=round(min(0.99, d.get("downloaded_bytes", 0) / total), 3))

        options = {"format": format_for(row["quality"]), "outtmpl": str(target / f"{vid}.%(ext)s"),
                   "quiet": True, "no_warnings": True, "noprogress": True, "progress_hooks": [hook],
                   "noplaylist": True}
        auth = ytauth.ytdlp_options(self.db, self.data_dir)
        options.update(auth)
        try:
            factory = self._factory
            if factory is None:
                import yt_dlp

                factory = yt_dlp.YoutubeDL
            with factory(options) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
                path = Path(ydl.prepare_filename(info))
        except Exception as exc:
            if ytauth.is_bot_check(str(exc)):
                ytauth.record(self.db, str(exc), signed_in=bool(auth))
                self._set(vid, status="queued", error=ytauth.advice(bool(auth)))
                return False
            self._fail(vid, str(exc)[:300])
            return True
        ytauth.clear(self.db)
        if not path.exists():
            self._fail(vid, "the download finished but no file appeared")
            return True
        self._set(vid, status="done", progress=1.0, path=str(path), bytes=path.stat().st_size)
        return True

    def _fail(self, vid: str, error: str) -> None:
        log.info("download %s failed: %s", vid, error)
        self._set(vid, status="failed", error=error)

    def _set(self, vid: str, **fields: Any) -> None:
        fields["updated_at"] = int(time.time())
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE downloads SET {assignments} WHERE video_id = ?", (*fields.values(), vid))
