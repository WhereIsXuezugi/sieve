"""The channel prior, your score overrides, and the model they train."""

from __future__ import annotations

import time

import pytest

from sieve import corrections, scoring
from sieve.db import Database

NOW = int(time.time())


def video(vid, title, channel="UCsame", description=""):
    return {"id": vid, "title": title, "description": description, "keywords": "[]",
            "duration": 900, "views": 1000, "likes": 10, "published": NOW, "genre": "",
            "author": "A", "author_id": channel, "family_safe": 1}


def test_a_sparse_video_leans_on_its_channel():
    """A title that says little is scored mostly by bias alone; its channel's
    usual score is better evidence."""
    bare = video("v1", "Part 3")
    alone = scoring.score_video(bare)
    lifted = scoring.score_video(bare, extra={"channel_means": {"education": 85.0}})
    lowered = scoring.score_video(bare, extra={"channel_means": {"education": 15.0}})
    assert lowered["education"] < alone["education"] < lifted["education"]
    assert lifted.base["education"] == alone["education"], "the base never includes the prior"
    assert any(s.name == "channel_prior" for s in lifted.signals["education"])


def test_your_override_wins_outright():
    card = scoring.score_video(video("v1", "Lecture 1: measure theory"),
                               extra={"overrides": {"education": 12.0}})
    assert card["education"] == 12.0
    assert card.signals["education"][-1].name == "your_override"


def test_corrections_learn_the_gap_and_reach_similar_videos(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    rows = [video(f"v{i:010d}", f"Speedrun commentary episode {i}", "UCgame") for i in range(6)]
    other = video("x0000000001", "Measure theory lecture", "UClect")
    db.upsert_videos([*rows, other])
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(dict(db.get_video(v["id"]))))
                                          for v in [*rows, other]])
    before = scoring.score_video(dict(db.get_video("v0000000005")))["education"]
    # You say these commentaries are more educational than the engine thinks.
    for v in rows[:4]:
        corrections.set_override(db, v["id"], "education", 80)
    model = corrections.train(db)
    after = scoring.score_video(dict(db.get_video("v0000000005")),
                                extra={"correction_model": model})["education"]
    unrelated = scoring.score_video(dict(db.get_video("x0000000001")), extra={"correction_model": model})
    unrelated_before = scoring.score_video(dict(db.get_video("x0000000001")))
    assert after > before + 5, "an uncorrected video like the corrected ones moves toward them"
    assert abs(unrelated["education"] - unrelated_before["education"]) < abs(after - before), \
        "an unlike video moves less"


def test_no_corrections_means_no_change():
    assert corrections.fit([]) == {}
    card = scoring.score_video(video("v1", "anything"), extra={"correction_model": {}})
    assert all(s.name != "your_corrections" for sigs in card.signals.values() for s in sigs)


def test_overrides_are_validated(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    with pytest.raises(ValueError):
        corrections.set_override(db, "v", "nonsense", 50)
    corrections.set_override(db, "v", "education", 400)
    assert corrections.overrides_for(db, ["v"]) == {"v": {"education": 100.0}}
    corrections.set_override(db, "v", "education", None)
    assert corrections.overrides_for(db, ["v"]) == {}


# -- the progress endpoint and the reference scales -----------------------------------


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from sieve.app import create_app
    from tests.support import offline_config

    return TestClient(create_app(offline_config(tmp_path), start_worker=False))


def test_progress_reports_each_step(client):
    ingestor = client.app.state.ingestor
    assert client.get("/api/fetch/progress").json()["running"] is False
    seen = []

    def spy(label):
        seen.append(dict(ingestor.progress))
    original = ingestor._tick
    ingestor._tick = lambda label: (original(label), spy(label))
    client.post("/api/fetch")
    assert seen and seen[0]["kind"] == "fetch" and seen[0]["total"] >= 1
    assert seen[0]["label"].startswith("Checking ")
    done = client.get("/api/fetch/progress").json()
    assert done["running"] is False and done["done"] == done["total"]


def test_a_scale_explains_a_score_with_your_own_videos(client):
    from sieve import demo

    demo.seed_demo(client.app.state.db, 60)
    body = client.get("/api/scales/clickbait?value=30").json()
    assert body["what"] and len(body["bands"]) == 4
    assert [b["current"] for b in body["bands"]] == [False, True, False, False]
    assert body["above"] + body["below"] == 100 and body["catalogue"] == 60
    assert all(abs(e["score"] - 30) <= 6 for e in body["examples"])


def test_a_unit_scale_describes_the_spread(client):
    from sieve import demo

    demo.seed_demo(client.app.state.db, 60)
    body = client.get("/api/scales/duration").json()
    assert body["unit"] == "seconds" and body["percentiles"]["10"] <= body["percentiles"]["90"]
    assert client.get("/api/scales/nonsense").status_code == 404


def test_every_score_slider_has_a_help_button(client):
    html = client.get("/settings").text
    from sieve.scales import SCALES
    for key in ("brainrot", "clickbait", "nsfw", "music", "ai_generated", "profanity",
                "education", "info_density"):
        assert f'data-help="{key}"' in html and key in SCALES
    for key in ("duration", "views", "subs"):
        assert f'data-help="{key}"' in html
