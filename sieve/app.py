"""The web application.

Server-rendered HTML with a small amount of vanilla JavaScript. No build step,
no bundler, no client framework: the whole UI is delivered as HTML and ~12 KB of
CSS, which matters when the target is a box that also has to run the scorer.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    actions,
    analytics,
    autotune,
    backups,
    corrections,
    demo,
    downloads,
    ingest,
    langdetect,
    library,
    llm,
    notify,
    problems,
    profiles,
    providers,
    ranking,
    scales,
    scoring,
    starter,
    ytauth,
)
from . import channels as channel_policy
from . import interests as interest_store
from . import learner as learner_mod
from . import rules as rule_engine
from . import textutil as T
from .api import (
    ChannelBody,
    FeedbackBody,
    FetchBody,
    HideBody,
    InterestBody,
    ProfileFetchBody,
    ProfileImportBody,
    ProgressBody,
    RestoreBody,
    RuleBody,
    documented,
    json_object,
)
from .api import router as api_router
from .community import CommunityData
from .config import (
    BUCKETS,
    PULL_WINDOWS,
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
from .invidious import UpstreamUnavailable
from .present import display_title as _display_title
from .present import thumb_url as _thumb
from .upstream import Upstream

log = logging.getLogger("sieve.app")
HERE = Path(__file__).parent


def create_app(cfg: Config | None = None, start_worker: bool = True) -> FastAPI:
    cfg = cfg or Config.load()
    db = Database(cfg.db_path)
    api = Upstream(cfg, db)
    community = CommunityData(cfg, db)
    ingestor = ingest.Ingestor(cfg, db, api, community)
    downloader = downloads.Downloader(db, str(cfg.data_dir), api.budget,
                                      ydl_factory=api.youtube._ytdlp_factory)

    app = FastAPI(title="Sieve", docs_url="/api/docs", redoc_url=None)
    app.state.cfg = cfg
    app.state.db = db
    app.state.api = api
    app.state.community = community
    app.state.ingestor = ingestor
    app.state.downloader = downloader

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(api_router)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["duration"] = _fmt_duration
    templates.env.filters["count"] = _fmt_count
    templates.env.filters["ago"] = _fmt_ago
    templates.env.filters["until"] = _fmt_until
    templates.env.filters["sentence"] = _sentence
    templates.env.filters["filesize"] = _filesize
    templates.env.filters["timecode"] = _timecode
    templates.env.filters["clock"] = lambda t: time.strftime("%d %b %H:%M:%S", time.localtime(int(t or 0)))

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
        active_settings = settings()
        dismissed = set(db.get_setting("dismissed_notices", []) or [])
        base = {
            "request": request,
            "dismissed": dismissed,
            # Backend problems, explained, until dismissed (a dismissal lasts
            # until the problem happens again: the key includes its time).
            "problems": [p for p in problems.recent(db) if f"problem-{p['kind']}-{p['at']}" not in dismissed],
            "cfg": cfg,
            "settings": active_settings,
            "stored": stored_settings(),
            "nav": _nav(request.url.path),
            "stats": db.stats(),
            "providers": providers.menu(active_settings, cfg),
            "default_provider": providers.PROVIDERS[providers.default_provider(active_settings, cfg)],
            "playable": providers.playable,
            "new_tab": active_settings["playback"].get("new_tab", True),
            "playback_unconfigured": providers.looks_unconfigured(
                active_settings, cfg, db.get_setting("settings", {}) or {}),
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
        result = ranking.homepage(
            db, active, mood=bool(mood),
            seed=int(time.time()) if refresh else None,
            community=community,
        )
        # Nothing is pulled from here: fetching waits for you to press Fetch.
        fetched = ingestor.has_fetched()
        items = _cards(result.items, active)
        shown_ids = [c.id for c in result.items]
        feedback_state = {r["video_id"]: r["kind"] for r in db.query(
            f"SELECT video_id, kind FROM feedback WHERE kind IN ('more', 'less') "
            f"AND video_id IN ({','.join('?' * len(shown_ids))}) ORDER BY created_at", shown_ids)} if shown_ids else {}
        return page(
            request, "home.html",
            items=items, diagnostics=result.diagnostics,
            empty=_empty_state(db, ingestor, api, result, active) if not items else None,
            demo_only=bool(items) and bool(demo.demo_count(db)) and not db.playable_count(),
            feedback_state=feedback_state,
            fetched=fetched,
            fetch_plan=_fetch_plan(active, api),
            playable_videos=db.playable_count(),
            demo_videos=demo.demo_count(db),
            playlists=db.query("SELECT id, title FROM playlists"),
            moods=list(active.get("moods", {})),
            mode_label=MODE_LABELS.get(active["homepage"].get("mode", "blend"), "Blend"),
        )

    def _cards(candidates, active):
        return [
            {"c": c, "video": c.video, "explanation": ranking.explain(c),
             "display_title": _display_title(c, active), "thumb": _thumb(cfg, c, active),
             "filler": ranking._filler_reason(c)}
            for c in candidates
        ]

    @app.get("/sift", response_class=HTMLResponse, tags=["pages"])
    def sift_page(request: Request, q: str = "", kind: str = "auto", filters: int | None = None,
                  run: int = 0, limit: int | None = None):
        """GET, so a sift is a link you can bookmark or share. Runs only when
        asked (`run=1`), never just because the page was opened."""
        from . import sift

        active = settings()
        # An unticked checkbox sends nothing, so a submitted form without
        # `filters` means off; a plain visit to the page defaults to on.
        filters = (0 if run else 1) if filters is None else filters
        context = {"q": q, "kind": kind, "filters": bool(filters), "limit": limit,
                   "kinds": sift.KINDS, "outcome": None, "error": ""}
        if run:
            try:
                out = sift.run(db, api, active, q, kind, limit, bool(filters), community)
            except (actions.ActionError, UpstreamUnavailable) as exc:
                context["error"] = str(exc)
            else:
                result = out.pop("result")
                context["outcome"] = {**out, "items": _cards(result.items, active),
                                      "diagnostics": result.diagnostics,
                                      "rejected": [e for e in result.ledger if e["stage"] == "rejected"]}
        return page(request, "sift.html", **context)

    @app.get("/funnel", response_class=HTMLResponse, tags=["pages"])
    def funnel_page(request: Request):
        result = ranking.recommend(db, settings(), community=community, record=False, ledger=True)
        entries = result.ledger or []
        counts = {k: sum(1 for e in entries if e["stage"] == k) for k in ("shown", "ranked", "rejected")}
        reasons: dict[str, int] = {}
        for e in entries:
            if e["stage"] != "shown":
                reasons[e["reason"].split(":")[0]] = reasons.get(e["reason"].split(":")[0], 0) + 1
        return page(request, "funnel.html", entries=entries, counts=counts,
                    total=sum(counts.values()), diagnostics=result.diagnostics,
                    reasons=sorted(reasons.items(), key=lambda kv: -kv[1]))

    @app.get("/history", response_class=HTMLResponse, tags=["pages"])
    def history_page(request: Request, kind: str = "watches", page_no: int = 1):
        if kind not in actions.RECORD_KINDS:
            kind = "watches"
        per_page = 100
        listing = actions.list_records(db, kind, per_page, (max(1, page_no) - 1) * per_page)
        totals = {k: db.one(f"SELECT COUNT(*) AS n FROM {t[0]}")["n"]
                  for k, t in actions.RECORD_KINDS.items()}
        return page(request, "history.html", kind=kind, listing=listing, totals=totals,
                    page_no=max(1, page_no), pages=max(1, -(-listing["total"] // per_page)))

    @app.get("/open/{video_id}", tags=["pages"],
             summary="Open a video with your chosen provider, and record that you did")
    def open_video(request: Request, video_id: str, via: str = "", t: int | None = None):
        """Every watch link in Sieve points here.

        Deliberately not `/watch`: the documented nginx setup sends /watch to
        Invidious, so a Sieve route there would be unreachable behind the proxy.

        Records an *open*, not a watch. Redirects to the provider with no
        referrer, so the provider does not learn your Sieve address. A video no
        provider can play — the demo catalogue — goes to its Sieve page, which
        says why, instead of to a provider's 404.
        """
        active = settings()
        if not providers.playable(video_id):
            return RedirectResponse(f"/video/{quote(video_id)}?unplayable=1", status_code=303)
        provider = via or providers.default_provider(active, cfg)
        if provider not in providers.available(active, cfg):
            raise HTTPException(400, f"{provider!r} is not a provider you can use; "
                                     "see Controls, Playback")
        start = t
        if start is None and active["playback"].get("resume", True):
            start = _resume_point(db, video_id)
        url = providers.url_for(provider, video_id, active, cfg, start)
        # HEAD answers the same redirect, so link checkers see a working link,
        # but it is not you opening the video and is not recorded as one.
        if request.method == "GET":
            db.record_open(video_id, provider)
        response = RedirectResponse(url, status_code=302)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/play/{video_id}", response_class=HTMLResponse, tags=["pages"],
             summary="Watch in Sieve's own player")
    def play(request: Request, video_id: str, t: int | None = None):
        """YouTube's privacy-enhanced player, embedded in a Sieve page.

        Not `/embed` or `/watch`: the documented nginx setup routes both of
        those to Invidious.
        """
        if not providers.playable(video_id):
            return RedirectResponse(f"/video/{quote(video_id)}?unplayable=1", status_code=303)
        row = db.get_video(video_id)
        start = t if t is not None else (
            _resume_point(db, video_id) if settings()["playback"].get("resume", True) else None)
        others = [link for link in providers.links(video_id, settings(), cfg)
                  if link["provider"] != "nocookie"]
        return page(request, "play.html", video_id=video_id, video=dict(row) if row else None,
                    feedback_now=db.scalar("SELECT kind FROM feedback WHERE video_id = ? AND kind IN "
                                           "('more', 'less') ORDER BY created_at DESC LIMIT 1", (video_id,), None),
                    chapters=library.chapters(row["description"]) if row else [],
                    local=downloads.local_file(db, video_id) is not None,
                    start=start, other_links=others)

    # HEAD as a separate route, kept out of the schema: sharing one route with
    # GET gives both the same OpenAPI operation id, which makes the schema invalid.
    app.add_api_route("/open/{video_id}", open_video, methods=["HEAD"], include_in_schema=False)

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
        resume = _resume_point(db, video_id) if settings()["playback"].get("resume", True) else None
        return page(
            request, "video.html",
            watch_links=providers.links(video_id, settings(), cfg, resume),
            resume=resume,
            video=row, card=card,
            breakdown={key: card.explain(key) for key in SCORE_KEYS} if card else {},
            overrides=corrections.overrides_for(db, [video_id]).get(video_id, {}),
            override_source={r["axis"]: r["source"] for r in db.query(
                "SELECT axis, source FROM score_overrides WHERE video_id = ?", (video_id,))},
            predicted={key: round(sig.value * 100) for key, sigs in (card.signals.items() if card else [])
                       for sig in sigs if sig.name == "your_override"},
            feedback_now=db.scalar("SELECT kind FROM feedback WHERE video_id = ? AND kind IN ('more', 'less') "
                                   "ORDER BY created_at DESC LIMIT 1", (video_id,), None),
            sponsor_problem=problems.latest(db, "sponsorblock_timeout") or problems.latest(db, "sponsorblock_error"),
            bot_problem=problems.latest(db, "youtube_bot_check"),
            notes=library.notes_for(db, video_id),
            chapters=library.chapters(row["description"]) if row else [],
            download=db.one("SELECT * FROM downloads WHERE video_id = ?", (video_id,)),
            alerting=bool(row and db.scalar("SELECT alert FROM channel_prefs WHERE channel_id = ?",
                                            (row["author_id"],), 0)),
            can_download=downloader.available(),
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
        body = await json_object(request)
        return give_feedback(str(body.get("video_id", "")), str(body.get("kind", "")),
                             str(body.get("note", ""))[:400], bool(body.get("toggle", True)))

    def give_feedback(video_id: str, kind: str, note: str = "", toggle: bool = True) -> dict:
        """More, Less, and the structured kinds; shared by the buttons and
        by "tell Sieve why"."""
        if kind not in learner_mod.FEEDBACK_TARGETS:
            raise HTTPException(400, f"unknown feedback kind {kind!r}")
        state = kind
        if kind in ("more", "less"):
            # One choice per video: More and Less clear each other, and
            # choosing the one already chosen takes it back.
            previous = db.scalar(
                "SELECT kind FROM feedback WHERE video_id = ? AND kind IN ('more', 'less') "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (video_id,), None)
            db.execute("DELETE FROM feedback WHERE video_id = ? AND kind IN ('more', 'less')", (video_id,))
            if previous == kind and toggle:
                _nudge_topics(db, video_id, kind, undo=True)
                return {"ok": True, "state": None, "applied": {}}
            if previous and previous != kind:
                _nudge_topics(db, video_id, previous, undo=True)
            _nudge_topics(db, video_id, kind)
        db.execute(
            "INSERT INTO feedback(video_id, kind, note, created_at) VALUES(?,?,?,?)",
            (video_id, kind, note[:400], int(time.time())),
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
        return {"ok": True, "state": state if kind in ("more", "less") else None, "applied": applied}

    @app.post("/api/progress", summary="Report how much of a video was watched", tags=["api"],
              openapi_extra=documented(ProgressBody, {"video_id": "dQw4w9WgXcQ", "progress": 0.82}))
    async def progress(request: Request):
        body = await json_object(request)
        video_id = str(body.get("video_id", ""))
        session = str(body.get("session") or "")[:64] or None
        try:
            fraction = float(body.get("progress", 0))
            dwell = int(body.get("dwell", 0) or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "progress must be a number from 0 to 1") from None
        if not video_id or not 0 <= fraction <= 1:
            raise HTTPException(400, "send a video_id and a progress from 0 to 1")
        db.record_watch(video_id, fraction, max(0, dwell), origin="player", session=session)
        return {"ok": True}

    @app.post("/api/hide", summary="Hide a video (same as POST /api/blocklist)", tags=["api"],
              openapi_extra=documented(HideBody, {"kind": "video", "value": "dQw4w9WgXcQ"}))
    async def hide(request: Request):
        body = await json_object(request)
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
            backend_status=api.status(),
            topics=starter.TOPICS,
            pull_windows=PULL_WINDOWS,
            pull_status=api.budget.status(),
            pull_summary=api.budget.summary(),
            last_pull=ingestor.last_result,
            fetch_state=ingestor.pull_state(),
            fetch_plan=_fetch_plan(settings(), api),
            backup_settings=backups.settings(db),
            bot_check=ytauth.status(db),
            has_cookies=ytauth.cookies_file(cfg.data_dir).exists(),
            cookie_browsers=ytauth.BROWSERS,
            new_every_choices=ranking.NEW_EVERY_CHOICES,
            video_language_choices=list(langdetect.LANGUAGES.items()),
            ai_connections=dict(llm.CONNECTIONS),
            ai_status=ai_status(),
            ai_tune_log=db.get_setting("ai_tune_log", {}) or {},
            ai_tune_undo=bool((db.get_setting("ai_tune_undo", {}) or {}).get("settings") is not None
                              or db.scalar("SELECT COUNT(*) FROM score_overrides WHERE source = 'ai'", default=0)),
            demo_videos=demo.demo_count(db),
            provider_catalogue=providers.PROVIDERS,
            invidious_fallback=cfg.watch_base.split("/watch", 1)[0],
            safe_schemes=", ".join(sorted(providers.SAFE_SCHEMES)),
            test_video=providers.TEST_VIDEO,
            backups=actions.list_backups(db),
            backup_dir=str(cfg.data_dir / "backups"),
            saved=profiles.list_profiles(db),
        )

    @app.post("/api/settings", summary="Merge a settings patch into yours", tags=["api"],
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                  "schema": {"type": "object"},
                  "example": {"novelty": 60, "filters": {"hide_shorts": True, "max_brainrot": 25}}}}}})
    async def settings_api(request: Request):
        body = await json_object(request)
        if not isinstance(body, dict):
            raise HTTPException(400, "send a JSON object of settings")
        if isinstance(body.get("playback"), dict):
            # Imports drop a bad field quietly; a direct request should hear why.
            try:
                checked = providers.sanitise(body["playback"])
            except providers.ProviderError as exc:
                raise HTTPException(400, str(exc)) from None
            if checked.get("provider") == "custom" and not (
                    checked.get("custom_url") or settings()["playback"].get("custom_url")):
                raise HTTPException(400, "enter a custom template first, then choose Custom")
        applied = profiles.sanitise_settings(body)
        merged = save_settings(applied)
        # `applied` is what was actually stored. Autosave compares it with what
        # it sent, so a dropped value is reported instead of shown as saved.
        return {"ok": True, "applied": applied, "settings": merged, "effect": _effect()}

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
        body = await json_object(request)
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
        # record=False: opening the debugger is not being shown the homepage,
        # and recording it used up the daily caps.
        result = ranking.recommend(db, active, community=community, record=False)
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
        body = await json_object(request)
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
        compiled = llm.compile_brief(llm.effective(cfg, db), text, {"top_interests": T.top_terms(positive, 12)})
        if apply and compiled.get("patch"):
            actions.apply_brief(db, text, compiled)
            return RedirectResponse("/?applied=brief", status_code=303)
        return page(request, "brief.html", compiled=compiled, text=text)

    @app.post("/api/critique", summary="Explain the current homepage in prose", tags=["api"])
    def critique():
        active = settings()
        result = ranking.recommend(db, active, community=community, record=False)
        titles = [c.video["title"] for c in result.items[:12]]
        return {"text": llm.critique(llm.effective(cfg, db), active, result.diagnostics, titles)}

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
        body = await json_object(request)
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
        body = await json_object(request)
        expr = body.get("expr")
        try:
            if isinstance(expr, str):
                expr = json.loads(expr)
            rule_engine.validate(expr)
        except (rule_engine.RuleError, json.JSONDecodeError) as exc:
            # Was an unhandled 500; an invalid rule is the caller's mistake.
            raise HTTPException(400, f"not saved: {exc}") from None
        save_settings({"rules": {"enabled": bool(body.get("enabled", True)), "expr": expr}})
        return {"ok": True, "summary": rule_engine.describe(expr)}

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
        body = await json_object(request)
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
        body = await json_object(request)
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

        return vision.score_pending(db, cfg, limit, api.thumbnail_url)

    @app.get("/thumb/{video_id}", tags=["pages"], summary="A video's thumbnail, from wherever it is available")
    def thumbnail(video_id: str):
        """Every thumbnail on every page points here.

        Before this, thumbnails pointed straight at the first Invidious
        instance, so anyone without one saw a wall of broken images. Now the
        choice is made at request time: Invidious' proxy while it answers,
        YouTube's image host when it does not. Short browser caching keeps it
        cheap without pinning a dead host for long.
        """
        if not providers.playable(video_id):
            return RedirectResponse(f"/demo/thumb/{quote(video_id)}.svg", status_code=302)
        response = RedirectResponse(api.thumbnail_url(video_id), status_code=302)
        response.headers["Cache-Control"] = "private, max-age=600"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/demo/thumb/{video_id}.svg")
    def demo_thumb(video_id: str):
        """Placeholder art for the synthetic catalogue.

        `sieve demo` is meant to be evaluable with no network at all, and a
        wall of broken images is not an evaluation. Real videos always use the
        instance's own thumbnail proxy; this only ever answers for demo ids.
        """
        if providers.playable(video_id):
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
            "playable_videos": db.playable_count(),
            "last_sync": ingestor.last_sync,
            "last_pull": ingestor.last_result,
            "worker": ingestor.status,
            "fetch": {**ingestor.pull_state(), "has_fetched": ingestor.has_fetched(),
                      "has_sources": ingestor.has_sources()},
            "pulls": api.budget.status(),
            "llm": cfg.llm_provider,
            "instances": cfg.instances,
            "backend": api.status(),
            "youtube_bot_check": ytauth.status(db),
        }

    @app.post("/api/sync", summary="Pull recent videos now, then rescore and rederive", tags=["api"])
    def sync(deep: bool = False):
        """Everything a background sync does, now. `deep=true` also searches
        at least eight terms and pulls every starter channel for your topics —
        what the empty homepage's button asks for. Counts as a pull you asked
        for, so the pull limit applies only if it is set to count those."""
        counts = ingestor.sync(deep=deep)
        ingestor.score_pending(limit=100)
        channel_policy.recompute_affinity(db)
        interest_store.derive_from_history(db)
        return {"ok": True, **counts}

    def _effect() -> dict[str, int]:
        """What the current settings do to the catalogue, right now."""
        # No community data: a save must never wait on SponsorBlock or DeArrow.
        d = ranking.recommend(db, settings(), community=None, record=False).diagnostics
        return {"on_page": d["shown"], "pass": d["pool"], "filtered": d["rejected_total"]}

    @app.put("/api/videos/{video_id}/scores", summary="Correct a video's scores", tags=["api"])
    async def correct_scores(video_id: str, request: Request):
        """`{"education": 80, "clickbait": null}`: set your own value for any
        score (0–100), or null to go back to the engine's. The video uses your
        value straight away; all your corrections then retrain the model that
        adjusts similar videos, in the background."""
        if db.get_video(video_id) is None:
            raise HTTPException(404, "no such video in the catalogue")
        body = await _json_or_empty(request)
        if not body:
            raise HTTPException(400, 'send {"<score>": value}, e.g. {"education": 80}')
        try:
            for axis, value in body.items():
                corrections.set_override(db, video_id, axis, None if value is None else float(value))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        ingestor.rescore_ids([video_id])
        ingestor.retrain_corrections_async()
        return {"ok": True, "overrides": corrections.overrides_for(db, [video_id]).get(video_id, {})}

    @app.post("/api/reevaluate", summary="Re-check every video against your current settings",
              tags=["api"])
    def reevaluate():
        """Filters and ranking re-run on every page load already. This redoes
        what is otherwise updated slowly in the background: every video's
        scores, channel quality and affinity, your interests and the learned
        model; and it forgets which videos were already shown, so nothing is
        held back as seen. Then reports what passes."""
        rescored = ingestor.rescore_all()
        channel_policy.recompute_affinity(db)
        channel_policy.recompute_quality(db)
        interest_store.derive_from_history(db)
        learner_mod.train_from_events(db)
        forgotten = db.execute("DELETE FROM impressions").rowcount
        return {"ok": True, "rescored": rescored, "impressions_forgotten": forgotten, **_effect()}

    @app.post("/api/fetch", summary="Find videos for your topics now", tags=["api"],
              openapi_extra=documented(FetchBody, {"topics": ["science", "history"]}))
    async def fetch(request: Request):
        """Pulls each chosen topic's starter channels and searches for the
        topics and your own phrases, then scores what arrived. Never runs by
        itself: this, the Fetch buttons and `sieve fetch` are the only ways.
        `topics` defaults to the ones chosen under Controls, Finding videos.
        Counts as a pull you asked for."""
        body = await _json_or_empty(request)
        topics = body.get("topics")
        if topics is not None and not isinstance(topics, list):
            raise HTTPException(400, "topics must be a list of topic keys")
        chosen = [str(t) for t in topics] if topics is not None else None

        def work() -> dict:
            counts = ingestor.fetch(chosen)
            ingestor.score_pending(limit=300, fetch_transcripts=False)
            channel_policy.recompute_quality(db)
            return counts

        try:
            # In a worker thread: run on the event loop, a fetch blocked every
            # other request — including the progress bar's — until it finished.
            counts = await run_in_threadpool(work)
        except actions.ActionError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, **counts}

    @app.post("/api/catalogue/reset-pulled", tags=["api"],
              summary="Delete the videos Sieve found for you, and start finding over")
    async def reset_pulled(request: Request):
        """Requires `"confirm": "reset"`. Keeps your subscriptions' and
        playlists' videos and anything you watched, opened, rated or hid.
        Writes a backup first unless `"backup": false`."""
        body = await _json_or_empty(request)
        try:
            result = actions.reset_pulled(db, str(body.get("confirm", "")), bool(body.get("backup", True)))
        except actions.ActionError as exc:
            raise HTTPException(400, str(exc)) from None
        ingestor.last_result = {}
        return {"ok": True, **result}

    @app.post("/api/pulls/reset", summary="Forget recorded pulls, so the pull limit starts from zero",
              tags=["api"])
    def pulls_reset():
        return {"ok": True, "forgotten": actions.reset_pull_counter(db)}

    @app.post("/api/backups", summary="Back up the database now", tags=["api"])
    def backup_now():
        path = backups.create(db, "manual")
        return {"ok": True, "backup": path.name if path else None}

    @app.delete("/api/backups/{name}", summary="Delete one backup", tags=["api"])
    def backup_delete(name: str):
        if not backups.delete(db, name):
            raise HTTPException(404, f"no backup called {name!r}")
        return {"ok": True}

    @app.post("/api/backups/{name}/restore", summary="Roll back to a backup, in place", tags=["api"],
              openapi_extra=documented(RestoreBody, {"confirm": "restore"}))
    async def backup_restore(name: str, request: Request):
        """Requires `"confirm": "restore"`. The current state is backed up
        first, so a restore can itself be undone. No restart needed."""
        body = await _json_or_empty(request)
        try:
            result = backups.restore(db, name, str(body.get("confirm", "")))
        except backups.BackupError as exc:
            raise HTTPException(404 if "no backup called" in str(exc) else 400, str(exc)) from None
        ingestor.last_result = {}
        return {"ok": True, **result}

    @app.get("/api/scales/{key}", summary="What a slider's numbers mean", tags=["api"])
    def scale(key: str, value: float | None = None):
        """For a score (clickbait, education…): what each range means, real
        titles from your catalogue near `value`, and how much of the catalogue
        is above and below it. For duration, views and subscribers: your
        catalogue's spread. The "?" beside each slider shows this."""
        try:
            return scales.describe(db, key, value)
        except KeyError:
            raise HTTPException(404, f"no scale called {key!r}") from None

    @app.get("/api/fetch/progress", summary="What a running fetch or sync is doing", tags=["api"])
    def fetch_progress():
        """`{"running", "kind": "fetch"|"sync", "done", "total", "label"}`. The
        progress bar polls this while a fetch or sync runs."""
        return ingestor.progress

    # ---------------------------------------------------------- library

    @app.get("/library", response_class=HTMLResponse, tags=["pages"])
    def library_page(request: Request, q: str = "", unwatched: bool = False,
                     min_duration: str = "", max_duration: str = ""):
        # A form's empty number box arrives as "", which an int parameter
        # rejects with an error page; blank means "any".
        def number(text: str) -> int:
            try:
                return max(0, int(float(text)))
            except ValueError:
                return 0
        min_duration, max_duration = number(min_duration), number(max_duration)
        return page(request, "library.html", q=q, unwatched=unwatched,
                    min_duration=min_duration, max_duration=max_duration,
                    results=library.search(db, q, unwatched=unwatched, min_duration=min_duration,
                                           max_duration=max_duration) if q else [],
                    downloads_list=downloads.listing(db), can_download=downloader.available(),
                    alerts=notify.unseen(db, 30), alerted=db.query(
                        "SELECT channel_id, name FROM channel_prefs WHERE alert = 1 ORDER BY name"),
                    digest=db.get_setting("digest_last", {}) or {},
                    note_count=db.scalar("SELECT COUNT(*) FROM notes", default=0),
                    export_formats=library.FORMATS)

    @app.get("/api/library/search", summary="Search your own catalogue by meaning", tags=["api"])
    def library_search(q: str, unwatched: bool = False, min_duration: int = 0,
                       max_duration: int = 0, channel: str = "", limit: int = 40):
        """Ranks every scored video by similarity to `q`, expanded once with
        what its best matches share, so related wording matches too. No
        network."""
        return {"results": library.search(db, q, limit=max(1, min(200, limit)), unwatched=unwatched,
                                          min_duration=min_duration, max_duration=max_duration,
                                          channel=channel)}

    # ------------------------------------------------------------ notes

    @app.get("/api/videos/{video_id}/notes", summary="Your notes on a video", tags=["api"])
    def notes_list(video_id: str):
        return {"notes": library.notes_for(db, video_id)}

    @app.post("/api/videos/{video_id}/notes", summary="Add a note, optionally at a moment", tags=["api"])
    async def notes_add(video_id: str, request: Request):
        """`{"text": "...", "at_second": 125}`; leave out at_second for a note
        about the whole video."""
        body = await _json_or_empty(request)
        at = body.get("at_second")
        try:
            note_id = library.add_note(db, video_id, str(body.get("text", "")),
                                       None if at in (None, "") else int(at))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, "id": note_id}

    @app.delete("/api/notes/{note_id}", summary="Delete a note", tags=["api"])
    def notes_delete(note_id: int):
        if not library.delete_note(db, note_id):
            raise HTTPException(404, "no such note")
        return {"ok": True}

    @app.get("/api/export/notes", summary="Export notes to Obsidian, Logseq or Readwise", tags=["api"])
    def notes_export(format: str = "obsidian", watched: bool = False):
        """Obsidian or Logseq: a zip of Markdown pages, one per video, with
        timestamp links. Readwise: a CSV of highlights. `watched=true` also
        includes every video you watched at least half of."""
        try:
            content, media, name = library.export(db, format, include_watched=watched)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return Response(content, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # -------------------------------------------------------- downloads

    @app.post("/api/downloads", summary="Save a video for offline viewing", tags=["api"])
    async def downloads_add(request: Request):
        """`{"video_id": "...", "quality": "720"}` (360, 480, 720 or 1080).
        Downloads run one at a time in the background; needs yt-dlp."""
        body = await _json_or_empty(request)
        vid = str(body.get("video_id", ""))
        if len(vid) != 11:
            raise HTTPException(400, "video_id must be a YouTube video id")
        try:
            row = downloads.queue(db, vid, str(body.get("quality", "720")))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        downloader.wake()
        return {"ok": True, "download": row,
                "note": None if downloader.available() else "needs yt-dlp: pip install 'sieve[youtube]'"}

    @app.get("/api/downloads", summary="Saved and queued videos", tags=["api"])
    def downloads_list():
        return {"downloads": downloads.listing(db), "available": downloader.available()}

    @app.delete("/api/downloads/{video_id}", summary="Delete a saved video", tags=["api"])
    def downloads_delete(video_id: str):
        if not downloads.remove(db, video_id):
            raise HTTPException(404, "not downloaded")
        return {"ok": True}

    @app.get("/media/{video_id}", tags=["pages"])
    def media(video_id: str, request: Request):
        """A saved video, with byte ranges: a <video> element seeks by asking
        for a range, and the Starlette the Android build pins (0.37) ignores
        Range headers, so seeking and chapters broke there. Handled here."""
        path = downloads.local_file(db, video_id)
        if path is None:
            raise HTTPException(404, "not downloaded")
        size = path.stat().st_size
        media_type = "video/webm" if path.suffix == ".webm" else "video/mp4"
        header = request.headers.get("range", "")
        match = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
        if not match or not (match.group(1) or match.group(2)):
            return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
        first, last = match.groups()
        if first:
            start, end = int(first), min(int(last) if last else size - 1, size - 1)
        else:   # "bytes=-500": the last 500 bytes
            start, end = max(0, size - int(last)), size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

        def chunks():
            with path.open("rb") as handle:
                handle.seek(start)
                left = end - start + 1
                while left > 0:
                    block = handle.read(min(1 << 16, left))
                    if not block:
                        break
                    left -= len(block)
                    yield block

        return StreamingResponse(chunks(), status_code=206, media_type=media_type, headers={
            "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1)})

    # ---------------------------------------------- alerts and digest

    @app.post("/api/channels/{channel_id}/alert", summary="Alerts for a channel's new uploads", tags=["api"])
    async def channel_alert(channel_id: str, request: Request):
        """`{"on": true}`. Existing videos count as seen; you hear about new ones."""
        body = await _json_or_empty(request)
        name = db.scalar("SELECT author FROM videos WHERE author_id = ? LIMIT 1", (channel_id,), "") or ""
        notify.set_alert(db, channel_id, bool(body.get("on", True)), name)
        return {"ok": True, "on": bool(body.get("on", True))}

    @app.get("/api/alerts", summary="New uploads from alerted channels", tags=["api"])
    def alerts_list():
        return {"alerts": notify.unseen(db, 50)}

    @app.post("/api/alerts/seen", summary="Mark every alert as seen", tags=["api"])
    def alerts_seen():
        return {"ok": True, "marked": notify.mark_seen(db)}

    @app.get("/api/digest", summary="The latest digest", tags=["api"])
    def digest_get():
        return db.get_setting("digest_last", {}) or {}

    @app.post("/api/digest", summary="Write a digest now (and send it, if a webhook is set)", tags=["api"])
    def digest_now(request: Request):
        digest = notify.build_digest(db)
        notify.deliver_pending(db, str(request.base_url).rstrip("/"))
        return {"ok": True, **db.get_setting("digest_last", digest)}

    @app.post("/api/notify/test", summary="Send a test message to your webhook", tags=["api"])
    def notify_test():
        if not notify.settings(db).get("webhook_url"):
            raise HTTPException(400, "set a webhook address first")
        if not notify.send(db, "Sieve", "Test message: notifications work."):
            raise HTTPException(502, "the webhook did not accept the message")
        return {"ok": True}

    @app.get("/feeds/{name}.xml", tags=["pages"])
    def feed(name: str, request: Request):
        if name not in ("alerts", "digest"):
            raise HTTPException(404, "feeds: alerts, digest")
        return Response(notify.rss(db, name, str(request.base_url).rstrip("/")),
                        media_type="application/rss+xml")

    @app.get("/userscript/sieve.user.js", tags=["pages"])
    def userscript(request: Request):
        """The watch-elsewhere userscript, with this server's address in it."""
        base = str(request.base_url).rstrip("/")
        from urllib.parse import urlparse

        script = (HERE / "static" / "sieve.user.js").read_text()
        invidious = providers.invidious_base(settings(), cfg)
        host = urlparse(invidious).hostname
        match = f"// @match        {urlparse(invidious).scheme}://{host}/*" if host else ""
        script = (script.replace("__SIEVE_URL__", base).replace("__SIEVE_HOST__", urlparse(base).hostname or "localhost")
                  .replace("// __INVIDIOUS_MATCH__", match))
        return Response(script, media_type="text/javascript")

    # ------------------------------------------------- notices, problems

    @app.post("/api/notices/{key}/dismiss", summary="Dismiss a notice or banner", tags=["api"])
    def notice_dismiss(key: str):
        """Hides that notice for good (a problem banner: until it happens again)."""
        dismissed = list(db.get_setting("dismissed_notices", []) or [])
        if key not in dismissed:
            dismissed.append(key[:120])
        db.set_setting("dismissed_notices", dismissed[-200:])
        return {"ok": True}

    @app.get("/api/problems", summary="Recent backend problems, explained", tags=["api"])
    def problems_list():
        return {"problems": problems.recent(db)}

    # ------------------------------------------------------ homepage search

    @app.post("/api/homepage/search", summary="Find videos on your homepage", tags=["api"])
    async def homepage_search(request: Request):
        """`{"q": "...", "ids": [the homepage's video ids, in page order]}`.
        Returns the matching ids in the order given. Only searches those
        videos; changes nothing — not the ranking, scores or feedback. Mode:
        the homepage.search_mode setting, or `"mode": "ai" | "math"`. AI falls
        back to the mathematical search if it is unavailable or fails."""
        body = await json_object(request)
        q = str(body.get("q", "")).strip()[:300]
        ids = [str(i) for i in body.get("ids", []) if isinstance(i, str)][:200]
        mode = body.get("mode") or settings()["homepage"].get("search_mode", "ai")
        if not q or not ids:
            return {"ids": ids if not q else [], "mode": "none"}
        note = ""
        if mode == "ai":
            try:
                matched = await run_in_threadpool(_ai_search, llm.effective(cfg, db), db, q, ids)
                return {"ids": matched, "mode": "ai"}
            except llm.LLMError as exc:
                note = f"AI search unavailable ({exc}); used the mathematical search"
        return {"ids": library.match_subset(db, q, ids), "mode": "math", "note": note}

    # --------------------------------------------- "tell Sieve why" (#12)

    @app.post("/api/videos/{video_id}/explain", summary="Tell Sieve in words why (not) this video",
              tags=["api"])
    async def video_explain(video_id: str, request: Request):
        """`{"text": "too much drama, I only want the technical parts"}`. An
        AI (or, without one, keyword matching) turns it into feedback — Less
        or More, topics to avoid or prefer, shorter or deeper — applied like
        the buttons: to similar videos. Unlike homepage search, this does
        change your recommendations."""
        body = await json_object(request)
        text = str(body.get("text", "")).strip()[:1000]
        row = db.get_video(video_id)
        if not text:
            raise HTTPException(400, "say why, in a sentence or two")
        if row is None:
            raise HTTPException(404, "no such video in the catalogue")
        topics_row = db.one("SELECT topics FROM scores WHERE video_id = ?", (video_id,))
        topics = json.loads(topics_row["topics"]) if topics_row and topics_row["topics"] else []
        via = "keywords"
        try:
            understood = await run_in_threadpool(_ai_explain, llm.effective(cfg, db), dict(row), topics, text)
            via = "ai"
        except llm.LLMError:
            understood = _keyword_explain(dict(row), topics, text)
        verdict = understood["verdict"]
        result = give_feedback(video_id, verdict, f"why: {text}", toggle=False)
        for kind in understood["adjust"]:
            give_feedback(video_id, kind, f"why: {text}", toggle=False)
        now = int(time.time())
        for tag, weight in [(t, -0.3) for t in understood["avoid"]] + [(t, 0.3) for t in understood["prefer"]]:
            db.execute("INSERT INTO interests(tag, weight, confidence, origin, updated_at) VALUES(?,?,0.5,'feedback',?) "
                       "ON CONFLICT(tag) DO UPDATE SET weight = MAX(-1, MIN(1, interests.weight + ?)), "
                       "updated_at = excluded.updated_at", (tag, weight, now, weight))
        return {"ok": True, "via": via, "verdict": verdict, "avoid": understood["avoid"],
                "prefer": understood["prefer"], "adjust": understood["adjust"],
                "summary": understood["summary"], "state": result.get("state")}

    # -------------------------------------------------- full score range

    @app.get("/api/scales/{key}/range", summary="Every score on one axis, and the videos at one",
             tags=["api"])
    def scale_range(key: str, at: int | None = None):
        try:
            return scales.full_range(db, key, at)
        except KeyError:
            raise HTTPException(404, f"no score called {key!r}") from None

    # ----------------------------------------------------- AI connection

    @app.get("/api/ai", summary="The AI connection", tags=["api"])
    def ai_status():
        live = llm.effective(cfg, db)
        return {"provider": live.llm_provider, "model": live.llm_model, "base_url": live.llm_base_url,
                "has_key": bool(live.llm_api_key), "available": llm.available(live)}

    @app.post("/api/ai/key", summary="Store the AI provider's API key", tags=["api"])
    async def ai_key(request: Request):
        """Kept apart from your settings: never in profile exports or API answers."""
        body = await json_object(request)
        key = str(body.get("api_key", "")).strip()
        if not key:
            raise HTTPException(400, "send {\"api_key\": \"...\"}")
        db.set_setting("ai_secret", {"api_key": key[:400]})
        return {"ok": True}

    @app.delete("/api/ai/key", summary="Forget the AI provider's API key", tags=["api"])
    def ai_key_delete():
        db.set_setting("ai_secret", {})
        return {"ok": True}

    @app.post("/api/ai/test", summary="Check the AI connection answers", tags=["api"])
    async def ai_test():
        live = llm.effective(cfg, db)
        try:
            answer = await run_in_threadpool(
                llm.complete, live, "Answer with exactly: OK", "Say OK.", want_json=False, max_tokens=10)
        except llm.LLMError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"ok": True, "message": f"{live.llm_provider} ({live.llm_model}) answered: {answer.strip()[:40]}"}

    @app.post("/api/ai/tune", summary="Let the AI tune scores, targets and weights now", tags=["api"])
    async def ai_tune_now():
        """What the automatic tuning does each hour, now — including the
        daily targets-and-weights step. Works whether or not it is switched on."""
        log = await run_in_threadpool(autotune.run, cfg, db, True)
        if log["error"]:
            raise HTTPException(502, log["error"])
        ingestor.retrain_corrections_async()
        return {"ok": True, **log}

    @app.post("/api/ai/tune/undo", summary="Undo everything the AI tuning changed", tags=["api"])
    def ai_tune_undo():
        """Removes every AI-set score and restores the settings from before its
        last change. Your own corrections stay."""
        result = autotune.undo(db)
        ingestor.retrain_corrections_async()
        return {"ok": True, **result}

    @app.post("/api/source/cookies", summary="Sign yt-dlp in: upload YouTube cookies", tags=["api"])
    async def cookies_upload(file: UploadFile):
        """A cookies.txt (Netscape format) with youtube.com cookies, for when
        YouTube asks yt-dlp to prove it is not a bot. Stored beside the
        database, readable only by you, never included in backups."""
        try:
            ytauth.save_cookies(cfg.data_dir, await file.read())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        ytauth.clear(db)             # try again straight away, now signed in
        downloader.wake()
        return {"ok": True, "message": "Cookies saved. yt-dlp now signs in with them."}

    @app.delete("/api/source/cookies", summary="Forget the uploaded YouTube cookies", tags=["api"])
    def cookies_delete():
        return {"ok": True, "removed": ytauth.remove_cookies(cfg.data_dir)}

    @app.get("/api/pulls", summary="Pulls made to YouTube or Invidious, and the limit", tags=["api"])
    def pulls_status():
        """How many requests left this machine in the limit's window, split by
        kind and by who asked — you, or Sieve by itself — and when the next
        one is free if the limit is spent."""
        return {**api.budget.status(), "summary": api.budget.summary(),
                "last_pull": ingestor.last_result}

    @app.post("/api/catalogue/remove-demo", tags=["api"],
              summary="Delete the synthetic demo catalogue and what was learned from it")
    def remove_demo_catalogue():
        """Removes the invented videos, their made-up watch history, the demo
        channels and subscriptions, then rebuilds interests and the model from
        what remains. Real data is untouched."""
        removed = demo.remove_demo(db)
        if pull_auto():
            ingestor.request_pull()
        return {"ok": True, "removed": removed["videos"], "history": removed.get("history", 0)}

    def pull_auto() -> bool:
        return bool(settings().get("pull", {}).get("auto", True))

    @app.on_event("startup")
    def _startup() -> None:
        if start_worker:
            ingestor.start()
            downloader.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        ingestor.stop()
        downloader.stop()
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


def _resume_point(db: Database, video_id: str) -> int | None:
    """Seconds to start at, from the latest real progress report, if any."""
    row = db.one(
        "SELECT h.progress, v.duration FROM history h JOIN videos v ON v.id = h.video_id "
        "WHERE h.video_id = ? ORDER BY h.watched_at DESC LIMIT 1", (video_id,))
    if row is None:
        return None
    return providers.resume_at(float(row["progress"] or 0), int(row["duration"] or 0))


# The pages you use every day; the tools for understanding and tuning Sieve
# sit under "More", so the header stays readable.
NAV_MAIN = [("/", "Home"), ("/library", "Library"), ("/sift", "Sift"), ("/channels", "Channels"),
            ("/settings", "Controls")]
NAV_MORE = [("/rules", "Rules"), ("/brief", "Brief"), ("/funnel", "Funnel"), ("/history", "History"),
            ("/debugger", "Debugger"), ("/analytics", "Analytics")]


def _nav(path: str) -> list[dict]:
    main = [{"href": h, "label": label, "active": path == h, "more": False} for h, label in NAV_MAIN]
    more = [{"href": h, "label": label, "active": path == h, "more": True} for h, label in NAV_MORE]
    return main + more


MODE_LABELS = {
    "blend": "Blend of sources", "playlist": "One playlist", "continue": "Unfinished only",
    "subscriptions": "Subscriptions only",
}


def _empty_state(db: Database, ingestor: ingest.Ingestor, api: Upstream,
                 result: ranking.Result, settings: dict) -> dict[str, Any]:
    """What an empty homepage says, and what it offers to do about it.

    `kind` picks the wording in home.html: start (nothing to show yet — the
    Fetch plan and button), limit (the last attempt hit the pull limit),
    unreachable (the last attempt found nothing to talk to), filtered (your
    filters removed everything) or nomatch."""
    stats = db.stats()
    if stats["videos"] == 0 or (not db.playable_count() and not demo.demo_count(db)):
        last = ingestor.last_result or {}
        if last.get("stopped") and not last.get("seen"):
            if "request limit" in last["stopped"]:
                return {"kind": "limit", "detail": api.budget.summary()}
            if not last["stopped"].startswith("nothing to sync yet"):
                return {"kind": "unreachable", "detail": last["stopped"]}
        return {"kind": "start", "fetched_before": ingestor.has_fetched()}
    d = result.diagnostics
    if d.get("new_videos_at"):
        return {"kind": "waiting", "at": d["new_videos_at"]}
    if d["rejected_total"] > 0 and d["rejected"]:
        top = d["rejected"][0]
        return {"kind": "filtered", "reason": top["reason"], "count": top["count"],
                "fill": settings["homepage"].get("fill", "catalogue")}
    return {"kind": "nomatch", "mode": settings["homepage"].get("mode", "blend")}


async def _json_or_empty(request: Request) -> dict:
    try:
        body = await json_object(request)
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _fetch_plan(settings: dict, api: Upstream) -> dict[str, Any]:
    """What pressing Fetch would do, in words the button can show beside it."""
    pull = settings.get("pull", {})
    topics = [starter.TOPICS[t] for t in pull.get("topics") or [] if t in starter.TOPICS]
    channels = len(starter.channels_for([t.key for t in topics])) if pull.get("starter_channels", True) else 0
    custom = [str(t) for t in pull.get("custom_topics") or [] if str(t).strip()]
    searches = 0
    if api.can_search():
        topic_phrases = [t for t in starter.searches_for([t.key for t in topics]) if t not in custom]
        wanted = int(settings.get("compute", {}).get("discover_terms", 4))
        searches = len(custom[:12]) + len(topic_phrases[:wanted])
    budget = api.budget.status()
    return {"topics": [t.label.lower() for t in topics] + custom, "channels": channels,
            "searches": searches, "requests": channels + searches,
            "remaining": budget["remaining"] if budget["enabled"] and (budget["counts_manual"]) else None,
            "ready": bool(topics or custom)}


AI_SEARCH_PROMPT = (
    "You help someone find videos on their own homepage. You get a search and a numbered list of "
    "videos (title, channel). Answer JSON only: {\"matches\": [numbers of the videos that match]}. "
    "Match by meaning, not only words. Order does not matter.")


def _ai_search(cfg: Config, db: Database, q: str, ids: list[str]) -> list[str]:
    rows = {r["id"]: r for r in db.query(
        f"SELECT id, title, author FROM videos WHERE id IN ({','.join('?' * len(ids))})", ids)}
    listing = "\n".join(f"{n}. {rows[i]['title']} — {rows[i]['author']}" for n, i in enumerate(ids) if i in rows)
    parsed = llm.complete_json(cfg, AI_SEARCH_PROMPT, f"Search: {q}\n\nVideos:\n{listing}")
    numbers = {int(n) for n in parsed.get("matches", []) if str(n).lstrip("-").isdigit()}
    return [vid for n, vid in enumerate(ids) if n in numbers]


AI_EXPLAIN_PROMPT = (
    "Someone explains why they do (or do not) want a video, or content like it, recommended. Answer "
    "JSON only: {\"verdict\": \"less\" or \"more\", \"avoid\": [up to 5 short lowercase topics to show "
    "less of], \"prefer\": [up to 5 to show more of], \"adjust\": [any of \"shorter\", \"deeper\", "
    "\"higher_quality\"], \"summary\": \"one plain sentence of what you understood\"}.")


def _ai_explain(cfg: Config, video: dict, topics: list[str], text: str) -> dict:
    parsed = llm.complete_json(cfg, AI_EXPLAIN_PROMPT,
                               f"Video: {video.get('title')} — channel {video.get('author')}\n"
                               f"Its topics: {', '.join(topics[:8])}\n\nWhat they said: {text}")
    return _clean_understanding(parsed)


def _clean_understanding(parsed: dict) -> dict:
    def topics(value: Any) -> list[str]:
        return [str(t).strip().lower()[:40] for t in (value or []) if str(t).strip()][:5] if isinstance(value, list) else []
    adjust = [a for a in (parsed.get("adjust") or []) if a in ("shorter", "deeper", "higher_quality")]
    return {"verdict": "more" if parsed.get("verdict") == "more" else "less",
            "avoid": topics(parsed.get("avoid")), "prefer": topics(parsed.get("prefer")),
            "adjust": adjust, "summary": str(parsed.get("summary", ""))[:300]}


_POSITIVE = ("love", "great", "more of", "more like", "want more", "excellent", "brilliant", "perfect")
_ADJUST_WORDS = {"shorter": ("too long", "shorter", "drags", "padded", "rambling"),
                 "deeper": ("too basic", "superficial", "shallow", "deeper", "more technical", "surface level"),
                 "higher_quality": ("low quality", "poorly made", "bad audio", "better made")}


def _keyword_explain(video: dict, topics: list[str], text: str) -> dict:
    """Without an AI: words from what you said that also describe the video
    become topics to avoid (or prefer), and a few phrases set the kind."""
    lowered = text.lower()
    verdict = "more" if any(p in lowered for p in _POSITIVE) and "not" not in lowered.split()[:3] else "less"
    said = set(T.content_tokens(text))
    about = set(T.content_tokens(video.get("title") or "")) | set(topics)
    named = [t for t in topics if t in said] + sorted((said & about) - set(topics))
    if not named:
        named = [w for w in T.content_tokens(text) if len(w) > 3][:3]
    adjust = [kind for kind, phrases in _ADJUST_WORDS.items() if any(ph in lowered for ph in phrases)]
    summary = (f"{'More' if verdict == 'more' else 'Less'} like this"
               + (f", {'more' if verdict == 'more' else 'less'} about {', '.join(named[:3])}" if named else "")
               + (f"; {', '.join(adjust)}" if adjust else "") + ".")
    return {"verdict": verdict, "avoid": [] if verdict == "more" else named[:5],
            "prefer": named[:5] if verdict == "more" else [], "adjust": adjust, "summary": summary}


TOPIC_NUDGE = 0.12


def _nudge_topics(db: Database, video_id: str, kind: str, undo: bool = False) -> None:
    """More / Less shift the video's main topics in your interests, so videos
    about the same things rise or fall — not just this one. Undone exactly
    when the choice is taken back or switched."""
    row = db.one("SELECT topics FROM scores WHERE video_id = ?", (video_id,))
    topics = json.loads(row["topics"]) if row and row["topics"] else []
    delta = (TOPIC_NUDGE if kind == "more" else -TOPIC_NUDGE) * (-1 if undo else 1)
    now = int(time.time())
    for tag in topics[:3]:
        db.execute(
            "INSERT INTO interests(tag, weight, confidence, origin, updated_at) VALUES(?, ?, 0.3, 'feedback', ?) "
            "ON CONFLICT(tag) DO UPDATE SET weight = MAX(-1, MIN(1, interests.weight + ?)), "
            "updated_at = excluded.updated_at", (tag, max(-1, min(1, delta)), now, delta))


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
    """1234 -> 1.2K, 999999 -> 1M (not "1000K"), 12_500_000 -> 12.5M."""
    value = int(value or 0)
    units = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for index, (limit, suffix) in enumerate(units):
        if value >= limit:
            text = f"{value / limit:.1f}"
            if float(text) >= 1000 and index > 0:
                limit, suffix = units[index - 1]
                text = f"{value / limit:.1f}"
            return (text[:-2] if text.endswith(".0") else text) + suffix
    return str(value)


def _timecode(seconds: Any) -> str:
    """A moment in a video: 0 is 0:00, unlike a duration, where 0 is unknown."""
    seconds = int(seconds or 0)
    h, rest = divmod(seconds, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _filesize(n: Any) -> str:
    n = int(n or 0)
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} bytes"


def _sentence(text: Any) -> str:
    """A message written for logs and the API ("no config file found, so…"),
    as a sentence for a page: capital first letter, full stop at the end."""
    text = str(text or "").strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?:)\"”" else text + "."


def _fmt_until(timestamp: Any) -> str:
    """A moment in the future, in words: "in 4 minutes", "now"."""
    delta = int(timestamp or 0) - int(time.time())
    if delta <= 30:
        return "now"
    for limit, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if delta >= limit:
            n = round(delta / limit)
            return f"in {n} {unit}{'' if n == 1 else 's'}"
    return f"in {delta} seconds"


def _fmt_ago(timestamp: Any) -> str:
    timestamp = int(timestamp or 0)
    if not timestamp:
        return "never"
    delta = max(0, int(time.time()) - timestamp)
    for limit, unit in ((86400 * 365, "y"), (86400 * 30, "mo"), (86400, "d"), (3600, "h"), (60, "m")):
        if delta >= limit:
            return f"{delta // limit}{unit} ago"
    return "just now"
