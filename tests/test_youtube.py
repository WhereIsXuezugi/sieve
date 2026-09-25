"""YouTube compatibility.

Nothing here touches the network: YouTube's feed is served from a fixture
through httpx's MockTransport, and yt-dlp is replaced with a fake returning the
dict shapes yt-dlp documents. The fixture follows YouTube's published Atom
format, including a Short and a channel id missing its "UC" prefix.
"""

from __future__ import annotations

import json
import time
from typing import ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient

from sieve import profiles, ranking, scoring
from sieve.app import create_app
from sieve.config import resolve_settings
from sieve.db import Database
from sieve.ingest import Ingestor, import_history
from sieve.invidious import UpstreamUnavailable
from sieve.upstream import Upstream
from sieve.youtube import YouTube, from_ytdlp, parse_feed, pick_caption
from tests.support import offline_config

CHANNEL = "UCsXVk37bltHxD1rDPwtNM8Q"

FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <link rel="self" href="http://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"/>
 <id>yt:channel:{CHANNEL[2:]}</id>
 <yt:channelId>{CHANNEL[2:]}</yt:channelId>
 <title>Kurzgesagt – In a Nutshell</title>
 <author><name>Kurzgesagt – In a Nutshell</name><uri>https://www.youtube.com/channel/{CHANNEL}</uri></author>
 <published>2013-07-09T15:46:00+00:00</published>
 <entry>
  <id>yt:video:aaaaaaaaaaa</id>
  <yt:videoId>aaaaaaaaaaa</yt:videoId>
  <yt:channelId>{CHANNEL[2:]}</yt:channelId>
  <title>The Largest Star in the Universe</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=aaaaaaaaaaa"/>
  <author><name>Kurzgesagt – In a Nutshell</name><uri>https://www.youtube.com/channel/{CHANNEL}</uri></author>
  <published>2024-05-01T12:00:00+00:00</published>
  <updated>2024-05-02T08:00:00+00:00</updated>
  <media:group>
   <media:title>The Largest Star in the Universe</media:title>
   <media:content url="https://www.youtube.com/v/aaaaaaaaaaa?version=3" type="application/x-shockwave-flash" width="640" height="390"/>
   <media:thumbnail url="https://i1.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg" width="480" height="360"/>
   <media:description>How big can stars get? Sources: https://arxiv.org/abs/1234.5678</media:description>
   <media:community>
    <media:starRating count="41230" average="5.00" min="1" max="5"/>
    <media:statistics views="3120456"/>
   </media:community>
  </media:group>
 </entry>
 <entry>
  <id>yt:video:bbbbbbbbbbb</id>
  <yt:videoId>bbbbbbbbbbb</yt:videoId>
  <yt:channelId>{CHANNEL[2:]}</yt:channelId>
  <title>Why stars explode #shorts</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/bbbbbbbbbbb"/>
  <author><name>Kurzgesagt – In a Nutshell</name><uri>https://www.youtube.com/channel/{CHANNEL}</uri></author>
  <published>2024-05-03T12:00:00+00:00</published>
  <updated>2024-05-03T12:00:00+00:00</updated>
  <media:group>
   <media:title>Why stars explode #shorts</media:title>
   <media:description></media:description>
   <media:community>
    <media:starRating count="900" average="5.00" min="1" max="5"/>
    <media:statistics views="88000"/>
   </media:community>
  </media:group>
 </entry>
</feed>"""


def feed_transport(status: int = 200, body: str = FEED, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path.endswith("/oembed"):
            return httpx.Response(200, json={"title": "An oEmbed title", "author_name": "Someone"})
        return httpx.Response(status, text=body)
    return httpx.MockTransport(handler)


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL with documented return shapes."""

    responses: ClassVar[dict] = {}
    calls: ClassVar[list] = []

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        FakeYDL.calls.append((url, self.options.get("extract_flat")))
        for fragment, response in FakeYDL.responses.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise RuntimeError(f"DownloadError: nothing for {url}")

    def sanitize_info(self, info):
        return info


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeYDL.responses = {}
    FakeYDL.calls = []


