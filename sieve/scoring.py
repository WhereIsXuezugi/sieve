"""Content scoring.

Every score here is a logistic over a *linear* combination of named features.
That is a deliberate modelling choice rather than a shortcut: because the model
is linear, each feature's contribution to the final number is exact and free to
compute, so the debugger can say "this scored 82 on education, 31 points of
which came from citation links in the description" without SHAP, LIME, or a
second inference pass. A gradient-boosted ensemble would score marginally better
and cost an approximation layer plus a training pipeline to explain itself.

Where a mature project already answers a question better than a heuristic can,
we defer to it instead of guessing:

* clickbait      -> DeArrow's crowd-voted title corrections, when available
* sponsor load   -> SponsorBlock segments, when available
* transcript     -> Invidious caption tracks, not local Whisper

The heuristics below are the fallback for videos no one has annotated yet, and
the online learner in ``learner.py`` corrects their bias against your own
behaviour over time.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import textutil as T

SCORER_VERSION = 5   # 5: channel prior, your corrections and overrides


@dataclass
class Signal:
    name: str
    value: float          # normalised feature value, 0..1
    weight: float         # model coefficient
    label: str = ""

    @property
    def contribution(self) -> float:
        return self.value * self.weight


@dataclass
class ScoreCard:
    video_id: str
    scores: dict[str, float] = field(default_factory=dict)
    signals: dict[str, list[Signal]] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    vector: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5
    # Scores before the channel prior and your corrections (see ingest).
    base: dict[str, float] = field(default_factory=dict)

    def __getitem__(self, key: str) -> float:
        return self.scores.get(key, 50.0)

    def explain(self, key: str, top: int = 4) -> list[dict]:
        """Human-readable breakdown of one score, strongest influences first."""
        signals = self.signals.get(key, [])
        ranked = sorted(signals, key=lambda s: -abs(s.contribution))[:top]
        return [
            {
                "label": s.label or s.name.replace("_", " "),
                "effect": round(s.contribution * 25, 1),
                "direction": "up" if s.contribution >= 0 else "down",
            }
            for s in ranked
            if abs(s.contribution) > 0.02
        ]


def _score(signals: list[Signal], bias: float = 0.0) -> float:
    total = bias + sum(s.contribution for s in signals)
    return round(100.0 * T.sigmoid(total), 1)


# --------------------------------------------------------------------------
# Feature extraction
# --------------------------------------------------------------------------


def extract_features(video: Mapping[str, Any], transcript: str = "",
                     extra: Mapping[str, Any] | None = None) -> dict[str, float]:
    extra = extra or {}
    title = video["title"] or ""
    description = video["description"] or ""
    keywords = video["keywords"]
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except json.JSONDecodeError:
            keywords = []
    keyword_text = " ".join(keywords or [])
    duration = int(video["duration"] or 0)
    views = int(video["views"] or 0)
    likes = int(video["likes"] or 0)
    published = int(video["published"] or 0)
    genre = (video["genre"] or "").lower()

    title_tokens = T.content_tokens(title)
    body_tokens = T.content_tokens(f"{title} {keyword_text} {description}")
    transcript_tokens = T.content_tokens(transcript) if transcript else []
    all_tokens = body_tokens + transcript_tokens

    age_days = max(0.0, (time.time() - published) / 86400.0) if published else 999.0
    wpm = T.words_per_minute(transcript, duration)

    f: dict[str, float] = {
        # shape
        "very_short": T.clamp(1.0 - duration / 90.0) if duration else 0.0,
        "short": T.clamp(1.0 - duration / 300.0) if duration else 0.0,
        "long": T.clamp((duration - 900) / 2700.0),
        "very_long": T.clamp((duration - 3600) / 5400.0),
        # title surface
        "caps": T.clamp(T.caps_ratio(title) * 1.6),
        "emoji": T.clamp(T.emoji_count(title) / 3.0),
        "punct": T.punctuation_heat(title),
        "title_len": T.clamp(len(title) / 90.0),
        # lexicons
        "technical": T.lexicon_ratio(all_tokens, T.TECHNICAL),
        "academic": T.lexicon_ratio(all_tokens, T.ACADEMIC),
        "meme": T.lexicon_ratio(body_tokens, T.MEME),
        "clickbait_words": T.lexicon_ratio(title_tokens, T.CLICKBAIT),
        "music_words": T.lexicon_ratio(title_tokens + T.content_tokens(keyword_text), T.MUSIC_MARKERS),
        "nsfw_words": T.lexicon_ratio(title_tokens + body_tokens, T.NSFW_MARKERS),
        "ai_words": T.lexicon_ratio(body_tokens, T.AI_MARKERS),
        "entertainment_words": T.lexicon_ratio(body_tokens, T.ENTERTAINMENT),
        "hobby_words": T.lexicon_ratio(body_tokens, T.HOBBY),
        # structure
        "citations": T.clamp(len(T.CITATION_RE.findall(description)) / 2.0),
        "chapters": T.clamp(len(T.CHAPTER_RE.findall(description)) / 6.0),
        "sponsor_text": 1.0 if T.SPONSOR_RE.search(description) else 0.0,
        "desc_len": T.clamp(len(description) / 1200.0),
        # transcript
        "has_transcript": 1.0 if transcript_tokens else 0.0,
        "wpm_high": T.clamp((wpm - 110) / 90.0) if transcript_tokens else 0.0,
        "wpm_low": T.clamp((80 - wpm) / 80.0) if transcript_tokens else 0.0,
        "lexical_variety": T.unique_ratio(transcript_tokens) if transcript_tokens else 0.5,
        "transcript_technical": T.lexicon_ratio(transcript_tokens, T.TECHNICAL) if transcript_tokens else 0.0,
        # engagement
        "like_ratio": T.clamp(likes / max(1.0, views) * 40.0),
        "velocity": T.clamp(math.log10(views + 10) / 7.0 * (1.0 if age_days < 14 else 0.4)),
        "obscure": T.clamp(1.0 - math.log10(views + 10) / 6.0),
        "fresh": T.clamp(1.0 - age_days / 21.0),
        # flags
        "flag_music_genre": 1.0 if genre == "music" else 0.0,
        "flag_gaming_genre": 1.0 if genre == "gaming" else 0.0,
        "flag_education_genre": 1.0 if genre in {"education", "science & technology"} else 0.0,
        "flag_topic_channel": 1.0 if (video["author"] or "").endswith(" - Topic") else 0.0,
        "flag_not_family_safe": 0.0 if video["family_safe"] else 1.0,
        "flag_shorts_tag": 1.0 if "#shorts" in f"{title} {description}".lower() else 0.0,
    }

    f["profanity_density"] = T.lexicon_density(all_tokens, T.PROFANITY)

    # Signals contributed by external open-data projects, when present.
    f["dearrow_retitled"] = float(extra.get("dearrow_retitled", 0.0))
    f["sponsor_ratio"] = T.clamp(float(extra.get("sponsor_ratio", 0.0)) * 4.0)
    f["filler_ratio"] = T.clamp(float(extra.get("filler_ratio", 0.0)) * 4.0)
    return f


# --------------------------------------------------------------------------
# Scorers
# --------------------------------------------------------------------------

# name -> (bias, {feature: weight})
MODELS: dict[str, tuple[float, dict[str, float]]] = {
    "education": (-1.4, {
        "academic": 2.6, "technical": 1.5, "citations": 1.3, "chapters": 1.0,
        "long": 0.8, "has_transcript": 0.4, "flag_education_genre": 1.1,
        "wpm_high": 0.3, "meme": -1.8, "clickbait_words": -0.9, "very_short": -1.6,
        "flag_music_genre": -1.4, "entertainment_words": -0.8,
    }),
    "entertainment": (-0.9, {
        "entertainment_words": 2.4, "flag_gaming_genre": 1.3, "meme": 1.0,
        "hobby_words": 0.5, "like_ratio": 0.6, "velocity": 0.4,
        "academic": -1.2, "citations": -0.8, "very_long": -0.4,
    }),
    "stimulation": (-1.1, {
        "technical": 1.6, "transcript_technical": 1.1, "lexical_variety": 1.2,
        "chapters": 0.7, "citations": 0.6, "hobby_words": 0.7, "academic": 0.8,
        "meme": -1.5, "wpm_low": -0.6, "flag_music_genre": -0.9, "very_short": -1.0,
    }),
    "brainrot": (-1.6, {
        "meme": 3.0, "very_short": 1.8, "flag_shorts_tag": 1.6, "emoji": 1.0,
        "caps": 0.9, "clickbait_words": 1.0, "punct": 0.5, "filler_ratio": 0.6,
        "academic": -1.8, "technical": -1.4, "citations": -1.2, "long": -1.0,
        "chapters": -0.7, "lexical_variety": -0.8,
    }),
    "clickbait": (-1.5, {
        "clickbait_words": 2.8, "caps": 1.6, "emoji": 1.1, "punct": 0.9,
        "dearrow_retitled": 2.2, "velocity": 0.5, "title_len": 0.4,
        "academic": -1.0, "citations": -0.8, "chapters": -0.6,
    }),
    "info_density": (-1.2, {
        "wpm_high": 1.6, "technical": 1.4, "transcript_technical": 1.3,
        "lexical_variety": 1.2, "citations": 0.9, "chapters": 0.8, "academic": 0.9,
        "wpm_low": -1.4, "flag_music_genre": -1.6, "meme": -1.5, "filler_ratio": -0.9,
    }),
    "technical_depth": (-1.8, {
        "technical": 3.0, "transcript_technical": 2.0, "academic": 1.0,
        "citations": 0.8, "long": 0.5, "meme": -1.5, "entertainment_words": -0.8,
    }),
    "production": (-0.6, {
        "desc_len": 0.9, "chapters": 0.8, "sponsor_text": 0.7, "like_ratio": 0.5,
        "velocity": 0.6, "obscure": -1.1, "caps": -0.4,
    }),
    "ai_generated": (-2.2, {
        "ai_words": 3.2, "wpm_low": 0.5, "obscure": 0.6, "desc_len": -0.3,
        "has_transcript": -0.2, "lexical_variety": -0.9,
    }),
    "nsfw": (-3.0, {
        "nsfw_words": 4.0, "flag_not_family_safe": 2.2, "caps": 0.3,
        "academic": -1.0, "technical": -0.8,
    }),
    # Profanity is reported, not judged: the spec asks for "presence of
    # profanity" as a filterable fact, so this is close to a passthrough of the
    # measurement rather than a model with an opinion.
    "profanity": (-2.4, {
        "profanity_density": 6.0, "meme": 0.6, "caps": 0.3,
        "academic": -1.2, "flag_education_genre": -0.8,
    }),
    "music": (-2.6, {
        "flag_music_genre": 3.2, "music_words": 2.6, "flag_topic_channel": 2.4,
        "wpm_low": 0.6, "has_transcript": -0.5, "technical": -1.0, "academic": -0.8,
    }),
}

LABELS = {
    "academic": "lecture and course vocabulary",
    "technical": "technical vocabulary",
    "transcript_technical": "technical vocabulary in transcript",
    "citations": "links to papers or source code",
    "chapters": "timestamped chapters",
    "meme": "meme vocabulary",
    "clickbait_words": "sensational title wording",
    "caps": "shouting in the title",
    "emoji": "emoji in the title",
    "punct": "exclamation marks",
    "very_short": "under 90 seconds",
    "short": "short runtime",
    "long": "long runtime",
    "very_long": "feature length",
    "flag_shorts_tag": "tagged as a Short",
    "flag_music_genre": "filed under Music",
    "flag_gaming_genre": "filed under Gaming",
    "flag_education_genre": "filed under Education",
    "flag_topic_channel": "auto-generated topic channel",
    "flag_not_family_safe": "marked not family safe",
    "wpm_high": "dense narration",
    "wpm_low": "sparse narration",
    "lexical_variety": "varied vocabulary",
    "like_ratio": "strong like ratio",
    "velocity": "spreading fast",
    "obscure": "few views",
    "dearrow_retitled": "title rewritten by DeArrow contributors",
    "sponsor_ratio": "sponsor segments",
    "filler_ratio": "tangent and filler segments",
    "has_transcript": "captions available",
    "desc_len": "detailed description",
    "sponsor_text": "sponsor wording in description",
    "nsfw_words": "adult wording",
    "profanity_density": "swearing",
    "music_words": "music release wording",
    "ai_words": "declares AI generation",
    "entertainment_words": "entertainment vocabulary",
    "hobby_words": "hands-on hobby vocabulary",
    "channel_prior": "this channel's other videos",
    "title_len": "long title",
    "fresh": "just published",
    "your_corrections": "learned from scores you corrected",
    "your_override": "you set this score",
}

# How far a channel's usual score pulls one video, in log-odds. A channel whose
# other videos average 80 lifts a sparse video by up to ~1.1: enough to decide
# a video whose own text says little, not enough to outvote one that says a lot.
CHANNEL_PRIOR_WEIGHT = 1.1
# A channel's average counts once it has this many scored videos.
CHANNEL_PRIOR_MIN = 3

TOPIC_LEXICONS = {
    "education": T.ACADEMIC,
    "technical": T.TECHNICAL,
    "meme": T.MEME,
    "music": T.MUSIC_MARKERS,
    "entertainment": T.ENTERTAINMENT,
    "hobby": T.HOBBY,
}


def score_video(video: Mapping[str, Any], transcript: str = "",
                extra: Mapping[str, Any] | None = None, embedder=None) -> ScoreCard:
    """Score one video.

    `embedder` is optional: pass one from `embed.Embedder` to use a real
    embedding model for the similarity vector. Omit it and the default hashed
    term vector is used, which is what every benchmark in the README assumes.
    """
    extra = extra or {}
    features = extract_features(video, transcript, extra)
    card = ScoreCard(video_id=video["id"])
    channel_means = extra.get("channel_means") or {}
    correction_model = extra.get("correction_model") or {}
    overrides = extra.get("overrides") or {}
    correction_input = None

    for name, (bias, weights) in MODELS.items():
        signals = [
            Signal(feature, features.get(feature, 0.0), weight, LABELS.get(feature, ""))
            for feature, weight in weights.items()
            if abs(features.get(feature, 0.0)) > 1e-6
        ]
        base = _score(signals, bias)
        if name == "clickbait" and features.get("dearrow_retitled"):
            # A crowd-corrected title is direct evidence: let it dominate.
            base = max(base, 72.0)
        card.base[name] = base
        # The channel's usual score on this axis, as one explainable signal.
        mean = channel_means.get(name)
        if mean is not None:
            signals.append(Signal("channel_prior", round((mean - 50.0) / 50.0, 4),
                                  CHANNEL_PRIOR_WEIGHT, LABELS["channel_prior"]))
        # What your own corrections taught it, as one more signal.
        if correction_model.get(name):
            from . import corrections

            if correction_input is None:
                correction_input = corrections.inputs(video, features)
            delta = corrections.predict(correction_model[name], correction_input)
            if abs(delta) > 0.01:
                signals.append(Signal("your_corrections", 1.0, round(delta, 3),
                                      LABELS["your_corrections"]))
        added = sum(s.contribution for s in signals if s.name in ("channel_prior", "your_corrections"))
        card.scores[name] = round(100.0 * T.sigmoid(T.logit(base / 100.0) + added), 1) if added else base
        # Your own value for this video wins outright.
        if name in overrides:
            # Keep what the engine predicted beside your value, so the page
            # can show both; your value is the score used.
            predicted = card.scores[name]
            card.scores[name] = float(overrides[name])
            signals.append(Signal("your_override", round(predicted / 100.0, 4), 0.0,
                                  LABELS["your_override"]))
        card.signals[name] = signals

    keywords = video["keywords"]
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except json.JSONDecodeError:
            keywords = []
    parts = [
        (video["title"] or "", 3.0),
        (" ".join(keywords or []), 2.0),
        (video["author"] or "", 1.0),
        ((video["description"] or "")[:1500], 1.0),
        (transcript[:6000], 0.6),
    ]
    card.vector = embedder.vector(parts) if embedder is not None else T.term_vector(parts)
    card.topics = T.top_terms(card.vector, 10)
    card.confidence = 0.45 + (0.35 if transcript else 0.0) + (0.2 if card.vector else 0.0)
    return card


def bucket_of(card: ScoreCard, video: Mapping[str, Any]) -> str:
    """Which composition bucket a video counts against in the budget."""
    if card["music"] >= 65:
        return "music"
    if card["brainrot"] >= 65 or (card["education"] < 30 and card["entertainment"] < 40):
        return "meme"
    candidates = {
        "education": card["education"],
        "entertainment": card["entertainment"],
        "hobby": 100.0 if card.signals and any(
            s.name == "hobby_words" and s.value > 0.3 for s in card.signals.get("entertainment", [])
        ) else 0.0,
    }
    best = max(candidates.items(), key=lambda kv: kv[1])
    return best[0] if best[1] >= 45 else "other"


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def card_to_row(card: ScoreCard) -> tuple:
    signals = {
        key: [[s.name, round(s.value, 4), round(s.weight, 3)] for s in sigs]
        for key, sigs in card.signals.items()
    }
    return (
        card.video_id, SCORER_VERSION,
        card.scores.get("education", 50), card.scores.get("entertainment", 50),
        card.scores.get("stimulation", 50), card.scores.get("brainrot", 50),
        card.scores.get("clickbait", 50), card.scores.get("info_density", 50),
        card.scores.get("technical_depth", 50), card.scores.get("production", 50),
        card.scores.get("ai_generated", 50), card.scores.get("nsfw", 0),
        card.scores.get("music", 0), card.scores.get("profanity", 0),
        json.dumps(card.topics), json.dumps({k: round(v, 5) for k, v in card.vector.items()}),
        json.dumps(signals), int(time.time()), json.dumps(card.base),
    )


INSERT_SCORE = (
    "INSERT INTO scores(video_id, version, education, entertainment, stimulation, brainrot, "
    "clickbait, info_density, technical_depth, production, ai_generated, nsfw, music, profanity, "
    "topics, vector, signals, computed_at, base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(video_id) DO UPDATE SET version=excluded.version, education=excluded.education, "
    "entertainment=excluded.entertainment, stimulation=excluded.stimulation, "
    "brainrot=excluded.brainrot, clickbait=excluded.clickbait, info_density=excluded.info_density, "
    "technical_depth=excluded.technical_depth, production=excluded.production, "
    "ai_generated=excluded.ai_generated, nsfw=excluded.nsfw, music=excluded.music, "
    "profanity=excluded.profanity, "
    "topics=excluded.topics, vector=excluded.vector, signals=excluded.signals, "
    "computed_at=excluded.computed_at, base=excluded.base"
)


def _has(row: Mapping[str, Any], key: str) -> bool:
    """True when a row actually carries a column, with a value.

    `sqlite3.Row` has no `.get`, and a row written before a migration lacks the
    column outright, so this keeps `row_to_card` working across both shapes.
    """
    try:
        return row[key] is not None
    except (IndexError, KeyError):
        return False


def row_to_card(row: Mapping[str, Any]) -> ScoreCard:
    card = ScoreCard(video_id=row["video_id"])
    for key in ("education", "entertainment", "stimulation", "brainrot", "clickbait",
                "info_density", "technical_depth", "production", "ai_generated", "nsfw",
                "music", "profanity"):
        # `profanity` arrived after the first release; a row written before the
        # migration simply has no opinion about it.
        card.scores[key] = float(row[key]) if _has(row, key) else 0.0
    card.topics = json.loads(row["topics"] or "[]")
    card.vector = json.loads(row["vector"] or "{}")
    raw_signals = json.loads(row["signals"] or "{}")
    card.signals = {
        key: [Signal(name, value, weight, LABELS.get(name, "")) for name, value, weight in sigs]
        for key, sigs in raw_signals.items()
    }
    return card
