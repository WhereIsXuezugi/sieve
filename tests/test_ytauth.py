"""YouTube's "Sign in to confirm you're not a bot", and signing yt-dlp in."""

from __future__ import annotations

import time
import typing

import httpx
import pytest
from fastapi.testclient import TestClient

from sieve import downloads, ytauth
from sieve.app import create_app
from sieve.config import Config
from sieve.db import Database
from sieve.youtube import YouTube
from tests.support import offline_config

# The exact message from the report, curly apostrophe included.
BOT = ("ERROR: [youtube] anzafBqwUbo: Sign in to confirm you\u2019re not a bot. Use --cookies-from-browser "
       "or --cookies for the authentication.")
COOKIES = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"


class BlockedYDL:
    seen: typing.ClassVar[list] = []

    def __init__(self, options):
        BlockedYDL.seen.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, url, download=False):
        raise Exception(BOT)


@pytest.fixture()
def yt(tmp_path):
    BlockedYDL.seen = []
    cfg = Config(data_dir=tmp_path, youtube_url="https://yt", use_ytdlp=True)
    client = YouTube(cfg, Database(cfg.db_path), ytdlp_factory=BlockedYDL)
    client._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"title": "From oEmbed", "author_name": "Chan",
                                            "author_url": "https://www.youtube.com/channel/UCx"})))
    return client


def test_the_bot_check_is_recognised_and_only_the_bot_check():
    assert ytauth.is_bot_check(BOT)
    assert ytauth.is_bot_check("Sign in to confirm you're not a bot")
    assert not ytauth.is_bot_check("Sign in to confirm your age"), "one video, not the address"
    assert not ytauth.is_bot_check("HTTP Error 404: Not Found")


def test_a_bot_check_pauses_ytdlp_and_falls_back(yt):
    video = yt.video("anzafBqwUbo")
    assert video["title"] == "From oEmbed", "oEmbed still answers"
    state = ytauth.status(yt.db)
    assert state["blocked"] and not state["signed_in"] and state["retry_at"] > time.time()
    calls = len(BlockedYDL.seen)
    yt.video("anzafBqwUbo")
    assert len(BlockedYDL.seen) == calls, "paused: yt-dlp is not asked again straight away"


def test_cookies_sign_ytdlp_in(yt):
    ytauth.save_cookies(yt.cfg.data_dir, COOKIES)
    ytauth.clear(yt.db)
    yt.video("anzafBqwUbo")
    assert BlockedYDL.seen[-1]["cookiefile"].endswith("youtube-cookies.txt")
    assert ytauth.status(yt.db)["signed_in"], "the advice changes: the cookies may have expired"
    assert oct(ytauth.cookies_file(yt.cfg.data_dir).stat().st_mode)[-3:] == "600"


def test_a_browser_can_be_used_instead(yt):
    from sieve import actions

    actions.save_settings(yt.db, {"source": {"cookies_browser": "firefox"}})
    assert ytauth.ytdlp_options(yt.db, yt.cfg.data_dir) == {"cookiesfrombrowser": ("firefox",)}
    ytauth.save_cookies(yt.cfg.data_dir, COOKIES)
    assert "cookiefile" in ytauth.ytdlp_options(yt.db, yt.cfg.data_dir), "a file wins"


@pytest.mark.parametrize("content, error", [
    (b"just some text", "not a cookies.txt"),
    (b".google.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n", "no youtube.com cookies"),
])
def test_bad_cookie_files_are_refused(tmp_path, content, error):
    with pytest.raises(ValueError, match=error):
        ytauth.save_cookies(tmp_path, content)


def test_a_blocked_download_waits_with_advice_instead_of_failing(tmp_path):
    db = Database(str(tmp_path / "d.db"))
    downloads.queue(db, "anzafBqwUbo")
    worker = downloads.Downloader(db, str(tmp_path), ydl_factory=BlockedYDL)
    assert worker.run_once() is False
    row = downloads.listing(db)[0]
    assert row["status"] == "queued" and "Controls, Source" in row["error"]
    assert ytauth.status(db)["blocked"]


def test_uploading_cookies_through_the_page(tmp_path):
    client = TestClient(create_app(offline_config(tmp_path), start_worker=False))
    db = client.app.state.db
    ytauth.record(db, BOT, signed_in=False)
    assert "prove it is not a bot" in client.get("/settings").text
    response = client.post("/api/source/cookies", files={"file": ("cookies.txt", COOKIES)})
    assert response.json()["ok"] and not ytauth.status(db)["blocked"], "signed in: try again now"
    assert "Cookies saved" in client.get("/settings").text
    assert client.delete("/api/source/cookies").json()["removed"]
    backups_dir = tmp_path / "backups"
    assert not any(p.name.endswith("cookies.txt") for p in backups_dir.glob("*")) if backups_dir.exists() else True
