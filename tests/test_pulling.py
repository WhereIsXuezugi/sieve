"""Finding videos without an import, the pull limit, filling the homepage,
and the bugs fixed alongside them."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from sieve import actions, demo, profiles, ranking, scoring, starter
from sieve.app import _fmt_count, create_app
from sieve.config import resolve_settings
from sieve.db import Database
from sieve.ingest import Ingestor
from sieve.invidious import NotFound, UpstreamUnavailable
from sieve.pulls import PullBudget, PullLimitReached
from tests.support import offline_config

NOW = int(time.time())


def yt(vid, title, channel="UCaaaaaaaaaaaaaaaaaaaaaa", **extra):
    return {"videoId": vid, "title": title, "author": "Some Channel", "authorId": channel,
            "published": NOW - 3600, "lengthSeconds": extra.pop("duration", 900),
            "viewCount": 40000, **extra}


class FakeUpstream:
    """An upstream with a real pull budget but no network."""

    def __init__(self, db, dead=(), down=False, can_search=True):
        self.budget = PullBudget(db)
        self.dead, self.down, self.can_search = set(dead), down, can_search
        self.channels, self.searched = [], []

    def automatic(self):
        return self.budget.automatic()

    def status(self):
        return {"can_search": self.can_search}

    def channel_videos(self, channel_id, sort="newest"):
        self.budget.spend("channel")
        if self.down:
            raise UpstreamUnavailable("connection refused")
        if channel_id in self.dead:
            raise NotFound("no such channel")
        self.channels.append(channel_id)
        return [yt(f"{channel_id[-6:]}{i:05d}", f"Upload {i}", channel_id) for i in range(3)]

    def search(self, query, **params):
        self.budget.spend("search")
        self.searched.append(query)
        return [yt(f"s{abs(hash(query)) % 10**9:09d}{i}", f"{query} {i}") for i in range(2)]

    def playlist(self, playlist_id):
        return {"title": "", "videos": []}

    def trending(self, region="US", category=""):
        return []


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def ingestor_for(db, tmp_path, **fake):
    api = FakeUpstream(db, **fake)
    return Ingestor(offline_config(tmp_path), db, api, community=None), api


def subscribe(db, *channels):
    db.executemany("INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?,?,1,?)",
                   [(c, c, NOW) for c in channels])


# -- finding videos with no import ---------------------------------------------


def test_no_searches_when_the_backend_cannot_search(db, tmp_path):
    ingestor, api = ingestor_for(db, tmp_path, can_search=False)
    ingestor.fetch()
    assert api.searched == []


def test_every_channel_not_found_is_an_outage_and_blames_no_channel(db, tmp_path):
    """YouTube's feeds answer 404 for everything during an outage. Reading
    that as deleted channels rested every starter channel for a week."""
    ingestor, _ = ingestor_for(db, tmp_path, dead={cid for cid, _ in starter.channels_for(
        ["science", "engineering", "history", "technology"])})
    counts = ingestor.fetch()
    assert "failing for every channel" in counts["stopped"] and counts["pulls"] == 3
    assert db.scalar("SELECT COUNT(*) FROM channel_fetches WHERE failures > 0") == 0


def test_a_deleted_channel_no_longer_stops_the_sync(db, tmp_path):
    """Regression: one 404 raised UpstreamUnavailable and the loop `break`s,
    so every subscription after a deleted channel was never synced."""
    subscribe(db, "UCdead000000000000000000", "UCalive00000000000000000")
    actions.save_settings(db, {"pull": {"auto": False}})
    ingestor, api = ingestor_for(db, tmp_path, dead={"UCdead000000000000000000"})
    ingestor.sync()
    assert api.channels == ["UCalive00000000000000000"]


def test_an_outage_stops_the_sync_and_says_so(db, tmp_path):
    subscribe(db, *[f"UC{i:022d}" for i in range(10)])
    actions.save_settings(db, {"compute": {"discover_terms": 0}})
    ingestor, api = ingestor_for(db, tmp_path, down=True)
    counts = ingestor.sync()
    assert counts["stopped"] == "could not reach YouTube: the connection was refused"
    assert api.budget.used(60) == 3, "three failures in a row is an outage; stop there"


def test_subscriptions_rotate_so_a_cut_short_sync_resumes(db, tmp_path):
    channels = [f"UC{i:022d}" for i in range(6)]
    subscribe(db, *channels)
    actions.save_settings(db, {"pull": {"auto": False, "limit_enabled": True, "limit_count": 3,
                                        "limit_manual": True}})
    ingestor, api = ingestor_for(db, tmp_path)
    first = ingestor.sync()
    assert "request limit reached" in first["stopped"] and len(api.channels) == 3
    assert first["pulls"] == 3
    db.execute("DELETE FROM pulls")
    ingestor.sync()
    assert set(api.channels) == set(channels), "the second sync starts with the ones it missed"


def test_followed_channels_are_ones_the_ranking_rates_well(db, tmp_path):
    good, meh, blocked = "UC" + "g" * 22, "UC" + "m" * 22, "UC" + "b" * 22
    db.executemany("INSERT INTO channels(id, name, quality, affinity) VALUES(?,?,?,?)",
                   [(good, "Good", 80, 0.5), (meh, "Meh", 40, 0.0), (blocked, "Blocked", 90, 0.9)])
    db.execute("INSERT INTO channel_prefs(channel_id, listing) VALUES(?, 'block')", (blocked,))
    actions.save_settings(db, {"compute": {"discover_terms": 0}})
    db.set_setting("pull_state", {"first_fetch_at": NOW})
    ingestor, api = ingestor_for(db, tmp_path)
    ingestor.sync()
    assert api.channels == [good]


def test_real_videos_replace_the_demo_catalogue(db, tmp_path):
    demo.seed_demo(db, 40)
    assert demo.demo_count(db) == 40
    actions.save_settings(db, {"pull": {"topics": ["space"]}, "compute": {"discover_terms": 0}})
    ingestor, _ = ingestor_for(db, tmp_path)
    counts = ingestor.fetch()
    assert counts["demo_removed"] == 40 and demo.demo_count(db) == 0
    assert db.scalar("SELECT COUNT(*) FROM history WHERE origin = 'demo'") == 0
    assert db.scalar("SELECT COUNT(*) FROM subscriptions WHERE channel_id LIKE 'UC\\_%' ESCAPE '\\'") == 0


def test_the_demo_is_kept_when_nothing_real_arrived(db, tmp_path):
    demo.seed_demo(db, 20)
    ingestor, _ = ingestor_for(db, tmp_path, down=True)
    ingestor.fetch()
    assert demo.demo_count(db) == 20


def test_starter_topics_interleave_so_a_small_budget_reaches_each():
    picked = starter.channels_for(["science", "history"])
    assert picked[0][0] == starter.TOPICS["science"].channels[0][0]
    assert picked[1][0] == starter.TOPICS["history"].channels[0][0]


# -- the pull limit --------------------------------------------------------------


def test_the_limit_binds_automatic_work_but_counts_everything(db):
    budget = PullBudget(db)
    actions.save_settings(db, {"pull": {"limit_enabled": True, "limit_count": 2}})
    with budget.automatic():
        budget.spend("channel")
        budget.spend("search")
        with pytest.raises(PullLimitReached):
            budget.spend("channel")
    budget.spend("channel")   # you asked: goes through, and is counted
    status = budget.status()
    assert status["used"] == 3 and status["automatic"] == 2 and status["manual"] == 1
    actions.save_settings(db, {"pull": {"limit_manual": True}})
    with pytest.raises(PullLimitReached):
        budget.spend("channel")


def test_the_window_rolls(db):
    budget = PullBudget(db)
    actions.save_settings(db, {"pull": {"limit_enabled": True, "limit_count": 1, "limit_window": 15}})
    db.execute("INSERT INTO pulls(at, kind, automatic) VALUES(?, 'channel', 1)", (NOW - 16 * 60,))
    with budget.automatic():
        budget.spend("channel")   # the old one aged out of the 15-minute window
        with pytest.raises(PullLimitReached, match="15 minutes"):
            budget.spend("channel")


def test_cache_hits_are_free(tmp_path):
    """Only a request that leaves the machine is a pull."""
    from sieve.upstream import Upstream

    cfg = offline_config(tmp_path)
    db = Database(cfg.db_path)
    api = Upstream(cfg, db)
    api.youtube._store("yt:rss:channel_id=UCx", {"title": "", "videos": []}, 3600)
    actions.save_settings(db, {"source": {"backend": "youtube"}})
    api.channel_videos("UCx")
    assert api.budget.used(60) == 0
    with pytest.raises(UpstreamUnavailable):
        api.channel_videos("UCy")   # a closed port: a real request
    assert api.budget.used(60) == 1
    api.close()


def test_the_limit_stops_the_upstream_without_trying_the_next_backend(tmp_path):
    from sieve.upstream import Upstream

    cfg = offline_config(tmp_path)
    db = Database(cfg.db_path)
    api = Upstream(cfg, db)
    actions.save_settings(db, {"pull": {"limit_enabled": True, "limit_count": 1, "limit_manual": True}})
    db.execute("INSERT INTO pulls(at, kind, automatic) VALUES(?, 'x', 0)", (NOW,))
    with pytest.raises(PullLimitReached):
        api.channel_videos("UCz")
    assert api.budget.used(60) == 1 and api._down_until == 0
    api.close()


# -- settings ----------------------------------------------------------------------


@pytest.mark.parametrize("sent, stored", [(0, 1), (57, 57), (500, 100), ("12", 12)])
def test_the_homepage_holds_one_to_a_hundred(sent, stored):
    assert profiles.sanitise_settings({"homepage": {"count": sent}})["homepage"]["count"] == stored


def test_string_booleans_mean_what_they_say():
    """Regression: bool("false") is True, so sending strings switched things on."""
    out = profiles.sanitise_settings({"filters": {"hide_shorts": "false"}, "pull": {"auto": "off"}})
    assert out["filters"]["hide_shorts"] is False and out["pull"]["auto"] is False


def test_pull_settings_are_whitelisted_and_clamped():
    out = profiles.sanitise_settings({"pull": {
        "topics": ["science", "not-a-topic"], "limit_window": 5, "limit_count": -3,
        "region": "gb", "per_pull": 10**6, "unknown": 1}})["pull"]
    assert out == {"topics": ["science"], "limit_window": 15, "limit_count": 1,
                   "region": "GB", "per_pull": 50}


def test_filters_cannot_go_negative():
    out = profiles.sanitise_settings({"filters": {"min_duration": -60, "max_brainrot": 400}})
    assert out["filters"] == {"min_duration": 0, "max_brainrot": 100}


# -- filling the homepage ------------------------------------------------------------


def catalogue(db, n=10, **video):
    videos = [{"id": f"x{i:010d}", "title": f"Video {i}", "author": f"C{i}",
               "author_id": f"UC{i:022d}", "published": NOW - i * 60, "duration": 900,
               "views": 5000, **video} for i in range(n)]
    db.upsert_videos(videos)
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(dict(db.get_video(v["id"]))))
                                          for v in videos])


def page(db, **settings):
    base = {"sources": {"subscriptions": 100, "history": 0, "playlists": 0, "discovery": 0,
                        "interests": 0, "niche": 0}, "homepage": {"count": 6}}
    for key, value in settings.items():
        base.setdefault(key, {}).update(value)
    return ranking.recommend(db, resolve_settings(base), record=False, ledger=True)


def test_catalogue_fill_tops_up_a_page_no_source_filled(db):
    catalogue(db)
    result = page(db)   # nothing subscribed: the source finds nothing
    assert len(result.items) == 6 and result.diagnostics["filled"] == 6
    assert all(c.filler == "catalogue" for c in result.items)
    assert page(db, homepage={"fill": "off"}).items == []


def test_catalogue_fill_keeps_every_filter(db):
    catalogue(db, duration=300)
    assert page(db, filters={"min_duration": 600}).items == []


def test_relaxed_fill_says_which_filter_each_video_fails(db):
    catalogue(db, duration=300)
    result = page(db, homepage={"fill": "relaxed"}, filters={"min_duration": 600})
    assert len(result.items) == 6
    assert all("shorter than your minimum duration" in ranking._filler_reason(c) for c in result.items)
    shown = [e for e in result.ledger if e["stage"] == "shown"]
    assert len(shown) == 6 and all(e["reason"].startswith("filled in despite") for e in shown)


def test_relaxing_never_shows_hidden_blocked_or_watched_videos(db):
    catalogue(db, n=3)
    actions.add_block(db, "video", "x0000000000")
    db.execute("INSERT INTO channel_prefs(channel_id, listing) VALUES(?, 'block')", (f"UC{1:022d}",))
    db.record_watch("x0000000002", 1.0)
    assert page(db, homepage={"fill": "relaxed"}).items == []


def test_an_unknown_like_count_does_not_trip_the_like_filter():
    video = {"id": "y", "views": 1000, "likes": 0, "duration": 900}
    assert ranking._gate(video, scoring.ScoreCard("y"),
                         resolve_settings({"filters": {"min_like_ratio": 0.05, "max_nsfw": 100}}),
                         {}, set(), []) is None


# -- pages ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(offline_config(tmp_path), start_worker=False))


def test_the_rabbit_hole_button_posts_somewhere_that_exists(client):
    """Regression: it posted a form to /settings, which only answers GET."""
    html = client.get("/").text
    assert 'action="/settings"' not in html
    assert client.post("/settings").status_code == 405


def test_the_debugger_does_not_count_as_an_impression(client):
    db = client.app.state.db
    catalogue(db)
    client.get("/debugger")
    client.post("/api/critique")
    assert db.scalar("SELECT COUNT(*) FROM impressions") == 0


def test_videos_play_in_sieves_player_until_a_provider_is_chosen(client):
    location = TestClient(client.app, follow_redirects=False).get("/open/dQw4w9WgXcQ").headers["location"]
    assert location == "/play/dQw4w9WgXcQ"


def test_remove_demo_endpoint(client):
    demo.seed_demo(client.app.state.db, 30)
    assert client.post("/api/catalogue/remove-demo").json()["removed"] == 30
    assert demo.demo_count(client.app.state.db) == 0


def test_the_pulls_endpoint_reports_usage(client):
    client.post("/api/settings", json={"pull": {"limit_enabled": True, "limit_count": 50}})
    body = client.get("/api/pulls").json()
    assert body["limit"] == 50 and body["window"] == "day" and "summary" in body


@pytest.mark.parametrize("value, text", [
    (0, "0"), (999, "999"), (1234, "1.2K"), (999_999, "1M"), (12_500_000, "12.5M"),
])
def test_counts_read_naturally(value, text):
    assert _fmt_count(value) == text


def test_an_unscored_video_is_not_judged_on_scores_it_does_not_have():
    """A video that arrived seconds ago has a blank scorecard, which reads as
    50 on every axis: above the default nudity limit of 25, so every fresh
    video was hidden until the worker scored it."""
    blank = scoring.ScoreCard("fresh")
    fresh = {"id": "fresh", "duration": 900, "family_safe": 1}
    assert ranking._gate(fresh, blank, resolve_settings({}), {}, set(), []) is None
    adult = {**fresh, "family_safe": 0}
    assert "family-safe" in ranking._gate(adult, blank, resolve_settings({}), {}, set(), [])


def test_optional_work_waits_while_the_limit_is_tight(db, tmp_path):
    """A Takeout import used to spend the whole limit on backfill before any
    sync got a pull."""
    db.record_watch("abcdefghijk", 0.9)
    ingestor, api = ingestor_for(db, tmp_path)
    actions.save_settings(db, {"pull": {"limit_enabled": True, "limit_count": 10}})
    for _ in range(6):
        db.execute("INSERT INTO pulls(at, kind, automatic) VALUES(?, 'x', 1)", (NOW,))
    with api.automatic():
        assert api.budget.tight() and ingestor.backfill() == 0


def test_videos_from_followed_channels_have_their_own_source(db):
    catalogue(db, n=2)
    db.execute("INSERT INTO channel_fetches(channel_id, fetched_at, origin) VALUES(?,?, 'followed')",
               (f"UC{0:022d}", NOW))
    pool = ranking.gather_candidates(db, resolve_settings({}))
    assert pool["x0000000000"].get("followed") == 0.8
    result = page(db, homepage={"fill": "off"})
    assert [c.id for c in result.items] == ["x0000000000"]
    assert ranking.explain(result.items[0])[0]["label"] in (
        "from a channel Sieve follows for you", "matches your score targets", "recently published",
        "recently fetched")


def test_a_video_with_no_upload_date_still_reaches_the_page(db):
    catalogue(db, n=1, published=0)
    db.execute("INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?,?,1,?)",
               (f"UC{0:022d}", "", NOW))
    assert "x0000000000" in ranking.gather_candidates(db, resolve_settings({}))


def dev_server_upstream(tmp_path, status=404, body="<html>Cannot GET</html>"):
    import httpx

    from sieve.config import Config
    from sieve.upstream import Upstream

    cfg = Config(data_dir=str(tmp_path), instances=["http://devserver"], youtube_url="http://yt",
                 use_ytdlp=False, request_timeout=1)
    db = Database(cfg.db_path)
    api = Upstream(cfg, db)

    def handler(request):
        if request.url.host == "devserver":
            return httpx.Response(status, text=body)
        return httpx.Response(200, text=FEED)

    for client in (api.invidious, api.youtube):
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return api


FEED = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:yt="http://www.youtube.com/xml/schemas/2015"><title>C</title>
<entry><yt:videoId>abcdefghijk</yt:videoId><yt:channelId>UCx</yt:channelId><title>A video</title>
<published>2026-09-01T00:00:00+00:00</published></entry></feed>"""