def youtube(tmp_path, transport=None, with_ytdlp=False) -> YouTube:
    db = Database(str(tmp_path / "yt.db"))
    yt = YouTube(offline_config(tmp_path), db, ytdlp_factory=FakeYDL if with_ytdlp else None)
    yt._client = httpx.Client(transport=transport or feed_transport())
    return yt


# -- parsing ------------------------------------------------------------------


def test_feed_parses_into_invidious_shaped_videos():
    feed = parse_feed(FEED)
    assert feed["title"] == "Kurzgesagt – In a Nutshell"
    first, short = feed["videos"]
    assert first["videoId"] == "aaaaaaaaaaa"
    assert first["title"] == "The Largest Star in the Universe"
    assert first["viewCount"] == 3120456 and first["likeCount"] == 41230
    assert first["published"] == 1714564800
    assert "arxiv" in first["description"]
    assert first["isShort"] is False and short["isShort"] is True


def test_a_channel_id_missing_its_prefix_is_repaired():
    assert parse_feed(FEED)["videos"][0]["authorId"] == CHANNEL


def test_flat_ytdlp_entries_map_across():
    entry = {"_type": "url", "ie_key": "Youtube", "id": "ccccccccccc",
             "url": "https://www.youtube.com/watch?v=ccccccccccc", "title": "A lecture",
             "duration": 3605.0, "view_count": 1200, "channel_id": CHANNEL, "channel": "Chan"}
    video = from_ytdlp(entry)
    assert video["lengthSeconds"] == 3605 and video["viewCount"] == 1200
    assert video["authorId"] == CHANNEL and video["isShort"] is False


def test_full_ytdlp_info_maps_across():
    info = {"id": "ccccccccccc", "title": "T", "duration": 61, "upload_date": "20240501",
            "tags": ["physics"], "categories": ["Education"], "like_count": 5,
            "channel_follower_count": 2_000_000, "live_status": "not_live", "age_limit": 0,
            "automatic_captions": {"en": [{"ext": "vtt", "url": "u"}]}, "channel_id": CHANNEL,
            "webpage_url": "https://www.youtube.com/shorts/ccccccccccc"}
    video = from_ytdlp(info)
    assert video["published"] > 0 and video["keywords"] == ["physics"]
    assert video["genre"] == "Education" and video["subCount"] == 2_000_000
    assert video["captions"] == [{"languageCode": "en"}] and video["isShort"] is True


def test_non_video_entries_are_skipped():
    assert from_ytdlp({"id": CHANNEL, "_type": "url", "ie_key": "YoutubeTab"}) is None
    assert from_ytdlp(None) is None


def test_captions_prefer_human_then_exact_language_then_vtt():
    info = {
        "subtitles": {"en-GB": [{"ext": "srv1", "url": "human-gb-srv"}, {"ext": "vtt", "url": "human-gb"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "auto-en"}]},
    }
    assert pick_caption(info, "en") == "human-gb"
    assert pick_caption({"automatic_captions": {"en": [{"ext": "json3", "url": "j"}]}}, "en") == "j"
    assert pick_caption({}, "en") == ""


# -- the client -----------------------------------------------------------------


def test_channel_videos_from_the_feed_alone(tmp_path):
    videos = youtube(tmp_path).channel_videos(CHANNEL)
    assert [v["videoId"] for v in videos] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert videos[0]["lengthSeconds"] == 0, "feeds carry no duration; it must not be invented"
    assert videos[1]["isShort"]


