"""Backups of the whole database: made by hand, on a schedule, or before
anything destructive; listed, deleted, and restored in place.

A backup is one file, `backups/sieve-<kind>-<time>.db` next to `sieve.db`,
written with `VACUUM INTO`, which is consistent while the server is running.
Kinds:

    manual          "Back up now"
    auto            the schedule (Controls, Backups)
    before-reset    written before a reset or deleting pulled videos
    before-restore  written before a restore, so a restore can be undone

Each kind keeps its own newest `keep` files, so a daily schedule can never
push out the backup taken before last week's reset. Files named by older
versions (`sieve-<time>.db`) count as before-reset.

Restoring copies the backup into the live database with SQLite's backup API —
no restart, no file juggling — then brings its schema up to date, since a
backup may come from an older Sieve.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .db import Database

KINDS = ("manual", "auto", "before-reset", "before-restore")
DEFAULT_KEEP = 5
NAME = re.compile(r"^sieve-(?:(manual|auto|before-reset|before-restore)-)?\d{8}-\d{6}-\d{3}\.db$")
RESTORE_WORD = "restore"


class BackupError(ValueError):
    """A backup request that cannot be carried out. Maps to HTTP 400."""


def folder(db: Database) -> Path | None:
    if db.path == ":memory:":
        return None
    return Path(db.path).expanduser().parent / "backups"


def settings(db: Database) -> dict[str, Any]:
    from .config import resolve_settings

    raw = resolve_settings(db.get_setting("settings", {}) or {}).get("backups", {})
    return {"auto": bool(raw.get("auto", False)),
            "every_hours": max(1, int(raw.get("every_hours", 24))),
            "keep": max(1, min(50, int(raw.get("keep", DEFAULT_KEEP))))}


def kind_of(path: Path) -> str:
    match = NAME.match(path.name)
    return (match.group(1) or "before-reset") if match else "other"


def _all(db: Database) -> list[Path]:
    """Oldest first, by modification time: names from the same second would
    sort the wrong way round ("…-2.db" before "….db")."""
    where = folder(db)
    if where is None or not where.exists():
        return []
    return sorted((p for p in where.glob("sieve-*.db") if NAME.match(p.name)),
                  key=lambda p: (p.stat().st_mtime_ns, p.name))


def listing(db: Database) -> list[dict[str, Any]]:
    """Newest first."""
    return [{"file": str(p), "name": p.name, "kind": kind_of(p), "bytes": p.stat().st_size,
             "created_at": int(p.stat().st_mtime)} for p in reversed(_all(db))]


def create(db: Database, kind: str = "manual") -> Path | None:
    if kind not in KINDS:
        raise BackupError(f"kind must be one of {list(KINDS)}")
    where = folder(db)
    if where is None:
        return None
    where.mkdir(parents=True, exist_ok=True)
    # Milliseconds in the name, so a name is never reused: the same name
    # holding different data at different times is the last thing you want to
    # discover while restoring.
    while True:
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
        target = where / f"sieve-{kind}-{stamp}.db"
        if not target.exists():
            break
        time.sleep(0.002)
    with db._write_lock:
        db.conn.execute("VACUUM INTO ?", (str(target),))
    prune(db)
    return target


def prune(db: Database) -> int:
    keep = settings(db)["keep"]
    removed = 0
    for kind in KINDS:
        files = [p for p in _all(db) if kind_of(p) == kind]
        for old in files[:-keep]:
            old.unlink(missing_ok=True)
            removed += 1
    return removed


def _find(db: Database, name: str) -> Path:
    """Only a file this module listed: a name is never a path."""
    for path in _all(db):
        if path.name == name:
            return path
    raise BackupError(f"no backup called {name!r}")


def delete(db: Database, name: str) -> bool:
    try:
        path = _find(db, name)
    except BackupError:
        return False
    path.unlink(missing_ok=True)
    return True


def restore(db: Database, name: str, confirm: str) -> dict[str, Any]:
    """Replace the live database with a backup, in place.

    `confirm` must be the word "restore". The current state is backed up
    first (kind before-restore), so restoring the wrong file is undone by
    restoring that one.
    """
    if (confirm or "").strip().lower() != RESTORE_WORD:
        raise BackupError(f'type "{RESTORE_WORD}" to confirm; nothing was changed')
    path = _find(db, name)
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            ok = source.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"{name} is not a readable database: {exc}") from None
        if not ok or not {"videos", "settings"} <= tables:
            raise BackupError(f"{name} is damaged or is not a Sieve database; nothing was changed")
        safety = create(db, "before-restore")
        with db._write_lock:
            source.backup(db.conn)
            db.conn.commit()
    finally:
        source.close()
    # A backup from an older Sieve lacks newer tables and columns.
    db.init()
    return {"restored": name, "safety_backup": safety.name if safety else None}


def auto_due(db: Database) -> bool:
    wanted = settings(db)
    if not wanted["auto"] or folder(db) is None:
        return False
    autos = [p for p in _all(db) if kind_of(p) == "auto"]
    if not autos:
        return True
    return time.time() - autos[-1].stat().st_mtime >= wanted["every_hours"] * 3600
