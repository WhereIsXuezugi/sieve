"""Operations shared by the web app and the JSON API.

Every user-facing action lives here once. A web form handler and an API endpoint
that do the same thing both call the same function, so the two surfaces cannot
drift apart — one of them cannot quietly gain validation the other lacks, or
write a slightly different shape to the database.

`tests/test_parity.py` holds both surfaces to that.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import interests as interest_store
from .config import MOOD_SECTIONS, deep_merge, default_settings, diff_settings, resolve_settings
from .db import Database


class ActionError(ValueError):
    """A request that is well-formed but cannot be carried out. Maps to HTTP 400."""


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def stored_settings(db: Database) -> dict:
    return db.get_setting("settings", {}) or {}


def resolved_settings(db: Database) -> dict:
    return resolve_settings(stored_settings(db))


def save_settings(db: Database, patch: dict) -> dict:
    merged = deep_merge(stored_settings(db), patch)
    db.set_setting("settings", merged)
    return merged


def reset_settings(db: Database) -> None:
    """Reset controls to defaults. History, subscriptions, channel policy,
    interests and playlists are untouched — they are data, not settings."""
    db.set_setting("settings", {})


# --------------------------------------------------------------------------
# Moods
# --------------------------------------------------------------------------

MOOD_NAME = re.compile(r"^[\w .\-]{1,40}$", re.UNICODE)


def list_moods(db: Database) -> dict[str, Any]:
    settings = resolved_settings(db)
    return {"active": settings.get("active_mood", ""), "moods": settings.get("moods", {})}


def save_mood(db: Database, name: str) -> dict:
    """Freeze the current settings as a named mood.

    A mood stores only what differs from the defaults, so editing a filter
    later still reaches every mood that did not deliberately override it.
    """
    name = (name or "").strip()
    if not MOOD_NAME.match(name):
        raise ActionError("mood names are 1–40 letters, digits, spaces, dots or dashes")
    base = default_settings()
    current = deep_merge(base, stored_settings(db))
    patch = diff_settings(base, current, MOOD_SECTIONS)
    save_settings(db, {"moods": {name: patch}})
    return patch


def delete_mood(db: Database, name: str) -> bool:
    if name not in list_moods(db)["moods"]:
        return False
    stored = stored_settings(db)
    moods = dict(stored.get("moods") or {})
    # Default moods live in DEFAULT_SETTINGS, not in storage. Deleting one
    # means recording that it is gone, or it would reappear on the next merge.
    moods[name] = None
    stored["moods"] = moods
    if stored.get("active_mood") == name:
        stored["active_mood"] = ""
    db.set_setting("settings", stored)
    return True


def activate_mood(db: Database, name: str) -> str:
    name = (name or "").strip()
    if name and name not in list_moods(db)["moods"]:
        raise ActionError(f"no mood called {name!r}")
    save_settings(db, {"active_mood": name})
    return name


# --------------------------------------------------------------------------
# Playlists
# --------------------------------------------------------------------------

PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def parse_playlist_ref(ref: str) -> str:
    """Accept a playlist id or any URL that carries one.

    People paste whatever is in their address bar: a YouTube or Invidious
    playlist page, a watch URL with `&list=`, or a bare id. All of those carry
    the id in a `list` query parameter, and a bare id is recognisable on sight.
    """
    ref = (ref or "").strip()
    if not ref:
        raise ActionError("paste a playlist id or URL")
    if "://" in ref or ref.startswith(("www.", "youtube.", "youtu.be")):
        parsed = urlparse(ref if "://" in ref else f"https://{ref}")
        candidates = parse_qs(parsed.query).get("list", [])
        if not candidates:
            raise ActionError("that URL has no playlist in it (no list= parameter)")
        ref = candidates[0]
    if ref in {"WL", "LL", "LM"}:
        raise ActionError(
            "YouTube's Watch Later and Liked lists are private to your Google account and "
            "not reachable through Invidious. Keep a public or unlisted playlist on your "
            "Invidious account instead, and import that."
        )
    if not PLAYLIST_ID.match(ref):
        raise ActionError(f"{ref!r} does not look like a playlist id")
    return ref


def list_playlists(db: Database) -> list[dict[str, Any]]:
    settings = resolved_settings(db)
    active = settings["homepage"].get("playlist_id", "")
    mode = settings["homepage"].get("mode", "")
    out = []
    for row in db.query("SELECT * FROM playlists ORDER BY updated_at DESC"):
        ids = json.loads(row["video_ids"] or "[]")
        out.append({
            "id": row["id"],
            "title": row["title"] or row["id"],
            "videos": len(ids),
            "updated_at": row["updated_at"],
            "homepage": mode == "playlist" and active in ("", row["id"]),
        })
    return out


def remove_playlist(db: Database, playlist_id: str) -> bool:
    existed = db.one("SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)) is not None
    db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    # A homepage pinned to a playlist that no longer exists would be empty, so
    # unpin it: an empty playlist_id means "all imported playlists".
    if resolved_settings(db)["homepage"].get("playlist_id") == playlist_id:
        save_settings(db, {"homepage": {"playlist_id": ""}})
    return existed


# --------------------------------------------------------------------------
# Blocklist: hidden videos and blocked title terms
# --------------------------------------------------------------------------

BLOCK_KINDS = {"video", "term"}


def list_blocklist(db: Database, kind: str | None = None) -> list[dict[str, Any]]:
    if kind is not None and kind not in BLOCK_KINDS:
        raise ActionError(f"kind must be one of {sorted(BLOCK_KINDS)}")
    rows = db.query(
        "SELECT b.kind, b.value, b.note, b.created_at, v.title FROM blocklist b "
        "LEFT JOIN videos v ON b.kind = 'video' AND v.id = b.value "
        + ("WHERE b.kind = ? " if kind else "WHERE b.kind IN ('video','term') ")
        + "ORDER BY b.created_at DESC",
        (kind,) if kind else (),
    )
    return [dict(r) for r in rows]


def add_block(db: Database, kind: str, value: str, note: str = "") -> dict[str, Any]:
    if kind not in BLOCK_KINDS:
        raise ActionError(
            f"kind must be one of {sorted(BLOCK_KINDS)}. Channels are blocked from the "
            "Channels page, which keeps priority and listing together."
        )
    value = (value or "").strip()
    if kind == "term":
        # The gate matches case-insensitively against titles, so store it the
        # way it will be compared.
        value = value.lower()
        if not 2 <= len(value) <= 80:
            raise ActionError("a blocked term is 2–80 characters")
    elif not value:
        raise ActionError("which video?")
    db.execute(
        "INSERT OR REPLACE INTO blocklist(kind, value, note, created_at) VALUES(?,?,?,?)",
        (kind, value, (note or "")[:200], int(time.time())),
    )
    return {"kind": kind, "value": value}


def remove_block(db: Database, kind: str, value: str) -> bool:
    if kind == "term":
        value = value.lower()
    existed = db.one("SELECT 1 FROM blocklist WHERE kind = ? AND value = ?", (kind, value)) is not None
    db.execute("DELETE FROM blocklist WHERE kind = ? AND value = ?", (kind, value))
    return existed


# --------------------------------------------------------------------------
# Brief
# --------------------------------------------------------------------------


def apply_brief(db: Database, text: str, compiled: dict) -> dict[str, Any]:
    """Apply a compiled brief: interests to the graph, the rest to settings."""
    patch = dict(compiled.get("patch") or {})
    if not patch:
        raise ActionError("nothing recognisable came out of that brief")
    interests = patch.pop("interests", [])
    for item in interests:
        interest_store.set_interest(db, item["tag"], item["weight"], origin="llm",
                                    confidence=0.9, pinned=False)
    patch["brief"] = {"text": text[:2000], "summary": compiled.get("summary", ""),
                      "compiled_at": int(time.time())}
    save_settings(db, patch)
    return {"interests": len(interests),
            "settings": sorted(k for k in patch if k != "brief")}


# --------------------------------------------------------------------------
# Saved profiles
# --------------------------------------------------------------------------


def delete_saved_profile(db: Database, name: str) -> bool:
    existed = db.one("SELECT 1 FROM saved_profiles WHERE name = ?", (name,)) is not None
    db.execute("DELETE FROM saved_profiles WHERE name = ?", (name,))
    return existed


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------

# "catalogue" empties the video catalogue and everything computed from it, and
# keeps everything that is yours: subscriptions, history, opens, feedback,
# channel settings, interests, playlists, controls, profiles, the trained model.
# The next sync rebuilds the catalogue from those.
CATALOGUE_TABLES = (
    "videos", "scores", "dearrow", "sponsor_segments", "hash_prefix_log",
    "http_cache", "impressions", "channel_fetches",
)
RESET_SCOPES = ("catalogue", "everything")
CONFIRM_WORD = "reset"
BACKUPS_KEPT = 5   # per kind; see backups.py and Controls, Backups


def reset(db: Database, scope: str, confirm: str, backup: bool = True) -> dict[str, Any]:
    """Delete the catalogue, or everything.

    `confirm` must be the word "reset". That is required from the API as well
    as the page, so a stray request cannot erase a database.

    With `backup` (the default) a consistent copy is written first with
    `VACUUM INTO`, which is safe while the server is running. The newest
    five are kept.
    """
    if scope not in RESET_SCOPES:
        raise ActionError(f"scope must be one of {list(RESET_SCOPES)}")
    if (confirm or "").strip().lower() != CONFIRM_WORD:
        raise ActionError(f'type "{CONFIRM_WORD}" to confirm; nothing was deleted')

    backup_path = _backup(db) if backup else None

    if scope == "everything":
        tables = [r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    else:
        tables = list(CATALOGUE_TABLES)

    deleted: dict[str, int] = {}
    with db._write_lock:
        conn = db.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Count first, then delete: some tables empty by cascade when
            # videos go, and rowcount would miss those.
            for table in tables:
                deleted[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables:
                conn.execute(f'DELETE FROM "{table}"')
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if scope == "catalogue":
            # An empty catalogue is a fresh start: let the first fetch run again.
            conn.execute("DELETE FROM settings WHERE key = 'pull_state'")
            conn.commit()
        # Give the space back. Outside the transaction: VACUUM cannot run in one.
        conn.execute("VACUUM")

    return {
        "scope": scope,
        "deleted": {t: n for t, n in deleted.items() if n},
        "rows": sum(deleted.values()),
        "backup": str(backup_path) if backup_path else None,
    }


def reset_pulled(db: Database, confirm: str, backup: bool = True) -> dict[str, Any]:
    """Delete the videos Sieve found for you, and start finding over.

    Deleted: videos that arrived through Fetch or background finding —
    starter channels, followed channels, searches, trending — with their
    scores, and the record of which channels were pulled when. The next Fetch
    starts from scratch.

    Kept, even if Sieve found it: anything from a channel you subscribe to or
    a playlist you imported, anything you watched, opened, rated or hid, and
    everything you brought yourself. Videos from before origins were recorded
    count as found when their channel was one Sieve pulled on its own.
    """
    from .ingest import PULLED_ORIGINS

    if (confirm or "").strip().lower() != CONFIRM_WORD:
        raise ActionError(f'type "{CONFIRM_WORD}" to confirm; nothing was deleted')
    backup_path = _backup(db) if backup else None
    marks = ",".join("?" * len(PULLED_ORIGINS))
    playlist_ids: set[str] = set()
    for row in db.query("SELECT video_ids FROM playlists"):
        playlist_ids.update(json.loads(row["video_ids"] or "[]"))
    rows = db.query(
        f"""
        SELECT v.id FROM videos v
        WHERE (v.origin IN ({marks})
               OR (v.origin = '' AND v.author_id IN (
                   SELECT channel_id FROM channel_fetches WHERE origin IN ('followed', 'starter'))))
          AND v.author_id NOT IN (SELECT channel_id FROM subscriptions)
          AND v.id NOT IN (SELECT video_id FROM history)
          AND v.id NOT IN (SELECT video_id FROM opens)
          AND v.id NOT IN (SELECT video_id FROM feedback)
          AND v.id NOT IN (SELECT value FROM blocklist WHERE kind = 'video')
        """, PULLED_ORIGINS)
    ids = [r["id"] for r in rows if r["id"] not in playlist_ids]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        chunk_marks = ",".join("?" * len(chunk))
        for table, column in (("scores", "video_id"), ("impressions", "video_id"),
                              ("dearrow", "video_id"), ("sponsor_segments", "video_id"),
                              ("videos", "id")):
            db.execute(f"DELETE FROM {table} WHERE {column} IN ({chunk_marks})", chunk)
    fetches = db.execute(
        "DELETE FROM channel_fetches WHERE origin IN ('followed', 'starter')").rowcount
    db.execute("DELETE FROM settings WHERE key = 'pull_state'")
    db.execute("DELETE FROM channels WHERE id NOT IN (SELECT DISTINCT author_id FROM videos) "
               "AND id NOT IN (SELECT channel_id FROM channel_prefs)")
    return {"videos": len(ids), "channels_forgotten": fetches,
            "backup": str(backup_path) if backup_path else None}


def reset_pull_counter(db: Database) -> int:
    """Forget every recorded pull, so the pull limit starts from zero."""
    return db.execute("DELETE FROM pulls").rowcount


def _backup(db: Database) -> Path | None:
    from . import backups

    return backups.create(db, "before-reset")


def list_backups(db: Database) -> list[dict[str, Any]]:
    from . import backups

    return backups.listing(db)


# --------------------------------------------------------------------------
# Records: everything Sieve has recorded about you, readable and deletable
# --------------------------------------------------------------------------

RECORD_KINDS = {
    # kind: (table, time column, extra columns shown)
    "watches": ("history", "watched_at", "progress, dwell, origin, session"),
    "opens": ("opens", "opened_at", "provider"),
    "feedback": ("feedback", "created_at", "kind, note"),
}
FORGET_WORD = "forget"


def list_records(db: Database, kind: str, limit: int = 100, offset: int = 0,
                 video_id: str = "") -> dict[str, Any]:
    if kind not in RECORD_KINDS:
        raise ActionError(f"kind must be one of {sorted(RECORD_KINDS)}")
    table, when, extra = RECORD_KINDS[kind]
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    where, params = ("WHERE r.video_id = ?", [video_id]) if video_id else ("", [])
    total = db.one(f"SELECT COUNT(*) AS n FROM {table} r {where}", params)["n"]
    rows = db.query(
        f"SELECT r.rowid AS record, r.video_id, r.{when} AS at, {', '.join('r.' + c.strip() for c in extra.split(','))}, "
        f"v.title, v.author, v.duration FROM {table} r LEFT JOIN videos v ON v.id = r.video_id "
        f"{where} ORDER BY r.{when} DESC, r.rowid DESC LIMIT ? OFFSET ?",
        [*params, limit, offset])
    return {"kind": kind, "total": total, "limit": limit, "offset": offset,
            "records": [dict(r) for r in rows]}


def delete_record(db: Database, kind: str, record: int) -> bool:
    if kind not in RECORD_KINDS:
        raise ActionError(f"kind must be one of {sorted(RECORD_KINDS)}")
    table = RECORD_KINDS[kind][0]
    existed = db.one(f"SELECT 1 FROM {table} WHERE rowid = ?", (record,)) is not None
    db.execute(f"DELETE FROM {table} WHERE rowid = ?", (record,))
    if existed and kind == "watches":
        _relearn(db)
    return existed


def forget_records(db: Database, kind: str, confirm: str) -> int:
    """Delete every record of one kind. Asks for the word "forget"."""
    if kind not in RECORD_KINDS:
        raise ActionError(f"kind must be one of {sorted(RECORD_KINDS)}")
    if (confirm or "").strip().lower() != FORGET_WORD:
        raise ActionError(f'type "{FORGET_WORD}" to confirm; nothing was deleted')
    table = RECORD_KINDS[kind][0]
    count = db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
    db.execute(f"DELETE FROM {table}")
    if kind == "watches":
        _relearn(db)
    return count


def _relearn(db: Database) -> None:
    """Deleted watches must stop counting: interests and channel affinity are
    derived from history, so rebuild them. (The trained model is only
    rebuilt when you ask; the Debugger has a Retrain button.)"""
    from . import channels as channel_policy

    interest_store.derive_from_history(db)
    channel_policy.recompute_affinity(db)
