"""YouTube, directly, for anyone not running Invidious.

Two existing tools, each used for what it is good at:

* **YouTube's own RSS feeds** for channel uploads. No API key, no scraping,
  stable for well over a decade, one request per channel. Each entry has the
  title, description, publish time, views, likes — and a link that is a
  `/shorts/` URL when the video is a Short. What a feed does not have is the
  duration, and it lists only the latest fifteen videos.
* **yt-dlp**, the standard YouTube extractor, when installed
  (`pip install 'sieve[youtube]'`). It fills in what the feeds cannot:
  durations, whole playlists, search, full video details and caption tracks.

Without yt-dlp, Sieve still works against YouTube: channels sync, Shorts are
recognised, and filters that need a duration abstain rather than guess.

Everything this module returns has the same shape as Invidious' JSON, so the
rest of Sieve cannot tell which backend a video came from — `upstream.py`
chooses between them.
"""

from __future__ import annotations

import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx

from .config import Config
from .db import Database
from .invidious import NotFound, UpstreamUnavailable, _strip_vtt
from .pulls import PullLimitReached

log = logging.getLogger("sieve.youtube")

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
THUMBNAIL = "https://i.ytimg.com/vi/{id}/mqdefault.jpg"


def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


# The results page is served differently to non-browser agents.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


