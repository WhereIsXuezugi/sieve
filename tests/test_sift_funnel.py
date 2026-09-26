"""Sift, the funnel, your records, the compute budget, and the player."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from sieve import actions, ranking, scoring, sift
from sieve.app import create_app
from sieve.cli import seed_demo
from sieve.config import COMPUTE_PRESETS, resolve_settings
from sieve.db import Database
from sieve.ingest import Ingestor
from tests.support import offline_config

NOW = int(time.time())


def yt(vid, title, author="Chan", channel="UCaaaaaaaaaaaaaaaaaaaaaa", duration=900, **extra):
    return {"videoId": vid, "title": title, "author": author, "authorId": channel,
            "published": NOW - 3600, "lengthSeconds": duration, "viewCount": 5000,
            "description": "", **extra}


class FakeUpstream:
    """Stands in for upstream.Upstream: Invidious-shaped answers, no network."""

    def __init__(self):
        self.searched = []

    def search(self, query, **params):
        self.searched.append(query)
        return [
            yt("aaaaaaaaaa1", f"Lecture: {query} from first principles", duration=2400),
            yt("aaaaaaaaaa2", f"{query} in 60 seconds #shorts", duration=45, isShort=True),
            yt("aaaaaaaaaa3", f"A careful introduction to {query}", channel="UCbbbbbbbbbbbbbbbbbbbbbb"),
        ]

    def channel_videos(self, channel_id, sort="newest"):
        return [yt(f"chan{i:07d}", f"Upload {i} about compilers", author="The Channel",
                   channel=channel_id, duration=600 + i) for i in range(8)]

    def playlist(self, playlist_id):
        return {"title": "My queue", "videos": [yt("plist000001", "Queued lecture"),
                                               yt("plist000002", "Queued talk")]}

    def video(self, video_id):
        return yt(video_id, "Register allocation by graph colouring")

    def resolve_handle(self, handle):
        return "UCcccccccccccccccccccccc"


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def settings(**patch):
    base = resolve_settings({})
    for section, values in patch.items():
        base[section].update(values)
    return base


# -- working out what was pasted ------------------------------------------------


@pytest.mark.parametrize("ref,kind,expected", [
    ("", "auto", ("interests", "")),
    ("rust async runtimes", "auto", ("search", "rust async runtimes")),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghijk", "auto", ("video", "dQw4w9WgXcQ")),
    ("https://youtu.be/dQw4w9WgXcQ", "auto", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "auto", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/playlist?list=PLabcdefghijk", "auto", ("playlist", "PLabcdefghijk")),
    ("https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q", "auto", ("channel", "UCsXVk37bltHxD1rDPwtNM8Q")),
    ("youtube.com/@3blue1brown/videos", "auto", ("handle", "3blue1brown")),
    ("@veritasium", "auto", ("handle", "veritasium")),
    ("UCsXVk37bltHxD1rDPwtNM8Q", "auto", ("channel", "UCsXVk37bltHxD1rDPwtNM8Q")),
    ("veritasium", "channel", ("handle", "veritasium")),
    ("https://www.youtube.com/@x", "search", ("search", "https://www.youtube.com/@x")),
])
def test_what_was_pasted_is_worked_out(ref, kind, expected):
    assert sift.resolve(ref, kind) == expected


@pytest.mark.parametrize("ref,kind", [
    ("https://example.com/something", "auto"),
    ("https://www.youtube.com/watch?v=short", "auto"),
    ("", "search"),
    ("not a video", "video"),
    ("x", "nonsense"),
])
def test_bad_input_says_why(ref, kind):
    with pytest.raises(sift.SiftError):
        sift.resolve(ref, kind)


# -- running the algorithm over a source ------------------------------------------


def test_a_search_is_scored_filtered_and_explained(db):
    out = sift.run(db, FakeUpstream(), settings(), "compilers")
    result = out["result"]
    ids = [c.id for c in result.items]
    assert out["kind"] == "search" and out["fetched"] == 3
    assert "aaaaaaaaaa2" not in ids, "the Short must be filtered like on the homepage"
    assert all(ranking.explain(c) for c in result.items)
    rejected = [e for e in result.ledger if e["stage"] == "rejected"]
    assert rejected and rejected[0]["id"] == "aaaaaaaaaa2" and "Short" in rejected[0]["reason"]


def test_filters_can_be_switched_off(db):
    out = sift.run(db, FakeUpstream(), settings(), "compilers", apply_filters=False)
    assert "aaaaaaaaaa2" in [c.id for c in out["result"].items]


def test_a_channel_is_not_capped_at_three(db):
    """The homepage allows three per channel; sifting a channel wants them all."""
    out = sift.run(db, FakeUpstream(), settings(), "https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q")
    assert len(out["result"].items) == 8


def test_handles_are_resolved(db):
    out = sift.run(db, FakeUpstream(), settings(), "@somebody")
    assert out["kind"] == "handle" and len(out["result"].items) == 8


def test_a_playlist_and_a_single_video(db):
    upstream = FakeUpstream()
    assert sift.run(db, upstream, settings(), "PLabcdefghijk", kind="playlist")["fetched"] == 2
    out = sift.run(db, upstream, settings(), "https://youtu.be/dQw4w9WgXcQ")
    assert out["kind"] == "video" and upstream.searched, "more-like-this searches the video's terms"


def test_my_interests_searches_for_them(db):
    from sieve import interests
    interests.set_interest(db, "compilers", 1.0)
    interests.set_interest(db, "allocators", 0.8)
    upstream = FakeUpstream()
    out = sift.run(db, upstream, settings(), "")
    assert out["kind"] == "interests" and set(upstream.searched) >= {"compilers", "allocators"}


def test_sifted_videos_join_the_catalogue_without_counting_as_shown(db):
    sift.run(db, FakeUpstream(), settings(), "compilers")
    assert db.get_video("aaaaaaaaaa1") is not None
    assert db.one("SELECT COUNT(*) AS n FROM scores WHERE video_id = 'aaaaaaaaaa1'")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM impressions")["n"] == 0


def test_the_limit_comes_from_the_compute_budget(db):
    out = sift.run(db, FakeUpstream(), settings(compute={"sift_limit": 2}),
                   "https://www.youtube.com/channel/UCsXVk37bltHxD1rDPwtNM8Q")
    assert out["fetched"] == 2


# -- the funnel ---------------------------------------------------------------------


@pytest.fixture()
def demo(db):
    seed_demo(db, 150)
    return db


def test_the_ledger_accounts_for_every_candidate(demo):
    result = ranking.recommend(demo, resolve_settings({}), ledger=True, record=False)
    stages = [e["stage"] for e in result.ledger]
    assert stages.count("shown") == len(result.items)
    assert stages.count("rejected") == result.diagnostics["rejected_total"]
    assert len(stages) == result.diagnostics["pool"] + result.diagnostics["rejected_total"]


def test_everything_left_off_the_page_says_why(demo):
    result = ranking.recommend(demo, resolve_settings({"homepage": {"count": 6}}), ledger=True, record=False)
    ranked = [e for e in result.ledger if e["stage"] == "ranked"]
    assert ranked and all(e["reason"] for e in ranked)
    reasons = " ".join(e["reason"] for e in ranked)
    assert "below the cut" in reasons or "from this channel" in reasons


def test_shown_entries_carry_slot_and_explanation(demo):
    result = ranking.recommend(demo, resolve_settings({}), ledger=True, record=False)
    shown = [e for e in result.ledger if e["stage"] == "shown"]
    assert [e["slot"] for e in shown] == list(range(len(shown)))
    assert all(e["explanation"] for e in shown)


def test_looking_at_the_funnel_records_no_impressions(tmp_path):
    app = create_app(offline_config(tmp_path), start_worker=False)
    seed_demo(app.state.db, 80)
    client = TestClient(app)
    body = client.get("/api/funnel").json()
    assert body["total"] == sum(body["counts"].values())
    client.get("/funnel")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM impressions")["n"] == 0


# -- records --------------------------------------------------------------------------


def test_records_list_delete_and_forget(demo):
    listing = actions.list_records(demo, "watches", limit=5)
    assert listing["total"] > 5 and len(listing["records"]) == 5
    first = listing["records"][0]["record"]
    assert actions.delete_record(demo, "watches", first)
    assert not actions.delete_record(demo, "watches", first)
    with pytest.raises(actions.ActionError):
        actions.forget_records(demo, "watches", "yes")
    assert actions.forget_records(demo, "watches", "FORGET") == listing["total"] - 1
    assert actions.list_records(demo, "watches")["total"] == 0


def test_forgetting_watches_forgets_what_was_learned_from_them(demo):
    derived = demo.one("SELECT COUNT(*) AS n FROM interests WHERE origin = 'derived'")["n"]
    assert derived > 0
    actions.forget_records(demo, "watches", "forget")
    assert demo.one("SELECT COUNT(*) AS n FROM interests WHERE origin = 'derived'")["n"] == 0


def test_opens_have_records_too(db):
    db.record_open("dQw4w9WgXcQ", "youtube")
    record = actions.list_records(db, "opens")["records"][0]
    assert record["provider"] == "youtube"
    assert actions.delete_record(db, "opens", record["record"])


# -- the compute budget ----------------------------------------------------------------


def test_a_preset_sets_every_number():
    from sieve.profiles import sanitise_settings
    assert sanitise_settings({"compute": {"preset": "light"}})["compute"] == {"preset": "light", **COMPUTE_PRESETS["light"]}


def test_changing_a_number_makes_it_custom_and_is_clamped():
    from sieve.profiles import sanitise_settings
    out = sanitise_settings({"compute": {"pool_size": 10**9}})["compute"]
    assert out == {"pool_size": 5000, "preset": "custom"}


def test_the_pool_size_limits_the_work(demo):
    small = ranking.recommend(demo, resolve_settings({"compute": {"pool_size": 50}}), record=False)
    large = ranking.recommend(demo, resolve_settings({"compute": {"pool_size": 5000}}), record=False)
    assert small.diagnostics["pool"] < large.diagnostics["pool"]


def test_the_worker_reads_the_budget_each_pass(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.upsert_videos([{"id": f"v{i:010d}", "title": f"Video {i}", "author": "A", "author_id": "UCa",
                       "published": NOW, "duration": 600} for i in range(30)])
    ingestor = Ingestor(offline_config(tmp_path), db, FakeUpstream(), community=None)
    ingestor.community = type("C", (), {"enrich": lambda *a, **k: None,
                                        "branding_map": lambda *a, **k: {},
                                        "segments_map": lambda *a, **k: {}})()
    actions.save_settings(db, {"compute": {"score_batch": 7, "transcripts": False}})
    assert ingestor.score_pending() == 7
    actions.save_settings(db, {"compute": {"score_batch": 11}})
    assert ingestor.score_pending() == 11


def test_discovery_searches_follow_the_budget(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    from sieve import interests
    for tag in ("compilers", "allocators", "kernels", "parsers", "linkers"):
        interests.set_interest(db, tag, 1.0)
    upstream = FakeUpstream()
    upstream.trending = lambda *a, **k: []
    ingestor = Ingestor(offline_config(tmp_path), db, upstream, community=None)
    actions.save_settings(db, {"compute": {"discover_terms": 2}})
    ingestor.sync()
    assert len(upstream.searched) == 2
    upstream.searched.clear()
    actions.save_settings(db, {"compute": {"discover_terms": 0}})
    ingestor.sync()
    assert upstream.searched == []


# -- the player -----------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    app = create_app(offline_config(tmp_path), start_worker=False)
    video = {"id": "dQw4w9WgXcQ", "title": "A real video", "author": "Rick", "author_id": "UCr",
             "published": NOW, "duration": 213}
    app.state.db.upsert_videos([video])
    app.state.db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(
        dict(app.state.db.get_video("dQw4w9WgXcQ"))))])
    return TestClient(app, follow_redirects=False)


def test_the_player_page_embeds_the_video(client):
    html = client.get("/play/dQw4w9WgXcQ").text
    assert 'data-video="dQw4w9WgXcQ"' in html and "/static/player.js" in html
    assert 'referrerpolicy="strict-origin-when-cross-origin"' in html, \
        "YouTube's embed refuses to play without a referrer: Error 153"


def test_the_player_is_where_the_nocookie_provider_goes(client):
    client.post("/api/settings", json={"playback": {"provider": "nocookie"}})
    assert client.get("/open/dQw4w9WgXcQ").headers["location"] == "/play/dQw4w9WgXcQ"


def test_demo_ids_never_reach_the_player(client):
    assert client.get("/play/demo0001").headers["location"] == "/video/demo0001?unplayable=1"


def test_one_viewing_is_one_row(client):
    for fraction in (0.1, 0.4, 0.35, 0.8):
        client.post("/api/progress", json={"video_id": "dQw4w9WgXcQ", "progress": fraction,
                                           "session": "abc123", "dwell": int(fraction * 200)})
    rows = client.app.state.db.query("SELECT progress, dwell FROM history")
    assert len(rows) == 1 and rows[0]["progress"] == 0.8 and rows[0]["dwell"] == 160


def test_separate_viewings_are_separate_rows(client):
    client.post("/api/progress", json={"video_id": "dQw4w9WgXcQ", "progress": 1.0, "session": "one"})
    client.post("/api/progress", json={"video_id": "dQw4w9WgXcQ", "progress": 0.5, "session": "two"})
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM history")["n"] == 2


@pytest.mark.parametrize("body", [
    {"video_id": "dQw4w9WgXcQ", "progress": "lots"},
    {"video_id": "dQw4w9WgXcQ", "progress": 7},
    {"progress": 0.5},
])
def test_bad_progress_is_a_400(client, body):
    assert client.post("/api/progress", json=body).status_code == 400


def test_every_ranking_component_has_a_colour():
    """Regression: channel_quality had no colour rule, so its share of every
    why-bar rendered as a blank gap."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "sieve"
    components = set(re.findall(r'components\["([a-z_]+)"\]', (root / "ranking.py").read_text()))
    components.discard("penalty")  # only ever negative, never drawn as a segment
    css = (root / "static" / "app.css").read_text()
    coloured = set(re.findall(r'\.why \.bar i\[data-k="([a-z_]+)"\]', css))
    assert components <= coloured, f"no colour for {sorted(components - coloured)}"