def test_something_else_on_the_invidious_port_is_not_an_invidious(tmp_path):
    """A dev server on port 3000 answers 404 for everything. That was read as
    "no such channel", so a Mac running one never fetched a single video."""
    api = dev_server_upstream(tmp_path)
    assert [v["videoId"] for v in api.channel_videos("UCx")] == ["abcdefghijk"]
    assert api.budget.used(60) == 1, "the dead Invidious costs nothing"
    api.close()


def test_a_real_invidious_not_found_is_believed(tmp_path):
    api = dev_server_upstream(tmp_path, body='{"error": "This channel does not exist."}')
    api._health = (time.time(), True)
    with pytest.raises(NotFound):
        api.channel_videos("UCgone")
    api.close()


# -- ranking fixes from the logic review ----------------------------------------------


def test_quotas_are_not_dropped_to_fill_the_page(db):
    """"Memes: 1%" showed six memes: when the per-channel cap blocked the
    rest, the arranger dropped the quotas without saying so."""
    demo.seed_demo(db, 200)
    settings = resolve_settings({"budget": {"enabled": True, "quotas": {"meme": 1, "education": 99}},
                                 "homepage": {"fill": "catalogue"}})
    items = ranking.recommend(db, settings, record=False).items
    assert sum(1 for c in items if c.bucket == "meme") == 0
    relaxed = resolve_settings({"budget": {"enabled": True, "quotas": {"meme": 1, "education": 99}},
                                "homepage": {"fill": "relaxed", "count": 60}})
    assert len(ranking.recommend(db, relaxed, record=False).items) == 60


