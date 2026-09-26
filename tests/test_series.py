"""Recognising series, and putting the next episode first."""

from __future__ import annotations

import json
import time

import pytest

from sieve import ranking, scoring, series
from sieve.config import resolve_settings
from sieve.db import Database

NOW = int(time.time())


@pytest.mark.parametrize("title, parsed", [
    ("Calculus 101 ep 1", ("calculus 101", 1)),
    ("Calculus 101 — Lecture 5: Continuity", ("calculus 101", 5)),
    ("Heap exploitation #7 - tcache poisoning", ("heap exploitation", 7)),
    ("Linear algebra (3 of 10): eigenvalues", ("linear algebra", 3)),
    ("The Expanse S2E5 review", ("expanse", 5)),
    ("Part 3 of my Rust compiler series", ("my rust compiler series", 3)),
    ("Building an OS - Day 12", ("building os", 12)),
])
def test_episodes_are_recognised(title, parsed):
    assert series.parse(title) == parsed


@pytest.mark.parametrize("title", ["iPhone 15 review", "Calculus 101 full course",
                                   "Top 10 of 2026", "Top 10 CPUs of 2026", "Windows 11 tips"])
def test_a_bare_number_is_not_an_episode(title):
    assert series.parse(title) is None


def catalogue(db, titles, channel="UCcalc", prefix="e"):
    videos = [{"id": f"{prefix}{i:010d}", "title": t, "author": "Calc", "author_id": channel,
               "published": NOW - i * 3600, "duration": 1200, "views": 5000} for i, t in enumerate(titles)]
    db.upsert_videos(videos)
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(dict(db.get_video(v["id"]))))
                                          for v in videos])
    return [v["id"] for v in videos]


def test_the_next_episode_is_found_and_goes_first(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    ids = catalogue(db, ["Calculus 101 ep 1: limits", "Calculus 101 ep 2: derivatives",
                         "Calculus 101 ep 3: integrals", "Unrelated vlog"] +
                    [f"Filler video {i}" for i in range(30)])
    db.record_watch(ids[0], 1.0)
    assert series.next_episodes(db) == {ids[1]: "episode 2 — you watched up to 1"}
    items = ranking.recommend(db, resolve_settings({}), record=False).items
    assert items[0].id == ids[1]
    assert ranking.explain(items[0])[0]["label"] == "next episode of a series you're watching"
    db.record_watch(ids[1], 0.9)
    assert set(series.next_episodes(db)) == {ids[2]}, "it moves on as you watch"


def test_a_barely_started_episode_does_not_count(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    ids = catalogue(db, ["Rust ep 1", "Rust ep 2"])
    db.record_watch(ids[0], 0.1)
    assert series.next_episodes(db) == {}


def test_the_next_video_in_a_playlist_is_next(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    ids = catalogue(db, ["Intro", "Some talk", "Another talk"])
    db.execute("INSERT INTO playlists(id, title, video_ids, updated_at) VALUES('PLx', 'Talks', ?, ?)",
               (json.dumps(ids), NOW))
    db.record_watch(ids[0], 0.95)
    assert series.next_episodes(db) == {ids[1]: "next in your playlist “Talks”"}


def test_it_can_be_turned_off(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    ids = catalogue(db, ["Calc ep 1", "Calc ep 2"] + [f"Other {i}" for i in range(20)])
    db.record_watch(ids[0], 1.0)
    items = ranking.recommend(db, resolve_settings({"homepage": {"next_episode": False}}), record=False).items
    assert all("next" not in c.sources for c in items)


def test_new_videos_only_appear_every_interval(tmp_path):
    """Between refreshes the page only draws from what it already showed."""
    db = Database(str(tmp_path / "s.db"))
    catalogue(db, [f"Old {i}" for i in range(10)], channel="UCold")
    settings = resolve_settings({"homepage": {"new_every": 1440, "count": 10}})
    first = {c.id for c in ranking.homepage(db, settings, record=False).items}
    catalogue(db, [f"Brand new {i}" for i in range(10)], channel="UCnew", prefix="n")
    again = ranking.homepage(db, settings, record=False)
    assert {c.id for c in again.items} <= first, "nothing new before the interval passes"
    assert again.diagnostics["new_videos_at"] > NOW
    snap = db.get_setting("homepage_snapshot")
    db.set_setting("homepage_snapshot", {**snap, "at": snap["at"] - 1441 * 60})
    assert {c.id for c in ranking.homepage(db, settings, record=False).items} - first, "then new ones arrive"


def test_sync_never_means_the_worker_never_syncs(tmp_path):
    from sieve import actions
    from sieve.ingest import Ingestor
    from tests.support import offline_config

    db = Database(str(tmp_path / "s.db"))
    actions.save_settings(db, {"compute": {"sync_minutes": 0}})
    db.set_setting("pull_state", {"first_fetch_at": NOW})
    assert actions.resolved_settings(db)["compute"]["sync_minutes"] == 0
    ingestor = Ingestor(offline_config(tmp_path), db, api=None, community=None)
    ingestor.start()
    time.sleep(1.5)
    ingestor.stop()
    assert ingestor.last_attempt == 0
