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