def test_the_video_you_are_watching_is_never_capped_off(db):
    demo.seed_demo(db, 200)
    db.record_watch("demo0150", 0.4)
    items = ranking.recommend(db, resolve_settings({}), record=False).items
    assert items[0].id == "demo0150"


def test_abandoned_videos_do_not_flood_the_page(db):
    from collections import Counter

    demo.seed_demo(db, 200)
    items = ranking.recommend(db, resolve_settings({"homepage": {"count": 36}}), record=False).items
    assert max(Counter(c.channel_id for c in items).values()) <= 3 + 36 // 4


def test_a_short_page_says_which_limit_held_it_back(client):
    demo.seed_demo(client.app.state.db, 200)
    client.post("/api/settings", json={"homepage": {"count": 100}})
    html = client.get("/").text
    assert "Showing 36 of 100" in html and "(max 3 videos per channel)" in html
    assert "Fill the page anyway" in html


# -- nothing is pulled until you ask ----------------------------------------------------


def test_a_fresh_install_pulls_nothing_until_you_fetch(db, tmp_path):
    ingestor, api = ingestor_for(db, tmp_path)
    assert not ingestor.has_sources(), "the worker's scheduled sync waits for something to refresh"
    counts = ingestor.sync()
    assert "nothing to sync yet" in counts["stopped"]
    assert api.channels == [] and api.searched == [] and api.budget.used(60) == 0


