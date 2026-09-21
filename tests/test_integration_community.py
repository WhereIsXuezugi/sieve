"""End-to-end check of the two community integrations.

The real APIs are not reachable from a test run, so the HTTP layer is stubbed
with responses shaped exactly like the published ones. What is being tested is
our wiring: prefix batching, caching, the effect on ranking, and the title and
thumbnail substitution the user actually sees.
"""

import time
from typing import ClassVar

import pytest

from sieve import community, ranking, scoring
from sieve.app import _display_title, _thumb
from sieve.config import Config, deep_merge, resolve_settings
from sieve.db import Database

VIDEOS = [
    {
        "id": "bait0001", "title": "I CAN'T BELIEVE THIS HAPPENED!!! 😱",
        "author": "Clip Channel", "author_id": "UCbait", "published": int(time.time()) - 86400,
        "duration": 900, "views": 500000, "likes": 20000,
        "description": "sponsored by a vpn, use code TEST", "keywords": ["shocking"],
        "genre": "Entertainment", "is_live": 0, "is_upcoming": 0, "family_safe": 1,
        "sub_count": 900000,
    },
    {
        "id": "plain001", "title": "Rebuilding a bandsaw gearbox",
        "author": "Fix It", "author_id": "UCfix", "published": int(time.time()) - 86400,
        "duration": 1800, "views": 40000, "likes": 3000,
        "description": "0:00 teardown\n6:00 bearings", "keywords": ["repair"],
        "genre": "Howto & Style", "is_live": 0, "is_upcoming": 0, "family_safe": 1,
        "sub_count": 60000,
    },
]


class StubResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class StubClient:
    """Stands in for httpx, and answers only for the prefix that was asked for.

    Modelling the prefix behaviour matters: a stub that returned every video for
    every request would hide bugs in our batching and caching.
    """

    SEGMENTS: ClassVar[list[dict]] = [
        {
            "videoID": "bait0001",
            "segments": [
                {"category": "sponsor", "segment": [30, 330], "videoDuration": 900,
                 "votes": 12, "locked": 0, "UUID": "a", "actionType": "skip"},
                {"category": "filler", "segment": [400, 460], "videoDuration": 900,
                 "votes": 3, "locked": 0, "UUID": "b", "actionType": "skip"},
            ],
        },
        {
            "videoID": "plain001",
            "segments": [
                {"category": "intro", "segment": [0, 8], "videoDuration": 1800,
                 "votes": 5, "locked": 1, "UUID": "c", "actionType": "skip"},
            ],
        },
    ]

    BRANDING: ClassVar[dict[str, dict]] = {
        "bait0001": {
            "titles": [
                {"title": "I CAN'T BELIEVE THIS HAPPENED!!! \U0001F631", "original": True, "votes": 0},
                {"title": "Reviewing a delayed package", "original": False, "votes": 9, "locked": 1},
            ],
            "thumbnails": [{"timestamp": 120.0, "original": False, "votes": 4}],
        },
    }

    def __init__(self):
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        prefix = url.rsplit("/", 1)[-1]
        if "/api/skipSegments/" in url:
            hits = [e for e in self.SEGMENTS if community.hash_prefix(e["videoID"]) == prefix]
            return StubResponse(hits) if hits else StubResponse([], 404)
        if "/api/branding/" in url:
            hits = {k: v for k, v in self.BRANDING.items() if community.hash_prefix(k) == prefix}
            return StubResponse(hits) if hits else StubResponse({}, 404)
        return StubResponse({}, 404)

    def close(self):
        pass


