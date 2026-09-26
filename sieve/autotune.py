"""Letting an AI tune Sieve for you (Controls, AI: "Tune automatically").

Off by default; switching it off returns to fully manual. When on, the
background worker runs it at most once an hour:

1. Video scores. It rates up to `videos_per_hour` of your homepage videos on
   a few axes. Where it clearly disagrees with the engine (by 15 points or
   more), its value becomes an *AI* override — used for that video, and
   training the same correction model your own corrections train. Your own
   overrides always win and are never touched.
2. Targets and weights, at most once a day: from your recent More / Less
   choices and "tell Sieve why" notes, through the same whitelist the Brief
   page uses (llm.sanitise). The settings before each change are kept, so
   "Undo AI changes" puts them back and removes every AI override.

The slider bounds the work: model calls per run, videos per call.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import corrections, llm, ranking
from .config import Config, resolve_settings
from .db import Database

AXES = ("education", "clickbait", "brainrot", "info_density", "entertainment")
DISAGREE = 15.0
BATCH = 10

RATE_PROMPT = (
    "You rate YouTube videos for someone's personal recommender, from title and channel. For each "
    "numbered video give 0-100 scores for: education, clickbait, brainrot, info_density, entertainment. "
    "Be calibrated: most ordinary videos are 20-60. Answer JSON only: "
    '{"ratings": {"<number>": {"education": 0, ...}}}.')


def settings(db: Database) -> dict[str, Any]:
    raw = resolve_settings(db.get_setting("settings", {}) or {}).get("ai_tune", {})
    return {"enabled": bool(raw.get("enabled", False)),
            "videos_per_hour": max(5, min(50, int(raw.get("videos_per_hour", 15))))}


def due(db: Database) -> bool:
    last = (db.get_setting("ai_tune_log", {}) or {}).get("at", 0)
    return settings(db)["enabled"] and time.time() - last >= 3600


def run(cfg: Config, db: Database, force: bool = False) -> dict[str, Any]:
    tune = settings(db)
    live = llm.effective(cfg, db)
    log: dict[str, Any] = {"at": int(time.time()), "videos_checked": 0, "videos_adjusted": 0,
                           "settings_changed": [], "error": ""}
    try:
        if not llm.available(live):
            raise llm.LLMError("no AI connection is set up (Controls, AI)")
        log.update(_rate_videos(live, db, tune["videos_per_hour"]))
        last_settings = (db.get_setting("ai_tune_log", {}) or {}).get("settings_at", 0)
        if force or time.time() - last_settings >= 86400:
            log["settings_changed"] = _tune_settings(live, db)
            log["settings_at"] = log["at"]
        else:
            log["settings_at"] = last_settings
    except llm.LLMError as exc:
        log["error"] = str(exc)
    db.set_setting("ai_tune_log", log)
    return log


def _rate_videos(cfg: Config, db: Database, budget: int) -> dict[str, int]:
    user_set = {(r["video_id"], r["axis"]) for r in db.query(
        "SELECT video_id, axis FROM score_overrides WHERE source = 'user'")}
    rated = {r["video_id"] for r in db.query("SELECT DISTINCT video_id FROM score_overrides WHERE source = 'ai'")}
    items = [c for c in ranking.recommend(db, resolve_settings(db.get_setting("settings", {}) or {}),
                                          record=False, limit=100).items if c.id not in rated][:budget]
    checked = adjusted = 0
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        listing = "\n".join(f"{n}. {c.video.get('title')} — {c.video.get('author')}" for n, c in enumerate(batch))
        ratings = llm.complete_json(cfg, RATE_PROMPT, listing).get("ratings", {})
        for n, c in enumerate(batch):
            checked += 1
            for axis, value in (ratings.get(str(n)) or {}).items():
                if axis not in AXES or (c.id, axis) in user_set:
                    continue
                try:
                    value = max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    continue
                if abs(value - c.card.scores.get(axis, 50)) >= DISAGREE:
                    corrections.set_override(db, c.id, axis, value, source="ai")
                    adjusted += 1
    if adjusted:
        corrections.train(db)
    return {"videos_checked": checked, "videos_adjusted": adjusted}


def _tune_settings(cfg: Config, db: Database) -> list[str]:
    rows = db.query("SELECT f.kind, f.note, v.title FROM feedback f JOIN videos v ON v.id = f.video_id "
                    "ORDER BY f.created_at DESC LIMIT 40")
    if not rows:
        return []
    liked = [r["title"] for r in rows if r["kind"] == "more"][:15]
    disliked = [r["title"] for r in rows if r["kind"] == "less"][:15]
    notes = [r["note"][5:] for r in rows if (r["note"] or "").startswith("why: ")][:10]
    text = ("Adjust only score targets and ranking weights to fit these reactions.\n"
            f"Liked: {json.dumps(liked)}\nDisliked: {json.dumps(disliked)}\nTheir words: {json.dumps(notes)}")
    compiled = llm.compile_brief(cfg, text)
    patch = {k: v for k, v in (compiled.get("settings") or compiled).items() if k in ("targets", "weights")}
    if not patch:
        return []
    from .actions import save_settings

    db.set_setting("ai_tune_undo", {"at": int(time.time()), "settings": db.get_setting("settings", {}) or {}})
    save_settings(db, patch)
    return sorted(patch)


def undo(db: Database) -> dict[str, Any]:
    removed = db.execute("DELETE FROM score_overrides WHERE source = 'ai'").rowcount or 0
    restored = False
    snapshot = db.get_setting("ai_tune_undo", {}) or {}
    if snapshot.get("settings") is not None:
        db.set_setting("settings", snapshot["settings"])
        db.set_setting("ai_tune_undo", {})
        restored = True
    corrections.train(db)
    return {"overrides_removed": removed, "settings_restored": restored}
