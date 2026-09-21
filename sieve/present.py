"""How a recommendation is presented, shared by the page and the API.

The homepage and `GET /api/recommendations` must agree on what a video is
called and what it looks like — if DeArrow is on, both show the corrected title —
so the rules for that live here once rather than in a template and a serialiser.
"""

from __future__ import annotations

from typing import Any

from . import ranking
from .config import Config


def display_title(candidate: ranking.Candidate, settings: dict) -> dict[str, str]:
    video = candidate.video
    da = settings["dearrow"]
    if da.get("enabled") and da.get("replace_titles") and video.get("dearrow_title"):
        return {
            "text": video["dearrow_title"],
            "original": video.get("original_title", "") if da.get("show_original") else "",
            "source": "DeArrow",
        }
    return {"text": video["title"], "original": "", "source": ""}


def thumb_url(cfg: Config, candidate: ranking.Candidate, settings: dict) -> str:
    """Prefer a DeArrow community thumbnail, fall back to the instance proxy."""
    video_id = candidate.id
    da = settings["dearrow"]
    if da.get("enabled") and da.get("replace_thumbnails"):
        thumb_time = candidate.video.get("dearrow_thumb_time")
        base = cfg.dearrow_thumbnail_url.rstrip("/")
        if thumb_time is not None:
            return f"{base}/api/v1/getThumbnail?videoID={video_id}&time={thumb_time}"
    if video_id.startswith("demo"):
        return f"/demo/thumb/{video_id}.svg"
    return f"{cfg.instances[0].rstrip('/')}/vi/{video_id}/mqdefault.jpg"


def candidate_json(cfg: Config, candidate: ranking.Candidate, settings: dict,
                   slot: int) -> dict[str, Any]:
    """One recommendation, with everything the page shows and nothing it hides."""
    video = candidate.video
    title = display_title(candidate, settings)
    return {
        "slot": slot,
        "id": candidate.id,
        "title": title["text"],
        "original_title": title["original"] or None,
        "title_source": title["source"] or None,
        "author": video.get("author", ""),
        "author_id": candidate.channel_id,
        "duration": int(video.get("duration") or 0),
        "views": int(video.get("views") or 0),
        "published": int(video.get("published") or 0),
        "thumbnail": thumb_url(cfg, candidate, settings),
        "watch_url": f"{cfg.watch_base}{candidate.id}",
        "bucket": candidate.bucket,
        "progress": round(candidate.progress, 4),
        "score": round(candidate.score, 4),
        "scores": {k: round(v, 1) for k, v in candidate.card.scores.items()},
        "topics": candidate.card.topics,
        "sources": candidate.sources,
        "explanation": ranking.explain(candidate),
        "components": {k: round(v, 4) for k, v in candidate.components.items()},
        "notes": candidate.notes,
        "sponsor": video.get("sponsor") or None,
    }