def test_ytdlp_adds_durations_and_older_uploads(tmp_path):
    FakeYDL.responses = {f"/channel/{CHANNEL}/videos": {"entries": [
        {"id": "aaaaaaaaaaa", "title": "The Largest Star in the Universe", "duration": 812,
         "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"},
        {"id": "ddddddddddd", "title": "An older upload", "duration": 1400,
         "url": "https://www.youtube.com/watch?v=ddddddddddd"},
    ]}}
    videos = {v["videoId"]: v for v in youtube(tmp_path, with_ytdlp=True).channel_videos(CHANNEL)}
    assert videos["aaaaaaaaaaa"]["lengthSeconds"] == 812
    assert videos["aaaaaaaaaaa"]["published"] == 1714564800, "the feed's date survives the merge"
    assert videos["ddddddddddd"]["lengthSeconds"] == 1400
    assert FakeYDL.calls[0][1] == "in_playlist", "channel listings must use flat extraction"


def test_ytdlp_failure_falls_back_to_the_feed(tmp_path):
    FakeYDL.responses = {"/videos": RuntimeError("Sign in to confirm you're not a bot")}
    videos = youtube(tmp_path, with_ytdlp=True).channel_videos(CHANNEL)
    assert len(videos) == 2


def test_a_missing_feed_is_an_upstream_failure(tmp_path):
    with pytest.raises(UpstreamUnavailable):
        youtube(tmp_path, transport=feed_transport(status=404)).channel_videos(CHANNEL)


def test_feeds_are_cached(tmp_path):
    calls: list = []
    yt = youtube(tmp_path, transport=feed_transport(calls=calls))
    yt.channel_videos(CHANNEL)
    yt.channel_videos(CHANNEL)
    assert len(calls) == 1


def test_playlists_use_ytdlp_when_available(tmp_path):
    FakeYDL.responses = {"playlist?list=PLxxxxxxxxxxxx": {"title": "Queue", "entries": [
        {"id": "eeeeeeeeeee", "title": "One", "duration": 300,
         "url": "https://www.youtube.com/watch?v=eeeeeeeeeee"}]}}
    playlist = youtube(tmp_path, with_ytdlp=True).playlist("PLxxxxxxxxxxxx")
    assert playlist["title"] == "Queue" and playlist["videos"][0]["lengthSeconds"] == 300
    assert "truncated" not in playlist


def test_playlists_fall_back_to_the_feed_and_say_so(tmp_path):
    playlist = youtube(tmp_path).playlist("PLxxxxxxxxxxxx")
    assert playlist["truncated"] is True and len(playlist["videos"]) == 2


def test_single_video_without_ytdlp_uses_oembed(tmp_path):
    assert youtube(tmp_path).video("aaaaaaaaaaa")["title"] == "An oEmbed title"


def test_search_works_without_ytdlp_and_prefers_it_when_installed(tmp_path):
    from tests.test_ytsearch import results_page

    page = results_page()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=page)
                                    if request.url.path == "/results" else httpx.Response(404))
    found = youtube(tmp_path, transport=transport).search("compilers")
    assert [v["videoId"] for v in found] == ["aaaaaaaaaa1", "bbbbbbbbbb2"]
    FakeYDL.responses = {"ytsearchdate20:compilers": {"entries": [
        {"id": "fffffffffff", "title": "Compilers", "url": "https://www.youtube.com/watch?v=fffffffffff"}]}}
    found = youtube(tmp_path, with_ytdlp=True).search("compilers", sort_by="upload_date")
    assert found[0]["videoId"] == "fffffffffff"


# -- choosing a backend ---------------------------------------------------------


class DeadInvidious:
    name = "invidious"

    def __init__(self):
        self.calls = 0

    def channel_videos(self, *args, **kwargs):
        self.calls += 1
        raise UpstreamUnavailable("connection refused")

    def thumbnail_url(self, video_id):
        return f"http://inv.example/vi/{video_id}/mqdefault.jpg"

    def reachable(self, timeout=2.0):
        return False

    def close(self):
        pass


def upstream(tmp_path, backend="auto", invidious=None):
    db = Database(str(tmp_path / "up.db"))
    db.set_setting("settings", {"source": {"backend": backend}})
    yt = YouTube(offline_config(tmp_path), db)
    yt._client = httpx.Client(transport=feed_transport())
    return Upstream(offline_config(tmp_path), db, youtube=yt, invidious=invidious or DeadInvidious())


