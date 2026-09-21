"""Command line interface.

    sieve serve                      run the web app
    sieve sync [--deep]              refresh the catalogue from Invidious
    sieve score [--all]              score anything unscored
    sieve import subs FILE           import a subscription export
    sieve import history FILE        import a watch-history export
    sieve import playlist ID         pull a playlist
    sieve channel ID --priority 4    set channel policy from a script
    sieve train                      retrain the ranker from history
    sieve affinity                   recompute channel affinity from watch time
    sieve quality                    rescore channels from their own catalogue
    sieve vision                     score thumbnails (needs the 'vision' extra)
    sieve doctor                     diagnose an empty or wrong-looking homepage
    sieve profile export/import      move configurations between machines
    sieve demo                       fill the database with synthetic videos
    sieve prune                      drop stale rows and vacuum
    sieve stats                      what is in the database
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from . import channels as channel_policy
from . import ingest, learner, profiles
from . import interests as interest_store
from .community import CommunityData
from .config import Config
from .db import Database
from .invidious import Invidious


def main(argv: list[str] | None = None) -> int:
    # `sieve stats | head` should not produce a traceback.
    try:
        import signal

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # no SIGPIPE on Windows, and not settable off the main thread

    parser = argparse.ArgumentParser(prog="sieve", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to a TOML config file")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-worker", action="store_true",
                       help="do not run background sync and scoring")

    sync = sub.add_parser("sync", help="refresh the catalogue")
    sync.add_argument("--deep", action="store_true", help="also search your top interests")

    score = sub.add_parser("score", help="score videos")
    score.add_argument("--all", action="store_true", help="rescore everything")
    score.add_argument("--limit", type=int, default=500)
    score.add_argument("--no-transcripts", action="store_true")

    imp = sub.add_parser("import", help="import data")
    imp.add_argument("kind", choices=["subs", "history", "playlist", "channels"])
    imp.add_argument("target", help="file path, or a playlist id")

    channel = sub.add_parser("channel", help="set channel policy")
    channel.add_argument("channel_id")
    channel.add_argument("--priority", type=int, choices=range(-5, 6))
    channel.add_argument("--allow", action="store_true")
    channel.add_argument("--block", action="store_true")
    channel.add_argument("--clear", action="store_true")
    channel.add_argument("--note", default=None)

    sub.add_parser("train", help="retrain the ranker from history and feedback")
    sub.add_parser("affinity", help="recompute channel affinity from watch time")
    sub.add_parser("quality", help="rescore channels from their own catalogue")

    vision_cmd = sub.add_parser(
        "vision", help="score thumbnails for visual NSFW (needs the 'vision' extra)")
    vision_cmd.add_argument("--limit", type=int, default=200)

    doctor = sub.add_parser("doctor", help="check configuration and connectivity")
    doctor.add_argument("--quick", action="store_true")
    sub.add_parser("interests", help="rederive the interest graph")
    sub.add_parser("stats", help="show database counts")
    sub.add_parser("prune", help="drop stale rows and vacuum")

    prof = sub.add_parser("profile", help="export or import a configuration")
    prof.add_argument("action", choices=["export", "import"])
    prof.add_argument("path", nargs="?")

    demo = sub.add_parser("demo", help="populate a synthetic catalogue for trying the controls")
    demo.add_argument("--count", type=int, default=400)

    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    db = Database(cfg.db_path)

    if args.command == "serve":
        import uvicorn

        from .app import create_app

        app = create_app(cfg, start_worker=not args.no_worker)
        uvicorn.run(app, host=args.host or cfg.host, port=args.port or cfg.port, log_level="info")
        return 0

    api = Invidious(cfg, db)
    community = CommunityData(cfg, db)
    ingestor = ingest.Ingestor(cfg, db, api, community)

    try:
        if args.command == "sync":
            print(json.dumps(ingestor.sync(deep=args.deep), indent=2))
            channel_policy.recompute_affinity(db)
            interest_store.derive_from_history(db)

        elif args.command == "score":
            if args.all:
                print(f"rescored {ingestor.rescore_all()} videos")
            else:
                total = 0
                while True:
                    done = ingestor.score_pending(
                        limit=min(50, args.limit - total),
                        fetch_transcripts=not args.no_transcripts,
                    )
                    total += done
                    print(f"\rscored {total}", end="", flush=True)
                    if done == 0 or total >= args.limit:
                        break
                print()

        elif args.command == "import":
            if args.kind == "playlist":
                print(f"imported {ingest.import_playlist(db, api, args.target)} videos")
            elif args.kind == "channels":
                body = json.loads(Path(args.target).read_text())
                print(json.dumps(channel_policy.import_lists(
                    db, body.get("allow", []), body.get("block", []), body.get("priorities", {})
                ), indent=2))
            else:
                payload = json.loads(Path(args.target).read_text())
                if args.kind == "subs":
                    print(f"imported {ingest.import_subscriptions(db, payload)} subscriptions")
                else:
                    print(f"imported {ingest.import_history(db, payload, api)} watch events")
                    channel_policy.recompute_affinity(db)

        elif args.command == "channel":
            if args.clear:
                channel_policy.clear_preference(db, args.channel_id)
                print("cleared")
            else:
                listing = "allow" if args.allow else ("block" if args.block else None)
                result = channel_policy.set_preference(
                    db, args.channel_id, priority=args.priority, listing=listing, note=args.note
                )
                print(json.dumps(result, indent=2))

        elif args.command == "train":
            print(json.dumps(learner.train_from_events(db), indent=2))

        elif args.command == "affinity":
            print(f"updated {channel_policy.recompute_affinity(db)} channels")

        elif args.command == "quality":
            print(f"scored {channel_policy.recompute_quality(db)} channels")

        elif args.command == "vision":
            from . import vision

            print(json.dumps(vision.score_pending(db, cfg, args.limit), indent=2))

        elif args.command == "doctor":
            print(json.dumps(doctor_report(cfg, db, api, quick=args.quick), indent=2))

        elif args.command == "interests":
            print(f"derived {interest_store.derive_from_history(db)} interests")

        elif args.command == "stats":
            print(json.dumps(db.stats(), indent=2))

        elif args.command == "prune":
            print(json.dumps(db.prune(), indent=2))

        elif args.command == "profile":
            if args.action == "export":
                body = profiles.export_profile(db, Path(args.path).stem if args.path else "")
                text = json.dumps(body, indent=2)
                if args.path:
                    Path(args.path).write_text(text)
                    print(f"wrote {args.path}")
                else:
                    print(text)
            else:
                body = json.loads(Path(args.path).read_text())
                print(json.dumps(profiles.import_profile(db, body), indent=2))

        elif args.command == "demo":
            print(json.dumps(seed_demo(db, args.count), indent=2))
    finally:
        api.close()
        community.close()
    return 0


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


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
        "database": {"path": str(cfg.db_path), **stats},
        "problems": [],
        "notes": [],
    }
    problems: list[str] = report["problems"]  # type: ignore[assignment]
    notes: list[str] = report["notes"]  # type: ignore[assignment]

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


# --------------------------------------------------------------------------
# Demo data
# --------------------------------------------------------------------------

DEMO_CHANNELS = [
    ("UC_kernel", "Kernel Internals", 84000, "technical"),
    ("UC_forge", "Backyard Forge", 51000, "hobby"),
    ("UC_lect", "Mathematics Lectures", 310000, "academic"),
    ("UC_rev", "Reverse Engineering Weekly", 22000, "technical"),
    ("UC_slop", "Daily Clip Machine", 1900000, "meme"),
    ("UC_react", "Reaction Central", 4200000, "meme"),
    ("UC_lofi", "Lofi Radio - Topic", 760000, "music"),
    ("UC_space", "Orbital Mechanics", 140000, "academic"),
    ("UC_repair", "Fix It Properly", 68000, "hobby"),
    ("UC_ai", "AI Narrated Facts", 330000, "ai"),
]

DEMO_TITLES = {
    "technical": [
        "Writing a page allocator from scratch", "How the scheduler picks the next task",
        "Tracing a syscall through the kernel", "Building a toy compiler backend",
        "Cache coherency explained with a logic analyser", "Fuzzing a parser until it breaks",
        "Reading disassembly without an IDE", "Why your mutex is slower than you think",
    ],
    "academic": [
        "Lecture 7: manifolds and charts", "A proof of the spectral theorem",
        "Orbital transfers, derived properly", "Introduction to measure theory, part 3",
        "Numerical methods for stiff systems", "The derivation nobody shows you",
    ],
    "hobby": [
        "Forging a chisel from a leaf spring", "Restoring a seized 1960s lathe",
        "Rebuilding a bandsaw gearbox", "Casting aluminium in the back garden",
        "Repairing a bench power supply", "Making a jig that actually holds square",
    ],
    "meme": [
        "SHOCKING moments that broke the internet 😱", "I tried this for 24 HOURS (gone wrong)",
        "Ranking EVERY single one #shorts", "you won't BELIEVE what happens next",
        "POV: it's 3am and the algorithm won 💀", "Tier list of things nobody asked about",
    ],
    "music": [
        "lofi beats to not study to [1 hour mix]", "Official Music Video - Night Drive",
        "Ambient set, live session", "Remix (extended instrumental)",
    ],
    "ai": [
        "10 facts about the ocean (AI narrated)", "The history of concrete, text-to-speech documentary",
        "Top 5 mysteries explained by AI voice", "Generated using AI: the future of transport",
    ],
}

DEMO_SUFFIXES = [
    "part 2", "part 3", "revisited", "follow-up", "the long version",
    "annotated", "one year later", "corrections", "extended cut", "appendix",
]

DEMO_TRANSCRIPTS = {
    "technical": "we allocate a page from the buddy allocator then map it into the process address space "
                 "the kernel keeps a free list per order so the lookup stays constant time under contention "
                 "a mutex here would serialise every fault so we use a per-cpu cache instead register "
                 "pressure matters because the compiler spills to the stack and that costs a cache miss",
    "academic": "consider a smooth manifold with an atlas of charts each transition map is a diffeomorphism "
                "the theorem follows from the spectral decomposition of a self adjoint operator we derive "
                "the integral by parts and the boundary term vanishes because the support is compact",
    "hobby": "i heated the leaf spring to a bright orange and drew it out on the anvil then normalised it "
             "three times before hardening in oil the temper colour we want is a light straw so back it off "
             "slowly and keep the edge cool while grinding",
    "meme": "yo chat this is actually insane bro look at this wait for it okay okay okay that's crazy "
            "no way bro that's actually crazy chat drop a like",
    "music": "",
    "ai": "the ocean covers seventy one percent of the earth surface. it contains an estimated one point "
          "three three five billion cubic kilometres of water. this is fact number two. the deepest point "
          "is the challenger deep. this is fact number three.",
}


def seed_demo(db: Database, count: int = 400) -> dict[str, int]:
    """Build a synthetic catalogue so the controls can be explored offline.

    Useful for development and for deciding whether you like the shape of the
    thing before pointing it at a real instance.
    """
    from . import scoring

    rng = random.Random(1979)
    now = int(time.time())
    videos = []
    for index in range(count):
        cid, name, subs, kind = DEMO_CHANNELS[index % len(DEMO_CHANNELS)]
        # Walk the title list rather than sampling it, so a demo catalogue does
        # not show the same headline four times on one screen.
        titles = DEMO_TITLES[kind]
        slot = (index // len(DEMO_CHANNELS))
        title = titles[slot % len(titles)]
        series = slot // len(titles)
        if series:
            title = f"{title} ({DEMO_SUFFIXES[series % len(DEMO_SUFFIXES)]})"
        if kind == "meme":
            duration = rng.choice([35, 48, 61, 95, 180, 240])
        elif kind == "music":
            duration = rng.choice([210, 240, 3600, 4200])
        elif kind == "academic":
            duration = rng.randint(1500, 5400)
        else:
            duration = rng.randint(400, 3000)
        views = int(rng.lognormvariate(10.5 if kind in {"meme", "music"} else 8.6, 1.3))
        description = {
            "technical": "Source: https://github.com/example/demo\n0:00 intro\n2:14 the allocator\n11:40 benchmarks",
            "academic": "Notes and problem sets linked. Reference: https://arxiv.org/abs/2401.00000\n0:00 setup\n6:30 proof",
            "hobby": "Tools used are listed below. Sponsored by nobody.",
            "meme": "LIKE AND SUBSCRIBE 🔥🔥 #shorts",
            "music": "Official audio. Stream everywhere.",
            "ai": "This video was generated using AI text-to-speech narration.",
        }[kind]
        videos.append({
            "id": f"demo{index:04d}",
            "title": title if kind != "meme" else title.upper()[:70],
            "author": name,
            "author_id": cid,
            "published": now - rng.randint(0, 90) * 86400,
            "duration": duration,
            "views": views,
            "likes": int(views * rng.uniform(0.01, 0.06)),
            "description": description,
            "keywords": {"technical": ["kernel", "systems", "c"], "academic": ["mathematics", "lecture"],
                         "hobby": ["workshop", "restoration"], "meme": ["funny", "compilation", "shorts"],
                         "music": ["music", "mix", "lofi"], "ai": ["facts", "documentary"]}[kind],
            "genre": {"technical": "Science & Technology", "academic": "Education", "hobby": "Howto & Style",
                      "meme": "Entertainment", "music": "Music", "ai": "Education"}[kind],
            "is_live": 0, "is_upcoming": 0, "family_safe": 1, "sub_count": subs,
            "caption_langs": ["en"] if kind != "music" else [],
            "transcript": DEMO_TRANSCRIPTS[kind] * (3 if duration > 900 else 1),
        })

    db.upsert_videos(videos)
    db.executemany(
        "INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?,?,1.0,?) "
        "ON CONFLICT(channel_id) DO NOTHING",
        [(cid, name, now) for cid, name, _, _ in DEMO_CHANNELS[:6]],
    )

    rows = []
    for video in videos:
        card = scoring.score_video(video, video["transcript"])
        rows.append(scoring.card_to_row(card))
    db.executemany(scoring.INSERT_SCORE, rows)

    # A plausible history: mostly technical and academic, a little late-night meme.
    watched = []
    for video in videos:
        kind_weight = {"Science & Technology": 0.45, "Education": 0.35,
                       "Howto & Style": 0.2, "Entertainment": 0.08, "Music": 0.05}
        if rng.random() < kind_weight.get(video["genre"], 0.1):
            progress = rng.uniform(0.55, 1.0) if video["genre"] != "Entertainment" else rng.uniform(0.05, 0.4)
            watched.append((video["id"], now - rng.randint(0, 60) * 86400, progress, 0, "demo"))
    db.executemany(
        "INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,?,?,?)", watched
    )

    channel_policy.recompute_affinity(db)
    derived = interest_store.derive_from_history(db)
    learner.train_from_events(db)
    return {"videos": len(videos), "watched": len(watched), "interests": derived}


if __name__ == "__main__":
    sys.exit(main())
