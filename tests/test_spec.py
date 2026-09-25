"""One test (or a few) per requirement of the update specification."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from sieve import actions, langdetect, problems, ranking, scales, scoring
from sieve.app import create_app
from sieve.config import resolve_settings
from tests.support import offline_config

NOW = int(time.time())


def add(db, vid, title, channel="UCa", author="A channel", description="", first_seen=None, **extra):
    db.upsert_videos([{"id": vid, "title": title, "author": author, "author_id": channel,
                       "description": description, "published": NOW - 3600, "duration": 900,
                       "views": 5000, **({"first_seen": first_seen} if first_seen else {}), **extra}])
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(dict(db.get_video(vid))))])


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(offline_config(tmp_path), start_worker=False))


@pytest.fixture()
def db(client):
    return client.app.state.db


# 1 ---------------------------------------------------------- channel status
def test_channel_status_is_shown_and_the_selected_channel_marked(client, db):
    add(db, "a0000000001", "x", channel="UCchan000000000000000000")
    db.execute("INSERT INTO channels(id, name) VALUES('UCchan000000000000000000', 'Chan')")
    html = client.get("/channels?q=UCchan000000000000000000").text
    assert 'class="selected"' in html and "data-status" in html
    assert "past your" in html and "length limits, score limits and rules" in html


# 2 ------------------------------------------------------------- feedback
def test_more_and_less_are_one_choice_and_undoable(client, db):
    add(db, "b0000000001", "Rust ownership explained")
    def post(kind):
        return client.post("/api/feedback", json={"video_id": "b0000000001", "kind": kind}).json()
    assert post("more")["state"] == "more"
    assert post("less")["state"] == "less"
    assert db.scalar("SELECT COUNT(*) FROM feedback WHERE video_id='b0000000001'") == 1, "Less replaced More"
    assert post("less")["state"] is None, "choosing it again takes it back"
    assert db.scalar("SELECT COUNT(*) FROM feedback WHERE video_id='b0000000001'") == 0


def test_the_homepage_and_player_show_the_same_choice(client, db):
    add(db, "dQw4w9WgXcQ", "A video")
    client.post("/api/feedback", json={"video_id": "dQw4w9WgXcQ", "kind": "more"})
    assert 'data-feedback="more" aria-pressed="true"' in client.get("/play/dQw4w9WgXcQ").text
    home = client.get("/").text
    assert 'aria-pressed="true"' in home.split('data-video="dQw4w9WgXcQ"')[1].split("</article>")[0]


def test_hide_removes_the_video_from_the_homepage(client, db):
    for i in range(3):
        add(db, f"c000000000{i}", f"Video {i}", channel=f"UC{i}")
    client.post("/api/hide", json={"kind": "video", "value": "c0000000001"})
    assert 'data-video="c0000000001"' not in client.get("/").text


def test_feedback_moves_similar_videos_not_the_rated_one(client, db):
    add(db, "d0000000001", "Kernel scheduler deep dive", description="kernel scheduler linux cfs")
    add(db, "d0000000002", "Linux kernel scheduling latency", description="kernel scheduler linux latency")
    before = {c.id: c for c in ranking.recommend(db, resolve_settings({}), record=False).items}
    client.post("/api/feedback", json={"video_id": "d0000000001", "kind": "less"})
    after = {c.id: c for c in ranking.recommend(db, resolve_settings({}), record=False).items}
    assert after["d0000000001"].components.get("learned", 0) == 0, "the rated video is not moved by its own label"
    weights = {r["tag"]: r["weight"] for r in db.query("SELECT tag, weight FROM interests")}
    assert weights and all(w < 0 for w in weights.values()), "its topics are pushed down, for similar videos"
    assert after["d0000000002"].score < before["d0000000002"].score


# 3 -------------------------------------------------------- video language
def test_video_language_prefer_and_only(db):
    add(db, "e0000000001", "How to build a compiler: the complete guide for beginners")
    add(db, "e0000000002", "Wie man einen Compiler baut und warum das so wichtig ist")
    add(db, "e0000000003", "Rust!")   # language unknown: never hidden
    only = resolve_settings({"filters": {"video_languages": ["en"], "video_language_mode": "only"},
                             "homepage": {"fill": "off"}})
    ids = {c.id for c in ranking.recommend(db, only, record=False).items}
    assert ids == {"e0000000001", "e0000000003"}
    prefer = resolve_settings({"filters": {"video_languages": ["en"], "video_language_mode": "prefer"}})
    items = {c.id: c for c in ranking.recommend(db, prefer, record=False).items}
    assert items["e0000000002"].components.get("language") == -0.6
    assert langdetect.detect("Как написать компилятор с нуля") == "ru"


# 4 ------------------------------------------------------------ dismissing
def test_notices_can_be_dismissed(client, db):
    add(db, "f0000000001", "A video")
    assert "Sieve's built-in player" in client.get("/").text
    client.post("/api/notices/playback/dismiss")
    assert "Sieve's built-in player" not in client.get("/").text


# 5 -------------------------------------------------- predicted vs yours
def test_an_override_shows_the_prediction_beside_it(client, db):
    import re

    add(db, "g0000000001", "Measure theory lecture")
    client.put("/api/videos/g0000000001/scores", json={"education": 91})
    html = client.get("/video/g0000000001").text
    # Your value is the score; the engine's prediction (which your correction
    # also trains, in the background) is shown beside it.
    assert '<span class="tag high">yours</span>' in html and re.search(r"predicted \d+", html)
    assert '<span class="val">91</span>' in html


# 6 -------------------------------------------------------- the full range
def test_the_full_range_uses_real_scores_and_has_empty_points(db):
    for i in range(5):
        add(db, f"h000000000{i}", f"Video {i}")
    r = scales.full_range(db, "education")
    assert len(r["histogram"]) == 101 and sum(r["histogram"]) == 5 and r["total"] == 5
    empty = r["histogram"].index(0)
    assert scales.full_range(db, "education", empty)["videos"] == []
    point = next(i for i, n in enumerate(r["histogram"]) if n)
    assert scales.full_range(db, "education", point)["videos"]


# 7 ------------------------------------------------------- backend errors
def test_backend_problems_are_explained_in_the_page(client, db):
    problems.record(db, problems.classify_community("sponsorblock", "The read operation timed out"),
                    "sponsorblock prefix 9d47 failed: The read operation timed out")
    from sieve import ytauth

    ytauth.record(db, "Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser", False)
    html = client.get("/").text
    assert "SponsorBlock did not answer in time" in html and "play normally" in html
    assert "sponsorblock prefix 9d47 failed" in html, "technical details, expandable"
    assert "YouTube is asking Sieve to sign in" in html
    assert "wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp" in html and "Extractors#exporting-youtube-cookies" in html


def test_a_sponsorblock_timeout_is_not_a_playback_failure(client, db):
    add(db, "i0000000001", "A video")
    problems.record(db, "sponsorblock_timeout", "sponsorblock prefix 9770 failed: The read operation timed out")
    html = client.get("/video/i0000000001").text
    assert "the video itself loads and plays normally" in html


# 8 ------------------------------------------------------------- narrowing
def test_no_narrowing_warning_without_topic_data(db):
    for i in range(12):
        db.upsert_videos([{"id": f"j{i:010d}", "title": "", "author": "", "author_id": f"UC{i}",
                           "published": NOW, "duration": 900, "views": 100}])
    d = ranking.recommend(db, resolve_settings({}), record=False).diagnostics
    assert d["rabbit_hole"] is None


# 9 ---------------------------------------------------- labels by their bars
def test_each_label_sits_with_its_own_bar(client, db):
    add(db, "k0000000001", "A video")
    card = client.get("/").text.split('data-video="k0000000001"')[1].split("</article>")[0]
    assert '<ul class="whyrows">' in card and 'class="label"' in card and 'style="left: 0%;' in card


# 10 ---------------------------------------------------------------- AI
def test_ai_connection_and_the_key_stays_private(client, db, monkeypatch):
    client.post("/api/settings", json={"ai": {"provider": "anthropic"}})
    client.post("/api/ai/key", json={"api_key": "sk-secret"})
    status = client.get("/api/ai").json()
    assert status["provider"] == "anthropic" and status["has_key"] and "sk-secret" not in json.dumps(status)
    assert "sk-secret" not in client.get("/api/profile/export").text
    import httpx

    seen = {}

    def fake_post(url, **kwargs):
        seen["url"], seen["headers"] = url, kwargs.get("headers", {})
        return httpx.Response(200, json={"content": [{"text": "OK"}]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)
    assert client.post("/api/ai/test").json()["ok"]
    assert seen["url"].endswith("/v1/messages") and seen["headers"]["x-api-key"] == "sk-secret"


# 11 ---------------------------------------------------- homepage search
def test_homepage_search_keeps_order_and_changes_nothing(client, db):
    add(db, "l0000000001", "Kernel scheduling deep dive", description="kernel scheduler")
    add(db, "l0000000002", "Sourdough baking", description="bread flour")
    add(db, "l0000000003", "Kernel memory management", description="kernel memory pages")
    ids = ["l0000000003", "l0000000002", "l0000000001"]
    before = (db.scalar("SELECT COUNT(*) FROM impressions"), db.scalar("SELECT COUNT(*) FROM feedback"))
    r = client.post("/api/homepage/search", json={"q": "kernel", "ids": ids}).json()
    assert r["ids"] == ["l0000000003", "l0000000001"], "matches, in the page's order"
    assert r["mode"] == "math" and "AI search unavailable" in r["note"], "AI mode fell back"
    assert (db.scalar("SELECT COUNT(*) FROM impressions"), db.scalar("SELECT COUNT(*) FROM feedback")) == before
    assert client.post("/api/homepage/search", json={"q": "", "ids": ids}).json()["ids"] == ids


# 12 ------------------------------------------------------ tell Sieve why
def test_telling_sieve_why_changes_recommendations(client, db):
    add(db, "m0000000001", "Celebrity drama compilation", description="drama gossip celebrity")
    r = client.post("/api/videos/m0000000001/explain", json={"text": "too much drama, it drags on"}).json()
    assert r["verdict"] == "less" and "drama" in r["avoid"] and "shorter" in r["adjust"] and r["via"] == "keywords"
    assert db.scalar("SELECT weight FROM interests WHERE tag = 'drama'") < 0


# 13 ------------------------------------------------- missing names
def test_missing_title_and_channel_have_a_fallback(client, db):
    db.upsert_videos([{"id": "n0000000001", "title": "", "author": "", "author_id": "", "published": NOW,
                       "duration": 900, "views": 100}])
    card = client.get("/").text.split('data-video="n0000000001"')[1].split("</article>")[0]
    assert "Untitled video" in card and "Unknown channel" in card


# 14 --------------------------------------------------- incremental keywords
def test_keywords_are_fetched_incrementally(tmp_path):
    from sieve.db import Database
    from sieve.ingest import Ingestor
    from tests.test_pulling import FakeUpstream

    kdb = Database(str(tmp_path / "k.db"))
    api = FakeUpstream(kdb)
    shared = {"videoId": "sharedvid01", "title": "In both", "authorId": "UCs", "published": NOW,
              "lengthSeconds": 900, "viewCount": 100}
    api.search = lambda q, **k: (api.searched.append(q), [shared, {**shared, "videoId": f"{q[:4]}uniq001"[:11],
                                                                    "title": q}])[1]
    ing = Ingestor(offline_config(tmp_path), kdb, api, community=None)
    actions.save_settings(kdb, {"pull": {"topics": [], "custom_topics": ["alpha", "beta"]},
                                "compute": {"discover_terms": 0}})
    ing.fetch()
    assert api.searched == ["alpha", "beta"]
    api.searched.clear()
    actions.save_settings(kdb, {"pull": {"custom_topics": ["alpha", "beta", "gamma"]}})
    counts = ing.fetch()
    assert api.searched == ["gamma"] and counts["keywords_reused"] == 2, "only the new keyword"
    actions.save_settings(kdb, {"pull": {"custom_topics": ["alpha", "gamma"]}})
    ing.fetch()
    ids = {r["id"] for r in kdb.query("SELECT id FROM videos")}
    assert "betauniq001" not in ids, "only beta's own video goes"
    assert "sharedvid01" in ids, "a video another keyword still found stays"
    assert kdb.scalar("SELECT COUNT(*) FROM videos WHERE id = 'sharedvid01'") == 1, "no duplicates"


# 15 ------------------------------------------------------- recency boost
def test_recently_fetched_videos_get_a_fading_lift(db):
    add(db, "o0000000001", "Fresh find", first_seen=NOW)
    add(db, "o0000000002", "Day-old find", first_seen=NOW - 24 * 3600)
    add(db, "o0000000003", "From before", first_seen=NOW - 30 * 86400)
    items = {c.id: c for c in ranking.recommend(db, resolve_settings({}), record=False).items}
    fresh, day = items["o0000000001"].components["recent"], items["o0000000002"].components["recent"]
    assert fresh == pytest.approx(1.0, abs=0.01) and day == pytest.approx(0.5, abs=0.01), "halves per half-life"
    assert "recent" not in items["o0000000003"].components
    assert db.one("SELECT education FROM scores WHERE video_id='o0000000001'")["education"] == \
        db.one("SELECT education FROM scores WHERE video_id='o0000000003'")["education"], "never in the scores"
    db.upsert_videos([{"id": "o0000000003", "title": "From before"}])
    assert db.scalar("SELECT first_seen FROM videos WHERE id='o0000000003'") == NOW - 30 * 86400, \
        "seeing it again does not make it new"
    off = resolve_settings({"homepage": {"recent_boost": False}})
    assert all("recent" not in c.components for c in ranking.recommend(db, off, record=False).items)


def test_the_lift_never_undoes_hide_block_or_less(client, db):
    add(db, "p0000000001", "Hidden fresh", first_seen=NOW, channel="UCp1")
    add(db, "p0000000002", "Disliked fresh", first_seen=NOW, channel="UCp2")
    client.post("/api/hide", json={"kind": "video", "value": "p0000000001"})
    client.post("/api/feedback", json={"video_id": "p0000000002", "kind": "less"})
    items = {c.id: c for c in ranking.recommend(db, resolve_settings({}), record=False).items}
    assert "p0000000001" not in items
    assert "recent" not in items.get("p0000000002", ranking.Candidate(video={}, card=None, sources={})).components