def test_fetch_pulls_the_starter_channels_for_your_topics(db, tmp_path):
    actions.save_settings(db, {"pull": {"topics": ["mathematics"]}, "compute": {"discover_terms": 0}})
    ingestor, api = ingestor_for(db, tmp_path)
    counts = ingestor.fetch()
    expected = {cid for cid, _ in starter.TOPICS["mathematics"].channels}
    assert set(api.channels) == expected
    assert counts["from"]["starter"] == 3 * len(expected) == counts["new"] == db.playable_count()
    assert ingestor.has_fetched() and ingestor.has_sources()


def test_sync_never_pulls_starter_channels(db, tmp_path):
    actions.save_settings(db, {"pull": {"topics": ["mathematics"], "follow_channels": 0},
                               "compute": {"discover_terms": 0}})
    ingestor, api = ingestor_for(db, tmp_path)
    ingestor.fetch()
    api.channels.clear()
    assert ingestor.sync()["from"]["starter"] == 0 and api.channels == []


def test_fetch_searches_your_phrases_then_your_topics(db, tmp_path):
    actions.save_settings(db, {"pull": {"topics": ["history"], "custom_topics": ["woodturning"],
                                        "starter_channels": False},
                               "compute": {"discover_terms": 2}})
    ingestor, api = ingestor_for(db, tmp_path)
    ingestor.fetch()
    # Your phrase always; then as many topic phrases as discovery searches allows.
    assert api.searched == ["woodturning", *starter.TOPICS["history"].searches[:2]]


