"""The web application.

Server-rendered HTML with a small amount of vanilla JavaScript. No build step,
no bundler, no client framework: the whole UI is delivered as HTML and ~12 KB of
CSS, which matters when the target is a box that also has to run the scorer.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import actions, analytics, ingest, llm, profiles, ranking, scoring
from . import channels as channel_policy
from . import interests as interest_store
from . import learner as learner_mod
from . import rules as rule_engine
from .api import (
    ChannelBody,
    FeedbackBody,
    HideBody,
    InterestBody,
    ProfileFetchBody,
    ProfileImportBody,
    ProgressBody,
    RuleBody,
    documented,
)
from .api import router as api_router
from .community import CommunityData
from .config import (
    BUCKETS,
    SCORE_KEYS,
    SOURCE_KEYS,
    Config,
    deep_merge,
    default_settings,
    drop_deleted_moods,
    resolve_settings,
)
from .db import Database
from .doctor import doctor_report
from .invidious import Invidious
from .present import display_title as _display_title
from .present import thumb_url as _thumb

log = logging.getLogger("sieve.app")
HERE = Path(__file__).parent


def create_app(cfg: Config | None = None, start_worker: bool = True) -> FastAPI:
    cfg = cfg or Config.load()
    db = Database(cfg.db_path)
    api = Invidious(cfg, db)
    community = CommunityData(cfg, db)
    ingestor = ingest.Ingestor(cfg, db, api, community)

    app = FastAPI(title="Sieve", docs_url="/api/docs", redoc_url=None)
    app.state.cfg = cfg
    app.state.db = db
    app.state.api = api
    app.state.community = community
    app.state.ingestor = ingestor

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(api_router)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["duration"] = _fmt_duration
    templates.env.filters["count"] = _fmt_count
    templates.env.filters["ago"] = _fmt_ago

    def settings() -> dict:
        return resolve_settings(db.get_setting("settings", {}))

    def stored_settings() -> dict:
        return drop_deleted_moods(deep_merge(default_settings(), db.get_setting("settings", {}) or {}))

    def save_settings(patch: dict) -> dict:
        current = db.get_setting("settings", {}) or {}
        merged = deep_merge(current, patch)
        db.set_setting("settings", merged)
        return merged

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        base = {
            "request": request,
            "cfg": cfg,
            "settings": settings(),
            "stored": stored_settings(),
            "nav": _nav(request.url.path),
            "watch_base": cfg.watch_base,
            "stats": db.stats(),
        }
        base.update(context)
        return templates.TemplateResponse(request, name, base)

    app.state.settings = settings

    # ----------------------------------------------------------------- home

    @app.get("/", response_class=HTMLResponse, tags=["pages"])
    def home(request: Request, mood: str = "", refresh: int = 0):
        active = settings()
        if mood:
            active = resolve_settings({**(db.get_setting("settings", {}) or {}), "active_mood": mood})
        result = ranking.recommend(
            db, active,
            seed=int(time.time()) if refresh else None,
            community=community,
        )
        items = [
            {
                "c": candidate,
                "video": candidate.video,
                "explanation": ranking.explain(candidate),
                "display_title": _display_title(candidate, active),
                "thumb": _thumb(cfg, candidate, active),
            }
            for candidate in result.items
        ]
        empty_hint = _empty_hint(db, result)
        return page(
            request, "home.html",
            items=items, diagnostics=result.diagnostics, empty_hint=empty_hint,
            playlists=db.query("SELECT id, title FROM playlists"),
            moods=list(active.get("moods", {})),
        )

    @app.get("/video/{video_id}", response_class=HTMLResponse, tags=["pages"])
    def video_detail(request: Request, video_id: str):
        row = ingestor.ensure_video(video_id)
        if row is None:
            raise HTTPException(404, "video not found")
        score_row = db.one("SELECT * FROM scores WHERE video_id = ?", (video_id,))
        card = scoring.row_to_card(score_row) if score_row else None
        impression = db.one(
            "SELECT reason, shown_at FROM impressions WHERE video_id = ? ORDER BY shown_at DESC LIMIT 1",
            (video_id,),
        )
        segments = community.segments_for(video_id)
        pref = db.one("SELECT * FROM channel_prefs WHERE channel_id = ?", (row["author_id"],))
        return page(
            request, "video.html",
            video=row, card=card,
            breakdown={key: card.explain(key) for key in SCORE_KEYS} if card else {},
            explanation=json.loads(impression["reason"]) if impression else [],
            segments=segments,
            channel_pref=dict(pref) if pref else None,
            history=db.query(
                "SELECT watched_at, progress FROM history WHERE video_id = ? ORDER BY watched_at DESC",
                (video_id,),
            ),
        )

    # ------------------------------------------------------------- feedback

    @app.post("/api/feedback", summary="Tell the ranker what you thought of a video", tags=["api"],
              openapi_extra=documented(FeedbackBody, {"video_id": "dQw4w9WgXcQ", "kind": "more"}))
    async def feedback(request: Request):
        body = await request.json()
        video_id = body.get("video_id", "")
        kind = body.get("kind", "")
        if kind not in learner_mod.FEEDBACK_TARGETS:
            raise HTTPException(400, f"unknown feedback kind {kind!r}")
        db.execute(
            "INSERT INTO feedback(video_id, kind, note, created_at) VALUES(?,?,?,?)",
            (video_id, kind, str(body.get("note", ""))[:400], int(time.time())),
        )

        row = db.one(
            "SELECT v.duration, v.author_id, v.views, s.* FROM videos v "
            "JOIN scores s ON s.video_id = v.id WHERE v.id = ?",
            (video_id,),
        )
        applied = {}
        if row is not None:
            card = scoring.row_to_card(row)
            ranker = learner_mod.load(db)
            affinity = db.scalar("SELECT affinity FROM channels WHERE id = ?", (row["author_id"],), 0.0)
            priority = db.scalar(
                "SELECT priority FROM channel_prefs WHERE channel_id = ?", (row["author_id"],), 0
            )
            vector = learner_mod.features_for(card, {
                "duration": row["duration"], "channel_affinity": affinity,
                "channel_priority": priority,
            })
            cfg_learn = settings()["learning"]
            for _ in range(3):
                ranker.observe(vector, learner_mod.FEEDBACK_TARGETS[kind],
                               float(cfg_learn.get("rate", 0.08)), float(cfg_learn.get("l2", 0.001)))
            learner_mod.save(db, ranker)

            # Structured feedback also edits the profile directly, because
            # "same topic but shorter" is a settings change, not just a label.
            applied = _apply_structured_feedback(db, kind, card, row, save_settings)

        if kind == "block_channel" and row is not None and row["author_id"]:
            channel_policy.set_preference(db, row["author_id"], listing="block")
            applied["channel"] = "blocked"
        return {"ok": True, "applied": applied}

    @app.post("/api/progress", summary="Report how much of a video was watched", tags=["api"],
              openapi_extra=documented(ProgressBody, {"video_id": "dQw4w9WgXcQ", "progress": 0.82}))
    async def progress(request: Request):
        body = await request.json()
        db.record_watch(
            body.get("video_id", ""),
            float(body.get("progress", 0)),
            int(body.get("dwell", 0)),
            origin="player",
        )
        return {"ok": True}

    @app.post("/api/hide", summary="Hide a video (same as POST /api/blocklist)", tags=["api"],
              openapi_extra=documented(HideBody, {"kind": "video", "value": "dQw4w9WgXcQ"}))
    async def hide(request: Request):
        body = await request.json()
        try:
            return {"ok": True, **actions.add_block(
                db, body.get("kind", "video"), body.get("value", ""), body.get("note", ""))}
        except actions.ActionError as exc:
            raise HTTPException(400, str(exc)) from None

    # ------------------------------------------------------------- settings

    @app.get("/settings", response_class=HTMLResponse, tags=["pages"])
    def settings_page(request: Request):
        return page(
            request, "settings.html",
            score_keys=SCORE_KEYS, source_keys=SOURCE_KEYS, buckets=BUCKETS,
            playlists=db.query("SELECT id, title FROM playlists"),
            playlist_rows=actions.list_playlists(db),
            saved=profiles.list_profiles(db),
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        patch = _form_to_patch(form)
        save_settings(patch)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/api/settings", summary="Merge a settings patch into yours", tags=["api"],
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                  "schema": {"type": "object"},
                  "example": {"novelty": 60, "filters": {"hide_shorts": True, "max_brainrot": 25}}}}}})
    async def settings_api(request: Request):
        body = await request.json()
        merged = save_settings(profiles.sanitise_settings(body))
        return {"ok": True, "settings": merged}

    @app.post("/settings/reset")
    def settings_reset():
        actions.reset_settings(db)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/mood")
    async def set_mood(mood: str = Form("")):
        try:
            actions.activate_mood(db, mood)
        except actions.ActionError as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------- channels

    @app.get("/channels", response_class=HTMLResponse, tags=["pages"])
    def channels_page(request: Request, q: str = ""):
        table = channel_policy.channel_table(db)
        if q:
            needle = q.lower()
            table = [c for c in table if needle in (c["name"] or "").lower()
                     or needle in c["id"].lower()]
        return page(request, "channels.html", channels=table, q=q)

    @app.post("/api/channels/quality", summary="Rescore channels from their own catalogues", tags=["api"])
    def channel_quality():
        return {"ok": True, "channels": channel_policy.recompute_quality(db)}

    @app.post("/api/channels/recompute", summary="Recompute channel affinity from watch time", tags=["api"])
    def channel_recompute():
        updated = channel_policy.recompute_affinity(
            db, float(settings()["channels"].get("affinity_half_life_days", 45))
        )
        return {"ok": True, "channels": updated}

    @app.post("/api/channels/{channel_id}", summary="Set priority, listing, exemption or note for a channel", tags=["api"],
              openapi_extra=documented(ChannelBody, {"priority": 4, "listing": "allow", "note": "the good lectures"}))
    async def channel_update(channel_id: str, request: Request):
        body = await request.json()
        try:
            result = channel_policy.set_preference(
                db, channel_id,
                name=body.get("name"),
                priority=body.get("priority"),
                listing=body.get("listing"),
                exempt_filters=body.get("exempt_filters"),
                note=body.get("note"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, "channel": result}

    @app.delete("/api/channels/{channel_id}", summary="Forget everything you set for a channel", tags=["api"])
    def channel_clear(channel_id: str):
        channel_policy.clear_preference(db, channel_id)
        return {"ok": True}

    # ------------------------------------------------------------- debugger

    @app.get("/debugger", response_class=HTMLResponse, tags=["pages"])
    def debugger(request: Request):
        active = settings()
        result = ranking.recommend(db, active, community=community)
        ranker = learner_mod.load(db)
        return page(
            request, "debugger.html",
            diagnostics=result.diagnostics,
            interests=interest_store.listing(db),
            ranker=ranker,
            rule_summary=rule_engine.describe(active["rules"]["expr"]) if active["rules"]["enabled"] else "",
            blocked_terms=actions.list_blocklist(db, "term"),
            hidden_videos=actions.list_blocklist(db, "video")[:50],
            doctor=doctor_report(cfg, db, api, quick=True),
            sample=[(c.video["title"], round(c.score, 3), ranking.explain(c)) for c in result.items[:10]],
        )

    @app.post("/api/interests", summary="Set or remove one interest", tags=["api"],
              openapi_extra=documented(InterestBody, {"action": "set", "tag": "compilers", "weight": 0.9}))
    async def interests_update(request: Request):
        body = await request.json()
        action = body.get("action", "set")
        if action == "remove":
            interest_store.remove_interest(db, body.get("tag", ""))
        elif action == "rederive":
            count = interest_store.derive_from_history(db)
            return {"ok": True, "derived": count}
        else:
            if not str(body.get("tag", "")).strip():
                raise HTTPException(400, "which interest? send a non-empty `tag`, or "
                                         "use POST /api/interests/rederive to rebuild them")
            interest_store.set_interest(
                db, body.get("tag", ""), float(body.get("weight", 0.8)),
                origin="manual", confidence=float(body.get("confidence", 1.0)), pinned=True,
            )
        return {"ok": True, "interests": interest_store.listing(db, 60)}

    @app.post("/api/model/reset", summary="Forget everything the ranker learned", tags=["api"])
    def model_reset():
        learner_mod.reset(db)
        return {"ok": True}

    @app.post("/api/model/retrain", summary="Rebuild the ranker from history and feedback", tags=["api"])
    def model_retrain():
        active = settings()["learning"]
        return learner_mod.train_from_events(
            db, float(active.get("rate", 0.08)), float(active.get("l2", 0.001))
        )

    # ----------------------------------------------------------------- llm

    @app.get("/brief", response_class=HTMLResponse, tags=["pages"])
    def brief_page(request: Request):
        return page(request, "brief.html", compiled=None)

    @app.post("/brief", response_class=HTMLResponse)
    async def brief_compile(request: Request, text: str = Form(""), apply: str = Form("")):
        positive, _ = interest_store.interest_vector(db)
        from . import textutil as T
        compiled = llm.compile_brief(cfg, text, {"top_interests": T.top_terms(positive, 12)})
        if apply and compiled.get("patch"):
            actions.apply_brief(db, text, compiled)
            return RedirectResponse("/?applied=brief", status_code=303)
        return page(request, "brief.html", compiled=compiled, text=text)

    @app.post("/api/critique", summary="Explain the current homepage in prose", tags=["api"])
    def critique():
        active = settings()
        result = ranking.recommend(db, active, community=community)
        titles = [c.video["title"] for c in result.items[:12]]
        return {"text": llm.critique(cfg, active, result.diagnostics, titles)}

    # ---------------------------------------------------------------- rules

    @app.get("/rules", response_class=HTMLResponse, tags=["pages"])
    def rules_page(request: Request):
        active = settings()
        return page(
            request, "rules.html",
            fields=rule_engine.FIELDS, operators=list(rule_engine.OPERATORS),
            expr=json.dumps(active["rules"]["expr"], indent=2),
            summary=rule_engine.describe(active["rules"]["expr"]),
        )

    @app.post("/api/rules/validate", summary="Check a rule and preview what it keeps and drops", tags=["api"],
              openapi_extra=documented(RuleBody, {"expr": {"all": [{"field": "education", "op": ">", "value": 70}]}}))
    async def rules_validate(request: Request):
        body = await request.json()
        try:
            expr = body.get("expr")
            if isinstance(expr, str):
                expr = json.loads(expr)
            rule_engine.validate(expr)
        except (rule_engine.RuleError, json.JSONDecodeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)
        matched = _rule_preview(db, expr, settings())
        return {"ok": True, "summary": rule_engine.describe(expr), "preview": matched}

    @app.post("/api/rules", summary="Save and enable or disable the rule", tags=["api"],
              openapi_extra=documented(RuleBody, {"enabled": True, "expr": {"all": [{"field": "brainrot", "op": "<", "value": 20}]}}))
    async def rules_save(request: Request):
        body = await request.json()
        expr = body.get("expr")
        if isinstance(expr, str):
            expr = json.loads(expr)
        rule_engine.validate(expr)
        save_settings({"rules": {"enabled": bool(body.get("enabled", True)), "expr": expr}})
        return {"ok": True}

    # ------------------------------------------------------------ analytics

    @app.get("/analytics", response_class=HTMLResponse, tags=["pages"])
    def analytics_page(request: Request, days: int = 30):
        return page(
            request, "analytics.html",
            data=analytics.dashboard(db, days),
            attention=analytics.attention(db),
            trends=interest_store.timeseries(db),
            rewatches=analytics.rewatches(db),
            blind_spots=analytics.blind_spots(db),
            days=days,
        )

    # -------------------------------------------------------------- profile

    @app.get("/api/profile/export", summary="Download your whole configuration as JSON", tags=["api"])
    def profile_export(name: str = "", author: str = ""):
        body = profiles.export_profile(db, name, author=author)
        return Response(
            json.dumps(body, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="sieve-profile.json"'},
        )

    @app.post("/api/profile/import", summary="Import a profile document", tags=["api"],
              openapi_extra=documented(ProfileImportBody, {"profile": {"format": 1, "settings": {"novelty": 55}}, "merge": True}))
    async def profile_import(request: Request):
        body = await request.json()
        applied = profiles.import_profile(
            db, body.get("profile", body), merge=bool(body.get("merge", True))
        )
        return {"ok": True, "applied": applied}

    @app.post("/profile/save")
    async def profile_save(name: str = Form(...), author: str = Form("")):
        profiles.save_profile(db, name, profiles.export_profile(db, name, author=author), author)
        return RedirectResponse("/settings?saved=profile", status_code=303)

    @app.post("/profile/load")
    async def profile_load(name: str = Form(...)):
        body = profiles.load_profile(db, name)
        if body is None:
            raise HTTPException(404, "profile not found")
        profiles.import_profile(db, body, merge=False)
        return RedirectResponse("/settings?loaded=1", status_code=303)

    @app.post("/settings/mood/save")
    async def mood_save(name: str = Form(...)):
        try:
            actions.save_mood(db, name)
        except actions.ActionError as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse("/settings?saved=mood#moods", status_code=303)

    @app.post("/settings/mood/delete")
    async def mood_delete(name: str = Form(...)):
        actions.delete_mood(db, name)
        return RedirectResponse("/settings?deleted=mood#moods", status_code=303)

    @app.post("/api/profile/fetch", summary="Import a profile from a URL", tags=["api"],
              openapi_extra=documented(ProfileFetchBody, {"url": "https://example.com/no-brainrot.json"}))
    async def profile_fetch(request: Request):
        """Import a shared profile straight from a URL.

        This is the "recommendation market" from the brief, without a market:
        a profile is a JSON file, so a gist, a forum attachment or a git repo is
        already a distribution channel. The fetched document goes through the
        same whitelist as everything else, because a URL someone posted is the
        least trusted input this program has.
        """
        body = await request.json()
        url = str(body.get("url", ""))
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "expected an http or https URL")
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                document = response.json()
        except Exception as exc:
            raise HTTPException(400, f"could not fetch that profile: {exc}") from None
        applied = profiles.import_profile(db, document, merge=bool(body.get("merge", True)))
        return {"ok": True, "applied": applied, "name": document.get("name", "")}

    # --------------------------------------------------------------- import

    @app.post("/api/import/subscriptions", summary="Import subscriptions from an Invidious, NewPipe or FreeTube export", tags=["api"])
    async def import_subs(file: UploadFile):
        payload = json.loads((await file.read()).decode("utf-8"))
        count = ingest.import_subscriptions(db, payload)
        return {"ok": True, "imported": count}

    @app.post("/api/import/history", summary="Import watch history from Invidious or Google Takeout", tags=["api"])
    async def import_hist(file: UploadFile):
        payload = json.loads((await file.read()).decode("utf-8"))
        count = ingest.import_history(db, payload, api)
        channel_policy.recompute_affinity(db)
        return {"ok": True, "imported": count}

    # --------------------------------------------------------------- system

    @app.get("/api/sponsorblock/{video_id}", summary="SponsorBlock segments for a video, and which to skip", tags=["api"])
    def sponsor_segments(video_id: str):
        active = settings()["sponsorblock"]
        if not active.get("enabled"):
            return {"segments": [], "skip": []}
        community.fetch_segments([video_id], active.get("categories"))
        data = community.segments_for(video_id)
        data["skip"] = active.get("skip", [])
        return data

    @app.post("/api/vision/scan", summary="Score thumbnails for visual NSFW (optional extra)", tags=["api"])
    def vision_scan(limit: int = 200):
        from . import vision

        return vision.score_pending(db, cfg, limit)

    @app.get("/demo/thumb/{video_id}.svg")
    def demo_thumb(video_id: str):
        """Placeholder art for the synthetic catalogue.

        `sieve demo` is meant to be evaluable with no network at all, and a
        wall of broken images is not an evaluation. Real videos always use the
        instance's own thumbnail proxy; this only ever answers for demo ids.
        """
        if not video_id.startswith("demo"):
            raise HTTPException(404, "not a demo video")
        row = db.get_video(video_id)
        return Response(
            _placeholder_svg(video_id, row["author"] if row else "", row["genre"] if row else ""),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/status", summary="Counts, sync state, configured services", tags=["api"])
    def status():
        return {
            "stats": db.stats(),
            "last_sync": ingestor.last_sync,
            "worker": ingestor.status,
            "llm": cfg.llm_provider,
            "instances": cfg.instances,
        }

    @app.post("/api/sync", summary="Pull recent videos now, then rescore and rederive", tags=["api"])
    def sync(deep: bool = False):
        counts = ingestor.sync(deep=deep)
        ingestor.score_pending(limit=100)
        channel_policy.recompute_affinity(db)
        interest_store.derive_from_history(db)
        return {"ok": True, **counts}

    @app.on_event("startup")
    def _startup() -> None:
        if start_worker:
            ingestor.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        ingestor.stop()
        api.close()
        community.close()

    return app


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------



PLACEHOLDER_PALETTE = [
    ("#3f3aa8", "#2b2780"), ("#0f6b60", "#0b4f47"), ("#916200", "#6b4900"),
    ("#6c3480", "#4d2460"), ("#4a7fb5", "#325978"), ("#9c3b25", "#70291a"),
]


def _placeholder_svg(video_id: str, author: str, genre: str) -> str:
    """Deterministic, abstract cover art for a demo video.

    Deliberately not an imitation of a video thumbnail: it is a coloured field
    with the channel's initials, so nobody mistakes the demo catalogue for real
    content.
    """
    import hashlib

    digest = hashlib.sha256(f"{author}|{video_id}".encode()).digest()
    top, bottom = PLACEHOLDER_PALETTE[digest[0] % len(PLACEHOLDER_PALETTE)]
    initials = "".join(word[0] for word in (author or "??").split()[:2]).upper() or "??"
    angle = 12 + digest[1] % 40
    bars = "".join(
        f'<rect x="{18 + i * 42}" y="{150 - (18 + digest[(i + 2) % 30] % 70)}" '
        f'width="24" height="{18 + digest[(i + 2) % 30] % 70}" fill="#ffffff" opacity="0.10"/>'
        for i in range(7)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180" width="320" height="180">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1" '
        f'gradientTransform="rotate({angle} .5 .5)">'
        f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/>'
        f'</linearGradient></defs>'
        f'<rect width="320" height="180" fill="url(#g)"/>{bars}'
        f'<text x="160" y="98" text-anchor="middle" fill="#ffffff" fill-opacity="0.9" '
        f'font-family="system-ui,sans-serif" font-size="42" font-weight="600" '
        f'letter-spacing="1">{initials}</text>'
        f'<text x="160" y="122" text-anchor="middle" fill="#ffffff" fill-opacity="0.55" '
        f'font-family="ui-monospace,monospace" font-size="10">demo catalogue</text>'
        f'</svg>'
    )


def _nav(path: str) -> list[dict]:
    entries = [
        ("/", "Home"), ("/settings", "Controls"), ("/channels", "Channels"),
        ("/rules", "Rules"), ("/brief", "Brief"), ("/debugger", "Debugger"),
        ("/analytics", "Analytics"),
    ]
    return [{"href": href, "label": label, "active": path == href} for href, label in entries]


def _empty_hint(db: Database, result: ranking.Result) -> str:
    if result.items:
        return ""
    stats = db.stats()
    if stats["videos"] == 0:
        return ("Nothing in the catalogue yet. Import your subscriptions, then run a sync — "
                "or run `sieve demo` to populate a synthetic catalogue and try the controls.")
    if result.diagnostics["rejected_total"] > 0:
        top = result.diagnostics["rejected"][0]
        return f"Everything was filtered out. The biggest cause: {top['reason']} ({top['count']} videos)."
    return "No candidates matched. Try widening your sources on the Controls page."


def _apply_structured_feedback(db: Database, kind: str, card: scoring.ScoreCard,
                               row: Any, save) -> dict[str, Any]:
    """Turn "same topic but shorter/deeper/better" into an actual settings nudge."""
    duration = int(row["duration"] or 0)
    if kind == "shorter" and duration:
        save({"filters": {"max_duration": max(300, int(duration * 0.8))}})
        return {"filters.max_duration": max(300, int(duration * 0.8))}
    if kind == "longer" and duration:
        save({"filters": {"min_duration": max(0, int(duration * 1.2))}})
        return {"filters.min_duration": max(0, int(duration * 1.2))}
    if kind == "deeper":
        target = min(100, int(card["technical_depth"]) + 15)
        save({"targets": {"technical_depth": {"enabled": True, "target": target, "weight": 1.3}}})
        return {"targets.technical_depth": target}
    if kind == "lighter":
        target = max(0, int(card["technical_depth"]) - 20)
        save({"targets": {"technical_depth": {"enabled": True, "target": target, "weight": 1.0}}})
        return {"targets.technical_depth": target}
    if kind == "higher_quality":
        target = min(100, int(card["production"]) + 20)
        save({"targets": {"production": {"enabled": True, "target": target, "weight": 1.1}},
              "filters": {"max_clickbait": 40}})
        return {"targets.production": target, "filters.max_clickbait": 40}
    return {}


def _rule_preview(db: Database, expr: dict, settings: dict, limit: int = 8) -> dict:
    """Run a candidate rule over the catalogue so the editor shows real effects."""
    rows = db.query(
        "SELECT v.*, s.* FROM videos v JOIN scores s ON s.video_id = v.id "
        "ORDER BY v.published DESC LIMIT 400"
    )
    matched, rejected = [], []
    policy = channel_policy.load_policy(db, settings)
    watched = db.watched_ids()
    subscribed = {r["channel_id"] for r in db.query("SELECT channel_id FROM subscriptions")}
    for row in rows:
        record = ranking._rule_record(
            dict(row), scoring.row_to_card(row), {}, policy, subscribed, watched, {}
        )
        target = matched if rule_engine.evaluate(expr, record) else rejected
        if len(target) < limit:
            target.append({"title": row["title"], "author": row["author"]})
    return {"kept": matched, "dropped": rejected,
            "kept_share": round(len(matched) / max(1, len(rows)), 3)}


def _form_to_patch(form) -> dict:
    """Translate the Controls form into a settings patch."""
    patch: dict[str, Any] = {
        "homepage": {}, "sources": {}, "weights": {}, "filters": {}, "targets": {},
        "channels": {}, "dearrow": {}, "sponsorblock": {}, "budget": {"quotas": {}, "daily_caps": {}},
        "diversity": {},
    }

    def get(name: str, caster=str, default=None):
        value = form.get(name)
        if value is None or value == "":
            return default
        try:
            return caster(value)
        except (TypeError, ValueError):
            return default

    def flag(name: str) -> bool:
        return form.get(name) is not None

    patch["homepage"] = {
        "count": get("homepage.count", int, 36),
        "columns": get("homepage.columns", int, 3),
        "density": get("homepage.density", str, "comfortable"),
        "mode": get("homepage.mode", str, "blend"),
        "playlist_id": get("homepage.playlist_id", str, ""),
        "refresh_seed": get("homepage.refresh_seed", str, "daily"),
        "show_explanations": flag("homepage.show_explanations"),
        "show_scores": flag("homepage.show_scores"),
        "continue_first": flag("homepage.continue_first"),
        "shuffle": flag("homepage.shuffle"),
    }
    for key in SOURCE_KEYS:
        patch["sources"][key] = get(f"sources.{key}", int, 0)
    for key in ("source", "interest", "preference", "learned", "freshness", "novelty",
                "continue", "channel_quality", "channel_priority", "penalty"):
        value = get(f"weights.{key}", float)
        if value is not None:
            patch["weights"][key] = value
    patch["novelty"] = get("novelty", int, 30)

    for key in SCORE_KEYS:
        if flag(f"targets.{key}.enabled"):
            patch["targets"][key] = {
                "enabled": True,
                "target": get(f"targets.{key}.target", int, 70),
                "weight": get(f"targets.{key}.weight", float, 1.0),
            }
        else:
            patch["targets"][key] = {"enabled": False,
                                     "target": get(f"targets.{key}.target", int, 70),
                                     "weight": get(f"targets.{key}.weight", float, 1.0)}

    for key, caster, default in (
        ("hide_shorts", bool, None), ("hide_live", bool, None), ("hide_upcoming", bool, None),
        ("hide_watched", bool, None), ("shorts_seconds", int, 180),
        ("min_duration", int, 0), ("max_duration", int, 0), ("max_brainrot", int, 100),
        ("max_clickbait", int, 100), ("max_nsfw", int, 25), ("max_music", int, 100),
        ("max_ai_generated", int, 100), ("max_profanity", int, 100),
        ("min_education", int, 0), ("min_info_density", int, 0),
        ("min_views", int, 0), ("max_views", int, 0), ("min_subs", int, 0), ("max_subs", int, 0),
        ("max_age_days", int, 0), ("min_like_ratio", float, 0.0),
    ):
        patch["filters"][key] = flag(f"filters.{key}") if caster is bool else get(f"filters.{key}", caster, default)

    # Languages arrive as a comma-separated box rather than a multi-select:
    # caption language codes are a long tail and people know their own.
    raw_languages = get("filters.languages", str, "")
    patch["filters"]["languages"] = [
        code.strip().lower()[:8] for code in raw_languages.replace(";", ",").split(",")
        if code.strip()
    ][:12]

    patch["channels"] = {
        "whitelist_only": flag("channels.whitelist_only"),
        "affinity_enabled": flag("channels.affinity_enabled"),
        "blocked_hidden": flag("channels.blocked_hidden"),
        "manual_strength": get("channels.manual_strength", float, 0.18),
        "affinity_strength": get("channels.affinity_strength", float, 0.5),
        "affinity_half_life_days": get("channels.affinity_half_life_days", float, 45),
    }
    patch["dearrow"] = {
        "enabled": flag("dearrow.enabled"),
        "replace_titles": flag("dearrow.replace_titles"),
        "replace_thumbnails": flag("dearrow.replace_thumbnails"),
        "show_original": flag("dearrow.show_original"),
        "min_votes": get("dearrow.min_votes", int, 0),
    }
    patch["sponsorblock"] = {
        "enabled": flag("sponsorblock.enabled"),
        "hide_exclusive_access": flag("sponsorblock.hide_exclusive_access"),
        "max_sponsor_ratio": get("sponsorblock.max_sponsor_ratio", float, 1.0),
        "max_filler_ratio": get("sponsorblock.max_filler_ratio", float, 1.0),
        "score_penalty": get("sponsorblock.score_penalty", float, 0.0),
        "skip": form.getlist("sponsorblock.skip") if hasattr(form, "getlist") else [],
    }
    patch["budget"]["enabled"] = flag("budget.enabled")
    for bucket in BUCKETS:
        value = get(f"budget.quotas.{bucket}", int)
        if value is not None:
            patch["budget"]["quotas"][bucket] = value
        cap = get(f"budget.daily_caps.{bucket}", int)
        if cap is not None:
            patch["budget"]["daily_caps"][bucket] = cap
    patch["diversity"] = {
        "enabled": flag("diversity.enabled"),
        "max_per_channel": get("diversity.max_per_channel", int, 3),
        "mmr_lambda": get("diversity.mmr_lambda", float, 0.75),
        "warn_below": get("diversity.warn_below", float, 0.35),
    }
    patch["learning"] = {"enabled": flag("learning.enabled")}
    return patch


def _fmt_duration(seconds: Any) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "—"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _fmt_count(value: Any) -> str:
    value = int(value or 0)
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}".replace(".0", "")
    return str(value)


def _fmt_ago(timestamp: Any) -> str:
    timestamp = int(timestamp or 0)
    if not timestamp:
        return "never"
    delta = max(0, int(time.time()) - timestamp)
    for limit, unit in ((86400 * 365, "y"), (86400 * 30, "mo"), (86400, "d"), (3600, "h"), (60, "m")):
        if delta >= limit:
            return f"{delta // limit}{unit} ago"
    return "just now"
