"""Analytics aggregations.

All of these are plain SQL over the local database. The brief suggested
Chart.js / ECharts / Recharts; the charts here are rendered as inline SVG from
these dicts instead, which removes ~200 KB of JavaScript from every page load
and works with scripting disabled. If you want interactive charts later, the
shapes below are already chart-library-ready.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .db import Database


def dashboard(db: Database, days: int = 30) -> dict[str, Any]:
    since = int(time.time()) - days * 86400
    rows = db.query(
        """
        SELECT h.watched_at, h.progress, v.duration, v.author, v.author_id,
               s.education, s.entertainment, s.brainrot, s.info_density, s.music, s.topics
        FROM history h
        JOIN videos v ON v.id = h.video_id
        LEFT JOIN scores s ON s.video_id = h.video_id
        WHERE h.watched_at >= ?
        """,
        (since,),
    )

    total_seconds = 0.0
    completions: list[float] = []
    durations: list[int] = []
    educational = entertainment = brainrot = 0.0
    scored = 0
    shorts = 0
    per_day: dict[str, float] = {}
    per_channel: dict[str, float] = {}
    topic_counts: dict[str, float] = {}

    for row in rows:
        duration = int(row["duration"] or 0)
        progress = float(row["progress"] or 0)
        seconds = duration * progress
        total_seconds += seconds
        completions.append(progress)
        if duration:
            durations.append(duration)
        if duration and duration <= 180:
            shorts += 1
        day = time.strftime("%Y-%m-%d", time.localtime(row["watched_at"]))
        per_day[day] = per_day.get(day, 0.0) + seconds / 3600.0
        if row["author"]:
            per_channel[row["author"]] = per_channel.get(row["author"], 0.0) + seconds / 3600.0
        if row["education"] is not None:
            scored += 1
            educational += float(row["education"])
            entertainment += float(row["entertainment"])
            brainrot += float(row["brainrot"])
        for topic in json.loads(row["topics"] or "[]")[:5]:
            topic_counts[topic] = topic_counts.get(topic, 0.0) + max(0.15, progress)

    labels = [
        time.strftime("%Y-%m-%d", time.localtime(since + i * 86400))
        for i in range(days)
    ]

    impressions = db.scalar(
        "SELECT COUNT(*) FROM impressions WHERE shown_at >= ?", (since,), default=0
    )
    clicked = db.scalar(
        "SELECT COUNT(DISTINCT i.video_id) FROM impressions i "
        "JOIN history h ON h.video_id = i.video_id AND h.watched_at >= i.shown_at "
        "WHERE i.shown_at >= ?",
        (since,), default=0,
    )

    return {
        "days": days,
        "watch_hours": round(total_seconds / 3600.0, 1),
        "videos": len(rows),
        "shorts": shorts,
        "avg_completion": round(sum(completions) / len(completions), 3) if completions else 0.0,
        "median_duration": _median(durations),
        "education_share": round(educational / scored, 1) if scored else 0.0,
        "entertainment_share": round(entertainment / scored, 1) if scored else 0.0,
        "brainrot_share": round(brainrot / scored, 1) if scored else 0.0,
        "edu_ent_ratio": round(educational / entertainment, 2) if entertainment else 0.0,
        "daily": _series(labels, [round(per_day.get(d, 0.0), 2) for d in labels]),
        "channels": sorted(
            ({"name": k, "hours": round(v, 1)} for k, v in per_channel.items()),
            key=lambda item: -item["hours"],
        )[:12],
        "topics": sorted(
            ({"name": k, "weight": round(v, 1)} for k, v in topic_counts.items()),
            key=lambda item: -item["weight"],
        )[:14],
        "impressions": impressions,
        "click_through": round(clicked / impressions, 3) if impressions else 0.0,
        "diversity": _diversity(topic_counts),
    }


def attention(db: Database, limit: int = 500) -> dict[str, Any]:
    """Completion rate bucketed by video length, for "what length suits me"."""
    rows = db.query(
        "SELECT h.progress, v.duration FROM history h JOIN videos v ON v.id = h.video_id "
        "WHERE v.duration > 0 ORDER BY h.watched_at DESC LIMIT ?",
        (limit,),
    )
    buckets = [
        ("under 3 min", 0, 180), ("3-10 min", 180, 600), ("10-30 min", 600, 1800),
        ("30-60 min", 1800, 3600), ("over 1 h", 3600, 10**9),
    ]
    out = []
    for label, low, high in buckets:
        values = [float(r["progress"]) for r in rows if low <= r["duration"] < high]
        out.append({
            "label": label,
            "count": len(values),
            "completion": round(sum(values) / len(values), 3) if values else 0.0,
        })
    best = max(out, key=lambda b: (b["completion"], b["count"]))

    # Skipped segments, from SponsorBlock, for the videos you actually watched.
    # The interesting number is not how many ads exist but how much of your
    # watch time they would have taken had you not skipped them.
    skipped = db.one(
        """
        SELECT COUNT(*) AS n,
               AVG(sp.sponsor_ratio) AS sponsor,
               AVG(sp.filler_ratio) AS filler,
               SUM(sp.sponsor_ratio * v.duration * h.progress) AS seconds
        FROM history h
        JOIN videos v ON v.id = h.video_id
        JOIN sponsor_segments sp ON sp.video_id = h.video_id
        """
    )
    segments = {
        "videos": int((skipped["n"] if skipped else 0) or 0),
        "mean_sponsor": round(float((skipped["sponsor"] if skipped else 0) or 0) * 100, 1),
        "mean_filler": round(float((skipped["filler"] if skipped else 0) or 0) * 100, 1),
        "hours_saved": round(float((skipped["seconds"] if skipped else 0) or 0) / 3600.0, 2),
    }
    return {"buckets": out, "best": best["label"] if best["count"] else "",
            "segments": segments}


def rewatches(db: Database, limit: int = 15) -> list[dict]:
    rows = db.query(
        "SELECT h.video_id, COUNT(*) AS plays, v.title, v.author "
        "FROM history h JOIN videos v ON v.id = h.video_id "
        "GROUP BY h.video_id HAVING plays > 1 ORDER BY plays DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


def blind_spots(db: Database, limit: int = 12) -> list[dict]:
    """Videos shown repeatedly and never watched — usually a sign a filter or
    an interest weight is wrong rather than a sign of good targeting."""
    rows = db.query(
        """
        SELECT i.video_id, COUNT(*) AS shows, v.title, v.author
        FROM impressions i JOIN videos v ON v.id = i.video_id
        WHERE i.video_id NOT IN (SELECT video_id FROM history)
        GROUP BY i.video_id HAVING shows >= 3 ORDER BY shows DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def _series(labels: list[str], points: list[float]) -> dict[str, Any]:
    """Bundle a chart series with its own peak.

    Templates must not compute `max` over a key called "values": Jinja would
    resolve the dict's method of that name first.
    """
    return {"labels": labels, "points": points, "peak": max(points) if points else 1.0}


def _median(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _diversity(counts: dict[str, float]) -> float:
    import math

    total = sum(counts.values())
    if total <= 0 or len(counts) < 2:
        return 0.0
    entropy = -sum((v / total) * math.log(v / total) for v in counts.values() if v > 0)
    return round(entropy / math.log(len(counts)), 3)
