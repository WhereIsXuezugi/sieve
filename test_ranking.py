import time

import pytest

from sieve import channels, ranking, scoring
from sieve.config import deep_merge, resolve_settings
from sieve.db import Database


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    now = int(time.time())
    videos = []
    for index in range(30):
        channel = f"UC{index % 3}"
        videos.append({
            "id": f"v{index:03d}",
            "title": f"Video {index} about kernels and allocators",
            "author": f"Channel {index % 3}",
            "author_id": channel,
            "published": now - index * 3600,
            "duration": 1200,
            "views": 5000 + index * 100,
            "likes": 200,
            "description": "0:00 intro\n4:00 body",
            "keywords": ["kernel", "systems"],
            "genre": "Science & Technology",
            "is_live": 0, "is_upcoming": 0, "family_safe": 1, "sub_count": 20000,
        })
    database.upsert_videos(videos)
    database.executemany(
        scoring.INSERT_SCORE,
        [scoring.card_to_row(scoring.score_video(v, "technical narration about the kernel"))
         for v in videos],
    )
    database.executemany(
        "INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?,?,1.0,?)",
        [(f"UC{i}", f"Channel {i}", now) for i in range(3)],
    )
    return database


def settings(**patch):
    return resolve_settings(deep_merge({}, patch))


# -- channel policy --------------------------------------------------------


def test_priority_is_clamped_to_the_documented_range(db):
    assert channels.set_preference(db, "UC0", priority=99)["priority"] == 5
    assert channels.set_preference(db, "UC0", priority=-99)["priority"] == -5


def test_blocked_channel_disappears(db):
    channels.set_preference(db, "UC0", listing="block")
    result = ranking.recommend(db, settings())
    assert all(item.channel_id != "UC0" for item in result.items)
    reasons = [row["reason"] for row in result.diagnostics["rejected"]]
    assert any("block list" in reason for reason in reasons)


def test_blocked_channel_can_be_buried_instead_of_hidden(db):
    channels.set_preference(db, "UC0", listing="block")
    result = ranking.recommend(db, settings(channels={"blocked_hidden": False}))
    positions = [i for i, item in enumerate(result.items) if item.channel_id == "UC0"]
    assert positions, "with blocked_hidden off the channel should still appear"
    assert min(positions) > len(result.items) / 2, "but it should be pushed to the back"


def test_whitelist_only_admits_nothing_else(db):
    channels.set_preference(db, "UC1", listing="allow")
    result = ranking.recommend(db, settings(channels={"whitelist_only": True}))
    assert result.items
    assert {item.channel_id for item in result.items} == {"UC1"}


def test_manual_priority_lifts_a_channel(db):
    baseline = ranking.recommend(db, settings())
    before = [item.channel_id for item in baseline.items[:6]].count("UC2")
    channels.set_preference(db, "UC2", priority=5)
    after_result = ranking.recommend(db, settings(diversity={"enabled": False}))
    after = [item.channel_id for item in after_result.items[:6]].count("UC2")
    assert after >= before


def test_negative_priority_buries_without_hiding(db):
    channels.set_preference(db, "UC2", priority=-5)
    result = ranking.recommend(db, settings(diversity={"enabled": False}))
    ranked = [item.channel_id for item in result.items]
    assert "UC2" in ranked, "a deprioritised channel is not a blocked channel"
    assert ranked.index("UC2") > 0


# -- derived affinity ------------------------------------------------------


def test_affinity_follows_watch_time(db):
    now = int(time.time())
    db.executemany(
        "INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,?,?,?)",
        [(f"v{i:03d}", now - 3600, 0.95, 0, "test") for i in range(0, 30, 3)],  # all UC0
    )
    updated = channels.recompute_affinity(db)
    assert updated >= 1
    policy = channels.load_policy(db, settings())
    assert policy.affinity["UC0"] > policy.affinity.get("UC1", 0.0)


def test_affinity_discounts_videos_you_bail_out_of(db):
    now = int(time.time())
    db.executemany(
        "INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,?,?,?)",
        [(f"v{i:03d}", now - 3600, 1.0, 0, "t") for i in range(0, 30, 3)]      # UC0, finished
        + [(f"v{i:03d}", now - 3600, 0.05, 0, "t") for i in range(1, 30, 3)],  # UC1, bounced
    )
    channels.recompute_affinity(db)
    policy = channels.load_policy(db, settings())
    assert policy.affinity["UC0"] > policy.affinity["UC1"]


