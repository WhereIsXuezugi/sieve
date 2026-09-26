"""Reading YouTube's search results page without yt-dlp or Invidious.

YouTube serves two shapes for a video in the results — the long-standing
videoRenderer and the newer lockupViewModel — and moves fields around within
them. These pages are built from both, and the parser must read either."""

from __future__ import annotations

import json

import httpx
import pytest

from sieve import ytsearch

NOW = 1_800_000_000


def renderer(vid="aaaaaaaaaa1", title="Heap exploitation from scratch", channel="UCabcdefghijklmnopqrstuv"):
    return {"videoRenderer": {
        "videoId": vid,
        "title": {"runs": [{"text": title}]},
        "ownerText": {"runs": [{"text": "Low Level Lab", "navigationEndpoint": {
            "browseEndpoint": {"browseId": channel}}}]},
        "lengthText": {"simpleText": "1:02:03"},
        "viewCountText": {"simpleText": "12,345 views"},
        "publishedTimeText": {"simpleText": "3 days ago"},
        "detailedMetadataSnippets": [{"snippetText": {"runs": [{"text": "tcache, "}, {"text": "fastbins"}]}}],
    }}


def lockup(vid="bbbbbbbbbb2"):
    return {"lockupViewModel": {
        "contentId": vid,
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"overlays": [{"thumbnailOverlayBadgeViewModel": {
            "thumbnailBadges": [{"thumbnailBadgeViewModel": {"text": "24:10"}}]}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": "Algebraic topology, lecture 7"},
            "image": {"decoratedAvatarViewModel": {"rendererContext": {"commandContext": {"onTap": {
                "innertubeCommand": {"browseEndpoint": {"browseId": "UCzyxwvutsrqponmlkjihgfe"}}}}}}},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": "Maths Department"}}]},
                {"metadataParts": [{"text": {"content": "1.2K views"}}, {"text": {"content": "2 weeks ago"}}]},
            ]}},
        }},
    }}


def playlist_lockup():
    return {"lockupViewModel": {"contentId": "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                                "contentType": "LOCKUP_CONTENT_TYPE_PLAYLIST"}}


def results_page(*items) -> str:
    items = items or (renderer(), lockup(), playlist_lockup())
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {
        "contents": [{"itemSectionRenderer": {"contents": list(items)}}]}}}}}
    return f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"


def test_both_shapes_are_read():
    found = ytsearch.parse_results(ytsearch.extract_initial_data(results_page()), now=NOW)
    old, new = found
    assert old == {"videoId": "aaaaaaaaaa1", "title": "Heap exploitation from scratch",
                   "author": "Low Level Lab", "authorId": "UCabcdefghijklmnopqrstuv",
                   "description": "tcache, fastbins", "liveNow": False, "isUpcoming": False,
                   "lengthSeconds": 3723, "viewCount": 12345, "published": NOW - 3 * 86400}
    assert new["videoId"] == "bbbbbbbbbb2" and new["title"] == "Algebraic topology, lecture 7"
    assert new["author"] == "Maths Department" and new["authorId"] == "UCzyxwvutsrqponmlkjihgfe"
    assert (new["lengthSeconds"], new["viewCount"], new["published"]) == (1450, 1200, NOW - 2 * 604800)


def test_playlists_and_channels_are_not_videos():
    found = ytsearch.parse_results(ytsearch.extract_initial_data(results_page(playlist_lockup())), now=NOW)
    assert found == []


def test_a_page_with_only_the_new_shape_is_not_empty():
    """The failure scrapers hit in 2026: all lockupViewModel, zero
    videoRenderer, and a parser that knew only the old shape reported an
    empty page as success."""
    page = results_page(lockup("ccccccccccc"), lockup("ddddddddddd"))
    assert len(ytsearch.parse_results(ytsearch.extract_initial_data(page), now=NOW)) == 2


@pytest.mark.parametrize("text, count", [("1,234 views", 1234), ("15M views", 15_000_000),
                                         ("1.2K views", 1200), ("1 view", 1), ("No views", 0)])
def test_view_counts(text, count):
    assert ytsearch.views(text) == count


@pytest.mark.parametrize("text, secs", [("4:05", 245), ("1:02:03", 3723), ("LIVE", 0)])
def test_durations(text, secs):
    assert ytsearch.seconds(text) == secs


def test_a_consent_wall_is_an_error_not_an_empty_search(tmp_path):
    from sieve.invidious import UpstreamUnavailable
    from tests.test_youtube import youtube

    wall = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>Before you continue</html>"))
    with pytest.raises(UpstreamUnavailable, match="consent"):
        youtube(tmp_path, transport=wall).search("x")


def test_consent_cookies_and_video_filter_are_sent(tmp_path):
    from tests.test_youtube import youtube

    seen = {}

    def handler(request):
        seen["cookie"] = request.headers.get("cookie", "")
        seen["sp"] = request.url.params.get("sp")
        return httpx.Response(200, text=results_page())

    youtube(tmp_path, transport=httpx.MockTransport(handler)).search("x", sort_by="upload_date")
    assert "SOCS=CAI" in seen["cookie"] and seen["sp"] == ytsearch.FILTER_NEWEST


def test_the_filter_reaches_youtube_encoded_exactly_once(tmp_path):
    from tests.test_youtube import youtube

    seen = {}

    def handler(request):
        seen["raw"] = str(request.url)
        return httpx.Response(200, text=results_page())

    youtube(tmp_path, transport=httpx.MockTransport(handler)).search("x")
    assert "sp=EgIQAQ%3D%3D" in seen["raw"] and "%253D" not in seen["raw"]
