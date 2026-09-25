"""Regression tests for bugs found while bringing the web app and API to parity.

Each test names the bug it pins down. The parity suite proves every route
answers; these prove the routes do the right thing.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from sieve import actions, channels, ranking, scoring
from sieve.app import create_app
from sieve.config import Config, config_path, default_data_dir, resolve_settings
from sieve.db import Database
from tests.support import offline_config


def make_video(vid, **overrides):
    base = {
        "id": vid, "title": "A calm video about kernels", "author": "Chan", "author_id": "UC1",
        "published": int(time.time()) - 86400, "duration": 1200, "views": 9000, "likes": 400,
        "description": "0:00 intro", "keywords": ["kernel"], "genre": "Science & Technology",
        "is_live": 0, "is_upcoming": 0, "family_safe": 1, "sub_count": 50000,
        "caption_langs": ["en"],
    }
    base.update(overrides)
    return base


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def seed(db, videos):
    db.upsert_videos(videos)
    db.executemany(scoring.INSERT_SCORE,
                   [scoring.card_to_row(scoring.score_video(v)) for v in videos])


def settings(**patch):
    base = {"filters": {"hide_shorts": False}}
    for key, value in patch.items():
        base.setdefault(key, {}).update(value) if isinstance(value, dict) else base.update({key: value})
    return resolve_settings(base)


# -- exempt channels -------------------------------------------------------


def test_exempt_skips_quality_filters(db):
    seed(db, [make_video("long01", duration=9000, author_id="UCfav")])
    channels.set_preference(db, "UCfav", listing="allow", exempt_filters=True)
    result = ranking.recommend(db, settings(filters={"max_duration": 600}))
    assert "long01" in {i.id for i in result.items}, "exempt should skip the duration filter"


def test_exempt_does_not_resurface_watched_videos(db):
    """Bug: an exempt channel skipped the whole gate, so its watched videos
    came back on every homepage."""
    seed(db, [make_video("seen01", author_id="UCfav")])
    channels.set_preference(db, "UCfav", listing="allow", exempt_filters=True)
    db.execute("INSERT INTO history(video_id, watched_at, progress, dwell, origin) "
               "VALUES('seen01', ?, 1.0, 0, 't')", (int(time.time()),))
    result = ranking.recommend(db, settings())
    assert "seen01" not in {i.id for i in result.items}


def test_exempt_does_not_bypass_blocked_terms(db):
    """Bug: a word you blocked still appeared if the channel was exempt."""
    seed(db, [make_video("react1", title="My reaction to the new release", author_id="UCfav")])
    channels.set_preference(db, "UCfav", listing="allow", exempt_filters=True)
    actions.add_block(db, "term", "Reaction")
    result = ranking.recommend(db, settings())
    assert "react1" not in {i.id for i in result.items}


# -- blocklist -------------------------------------------------------------


def test_a_blocked_term_actually_filters(db):
    """Bug: the gate read blocked terms, but nothing in the product could set one."""
    seed(db, [make_video("keep01"), make_video("drop01", title="Tier list of every compiler")])
    actions.add_block(db, "term", "TIER LIST")
    result = ranking.recommend(db, settings())
    ids = {i.id for i in result.items}
    assert "keep01" in ids and "drop01" not in ids
    reasons = " ".join(r["reason"] for r in result.diagnostics["rejected"])
    assert "tier list" in reasons


def test_terms_are_stored_as_they_are_compared(db):
    actions.add_block(db, "term", "  SHOCKING  ")
    assert [b["value"] for b in actions.list_blocklist(db, "term")] == ["shocking"]
    assert actions.remove_block(db, "term", "Shocking")


def test_channels_cannot_be_smuggled_into_the_blocklist(db):
    """Bug: /api/hide accepted any kind, including 'channel', which nothing
    reads. The block silently did nothing."""
    with pytest.raises(actions.ActionError, match="Channels page"):
        actions.add_block(db, "channel", "UC_x")


# -- moods -----------------------------------------------------------------


def test_deleting_a_built_in_mood_sticks(db):
    """Bug: built-in moods live in the defaults, so removing them from storage
    let the next merge bring them straight back."""
    assert "Study" in actions.list_moods(db)["moods"]
    assert actions.delete_mood(db, "Study")
    assert "Study" not in actions.list_moods(db)["moods"]
    assert not actions.delete_mood(db, "Study"), "a second delete finds nothing"


def test_deleting_the_active_mood_clears_it(db):
    actions.activate_mood(db, "Relax")
    actions.delete_mood(db, "Relax")
    assert actions.list_moods(db)["active"] == ""


def test_a_deleted_mood_can_be_saved_again(db):
    actions.delete_mood(db, "Study")
    actions.save_mood(db, "Study")
    assert "Study" in actions.list_moods(db)["moods"]


def test_mood_names_are_validated(db):
    with pytest.raises(actions.ActionError):
        actions.save_mood(db, "../../etc")
    with pytest.raises(actions.ActionError):
        actions.activate_mood(db, "Nonexistent")


# -- playlists -------------------------------------------------------------


@pytest.mark.parametrize("ref,expected", [
    ("PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf", "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"),
    ("https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
     "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=3",
     "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"),
    ("https://yewtu.be/playlist?list=IVxAbCdEfGhIjKlMn", "IVxAbCdEfGhIjKlMn"),
    ("youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
     "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"),
])
def test_playlist_refs_accept_whatever_people_paste(ref, expected):
    assert actions.parse_playlist_ref(ref) == expected


@pytest.mark.parametrize("ref,message", [
    ("", "paste"),
    ("https://youtube.com/watch?v=abc", "no playlist"),
    ("https://youtube.com/playlist?list=WL", "Watch Later"),
    ("not a playlist", "does not look like"),
])
def test_bad_playlist_refs_say_why(ref, message):
    with pytest.raises(actions.ActionError, match=message):
        actions.parse_playlist_ref(ref)


def test_removing_the_homepage_playlist_unpins_it(db):
    """A homepage pinned to a deleted playlist would render empty."""
    db.execute("INSERT INTO playlists(id, title, video_ids, updated_at) VALUES('PLaaaaaaaaaa','x','[]',0)")
    actions.save_settings(db, {"homepage": {"mode": "playlist", "playlist_id": "PLaaaaaaaaaa"}})
    assert actions.list_playlists(db)[0]["homepage"]
    assert actions.remove_playlist(db, "PLaaaaaaaaaa")
    assert actions.resolved_settings(db)["homepage"]["playlist_id"] == ""
    assert not actions.remove_playlist(db, "PLaaaaaaaaaa")


# -- configuration ---------------------------------------------------------


def test_the_documented_config_location_is_read(tmp_path, monkeypatch):
    """Bug: the install guide said ~/.config/sieve/config.toml, and the loader
    never looked there. Edits were silently ignored."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIEVE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    target = tmp_path / "conf" / "sieve" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text("port = 9123\n")
    cfg = Config.load()
    assert cfg.port == 9123
    assert cfg.source == str(target.resolve())