def test_auto_falls_back_to_youtube(tmp_path):
    up = upstream(tmp_path)
    assert len(up.channel_videos(CHANNEL)) == 2
    assert up.last_backend == "youtube"


def test_auto_stops_waiting_on_a_dead_instance(tmp_path):
    """The breaker: a sync of 200 channels must not pay Invidious' timeout
    200 times. The health check now catches a dead instance before even one
    request is made, so none is."""
    dead = DeadInvidious()
    up = upstream(tmp_path, invidious=dead)
    for _ in range(5):
        up.channel_videos(CHANNEL)
    assert dead.calls <= 1


def test_invidious_only_never_touches_youtube(tmp_path):
    up = upstream(tmp_path, backend="invidious")
    with pytest.raises(UpstreamUnavailable):
        up.channel_videos(CHANNEL)


def test_youtube_only_never_touches_invidious(tmp_path):
    dead = DeadInvidious()
    up = upstream(tmp_path, backend="youtube", invidious=dead)
    up.channel_videos(CHANNEL)
    assert dead.calls == 0


@pytest.mark.parametrize("backend,host", [
    ("youtube", "i.ytimg.com"), ("invidious", "inv.example"), ("auto", "i.ytimg.com"),
])
def test_thumbnails_come_from_the_live_backend(tmp_path, backend, host):
    assert host in upstream(tmp_path, backend=backend).thumbnail_url("aaaaaaaaaaa")


def test_an_unknown_backend_setting_is_refused():
    assert "source" not in profiles.sanitise_settings({"source": {"backend": "vimeo"}})
    assert profiles.sanitise_settings({"source": {"backend": "youtube"}}) == {"source": {"backend": "youtube"}}


# -- through the whole app ---------------------------------------------------------


@pytest.fixture()
def app_client(tmp_path):
    app = create_app(offline_config(tmp_path), start_worker=False)
    app.state.api.youtube._client = httpx.Client(transport=feed_transport())
    return TestClient(app, follow_redirects=False)


def test_a_sync_with_no_invidious_fills_the_catalogue_from_youtube(app_client):
    db = app_client.app.state.db
    db.execute("INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?, 'K', 1.0, 0)",
               (CHANNEL,))
    result = app_client.post("/api/sync").json()
    assert result["from"]["subscriptions"] == 2 and result["new"] == 2
    assert db.get_video("aaaaaaaaaaa")["title"] == "The Largest Star in the Universe"
    assert db.get_video("bbbbbbbbbbb")["is_short"] == 1
    assert app_client.get("/api/status").json()["backend"]["last_backend"] == "youtube"


def test_youtube_shorts_are_filtered_without_a_duration(app_client):
    db = app_client.app.state.db
    db.execute("INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?, 'K', 1.0, 0)",
               (CHANNEL,))
    app_client.post("/api/sync")
    app_client.post("/api/score", json={"limit": 10, "transcripts": False})
    result = ranking.recommend(db, resolve_settings({}))
    ids = {i.id for i in result.items}
    assert "aaaaaaaaaaa" in ids and "bbbbbbbbbbb" not in ids
    assert any("Short" in r["reason"] for r in result.diagnostics["rejected"])


def test_thumbnails_never_point_at_a_dead_instance(app_client):
    """The grey-box glitch: with no Invidious, every real thumbnail pointed at it."""
    response = app_client.get("/thumb/aaaaaaaaaaa")
    assert response.status_code == 302
    assert response.headers["location"] == "https://i.ytimg.com/vi/aaaaaaaaaaa/mqdefault.jpg"
    assert app_client.get("/thumb/demo0001").headers["location"] == "/demo/thumb/demo0001.svg"


def test_playlist_import_works_without_invidious(app_client):
    response = app_client.post("/api/import/playlist", json={"playlist": "PLxxxxxxxxxxxx"})
    assert response.status_code == 200 and response.json()["imported"] == 2


