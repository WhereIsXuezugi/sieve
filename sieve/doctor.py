"""Diagnostics for an empty or wrong-looking homepage.

Self-hosted software fails quietly: an instance stops answering, a filter is set
to something impossible, nothing has been scored yet. Each of those looks the
same from outside — a blank page — so this module names them.

Shared by `sieve doctor`, `GET /api/doctor`, and the debugger page.
"""

from __future__ import annotations

from .config import Config
from .db import Database
from .invidious import Invidious


def doctor_report(cfg: Config, db: Database, api: Invidious, quick: bool = False) -> dict:
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
    if stats["videos"] == 0:
        problems.append("catalogue is empty — import subscriptions and run `sieve sync`")
    elif stats["scored"] < stats["videos"]:
        notes.append(f"{stats['videos'] - stats['scored']} videos still need scoring "
                     f"(`sieve score`)")
    if stats["history"] == 0:
        notes.append("no watch history, so interests and channel affinity are empty "
                     "(`sieve import history FILE`)")

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

    # Reachability.
    services: dict[str, str] = {}
    for instance in cfg.instances:
        try:
            api.get("/api/v1/stats", ttl=0)
            services[instance] = "ok"
        except Exception as exc:
            services[instance] = f"unreachable: {type(exc).__name__}"
    if all(value != "ok" for value in services.values()):
        problems.append("no Invidious instance answered; the homepage will still render "
                        "from the local catalogue, but nothing new will arrive")

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
