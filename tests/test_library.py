"""Local search, chapters, notes and export, downloads, alerts, digest,
adaptive sync, automatic captions and the userscript."""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sieve import downloads, library, notify, scoring
from sieve.app import create_app
from sieve.db import Database
from tests.support import offline_config

NOW = int(time.time())


def add(db, vid, title, description="", channel="UCa", published=None, duration=900):
    db.upsert_videos([{"id": vid, "title": title, "description": description, "author": "Chan " + channel[-2:],
                       "author_id": channel, "published": published or NOW - 3600, "duration": duration,
                       "views": 5000}])
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(dict(db.get_video(vid))))])


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "l.db"))


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(offline_config(tmp_path), start_worker=False))


# ------------------------------------------------------------------ search


def test_search_finds_related_wording_not_only_exact_words(db):
    add(db, "a0000000001", "Heap exploitation: tcache poisoning explained", "heap tcache malloc exploit glibc")
    add(db, "a0000000002", "Use after free in glibc malloc", "tcache malloc glibc free chunk exploit")
    add(db, "a0000000003", "Baking sourdough bread", "flour water yeast bread oven")
    ids = [r["id"] for r in library.search(db, "heap exploitation")]
    assert ids[0] == "a0000000001"
    assert "a0000000002" in ids, "found through what the best match shares, without the words"
    assert "a0000000003" not in ids


def test_search_filters(db):
    add(db, "b0000000001", "Rust ownership", "rust borrow checker", duration=300)
    add(db, "b0000000002", "Rust lifetimes", "rust borrow checker lifetimes", duration=3000)
    db.record_watch("b0000000002", 1.0)
    assert [r["id"] for r in library.search(db, "rust", unwatched=True)] == ["b0000000001"]
    assert [r["id"] for r in library.search(db, "rust", min_duration=1000)] == ["b0000000002"]
    assert library.search(db, "") == []


# ---------------------------------------------------------------- chapters


def test_chapters_follow_youtubes_rule():
    text = "Intro text\n0:00 Intro\n1:30 - The setup\n12:05 Results\n1:02:03 Q&A"
    assert library.chapters(text) == [{"at": 0, "title": "Intro"}, {"at": 90, "title": "The setup"},
                                      {"at": 725, "title": "Results"}, {"at": 3723, "title": "Q&A"}]
    assert library.chapters("At 3:15 he says something\n5:00 later") == [], "must start at 0:00"
    assert library.chapters("0:00 only one") == []


# ------------------------------------------------------------ notes, export


def test_notes_export_to_obsidian_logseq_and_readwise(db):
    add(db, "c0000000001", "Measure theory lecture 3")
    library.add_note(db, "c0000000001", "Sigma algebras defined here", 125)
    library.add_note(db, "c0000000001", "Whole lecture is good")
    with pytest.raises(ValueError):
        library.add_note(db, "c0000000001", "   ")
    content, _, _ = library.export(db, "obsidian")
    page = zipfile.ZipFile(io.BytesIO(content)).read("Measure theory lecture 3 (c0000000001).md").decode()
    assert page.startswith("---\ntitle:") and "[2:05](https://www.youtube.com/watch?v=c0000000001&t=125)" in page
    logseq = zipfile.ZipFile(io.BytesIO(library.export(db, "logseq")[0]))
    assert "url:: https://www.youtube.com/watch?v=c0000000001" in logseq.read(logseq.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(library.export(db, "readwise")[0].decode())))
    assert rows[0] == ["Highlight", "Title", "Author", "URL", "Note", "Location", "Date"] and len(rows) == 3
    with pytest.raises(ValueError):
        library.export(db, "notion")


def test_notes_through_the_api(client):
    add(client.app.state.db, "dQw4w9WgXcQ", "A video")
    assert client.post("/api/videos/dQw4w9WgXcQ/notes", json={"text": "hi", "at_second": 5}).json()["ok"]
    notes = client.get("/api/videos/dQw4w9WgXcQ/notes").json()["notes"]
    assert notes[0]["text"] == "hi" and notes[0]["at_second"] == 5
    assert client.delete(f"/api/notes/{notes[0]['id']}").json()["ok"]
    html = client.get("/video/dQw4w9WgXcQ").text
    assert 'data-note-form="dQw4w9WgXcQ"' in html


# --------------------------------------------------------------- downloads


class FakeYDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, url, download=True):
        vid = url.split("v=")[1]
        path = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
        for hook in self.options["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        path.write_bytes(b"x" * 100)
        return {"id": vid, "ext": "mp4"}

    def prepare_filename(self, info):
        return self.options["outtmpl"].replace("%(ext)s", info["ext"])


def test_a_download_runs_needs_no_ffmpeg_and_plays_offline(tmp_path, db):
    add(db, "d0000000001", "Something to watch on a plane")
    worker = downloads.Downloader(db, str(tmp_path), ydl_factory=FakeYDL)
    downloads.queue(db, "d0000000001", "480")
    assert worker.run_once() and not worker.run_once()
    row = downloads.listing(db)[0]
    assert row["status"] == "done" and row["bytes"] == 100 and downloads.local_file(db, "d0000000001")
    assert "height<=480" in downloads.format_for("480") and "acodec!=none" in downloads.format_for("480")
    assert downloads.remove(db, "d0000000001") and downloads.local_file(db, "d0000000001") is None


def test_without_ytdlp_a_download_fails_with_advice(tmp_path, db, monkeypatch):
    import builtins

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError())
                        if name == "yt_dlp" else real_import(name, *a, **k))
    downloads.queue(db, "d0000000002")
    downloads.Downloader(db, str(tmp_path)).run_once()
    assert "needs yt-dlp" in downloads.listing(db)[0]["error"]


def test_the_player_uses_the_offline_copy(client, tmp_path):
    db = client.app.state.db
    add(db, "dQw4w9WgXcQ", "A video", "0:00 Start\n2:00 Middle")
    client.app.state.downloader._factory = FakeYDL
    client.post("/api/downloads", json={"video_id": "dQw4w9WgXcQ"})
    client.app.state.downloader.run_once()
    html = client.get("/play/dQw4w9WgXcQ").text
    assert 'src="/media/dQw4w9WgXcQ' in html and 'data-seek="120"' in html
    assert client.get("/media/dQw4w9WgXcQ").content == b"x" * 100


# ------------------------------------------------------------------ alerts


def test_alerts_are_for_new_uploads_not_the_back_catalogue(db):
    add(db, "e0000000001", "Old one", channel="UCalerts000000000000000")
    notify.set_alert(db, "UCalerts000000000000000", True)
    assert notify.collect(db) == 0 and notify.unseen(db) == []
    add(db, "e0000000002", "Brand new", channel="UCalerts000000000000000")
    assert notify.collect(db) == 1 and [a["video_id"] for a in notify.unseen(db)] == ["e0000000002"]
    assert notify.mark_seen(db) == 1 and notify.unseen(db) == []


def test_webhook_ntfy_and_json(db):
    from sieve import actions

    sent = []
    fake = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    assert not notify.send(db, "t", "x", client=fake), "no address, nothing sent"
    actions.save_settings(db, {"notify": {"webhook_url": "https://ntfy.sh/secret"}})
    add(db, "e0000000003", "Fresh", channel="UCb")
    notify.set_alert(db, "UCb", True)
    add(db, "e0000000004", "Fresher", channel="UCb")
    notify.collect(db)
    assert notify.deliver_pending(db, client=fake) == 1
    assert sent[-1].headers["Title"] == "1 new upload" and b"Fresher" in sent[-1].content
    assert notify.deliver_pending(db, client=fake) == 0, "sent once"
    actions.save_settings(db, {"notify": {"webhook_style": "json"}})
    notify.send(db, "t", "body", client=fake)
    assert json.loads(sent[-1].content)["text"] == "body"


def test_digest_and_rss(client):
    db = client.app.state.db
    add(db, "f0000000001", "A pick")
    body = client.post("/api/digest").json()
    assert body["text"].startswith("1 new video") and "Top picks" in body["text"]
    assert "<title>Sieve: digest</title>" in client.get("/feeds/digest.xml").text
    assert client.get("/feeds/alerts.xml").headers["content-type"].startswith("application/rss+xml")
    assert client.get("/feeds/nope.xml").status_code == 404


def test_digest_schedule(db):
    from sieve import actions

    assert not notify.digest_due(db)
    actions.save_settings(db, {"notify": {"digest_hours": 24}})
    assert notify.digest_due(db)
    notify.build_digest(db)
    assert not notify.digest_due(db)


# ------------------------------------------------------------ adaptive sync


def test_busy_channels_are_checked_more_often_than_quiet_ones(db, tmp_path):
    from sieve.ingest import Ingestor

    for i in range(15):   # a daily uploader, as its feed shows it: the latest 15
        add(db, f"g{i:010d}", f"Daily {i}", channel="UCbusy", published=NOW - i * 86400)
    add(db, "h0000000001", "Monthly", channel="UCquiet", published=NOW - 30 * 86400)
    # A daily uploader is due every 8 hours; one with a single known video
    # (assumed monthly) every week. Both were last checked 10 hours ago.
    ten_hours_ago = NOW - 10 * 3600
    db.executemany("INSERT INTO channel_fetches(channel_id, fetched_at, origin) VALUES(?,?, 'subscription')",
                   [("UCbusy", ten_hours_ago), ("UCquiet", ten_hours_ago)])
    ingestor = Ingestor(offline_config(tmp_path), db, api=None, community=None)
    due = ingestor._due([("UCbusy", "b"), ("UCquiet", "q"), ("UCnew", "n")])
    assert due == [("UCbusy", "b"), ("UCnew", "n")] and ingestor._quiet == 1
    ingestor._deep = True
    assert len(ingestor._due([("UCbusy", "b"), ("UCquiet", "q")])) == 2, "a deep sync checks everything"


