"""Series: recognising "episode N" and finding episode N+1.

A series is a channel plus a title with its episode number taken out:
"Calculus 101 — Lecture 4: Limits" and "Calculus 101 — Lecture 5: Continuity"
are both ("UC…", "calculus 101 lecture"). Only a number next to an episode
word (episode, ep, part, lecture, lesson, chapter, day, …) or in a clear "3 of
10", "3/10" or "#3" counts — never a bare number, so "Calculus 101" and
"iPhone 15" are not episodes.

The subtitle after the number ("Limits", "Continuity") differs per episode, so
only what comes before the number names the series. When the number comes
first ("Part 3 of my rust series"), the text after it does.

Also: in a playlist you imported, the next video in the playlist is the next
episode, whatever its title says.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable

from .db import Database

_WORDS = (r"episode|episodes|ep|eps|part|pt|lecture|lec|lesson|chapter|ch|day|session|week|"
          r"module|unit|video|vid|class|tutorial|devlog|vlog|stream|round|season\s*\d+\s*episode")
_PATTERNS = [
    re.compile(rf"\b(?:{_WORDS})\.?\s*#?\s*(\d{{1,4}})\b", re.I),     # Lecture 4, Ep. 12, part #3
    re.compile(r"\b(\d{1,4})\s*(?:/|of)\s*(\d{1,4})\b", re.I),        # 3/10, 3 of 10
    re.compile(r"(?:^|\s)#(\d{1,4})\b"),                              # #7
    re.compile(r"\bS\d{1,2}\s*E(\d{1,3})\b", re.I),                   # S2E5
]
_NOISE = re.compile(r"[^a-z0-9]+")


def parse(title: str) -> tuple[str, int] | None:
    """(series key, episode number) or None."""
    title = title or ""
    for pattern in _PATTERNS:
        match = pattern.search(title)
        if not match:
            continue
        number = int(match.group(1))
        if number > 999 or number == 0:
            continue
        # "3 of 10" needs a plausible total: "Top 10 of 2026" is not episode 10.
        if pattern.groups == 2 and not number <= int(match.group(2)) < 1000:
            continue
        before = title[:match.start()]
        after = title[match.end():]
        # Normally the series is named before the number; if nothing is there
        # ("Part 3 of my series"), it is named after.
        name = before if len(_NOISE.sub("", before.lower())) >= 3 else after
        key = _NOISE.sub(" ", name.lower()).strip()
        # Drop a trailing subtitle separator's leftovers and generic words.
        key = re.sub(r"\b(the|a|an|of|and|with|in|on)\b", " ", key)
        key = re.sub(r"\s+", " ", key).strip()
        if len(key) < 3:
            continue
        return key, number
    return None


def next_episodes(db: Database, since_days: int = 60) -> dict[str, str]:
    """{video id: why} for the next episode of every series you are in.

    You are in a series once you have watched most of an episode (60% or
    more) in the last `since_days` days. Its next episode is the lowest
    numbered one above the highest you watched, from the same channel, that
    you have not watched."""
    since = int(time.time()) - since_days * 86400
    watched = db.query(
        "SELECT h.video_id, MAX(h.progress) AS progress, v.title, v.author_id FROM history h "
        "JOIN videos v ON v.id = h.video_id WHERE h.watched_at > ? GROUP BY h.video_id", (since,))
    seen_ids = {r["video_id"] for r in db.query("SELECT DISTINCT video_id FROM history")}
    furthest: dict[tuple[str, str], int] = {}
    for row in watched:
        if (row["progress"] or 0) < 0.6 or not row["author_id"]:
            continue
        parsed = parse(row["title"])
        if parsed:
            key = (row["author_id"], parsed[0])
            furthest[key] = max(furthest.get(key, 0), parsed[1])

    out: dict[str, str] = {}
    channels = {c for c, _ in furthest}
    if channels:
        marks = ",".join("?" * len(channels))
        best: dict[tuple[str, str], tuple[int, str]] = {}
        for row in db.query(f"SELECT id, title, author_id FROM videos WHERE author_id IN ({marks})",
                            list(channels)):
            if row["id"] in seen_ids:
                continue
            parsed = parse(row["title"])
            if not parsed:
                continue
            key = (row["author_id"], parsed[0])
            if key in furthest and parsed[1] > furthest[key] and (
                    key not in best or parsed[1] < best[key][0]):
                best[key] = (parsed[1], row["id"])
        for (channel, name), (number, vid) in best.items():
            out[vid] = f"episode {number} — you watched up to {furthest[(channel, name)]}"

    out.update(_playlist_next(db, since, seen_ids))
    return out


def _playlist_next(db: Database, since: int, seen_ids: set[str]) -> dict[str, str]:
    """In an imported playlist, the first unwatched video after the last one
    you watched most of."""
    recent = {r["video_id"] for r in db.query(
        "SELECT video_id FROM history WHERE watched_at > ? AND progress >= 0.6", (since,))}
    out: dict[str, str] = {}
    for row in db.query("SELECT title, video_ids FROM playlists"):
        ids = json.loads(row["video_ids"] or "[]")
        last = max((i for i, v in enumerate(ids) if v in recent), default=None)
        if last is None:
            continue
        for vid in ids[last + 1:]:
            if vid not in seen_ids:
                out.setdefault(vid, f"next in your playlist “{row['title'] or 'untitled'}”")
                break
    return out


def missing_next(db: Database, since_days: int = 60, limit: int = 3) -> Iterable[tuple[str, str, int]]:
    """Series you are in whose next episode is not in the catalogue yet:
    (channel id, series key, next number), for the sync to search for."""
    since = int(time.time()) - since_days * 86400
    furthest: dict[tuple[str, str], int] = {}
    for row in db.query(
            "SELECT v.title, v.author_id, MAX(h.progress) AS p FROM history h JOIN videos v "
            "ON v.id = h.video_id WHERE h.watched_at > ? GROUP BY h.video_id", (since,)):
        parsed = parse(row["title"]) if (row["p"] or 0) >= 0.6 else None
        if parsed and row["author_id"]:
            key = (row["author_id"], parsed[0])
            furthest[key] = max(furthest.get(key, 0), parsed[1])
    have = set(next_episodes(db, since_days))
    found = 0
    for (channel, key), number in furthest.items():
        in_catalogue = any(
            (p := parse(r["title"])) and p[0] == key and p[1] > number and r["id"] in have
            for r in db.query("SELECT id, title FROM videos WHERE author_id = ?", (channel,)))
        if not in_catalogue:
            yield channel, key, number + 1
            found += 1
            if found >= limit:
                return