def test_fetch_needs_a_topic(db, tmp_path):
    actions.save_settings(db, {"pull": {"topics": [], "custom_topics": []}})
    ingestor, _ = ingestor_for(db, tmp_path)
    with pytest.raises(actions.ActionError, match="at least one topic"):
        ingestor.fetch()


def test_fetch_endpoint_and_empty_homepage(client):
    html = client.get("/").text
    assert "Ready when you are" in html and "Nothing is fetched until you press the button" in html
    assert 'data-action="/api/fetch"' in html
    body = client.post("/api/fetch").json()   # nothing answers in the test config
    assert body["ok"] and body["new"] == 0 and body["stopped"]
    assert "Could not reach YouTube or Invidious" in client.get("/").text
    client.post("/api/settings", json={"pull": {"topics": [], "custom_topics": []}})
    assert client.post("/api/fetch").status_code == 400


def test_every_page_has_fetch_and_sync_in_the_header(client):
    for path in ("/", "/settings", "/channels", "/history"):
        header = client.get(path).text.split("<main>")[0]
        assert 'data-action="/api/fetch"' in header and 'data-action="/api/sync"' in header


# -- deleting pulled videos --------------------------------------------------------------


def test_reset_pulled_keeps_what_is_yours(db, tmp_path):
    actions.save_settings(db, {"pull": {"topics": ["mathematics"]}, "compute": {"discover_terms": 0}})
    ingestor, _ = ingestor_for(db, tmp_path)
    ingestor.fetch()
    found = [r["id"] for r in db.query("SELECT id FROM videos")]
    watched, rated = found[0], found[1]
    db.record_watch(watched, 1.0)
    db.execute("INSERT INTO feedback(video_id, kind, created_at) VALUES(?, 'more', ?)", (rated, NOW))
    db.upsert_videos([{"id": "mine0000001", "title": "Mine", "author_id": "UCmine", "origin": "subscription"}])
    with pytest.raises(actions.ActionError):
        actions.reset_pulled(db, "no")
    result = actions.reset_pulled(db, "reset", backup=False)
    left = {r["id"] for r in db.query("SELECT id FROM videos")}
    assert left == {watched, rated, "mine0000001"} and result["videos"] == len(found) - 2
    assert not ingestor.has_fetched(), "the next Fetch starts from scratch"


