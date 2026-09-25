"""The interest graph.

This is the "what the system thinks you like" table, and it is a first-class
editable object rather than a hidden embedding. Three things can write to it:

* ``derive_from_history`` — statistical, decayed, always attributed as derived
* the user, from the debugger — attributed as manual and pinned
* the LLM brief compiler — attributed as llm

Origin is never overwritten by the deriver, so a tag you pinned stays exactly
where you put it no matter what you watch afterwards.
"""

from __future__ import annotations

import math
import time
from typing import Any

from . import textutil as T
from .db import Database

MIN_CONFIDENCE = 0.08


def derive_from_history(db: Database, half_life_days: float = 30.0, limit: int = 400) -> int:
    """Rebuild derived interests from watch history.

    Each watch contributes its video's term vector, weighted by how much of it
    you actually watched and how recently. Watching 8% of a video is not a
    preference; the completion curve below treats it as a weak negative.
    """
    rows = db.query(
        """
        SELECT h.video_id, h.progress, h.watched_at, s.vector
        FROM history h JOIN scores s ON s.video_id = h.video_id
        ORDER BY h.watched_at DESC LIMIT ?
        """,
        (limit,),
    )
    if not rows:
        # No history left — perhaps you just deleted it. What was derived from
        # it must go too; returning early here kept it forever.
        db.execute("DELETE FROM interests WHERE origin = 'derived' AND pinned = 0")
        return 0

    import json

    now = time.time()
    decay_lambda = math.log(2) / max(1.0, half_life_days * 86400.0)
    accumulator: dict[str, float] = {}
    negative: dict[str, float] = {}
    total_weight = 0.0

    for row in rows:
        vector = json.loads(row["vector"] or "{}")
        if not vector:
            continue
        recency = math.exp(-decay_lambda * max(0.0, now - row["watched_at"]))
        progress = float(row["progress"] or 0.0)
        # completion curve: <15% watched is a bounce, >70% is an endorsement
        signal = -0.6 if progress < 0.15 else (progress - 0.35) / 0.65
        weight = recency * signal
        target = accumulator if weight >= 0 else negative
        for term, value in vector.items():
            target[term] = target.get(term, 0.0) + abs(weight) * value
        total_weight += abs(weight)

    if total_weight <= 0:
        db.execute("DELETE FROM interests WHERE origin = 'derived' AND pinned = 0")
        return 0

    peak = max(accumulator.values(), default=0.0)
    peak_negative = max(negative.values(), default=0.0)
    written = 0
    now_int = int(now)

    existing = {r["tag"]: r for r in db.query("SELECT * FROM interests")}
    rows_to_write = []
    for term, value in accumulator.items():
        confidence = round(value / peak, 4) if peak else 0.0
        if confidence < MIN_CONFIDENCE:
            continue
        prior = existing.get(term)
        if prior and (prior["pinned"] or prior["origin"] in {"manual", "llm"}):
            continue
        penalty = negative.get(term, 0.0) / peak_negative if peak_negative else 0.0
        weight = round(max(-1.0, min(1.0, confidence - 0.5 * penalty)), 4)
        rows_to_write.append((term, weight, confidence, "derived", 0, now_int))
        written += 1

    # Terms that only ever show up in bounced videos become soft negatives.
    for term, value in negative.items():
        if term in accumulator:
            continue
        confidence = round(value / peak_negative, 4) if peak_negative else 0.0
        if confidence < 0.25:
            continue
        prior = existing.get(term)
        if prior and (prior["pinned"] or prior["origin"] in {"manual", "llm"}):
            continue
        rows_to_write.append((term, round(-0.4 * confidence, 4), confidence, "derived", 0, now_int))
        written += 1

    db.execute("DELETE FROM interests WHERE origin = 'derived' AND pinned = 0")
    db.executemany(
        "INSERT INTO interests(tag, weight, confidence, origin, pinned, updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(tag) DO UPDATE SET weight=excluded.weight, "
        "confidence=excluded.confidence, updated_at=excluded.updated_at",
        rows_to_write,
    )
    return written


def set_interest(db: Database, tag: str, weight: float, *, origin: str = "manual",
                 confidence: float = 1.0, pinned: bool = True) -> None:
    tag = tag.strip().lower()
    if not tag:
        return
    db.execute(
        "INSERT INTO interests(tag, weight, confidence, origin, pinned, updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(tag) DO UPDATE SET weight=excluded.weight, "
        "confidence=excluded.confidence, origin=excluded.origin, pinned=excluded.pinned, "
        "updated_at=excluded.updated_at",
        (tag, max(-1.0, min(1.0, weight)), max(0.0, min(1.0, confidence)),
         origin, int(pinned), int(time.time())),
    )


def remove_interest(db: Database, tag: str) -> None:
    db.execute("DELETE FROM interests WHERE tag = ?", (tag.strip().lower(),))


def interest_vector(db: Database) -> tuple[dict[str, float], dict[str, float]]:
    """Return (positive vector, negative vector), both L2-normalised."""
    positive: dict[str, float] = {}
    negative: dict[str, float] = {}
    for row in db.query("SELECT tag, weight, confidence FROM interests"):
        magnitude = abs(row["weight"]) * max(0.25, row["confidence"])
        if row["weight"] >= 0:
            positive[row["tag"]] = magnitude
        else:
            negative[row["tag"]] = magnitude
    return T.normalise(positive), T.normalise(negative)


def listing(db: Database, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM interests ORDER BY pinned DESC, ABS(weight) DESC, confidence DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in rows:
        out.append({
            "tag": row["tag"],
            "weight": round(row["weight"], 3),
            "confidence": round(row["confidence"], 3),
            "origin": row["origin"],
            "pinned": bool(row["pinned"]),
            "band": _band(row["confidence"]),
        })
    return out


def _band(confidence: float) -> str:
    if confidence >= 0.66:
        return "high"
    if confidence >= 0.33:
        return "medium"
    return "low"


def timeseries(db: Database, weeks: int = 12, top: int = 6) -> dict[str, Any]:
    """Interest strength per week, for the trend chart."""
    import json

    now = int(time.time())
    start = now - weeks * 7 * 86400
    rows = db.query(
        "SELECT h.watched_at, h.progress, s.topics FROM history h "
        "JOIN scores s ON s.video_id = h.video_id WHERE h.watched_at >= ? ORDER BY h.watched_at",
        (start,),
    )
    buckets: list[dict[str, float]] = [{} for _ in range(weeks)]
    totals: dict[str, float] = {}
    for row in rows:
        index = min(weeks - 1, max(0, int((row["watched_at"] - start) // (7 * 86400))))
        weight = max(0.1, float(row["progress"] or 0))
        for topic in json.loads(row["topics"] or "[]")[:6]:
            buckets[index][topic] = buckets[index].get(topic, 0.0) + weight
            totals[topic] = totals.get(topic, 0.0) + weight
    leaders = [t for t, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:top]]
    labels = [time.strftime("%d %b", time.localtime(start + i * 7 * 86400)) for i in range(weeks)]
    series = [{"name": t, "points": [round(b.get(t, 0.0), 2) for b in buckets]} for t in leaders]
    peak = max((max(s["points"]) for s in series), default=1.0) or 1.0
    # "points" rather than "values": a Jinja template asking for .values on a
    # dict gets the method, not the key.
    return {"labels": labels, "series": series, "peak": peak}
