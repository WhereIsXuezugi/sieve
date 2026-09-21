"""The JSON API.

Everything the web interface can do is also here, so Sieve can be scripted,
driven from another front end, or queried by a shell one-liner. Endpoints call
the same functions as the web forms (`actions.py` and the domain modules), so
the two surfaces cannot drift.

Interactive reference: `/api/docs` on a running instance.
Written reference: `docs/api.md`.

Conventions
    GET     reads, never writes
    POST    performs an action or creates
    PUT     saves something under a name you chose
    DELETE  removes
    400     a well-formed request that cannot be carried out, with a reason
    404     the thing named does not exist
    502     the upstream Invidious instance did not answer
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from . import (
    actions,
    analytics,
    ingest,
    llm,
    profiles,
    ranking,
    scoring,
)
from . import (
    channels as channel_policy,
)
from . import (
    interests as interest_store,
)
from . import (
    learner as learner_mod,
)
from . import (
    rules as rule_engine,
)
from .config import SCORE_KEYS, resolve_settings
from .doctor import doctor_report
from .invidious import InvidiousUnavailable
from .present import candidate_json

router = APIRouter(prefix="/api", tags=["api"])


def _state(request: Request):
    return request.app.state


def _fail(exc: Exception) -> HTTPException:
    """Map a domain failure to the right status, keeping its message."""
    if isinstance(exc, InvidiousUnavailable):
        return HTTPException(502, f"Invidious did not answer: {exc}")
    return HTTPException(400, str(exc))


# --------------------------------------------------------------------------
# Request bodies. Declared so /api/docs documents them.
# --------------------------------------------------------------------------


class NameBody(BaseModel):
    name: str = ""


class TextBody(BaseModel):
    text: str = Field(..., description="The brief, in prose")


class BlockBody(BaseModel):
    kind: str = Field(..., description="'term' to block a title word, 'video' to hide a video")
    value: str
    note: str = ""


class ChannelListsBody(BaseModel):
    """Accepts exactly what `GET /api/channels/export` produces.

    Export writes `{"id", "name"}` objects so the file is readable in a git
    diff; import takes those or bare id strings. Anything else would make the
    export-then-import round trip fail the moment you had one allowed channel.
    """

    allow: list[str] = []
    block: list[str] = []
    priorities: dict[str, int] = {}

    @field_validator("allow", "block", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> list[str]:
        out = []
        for item in value or []:
            if isinstance(item, dict):
                item = item.get("id", "")
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out


class ScoreBody(BaseModel):
    all: bool = Field(False, description="Rescore everything, not just what is unscored")
    limit: int = Field(200, ge=1, le=5000)
    transcripts: bool = True


class ProfileSaveBody(BaseModel):
    author: str = ""


class FeedbackBody(BaseModel):
    video_id: str
    kind: str = Field(..., description="more, less, deeper, lighter, shorter, longer, "
                                       "higher_quality, or block_channel")
    note: str = ""


class ProgressBody(BaseModel):
    video_id: str
    progress: float = Field(..., ge=0, le=1, description="Fraction watched, 0 to 1")
    dwell: int = Field(0, description="Seconds on the page, if known")


class HideBody(BaseModel):
    kind: str = Field("video", description="'video' or 'term'")
    value: str
    note: str = ""


class ChannelBody(BaseModel):
    priority: int | None = Field(None, ge=-5, le=5)
    listing: str | None = Field(None, description="neutral, allow or block")
    exempt_filters: bool | None = Field(None, description="Only meaningful for allowed channels")
    note: str | None = None
    name: str | None = None


class InterestBody(BaseModel):
    action: str = Field("set", description="'set' or 'remove'")
    tag: str
    weight: float = Field(0.8, ge=-1, le=1)
    confidence: float = Field(1.0, ge=0, le=1)


class RuleBody(BaseModel):
    expr: dict | str = Field(..., description="A rule tree, or the same as a JSON string")
    enabled: bool = True


class ProfileImportBody(BaseModel):
    profile: dict = Field(..., description="A profile document, as exported")
    merge: bool = Field(True, description="False replaces your settings instead of merging")


class ProfileFetchBody(BaseModel):
    url: str
    merge: bool = True


def documented(model: type[BaseModel], example: dict) -> dict[str, Any]:
    """OpenAPI metadata describing a JSON body a handler reads itself.

    Several older endpoints parse `await request.json()` directly, with
    validation and error messages the tests already pin down. Converting them
    to typed parameters would change those behaviours; this documents the
    contract in /api/docs without touching them.
    """
    return {"requestBody": {"required": True, "content": {"application/json": {
        "schema": model.model_json_schema(), "example": example}}}}


# --------------------------------------------------------------------------
# Recommendations and videos
# --------------------------------------------------------------------------


@router.get("/recommendations", summary="The homepage, as data")
def recommendations(request: Request,
                    limit: int | None = Query(None, ge=1, le=500),
                    mood: str = "",
                    refresh: bool = Query(False, description="Reshuffle instead of today's order")):
    """The same ranking the homepage renders: items with their explanations,
    plus the diagnostics — pool size, rejection tally, diversity, rabbit-hole
    warning — that the readout strip and the debugger show."""
    state = _state(request)
    settings = actions.resolved_settings(state.db)
    if mood:
        settings = resolve_settings({**actions.stored_settings(state.db), "active_mood": mood})
    result = ranking.recommend(
        state.db, settings, limit=limit,
        seed=int(time.time()) if refresh else None, community=state.community,
    )
    return {
        "items": [candidate_json(state.cfg, c, settings, i) for i, c in enumerate(result.items)],
        "diagnostics": result.diagnostics,
        "mood": settings.get("active_mood", ""),
    }


@router.get("/videos/{video_id}", summary="One video, with its full score breakdown")
def video(request: Request, video_id: str):
    state = _state(request)
    row = state.ingestor.ensure_video(video_id)
    if row is None:
        raise HTTPException(404, "video not found")
    score_row = state.db.one("SELECT * FROM scores WHERE video_id = ?", (video_id,))
    card = scoring.row_to_card(score_row) if score_row else None
    impression = state.db.one(
        "SELECT reason, shown_at FROM impressions WHERE video_id = ? ORDER BY shown_at DESC LIMIT 1",
        (video_id,),
    )
    pref = state.db.one("SELECT * FROM channel_prefs WHERE channel_id = ?", (row["author_id"],))
    video_out = {k: v for k, v in row.items() if k != "transcript"}
    video_out["has_transcript"] = bool(row.get("transcript"))
    return {
        "video": video_out,
        "scores": card.scores if card else None,
        "breakdown": {key: card.explain(key, top=8) for key in SCORE_KEYS} if card else None,
        "topics": card.topics if card else [],
        "last_explanation": json.loads(impression["reason"]) if impression else None,
        "sponsorblock": state.community.segments_for(video_id),
        "channel_policy": dict(pref) if pref else None,
        "history": [dict(r) for r in state.db.query(
            "SELECT watched_at, progress FROM history WHERE video_id = ? ORDER BY watched_at DESC",
            (video_id,),
        )],
    }


# --------------------------------------------------------------------------
# Settings and moods
# --------------------------------------------------------------------------


@router.get("/settings", summary="Stored settings, and what they resolve to")
def get_settings(request: Request):
    """`stored` is what you have changed. `resolved` is what the ranking
    actually uses: defaults, then your changes, then the active mood."""
    db = _state(request).db
    return {"stored": actions.stored_settings(db), "resolved": actions.resolved_settings(db)}


@router.post("/settings/reset", summary="Reset every control to its default")
def reset_settings(request: Request):
    actions.reset_settings(_state(request).db)
    return {"ok": True}


@router.get("/moods", summary="Every mood, and which is active")
def get_moods(request: Request):
    return actions.list_moods(_state(request).db)


@router.post("/moods/active", summary="Switch mood; an empty name clears it")
def set_active_mood(request: Request, body: NameBody):
    try:
        return {"ok": True, "active": actions.activate_mood(_state(request).db, body.name)}
    except actions.ActionError as exc:
        raise _fail(exc) from None


@router.put("/moods/{name}", summary="Save the current settings as a mood")
def save_mood(request: Request, name: str):
    try:
        return {"ok": True, "name": name, "overrides": actions.save_mood(_state(request).db, name)}
    except actions.ActionError as exc:
        raise _fail(exc) from None


@router.delete("/moods/{name}", summary="Delete a mood, built-in ones included")
def delete_mood(request: Request, name: str):
    if not actions.delete_mood(_state(request).db, name):
        raise HTTPException(404, f"no mood called {name!r}")
    return {"ok": True}


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


@router.get("/channels", summary="Every channel: priority, listing, affinity, quality")
def get_channels(request: Request, q: str = "", limit: int = Query(300, ge=1, le=5000)):
    table = channel_policy.channel_table(_state(request).db, limit)
    if q:
        needle = q.lower()
        table = [c for c in table if needle in (c["name"] or "").lower() or needle in c["id"].lower()]
    return {"channels": table}


@router.get("/channels/export", summary="Allow list, block list and priorities")
def export_channels(request: Request):
    return channel_policy.export_lists(_state(request).db)


@router.post("/channels/import", summary="Merge channel lists into yours")
def import_channels(request: Request, body: ChannelListsBody):
    counts = channel_policy.import_lists(_state(request).db, body.allow, body.block, body.priorities)
    return {"ok": True, "applied": counts}


# --------------------------------------------------------------------------
# Interests, model, blocklist
# --------------------------------------------------------------------------


@router.get("/interests", summary="What the system thinks you like, with confidence")
def get_interests(request: Request, limit: int = Query(200, ge=1, le=2000)):
    return {"interests": interest_store.listing(_state(request).db, limit)}


@router.post("/interests/rederive", summary="Rebuild derived interests from watch history")
def rederive_interests(request: Request):
    db = _state(request).db
    return {"ok": True, "derived": interest_store.derive_from_history(db)}


@router.get("/model", summary="The learned ranker and its coefficients")
def get_model(request: Request):
    ranker = learner_mod.load(_state(request).db)
    return {
        "trained": ranker.trained,
        "samples": ranker.seen,
        "weights": dict(zip(learner_mod.FEATURES, (round(w, 4) for w in ranker.weights), strict=True)),
        "preferences": ranker.top_preferences(12),
    }


@router.get("/blocklist", summary="Hidden videos and blocked title terms")
def get_blocklist(request: Request, kind: str | None = None):
    try:
        return {"items": actions.list_blocklist(_state(request).db, kind)}
    except actions.ActionError as exc:
        raise _fail(exc) from None


@router.post("/blocklist", summary="Hide a video, or block every title containing a term")
def add_block(request: Request, body: BlockBody):
    try:
        return {"ok": True, **actions.add_block(_state(request).db, body.kind, body.value, body.note)}
    except actions.ActionError as exc:
        raise _fail(exc) from None


@router.delete("/blocklist/{kind}/{value}", summary="Unhide a video, or unblock a term")
def remove_block(request: Request, kind: str, value: str):
    if not actions.remove_block(_state(request).db, kind, value):
        raise HTTPException(404, "not on the blocklist")
    return {"ok": True}


# --------------------------------------------------------------------------
# Brief and rules
# --------------------------------------------------------------------------


def _compile(request: Request, text: str) -> dict:
    from . import textutil as T

    state = _state(request)
    positive, _ = interest_store.interest_vector(state.db)
    return llm.compile_brief(state.cfg, text, {"top_interests": T.top_terms(positive, 12)})


@router.post("/brief/compile", summary="Turn a written brief into a settings patch, without applying it")
def compile_brief(request: Request, body: TextBody):
    return _compile(request, body.text)


@router.post("/brief/apply", summary="Compile a brief and apply it")
def apply_brief(request: Request, body: TextBody):
    compiled = _compile(request, body.text)
    try:
        applied = actions.apply_brief(_state(request).db, body.text, compiled)
    except actions.ActionError as exc:
        raise _fail(exc) from None
    return {"ok": True, "summary": compiled.get("summary", ""), "applied": applied,
            "provider": compiled.get("provider")}


@router.get("/rules", summary="The active rule, and what it means in English")
def get_rules(request: Request):
    rules = actions.resolved_settings(_state(request).db)["rules"]
    return {"enabled": rules["enabled"], "expr": rules["expr"],
            "summary": rule_engine.describe(rules["expr"]),
            "fields": rule_engine.FIELDS, "operators": sorted(rule_engine.OPERATORS)}


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------


@router.get("/analytics", summary="Watch time, completion, channels, trends")
def get_analytics(request: Request, days: int = Query(30, ge=1, le=3650)):
    db = _state(request).db
    return {
        "dashboard": analytics.dashboard(db, days),
        "attention": analytics.attention(db),
        "trends": interest_store.timeseries(db),
        "rewatches": analytics.rewatches(db),
        "blind_spots": analytics.blind_spots(db),
    }


# --------------------------------------------------------------------------
# Playlists
# --------------------------------------------------------------------------


class PlaylistBody(BaseModel):
    playlist: str = Field("", description="A playlist id, or any URL containing list=")
    playlist_id: str = Field("", description="Accepted as an alias for `playlist`")


@router.post("/import/playlist", summary="Import or refresh a playlist, by id or URL")
def import_playlist(request: Request, body: PlaylistBody):
    """Importing a playlist that is already imported refreshes it. After that,
    every sync keeps it current, so videos you add later show up by themselves."""
    state = _state(request)
    try:
        playlist_id = actions.parse_playlist_ref(body.playlist or body.playlist_id)
        count = ingest.import_playlist(state.db, state.api, playlist_id)
    except (actions.ActionError, InvidiousUnavailable) as exc:
        raise _fail(exc) from None
    row = state.db.one("SELECT title FROM playlists WHERE id = ?", (playlist_id,))
    if count == 0:
        # Invidious answers a private or deleted playlist with an empty one
        # rather than an error. Say so, instead of reporting a happy zero.
        state.db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        raise HTTPException(
            400, "that playlist came back empty. It may be private, deleted, or "
                 "not visible to your Invidious instance; only public and unlisted "
                 "playlists can be imported")
    return {"ok": True, "id": playlist_id, "title": row["title"] if row else "", "imported": count}


@router.get("/playlists", summary="Imported playlists")
def get_playlists(request: Request):
    return {"playlists": actions.list_playlists(_state(request).db)}


@router.delete("/playlists/{playlist_id}", summary="Forget an imported playlist")
def delete_playlist(request: Request, playlist_id: str):
    if not actions.remove_playlist(_state(request).db, playlist_id):
        raise HTTPException(404, "no such playlist")
    return {"ok": True}


# --------------------------------------------------------------------------
# Saved profiles
# --------------------------------------------------------------------------


@router.get("/profiles", summary="Configurations saved on this machine")
def get_profiles(request: Request):
    return {"profiles": profiles.list_profiles(_state(request).db)}


@router.put("/profiles/{name}", summary="Save the current configuration under a name")
def save_profile(request: Request, name: str, body: ProfileSaveBody | None = None):
    db = _state(request).db
    author = body.author if body else ""
    profiles.save_profile(db, name, profiles.export_profile(db, name, author=author), author)
    return {"ok": True, "name": name}


@router.post("/profiles/{name}/load", summary="Replace the current configuration with a saved one")
def load_profile(request: Request, name: str):
    db = _state(request).db
    body = profiles.load_profile(db, name)
    if body is None:
        raise HTTPException(404, "no such profile")
    return {"ok": True, "applied": profiles.import_profile(db, body, merge=False)}


@router.delete("/profiles/{name}", summary="Delete a saved configuration")
def delete_profile(request: Request, name: str):
    if not actions.delete_saved_profile(_state(request).db, name):
        raise HTTPException(404, "no such profile")
    return {"ok": True}


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------


@router.post("/score", summary="Score unscored videos, or rescore everything")
def score(request: Request, body: ScoreBody | None = None):
    body = body or ScoreBody()
    ingestor = _state(request).ingestor
    if body.all:
        return {"ok": True, "scored": ingestor.rescore_all()}
    total = 0
    while total < body.limit:
        done = ingestor.score_pending(limit=min(50, body.limit - total),
                                      fetch_transcripts=body.transcripts)
        total += done
        if done == 0:
            break
    return {"ok": True, "scored": total}


@router.post("/maintenance/prune", summary="Drop stale rows and vacuum")
def prune(request: Request):
    return {"ok": True, **_state(request).db.prune()}


@router.get("/doctor", summary="Why is my homepage empty")
def doctor(request: Request, quick: bool = True):
    """With `quick=false` this also checks that the instance, the community
    APIs and any configured model endpoints answer, which takes a few seconds."""
    state = _state(request)
    return doctor_report(state.cfg, state.db, state.api, quick=quick)