def test_a_video_keeps_the_origin_it_arrived_with(db):
    db.upsert_videos([{"id": "abcdefghijk", "title": "t", "origin": "subscription"}])
    db.upsert_videos([{"id": "abcdefghijk", "title": "t", "origin": "search"}])
    assert db.scalar("SELECT origin FROM videos WHERE id = 'abcdefghijk'") == "subscription"


def test_reset_the_pull_counter(client):
    db = client.app.state.db
    db.execute("INSERT INTO pulls(at, kind, automatic) VALUES(?, 'channel', 1)", (NOW,))
    assert client.post("/api/pulls/reset").json()["forgotten"] == 1
    assert client.get("/api/pulls").json()["used"] == 0


# -- backups ----------------------------------------------------------------------------


def test_backups_can_be_made_listed_and_deleted(client):
    name = client.post("/api/backups").json()["backup"]
    listed = client.get("/api/backups").json()["backups"]
    assert listed[0]["name"] == name and listed[0]["kind"] == "manual"
    assert client.delete(f"/api/backups/{name}").json()["ok"]
    assert client.get("/api/backups").json()["backups"] == []
    assert client.delete("/api/backups/..%2Fsieve.db").status_code == 404, "a name is never a path"


def test_roll_back_to_a_backup_in_place_and_undo_it(client):
    db = client.app.state.db
    demo.seed_demo(db, 30)
    name = client.post("/api/backups").json()["backup"]
    client.post("/api/catalogue/remove-demo")
    assert db.stats()["videos"] == 0
    assert client.post(f"/api/backups/{name}/restore", json={"confirm": "nope"}).status_code == 400
    result = client.post(f"/api/backups/{name}/restore", json={"confirm": "restore"}).json()
    assert db.stats()["videos"] == 30, "restored without a restart"
    assert client.get("/").status_code == 200
    # The state it replaced was saved first, so the rollback can be undone.
    client.post(f"/api/backups/{result['safety_backup']}/restore", json={"confirm": "restore"})
    assert db.stats()["videos"] == 0


