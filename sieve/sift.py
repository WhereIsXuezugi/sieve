"""Sift: run the algorithm over anything.

The homepage ranks what your sources bring in. Sift ranks what you point it at:

    search      "rust async runtimes"             videos YouTube finds for a query
    channel     /channel/UC…, /@handle, UC…      a channel's recent uploads
    playlist    ?list=…, PL…                      every video in a playlist
    video       watch?v=…, youtu.be/…             more like one video
    interests   (nothing typed)                   fresh searches for what you like

Whatever comes back goes through the same pipeline as the homepage — scored,
filtered, weighted, explained — with every rejection recorded. Only the
homepage's composition tools (per-channel caps, quotas, variety) are off: when
you sift one channel you want its best videos, not three of them.

Fetched videos join the catalogue, so a good search keeps paying off: its
videos can surface on the homepage later, through your interests.
"""

from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import actions, ranking, scoring
from . import interests as interest_store
from . import textutil as T
from .db import Database
from .invidious import UpstreamUnavailable, normalise_video

KINDS = ("auto", "search", "channel", "playlist", "video", "interests")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE = re.compile(r"^@?([A-Za-z0-9._-]{3,30})$")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class SiftError(actions.ActionError):
    pass


def resolve(ref: str, kind: str = "auto") -> tuple[str, str]:
    """Work out what was pasted. Returns (kind, value)."""
    ref = (ref or "").strip()
    if kind not in KINDS:
        raise SiftError(f"kind must be one of {list(KINDS)}")
    if kind == "interests" or (kind == "auto" and not ref):
        return "interests", ""
    if not ref:
        raise SiftError("type a search, or paste a channel, playlist or video address")
    if kind == "search":
        return "search", ref
    if kind == "playlist":
        return "playlist", actions.parse_playlist_ref(ref)

    looks_like_url = "://" in ref or ref.startswith(("www.", "youtube.", "youtu.be", "m.youtube."))
    parsed = urlparse(ref if "://" in ref else f"https://{ref}") if looks_like_url else None
    query = parse_qs(parsed.query) if parsed else {}
    path = parsed.path if parsed else ""

    if kind in ("auto", "video"):
        vid = ""
        if parsed:
            if "v" in query:
                vid = query["v"][0]
            elif parsed.netloc.endswith("youtu.be"):
                vid = path.strip("/").split("/")[0]
            elif path.startswith(("/shorts/", "/embed/", "/live/")):
                vid = path.split("/")[2] if len(path.split("/")) > 2 else ""
        elif kind == "video":
            vid = ref
        if vid:
            if not VIDEO_ID.match(vid):
                raise SiftError(f"{vid!r} is not a YouTube video id")
            return "video", vid
        if kind == "video":
            raise SiftError("that does not look like a video address")

    if kind in ("auto", "channel"):
        if parsed and "/channel/" in path:
            candidate = path.split("/channel/", 1)[1].split("/")[0]
            if CHANNEL_ID.match(candidate):
                return "channel", candidate
        if parsed and path.startswith("/@"):
            return "handle", path[2:].split("/")[0]
        if CHANNEL_ID.match(ref):
            return "channel", ref
        if ref.startswith("@") and HANDLE.match(ref):
            return "handle", ref[1:]
        if kind == "channel":
            match = HANDLE.match(ref)
            if match:
                return "handle", match.group(1)
            raise SiftError("paste a channel address, a UC… id, or an @handle")

    if kind == "auto" and parsed and "list" in query:
        return "playlist", actions.parse_playlist_ref(ref)
    if parsed:
        raise SiftError("that address is not a video, channel or playlist Sieve can read")
    return "search", ref


