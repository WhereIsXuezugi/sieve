"""Thin SQLite layer.

No ORM on purpose. The whole point of the project is that a power user can open
``sieve.db`` in the sqlite3 shell and see exactly what the system believes about
them, so the schema stays legible and the access layer stays out of the way.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, ClassVar

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


# How a refresh merges into a stored video. A refresh often knows less than
# what is stored: an Invidious channel listing has no likes, keywords or
# category; a YouTube RSS entry has no duration. Overwriting blindly meant
# every sync erased what a fuller lookup had found, so a field is only
# replaced when the incoming value actually says something.
_KEEP_IF_ZERO = {"published", "duration", "likes", "sub_count"}
_KEEP_IF_BLANK = {"title", "author", "author_id", "description", "genre"}
_KEEP_IF_EMPTY_LIST = {"keywords", "caption_langs"}


def _merge_rule(column: str) -> str:
    if column in _KEEP_IF_ZERO:
        return f"CASE WHEN excluded.{column} > 0 THEN excluded.{column} ELSE {column} END"
    if column == "views":
        # Views only grow; a listing that rounds or omits them should not shrink them.
        return f"MAX({column}, excluded.{column})"
    if column in _KEEP_IF_BLANK:
        return f"COALESCE(NULLIF(excluded.{column}, ''), {column})"
    if column in _KEEP_IF_EMPTY_LIST:
        return f"CASE WHEN excluded.{column} IN ('', '[]') THEN {column} ELSE excluded.{column} END"
    if column == "transcript":
        return f"COALESCE(NULLIF(excluded.{column}, ''), {column})"
    if column == "first_seen":
        return f"CASE WHEN {column} > 0 THEN {column} ELSE excluded.{column} END"
    if column == "origin":
        # The first way a video arrived is kept: finding it again in a search
        # does not make a subscription video "found by Sieve".
        return f"CASE WHEN {column} = '' THEN excluded.{column} ELSE {column} END"
    if column == "is_short":
        # Once known to be a Short, a video stays one.
        return f"MAX({column}, excluded.{column})"
    return f"excluded.{column}"


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # The database owns its directory. Leaving this to `Config.load` meant
        # any caller building a Config by hand got "unable to open database
        # file" on first use.
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.init()

    # -- connections -------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def init(self) -> None:
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self.migrate()

    # New columns added after the first release. `CREATE TABLE IF NOT EXISTS`
    # does nothing to a table that already exists, so an upgrade needs this.
    # An explicit list rather than a schema differ: it is shorter, it is
    # legible in the same way the schema is, and it fails loudly if wrong.
    MIGRATIONS: ClassVar[list[tuple[str, str, str]]] = [
        ("scores", "profanity", "REAL NOT NULL DEFAULT 0"),
        ("channels", "quality", "REAL NOT NULL DEFAULT 50"),
        ("channels", "consistency", "REAL NOT NULL DEFAULT 0"),
        ("channels", "avg_duration", "REAL NOT NULL DEFAULT 0"),
        ("channels", "education", "REAL NOT NULL DEFAULT 50"),
        ("channels", "clickbait", "REAL NOT NULL DEFAULT 50"),
        ("videos", "nsfw_vision", "REAL NOT NULL DEFAULT -1"),
        ("videos", "is_short", "INTEGER NOT NULL DEFAULT 0"),
        ("history", "session", "TEXT"),
        ("videos", "origin", "TEXT NOT NULL DEFAULT ''"),
        # Scores before the channel prior and your corrections, as JSON. The
        # channel prior is computed from these, never from final scores, so
        # rescoring cannot feed a channel's average back into itself.
        ("scores", "base", "TEXT NOT NULL DEFAULT '{}'"),
        ("channel_prefs", "alert", "INTEGER NOT NULL DEFAULT 0"),
        # Set once captions were looked for without success, so a video
        # without any is not asked about on every scoring pass.
        ("videos", "captions_tried", "INTEGER NOT NULL DEFAULT 0"),
        # When Sieve first saw a video. Unlike fetched_at, seeing it again
        # does not change it: the recency lift fades from the first sighting.
        ("videos", "first_seen", "INTEGER NOT NULL DEFAULT 0"),
        # Attempts at filling in a missing title or channel name (at most 2).
        ("videos", "meta_tries", "INTEGER NOT NULL DEFAULT 0"),
        # Who set an override: 'user' (always wins) or 'ai' (autotune.py).
        ("score_overrides", "source", "TEXT NOT NULL DEFAULT 'user'"),
    ]

    def migrate(self) -> list[str]:
        applied = []
        with self._write_lock:
            for table, column, ddl in self.MIGRATIONS:
                existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if not existing:
                    continue  # table not in this schema version at all
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    applied.append(f"{table}.{column}")
            if applied:
                self.conn.commit()
        applied += self._migrate_click_rows()
        return applied

    def _migrate_click_rows(self) -> list[str]:
        """Move old click-throughs out of the watch history.

        Before `opens` existed, clicking Watch posted a watch of exactly 2%
        with no dwell time. Every consumer of `history` reads under 15% as a
        bounce, so each click taught Sieve you disliked the video you chose.
        Those rows are unmistakable — a real player report landing on exactly
        0.02 with zero dwell is vanishingly unlikely — so they become opens.
        Runs once; the marker makes it idempotent.
        """
        marker = "migration:click-rows-to-opens"
        if self.one("SELECT 1 FROM settings WHERE key = ?", (marker,)) is not None:
            return []
        with self._write_lock:
            moved = self.conn.execute(
                "INSERT INTO opens(video_id, opened_at, provider) "
                "SELECT video_id, watched_at, 'migrated' FROM history "
                "WHERE origin = 'player' AND progress = 0.02 AND dwell = 0"
            ).rowcount
            self.conn.execute(
                "DELETE FROM history WHERE origin = 'player' AND progress = 0.02 AND dwell = 0")
            self.conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)",
                              (marker, str(moved)))
            self.conn.commit()
        return [f"history -> opens ({moved} click rows)"] if moved else []

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- primitives --------------------------------------------------------

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def one(self, sql: str, params: Sequence = ()) -> sqlite3.Row | None:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def scalar(self, sql: str, params: Sequence = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return row[0] if row is not None and row[0] is not None else default

    def execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._write_lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # -- videos ------------------------------------------------------------

    def upsert_video(self, video: dict[str, Any]) -> None:
        self.upsert_videos([video])

    def upsert_videos(self, videos: Iterable[dict[str, Any]]) -> None:
        cols = [
            "id", "title", "author", "author_id", "published", "duration", "views",
            "likes", "description", "keywords", "genre", "is_live", "is_upcoming",
            "family_safe", "sub_count", "caption_langs", "transcript", "is_short", "fetched_at",
            "origin", "first_seen",
        ]
        now = int(time.time())
        rows = []
        for v in videos:
            if not v.get("id"):
                continue
            record = dict(v)
            record.setdefault("fetched_at", now)
            record.setdefault("first_seen", now)
            for key in ("keywords", "caption_langs"):
                if isinstance(record.get(key), (list, tuple)):
                    record[key] = json.dumps(list(record[key]))
                record.setdefault(key, "[]")
            rows.append(tuple(record.get(c) if record.get(c) is not None else _blank(c) for c in cols))
        placeholders = ",".join("?" * len(cols))
        updates = ", ".join(f"{c} = {_merge_rule(c)}" for c in cols if c != "id")
        self.executemany(
            f"INSERT INTO videos({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            rows,
        )

    def get_video(self, video_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM videos WHERE id = ?", (video_id,))

    def get_videos(self, ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        out: dict[str, sqlite3.Row] = {}
        ids = list(ids)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for row in self.query(f"SELECT * FROM videos WHERE id IN ({marks})", chunk):
                out[row["id"]] = row
        return out

    # -- history -----------------------------------------------------------

    def record_watch(self, video_id: str, progress: float, dwell: int = 0,
                     origin: str = "manual", at: int | None = None,
                     session: str | None = None) -> None:
        """Record a watch. With a `session`, repeated reports from one viewing
        update a single row — keeping the furthest point reached — instead of
        adding one row per report, which would read as a dozen rewatches."""
        progress = max(0.0, min(1.0, progress))
        when = at or int(time.time())
        if session:
            with self._write_lock:
                updated = self.conn.execute(
                    "UPDATE history SET progress = MAX(progress, ?), dwell = MAX(dwell, ?), "
                    "watched_at = ? WHERE video_id = ? AND session = ?",
                    (progress, dwell, when, video_id, session)).rowcount
                if not updated:
                    self.conn.execute(
                        "INSERT INTO history(video_id, watched_at, progress, dwell, origin, session) "
                        "VALUES(?,?,?,?,?,?)", (video_id, when, progress, dwell, origin, session))
                self.conn.commit()
            return
        self.execute(
            "INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,?,?,?)",
            (video_id, when, progress, dwell, origin),
        )

    def record_open(self, video_id: str, provider: str = "", at: int | None = None) -> None:
        self.execute("INSERT INTO opens(video_id, opened_at, provider) VALUES(?,?,?)",
                     (video_id, at or int(time.time()), provider))

    def opened_ids(self) -> set[str]:
        return {r["video_id"] for r in self.query("SELECT DISTINCT video_id FROM opens")}

    def watched_ids(self, min_progress: float = 0.0) -> set[str]:
        rows = self.query("SELECT DISTINCT video_id FROM history WHERE progress >= ?", (min_progress,))
        return {r["video_id"] for r in rows}

    def recent_history(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.query(
            "SELECT h.*, v.title, v.author, v.author_id, v.duration FROM history h "
            "LEFT JOIN videos v ON v.id = h.video_id ORDER BY h.watched_at DESC LIMIT ?",
            (limit,),
        )

    def unfinished(self, low: float = 0.03, high: float = 0.95, limit: int = 60) -> list[sqlite3.Row]:
        """Latest progress per video, keeping only genuinely partial watches."""
        return self.query(
            """
            SELECT video_id, MAX(watched_at) AS watched_at,
                   (SELECT progress FROM history h2 WHERE h2.video_id = h.video_id
                    ORDER BY h2.watched_at DESC LIMIT 1) AS progress
            FROM history h GROUP BY video_id
            HAVING progress > ? AND progress < ?
            ORDER BY watched_at DESC LIMIT ?
            """,
            (low, high, limit),
        )

    # -- housekeeping ------------------------------------------------------

    def prune(self, keep_impressions_days: int = 60, keep_unwatched_days: int = 30) -> dict[str, int]:
        now = int(time.time())
        imp = self.execute(
            "DELETE FROM impressions WHERE shown_at < ?", (now - keep_impressions_days * 86400,)
        ).rowcount
        self.execute("DELETE FROM http_cache WHERE expires_at < ?", (now,))
        self.execute("DELETE FROM pulls WHERE at < ?", (now - 31 * 86400,))
        vids = self.execute(
            """
            DELETE FROM videos WHERE fetched_at < ?
              AND id NOT IN (SELECT video_id FROM history)
              AND id NOT IN (SELECT video_id FROM feedback)
            """,
            (now - keep_unwatched_days * 86400,),
        ).rowcount
        self.execute("DELETE FROM scores WHERE video_id NOT IN (SELECT id FROM videos)")
        with self._write_lock:
            self.conn.execute("VACUUM")
        return {"impressions": imp, "videos": vids}

    def playable_count(self) -> int:
        """Real videos: an eleven-character YouTube id, not a demo one."""
        return self.scalar("SELECT COUNT(*) FROM videos WHERE length(id) = 11", default=0)

    def stats(self) -> dict[str, int]:
        return {
            "videos": self.scalar("SELECT COUNT(*) FROM videos", default=0),
            "scored": self.scalar("SELECT COUNT(*) FROM scores", default=0),
            "history": self.scalar("SELECT COUNT(*) FROM history", default=0),
            "feedback": self.scalar("SELECT COUNT(*) FROM feedback", default=0),
            "interests": self.scalar("SELECT COUNT(*) FROM interests", default=0),
            "subscriptions": self.scalar("SELECT COUNT(*) FROM subscriptions", default=0),
            "impressions": self.scalar("SELECT COUNT(*) FROM impressions", default=0),
        }


def _blank(column: str) -> Any:
    if column in {"published", "duration", "views", "likes", "is_live", "is_upcoming",
                  "sub_count", "fetched_at", "is_short"}:
        return 0
    if column == "family_safe":
        return 1
    if column == "transcript":
        return None
    if column in {"keywords", "caption_langs"}:
        return "[]"
    return ""
