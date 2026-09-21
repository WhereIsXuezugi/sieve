"""Tests for the two places untrusted data enters the system: imported
profiles, and responses from the community APIs."""

import hashlib
import json

import pytest

from sieve import community, profiles
from sieve.config import Config, resolve_settings
from sieve.db import Database


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


# -- profile sanitising ----------------------------------------------------


def test_unknown_keys_are_dropped():
    clean = profiles.sanitise_settings({
        "filters": {"max_brainrot": 20, "__class__": "evil", "rm": "-rf"},
        "totally_new_section": {"x": 1},
    })
    assert clean == {"filters": {"max_brainrot": 20}}


def test_out_of_range_values_are_clamped():
    clean = profiles.sanitise_settings({
        "sources": {"subscriptions": 9999, "history": -50},
        "novelty": 400,
        "targets": {"education": {"enabled": True, "target": 900, "weight": 99}},
    })
    assert clean["sources"] == {"subscriptions": 100, "history": 0}
    assert clean["novelty"] == 100
    assert clean["targets"]["education"]["target"] == 100
    assert clean["targets"]["education"]["weight"] == 3.0


def test_invalid_rules_are_discarded_not_raised():
    clean = profiles.sanitise_settings({"rules": {"enabled": True, "expr": {
        "field": "os.system", "op": "exec", "value": "rm -rf /",
    }}})
    assert "rules" not in clean


def test_valid_rules_survive():
    expr = {"all": [{"field": "education", "op": ">", "value": 60}]}
    clean = profiles.sanitise_settings({"rules": {"enabled": True, "expr": expr}})
    assert clean["rules"]["expr"] == expr


def test_sponsorblock_categories_are_whitelisted():
    clean = profiles.sanitise_settings({"sponsorblock": {
        "enabled": True, "skip": ["sponsor", "rm -rf", "filler"],
    }})
    assert clean["sponsorblock"]["skip"] == ["sponsor", "filler"]


def test_roundtrip_keeps_the_settings_that_matter(db):
    db.set_setting("settings", {
        "filters": {"max_brainrot": 15},
        "sources": {"subscriptions": 60, "niche": 20},
        "channels": {"whitelist_only": True},
        "dearrow": {"enabled": True},
    })
    body = profiles.export_profile(db, "test")
    fresh = Database(db.path + ".2")
    profiles.import_profile(fresh, body)
    restored = resolve_settings(fresh.get_setting("settings", {}))
    assert restored["filters"]["max_brainrot"] == 15
    assert restored["sources"]["subscriptions"] == 60
    assert restored["channels"]["whitelist_only"] is True
    assert restored["dearrow"]["enabled"] is True


def test_a_newer_format_is_refused(db):
    with pytest.raises(ValueError):
        profiles.import_profile(db, {"format": 99, "settings": {}})


# -- community data --------------------------------------------------------


def test_hash_prefix_matches_the_published_scheme():
    """Both APIs key on the first four hex characters of sha256(videoID)."""
    for video_id in ("dQw4w9WgXcQ", "demo0001", "aaaaaaaaaaa"):
        expected = hashlib.sha256(video_id.encode()).hexdigest()[:4]
        assert community.hash_prefix(video_id) == expected
        assert len(community.hash_prefix(video_id)) == 4


def test_segment_ratios_are_computed_from_coverage():
    row = community._segment_row("abc", [
        {"category": "sponsor", "segment": [0, 60], "videoDuration": 600, "votes": 4},
        {"category": "sponsor", "segment": [300, 330], "videoDuration": 600, "votes": 2},
        {"category": "filler", "segment": [400, 460], "videoDuration": 600, "votes": 1},
        {"category": "exclusive_access", "segment": [0, 0], "videoDuration": 600},
    ])
    video_id, payload, sponsor, filler, selfpromo, exclusive, _ = row
    assert video_id == "abc"
    assert sponsor == pytest.approx(90 / 600)
    assert filler == pytest.approx(60 / 600)
    assert selfpromo == 0
    assert exclusive == 1
    assert len(json.loads(payload)) == 4


def test_malformed_segments_do_not_explode():
    row = community._segment_row("abc", [
        {"category": "sponsor", "segment": ["x", None], "videoDuration": 600},
        {"category": "sponsor", "segment": [10, 20], "videoDuration": 600},
    ])
    # ratios are stored rounded to four places
    assert row[2] == pytest.approx(10 / 600, abs=1e-4)


def test_branding_prefers_locked_then_most_voted():
    row = community._branding_row("abc", {
        "titles": [
            {"title": "original one", "original": True, "votes": 99},
            {"title": "what it is actually about", "original": False, "votes": 3},
            {"title": "a worse suggestion", "original": False, "votes": 1},
        ],
        "thumbnails": [{"timestamp": 42.5, "original": False, "votes": 2}],
    }, min_votes=0)
    assert row[1] == "what it is actually about"
    assert row[4] == 42.5


def test_branding_respects_a_vote_floor():
    assert community._branding_row(
        "abc", {"titles": [{"title": "x", "original": False, "votes": 1}], "thumbnails": []},
        min_votes=5,
    ) is None


def test_original_titles_are_never_treated_as_corrections():
    assert community._branding_row(
        "abc", {"titles": [{"title": "as uploaded", "original": True, "votes": 50}],
                "thumbnails": []}, min_votes=0,
    ) is None


def test_prefix_log_prevents_refetching(db, tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    client = community.CommunityData(cfg, db)
    try:
        ids = ["demo0001", "demo0002"]
        assert client._pending_prefixes("dearrow", ids), "nothing cached yet"
        for prefix in client._pending_prefixes("dearrow", ids):
            client._mark_prefix("dearrow", prefix)
        assert client._pending_prefixes("dearrow", ids) == []
    finally:
        client.close()


def test_one_prefix_request_can_cover_several_videos(db, tmp_path):
    """The privacy-preserving endpoint is also the cheap one."""
    cfg = Config(data_dir=str(tmp_path))
    client = community.CommunityData(cfg, db)
    try:
        ids = [f"demo{i:04d}" for i in range(400)]
        prefixes = client._pending_prefixes("sponsorblock", ids)
        assert len(prefixes) < len(ids)
    finally:
        client.close()