class YouTube:
    name = "youtube"

    def __init__(self, cfg: Config, db: Database, ytdlp_factory=None):
        self.cfg = cfg
        self.db = db
        self._client = httpx.Client(
            timeout=cfg.request_timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; sieve/0.1)",
                     "Accept-Language": "en"},
        )
        # Injectable so tests can stand in for yt-dlp without the network.
        self._ytdlp_factory = ytdlp_factory
        self._info_cache: dict[str, dict] = {}
        # See Invidious.on_request: called right before a request goes out.
        self.on_request = None
        # Returns True when the pull limit is tight; then a channel costs one
        # request (its feed) rather than two (feed, then yt-dlp for durations).
        self.frugal = None

    def _spend(self, kind: str) -> None:
        if self.on_request is not None:
            self.on_request(kind)

    @property
    def has_ytdlp(self) -> bool:
        if self._ytdlp_factory is not None:
            return True
        return self.cfg.use_ytdlp and ytdlp_available()

    @property
    def base(self) -> str:
        return self.cfg.youtube_url.rstrip("/")

    def close(self) -> None:
        self._client.close()

    # -- caching, in the same table the Invidious client uses -------------

    def _cached(self, key: str) -> Any | None:
        row = self.db.one("SELECT body FROM http_cache WHERE url = ? AND expires_at > ?",
                          (key, int(time.time())))
        return json.loads(row["body"]) if row else None

    def _store(self, key: str, value: Any, ttl: int) -> None:
        now = int(time.time())
        self.db.execute(
            "INSERT INTO http_cache(url, body, fetched_at, expires_at) VALUES(?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET body=excluded.body, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at",
            (key, json.dumps(value), now, now + ttl),
        )

    # -- RSS -----------------------------------------------------------------

    def _feed(self, **params: str) -> dict:
        key = "yt:rss:" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self._cached(key)
        if hit is not None:
            return hit
        self._spend("channel" if "channel_id" in params else "playlist")
        try:
            response = self._client.get(f"{self.base}/feeds/videos.xml", params=params)
            if response.status_code == 404:
                # Not an outage: this one channel or playlist does not exist.
                # A distinct type, so a sync skips it and carries on.
                raise NotFound(f"YouTube has no feed for {params}")
            response.raise_for_status()
            parsed = parse_feed(response.text)
        except UpstreamUnavailable:
            raise
        except Exception as exc:
            raise UpstreamUnavailable(f"YouTube feed failed: {exc}") from None
        self._store(key, parsed, self.cfg.cache_ttl)
        return parsed

    # -- yt-dlp ---------------------------------------------------------------

    def _ytdlp(self, url: str, *, flat: bool, limit: int | None = None) -> dict:
        from . import ytauth

        # After a bot check, leave yt-dlp alone for a while (ytauth.py); the
        # callers fall back to feeds, the results page and oEmbed.
        if self.db is not None and ytauth.status(self.db)["blocked"]:
            raise ytauth.BotCheck("yt-dlp is paused after a YouTube bot check")
        options: dict[str, Any] = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "socket_timeout": self.cfg.request_timeout, "noprogress": True,
        }
        auth = ytauth.ytdlp_options(self.db, self.cfg.data_dir) if self.db is not None else {}
        options.update(auth)
        if flat:
            options["extract_flat"] = "in_playlist"
        if limit:
            options["playlistend"] = limit
        self._spend("search" if url.startswith("ytsearch") else
                    "playlist" if "playlist?" in url else
                    "video" if "watch?v=" in url else "channel")
        if self._ytdlp_factory is not None:
            ydl = self._ytdlp_factory(options)
        else:
            import yt_dlp

            ydl = yt_dlp.YoutubeDL(options)
        try:
            with ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            if ytauth.is_bot_check(str(exc)) and self.db is not None:
                ytauth.record(self.db, str(exc), signed_in=bool(auth))
                raise ytauth.BotCheck(ytauth.advice(bool(auth))) from None
            raise UpstreamUnavailable(f"yt-dlp could not read {url}: {exc}") from None
        if self.db is not None:
            ytauth.clear(self.db)
        return ydl.sanitize_info(info) if hasattr(ydl, "sanitize_info") else info

    def _full_info(self, video_id: str) -> dict:
        if video_id not in self._info_cache:
            if len(self._info_cache) > 64:
                self._info_cache.clear()
            self._info_cache[video_id] = self._ytdlp(
                f"https://www.youtube.com/watch?v={video_id}", flat=False)
        return self._info_cache[video_id]

    # -- the interface upstream.py calls ------------------------------------

    def channel_videos(self, channel_id: str, sort: str = "newest") -> list[dict]:
        """Latest uploads: the feed for dates and Shorts, yt-dlp for durations."""
        feed_videos: list[dict] = []
        feed_error: Exception | None = None
        try:
            feed_videos = self._feed(channel_id=channel_id)["videos"]
        except UpstreamUnavailable as exc:
            feed_error = exc
        if not self.has_ytdlp or (feed_videos and self.frugal is not None and self.frugal()):
            if feed_error:
                raise feed_error
            return feed_videos

        try:
            listing = self._ytdlp(f"https://www.youtube.com/channel/{channel_id}/videos",
                                  flat=True, limit=30)
        except UpstreamUnavailable:
            if feed_error:
                raise feed_error from None
            return feed_videos
        details = {v["videoId"]: v for v in (from_ytdlp(e) for e in listing.get("entries") or []) if v}
        merged = []
        for video in feed_videos:
            extra = details.pop(video["videoId"], None)
            if extra:
                video = {**video, **{k: v for k, v in extra.items() if v not in (None, "", 0, [])}}
                video["published"] = video.get("published") or extra.get("published") or 0
            merged.append(video)
        merged.extend(details.values())  # older uploads the feed no longer lists
        return merged

    def resolve_handle(self, handle: str) -> str:
        """@name -> UC… channel id. Needs yt-dlp: a handle page is not a feed."""
        if not self.has_ytdlp:
            raise UpstreamUnavailable(
                f"@{handle} can only be looked up with yt-dlp installed; paste the channel's "
                "/channel/UC… address instead")
        info = self._ytdlp(f"https://www.youtube.com/@{handle}/videos", flat=True, limit=1)
        channel = _channel_id(info.get("channel_id") or "")
        if not channel.startswith("UC"):
            raise UpstreamUnavailable(f"no channel called @{handle}")
        return channel

    def channel(self, channel_id: str) -> dict:
        feed = self._feed(channel_id=channel_id)
        return {"author": feed.get("title", ""), "authorId": channel_id,
                "latestVideos": feed["videos"]}

    def playlist(self, playlist_id: str) -> dict:
        if self.has_ytdlp:
            try:
                info = self._ytdlp(f"https://www.youtube.com/playlist?list={playlist_id}",
                                   flat=True, limit=1000)
                return {"title": info.get("title") or "", "playlistId": playlist_id,
                        "videos": [v for v in (from_ytdlp(e) for e in info.get("entries") or []) if v]}
            except UpstreamUnavailable:
                log.info("yt-dlp could not read playlist %s; falling back to its feed", playlist_id)
        feed = self._feed(playlist_id=playlist_id)
        # A playlist feed lists only the latest fifteen entries.
        return {"title": feed.get("title", ""), "playlistId": playlist_id,
                "videos": feed["videos"], "truncated": True}

    def video(self, video_id: str) -> dict:
        if self.has_ytdlp:
            try:
                return from_ytdlp(self._full_info(video_id)) or {}
            except UpstreamUnavailable as exc:
                if isinstance(exc, PullLimitReached):
                    raise
                # yt-dlp failed or is paused after a bot check: oEmbed still
                # knows the title and channel.
        # Without yt-dlp: YouTube's oEmbed endpoint, which knows the title and
        # channel name but nothing else.
        self._spend("video")
        try:
            response = self._client.get(
                f"{self.base}/oembed",
                params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise UpstreamUnavailable(f"YouTube oEmbed failed for {video_id}: {exc}") from None
        return {"videoId": video_id, "title": data.get("title", ""),
                "author": data.get("author_name", ""), "authorId": ""}

    def search(self, query: str, **params: Any) -> list[dict]:
        """yt-dlp when installed; otherwise — or if it fails — YouTube's own
        results page (ytsearch.py). Search used to need yt-dlp or Invidious
        and returned nothing without either, silently."""
        newest = params.get("sort_by") == "upload_date"
        if self.has_ytdlp:
            try:
                prefix = "ytsearchdate20" if newest else "ytsearch20"
                info = self._ytdlp(f"{prefix}:{query}", flat=True)
                found = [v for v in (from_ytdlp(e) for e in info.get("entries") or []) if v]
                if found:
                    return found
            except PullLimitReached:
                raise
            except UpstreamUnavailable:
                pass   # fall back to the results page
        return self._search_page(query, newest)

    def _search_page(self, query: str, newest: bool) -> list[dict]:
        from . import ytsearch

        key = f"yt:search:{int(newest)}:{query}"
        hit = self._cached(key)
        if hit is not None:
            return hit["videos"]
        self._spend("search")
        try:
            response = self._client.get(
                f"{self.base}/results",
                params={"search_query": query, "sp": ytsearch.FILTER_NEWEST if newest
                        else ytsearch.FILTER_RELEVANT, "hl": "en", "gl": "US"},
                cookies=ytsearch.CONSENT_COOKIES,
                headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"YouTube search failed: {exc}") from exc
        data = ytsearch.extract_initial_data(response.text)
        if data is None:
            # A consent wall, a bot check, or a new page layout: say so rather
            # than report an empty search as a real result.
            raise UpstreamUnavailable("YouTube's search page had no results data "
                                      "(a consent or bot check, or a changed layout)")
        videos = ytsearch.parse_results(data)
        self._store(key, {"videos": videos}, 3600)
        return videos

    def trending(self, region: str = "US", category: str = "") -> list[dict]:
        return []  # YouTube retired its Trending page; discovery comes from search

    def popular(self) -> list[dict]:
        return []

    def captions(self, video_id: str, lang: str = "en") -> str:
        if not self.has_ytdlp:
            return self._captions_from_watch_page(video_id, lang)
        try:
            info = self._full_info(video_id)
        except UpstreamUnavailable:
            return self._captions_from_watch_page(video_id, lang)
        track = pick_caption(info, lang)
        if not track:
            return ""
        self._spend("captions")
        try:
            response = self._client.get(track)
            response.raise_for_status()
        except Exception:
            return ""
        return _strip_vtt(response.text)[: self.cfg.transcript_max_chars]

    def _captions_from_watch_page(self, video_id: str, lang: str) -> str:
        """Without yt-dlp: the watch page lists every caption track, YouTube's
        automatic ones included. Two requests: the page, then the track."""
        from . import ytsearch

        self._spend("captions")
        try:
            page = self._client.get(f"{self.base}/watch", params={"v": video_id, "hl": "en"},
                                    cookies=ytsearch.CONSENT_COOKIES, headers={"User-Agent": BROWSER_UA})
            page.raise_for_status()
        except httpx.HTTPError:
            return ""
        player = ytsearch.extract_initial_data(page.text, "ytInitialPlayerResponse")
        url = ytsearch.caption_track(player or {}, lang)
        if not url:
            return ""
        self._spend("captions")
        try:
            track = self._client.get(url + ("&" if "?" in url else "?") + "fmt=vtt",
                                     headers={"User-Agent": BROWSER_UA})
            track.raise_for_status()
        except httpx.HTTPError:
            return ""
        return _strip_vtt(track.text)[: self.cfg.transcript_max_chars]

    @staticmethod
    def thumbnail_url(video_id: str) -> str:
        return THUMBNAIL.format(id=video_id)


# --------------------------------------------------------------------------
# Parsing: pure functions, tested against fixtures
# --------------------------------------------------------------------------


def parse_feed(xml_text: str) -> dict:
    """A channel or playlist feed, as Invidious-shaped videos."""
    root = ET.fromstring(xml_text)
    feed_title = (root.findtext("atom:title", default="", namespaces=NS) or "").strip()
    feed_channel = root.findtext("yt:channelId", default="", namespaces=NS) or ""
    videos = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        link = entry.find("atom:link[@rel='alternate']", NS)
        href = link.get("href", "") if link is not None else ""
        group = entry.find("media:group", NS)
        description = ""
        views = likes = 0
        if group is not None:
            description = group.findtext("media:description", default="", namespaces=NS) or ""
            stats = group.find("media:community/media:statistics", NS)
            rating = group.find("media:community/media:starRating", NS)
            views = _int(stats.get("views") if stats is not None else 0)
            likes = _int(rating.get("count") if rating is not None else 0)
        channel_id = entry.findtext("yt:channelId", default="", namespaces=NS) or feed_channel
        videos.append({
            "videoId": vid,
            "title": (entry.findtext("atom:title", default="", namespaces=NS) or "").strip(),
            "author": (entry.findtext("atom:author/atom:name", default="", namespaces=NS) or feed_title).strip(),
            "authorId": _channel_id(channel_id),
            "published": _timestamp(entry.findtext("atom:published", default="", namespaces=NS)),
            "lengthSeconds": 0,
            "viewCount": views,
            "likeCount": likes,
            "description": description,
            "isShort": "/shorts/" in href,
        })
    return {"title": feed_title, "videos": videos}


def from_ytdlp(info: dict | None) -> dict | None:
    """A yt-dlp info dict — flat or full — as an Invidious-shaped video."""
    if not info:
        return None
    vid = info.get("id") or ""
    if len(vid) != 11:  # channel and playlist entries in a mixed listing
        return None
    url = info.get("url") or info.get("webpage_url") or ""
    live = info.get("live_status") or ""
    published = info.get("timestamp") or info.get("release_timestamp") or 0
    if not published and info.get("upload_date"):
        try:
            published = int(datetime.strptime(info["upload_date"], "%Y%m%d").timestamp())
        except ValueError:
            published = 0
    captions = sorted(set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {}))
    categories = info.get("categories") or []
    return {
        "videoId": vid,
        "title": info.get("title") or "",
        "author": info.get("channel") or info.get("uploader") or "",
        "authorId": _channel_id(info.get("channel_id") or ""),
        "published": int(published or 0),
        "lengthSeconds": int(info.get("duration") or 0),
        "viewCount": int(info.get("view_count") or 0),
        "likeCount": int(info.get("like_count") or 0),
        "description": info.get("description") or "",
        "keywords": info.get("tags") or [],
        "genre": categories[0] if categories else "",
        "liveNow": live == "is_live",
        "isUpcoming": live == "is_upcoming",
        "isFamilyFriendly": int(info.get("age_limit") or 0) < 18,
        "subCount": int(info.get("channel_follower_count") or 0),
        "captions": [{"languageCode": code} for code in captions],
        "isShort": "/shorts/" in url,
    }


def pick_caption(info: dict, lang: str) -> str:
    """URL of the best caption track: human subtitles before automatic ones,
    the exact language before a regional variant, WebVTT when offered."""
    for source in ("subtitles", "automatic_captions"):
        tracks = info.get(source) or {}
        codes = [c for c in tracks if c == lang] + [c for c in tracks if c.startswith(lang + "-")]
        for code in codes:
            formats = tracks[code] or []
            vtt = [f for f in formats if f.get("ext") == "vtt"]
            chosen = (vtt or formats)[:1]
            if chosen and chosen[0].get("url"):
                return chosen[0]["url"]
    return ""


def _channel_id(value: str) -> str:
    # Feeds have been seen to drop the "UC" prefix; everything else in Sieve
    # expects the full id.
    value = (value or "").strip()
    if value and not value.startswith("UC") and len(value) == 22:
        return "UC" + value
    return value


def _timestamp(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
