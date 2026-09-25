"""Diagnostics for an empty or wrong-looking homepage.

Self-hosted software fails quietly: an instance stops answering, a filter is set
to something impossible, nothing has been scored yet. Each of those looks the
same from outside — a blank page — so this module names them.

Shared by `sieve doctor`, `GET /api/doctor`, and the debugger page.
"""

from __future__ import annotations

from .config import Config
from .db import Database
from .upstream import Upstream


def doctor_report(cfg: Config, db: Database, api: Upstream, quick: bool = False) -> dict:
    """Check the things that silently produce an empty homepage.

    Self-hosted software fails quietly: an instance stops answering, a filter is
    set to something impossible, nothing has been scored yet. Each of those
    looks identical from the outside — a blank page — so they get named here
    rather than left to guess at.
    """
    from .config import resolve_settings

    stats = db.stats()
    settings = resolve_settings(db.get_setting("settings", {}))
    report: dict[str, object] = {
        "config": {
            "file": cfg.source or None,
            "data_dir": str(cfg.data_dir),
            "port": cfg.port,
            "instances": cfg.instances,
        },
        "database": {"path": str(cfg.db_path), **stats},
        "problems": [],
        "notes": [],
    }
    problems: list[str] = report["problems"]  # type: ignore[assignment]
    notes: list[str] = report["notes"]  # type: ignore[assignment]

    if not cfg.source:
        notes.append("no config file found, so every setting is a default; see "
                     "docs/configuration.md for where Sieve looks")
    from . import providers

    stored = db.get_setting("settings", {}) or {}
    unconfirmed_playback = providers.looks_unconfigured(settings, cfg, stored)
    if unconfirmed_playback:
        notes.append(
            "no player has been chosen and the configured Invidious "
            f"({providers.invidious_base(settings, cfg)}) is the shipped local guess, so videos "
            "play in Sieve's own player; choose another under Controls, Playback")
    if stats["videos"] and not db.scalar(
            "SELECT COUNT(*) FROM videos WHERE length(id) = 11", default=0):
        notes.append("the catalogue holds only demo videos, which cannot be played anywhere; "
                     "real videos replace them on the next pull, or `sieve demo remove` clears "
                     "them now")
    report["backend"] = api.status()
    if api.setting() != "invidious" and not api.youtube.has_ytdlp:
        notes.append("yt-dlp is not installed, so videos fetched from YouTube directly have no "
                     "duration and playlists stop at their latest fifteen; "
                     "pip install 'sieve[youtube]' fixes both")
    pull = settings.get("pull", {})
    budget = api.budget.status()
    report["pulls"] = budget
    if budget["exhausted"]:
        notes.append(f"the pull limit is used up — {api.budget.summary().rstrip('.')} — so "
                     "nothing new arrives until pulls free up")
    state = db.get_setting("pull_state", {}) or {}
    report["first_fetch"] = {"done_at": state.get("first_fetch_at") or None,
                             "failed_attempts": state.get("failed_attempts", 0),
                             "next_try": state.get("next_try") or None}
    if pull.get("auto", True) and not state.get("first_fetch_at") and state.get("failed_attempts"):
        problems.append(f"the first fetch has found nothing in {state['failed_attempts']} attempts; "
                        "the last sync's reason is under Controls, Finding videos, or run "
                        "`sieve sync` to see it")
    from . import ytauth

    bot = ytauth.status(db)
    report["youtube_bot_check"] = bot
    if bot["blocked"]:
        notes.append(ytauth.advice(bot["signed_in"]).rstrip(".") + "; meanwhile yt-dlp is paused and "
                     "Sieve uses feeds and YouTube's pages instead")
    if stats["videos"] == 0:
        if pull.get("auto", True):
            problems.append("catalogue is empty — Sieve is set to find videos by itself, so either "
                            "the first pull has not finished or nothing answered; run `sieve sync "
                            "--deep` to see which")
        else:
            problems.append("catalogue is empty and finding videos is off — turn it on under "
                            "Controls, Finding videos, or import subscriptions and run `sieve sync`")
    elif stats["scored"] < stats["videos"]:
        notes.append(f"{stats['videos'] - stats['scored']} videos still need scoring "
                     f"(`sieve score`)")
    if stats["history"] == 0:
        notes.append("no watch history yet, so interests and channel affinity are empty; they "
                     "build up as you watch, or import it (`sieve import history FILE`)")

    # Filters that can only ever produce nothing.
    f = settings["filters"]
    if f.get("max_duration") and f.get("min_duration", 0) > f["max_duration"]:
        problems.append("min_duration is above max_duration, so nothing can pass")
    if f.get("max_views") and f.get("min_views", 0) > f["max_views"]:
        problems.append("min_views is above max_views, so nothing can pass")
    if f.get("max_subs") and f.get("min_subs", 0) > f["max_subs"]:
        problems.append("min_subs is above max_subs, so nothing can pass")
    if settings["channels"]["whitelist_only"]:
        allowed = db.scalar("SELECT COUNT(*) FROM channel_prefs WHERE listing = 'allow'",
                            default=0)
        if not allowed:
            problems.append("whitelist-only mode is on but no channel is on the allow list")
        else:
            notes.append(f"whitelist-only mode: {allowed} channels can appear")
    if sum(settings["sources"].values()) == 0:
        problems.append("every recommendation source is set to zero")

    if quick:
        return report

    # Reachability, judged against the backend you chose.
    import httpx

    services: dict[str, str] = {}
    backend = api.setting()
    invidious_ok = youtube_ok = False
    if backend in ("auto", "invidious"):
        # Each instance on its own: going through the client would fail over
        # and report every instance as whichever one answered.
        for instance in cfg.instances:
            try:
                httpx.get(f"{instance.rstrip('/')}/api/v1/stats", timeout=4.0).raise_for_status()
                services[f"invidious {instance}"] = "ok"
                invidious_ok = True
            except Exception as exc:
                services[f"invidious {instance}"] = f"unreachable: {type(exc).__name__}"
    if backend in ("auto", "youtube"):
        try:
            # YouTube's own channel: a feed that will always exist.
            httpx.get("https://www.youtube.com/feeds/videos.xml",
                      params={"channel_id": "UCBR8-60-B28hp2BmDPdntcQ"}, timeout=6.0).raise_for_status()
            services["youtube (rss feeds)"] = "ok"
            youtube_ok = True
        except Exception as exc:
            services["youtube (rss feeds)"] = f"unreachable: {type(exc).__name__}"
    if backend == "invidious" and not invidious_ok:
        problems.append("no Invidious instance answered, and Getting videos is set to Invidious "
                        "only; nothing new will arrive. Choose Automatic or YouTube to fall back")
    elif backend == "youtube" and not youtube_ok:
        problems.append("YouTube's feeds did not answer, so nothing new will arrive")
    elif backend == "auto" and not (invidious_ok or youtube_ok):
        problems.append("neither Invidious nor YouTube answered, so nothing new will arrive; "
                        "the homepage still renders from the local catalogue")
    elif backend == "auto" and not invidious_ok:
        notes.append("no Invidious instance answered, so videos come from YouTube directly")

    if settings["dearrow"]["enabled"] or settings["sponsorblock"]["enabled"]:
        import httpx

        for name, url in (("dearrow", cfg.dearrow_url), ("sponsorblock", cfg.sponsorblock_url)):
            try:
                httpx.get(f"{url.rstrip('/')}/api/status", timeout=8.0)
                services[name] = "ok"
            except Exception as exc:
                services[name] = f"unreachable: {type(exc).__name__}"

    from .embed import Embedder

    embedder = Embedder(cfg)
    services[f"embeddings ({embedder.provider})"] = "ok" if embedder.available() else "unavailable"
    embedder.close()
    if embedder.provider != "hashed" and services[f"embeddings ({embedder.provider})"] != "ok":
        problems.append(f"embed_provider is {embedder.provider!r} but the endpoint did not "
                        "answer; scoring will silently fall back to hashed vectors")

    if cfg.llm_provider != "none":
        try:
            from . import llm

            llm.compile_brief(cfg, "probe")
            services[f"llm ({cfg.llm_provider})"] = "ok"
        except Exception as exc:
            services[f"llm ({cfg.llm_provider})"] = f"unreachable: {type(exc).__name__}"

    from .vision import ThumbnailScorer

    scorer = ThumbnailScorer(cfg)
    services["vision"] = "ok" if scorer.available else "not installed (optional)"
    scorer.close()

    report["services"] = services
    report["ok"] = not problems
    return report