def fetch(api, db: Database, kind: str, value: str, limit: int, compute: dict) -> tuple[list[dict], str]:
    """Pull candidates from the backend. Returns (raw videos, a human label)."""
    if kind == "search":
        found = api.search(value)
        if not found:
            raise SiftError(
                "the search returned nothing. Searching needs Invidious or yt-dlp; with "
                "neither, paste a channel or playlist address instead")
        return found[:limit], f"search results for “{value}”"
    if kind == "handle":
        value = api.resolve_handle(value)
        kind = "channel"
    if kind == "channel":
        videos = api.channel_videos(value)
        name = videos[0].get("author") if videos else value
        return videos[:limit], f"recent uploads from {name}"
    if kind == "playlist":
        data = api.playlist(value)
        return (data.get("videos") or [])[:limit], f"the playlist “{data.get('title') or value}”"
    if kind == "video":
        seed = db.get_video(value)
        title = seed["title"] if seed else ""
        if not title:
            title = (api.video(value) or {}).get("title", "")
        terms = " ".join(T.top_terms(T.term_vector([(title, 1.0)]), 5)) or title
        if not terms:
            raise SiftError("could not read that video's title to search for similar ones")
        found = [v for v in api.search(terms) if (v.get("videoId") or v.get("id")) != value]
        return found[:limit], f"videos like “{title or value}”"
    if kind == "interests":
        positive, _ = interest_store.interest_vector(db)
        terms = T.top_terms(positive, max(3, int(compute.get("discover_terms", 4))))
        if not terms:
            raise SiftError("Sieve does not know your interests yet; watch a few videos or add "
                            "interests on the Debugger page")
        per_term = max(5, limit // len(terms))
        found, seen = [], set()
        for term in terms:
            try:
                batch = api.search(term)
            except UpstreamUnavailable:
                continue
            for video in batch[:per_term]:
                vid = video.get("videoId") or video.get("id")
                if vid and vid not in seen:
                    seen.add(vid)
                    found.append(video)
        if not found:
            raise SiftError("the interest searches returned nothing. Searching needs Invidious "
                            "or yt-dlp")
        return found[:limit], "fresh searches for " + ", ".join(terms)
    raise SiftError(f"unknown kind {kind!r}")


def sift_settings(settings: dict, limit: int, apply_filters: bool) -> dict:
    """The user's settings, adapted to ranking one source."""
    s = copy.deepcopy(settings)
    s["homepage"]["count"] = limit
    s["diversity"]["enabled"] = False     # no per-channel cap: you asked for this channel
    s["budget"]["enabled"] = False
    if not apply_filters:
        # Rank everything; hide nothing but what you blocked outright.
        f = s["filters"]
        for key in list(f):
            if key.startswith("max_"):
                f[key] = 100 if key not in ("max_duration", "max_views", "max_subs", "max_age_days") else 0
            elif key.startswith("min_"):
                f[key] = 0
        f.update({"hide_shorts": False, "hide_watched": False, "hide_live": False,
                  "languages": []})
        s["rules"]["enabled"] = False
        s["sponsorblock"]["max_sponsor_ratio"] = 1.0
        s["sponsorblock"]["max_filler_ratio"] = 1.0
        s["sponsorblock"]["hide_exclusive_access"] = False
    return s


def run(db: Database, api, settings: dict, ref: str, kind: str = "auto",
        limit: int | None = None, apply_filters: bool = True, community=None) -> dict[str, Any]:
    compute = settings.get("compute", {})
    limit = max(1, min(int(limit or compute.get("sift_limit", 40)), 300))
    resolved_kind, value = resolve(ref, kind)
    raw, label = fetch(api, db, resolved_kind, value, limit, compute)

    videos = [{**normalise_video(v), "origin": "sift"} for v in raw]
    videos = [v for v in videos if v["id"]]
    db.upsert_videos(videos)
    _score_now(db, [v["id"] for v in videos])

    pool = {v["id"]: {"sift": 1.0} for v in videos}
    result = ranking.recommend(db, sift_settings(settings, limit, apply_filters), pool=pool,
                               record=False, ledger=True, community=community)
    return {"kind": resolved_kind, "value": value, "label": label, "fetched": len(videos),
            "filters": apply_filters, "result": result}


def _score_now(db: Database, ids: list[str]) -> None:
    """Score fetched videos immediately, without transcripts: a Sift is
    interactive, and a caption request per video would make it crawl."""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = db.query(
        f"SELECT v.* FROM videos v LEFT JOIN scores s ON s.video_id = v.id "
        f"WHERE v.id IN ({marks}) AND (s.video_id IS NULL OR s.version < ?)",
        [*ids, scoring.SCORER_VERSION])
    db.executemany(scoring.INSERT_SCORE,
                   [scoring.card_to_row(scoring.score_video(dict(r), dict(r).get("transcript") or ""))
                    for r in rows])
