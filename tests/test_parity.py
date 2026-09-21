"""Parity between the web app and the API.

Everything Sieve can do must be reachable both from a browser and from the JSON
API. This file holds that as an executable rule rather than a line in the docs:

* every `/api/*` route must name the web control that does the same thing,
  and that control must actually be on the page;
* every web route must name its API equivalent;
* every API route must answer without a server error.

Adding an endpoint without a web counterpart, or a page without an endpoint,
fails `test_every_route_is_declared`. That is deliberate. If a route genuinely
belongs on one surface only, it goes in ONE_SIDED with the reason written down.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from sieve.app import create_app
from sieve.cli import seed_demo
from sieve.config import Config
from sieve.db import Database

VIDEO = "demo0003"

# (method, api path) -> (web page, marker proving the control is on that page)
API_TO_WEB: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/api/recommendations"): ("/", "data-video="),
    ("GET", "/api/videos/{video_id}"): (f"/video/{VIDEO}", "Content scores"),
    ("GET", "/api/sponsorblock/{video_id}"): (f"/video/{VIDEO}", "SponsorBlock"),
    ("GET", "/api/status"): ("/", " scored"),
    ("POST", "/api/feedback"): ("/", "data-feedback="),
    ("POST", "/api/hide"): ("/", "data-hide="),
    ("POST", "/api/progress"): ("/", "data-watch="),

    ("GET", "/api/settings"): ("/settings", 'action="/settings"'),
    ("POST", "/api/settings"): ("/settings", 'action="/settings"'),
    ("POST", "/api/settings/reset"): ("/settings", 'action="/settings/reset"'),
    ("GET", "/api/moods"): ("/settings", 'id="moods"'),
    ("POST", "/api/moods/active"): ("/", 'action="/settings/mood"'),
    ("PUT", "/api/moods/{name}"): ("/settings", 'action="/settings/mood/save"'),
    ("DELETE", "/api/moods/{name}"): ("/settings", 'action="/settings/mood/delete"'),

    ("GET", "/api/channels"): ("/channels", "data-channel="),
    ("GET", "/api/channels/export"): ("/channels", 'href="/api/channels/export"'),
    ("POST", "/api/channels/import"): ("/channels", 'id="channels-file"'),
    ("POST", "/api/channels/{channel_id}"): ("/channels", "data-priority="),
    ("DELETE", "/api/channels/{channel_id}"): ("/channels", "data-channel-clear"),
    ("POST", "/api/channels/recompute"): ("/channels", 'data-action="/api/channels/recompute"'),
    ("POST", "/api/channels/quality"): ("/channels", 'data-action="/api/channels/quality"'),

    ("GET", "/api/interests"): ("/debugger", "data-tag="),
    ("POST", "/api/interests"): ("/debugger", 'id="add-interest"'),
    ("POST", "/api/interests/rederive"): ("/debugger", 'data-action="/api/interests/rederive"'),
    ("GET", "/api/model"): ("/debugger", "The learned model"),
    ("POST", "/api/model/retrain"): ("/debugger", 'data-action="/api/model/retrain"'),
    ("POST", "/api/model/reset"): ("/debugger", 'data-action="/api/model/reset"'),
    ("GET", "/api/blocklist"): ("/debugger", "Blocked title terms"),
    ("POST", "/api/blocklist"): ("/debugger", 'id="add-term"'),
    ("DELETE", "/api/blocklist/{kind}/{value}"): ("/debugger", "data-unblock"),
    ("POST", "/api/critique"): ("/debugger", 'id="critique"'),
    ("GET", "/api/doctor"): ("/debugger", 'id="doctor"'),

    ("POST", "/api/brief/compile"): ("/brief", 'action="/brief"'),
    ("POST", "/api/brief/apply"): ("/brief", 'action="/brief"'),
    ("GET", "/api/rules"): ("/rules", 'id="rule-json"'),
    ("POST", "/api/rules"): ("/rules", 'id="rule-save"'),
    ("POST", "/api/rules/validate"): ("/rules", 'id="rule-status"'),
    ("GET", "/api/analytics"): ("/analytics", "Completion by length"),

    ("POST", "/api/import/playlist"): ("/settings", 'id="playlist-import"'),
    ("GET", "/api/playlists"): ("/settings", 'id="playlists"'),
    ("DELETE", "/api/playlists/{playlist_id}"): ("/settings", 'data-playlist-action="remove"'),
    ("POST", "/api/import/subscriptions"): ("/settings", 'data-upload="/api/import/subscriptions"'),
    ("POST", "/api/import/history"): ("/settings", 'data-upload="/api/import/history"'),

    ("GET", "/api/profiles"): ("/settings", 'id="profiles"'),
    ("PUT", "/api/profiles/{name}"): ("/settings", 'action="/profile/save"'),
    ("POST", "/api/profiles/{name}/load"): ("/settings", 'action="/profile/load"'),
    ("DELETE", "/api/profiles/{name}"): ("/settings", "data-profile-delete"),
    ("GET", "/api/profile/export"): ("/settings", 'href="/api/profile/export"'),
    ("POST", "/api/profile/import"): ("/settings", 'id="profile-file"'),
    ("POST", "/api/profile/fetch"): ("/settings", 'id="profile-fetch"'),

    ("POST", "/api/sync"): ("/settings", 'data-action="/api/sync"'),
    ("POST", "/api/score"): ("/settings", 'data-action="/api/score"'),
    ("POST", "/api/maintenance/prune"): ("/settings", 'data-action="/api/maintenance/prune"'),
    ("POST", "/api/vision/scan"): ("/settings", 'data-action="/api/vision/scan"'),
}

# (method, web path) -> (method, api path) that does the same thing
WEB_TO_API: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/"): ("GET", "/api/recommendations"),
    ("GET", "/video/{video_id}"): ("GET", "/api/videos/{video_id}"),
    ("GET", "/settings"): ("GET", "/api/settings"),
    ("POST", "/settings"): ("POST", "/api/settings"),
    ("POST", "/settings/reset"): ("POST", "/api/settings/reset"),
    ("POST", "/settings/mood"): ("POST", "/api/moods/active"),
    ("POST", "/settings/mood/save"): ("PUT", "/api/moods/{name}"),
    ("POST", "/settings/mood/delete"): ("DELETE", "/api/moods/{name}"),
    ("GET", "/channels"): ("GET", "/api/channels"),
    ("GET", "/debugger"): ("GET", "/api/interests"),
    ("GET", "/rules"): ("GET", "/api/rules"),
    ("GET", "/brief"): ("POST", "/api/brief/compile"),
    ("POST", "/brief"): ("POST", "/api/brief/apply"),
    ("GET", "/analytics"): ("GET", "/api/analytics"),
    ("POST", "/profile/save"): ("PUT", "/api/profiles/{name}"),
    ("POST", "/profile/load"): ("POST", "/api/profiles/{name}/load"),
}

# Routes that are legitimately on one surface only, with the reason.
ONE_SIDED: dict[tuple[str, str], str] = {
    ("GET", "/demo/thumb/{video_id}.svg"):
        "an image asset for the demo catalogue, not a feature",
}

# How to call each API route in a way that must not produce a server error.
# Order matters: each target is created before the call that deletes it.
# Anything that would need the network is called so that it fails fast and
# cleanly (the configured instance is a closed local port).
CALLS: dict[tuple[str, str], tuple[str, dict, set[int]]] = {
    ("GET", "/api/recommendations"): ("/api/recommendations?limit=5", {}, {200}),
    ("GET", "/api/videos/{video_id}"): (f"/api/videos/{VIDEO}", {}, {200}),
    ("GET", "/api/sponsorblock/{video_id}"): (f"/api/sponsorblock/{VIDEO}", {}, {200}),
    ("GET", "/api/status"): ("/api/status", {}, {200}),
    ("POST", "/api/feedback"): ("/api/feedback", {"json": {"video_id": VIDEO, "kind": "more"}}, {200}),
    ("POST", "/api/hide"): ("/api/hide", {"json": {"kind": "video", "value": "demo0009"}}, {200}),
    ("POST", "/api/progress"): ("/api/progress", {"json": {"video_id": VIDEO, "progress": 0.5}}, {200}),

    ("GET", "/api/settings"): ("/api/settings", {}, {200}),
    ("POST", "/api/settings"): ("/api/settings", {"json": {"novelty": 40}}, {200}),
    ("POST", "/api/settings/reset"): ("/api/settings/reset", {}, {200}),
    ("GET", "/api/moods"): ("/api/moods", {}, {200}),
    ("POST", "/api/moods/active"): ("/api/moods/active", {"json": {"name": "Relax"}}, {200}),
    ("PUT", "/api/moods/{name}"): ("/api/moods/Parity", {}, {200}),
    ("DELETE", "/api/moods/{name}"): ("/api/moods/Parity", {}, {200}),

    ("GET", "/api/channels"): ("/api/channels", {}, {200}),
    ("GET", "/api/channels/export"): ("/api/channels/export", {}, {200}),
    ("POST", "/api/channels/import"): ("/api/channels/import", {"json": {"allow": ["UC_lect"]}}, {200}),
    ("POST", "/api/channels/{channel_id}"): ("/api/channels/UC_kernel", {"json": {"priority": 3}}, {200}),
    ("DELETE", "/api/channels/{channel_id}"): ("/api/channels/UC_kernel", {}, {200}),
    ("POST", "/api/channels/recompute"): ("/api/channels/recompute", {}, {200}),
    ("POST", "/api/channels/quality"): ("/api/channels/quality", {}, {200}),

    ("GET", "/api/interests"): ("/api/interests", {}, {200}),
    ("POST", "/api/interests"): ("/api/interests", {"json": {"tag": "allocator", "weight": 0.9}}, {200}),
    ("POST", "/api/interests/rederive"): ("/api/interests/rederive", {}, {200}),
    ("GET", "/api/model"): ("/api/model", {}, {200}),
    ("POST", "/api/model/retrain"): ("/api/model/retrain", {}, {200}),
    ("POST", "/api/model/reset"): ("/api/model/reset", {}, {200}),
    ("GET", "/api/blocklist"): ("/api/blocklist", {}, {200}),
    ("POST", "/api/blocklist"): ("/api/blocklist", {"json": {"kind": "term", "value": "parity"}}, {200}),
    ("DELETE", "/api/blocklist/{kind}/{value}"): ("/api/blocklist/term/parity", {}, {200}),
    ("POST", "/api/critique"): ("/api/critique", {}, {200}),
    ("GET", "/api/doctor"): ("/api/doctor", {}, {200}),

    ("POST", "/api/brief/compile"): ("/api/brief/compile", {"json": {"text": "technical lectures"}}, {200}),
    ("POST", "/api/brief/apply"): ("/api/brief/apply", {"json": {"text": "technical lectures, no shorts"}}, {200}),
    ("GET", "/api/rules"): ("/api/rules", {}, {200}),
    ("POST", "/api/rules"): ("/api/rules", {"json": {"enabled": False, "expr": {"all": []}}}, {200}),
    ("POST", "/api/rules/validate"): ("/api/rules/validate", {"json": {"expr": {"all": []}}}, {200}),
    ("GET", "/api/analytics"): ("/api/analytics", {}, {200}),

    ("POST", "/api/import/playlist"): ("/api/import/playlist", {"json": {"playlist": "PLparityparityparity"}}, {502}),
    ("GET", "/api/playlists"): ("/api/playlists", {}, {200}),
    ("DELETE", "/api/playlists/{playlist_id}"): ("/api/playlists/PLseededseededseeded", {}, {200}),
    ("POST", "/api/import/subscriptions"): ("/api/import/subscriptions", {"files": "subs"}, {200}),
    ("POST", "/api/import/history"): ("/api/import/history", {"files": "history"}, {200}),

    ("GET", "/api/profiles"): ("/api/profiles", {}, {200}),
    ("PUT", "/api/profiles/{name}"): ("/api/profiles/parity", {"json": {"author": "test"}}, {200}),
    ("POST", "/api/profiles/{name}/load"): ("/api/profiles/parity/load", {}, {200}),
    ("DELETE", "/api/profiles/{name}"): ("/api/profiles/parity", {}, {200}),
    ("GET", "/api/profile/export"): ("/api/profile/export", {}, {200}),
    ("POST", "/api/profile/import"): ("/api/profile/import", {"json": {"profile": {"settings": {"novelty": 55}}}}, {200}),
    ("POST", "/api/profile/fetch"): ("/api/profile/fetch", {"json": {"url": "not a url"}}, {400}),

    ("POST", "/api/sync"): ("/api/sync", {}, {200}),
    ("POST", "/api/score"): ("/api/score", {"json": {"limit": 5, "transcripts": False}}, {200}),
    ("POST", "/api/maintenance/prune"): ("/api/maintenance/prune", {}, {200}),
    ("POST", "/api/vision/scan"): ("/api/vision/scan", {}, {200}),
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data = tmp_path_factory.mktemp("parity")
    cfg = Config(data_dir=str(data), instances=["http://127.0.0.1:9"], request_timeout=1)
    db = Database(cfg.db_path)
    seed_demo(db, 120)
    # Seed the things some controls only render when they exist.
    now = int(time.time())
    db.execute("INSERT INTO playlists(id, title, video_ids, updated_at) VALUES(?,?,?,?)",
               ("PLseededseededseeded", "Seeded", json.dumps([VIDEO]), now))
    db.execute("INSERT INTO blocklist(kind, value, note, created_at) VALUES('term','seeded','',?)", (now,))
    db.execute("INSERT INTO sponsor_segments(video_id, segments, sponsor_ratio, fetched_at) "
               "VALUES(?,?,?,?)",
               (VIDEO, json.dumps([{"category": "sponsor", "start": 1, "end": 30, "action": "skip",
                                    "votes": 3, "locked": 0, "uuid": "x"}]), 0.05, now))
    db.execute("INSERT INTO saved_profiles(name, body, author, created_at) VALUES(?,?,?,?)",
               ("seeded", json.dumps({"settings": {}}), "", now))
    db.execute("INSERT INTO channel_prefs(channel_id, name, priority, listing, updated_at) "
               "VALUES('UC_forge','Backyard Forge',2,'neutral',?)", (now,))
    db.set_setting("settings", {"sponsorblock": {"enabled": True}})
    app = create_app(cfg, start_worker=False)
    return TestClient(app)


def _routes(app) -> set[tuple[str, str]]:
    """Every operation the app serves, read from its OpenAPI schema.

    Not from `app.routes`: recent FastAPI keeps an included router as a single
    lazy entry there rather than flattening it, so walking `app.routes` misses
    every endpoint in `api.py` — and a parity check that cannot see half the
    routes passes without checking anything. The schema is the public contract
    and resolves nested routers properly.
    """
    found = set()
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            if method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                found.add((method.upper(), path))
    return found


def test_the_inventory_sees_both_surfaces(client):
    """Guard the guard: if route discovery ever goes blind again, fail loudly
    rather than let the parity checks pass vacuously."""
    routes = _routes(client.app)
    assert ("GET", "/api/recommendations") in routes, "router endpoints not visible"
    assert ("GET", "/settings") in routes, "page routes not visible"
    assert len(routes) >= len(API_TO_WEB) + len(WEB_TO_API)


def test_every_route_is_declared(client):
    """A route missing from every table is a feature on one surface only."""
    declared = set(API_TO_WEB) | set(WEB_TO_API) | set(ONE_SIDED)
    undeclared = sorted(_routes(client.app) - declared)
    assert not undeclared, (
        "these routes have no declared counterpart on the other surface: "
        f"{undeclared}. Add the control or endpoint, then add it to "
        "API_TO_WEB or WEB_TO_API — or to ONE_SIDED with a reason.")


def test_tables_do_not_name_routes_that_do_not_exist(client):
    routes = _routes(client.app)
    stale = sorted(k for k in (*API_TO_WEB, *WEB_TO_API, *ONE_SIDED) if k not in routes)
    assert not stale, f"declared but not routed: {stale}"
    targets = sorted(v for v in WEB_TO_API.values() if v not in routes)
    assert not targets, f"web routes point at API routes that do not exist: {targets}"


def test_every_api_route_has_a_call(client):
    missing = sorted(set(API_TO_WEB) - set(CALLS))
    assert not missing, f"no test call defined for {missing}"


@pytest.mark.parametrize("route", sorted(API_TO_WEB), ids=lambda r: f"{r[0]} {r[1]}")
def test_web_control_is_on_the_page(client, route):
    page, marker = API_TO_WEB[route]
    response = client.get(page)
    assert response.status_code == 200, f"{page} returned {response.status_code}"
    assert marker in response.text, (
        f"{route[0]} {route[1]} is declared as reachable from {page}, "
        f"but {marker!r} is not on that page")


@pytest.mark.parametrize("route", list(CALLS), ids=lambda r: f"{r[0]} {r[1]}")
def test_api_route_answers(client, route):
    """Calls run in the order CALLS declares them — creation before deletion —
    against one shared database, as a user's session would."""
    method, _ = route
    path, kwargs, expected = CALLS[route]
    if kwargs.get("files") == "subs":
        body = json.dumps({"subscriptions": [{"url": "https://youtube.com/channel/UC_new", "name": "New"}]})
        kwargs = {"files": {"file": ("subs.json", io.BytesIO(body.encode()), "application/json")}}
    elif kwargs.get("files") == "history":
        body = json.dumps({"watch_history": [VIDEO]})
        kwargs = {"files": {"file": ("history.json", io.BytesIO(body.encode()), "application/json")}}
    response = client.request(method, path, **kwargs)
    assert response.status_code in expected, (
        f"{method} {path} returned {response.status_code}: {response.text[:300]}")
    assert response.status_code < 500 or response.status_code == 502


def test_the_openapi_schema_documents_the_new_endpoints(client):
    schema = client.get("/openapi.json").json()
    for path in ("/api/recommendations", "/api/playlists", "/api/blocklist", "/api/doctor"):
        assert path in schema["paths"], f"{path} missing from /api/docs"
