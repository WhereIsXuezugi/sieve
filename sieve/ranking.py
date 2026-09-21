"""The ranking engine.

Pipeline, in order:

    1. candidates   — pull from each enabled source, tagged with its origin
    2. gate         — channel listings, simple filters, then the rule tree
    3. blend        — weighted sum of named components, per video
    4. explain      — normalise the positive components into percentages
    5. arrange      — MMR diversification, per-channel caps, budget quotas
    6. diagnose     — diversity index, rabbit-hole warning, rejection tally

Every rejection is recorded with a reason so the debugger can answer "why am I
*not* seeing X", which is usually the more useful question.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import channels as channel_policy
from . import interests as interest_store
from . import learner as learner_mod
from . import rules as rule_engine
from . import textutil as T
from .config import SOURCE_KEYS
from .db import Database
from .scoring import ScoreCard, bucket_of, row_to_card

SOURCE_LABELS = {
    "subscriptions": "from a channel you subscribe to",
    "history": "similar to what you have watched",
    "playlists": "from one of your playlists",
    "discovery": "popular outside your usual topics",
    "interests": "matches an interest you set",
    "niche": "small channel in your topics",
    "continue": "you left this unfinished",
}


@dataclass
class Candidate:
    video: dict
    card: ScoreCard
    sources: dict[str, float] = field(default_factory=dict)
    components: dict[str, float] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)
    score: float = 0.0
    bucket: str = "other"
    progress: float = 0.0

    @property
    def id(self) -> str:
        return self.video["id"]

    @property
    def channel_id(self) -> str:
        return self.video.get("author_id", "")


@dataclass
class Result:
    items: list[Candidate]
    diagnostics: dict[str, Any]


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------


def gather_candidates(db: Database, settings: dict, pool_size: int = 600) -> dict[str, dict[str, float]]:
    """Return {video_id: {source: strength}} from the local catalogue.

    Nothing here touches the network: `ingest.py` keeps the catalogue warm in
    the background, so rendering a homepage is pure SQLite and stays fast even
    when the upstream Invidious instance is slow or down.
    """
    weights = settings["sources"]
    enabled = [s for s in SOURCE_KEYS if weights.get(s, 0) > 0]
    out: dict[str, dict[str, float]] = {}

    def add(video_id: str, source: str, strength: float) -> None:
        if not video_id:
            return
        out.setdefault(video_id, {})[source] = max(out.get(video_id, {}).get(source, 0.0), strength)

    per_source = max(40, pool_size // max(1, len(enabled)))
    cutoff = int(time.time()) - 120 * 86400

    if "subscriptions" in enabled:
        for row in db.query(
            "SELECT v.id, v.published FROM videos v JOIN subscriptions s ON s.channel_id = v.author_id "
            "WHERE v.published > ? ORDER BY v.published DESC LIMIT ?",
            (cutoff, per_source),
        ):
            add(row["id"], "subscriptions", 1.0)

    if "history" in enabled:
        positive, _ = interest_store.interest_vector(db)
        watched = db.watched_ids()
        for row in db.query(
            "SELECT s.video_id, s.vector FROM scores s ORDER BY s.computed_at DESC LIMIT 3000"
        ):
            if row["video_id"] in watched:
                continue
            similarity = T.cosine(positive, json.loads(row["vector"] or "{}"))
            if similarity > 0.08:
                add(row["video_id"], "history", similarity)

    if "playlists" in enabled:
        for row in db.query("SELECT video_ids FROM playlists"):
            for vid in json.loads(row["video_ids"] or "[]")[:per_source]:
                add(vid, "playlists", 1.0)

    if "discovery" in enabled:
        for row in db.query(
            "SELECT id FROM videos WHERE views > 20000 ORDER BY published DESC LIMIT ?",
            (per_source,),
        ):
            add(row["id"], "discovery", 1.0)

    if "interests" in enabled:
        positive, _ = interest_store.interest_vector(db)
        tags = set(T.top_terms(positive, 18))
        if tags:
            for row in db.query("SELECT video_id, topics FROM scores LIMIT 6000"):
                topics = set(json.loads(row["topics"] or "[]"))
                overlap = len(topics & tags)
                if overlap:
                    add(row["video_id"], "interests", min(1.0, overlap / 3.0))

    if "niche" in enabled:
        for row in db.query(
            "SELECT v.id FROM videos v WHERE v.views < 20000 AND v.views > 200 "
            "ORDER BY v.published DESC LIMIT ?",
            (per_source,),
        ):
            add(row["id"], "niche", 1.0)

    return out


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def recommend(db: Database, settings: dict, *, limit: int | None = None,
              seed: int | None = None, community=None) -> Result:
    homepage = settings["homepage"]
    limit = limit or int(homepage.get("count", 36))
    rng = random.Random(seed if seed is not None else _seed_for(homepage))

    policy = channel_policy.load_policy(db, settings)
    positive, negative = interest_store.interest_vector(db)
    ranker = learner_mod.load(db)
    subscribed = {r["channel_id"] for r in db.query("SELECT channel_id FROM subscriptions")}
    watched = db.watched_ids()
    progress_map = {r["video_id"]: float(r["progress"]) for r in db.unfinished(limit=200)}
    blocked_terms = [r["value"].lower() for r in db.query("SELECT value FROM blocklist WHERE kind='term'")]
    blocked_videos = {r["value"] for r in db.query("SELECT value FROM blocklist WHERE kind='video'")}

    mode = homepage.get("mode", "blend")
    if mode == "playlist":
        pool = _playlist_pool(db, homepage.get("playlist_id", ""))
    elif mode == "continue":
        pool = {vid: {"continue": 1.0} for vid in progress_map}
    elif mode == "subscriptions":
        pool = gather_candidates(db, {**settings, "sources": {"subscriptions": 100}})
    else:
        pool = gather_candidates(db, settings)
        for vid in progress_map:
            pool.setdefault(vid, {})["continue"] = 1.0

    rejected: dict[str, int] = {}
    rejected_examples: dict[str, list[str]] = {}

    def reject(reason: str, title: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1
        rejected_examples.setdefault(reason, [])
        if len(rejected_examples[reason]) < 4:
            rejected_examples[reason].append(title)

    rows = _load_rows(db, list(pool))
    if community is not None:
        try:
            community.enrich(list(rows), settings)
        except Exception:
            pass
    branding = community.branding_map(list(rows)) if community and settings["dearrow"]["enabled"] else {}
    segments = community.segments_map(list(rows)) if community and settings["sponsorblock"]["enabled"] else {}

    rule_cfg = settings.get("rules", {})
    rule_expr = rule_cfg.get("expr") if rule_cfg.get("enabled") else None

    candidates: list[Candidate] = []
    now = time.time()
    for video_id, sources in pool.items():
        row = rows.get(video_id)
        if row is None:
            continue
        video = dict(row)
        card = row_to_card(row) if row.get("education") is not None else ScoreCard(video_id)
        channel = video.get("author_id", "")

        veto = policy.rejects(channel)
        if veto and settings["channels"].get("blocked_hidden", True):
            reject(veto, video["title"])
            continue

        if video_id in blocked_videos:
            reject("you hid this video", video["title"])
            continue

        exempt = channel in policy.exempt
        extra = {
            "sponsor": segments.get(video_id, {}),
            "dearrow": branding.get(video_id, {}),
        }
        if not exempt:
            reason = _gate(video, card, settings, extra, watched, blocked_terms,
                           keep_unfinished=video_id in progress_map)
            if reason:
                reject(reason, video["title"])
                continue
            if rule_expr is not None:
                record = _rule_record(video, card, extra, policy, subscribed, watched, sources)
                if not rule_engine.evaluate(rule_expr, record):
                    reject("blocked by your rules", video["title"])
                    continue

        candidate = Candidate(video=video, card=card, sources=dict(sources))
        candidate.progress = progress_map.get(video_id, 0.0)
        candidate.bucket = bucket_of(card, video)
        brand = branding.get(video_id) or {}
        if brand.get("title"):
            candidate.video["dearrow_title"] = brand["title"]
            candidate.video["original_title"] = video["title"]
        if brand.get("thumb_time") is not None:
            candidate.video["dearrow_thumb_time"] = brand["thumb_time"]
        if segments.get(video_id):
            candidate.video["sponsor"] = segments[video_id]
        _blend(candidate, settings, policy, positive, negative, ranker,
               subscribed, now, rng, segments.get(video_id, {}))
        candidates.append(candidate)

    candidates.sort(key=lambda c: -c.score)

    if homepage.get("shuffle"):
        rng.shuffle(candidates)
        selected = candidates[:limit]
    else:
        selected = _arrange(candidates, settings, limit, db)

    if homepage.get("continue_first") and mode == "blend":
        selected.sort(key=lambda c: (0 if c.progress > 0 else 1, -c.score))

    diagnostics = _diagnose(db, selected, candidates, settings, rejected, rejected_examples, ranker)
    _record_impressions(db, selected)
    return Result(items=selected, diagnostics=diagnostics)


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def _gate(video: Mapping[str, Any], card: ScoreCard, settings: dict,
          extra: Mapping[str, Any], watched: set[str], blocked_terms: Sequence[str],
          keep_unfinished: bool = False) -> str | None:
    f = settings["filters"]
    duration = int(video.get("duration") or 0)
    title = (video.get("title") or "").lower()

    if video.get("id") in watched and f.get("hide_watched", True) and not keep_unfinished:
        return "already watched"
    if f.get("hide_live") and video.get("is_live"):
        return "live stream"
    if f.get("hide_upcoming", True) and video.get("is_upcoming"):
        return "not published yet"
    if f.get("hide_shorts", True) and duration and duration <= int(f.get("shorts_seconds", 180)):
        return f"shorter than {f.get('shorts_seconds', 180)}s (Shorts filter)"
    if f.get("min_duration") and duration and duration < int(f["min_duration"]):
        return "shorter than your minimum duration"
    if f.get("max_duration") and duration and duration > int(f["max_duration"]):
        return "longer than your maximum duration"
    if f.get("min_views") and (video.get("views") or 0) < int(f["min_views"]):
        return "below your view floor"
    if f.get("max_views") and (video.get("views") or 0) > int(f["max_views"]):
        return "above your view ceiling"
    if f.get("max_age_days"):
        age_days = (time.time() - (video.get("published") or 0)) / 86400.0
        if video.get("published") and age_days > float(f["max_age_days"]):
            return "older than your age limit"
    if f.get("min_like_ratio"):
        views = max(1, video.get("views") or 1)
        if (video.get("likes") or 0) / views < float(f["min_like_ratio"]):
            return "like ratio below your floor"

    for key, score_key, direction in (
        ("max_brainrot", "brainrot", "max"), ("max_clickbait", "clickbait", "max"),
        ("max_nsfw", "nsfw", "max"), ("max_music", "music", "max"),
        ("max_ai_generated", "ai_generated", "max"), ("max_profanity", "profanity", "max"),
        ("min_education", "education", "min"), ("min_info_density", "info_density", "min"),
    ):
        threshold = f.get(key)
        if threshold is None:
            continue
        value = card[score_key]
        if direction == "max" and threshold < 100 and value > threshold:
            return f"{score_key.replace('_', ' ')} {value:.0f} is above your limit of {threshold}"
        if direction == "min" and threshold > 0 and value < threshold:
            return f"{score_key.replace('_', ' ')} {value:.0f} is below your floor of {threshold}"

    # Subscriber count. Invidious does not always populate it, and a video we
    # know nothing about should not be silently dropped by a filter that cannot
    # actually see it, so zero means "no data" and abstains.
    subs = int(video.get("sub_count") or 0)
    if subs > 0:
        if f.get("min_subs") and subs < int(f["min_subs"]):
            return "channel is smaller than your subscriber floor"
        if f.get("max_subs") and subs > int(f["max_subs"]):
            return "channel is bigger than your subscriber ceiling"

    # Language, inferred from the caption tracks the video carries. Same
    # abstention rule: no caption data means no opinion.
    wanted = [str(code).lower()[:2] for code in (f.get("languages") or [])]
    if wanted:
        available = [str(code).lower()[:2] for code in _json_list(video.get("caption_langs"))]
        if available and not set(available) & set(wanted):
            return f"no captions in {', '.join(wanted)}"

    for term in blocked_terms:
        if term and term in title:
            return f"title contains the blocked term “{term}”"

    sb = settings["sponsorblock"]
    seg = extra.get("sponsor") or {}
    if sb.get("enabled") and seg:
        if seg.get("sponsor_ratio", 0) > float(sb.get("max_sponsor_ratio", 1.0)):
            return f"{seg['sponsor_ratio'] * 100:.0f}% of the video is sponsor read"
        if seg.get("filler_ratio", 0) > float(sb.get("max_filler_ratio", 1.0)):
            return "too much filler according to SponsorBlock"
        if sb.get("hide_exclusive_access") and seg.get("has_exclusive_access"):
            return "SponsorBlock flags this as paid exclusive access"
    return None


def _rule_record(video: Mapping[str, Any], card: ScoreCard, extra: Mapping[str, Any],
                 policy: channel_policy.ChannelPolicy, subscribed: set[str],
                 watched: set[str], sources: Mapping[str, float]) -> dict[str, Any]:
    duration = int(video.get("duration") or 0)
    views = max(1, int(video.get("views") or 0))
    published = int(video.get("published") or 0)
    channel = video.get("author_id", "")
    seg = extra.get("sponsor") or {}
    record = dict(card.scores)
    record.update({
        "duration": duration,
        "duration_min": duration / 60.0,
        "views": views,
        "likes": video.get("likes") or 0,
        "like_ratio": (video.get("likes") or 0) / views,
        "subs": video.get("sub_count") or 0,
        "age_days": (time.time() - published) / 86400.0 if published else 9999.0,
        "published": published,
        "title": video.get("title") or "",
        "description": video.get("description") or "",
        "author": video.get("author") or "",
        "author_id": channel,
        "genre": video.get("genre") or "",
        "keywords": _json_list(video.get("keywords")),
        "topics": card.topics,
        "is_live": bool(video.get("is_live")),
        "is_upcoming": bool(video.get("is_upcoming")),
        "is_short": bool(duration and duration <= 180),
        "family_safe": bool(video.get("family_safe", 1)),
        "watched": video.get("id") in watched,
        "subscribed": channel in subscribed,
        "has_transcript": bool(video.get("transcript")),
        "has_chapters": bool(T.CHAPTER_RE.search(video.get("description") or "")),
        "sponsor_ratio": seg.get("sponsor_ratio", 0.0),
        "filler_ratio": seg.get("filler_ratio", 0.0),
        "selfpromo_ratio": seg.get("selfpromo_ratio", 0.0),
        "exclusive_access": bool(seg.get("has_exclusive_access")),
        "dearrow_retitled": bool((extra.get("dearrow") or {}).get("title")),
        "channel_priority": policy.priority.get(channel, 0),
        "channel_affinity": policy.affinity.get(channel, 0.0),
        "channel_quality": policy.quality.get(channel, 50.0),
        "channel_consistency": 0.0,
        "channel_allowed": channel in policy.allow,
        "channel_blocked": channel in policy.block,
        "source": max(sources.items(), key=lambda kv: kv[1])[0] if sources else "",
        "language": (_json_list(video.get("caption_langs")) or [""])[0],
    })
    return record


# --------------------------------------------------------------------------
# Blending
# --------------------------------------------------------------------------


def _blend(candidate: Candidate, settings: dict, policy: channel_policy.ChannelPolicy,
           positive: dict, negative: dict, ranker: learner_mod.Ranker,
           subscribed: set[str], now: float, rng: random.Random,
           segment: Mapping[str, Any]) -> None:
    weights = settings["weights"]
    sources = settings["sources"]
    video = candidate.video
    card = candidate.card
    channel = video.get("author_id", "")
    components = candidate.components

    # source affinity, normalised so the slider values read as percentages
    total_source_weight = sum(max(0, v) for v in sources.values()) or 1
    source_score = 0.0
    for source, strength in candidate.sources.items():
        share = (100.0 if source == "continue" else sources.get(source, 0)) / total_source_weight
        source_score += share * strength
    components["source"] = min(1.5, source_score) * weights["source"]

    # interest match
    similarity = T.cosine(positive, card.vector)
    dislike = T.cosine(negative, card.vector)
    components["interest"] = (similarity - 0.8 * dislike) * weights["interest"]

    # explicit score targets
    targets = settings["targets"]
    active = [(k, t) for k, t in targets.items() if t.get("enabled")]
    if active:
        total = 0.0
        weight_sum = 0.0
        for key, target in active:
            distance = abs(card[key] - float(target.get("target", 70))) / 100.0
            w = float(target.get("weight", 1.0))
            total += (1.0 - distance) * w
            weight_sum += w
        components["preference"] = (total / max(weight_sum, 1e-6)) * weights["preference"]

    # channel policy
    channel_score, channel_notes = policy.score(
        channel,
        float(settings["channels"].get("manual_strength", 0.18)),
        float(settings["channels"].get("affinity_strength", 0.5)),
        bool(settings["channels"].get("affinity_enabled", True)),
    )
    if channel in policy.block:  # kept but buried
        channel_score -= 5.0
        channel_notes.append({"label": "on your block list", "effect": -5.0, "kind": "listing"})
    components["channel"] = channel_score * weights.get("channel_priority", 1.0)
    candidate.notes.extend(channel_notes)

    # Channel quality is a separate axis from channel priority: priority is what
    # you asked for, quality is what the channel's own catalogue looks like
    # (education, density, topic consistency, minus bait). Centred on 50 so an
    # unscored channel contributes nothing either way.
    channel_quality = policy.quality.get(channel)
    if channel_quality is not None and weights.get("channel_quality"):
        components["channel_quality"] = (
            (channel_quality - 50.0) / 50.0 * weights["channel_quality"]
        )

    # learned model
    context = {
        "duration": video.get("duration", 0),
        "interest_match": similarity,
        "channel_affinity": policy.affinity.get(channel, 0.0),
        "channel_priority": policy.priority.get(channel, 0),
        "subscribed": channel in subscribed,
        "fresh": T.clamp(1.0 - (now - (video.get("published") or now)) / (21 * 86400)),
        "popular": T.clamp(math.log10((video.get("views") or 0) + 10) / 7.0),
        "obscure": T.clamp(1.0 - math.log10((video.get("views") or 0) + 10) / 6.0),
        "sponsor_ratio": segment.get("sponsor_ratio", 0.0),
    }
    vector = learner_mod.features_for(card, context)
    if ranker.trained and settings["learning"].get("enabled", True):
        components["learned"] = (ranker.predict(vector) - 0.5) * 2.0 * weights["learned"]
        for part in ranker.contributions(vector):
            candidate.notes.append({
                "label": f"learned from your feedback: {part['feature'].replace('_', ' ')}",
                "effect": part["effect"], "kind": "learned",
            })

    components["freshness"] = context["fresh"] * weights["freshness"]

    # novelty: reward distance from the current interest centroid
    novelty = float(settings.get("novelty", 30)) / 100.0
    if novelty > 0:
        distance = 1.0 - similarity
        components["novelty"] = (distance * novelty - 0.5 * novelty) * weights["novelty"]
        if novelty > 0.6:
            components["novelty"] += rng.uniform(0, novelty * 0.3)

    if candidate.progress > 0:
        components["continue"] = (0.4 + candidate.progress * 0.6) * weights["continue"]

    # soft quality penalties: below the hard filter but still unwanted
    penalty = 0.0
    f = settings["filters"]
    for key, limit_key in (("brainrot", "max_brainrot"), ("clickbait", "max_clickbait"),
                           ("music", "max_music"), ("ai_generated", "max_ai_generated")):
        ceiling = float(f.get(limit_key, 100))
        if ceiling < 100:
            headroom = max(0.0, card[key] - ceiling * 0.6) / 100.0
            penalty += headroom
    sb = settings["sponsorblock"]
    if sb.get("enabled") and sb.get("score_penalty"):
        penalty += segment.get("sponsor_ratio", 0.0) * float(sb["score_penalty"])
    components["penalty"] = -penalty * weights["penalty"]

    candidate.score = sum(components.values())


# --------------------------------------------------------------------------
# Arrangement
# --------------------------------------------------------------------------


def _arrange(candidates: list[Candidate], settings: dict, limit: int, db: Database) -> list[Candidate]:
    diversity = settings["diversity"]
    budget = settings["budget"]
    max_per_channel = int(diversity.get("max_per_channel", 3)) if diversity.get("enabled") else 999
    lam = float(diversity.get("mmr_lambda", 0.75)) if diversity.get("enabled") else 1.0

    quotas: dict[str, int] = {}
    if budget.get("enabled"):
        raw = budget.get("quotas", {})
        total = sum(max(0, v) for v in raw.values()) or 1
        quotas = {k: max(0, round(limit * v / total)) for k, v in raw.items()}

    caps = _remaining_daily_caps(db, budget) if budget.get("enabled") else {}

    selected: list[Candidate] = []
    channel_counts: dict[str, int] = {}
    bucket_counts: dict[str, int] = {}
    remaining = list(candidates)

    while remaining and len(selected) < limit:
        best = None
        best_value = -1e9
        for candidate in remaining:
            channel = candidate.channel_id
            if channel and channel_counts.get(channel, 0) >= max_per_channel:
                continue
            bucket = candidate.bucket
            if quotas and bucket in quotas and bucket_counts.get(bucket, 0) >= quotas[bucket]:
                continue
            if bucket in caps and bucket_counts.get(bucket, 0) >= caps[bucket]:
                continue
            value = candidate.score
            if lam < 1.0 and selected:
                overlap = max(T.cosine(candidate.card.vector, s.card.vector) for s in selected[-12:])
                value = lam * candidate.score - (1.0 - lam) * overlap * 3.0
            if value > best_value:
                best_value, best = value, candidate
        if best is None:
            # every remaining candidate is blocked by a cap; relax quotas first
            if quotas:
                quotas = {}
                continue
            if caps:
                caps = {}
                continue
            break
        selected.append(best)
        remaining.remove(best)
        channel_counts[best.channel_id] = channel_counts.get(best.channel_id, 0) + 1
        bucket_counts[best.bucket] = bucket_counts.get(best.bucket, 0) + 1
    return selected


def _remaining_daily_caps(db: Database, budget: dict) -> dict[str, int]:
    caps = budget.get("daily_caps", {}) or {}
    if not caps:
        return {}
    midnight = int(time.time()) - int(time.time()) % 86400
    used = {
        row["bucket"]: row["n"]
        for row in db.query(
            "SELECT bucket, COUNT(*) AS n FROM impressions WHERE shown_at >= ? GROUP BY bucket",
            (midnight,),
        )
    }
    return {bucket: max(0, int(cap) - used.get(bucket, 0)) for bucket, cap in caps.items()}


# --------------------------------------------------------------------------
# Explanation and diagnostics
# --------------------------------------------------------------------------


def explain(candidate: Candidate) -> list[dict]:
    """Turn the component breakdown into percentages that sum to exactly 100.

    The naive version — round each share independently — lands on 99 or 101 often
    enough to be visible as a gap or an overflow in the stacked bar, and a bar
    that claims to be a complete explanation should look like one. Largest
    remainder apportionment fixes the total without misreporting any component
    by more than a single point.
    """
    positives = {k: v for k, v in candidate.components.items() if v > 0.001}
    total = sum(positives.values())
    out: list[dict] = []

    if total > 0:
        ranked = sorted(positives.items(), key=lambda kv: -kv[1])[:6]
        kept_total = sum(value for _, value in ranked)
        shares = [(key, 100.0 * value / kept_total) for key, value in ranked]
        for key, percent in _apportion(shares):
            if percent < 1:
                continue
            out.append({
                "key": key,
                "label": _component_label(key, candidate),
                "percent": percent,
            })
        # Dropping sub-1% slivers leaves a gap; give it to the largest component
        # so the bar still reads as a whole.
        shortfall = 100 - sum(part["percent"] for part in out)
        if out and shortfall:
            out[0]["percent"] += shortfall

    for key, value in candidate.components.items():
        if value < -0.001:
            out.append({
                "key": key, "label": _component_label(key, candidate),
                "percent": round(100.0 * value / (total or 1)), "negative": True,
            })
    return out


def _apportion(shares: list[tuple[str, float]]) -> list[tuple[str, int]]:
    """Round a list of percentages to integers summing to 100."""
    floors = [(key, int(value), value - int(value)) for key, value in shares]
    remainder = 100 - sum(floor for _, floor, _ in floors)
    order = sorted(range(len(floors)), key=lambda i: -floors[i][2])
    bump = {order[i] for i in range(max(0, min(remainder, len(floors))))}
    return [(key, floor + (1 if i in bump else 0)) for i, (key, floor, _) in enumerate(floors)]


def _component_label(key: str, candidate: Candidate) -> str:
    if key == "source":
        best = max(candidate.sources.items(), key=lambda kv: kv[1])[0] if candidate.sources else ""
        return SOURCE_LABELS.get(best, "from your sources")
    return {
        "interest": "matches your interests",
        "preference": "matches your score targets",
        "channel": "channel priority and watch time",
        "channel_quality": "this channel scores well on your own criteria",
        "learned": "learned from your feedback",
        "freshness": "recently published",
        "novelty": "outside your usual topics",
        "continue": "unfinished",
        "penalty": "penalised for quality signals",
    }.get(key, key)


def _diagnose(db: Database, selected: list[Candidate], pool: list[Candidate],
              settings: dict, rejected: dict[str, int], examples: dict[str, list[str]],
              ranker: learner_mod.Ranker) -> dict[str, Any]:
    topic_counts: dict[str, int] = {}
    for candidate in selected:
        for topic in candidate.card.topics[:4]:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
    total = sum(topic_counts.values()) or 1
    shares = sorted(topic_counts.items(), key=lambda kv: -kv[1])

    # normalised entropy over topics, 1.0 = perfectly varied
    entropy = -sum((c / total) * math.log(c / total) for c in topic_counts.values() if c)
    max_entropy = math.log(len(topic_counts)) if len(topic_counts) > 1 else 1.0
    diversity_index = round(entropy / max_entropy, 3) if max_entropy else 0.0

    dominant = shares[0] if shares else ("", 0)
    dominant_share = round(dominant[1] / total, 3) if total else 0.0
    warn_below = float(settings["diversity"].get("warn_below", 0.35))
    rabbit_hole = None
    if settings["diversity"].get("enabled") and selected and (
        diversity_index < warn_below or dominant_share > 0.55
    ):
        rabbit_hole = {
            "topic": dominant[0],
            "share": round(dominant_share * 100),
            "diversity": round(diversity_index * 100),
        }

    buckets: dict[str, int] = {}
    for candidate in selected:
        buckets[candidate.bucket] = buckets.get(candidate.bucket, 0) + 1

    return {
        "pool": len(pool),
        "shown": len(selected),
        "rejected": sorted(
            ({"reason": reason, "count": count, "examples": examples.get(reason, [])}
             for reason, count in rejected.items()),
            key=lambda item: -item["count"],
        ),
        "rejected_total": sum(rejected.values()),
        "diversity": diversity_index,
        "dominant_topic": dominant[0],
        "dominant_share": dominant_share,
        "rabbit_hole": rabbit_hole,
        "buckets": buckets,
        "topics": shares[:12],
        "model": {"trained": ranker.trained, "samples": ranker.seen,
                  "preferences": ranker.top_preferences()},
    }


def _record_impressions(db: Database, selected: list[Candidate]) -> None:
    now = int(time.time())
    db.executemany(
        "INSERT INTO impressions(video_id, shown_at, slot, score, bucket, reason) VALUES(?,?,?,?,?,?)",
        [
            (c.id, now, index, round(c.score, 4), c.bucket, json.dumps(explain(c)))
            for index, c in enumerate(selected)
        ],
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load_rows(db: Database, ids: Sequence[str]) -> dict[str, dict]:
    """Load videos with their scorecards.

    `s.*` rather than a column list on purpose: an explicit list silently drops
    any score added later, and the symptom is a filter that does nothing with no
    error anywhere. The two tables share no column names, so the star is safe,
    and `test_every_score_survives_the_load_path` holds it to that.
    """
    out: dict[str, dict] = {}
    ids = list(ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for row in db.query(
            f"SELECT v.*, s.* FROM videos v "
            f"LEFT JOIN scores s ON s.video_id = v.id WHERE v.id IN ({marks})",
            chunk,
        ):
            record = dict(row)
            record["video_id"] = record.get("video_id") or record["id"]
            out[record["id"]] = record
    return out


def _playlist_pool(db: Database, playlist_id: str) -> dict[str, dict[str, float]]:
    if playlist_id:
        rows = db.query("SELECT video_ids FROM playlists WHERE id = ?", (playlist_id,))
    else:
        rows = db.query("SELECT video_ids FROM playlists")
    pool: dict[str, dict[str, float]] = {}
    for row in rows:
        for vid in json.loads(row["video_ids"] or "[]"):
            pool[vid] = {"playlists": 1.0}
    return pool


def _seed_for(homepage: Mapping[str, Any]) -> int:
    policy = homepage.get("refresh_seed", "daily")
    if policy == "session":
        return random.randrange(1 << 30)
    if policy == "fixed":
        return 1
    return int(time.time() // 86400)


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
