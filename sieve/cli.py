"""Command line interface.

    sieve serve                      run the web app
    sieve sync [--deep]              pull new videos from YouTube or Invidious
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
    sieve demo [--topics a,b]        fill the catalogue with real videos for some topics
    sieve demo --synthetic           or with invented ones, for trying the controls offline
    sieve demo remove                delete the synthetic demo catalogue
    sieve fetch [--topics a,b]       find videos for your topics (never runs by itself)
    sieve backup [list|create]       backups of the whole database
    sieve backup restore NAME        roll back to one, in place (delete NAME to remove)
    sieve reset pulled               delete the videos Sieve found for you
    sieve pulls [reset]              requests made to YouTube or Invidious, and the limit
    sieve prune                      drop stale rows and vacuum
    sieve reset catalogue|everything delete every video, or factory-reset (backs up first)
    sieve stats                      what is in the database
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, ingest, learner, profiles
from . import channels as channel_policy
from . import interests as interest_store
from .community import CommunityData
from .config import Config
from .db import Database
from .demo import remove_demo, seed_demo  # seed_demo is also imported from here by tests
from .doctor import doctor_report
from .starter import TOPICS
from .upstream import Upstream


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
    parser.add_argument("--version", action="version",
                        version=f"sieve {__version__} ({Path(__file__).resolve().parent})",
                        help="print the version and where it is installed, then exit")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-worker", action="store_true",
                       help="do not run background sync and scoring")

    sync = sub.add_parser("sync", help="refresh the catalogue")
    sync.add_argument("--deep", action="store_true",
                      help="at least eight searches, and every starter channel for your topics")

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

    reset = sub.add_parser("reset", help="delete every video, or factory-reset everything")
    reset.add_argument("scope", choices=["pulled", "catalogue", "everything"],
                       help="pulled: the videos Sieve found for you; "
                            "catalogue: every video and score, keeping your data; "
                            "everything: back to a fresh install")
    reset.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    reset.add_argument("--no-backup", action="store_true", help="do not write a backup first")

    prof = sub.add_parser("profile", help="export or import a configuration")
    prof.add_argument("action", choices=["export", "import"])
    prof.add_argument("path", nargs="?")

    demo = sub.add_parser("demo", help="fill the catalogue with real videos (or --synthetic ones)")
    demo.add_argument("action", nargs="?", choices=["remove"],
                      help="remove: delete the synthetic demo catalogue")
    demo.add_argument("--synthetic", action="store_true",
                      help="invented videos, no network needed; they cannot be played")
    demo.add_argument("--topics", help="comma-separated topics: " + ", ".join(TOPICS))
    demo.add_argument("--count", type=int, default=400, help="how many synthetic videos")

    pulls = sub.add_parser("pulls", help="requests made to YouTube or Invidious, and the pull limit")
    pulls.add_argument("action", nargs="?", choices=["reset"],
                       help="reset: forget recorded pulls, so the limit starts from zero")

    fetch = sub.add_parser("fetch", help="find videos for your topics now (never runs by itself)")
    fetch.add_argument("--topics", help="comma-separated, replacing the chosen ones: " + ", ".join(TOPICS))

    backup = sub.add_parser("backup", help="list, make, delete or roll back to backups")
    backup.add_argument("action", nargs="?", default="list",
                        choices=["list", "create", "delete", "restore"])
    backup.add_argument("name", nargs="?", help="the backup's file name (see `sieve backup list`)")
    backup.add_argument("--yes", action="store_true", help="skip the typed confirmation")

    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    db = Database(cfg.db_path)

    if args.command == "serve":
        import uvicorn

        from .app import create_app

        app = create_app(cfg, start_worker=not args.no_worker)
        uvicorn.run(app, host=args.host or cfg.host, port=args.port or cfg.port, log_level="info")
        return 0

    api = Upstream(cfg, db)
    community = CommunityData(cfg, db)
    ingestor = ingest.Ingestor(cfg, db, api, community)

    try:
        if args.command == "sync":
            counts = ingestor.sync(deep=args.deep)
            print(json.dumps(counts, indent=2))
            if counts.get("stopped"):
                print(f"stopped early: {counts['stopped']}", file=sys.stderr)
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

            print(json.dumps(vision.score_pending(db, cfg, args.limit, api.thumbnail_url), indent=2))

        elif args.command == "doctor":
            print(json.dumps(doctor_report(cfg, db, api, quick=args.quick), indent=2))

        elif args.command == "interests":
            print(f"derived {interest_store.derive_from_history(db)} interests")

        elif args.command == "stats":
            print(json.dumps(db.stats(), indent=2))

        elif args.command == "reset":
            from . import actions

            what = {
                "everything": "This erases everything: videos, settings, history, subscriptions, "
                              "channel lists, interests and saved profiles.",
                "catalogue": "This deletes every video and score, keeping your subscriptions, "
                             "history and settings.",
                "pulled": "This deletes the videos Sieve found for you, keeping your "
                          "subscriptions' and playlists' videos and anything you watched or rated.",
            }[args.scope]
            confirm = "reset" if args.yes else input(
                f"{what}\nA backup is written first. Type \"reset\" to continue: ")
            try:
                if args.scope == "pulled":
                    result = actions.reset_pulled(db, confirm, backup=not args.no_backup)
                else:
                    result = actions.reset(db, args.scope, confirm, backup=not args.no_backup)
            except actions.ActionError as exc:
                print(exc)
                return 1
            print(json.dumps(result, indent=2))

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
            if args.action == "remove":
                print(json.dumps(remove_demo(db), indent=2))
            elif args.synthetic:
                print(json.dumps(seed_demo(db, args.count), indent=2))
            else:
                topics = [t.strip() for t in (args.topics or "").split(",") if t.strip()] or None
                unknown = [t for t in topics or [] if t not in TOPICS]
                if unknown:
                    print(f"unknown topics: {', '.join(unknown)}; choose from {', '.join(TOPICS)}")
                    return 1
                print("pulling real videos; this takes a minute…", file=sys.stderr)
                counts = ingestor.bootstrap(topics)
                channel_policy.recompute_quality(db)
                print(json.dumps(counts, indent=2))
                if not counts.get("seen"):
                    print("Nothing arrived, so YouTube and Invidious are probably unreachable from "
                          "here (`sieve doctor` says which). To try the controls offline, run "
                          "`sieve demo --synthetic`.", file=sys.stderr)
                    return 1

        elif args.command == "pulls":
            if args.action == "reset":
                from . import actions

                print(f"forgot {actions.reset_pull_counter(db)} recorded pulls")
            print(api.budget.summary())
            print(json.dumps(api.budget.status(), indent=2))

        elif args.command == "fetch":
            from . import actions

            topics = [t.strip() for t in (args.topics or "").split(",") if t.strip()] or None
            unknown = [t for t in topics or [] if t not in TOPICS]
            if unknown:
                print(f"unknown topics: {', '.join(unknown)}; choose from {', '.join(TOPICS)}")
                return 1
            print("fetching; this can take a minute…", file=sys.stderr)
            try:
                counts = ingestor.bootstrap(topics)
            except actions.ActionError as exc:
                print(exc)
                return 1
            print(json.dumps(counts, indent=2))
            if counts.get("stopped"):
                print(f"stopped early: {counts['stopped']}", file=sys.stderr)

        elif args.command == "backup":
            from . import backups

            if args.action == "list":
                for b in backups.listing(db):
                    print(f"{b['name']}  {b['kind']:<14} {b['bytes'] / 1048576:7.1f} MB  "
                          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(b['created_at']))}")
                if not backups.listing(db):
                    print("no backups yet")
            elif args.action == "create":
                made = backups.create(db, "manual")
                print(made.name if made else "nothing to back up (in-memory database)")
            elif not args.name:
                print(f"`sieve backup {args.action}` needs a name; see `sieve backup list`")
                return 1
            elif args.action == "delete":
                if not backups.delete(db, args.name):
                    print(f"no backup called {args.name}")
                    return 1
                print(f"deleted {args.name}")
            else:
                confirm = "restore" if args.yes else input(
                    f"Roll back to {args.name}? Everything now is replaced by it, after being "
                    "backed up itself. Type \"restore\" to continue: ")
                try:
                    print(json.dumps(backups.restore(db, args.name, confirm), indent=2))
                except backups.BackupError as exc:
                    print(exc)
                    return 1
    finally:
        api.close()
        community.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