@pytest.fixture()
def wired(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.upsert_videos(VIDEOS)
    db.executemany(scoring.INSERT_SCORE,
                   [scoring.card_to_row(scoring.score_video(v)) for v in VIDEOS])
    cfg = Config(data_dir=str(tmp_path))
    client = community.CommunityData(cfg, db)
    stub = StubClient()
    client._client = stub
    return db, cfg, client, stub


def enabled(**patch):
    return resolve_settings(deep_merge({
        "dearrow": {"enabled": True},
        "sponsorblock": {"enabled": True},
        "filters": {"hide_shorts": False},
    }, patch))


# -- fetching --------------------------------------------------------------


def test_segments_are_fetched_and_stored(wired):
    _db, _, client, _ = wired
    client.fetch_segments(["bait0001", "plain001"])
    data = client.segments_for("bait0001")
    assert data["sponsor_ratio"] == pytest.approx(300 / 900, abs=1e-4)
    assert data["filler_ratio"] == pytest.approx(60 / 900, abs=1e-4)
    assert len(data["segments"]) == 2
    assert data["segments"][0]["category"] == "sponsor"


def test_branding_is_fetched_and_stored(wired):
    _, _, client, _ = wired
    client.fetch_branding(["bait0001"])
    brand = client.branding_map(["bait0001"])["bait0001"]
    assert brand["title"] == "Reviewing a delayed package"
    assert brand["thumb_time"] == 120.0


def test_lookups_go_out_by_hash_prefix_only(wired):
    """The video id must never appear in an outgoing URL."""
    _, _, client, stub = wired
    client.fetch_segments(["bait0001", "plain001"])
    client.fetch_branding(["bait0001", "plain001"])
    assert stub.calls
    for url in stub.calls:
        assert "bait0001" not in url
        assert "plain001" not in url
        prefix = url.rsplit("/", 1)[-1]
        assert len(prefix) == 4 and all(c in "0123456789abcdef" for c in prefix)


def test_a_second_pass_hits_the_cache(wired):
    _, _, client, stub = wired
    client.fetch_segments(["bait0001", "plain001"])
    first = len(stub.calls)
    client.fetch_segments(["bait0001", "plain001"])
    assert len(stub.calls) == first, "cached prefixes must not be refetched"


def test_disabled_settings_make_no_requests(wired):
    _, _, client, stub = wired
    client.enrich(["bait0001"], resolve_settings({}))
    assert stub.calls == []


# -- effect on what the user sees ------------------------------------------


def test_dearrow_title_replaces_the_clickbait(wired):
    db, _cfg, client, _ = wired
    result = ranking.recommend(db, enabled(), community=client)
    item = next(c for c in result.items if c.id == "bait0001")
    shown = _display_title(item, enabled())
    assert shown["text"] == "Reviewing a delayed package"
    assert shown["original"].startswith("I CAN'T BELIEVE")
    assert shown["source"] == "DeArrow"


def test_original_title_can_be_kept(wired):
    db, _, client, _ = wired
    settings = enabled(dearrow={"replace_titles": False})
    result = ranking.recommend(db, settings, community=client)
    item = next(c for c in result.items if c.id == "bait0001")
    assert _display_title(item, settings)["text"].startswith("I CAN'T BELIEVE")


def test_dearrow_thumbnail_is_requested_at_the_voted_timestamp(wired):
    db, cfg, client, _ = wired
    result = ranking.recommend(db, enabled(), community=client)
    item = next(c for c in result.items if c.id == "bait0001")
    url = _thumb(cfg, item, enabled())
    assert "time=120.0" in url and "bait0001" in url


def test_sponsor_ratio_can_hide_a_video(wired):
    db, _, client, _ = wired
    result = ranking.recommend(db, enabled(sponsorblock={"max_sponsor_ratio": 0.2}),
                               community=client)
    assert all(item.id != "bait0001" for item in result.items)
    reasons = " ".join(row["reason"] for row in result.diagnostics["rejected"])
    assert "sponsor read" in reasons


def test_sponsor_ratio_can_merely_penalise(wired):
    db, _, client, _ = wired
    without = ranking.recommend(db, enabled(), community=client)
    with_penalty = ranking.recommend(db, enabled(sponsorblock={"score_penalty": 3.0}),
                                     community=client)
    before = next(c for c in without.items if c.id == "bait0001").score
    after = next(c for c in with_penalty.items if c.id == "bait0001").score
    assert after < before


def test_exclusive_access_flag_is_honoured(wired):
    db, _, client, _ = wired
    client.fetch_segments(["bait0001"])
    db.execute("UPDATE sponsor_segments SET has_exclusive_access = 1 WHERE video_id = ?",
               ("bait0001",))
    result = ranking.recommend(db, enabled(sponsorblock={"hide_exclusive_access": True}),
                               community=client)
    assert all(item.id != "bait0001" for item in result.items)


def test_retitled_videos_can_be_targeted_by_a_rule(wired):
    db, _, client, _ = wired
    result = ranking.recommend(db, enabled(rules={
        "enabled": True,
        "expr": {"field": "dearrow_retitled", "op": "is_false"},
    }), community=client)
    assert all(item.id != "bait0001" for item in result.items)


def test_segments_reach_the_player_endpoint_shape(wired):
    _, _, client, _ = wired
    client.fetch_segments(["bait0001"])
    payload = client.segments_for("bait0001")
    for segment in payload["segments"]:
        assert {"category", "start", "end", "action"} <= set(segment)
        assert segment["end"] >= segment["start"]
