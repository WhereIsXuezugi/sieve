"""The synthetic demo catalogue, for trying the controls with no network.

`sieve demo --synthetic` writes it. Its video ids ("demo0003") are invented, so
no provider can play them; they exist to show what the controls do. `sieve
demo` without the flag pulls real videos instead (ingest.Ingestor.bootstrap),
and real videos replace these automatically when Controls, Finding videos,
"replace the demo catalogue" is on — see `remove_demo`.
"""

from __future__ import annotations

import random
import time

from . import channels as channel_policy
from . import interests as interest_store
from . import learner
from .db import Database

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


DEMO_CHANNEL_IDS = [cid for cid, _, _, _ in DEMO_CHANNELS]
DEMO_VIDEO_WHERE = "id LIKE 'demo%' AND length(id) = 8"


def demo_count(db: Database) -> int:
    return db.scalar(f"SELECT COUNT(*) FROM videos WHERE {DEMO_VIDEO_WHERE}", default=0)


def remove_demo(db: Database) -> dict[str, int]:
    """Delete the synthetic catalogue and everything it brought with it: its
    videos and scores, the made-up watch history, the demo subscriptions and
    channels, and what was learned from them. Real data is untouched."""
    ids = [r["id"] for r in db.query(f"SELECT id FROM videos WHERE {DEMO_VIDEO_WHERE}")]
    if not ids:
        return {"videos": 0}
    removed = {"videos": len(ids), "history": 0}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        removed["history"] += db.execute(
            f"DELETE FROM history WHERE video_id IN ({marks})", chunk).rowcount
        for table, column in (("feedback", "video_id"), ("impressions", "video_id"),
                              ("opens", "video_id"), ("scores", "video_id"),
                              ("dearrow", "video_id"), ("sponsor_segments", "video_id"),
                              ("videos", "id")):
            db.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", chunk)
        db.execute(f"DELETE FROM blocklist WHERE kind = 'video' AND value IN ({marks})", chunk)
    marks = ",".join("?" * len(DEMO_CHANNEL_IDS))
    for table, column in (("subscriptions", "channel_id"), ("channels", "id"),
                          ("channel_prefs", "channel_id"), ("channel_fetches", "channel_id")):
        db.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", DEMO_CHANNEL_IDS)
    # Everything learned from the made-up history goes with it.
    channel_policy.recompute_affinity(db)
    interest_store.derive_from_history(db)
    learner.train_from_events(db)
    return removed
