"""The AI tuning scores, targets and weights by itself — and undoing it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sieve import actions, autotune, corrections, llm
from sieve.app import create_app
from tests.support import offline_config
from tests.test_spec import add


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(offline_config(tmp_path), start_worker=False))


def fake_ai(monkeypatch, rating):
    monkeypatch.setattr(llm, "available", lambda cfg: True)

    def answer(cfg, system, user):
        if system == autotune.RATE_PROMPT:
            return {"ratings": {str(n): rating for n in range(len(user.splitlines()))}}
        return {"targets": {"education": {"enabled": True, "target": 85, "weight": 1.5}},
                "weights": {"novelty": 0.4}, "filters": {"min_duration": 9999}}
    monkeypatch.setattr(llm, "complete_json", answer)
    monkeypatch.setattr(llm, "compile_brief", lambda cfg, text, context=None: answer(cfg, "brief", text))


def test_off_by_default_and_manual_stays_manual(client):
    db = client.app.state.db
    assert not autotune.settings(db)["enabled"] and not autotune.due(db)


def test_it_corrects_scores_it_disagrees_with_but_never_yours(client, monkeypatch):
    db = client.app.state.db
    for i in range(4):
        add(db, f"q000000000{i}", f"Video {i}", channel=f"UC{i}")
    corrections.set_override(db, "q0000000000", "education", 10)            # yours
    client.post("/api/feedback", json={"video_id": "q0000000002", "kind": "more"})   # something to learn from
    fake_ai(monkeypatch, {"education": 99, "clickbait": 1})
    log = autotune.run(client.app.state.ingestor.cfg, db, force=True)
    assert log["videos_checked"] == 4 and log["videos_adjusted"] > 0 and not log["error"]
    sources = {(r["video_id"], r["axis"]): (r["source"], r["value"]) for r in db.query("SELECT * FROM score_overrides")}
    assert sources[("q0000000000", "education")] == ("user", 10.0), "your correction wins"
    assert sources[("q0000000001", "education")] == ("ai", 99.0)
    assert log["settings_changed"] == ["targets", "weights"], "only targets and weights, never filters"
    stored = db.get_setting("settings")
    assert stored["targets"]["education"]["target"] == 85 and "min_duration" not in stored.get("filters", {})


def test_the_computation_slider_bounds_the_work(client, monkeypatch):
    db = client.app.state.db
    for i in range(30):
        add(db, f"r{i:010d}", f"Video {i}", channel=f"UC{i}")
    actions.save_settings(db, {"ai_tune": {"enabled": True, "videos_per_hour": 5}})
    fake_ai(monkeypatch, {"education": 50})
    assert autotune.run(client.app.state.ingestor.cfg, db)["videos_checked"] == 5
    assert not autotune.due(db), "at most once an hour"


def test_undo_removes_ai_scores_and_restores_settings(client, monkeypatch):
    db = client.app.state.db
    add(db, "s0000000001", "A video")
    corrections.set_override(db, "s0000000001", "clickbait", 5)
    client.post("/api/feedback", json={"video_id": "s0000000001", "kind": "more"})
    before = dict(db.get_setting("settings", {}) or {})
    fake_ai(monkeypatch, {"education": 99})
    autotune.run(client.app.state.ingestor.cfg, db, force=True)
    result = client.post("/api/ai/tune/undo").json()
    assert result["overrides_removed"] > 0 and result["settings_restored"]
    assert db.get_setting("settings", {}) == before
    assert db.scalar("SELECT source FROM score_overrides WHERE video_id = 's0000000001'") == "user"


def test_without_an_ai_it_says_so(client):
    assert client.post("/api/ai/tune").status_code == 502
    assert "no AI connection" in client.app.state.db.get_setting("ai_tune_log")["error"]


def test_settings_are_left_alone_without_feedback_to_learn_from(client, monkeypatch):
    db = client.app.state.db
    add(db, "t0000000001", "A video")
    fake_ai(monkeypatch, {"education": 99})
    assert autotune.run(client.app.state.ingestor.cfg, db, force=True)["settings_changed"] == []