def test_restoring_an_older_backup_brings_its_schema_up_to_date(client, tmp_path):
    import sqlite3

    from sieve import backups

    db = client.app.state.db
    path = backups.create(db, "manual")
    old = sqlite3.connect(path)
    old.execute("DROP TABLE pulls")
    old.commit()
    old.close()
    backups.restore(db, path.name, "restore")
    assert db.scalar("SELECT COUNT(*) FROM pulls") == 0


def test_each_kind_keeps_its_own_newest(db):
    from sieve import backups

    actions.save_settings(db, {"backups": {"keep": 2}})
    before_reset = backups.create(db, "before-reset")
    for _ in range(4):
        backups.create(db, "auto")
    kinds = [b["kind"] for b in backups.listing(db)]
    assert kinds.count("auto") == 2 and before_reset.exists(), "a schedule never pushes out a reset's backup"


def test_automatic_backups_follow_the_schedule(db):
    import os

    from sieve import backups

    assert not backups.auto_due(db), "off by default"
    actions.save_settings(db, {"backups": {"auto": True, "every_hours": 24}})
    assert backups.auto_due(db)
    made = backups.create(db, "auto")
    assert not backups.auto_due(db)
    os.utime(made, (NOW - 25 * 3600, NOW - 25 * 3600))
    assert backups.auto_due(db)
