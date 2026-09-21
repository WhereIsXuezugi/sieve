"""Channel policy.

Two independent notions of "I like this channel" live side by side, and the
distinction matters:

* **Manual priority** (-5..+5) is a standing instruction. You set it, it never
  moves on its own, and the debugger always attributes it to you.
* **Derived affinity** (0..1) is what the system infers from watch time,
  completion rate and recency. It decays, it is recomputed, and you can see the
  arithmetic behind it.

Blending them silently is how conventional feeds end up unexplainable, so Sieve
keeps them in separate columns and reports their contributions separately.

Listing is a third, orthogonal axis: ``allow`` (whitelist), ``block``
(blacklist), or ``neutral``. Whitelist-only mode turns the homepage into a
closed set of channels you chose, which is the most direct answer to "I want to
decide what I see".
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .db import Database

PRIORITY_MIN = -5
PRIORITY_MAX = 5


@dataclass
class ChannelPolicy:
    """Resolved view of every channel rule, built once per recommendation pass."""

    priority: dict[str, int]
    listing: dict[str, str]
    exempt: set[str]
    affinity: dict[str, float]
    quality: dict[str, float]
    names: dict[str, str]
    whitelist_only: bool
    allow: set[str]
    block: set[str]

    def score(self, channel_id: str, manual_strength: float,
              affinity_strength: float, affinity_enabled: bool) -> tuple[float, list[dict]]:
        """Return the channel contribution to the rank, plus its explanation."""
        parts: list[dict] = []
        total = 0.0
        prio = self.priority.get(channel_id, 0)
        if prio:
            delta = prio * manual_strength
            total += delta
            parts.append({
                "label": f"you set this channel to priority {prio:+d}",
                "effect": delta,
                "kind": "manual",
            })
        if affinity_enabled:
            aff = self.affinity.get(channel_id, 0.0)
            if aff > 0.01:
                delta = aff * affinity_strength
                total += delta
                parts.append({
                    "label": f"you watch this channel a lot ({round(aff * 100)}% affinity)",
                    "effect": delta,
                    "kind": "derived",
                })
        if channel_id in self.allow:
            parts.append({"label": "on your allow list", "effect": 0.0, "kind": "listing"})
        return total, parts

    def rejects(self, channel_id: str) -> str | None:
        if channel_id in self.block:
            return "channel is on your block list"
        if self.whitelist_only and channel_id not in self.allow:
            return "whitelist-only mode: channel is not on your allow list"
        return None


def load_policy(db: Database, settings: dict) -> ChannelPolicy:
    prefs = db.query("SELECT * FROM channel_prefs")
    priority = {r["channel_id"]: int(r["priority"]) for r in prefs}
    listing = {r["channel_id"]: r["listing"] for r in prefs}
    exempt = {r["channel_id"] for r in prefs if r["exempt_filters"] and r["listing"] == "allow"}
    names = {r["channel_id"]: r["name"] for r in prefs}

    affinity: dict[str, float] = {}
    quality: dict[str, float] = {}
    for row in db.query("SELECT id, name, affinity, quality FROM channels"):
        if row["affinity"]:
            affinity[row["id"]] = float(row["affinity"])
        quality[row["id"]] = float(row["quality"])
        names.setdefault(row["id"], row["name"])

    channel_cfg = settings.get("channels", {})
    return ChannelPolicy(
        priority=priority,
        listing=listing,
        exempt=exempt,
        affinity=affinity,
        quality=quality,
        names=names,
        whitelist_only=bool(channel_cfg.get("whitelist_only")),
        allow={cid for cid, mode in listing.items() if mode == "allow"},
        block={cid for cid, mode in listing.items() if mode == "block"},
    )


def set_preference(db: Database, channel_id: str, *, name: str | None = None,
                   priority: int | None = None, listing: str | None = None,
                   exempt_filters: bool | None = None, note: str | None = None) -> dict:
    row = db.one("SELECT * FROM channel_prefs WHERE channel_id = ?", (channel_id,))
    current = dict(row) if row else {
        "channel_id": channel_id, "name": "", "priority": 0,
        "listing": "neutral", "exempt_filters": 0, "note": "",
    }
    if name is not None:
        current["name"] = name
    elif not current["name"]:
        fallback = db.scalar("SELECT name FROM channels WHERE id = ?", (channel_id,), default="")
        current["name"] = fallback or db.scalar(
            "SELECT author FROM videos WHERE author_id = ? LIMIT 1", (channel_id,), default=""
        )
    if priority is not None:
        current["priority"] = max(PRIORITY_MIN, min(PRIORITY_MAX, int(priority)))
    if listing is not None:
        if listing not in {"neutral", "allow", "block"}:
            raise ValueError(f"unknown listing {listing!r}")
        current["listing"] = listing
    if exempt_filters is not None:
        current["exempt_filters"] = int(bool(exempt_filters))
    if note is not None:
        current["note"] = note[:500]

    db.execute(
        "INSERT INTO channel_prefs(channel_id, name, priority, listing, exempt_filters, note, updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET name=excluded.name, "
        "priority=excluded.priority, listing=excluded.listing, "
        "exempt_filters=excluded.exempt_filters, note=excluded.note, updated_at=excluded.updated_at",
        (channel_id, current["name"], current["priority"], current["listing"],
         current["exempt_filters"], current["note"], int(time.time())),
    )
    return current


def clear_preference(db: Database, channel_id: str) -> None:
    db.execute("DELETE FROM channel_prefs WHERE channel_id = ?", (channel_id,))


def recompute_affinity(db: Database, half_life_days: float = 45.0) -> int:
    """Derive per-channel affinity from watch time.

    Three things count, in descending order of honesty about intent:
      1. total watch seconds (progress x duration), recency-weighted
      2. mean completion, which separates "I finished it" from "I bailed at 20s"
      3. rewatch count

    The result is squashed to 0..1 against the busiest channel so the number is
    readable as "share of my attention" rather than an arbitrary magnitude.
    """
    rows = db.query(
        """
        SELECT v.author_id AS cid, v.author AS name,
               SUM(h.progress * v.duration) AS seconds,
               AVG(h.progress) AS completion,
               COUNT(*) AS plays,
               MAX(h.watched_at) AS last_watched
        FROM history h JOIN videos v ON v.id = h.video_id
        WHERE v.author_id != '' AND v.duration > 0
        GROUP BY v.author_id
        """
    )
    if not rows:
        return 0

    now = time.time()
    decay_lambda = math.log(2) / max(1.0, half_life_days * 86400.0)
    raw: dict[str, dict[str, float]] = {}
    for row in rows:
        age = max(0.0, now - (row["last_watched"] or now))
        recency = math.exp(-decay_lambda * age)
        seconds = float(row["seconds"] or 0.0)
        completion = float(row["completion"] or 0.0)
        plays = int(row["plays"] or 0)
        weighted = seconds * (0.5 + 0.5 * completion) * (0.35 + 0.65 * recency)
        raw[row["cid"]] = {
            "name": row["name"] or "",
            "seconds": seconds,
            "completion": completion,
            "plays": plays,
            "last_watched": row["last_watched"] or 0,
            "weighted": weighted,
        }

    peak = max((v["weighted"] for v in raw.values()), default=0.0)
    updates = []
    for cid, stats in raw.items():
        share = stats["weighted"] / peak if peak > 0 else 0.0
        # sqrt keeps a channel you watch a third as much as your favourite from
        # collapsing to a third of the boost
        affinity = round(math.sqrt(share), 4)
        updates.append((
            cid, stats["name"], affinity, int(stats["seconds"]), stats["plays"],
            round(stats["completion"], 4), int(stats["last_watched"]), int(now),
        ))

    db.executemany(
        "INSERT INTO channels(id, name, affinity, watch_seconds, watch_count, completion, "
        "last_watched, updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), channels.name), "
        "affinity=excluded.affinity, watch_seconds=excluded.watch_seconds, "
        "watch_count=excluded.watch_count, completion=excluded.completion, "
        "last_watched=excluded.last_watched, updated_at=excluded.updated_at",
        updates,
    )
    return len(updates)


def recompute_quality(db: Database, sample: int = 40) -> int:
    """Score each channel on its own catalogue.

    The spec asks for average duration, topic consistency, educational density
    and clickbait likelihood. Three of those are means over videos we have
    already scored, so they cost one query. Consistency is the interesting one:
    it is the mean cosine similarity between a channel's videos, which
    distinguishes "this channel has a subject" from "this channel uploads
    whatever is trending".

    Sampling is capped per channel because the pairwise comparison is quadratic
    and the estimate stops moving well before forty videos.
    """
    import itertools
    import json

    from . import textutil as T

    rows = db.query(
        """
        SELECT v.author_id AS cid, v.author AS name, v.duration, v.views,
               s.education, s.clickbait, s.brainrot, s.info_density, s.vector
        FROM videos v JOIN scores s ON s.video_id = v.id
        WHERE v.author_id != ''
        ORDER BY v.published DESC
        """
    )
    if not rows:
        return 0

    grouped: dict[str, list] = {}
    for row in rows:
        bucket = grouped.setdefault(row["cid"], [])
        if len(bucket) < sample:
            bucket.append(row)

    now = int(time.time())
    updates = []
    for cid, items in grouped.items():
        count = len(items)
        avg_duration = sum(int(i["duration"] or 0) for i in items) / count
        education = sum(float(i["education"]) for i in items) / count
        clickbait = sum(float(i["clickbait"]) for i in items) / count
        density = sum(float(i["info_density"]) for i in items) / count
        brainrot = sum(float(i["brainrot"]) for i in items) / count

        vectors = [json.loads(i["vector"] or "{}") for i in items]
        vectors = [v for v in vectors if v]
        consistency = 0.0
        if len(vectors) > 1:
            pairs = list(itertools.combinations(range(min(len(vectors), 18)), 2))
            consistency = sum(T.cosine(vectors[a], vectors[b]) for a, b in pairs) / len(pairs)

        # A channel is "good" here in the sense the user configured elsewhere:
        # substance up, bait down, and a subject it actually sticks to.
        quality = (
            0.35 * education
            + 0.25 * density
            + 0.20 * (100.0 * consistency)
            - 0.20 * clickbait
            - 0.10 * brainrot
        )
        updates.append((
            cid, items[0]["name"] or "", round(max(0.0, min(100.0, quality + 20.0)), 2),
            round(consistency, 4), round(avg_duration, 1),
            round(education, 2), round(clickbait, 2), now,
        ))

    db.executemany(
        "INSERT INTO channels(id, name, quality, consistency, avg_duration, education, "
        "clickbait, updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), channels.name), "
        "quality=excluded.quality, consistency=excluded.consistency, "
        "avg_duration=excluded.avg_duration, education=excluded.education, "
        "clickbait=excluded.clickbait, updated_at=excluded.updated_at",
        updates,
    )
    return len(updates)


def channel_table(db: Database, limit: int = 300) -> list[dict]:
    """Everything the channel manager screen needs, in one query."""
    rows = db.query(
        """
        SELECT c.id, c.name, c.affinity, c.watch_seconds, c.watch_count, c.completion,
               c.last_watched, c.quality, c.consistency, c.avg_duration,
               COALESCE(p.priority, 0) AS priority,
               COALESCE(p.listing, 'neutral') AS listing,
               COALESCE(p.exempt_filters, 0) AS exempt_filters,
               COALESCE(p.note, '') AS note,
               (SELECT COUNT(*) FROM subscriptions s WHERE s.channel_id = c.id) AS subscribed
        FROM channels c LEFT JOIN channel_prefs p ON p.channel_id = c.id
        UNION
        SELECT p.channel_id, p.name, 0, 0, 0, 0, 0, 50, 0, 0, p.priority, p.listing,
               p.exempt_filters, p.note,
               (SELECT COUNT(*) FROM subscriptions s WHERE s.channel_id = p.channel_id)
        FROM channel_prefs p
        WHERE p.channel_id NOT IN (SELECT id FROM channels)
        ORDER BY priority DESC, affinity DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def import_lists(db: Database, allow: Iterable[str] = (), block: Iterable[str] = (),
                 priorities: dict[str, int] | None = None) -> dict[str, int]:
    counts = {"allow": 0, "block": 0, "priority": 0}
    for cid in allow:
        set_preference(db, cid.strip(), listing="allow")
        counts["allow"] += 1
    for cid in block:
        set_preference(db, cid.strip(), listing="block")
        counts["block"] += 1
    for cid, value in (priorities or {}).items():
        set_preference(db, cid.strip(), priority=int(value))
        counts["priority"] += 1
    return counts


def export_lists(db: Database) -> dict[str, Any]:
    rows = db.query("SELECT * FROM channel_prefs WHERE listing != 'neutral' OR priority != 0")
    return {
        "allow": [{"id": r["channel_id"], "name": r["name"]} for r in rows if r["listing"] == "allow"],
        "block": [{"id": r["channel_id"], "name": r["name"]} for r in rows if r["listing"] == "block"],
        "priorities": {r["channel_id"]: r["priority"] for r in rows if r["priority"]},
    }
