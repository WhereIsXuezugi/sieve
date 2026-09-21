"""A tiny online learner.

The brief suggested XGBoost/LightGBM/CatBoost with SHAP on top. For this task
that is the wrong shape of tool: the training set is one person's feedback
(hundreds of rows, not millions), it arrives one event at a time, and the model
has to explain itself on every render. A regularised logistic regression trained
by SGD fits in a few hundred bytes, updates in microseconds on the request
thread, and — being linear — hands back exact per-feature contributions for free.

If you later have a decade of history and want a gradient-boosted ranker, the
interface here (`features_for`, `predict`, `observe`) is the seam to swap at;
nothing upstream knows what is behind it.

Learning signals, in order of strength:
    explicit "less like this"    -> 0.0
    bounce (<10% watched)        -> 0.1
    explicit "more like this"    -> 1.0
    finished (>80% watched)      -> 0.9
    partial watch                -> the progress fraction itself
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .db import Database
from .scoring import ScoreCard

MODEL_NAME = "ranker"

FEATURES: list[str] = [
    "bias",
    "education", "entertainment", "stimulation", "brainrot", "clickbait",
    "info_density", "technical_depth", "production", "ai_generated", "music",
    "duration_short", "duration_medium", "duration_long",
    "interest_match", "channel_affinity", "channel_priority",
    "subscribed", "fresh", "popular", "obscure", "sponsor_load",
]


def features_for(card: ScoreCard, context: Mapping[str, Any]) -> list[float]:
    duration = float(context.get("duration", 0) or 0)
    return [
        1.0,
        card["education"] / 100.0,
        card["entertainment"] / 100.0,
        card["stimulation"] / 100.0,
        card["brainrot"] / 100.0,
        card["clickbait"] / 100.0,
        card["info_density"] / 100.0,
        card["technical_depth"] / 100.0,
        card["production"] / 100.0,
        card["ai_generated"] / 100.0,
        card["music"] / 100.0,
        1.0 if duration < 300 else 0.0,
        1.0 if 300 <= duration < 1800 else 0.0,
        1.0 if duration >= 1800 else 0.0,
        float(context.get("interest_match", 0.0)),
        float(context.get("channel_affinity", 0.0)),
        float(context.get("channel_priority", 0)) / 5.0,
        1.0 if context.get("subscribed") else 0.0,
        float(context.get("fresh", 0.0)),
        float(context.get("popular", 0.0)),
        float(context.get("obscure", 0.0)),
        float(context.get("sponsor_ratio", 0.0)),
    ]


class Ranker:
    def __init__(self, weights: list[float] | None = None, seen: int = 0):
        self.weights = weights or [0.0] * len(FEATURES)
        if len(self.weights) != len(FEATURES):
            self.weights = (self.weights + [0.0] * len(FEATURES))[: len(FEATURES)]
        self.seen = seen

    # -- inference ---------------------------------------------------------

    def predict(self, vector: Sequence[float]) -> float:
        z = sum(w * x for w, x in zip(self.weights, vector, strict=False))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def contributions(self, vector: Sequence[float], top: int = 3) -> list[dict]:
        parts = [
            {"feature": name, "effect": self.weights[i] * vector[i]}
            for i, name in enumerate(FEATURES)
            if name != "bias" and abs(self.weights[i] * vector[i]) > 0.02
        ]
        parts.sort(key=lambda p: -abs(p["effect"]))
        return parts[:top]

    @property
    def trained(self) -> bool:
        return self.seen >= 12

    # -- training ----------------------------------------------------------

    def observe(self, vector: Sequence[float], target: float, rate: float = 0.08,
                l2: float = 0.001) -> None:
        error = self.predict(vector) - target
        for i, x in enumerate(vector):
            gradient = error * x + (l2 * self.weights[i] if i else 0.0)
            self.weights[i] -= rate * gradient
        self.seen += 1

    def top_preferences(self, n: int = 6) -> list[dict]:
        ranked = sorted(
            ((FEATURES[i], w) for i, w in enumerate(self.weights) if FEATURES[i] != "bias"),
            key=lambda kv: -abs(kv[1]),
        )
        return [
            {"feature": name.replace("_", " "), "weight": round(w, 3),
             "direction": "prefers" if w > 0 else "avoids"}
            for name, w in ranked[:n] if abs(w) > 0.01
        ]


def load(db: Database) -> Ranker:
    row = db.one("SELECT body FROM model WHERE name = ?", (MODEL_NAME,))
    if row is None:
        return Ranker()
    try:
        body = json.loads(row["body"])
        return Ranker(body.get("weights"), int(body.get("seen", 0)))
    except (json.JSONDecodeError, TypeError, ValueError):
        return Ranker()


def save(db: Database, ranker: Ranker) -> None:
    db.execute(
        "INSERT INTO model(name, body, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
        (MODEL_NAME, json.dumps({"weights": ranker.weights, "seen": ranker.seen}), int(time.time())),
    )


def reset(db: Database) -> None:
    db.execute("DELETE FROM model WHERE name = ?", (MODEL_NAME,))


FEEDBACK_TARGETS = {
    "more": 1.0,
    "less": 0.0,
    "block_channel": 0.0,
    "higher_quality": 0.25,
    "shorter": 0.35,
    "longer": 0.35,
    "deeper": 0.3,
    "lighter": 0.3,
}


def train_from_events(db: Database, rate: float = 0.08, l2: float = 0.001,
                      epochs: int = 2, limit: int = 1500) -> dict[str, int]:
    """Replay history and feedback into a fresh model.

    Called after bulk imports and from `sieve train`. Live feedback is applied
    incrementally at request time instead, so this is a repair tool, not the
    normal path.
    """
    from .scoring import row_to_card

    rows = db.query(
        """
        SELECT h.video_id, h.progress, v.duration, v.author_id, s.*
        FROM history h
        JOIN videos v ON v.id = h.video_id
        JOIN scores s ON s.video_id = h.video_id
        ORDER BY h.watched_at DESC LIMIT ?
        """,
        (limit,),
    )
    feedback = db.query(
        """
        SELECT f.video_id, f.kind, v.duration, v.author_id, s.*
        FROM feedback f
        JOIN videos v ON v.id = f.video_id
        JOIN scores s ON s.video_id = f.video_id
        ORDER BY f.created_at DESC LIMIT ?
        """,
        (limit,),
    )
    affinity = {r["id"]: r["affinity"] for r in db.query("SELECT id, affinity FROM channels")}
    priority = {r["channel_id"]: r["priority"] for r in db.query("SELECT channel_id, priority FROM channel_prefs")}
    subscribed = {r["channel_id"] for r in db.query("SELECT channel_id FROM subscriptions")}

    ranker = Ranker()
    samples: list[tuple[list[float], float]] = []

    def context_of(row: Mapping[str, Any]) -> dict[str, Any]:
        cid = row["author_id"] or ""
        return {
            "duration": row["duration"],
            "channel_affinity": affinity.get(cid, 0.0),
            "channel_priority": priority.get(cid, 0),
            "subscribed": cid in subscribed,
        }

    for row in rows:
        card = row_to_card(row)
        progress = float(row["progress"] or 0)
        target = 0.1 if progress < 0.1 else (0.9 if progress > 0.8 else progress)
        samples.append((features_for(card, context_of(row)), target))
    for row in feedback:
        card = row_to_card(row)
        target = FEEDBACK_TARGETS.get(row["kind"], 0.5)
        # explicit feedback counts three times as much as a passive watch
        for _ in range(3):
            samples.append((features_for(card, context_of(row)), target))

    for _ in range(max(1, epochs)):
        for vector, target in samples:
            ranker.observe(vector, target, rate, l2)

    save(db, ranker)
    return {"samples": len(samples), "watches": len(rows), "feedback": len(feedback)}
