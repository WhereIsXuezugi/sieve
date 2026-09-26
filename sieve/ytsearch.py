"""Search YouTube without yt-dlp or Invidious, by reading its results page.

The page embeds everything it shows as JSON (`var ytInitialData = {…}`). Its
layout shifts often, and YouTube is part-way through replacing one component
(`videoRenderer`) with another of a completely different shape
(`lockupViewModel`). A parser that follows a fixed path, or knows only one
shape, returns nothing and reports success. So this one walks the whole tree,
accepts both shapes, and recognises durations, view counts and ages by their
format rather than their position. If YouTube changes something, a field goes
missing; the search does not silently empty.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any

# Search filters, as YouTube encodes them: videos only, by relevance or newest.
FILTER_RELEVANT = "EgIQAQ=="   # decoded: httpx encodes it once, as YouTube expects
FILTER_NEWEST = "CAISAhAB"

# Without these, visitors in the EU get a "Before you continue" page instead
# of results.
CONSENT_COOKIES = {"SOCS": "CAI", "CONSENT": "YES+cb"}

_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
_VIEWS = re.compile(r"^([\d.,]+)\s*([KMB]?)\s*views?$", re.I)
_AGO = re.compile(r"(?:streamed\s+)?(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
         "month": 2629800, "year": 31557600}


def extract_initial_data(html: str, name: str = "ytInitialData") -> dict | None:
    """The JSON object after `<name> =` (ytInitialData on results pages,
    ytInitialPlayerResponse on watch pages), or None."""
    match = re.search(rf"(?:var\s+{name}|window\[\"{name}\"\])\s*=\s*", html)
    if not match:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_results(data: dict, now: float | None = None) -> list[dict]:
    """Every video in a search results tree, Invidious-shaped, in page order."""
    now = now or time.time()
    seen: set[str] = set()
    out: list[dict] = []
    for kind, node in _walk(data):
        video = _from_renderer(node, now) if kind == "videoRenderer" else _from_lockup(node, now)
        if video and video["videoId"] not in seen:
            seen.add(video["videoId"])
            out.append(video)
    return out


def _walk(node: Any) -> Iterator[tuple[str, dict]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("videoRenderer", "lockupViewModel") and isinstance(value, dict):
                yield key, value
            else:
                yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _text(node: Any) -> str:
    """YouTube's text objects: {"simpleText": …}, {"runs": [{"text": …}]}, {"content": …}."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    if "runs" in node:
        return "".join(str(r.get("text", "")) for r in node["runs"] if isinstance(r, dict))
    if "content" in node:
        return str(node["content"])
    return ""


def _strings(node: Any) -> Iterator[str]:
    """Every display string under a node."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("text", "content", "simpleText", "label") and isinstance(value, str):
                yield value
            else:
                yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def _channel_id(node: Any) -> str:
    """The first channel id (UC + 22 characters) under a node."""
    if isinstance(node, dict):
        value = node.get("browseId")
        if isinstance(value, str) and value.startswith("UC") and len(value) == 24:
            return value
        for child in node.values():
            found = _channel_id(child)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _channel_id(item)
            if found:
                return found
    return ""


def seconds(text: str) -> int:
    match = _DURATION.match(text.strip())
    if not match:
        return 0
    hours, minutes, secs = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)


def views(text: str) -> int:
    """ "1,234 views", "15M views", "1.2K views", "No views" -> a number."""
    text = text.strip()
    if text.lower().startswith("no views"):
        return 0
    match = _VIEWS.match(text)
    if not match:
        return -1
    number, suffix = match.groups()
    value = float(number.replace(",", "")) if suffix else float(number.replace(",", "").replace(".", ""))
    return int(value * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[suffix.upper()])


def published(text: str, now: float) -> int:
    """ "3 days ago" -> an approximate timestamp; YouTube shows nothing finer."""
    match = _AGO.search(text)
    if not match:
        return 0
    amount, unit = match.groups()
    return int(now - int(amount) * _UNIT[unit.lower()])


def _classify(strings: list[str], now: float) -> dict[str, int]:
    """Durations, views and ages recognised by format, wherever they sit."""
    found = {"lengthSeconds": 0, "viewCount": 0, "published": 0}
    for s in strings:
        if not found["lengthSeconds"] and (n := seconds(s)):
            found["lengthSeconds"] = n
        elif not found["viewCount"] and (n := views(s)) >= 0 and "view" in s.lower():
            found["viewCount"] = n
        elif not found["published"] and (n := published(s, now)):
            found["published"] = n
    return found


def _from_renderer(node: dict, now: float) -> dict | None:
    vid = node.get("videoId")
    if not isinstance(vid, str) or len(vid) != 11:
        return None
    upcoming = "upcomingEventData" in node
    live = any("LIVE" in str(b) for b in node.get("badges", [])) or \
        ("live" in _text(node.get("viewCountText")).lower() and "watching" in _text(node.get("viewCountText")).lower())
    snippet = ""
    for item in node.get("detailedMetadataSnippets") or []:
        snippet = _text(item.get("snippetText"))
        if snippet:
            break
    fields = _classify([_text(node.get("lengthText")), _text(node.get("viewCountText")),
                        _text(node.get("publishedTimeText"))], now)
    return {
        "videoId": vid,
        "title": _text(node.get("title")),
        "author": _text(node.get("ownerText") or node.get("longBylineText")),
        "authorId": _channel_id(node.get("ownerText") or node.get("longBylineText") or {}),
        "description": snippet,
        "liveNow": bool(live),
        "isUpcoming": upcoming,
        **fields,
    }


def _from_lockup(node: dict, now: float) -> dict | None:
    vid = node.get("contentId")
    kind = str(node.get("contentType", ""))
    if not isinstance(vid, str) or len(vid) != 11 or (kind and "VIDEO" not in kind):
        return None  # playlists, channels and mixes share this component
    metadata = node.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = _text(metadata.get("title"))
    rows = list(_strings(metadata.get("metadata", {})))
    image_strings = list(_strings(node.get("contentImage", {})))
    fields = _classify(image_strings + rows, now)
    # The channel name is the metadata string that is none of the others.
    author = next((s for s in rows if s and s != title and s.strip() not in ("•", "·")
                   and not seconds(s) and views(s) < 0 and not published(s, now)), "")
    return {
        "videoId": vid,
        "title": title,
        "author": author,
        "authorId": _channel_id(metadata) or _channel_id(node),
        "description": "",
        "liveNow": any(s.strip().upper() == "LIVE" for s in image_strings),
        "isUpcoming": False,
        **fields,
    }


def caption_track(player: dict, lang: str = "en") -> str:
    """The URL of the best caption track in a watch page's player response:
    human subtitles before automatic ("asr") ones, the exact language before a
    regional variant, then any language at all (a transcript in another
    language still tells the scorer something)."""
    tracks = (((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
              .get("captionTracks") or [])

    def rank(track: dict) -> tuple:
        code = str(track.get("languageCode", ""))
        return (0 if code == lang else 1 if code.startswith(lang + "-") else 2,
                1 if track.get("kind") == "asr" else 0)

    usable = [t for t in tracks if t.get("baseUrl")]
    return sorted(usable, key=rank)[0]["baseUrl"] if usable else ""
