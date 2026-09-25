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
from . import langdetect
from . import learner as learner_mod
from . import rules as rule_engine
from . import textutil as T
from .config import SOURCE_KEYS
from .db import Database
from .scoring import ScoreCard, bucket_of, row_to_card

SOURCE_LABELS = {
    "sift": "from what you asked Sieve to sift",
    "subscriptions": "from a channel you subscribe to",
    "followed": "from a channel Sieve follows for you",
    "topics": "matches a topic you chose",
    "history": "similar to what you have watched",
    "playlists": "from one of your playlists",
    "discovery": "popular outside your usual topics",
    "interests": "matches an interest you set",
    "niche": "small channel in your topics",
    "continue": "you left this unfinished",
    "fill": "filling the page: little else matched",
    "next": "next episode of a series you're watching",
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
    # Set when the video is on the page only because the page would otherwise
    # be short: "catalogue" (it passes every filter but no source picked it),
    # or the filter it fails, when soft filters were relaxed to fill the page.
    filler: str = ""

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
    # Every candidate and what became of it, when asked for (ledger=True):
    # the whole funnel, not only the counts.
    ledger: list[dict[str, Any]] | None = None


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------


def gather_candidates(db: Database, settings: dict, pool_size: int = 600,
                      followed: bool = True) -> dict[str, dict[str, float]]:
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

    # A video whose upload date is unknown (some yt-dlp listings omit it)
    # counts as uploaded when Sieve first saw it, rather than never.
    when = "CASE WHEN v.published > 0 THEN v.published ELSE v.fetched_at END"

    if "subscriptions" in enabled:
        for row in db.query(
            f"SELECT v.id FROM videos v JOIN subscriptions s ON s.channel_id = v.author_id "
            f"WHERE {when} > ? ORDER BY {when} DESC LIMIT ?",
            (cutoff, per_source),
        ):
            add(row["id"], "subscriptions", 1.0)
        # Found by searching your topics and phrases: a source of its own,
        # under the interests slider. Without it, a niche video with few views
        # reached the page only if the rest of the catalogue ran out.
        if followed and "interests" in enabled:
            for row in db.query(
                f"SELECT v.id FROM videos v WHERE v.origin = 'search' AND {when} > ? "
                f"ORDER BY {when} DESC LIMIT ?", (int(time.time()) - 365 * 86400, per_source),
            ):
                add(row["id"], "topics", 0.9)
        # Channels Sieve keeps up with on your behalf (Controls, Finding
        # videos): the algorithm's own subscriptions, weighted a little below
        # yours, under the same slider.
        if followed:
            for row in db.query(
                f"SELECT v.id FROM videos v JOIN channel_fetches f ON f.channel_id = v.author_id "
                f"WHERE f.origin IN ('followed', 'starter') AND f.failures = 0 "
                f"AND v.author_id NOT IN (SELECT channel_id FROM subscriptions) "
                f"AND {when} > ? ORDER BY {when} DESC LIMIT ?",
                (cutoff, per_source),
            ):
                add(row["id"], "followed", 0.8)

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
            "SELECT id FROM videos v WHERE views > 20000 "
            "ORDER BY CASE WHEN v.published > 0 THEN v.published ELSE v.fetched_at END DESC LIMIT ?",
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
            "ORDER BY CASE WHEN v.published > 0 THEN v.published ELSE v.fetched_at END DESC LIMIT ?",
            (per_source,),
        ):
            add(row["id"], "niche", 1.0)

    return out


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def recommend(db: Database, settings: dict, *, limit: int | None = None,
              seed: int | None = None, community=None,
              pool: dict[str, dict[str, float]] | None = None,
              record: bool = True, ledger: bool = False,
              restrict_to: set[str] | None = None) -> Result:
    """Rank a pool of videos with the user's whole configuration.

    `pool` ranks candidates handed in from outside — a search, a channel, a
    playlist — instead of the homepage's own sources; that is how Sift applies
    the algorithm to anything. `record=False` keeps such a pass out of the
    impression history. `ledger=True` returns every candidate and what became
    of it, for the Funnel page.
    """
    homepage = settings["homepage"]
    limit = max(1, int(limit or homepage.get("count", 36)))
    rng = random.Random(seed if seed is not None else _seed_for(homepage))
    # A pool handed in from outside (Sift) is ranked as given: no shuffle, no
    # filling from the catalogue. Decided before `pool` is reassigned below —
    # testing `pool is None` afterwards is why shuffle used to never happen.
    external = pool is not None

    policy = channel_policy.load_policy(db, settings)
    positive, negative = interest_store.interest_vector(db)
    ranker = learner_mod.load(db)
    subscribed = {r["channel_id"] for r in db.query("SELECT channel_id FROM subscriptions")}
    watched = db.watched_ids()
    opened = db.opened_ids()
    unfinished_rows = db.unfinished(limit=200)   # most recently watched first
    progress_map = {r["video_id"]: float(r["progress"]) for r in unfinished_rows}
    # What you are actually in the middle of: watched in the last 30 days.
    # Older half-watched videos are abandoned, not in progress.
    in_progress = [r["video_id"] for r in unfinished_rows
                   if r["watched_at"] and time.time() - r["watched_at"] < 30 * 86400]
    blocked_terms = [r["value"].lower() for r in db.query("SELECT value FROM blocklist WHERE kind='term'")]
    blocked_videos = {r["value"] for r in db.query("SELECT value FROM blocklist WHERE kind='video'")}

    compute = settings.get("compute", {})
    pool_size = int(compute.get("pool_size", 600))
    mode = homepage.get("mode", "blend") if not external else "sift"
    if external:
        pass
    elif mode == "playlist":
        pool = _playlist_pool(db, homepage.get("playlist_id", ""))
    elif mode == "continue":
        pool = {vid: {"continue": 1.0} for vid in progress_map}
    elif mode == "subscriptions":
        # "Subscriptions only" means yours, not the channels Sieve follows.
        pool = gather_candidates(db, {**settings, "sources": {"subscriptions": 100}}, pool_size,
                                 followed=False)
    else:
        pool = gather_candidates(db, settings, pool_size)
        for vid in progress_map:
            pool.setdefault(vid, {})["continue"] = 1.0
    # "New videos every X": between refreshes only what was already shown.
    if restrict_to is not None and not external:
        pool = {vid: src for vid, src in pool.items() if vid in restrict_to}
    # The next episode of a series you are watching (series.py).
    next_up: dict[str, str] = {}
    if mode == "blend" and homepage.get("next_episode", True):
        from . import series

        next_up = series.next_episodes(db)
        for vid in next_up:
            if restrict_to is None or vid in restrict_to:
                pool.setdefault(vid, {})["next"] = 1.0

    rejected: dict[str, int] = {}
    rejected_examples: dict[str, list[str]] = {}

    entries: list[dict[str, Any]] = []

    def reject(reason: str, video: Mapping[str, Any]) -> None:
        title = video.get("title", "") or video.get("id", "")
        rejected[reason] = rejected.get(reason, 0) + 1
        rejected_examples.setdefault(reason, [])
        if len(rejected_examples[reason]) < 4:
            rejected_examples[reason].append(title)
        if ledger:
            entries.append(_entry(video, "rejected", reason))

    rows = _load_rows(db, list(pool))
    if community is not None:
        try:
            community.enrich(list(rows), settings)
        except Exception:
            pass
    branding = community.branding_map(list(rows)) if community and settings["dearrow"]["enabled"] else {}
    segments = community.segments_map(list(rows)) if community and settings["sponsorblock"]["enabled"] else {}

    feedback_rows = db.query(
        "SELECT video_id, kind FROM feedback WHERE kind IN ('more', 'less') ORDER BY created_at")
    latest_feedback = {r["video_id"]: r["kind"] for r in feedback_rows}
    rated = set(latest_feedback)
    rated_less = {v for v, k in latest_feedback.items() if k == "less"}
    boost_strength = (float(homepage.get("recent_strength", 1.0))
                      if homepage.get("recent_boost", True) and not external else 0.0)
    boost_half_life = max(1.0, float(homepage.get("recent_half_life", 24)))
    wanted_langs = set(settings["filters"].get("video_languages") or [])
    lang_mode = settings["filters"].get("video_language_mode", "prefer")
    rule_cfg = settings.get("rules", {})
    rule_expr = rule_cfg.get("expr") if rule_cfg.get("enabled") else None
    now = time.time()
    relaxed_settings = _relaxed(settings)

    def consider(video_id: str, sources: dict[str, float], row: dict | None,
                 relaxed: bool = False) -> tuple[Candidate | None, str]:
        """Gate and blend one video. Returns the candidate, or why not.

        `relaxed` gates with the soft filters and the rule switched off; the
        hard checks — channel listings, hidden videos, blocked terms, what you
        watched, the nudity limit — still apply."""
        if row is None:
            return None, "not in the catalogue yet"
        video = dict(row)
        card = row_to_card(row) if row.get("education") is not None else ScoreCard(video_id)
        channel = video.get("author_id", "")

        veto = policy.rejects(channel)
        if veto and settings["channels"].get("blocked_hidden", True):
            return None, veto
        if video_id in blocked_videos:
            return None, "you hid this video"

        exempt = channel in policy.exempt
        extra = {
            "sponsor": segments.get(video_id, {}),
            "dearrow": branding.get(video_id, {}),
        }
        reason = _gate(video, card, relaxed_settings if relaxed else settings, extra, watched,
                       blocked_terms, keep_unfinished=video_id in progress_map, exempt=exempt,
                       opened=opened)
        if reason:
            return None, reason
        if lang_mode == "only" and wanted_langs and not exempt:
            found = langdetect.detect(f"{video.get('title', '')} {(video.get('description') or '')[:300]}")
            if found and found not in wanted_langs and not relaxed:
                return None, f"in {langdetect.LANGUAGES.get(found, found)}, not your languages"
        if not relaxed and not exempt and rule_expr is not None:
            record = _rule_record(video, card, extra, policy, subscribed, watched, sources)
            if not rule_engine.evaluate(rule_expr, record):
                return None, "blocked by your rules"

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
        components = candidate.components
        # Your More / Less on a video teaches Sieve about *similar* videos;
        # the rated video itself is neither pushed up nor buried by it.
        if video_id in rated and components.get("learned"):
            candidate.score -= components["learned"]
            components["learned"] = 0.0
        # Recently fetched: a temporary lift that halves every half-life,
        # kept out of the video's scores. Not for videos you said Less to.
        if boost_strength and video.get("first_seen") and video_id not in rated_less:
            age_hours = max(0.0, (now - video["first_seen"]) / 3600)
            lift = boost_strength * 0.5 ** (age_hours / boost_half_life)
            if lift > 0.01:
                components["recent"] = round(lift, 4)
                candidate.score += lift
        # Video language: "prefer" ranks other languages lower.
        if lang_mode == "prefer" and wanted_langs:
            found = langdetect.detect(f"{video.get('title', '')} {(video.get('description') or '')[:300]}")
            if found and found not in wanted_langs:
                components["language"] = -0.6
                candidate.score -= 0.6
        return candidate, ""

    candidates: list[Candidate] = []
    soft_rejects: dict[str, str] = {}   # video id -> the soft reason it failed
    for video_id, sources in pool.items():
        row = rows.get(video_id)
        candidate, reason = consider(video_id, sources, row)
        if candidate is None:
            reject(reason, row or {"id": video_id, "title": video_id})
            if row is not None:
                soft_rejects[video_id] = reason
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda c: -c.score)

    passed_over: dict[str, str] = {}
    # Up to a quarter of the page for videos you are in the middle of,
    # most recent first, whatever the per-channel cap says.
    # And the next episodes of series you are in, right after.
    pinned: list[Candidate] = []
    by_id = {c.id: c for c in candidates}
    if homepage.get("continue_first") and mode == "blend":
        pinned += [by_id[v] for v in in_progress if v in by_id]
    pinned += [by_id[v] for v in next_up if v in by_id and by_id[v] not in pinned]
    pinned = pinned[:max(1, limit // 4)]
    selected, passed_over = _arrange(candidates, settings, limit, db, pinned)

    # -- fill: never leave the page short ------------------------------------
    fill_mode = "off" if external or restrict_to is not None else homepage.get("fill", "relaxed")
    fillers: list[Candidate] = []
    if fill_mode in ("catalogue", "relaxed") and len(selected) < limit:
        chosen = {c.id for c in selected}
        # First, what the page's own caps (per channel, quotas) left off.
        extras = [c for c in candidates if c.id not in chosen]
        for c in extras:
            c.filler = "catalogue"
        # Then the rest of the catalogue, newest first, with every filter.
        outside = [r["id"] for r in db.query(
            "SELECT id FROM videos ORDER BY CASE WHEN published > 0 THEN published "
            "ELSE fetched_at END DESC LIMIT ?", (pool_size,))
            if r["id"] not in pool]
        outside_rows = _load_rows(db, outside)
        for video_id in outside:
            candidate, reason = consider(video_id, {"fill": 1.0}, outside_rows.get(video_id))
            if candidate is not None:
                candidate.filler = "catalogue"
                extras.append(candidate)
            elif outside_rows.get(video_id) is not None:
                soft_rejects[video_id] = reason
                rows.setdefault(video_id, outside_rows[video_id])
        extras.sort(key=lambda c: -c.score)
        fillers = _top_up(selected, extras, limit, settings, past_cap=False, db=db)
        if fill_mode == "relaxed":
            fillers += _top_up(selected + fillers, extras, limit, settings, past_cap=True, db=db)
        # Last, relax the soft filters, and say which one each video fails.
        if fill_mode == "relaxed" and len(selected) + len(fillers) < limit:
            taken = chosen | {c.id for c in fillers}
            relaxed: list[Candidate] = []
            for video_id, reason in soft_rejects.items():
                if video_id in taken:
                    continue
                candidate, _ = consider(video_id, pool.get(video_id) or {"fill": 1.0},
                                        rows.get(video_id), relaxed=True)
                if candidate is not None:
                    candidate.filler = reason
                    relaxed.append(candidate)
            relaxed.sort(key=lambda c: -c.score)
            fillers += _top_up(selected + fillers, relaxed, limit, settings, past_cap=True, db=db)
        selected = selected + fillers

    # (Unfinished videos are already first: `_arrange` pins them, most
    # recently watched first, and fillers only ever follow.)

    diagnostics = _diagnose(db, selected, candidates, settings, rejected, rejected_examples, ranker)
    diagnostics["filled"] = len(fillers)
    diagnostics["filled_relaxed"] = sum(1 for c in fillers if c.filler not in ("", "catalogue"))
    diagnostics["fill"] = fill_mode
    # A page shorter than asked for says why, instead of looking broken.
    diagnostics["short_by"] = max(0, limit - len(selected))
    diagnostics["short_reason"] = ""
    if diagnostics["short_by"] and not external:
        shown_ids = {c.id for c in selected}
        left = [_limit_phrase(passed_over.get(c.id, "")) for c in candidates if c.id not in shown_ids]
        left = [r for r in left if r]
        if left:
            diagnostics["short_reason"] = max(set(left), key=left.count)
        elif not candidates:
            diagnostics["short_reason"] = "nothing else passes your filters"
        else:
            diagnostics["short_reason"] = "the catalogue has nothing more to offer yet"
    if record:
        _record_impressions(db, selected)
    if ledger:
        shown = {c.id: slot for slot, c in enumerate(selected)}
        # A video rejected by a filter and then shown to fill the page is
        # listed once, as shown, with the filter it fails as its reason.
        entries = [e for e in entries if e["id"] not in shown]
        for candidate in selected:
            entries.append(_entry(candidate.video, "shown", _filler_reason(candidate),
                                  candidate, shown[candidate.id]))
        for candidate in candidates:
            if candidate.id not in shown:
                entries.append(_entry(candidate.video, "ranked", passed_over.get(
                    candidate.id, "below the cut"), candidate))
        order = {"shown": 0, "ranked": 1, "rejected": 2}
        entries.sort(key=lambda e: (order[e["stage"]], e["slot"] if e["slot"] is not None else 0,
                                    -(e["score"] or 0)))
    return Result(items=selected, diagnostics=diagnostics, ledger=entries if ledger else None)


def _limit_phrase(reason: str) -> str:
    """"3 from this channel already on the page" -> "your limit of 3 videos
    per channel", for the homepage's short-page note."""
    import re

    if m := re.match(r"(\d+) from this channel already", reason):
        return f"max {m.group(1)} videos per channel"
    if m := re.match(r"the (\w+) quota is full", reason):
        return f"your {m.group(1)} quota"
    if m := re.match(r"today's (\w+) cap is used up", reason):
        return f"today's {m.group(1)} cap"
    return ""


def _filler_reason(candidate: Candidate) -> str:
    if not candidate.filler:
        return ""
    if candidate.filler == "catalogue":
        return "filled in: passes your filters, though no source picked it"
    return f"filled in despite: {candidate.filler}"


def _relaxed(settings: dict) -> dict:
    """The settings with every soft filter opened up, for filling a short page.

    Kept: the nudity ceiling, hiding what you watched and unreleased
    premieres, blocked terms and channel listings (those are checked outside
    the filters). Everything else — duration, views, age, score cutoffs,
    Shorts, languages, sponsor load — is what a filler may fail."""
    import copy

    s = copy.deepcopy(settings)
    f = s["filters"]
    for key in list(f):
        if key in ("max_duration", "max_views", "max_subs", "max_age_days"):
            f[key] = 0
        elif key.startswith("max_") and key != "max_nsfw":
            f[key] = 100
        elif key.startswith("min_"):
            f[key] = 0
    f.update({"hide_shorts": False, "hide_live": False, "languages": []})
    sb = s["sponsorblock"]
    sb.update({"max_sponsor_ratio": 1.0, "max_filler_ratio": 1.0, "hide_exclusive_access": False})
    return s


def _top_up(selected: list[Candidate], extras: list[Candidate], limit: int,
            settings: dict, past_cap: bool, db: Database) -> list[Candidate]:
    """Add the best extras until the page is full, within every page limit;
    with `past_cap` (relaxed fill), then past them, since you asked for a
    full page over a varied or balanced one."""
    limits = _PageLimits(settings, limit, db)
    for c in selected:
        limits.take(c)
    taken = {c.id for c in selected}
    added: list[Candidate] = []
    for respect in ((True, False) if past_cap else (True,)):
        for candidate in extras:
            if len(selected) + len(added) >= limit:
                return added
            if candidate.id in taken or (respect and limits.refusal(candidate)):
                continue
            added.append(candidate)
            taken.add(candidate.id)
            limits.take(candidate)
    return added


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def _gate(video: Mapping[str, Any], card: ScoreCard, settings: dict,
          extra: Mapping[str, Any], watched: set[str], blocked_terms: Sequence[str],
          keep_unfinished: bool = False, exempt: bool = False,
          opened: set[str] | None = None) -> str | None:
    """Return why a video must not be shown, or None.

    Two tiers. The hard checks are facts about you rather than preferences
    about content — you already watched it, it is not out yet, you blocked a
    word in its title — and they apply to every channel. The soft checks are
    the quality and shape filters, which an allowed channel marked *exempt*
    skips: that is what exempting a favourite is for.
    """
    f = settings["filters"]
    duration = int(video.get("duration") or 0)
    title = (video.get("title") or "").lower()

    # -- hard: always applied -----------------------------------------------
    if video.get("id") in watched and f.get("hide_watched", True) and not keep_unfinished:
        return "already watched"
    if opened and video.get("id") in opened and f.get("hide_watched", True) and not keep_unfinished:
        return "you already opened this"
    if f.get("hide_live") and video.get("is_live"):
        return "live stream"
    if f.get("hide_upcoming", True) and video.get("is_upcoming"):
        return "not published yet"
    for term in blocked_terms:
        if term and term in title:
            return f"title contains the blocked term “{term}”"
    if exempt:
        return None

    # -- soft: skipped for exempt channels ----------------------------------
    # A Short is known either from its length or, for videos from YouTube's
    # feeds (which carry no duration), from its /shorts/ link.
    if f.get("hide_shorts", True) and video.get("is_short"):
        return "a YouTube Short (Shorts filter)"
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
    # No like count (YouTube's feeds and search listings often omit it) means
    # no data, and a filter that cannot see its data abstains.
    if (f.get("min_like_ratio") and video.get("likes") and video.get("views")
            and video["likes"] / video["views"] < float(f["min_like_ratio"])):
        return "like ratio below your floor"

    # Not scored yet (it arrived seconds ago; the worker scores it shortly):
    # the score cutoffs cannot see anything, so they abstain — except the
    # nudity limit, which falls back to YouTube's own family-safe flag rather
    # than going blind.
    if (not card.scores and int(f.get("max_nsfw", 100)) < 100
            and not video.get("family_safe", 1)):
        return "not family-safe according to YouTube (not scored yet)"
    for key, score_key, direction in () if not card.scores else (
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
        "is_short": bool(video.get("is_short") or (duration and duration <= 180)),
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
        weight = (100.0 if source in ("continue", "next") else
                  sources.get("subscriptions", 0) if source == "followed" else
                  sources.get("interests", 0) if source == "topics" else sources.get(source, 0))
        share = weight / total_source_weight
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
        # Unknown upload date: neither fresh nor stale.
        "fresh": T.clamp(1.0 - (now - video["published"]) / (21 * 86400))
                 if video.get("published") else 0.5,
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


class _PageLimits:
    """The page's limits in one place: the per-channel cap, composition
    quotas and today's remaining daily caps. `_arrange` and the fill both ask
    it, so the fill cannot quietly break a limit the ranking kept."""

    def __init__(self, settings: dict, limit: int, db: Database):
        diversity, budget = settings["diversity"], settings["budget"]
        self.max_per_channel = (int(diversity.get("max_per_channel", 3))
                                if diversity.get("enabled") else 10**6)
        self.quotas: dict[str, int] = {}
        if budget.get("enabled"):
            raw = budget.get("quotas", {})
            total = sum(max(0, v) for v in raw.values()) or 1
            self.quotas = {k: max(0, round(limit * v / total)) for k, v in raw.items()}
        self.caps = _remaining_daily_caps(db, budget) if budget.get("enabled") else {}
        self.channels: dict[str, int] = {}
        self.buckets: dict[str, int] = {}

    def refusal(self, candidate: Candidate) -> str:
        channel, bucket = candidate.channel_id, candidate.bucket
        if channel and self.channels.get(channel, 0) >= self.max_per_channel:
            return f"{self.max_per_channel} from this channel already on the page"
        if bucket in self.quotas and self.buckets.get(bucket, 0) >= self.quotas[bucket]:
            return f"the {bucket} quota is full"
        if bucket in self.caps and self.buckets.get(bucket, 0) >= self.caps[bucket]:
            return f"today's {bucket} cap is used up"
        return ""

    def take(self, candidate: Candidate) -> None:
        self.channels[candidate.channel_id] = self.channels.get(candidate.channel_id, 0) + 1
        self.buckets[candidate.bucket] = self.buckets.get(candidate.bucket, 0) + 1


def _arrange(candidates: list[Candidate], settings: dict, limit: int,
             db: Database, pinned: Sequence[Candidate] = ()) -> tuple[list[Candidate], dict[str, str]]:
    """Pick the page. Returns the selection and, for everything left out, why:
    a limit it hit, or simply that the page filled before its turn.

    `pinned` goes on first, ahead of every limit: unfinished videos when
    "put unfinished videos first" is on. Sorting them to the top after the
    page was picked meant one could be dropped by the per-channel cap.

    Limits are not relaxed here, however short that leaves the page: whether
    to go past them is the fill setting's decision (see `recommend`). This
    used to drop the quotas silently whenever the per-channel cap blocked
    everything else, so "memes: 1%" could still show six."""
    diversity = settings["diversity"]
    lam = float(diversity.get("mmr_lambda", 0.75)) if diversity.get("enabled") else 1.0
    limits = _PageLimits(settings, limit, db)
    window = max(1, int(settings.get("compute", {}).get("mmr_window", 12)))
    reasons: dict[str, str] = {}
    selected: list[Candidate] = []
    for candidate in pinned[:limit]:
        selected.append(candidate)
        limits.take(candidate)
    taken = {c.id for c in selected}
    remaining = [c for c in candidates if c.id not in taken]

    while remaining and len(selected) < limit:
        best = None
        best_value = -1e9
        for candidate in remaining:
            refusal = limits.refusal(candidate)
            if refusal:
                reasons[candidate.id] = refusal
                continue
            value = candidate.score
            if lam < 1.0 and selected:
                overlap = max(T.cosine(candidate.card.vector, s.card.vector) for s in selected[-window:])
                value = lam * candidate.score - (1.0 - lam) * overlap * 3.0
            if value > best_value:
                best_value, best = value, candidate
        if best is None:
            break
        selected.append(best)
        remaining.remove(best)
        limits.take(best)
        reasons.pop(best.id, None)
    for rank, candidate in enumerate(remaining, start=len(selected) + 1):
        reasons.setdefault(candidate.id, f"below the cut: the page holds {limit}, this ranked #{rank}")
    return selected, reasons


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
        # A next episode is here because it is next: say that first. (An
        # unfinished video already has its own "unfinished" part.)
        for reason in ("next",):
            if reason in candidate.sources:
                for part in out:
                    if part["key"] == "source":
                        part["label"] = SOURCE_LABELS[reason]
                        out.remove(part)
                        out.insert(0, part)
                        break
                else:
                    out.insert(0, {"key": "source", "label": SOURCE_LABELS[reason], "percent": 0})
                break

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
        "recent": "recently fetched",
        "language": "not in your chosen languages",
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
    # "Narrowing": one topic takes over the page, or topics are few. Only
    # meaningful when enough videos on the page have topics at all; before,
    # a page without topic data read as 0% diversity and warned about a
    # blank topic ("0% is about ' '").
    with_topics = sum(topic_counts.values()) if topic_counts else 0
    rabbit_hole = None
    if (settings["diversity"].get("enabled") and len(selected) >= 6 and dominant[0]
            and with_topics >= max(4, len(selected) // 2)
            and (diversity_index < warn_below or dominant_share > 0.55)):
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


def _entry(video: Mapping[str, Any], stage: str, reason: str,
           candidate: Candidate | None = None, slot: int | None = None) -> dict[str, Any]:
    """One row of the funnel: a candidate and what became of it."""
    card = candidate.card if candidate else None
    return {
        "id": video.get("id", ""),
        "title": video.get("title", "") or video.get("id", ""),
        "author": video.get("author", ""),
        "author_id": video.get("author_id", ""),
        "duration": int(video.get("duration") or 0),
        "stage": stage,
        "reason": reason,
        "slot": slot,
        "score": round(candidate.score, 4) if candidate else None,
        "sources": sorted(candidate.sources) if candidate else [],
        "bucket": candidate.bucket if candidate else None,
        "scores": {k: round(card.scores.get(k, 0)) for k in ("education", "brainrot", "clickbait")}
        if card else None,
        "explanation": explain(candidate) if candidate else [],
    }


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


# --------------------------------------------------------------------------
# The homepage, with "new videos every X"
# --------------------------------------------------------------------------

NEW_EVERY_CHOICES = [(0, "any time"), (60, "every hour"), (180, "every 3 hours"),
                     (360, "every 6 hours"), (720, "every 12 hours"), (1440, "once a day"),
                     (4320, "every 3 days"), (10080, "once a week"), (20160, "every 2 weeks"),
                     (43200, "every 30 days")]


def homepage(db: Database, settings: dict, *, community=None, seed: int | None = None,
             record: bool = True, limit: int | None = None, mood: bool = False) -> Result:
    """What the homepage shows — the page and the API both call this.

    With `homepage.new_every` set, the page remembers the videos it showed.
    Until that much time has passed it draws only from them: videos you watch
    or filter out leave, and nothing new arrives. A way to limit how much you
    watch. A mood, or a change to the interval, does not open the gate early;
    the time does."""
    every = int(settings["homepage"].get("new_every", 0) or 0)
    if not every or mood:
        return recommend(db, settings, community=community, seed=seed, record=record, limit=limit)
    now = int(time.time())
    snap = db.get_setting("homepage_snapshot", {}) or {}
    if snap.get("ids") and now - int(snap.get("at", 0)) < every * 60:
        result = recommend(db, settings, community=community, seed=seed, record=record,
                           limit=limit, restrict_to=set(snap["ids"]))
        result.diagnostics["new_videos_at"] = int(snap["at"]) + every * 60
        return result
    result = recommend(db, settings, community=community, seed=seed, record=record, limit=limit)
    if result.items:
        db.set_setting("homepage_snapshot", {"at": now, "ids": [c.id for c in result.items]})
        result.diagnostics["new_videos_at"] = now + every * 60
    return result