def test_search_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.delenv("SIEVE_CONFIG", raising=False)
    xdg = tmp_path / "conf" / "sieve" / "config.toml"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("port = 1\n")
    (tmp_path / "sieve.toml").write_text("port = 2\n")
    env = tmp_path / "env.toml"
    env.write_text("port = 3\n")
    assert config_path() == (tmp_path / "sieve.toml").resolve(), "./sieve.toml beats the XDG file"
    monkeypatch.setenv("SIEVE_CONFIG", str(env))
    assert config_path() == env.resolve(), "$SIEVE_CONFIG beats ./sieve.toml"
    assert config_path(xdg) == xdg.resolve(), "--config beats everything"


def test_an_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.toml")


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "sieve.toml").write_text("port = 9000\n")
    monkeypatch.setenv("SIEVE_PORT", "9001")
    assert Config.load().port == 9001


def test_tilde_is_expanded(tmp_path, monkeypatch):
    """Bug: `data_dir = "~/..."` created a directory literally named "~"."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert Config(data_dir="~/sieve-data").data_dir == tmp_path / "home" / "sieve-data"


def test_the_default_data_dir_does_not_depend_on_where_you_run_it(tmp_path, monkeypatch):
    """Bug: the default was ./data, so two working directories meant two databases."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    first = default_data_dir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert default_data_dir() == first == tmp_path / "xdg" / "sieve"