# -------------------------------------------------------- automatic captions


def test_automatic_captions_without_ytdlp(tmp_path):
    from sieve.config import Config
    from sieve.youtube import YouTube

    player = {"captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
        {"baseUrl": "https://yt/api/timedtext?v=x&lang=de", "languageCode": "de"},
        {"baseUrl": "https://yt/api/timedtext?v=x&lang=en&kind=asr", "languageCode": "en", "kind": "asr"}]}}}
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/watch":
            return httpx.Response(200, text=f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>")
        return httpx.Response(200, text="WEBVTT\n\n00:00.000 --> 00:02.000\nhello automatic world\n")

    cfg = Config(data_dir=tmp_path, youtube_url="https://yt", use_ytdlp=False)
    yt = YouTube(cfg, Database(cfg.db_path))
    yt._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert "hello automatic world" in yt.captions("x")
    assert "lang=en" in seen[1] and "fmt=vtt" in seen[1], "English automatic before German manual"


def test_a_video_without_captions_is_only_tried_once(client):
    db = client.app.state.db
    add(db, "i0000000001", "No captions anywhere")
    db.execute("UPDATE scores SET version = 0")
    calls = []
    client.app.state.ingestor.api.captions = lambda vid, lang="en": calls.append(vid) or ""
    client.app.state.ingestor.score_pending(fetch_transcripts=True)
    db.execute("UPDATE scores SET version = 0")
    client.app.state.ingestor.score_pending(fetch_transcripts=True)
    assert calls == ["i0000000001"]


# --------------------------------------------------------------- userscript


def test_the_userscript_is_filled_in_for_this_server(client):
    script = client.get("/userscript/sieve.user.js").text
    assert "const SIEVE = 'http://testserver';" in script and "@connect      testserver" in script
    assert "__" not in script.split("==/UserScript==")[1], "every placeholder replaced"
    assert "GM_xmlhttpRequest" in script and "/api/progress" in script


def test_library_page_renders(client):
    add(client.app.state.db, "j0000000001", "Kernel scheduling deep dive", "kernel scheduler linux cfs")
    html = client.get("/library?q=kernel").text
    assert "Kernel scheduling deep dive" in html and 'id="downloads"' in html and 'id="export"' in html


def test_the_search_form_accepts_empty_number_boxes(client):
    """An empty "min seconds" box submits min_duration=, which used to be a
    validation error page instead of results."""
    add(client.app.state.db, "k0000000001", "Kernel scheduling", "kernel scheduler")
    response = client.get("/library?q=kernel&min_duration=&max_duration=")
    assert response.status_code == 200 and "Kernel scheduling" in response.text


@pytest.mark.parametrize("header, status, body", [
    ("bytes=10-19", 206, b"x" * 10), ("bytes=90-", 206, b"x" * 10), ("bytes=-5", 206, b"x" * 5),
    ("bytes=500-", 416, b""), ("", 200, b"x" * 100),
])
def test_offline_videos_can_seek(client, header, status, body):
    """A <video> seeks with Range requests; the Android build's Starlette
    ignored them, so seeking and chapters broke on the phone."""
    add(client.app.state.db, "dQw4w9WgXcQ", "A video")
    client.app.state.downloader._factory = FakeYDL
    client.post("/api/downloads", json={"video_id": "dQw4w9WgXcQ"})
    client.app.state.downloader.run_once()
    response = client.get("/media/dQw4w9WgXcQ", headers={"Range": header} if header else {})
    assert response.status_code == status and response.content == body
    if status == 206:
        assert response.headers["content-range"].startswith("bytes ") and response.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize("path", ["/api/feedback", "/api/hide", "/api/settings", "/api/rules",
                                  "/api/progress", "/api/interests", "/api/profile/import"])
@pytest.mark.parametrize("body", [None, "not json", "[1, 2]"])
def test_a_malformed_body_is_a_400_not_a_crash(client, path, body):
    """These read the body directly, so an empty, non-JSON or list body was a 500."""
    response = client.post(path, content=body) if body is not None else client.post(path)
    assert response.status_code == 400 and "JSON object" in response.json()["detail"]
