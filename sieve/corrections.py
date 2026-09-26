"""Learning from the scores you correct.

When you set a score yourself ("this is an 80 on education, not a 35"), two
things happen. That video uses your value exactly. And every correction you
have made trains a small model of *where the engine is wrong for you*: it
learns the gap between your score and the engine's own, in log-odds, from the
same named features the engine uses, plus the channel and the words in the
title. The engine then adds that model's prediction to every video, shown in
the breakdown as "learned from scores you corrected".

It learns the gap, not the score, so with no corrections it adds nothing, and a
handful of corrections only moves videos that resemble them. Strong ridge
regularisation keeps a single correction from swinging the whole catalogue.
Pure Python: the numbers involved are small (your corrections, not the
catalogue) and the project has no numeric dependency.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from . import textutil as T
from .db import Database

AXES = ("education", "entertainment", "stimulation", "brainrot", "clickbait", "info_density",
        "technical_depth", "production", "ai_generated", "nsfw", "music", "profanity")
MAX_DELTA = 3.0          # log-odds: at most ~50 points around the middle
RIDGE = 2.0              # regularisation; larger = each correction reaches less far
EPOCHS = 300
RATE = 0.05


def inputs(video: Mapping[str, Any], features: Mapping[str, float]) -> dict[str, float]:
    """What the correction model sees: the engine's features, the channel,
    and the title's words (each word weighted so a long title is not louder)."""
    x = {f"f:{k}": float(v) for k, v in features.items() if abs(v) > 1e-6}
    channel = video.get("author_id") if hasattr(video, "get") else video["author_id"]
    if channel:
        x[f"c:{channel}"] = 1.0
    words = T.content_tokens(video["title"] or "")[:20]
    for word in words:
        x[f"w:{word}"] = x.get(f"w:{word}", 0.0) + 1.0 / max(1, len(words)) ** 0.5
    x["bias"] = 1.0
    return x


def predict(weights: Mapping[str, float], x: Mapping[str, float]) -> float:
    total = sum(weights.get(k, 0.0) * v for k, v in x.items())
    return max(-MAX_DELTA, min(MAX_DELTA, total))


def fit(samples: list[tuple[dict[str, float], float]]) -> dict[str, float]:
    """Ridge regression by gradient descent over sparse inputs."""
    weights: dict[str, float] = {}
    n = len(samples)
    for _ in range(EPOCHS):
        grad: dict[str, float] = {}
        for x, target in samples:
            error = sum(weights.get(k, 0.0) * v for k, v in x.items()) - target
            for k, v in x.items():
                grad[k] = grad.get(k, 0.0) + error * v
        for k in set(grad) | set(weights):
            w = weights.get(k, 0.0)
            weights[k] = w - RATE * (grad.get(k, 0.0) / n + RIDGE * w / n)
    return {k: round(w, 4) for k, w in weights.items() if abs(w) > 1e-3}


def train(db: Database) -> dict[str, dict[str, float]]:
    """Fit one model per axis from every correction, and store them."""
    from . import scoring

    rows = db.query(
        "SELECT o.axis, o.value, v.* FROM score_overrides o JOIN videos v ON v.id = o.video_id")
    samples: dict[str, list[tuple[dict[str, float], float]]] = {}
    for row in rows:
        video = dict(row)
        transcript = video.get("transcript") or ""
        features = scoring.extract_features(video, transcript)
        # The engine's own opinion, computed here rather than read back, so
        # the gap is measured against exactly what the engine would say.
        base = scoring.score_video(video, transcript).base[row["axis"]]
        gap = T.logit(row["value"] / 100.0) - T.logit(base / 100.0)
        samples.setdefault(row["axis"], []).append((inputs(video, features), gap))
    model = {axis: fit(data) for axis, data in samples.items() if data}
    db.set_setting("score_corrections", {"model": model, "trained_at": int(time.time()),
                                         "samples": {a: len(d) for a, d in samples.items()}})
    return model


def load(db: Database) -> dict[str, dict[str, float]]:
    return (db.get_setting("score_corrections", {}) or {}).get("model", {})


def overrides_for(db: Database, video_ids: list[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(video_ids), 400):
        chunk = video_ids[i:i + 400]
        for row in db.query(
                f"SELECT video_id, axis, value FROM score_overrides WHERE video_id IN "
                f"({','.join('?' * len(chunk))})", chunk):
            out.setdefault(row["video_id"], {})[row["axis"]] = float(row["value"])
    return out


def set_override(db: Database, video_id: str, axis: str, value: float | None, source: str = "user") -> None:
    if axis not in AXES:
        raise ValueError(f"unknown score {axis!r}; one of {', '.join(AXES)}")
    if value is None:
        db.execute("DELETE FROM score_overrides WHERE video_id = ? AND axis = ?", (video_id, axis))
        return
    value = max(0.0, min(100.0, float(value)))
    # Yours always wins: an AI value never replaces one you set.
    db.execute("INSERT INTO score_overrides(video_id, axis, value, created_at, source) VALUES(?,?,?,?,?) "
               "ON CONFLICT(video_id, axis) DO UPDATE SET value = excluded.value, "
               "created_at = excluded.created_at, source = excluded.source "
               "WHERE score_overrides.source = 'ai' OR excluded.source = 'user'",
               (video_id, axis, value, int(time.time()), source))