def test_the_default_port_is_the_documented_one():
    assert Config().port == 8377


def test_the_database_creates_its_own_directory(tmp_path):
    """Bug: only Config.load() made the directory, so building a Config by hand
    and calling create_app failed with 'unable to open database file'."""
    path = tmp_path / "a" / "b" / "c" / "sieve.db"
    Database(str(path)).close()
    assert path.exists()


def test_create_app_works_from_a_hand_built_config(tmp_path):
    app = create_app(offline_config(tmp_path / "fresh" / "dir"), start_worker=False)
    assert TestClient(app).get("/api/status").status_code == 200


# -- API behaviour ---------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    cfg = offline_config(tmp_path)
    return TestClient(create_app(cfg, start_worker=False))


def test_rederive_really_rederives(client):
    """Bug: the debugger's Rederive button posted an empty body, which the
    endpoint read as 'set an interest with no name' — a silent no-op."""
    response = client.post("/api/interests/rederive")
    assert response.status_code == 200
    assert "derived" in response.json()


def test_an_empty_interest_is_refused_not_ignored(client):
    response = client.post("/api/interests", json={})
    assert response.status_code == 400
    assert "rederive" in response.json()["detail"], "the error should point at the right endpoint"


def test_errors_carry_a_reason(client):
    """The web UI now shows `detail` verbatim, so it must be a sentence."""
    response = client.post("/api/import/playlist", json={"playlist": "https://youtube.com/playlist?list=WL"})
    assert response.status_code == 400
    assert "Watch Later" in response.json()["detail"]


def test_an_unreachable_instance_is_a_502_not_a_crash(client):
    response = client.post("/api/import/playlist", json={"playlist": "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"})
    assert response.status_code == 502


def test_recommendations_match_what_the_page_renders(client):
    """The API and the homepage must agree on titles and order."""
    from sieve.cli import seed_demo

    seed_demo(client.app.state.db, 80)
    items = client.get("/api/recommendations").json()["items"]
    html = client.get("/").text
    assert items, "the demo catalogue should produce recommendations"
    for item in items[:5]:
        assert f'data-video="{item["id"]}"' in html
    positions = [html.index(f'data-video="{item["id"]}"') for item in items[:5]]
    assert positions == sorted(positions), "the API and the page disagree on order"


def test_doctor_reports_the_config_in_effect(client):
    report = client.get("/api/doctor").json()
    assert "config" in report
    assert report["config"]["port"] == 8377


def test_channel_export_round_trips_through_import(client):
    """Regression: export writes {"id", "name"} objects, import only took bare
    strings, so the documented `export > file; import < file` recipe returned a
    422 as soon as one channel was allowed or blocked. The web page hid this by
    converting in JavaScript."""
    client.post("/api/channels/UC_one", json={"listing": "allow", "priority": 3})
    client.post("/api/channels/UC_two", json={"listing": "block"})
    exported = client.get("/api/channels/export").json()
    assert exported["allow"] and isinstance(exported["allow"][0], dict)

    client.delete("/api/channels/UC_one")
    client.delete("/api/channels/UC_two")
    response = client.post("/api/channels/import", json=exported)
    assert response.status_code == 200, response.text
    assert client.get("/api/channels/export").json() == exported


def test_channel_import_still_takes_bare_ids(client):
    response = client.post("/api/channels/import", json={"allow": ["UC_three"], "block": []})
    assert response.status_code == 200