def test_backend_choice_takes_effect_immediately(app_client):
    app_client.post("/api/settings", json={"source": {"backend": "invidious"}})
    assert app_client.post("/api/import/playlist", json={"playlist": "PLxxxxxxxxxxxx"}).status_code == 502
    app_client.post("/api/settings", json={"source": {"backend": "youtube"}})
    assert app_client.post("/api/import/playlist", json={"playlist": "PLxxxxxxxxxxxx"}).status_code == 200


# -- storage and imports -------------------------------------------------------------


def test_a_poorer_refresh_does_not_erase_detail(tmp_path):
    """Regression: every refresh overwrote every column, so a sync erased the
    likes, keywords and category a fuller lookup had found — and an RSS entry,
    which has no duration, would have zeroed every duration."""
    db = Database(str(tmp_path / "t.db"))
    db.upsert_videos([{"id": "aaaaaaaaaaa", "title": "Full", "duration": 600, "likes": 50,
                       "views": 1000, "keywords": ["x"], "genre": "Education", "description": "long"}])
    db.upsert_videos([{"id": "aaaaaaaaaaa", "title": "Renamed", "views": 1200, "is_short": 1}])
    db.upsert_videos([{"id": "aaaaaaaaaaa", "views": 900}])
    row = dict(db.get_video("aaaaaaaaaaa"))
    assert row["title"] == "Renamed"
    assert (row["duration"], row["likes"], row["genre"], row["description"]) == (600, 50, "Education", "long")
    assert json.loads(row["keywords"]) == ["x"]
    assert row["views"] == 1200 and row["is_short"] == 1


def test_a_large_history_import_does_not_fetch_every_video(tmp_path):
    """Regression: every unknown video was fetched inside the upload request."""
    class Counting:
        calls = 0

        def video(self, vid):
            Counting.calls += 1
            return {"videoId": vid, "title": vid}

    db = Database(str(tmp_path / "t.db"))
    history = [{"videoId": f"{i:011d}", "time": 1_700_000_000 + i} for i in range(500)]
    assert import_history(db, history, Counting()) == 500
    assert Counting.calls == 50


def test_import_stops_asking_once_nothing_answers(tmp_path):
    class Dead:
        calls = 0

        def video(self, vid):
            Dead.calls += 1
            raise UpstreamUnavailable("down")

    db = Database(str(tmp_path / "t.db"))
    import_history(db, [{"videoId": f"{i:011d}"} for i in range(40)], Dead())
    assert Dead.calls == 1


def test_backfill_fills_in_imported_videos(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.executemany("INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,0.6,0,'import')",
                   [(f"{i:011d}", i) for i in range(30)])

    class Api:
        def video(self, vid):
            return {"videoId": vid, "title": f"title {vid}"}

    ingestor = Ingestor(offline_config(tmp_path), db, Api(), community=None)
    assert ingestor.backfill(limit=20) == 20
    assert ingestor.backfill(limit=20) == 10
    assert ingestor.backfill(limit=20) == 0
    assert db.get_video(f"{0:011d}")["title"] == f"title {0:011d}"


def test_shorts_flag_hides_a_video_with_no_duration(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    video = {"id": "bbbbbbbbbbb", "title": "Why stars explode", "author": "K", "author_id": CHANNEL,
             "published": int(time.time()), "duration": 0, "views": 1, "likes": 0,
             "description": "", "keywords": [], "genre": "", "is_short": 1}
    db.upsert_videos([video])
    # Subscribed, so the video is a candidate and reaches the filter at all.
    db.execute("INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?, 'K', 1.0, 0)",
               (CHANNEL,))
    stored = dict(db.get_video("bbbbbbbbbbb"))  # scoring always runs on a stored row
    db.executemany(scoring.INSERT_SCORE, [scoring.card_to_row(scoring.score_video(stored))])
    result = ranking.recommend(db, resolve_settings({}))
    assert not result.items
    assert result.diagnostics["rejected"][0]["reason"] == "a YouTube Short (Shorts filter)"