def test_manual_priority_and_affinity_stay_separate(db):
    """Recomputing derived stats must never overwrite a decision the user made."""
    channels.set_preference(db, "UC0", priority=4)
    now = int(time.time())
    db.executemany(
        "INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,?,?,?)",
        [(f"v{i:03d}", now, 0.9, 0, "t") for i in range(0, 30, 3)],
    )
    channels.recompute_affinity(db)
    policy = channels.load_policy(db, settings())
    assert policy.priority["UC0"] == 4
    assert policy.affinity["UC0"] > 0
    _, notes = policy.score("UC0", 0.18, 0.5, True)
    kinds = {note["kind"] for note in notes}
    assert {"manual", "derived"} <= kinds, "both must be reported, and reported separately"


def test_export_import_roundtrip(db):
    channels.set_preference(db, "UC0", listing="allow", priority=3)
    channels.set_preference(db, "UC1", listing="block")
    exported = channels.export_lists(db)
    fresh = Database(db.path + ".copy")
    channels.import_lists(fresh, [c["id"] for c in exported["allow"]],
                          [c["id"] for c in exported["block"]], exported["priorities"])
    policy = channels.load_policy(fresh, settings())
    assert policy.allow == {"UC0"}
    assert policy.block == {"UC1"}
    assert policy.priority["UC0"] == 3


# -- ranking ---------------------------------------------------------------


def test_explanations_sum_to_exactly_one_hundred(db):
    """The stacked bar is the explanation, so it must fill exactly once — no
    gap, no overflow — for every video on the page."""
    result = ranking.recommend(db, settings())
    assert result.items
    for item in result.items:
        positive = [p["percent"] for p in ranking.explain(item) if not p.get("negative")]
        if positive:
            assert sum(positive) == 100, f"{item.id} apportioned to {sum(positive)}"


def test_apportionment_never_loses_or_invents_a_point():
    from sieve.ranking import _apportion

    cases = [
        [("a", 33.333), ("b", 33.333), ("c", 33.333)],
        [("a", 16.666), ("b", 16.666), ("c", 16.666), ("d", 16.666), ("e", 16.666), ("f", 16.67)],
        [("a", 99.9), ("b", 0.1)],
        [("a", 100.0)],
    ]
    for shares in cases:
        out = _apportion(shares)
        assert sum(value for _, value in out) == 100, shares
        for (key, original), (key2, rounded) in zip(shares, out, strict=True):
            assert key == key2
            assert abs(original - rounded) < 1.0, "no component may move by a whole point"


def test_every_video_can_explain_itself(db):
    result = ranking.recommend(db, settings())
    assert result.items
    for item in result.items:
        assert ranking.explain(item), f"{item.id} appeared with no reason given"


def test_rejections_are_counted_and_named(db):
    result = ranking.recommend(db, settings(filters={"min_education": 99}))
    assert result.diagnostics["rejected_total"] > 0
    for row in result.diagnostics["rejected"]:
        assert row["reason"] and row["count"] > 0


def test_per_channel_cap_is_respected(db):
    result = ranking.recommend(db, settings(diversity={"enabled": True, "max_per_channel": 2}))
    counts: dict[str, int] = {}
    for item in result.items:
        counts[item.channel_id] = counts.get(item.channel_id, 0) + 1
    assert all(count <= 2 for count in counts.values())


def test_hard_filters_are_absolute(db):
    result = ranking.recommend(db, settings(filters={"max_duration": 600}))
    assert all(item.video["duration"] <= 600 for item in result.items)


def test_rules_gate_the_feed(db):
    expr = {"all": [{"field": "views", "op": ">", "value": 7000}]}
    result = ranking.recommend(db, settings(rules={"enabled": True, "expr": expr}))
    assert all(item.video["views"] > 7000 for item in result.items)


def test_homepage_count_is_honoured(db):
    result = ranking.recommend(db, settings(homepage={"count": 5}))
    assert len(result.items) <= 5


def test_daily_ordering_is_stable(db):
    first = ranking.recommend(db, settings())
    second = ranking.recommend(db, settings())
    assert [i.id for i in first.items] == [i.id for i in second.items]
