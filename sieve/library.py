"""Your own catalogue: search it, read a video's chapters, keep notes, export.

Search. Every scored video has a term vector (scoring.py). A query is turned
into one, compared by cosine similarity, then expanded once with the terms
its best matches share — pseudo-relevance feedback — so "heap exploit" also
finds videos that only say "tcache poisoning" or "use after free". Exact
words in the title count extra. Filters (length, watched, channel) apply
after. No network: this only ever reads your database.

Chapters. YouTube's own rule: a description lists timestamps, the first at
0:00, at least two of them. Anything else is not treated as chapters, so a
description that mentions "at 3:15 he says" is left alone.

Export. Notes you take on the video page, and optionally everything you
watched, as Obsidian or Logseq Markdown (one page per video, with links back to
the moment) or a Readwise CSV.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import re
import time
import zipfile
from typing import Any

from . import textutil as T
from .db import Database

# ---------------------------------------------------------------- search


def search(db: Database, query: str, *, limit: int = 40, unwatched: bool = False,
           min_duration: int = 0, max_duration: int = 0, channel: str = "") -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    q = T.term_vector([(query, 1.0)])
    if not q:
        return []
    rows = db.query(
        "SELECT v.id, v.title, v.author, v.author_id, v.duration, v.published, v.views, s.vector "
        "FROM videos v JOIN scores s ON s.video_id = v.id")
    watched = db.watched_ids() if unwatched else set()
    words = set(T.content_tokens(query))
    candidates = []
    for row in rows:
        if unwatched and row["id"] in watched:
            continue
        if min_duration and (row["duration"] or 0) < min_duration:
            continue
        if max_duration and (row["duration"] or 0) > max_duration:
            continue
        if channel and channel.lower() not in (row["author"] or "").lower() and channel != row["author_id"]:
            continue
        vector = json.loads(row["vector"] or "{}")
        candidates.append((row, vector))

    def rank(qv: dict[str, float]) -> list[tuple[float, Any, dict, float]]:
        """(score, row, vector, similarity): the score adds a bonus for query
        words in the title; the plain similarity decides what is relevant."""
        scored = []
        for row, vector in candidates:
            sim = T.cosine(qv, vector)
            score = sim
            title_words = set(T.content_tokens(row["title"] or ""))
            if words and words <= title_words:
                score += 0.25          # every query word is in the title
            elif words & title_words:
                score += 0.1 * len(words & title_words) / len(words)
            if sim > 0.02:
                scored.append((score, row, vector, sim))
        return sorted(scored, key=lambda item: -item[0])

    first = rank(q)
    # Expand once with what the best matches have in common.
    if first:
        expansion: dict[str, float] = {}
        for _, _, vector, _ in first[:8]:
            for term, weight in vector.items():
                expansion[term] = expansion.get(term, 0.0) + weight
        top = sorted(expansion.items(), key=lambda kv: -kv[1])[:12]
        norm = max((w for _, w in top), default=1.0)
        expanded = dict(q)
        for term, weight in top:
            expanded[term] = expanded.get(term, 0.0) + 0.35 * weight / norm
        results = rank(expanded)
    else:
        results = first
    # Relevant means close to the best match, not merely above zero: two
    # videos share generic words, and that alone is not a match.
    best = max((r[3] for r in results), default=0.0)
    results = [r for r in results if r[3] >= max(0.06, 0.2 * best)]
    return [{"id": row["id"], "title": row["title"], "author": row["author"],
             "duration": row["duration"], "published": row["published"], "views": row["views"],
             "match": round(min(1.0, sim), 3)} for _, row, _, sim in results[:limit]]


# -------------------------------------------------------------- chapters

_CHAPTER = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*[-–—:|]?\s*(.+?)\s*$", re.M)


def chapters(description: str) -> list[dict[str, Any]]:
    found = []
    for match in _CHAPTER.finditer(description or ""):
        hours, minutes, seconds, title = match.groups()
        at = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
        found.append({"at": at, "title": title[:120]})
    # YouTube's rule: at least two, the first at 0:00, in order.
    if len(found) < 2 or found[0]["at"] != 0:
        return []
    if any(b["at"] <= a["at"] for a, b in itertools.pairwise(found)):
        return []
    return found


# ----------------------------------------------------------------- notes


def add_note(db: Database, video_id: str, text: str, at_second: int | None = None) -> int:
    text = (text or "").strip()
    if not text:
        raise ValueError("a note needs some text")
    cur = db.execute("INSERT INTO notes(video_id, at_second, text, created_at) VALUES(?,?,?,?)",
                     (video_id, None if at_second is None else max(0, int(at_second)), text[:10000],
                      int(time.time())))
    return int(cur.lastrowid)


def notes_for(db: Database, video_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT id, at_second, text, created_at FROM notes WHERE video_id = ? "
        "ORDER BY COALESCE(at_second, -1), created_at", (video_id,))]


def delete_note(db: Database, note_id: int) -> bool:
    return db.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount > 0


# ---------------------------------------------------------------- export

FORMATS = ("obsidian", "logseq", "readwise")


def _clock(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name).strip()[:100] or "untitled"


def _videos_to_export(db: Database, include_watched: bool) -> list[dict[str, Any]]:
    ids = {r["video_id"] for r in db.query("SELECT DISTINCT video_id FROM notes")}
    if include_watched:
        ids |= {r["video_id"] for r in db.query("SELECT DISTINCT video_id FROM history WHERE progress >= 0.5")}
    out = []
    for vid in sorted(ids):
        row = db.get_video(vid)
        if row is None:
            continue
        video = dict(row)
        score = db.one("SELECT topics FROM scores WHERE video_id = ?", (vid,))
        video["topics"] = json.loads(score["topics"]) if score and score["topics"] else []
        video["notes"] = notes_for(db, vid)
        video["progress"] = db.scalar("SELECT MAX(progress) FROM history WHERE video_id = ?", (vid,), None)
        out.append(video)
    return out


def export(db: Database, fmt: str, include_watched: bool = False) -> tuple[bytes, str, str]:
    """(content, media type, file name)."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    videos = _videos_to_export(db, include_watched)
    if fmt == "readwise":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Highlight", "Title", "Author", "URL", "Note", "Location", "Date"])
        for v in videos:
            url = f"https://www.youtube.com/watch?v={v['id']}"
            for note in v["notes"] or [{"text": v["title"], "at_second": None, "created_at": 0}]:
                at = note["at_second"]
                writer.writerow([note["text"], v["title"], v["author"],
                                 f"{url}&t={at}" if at is not None else url, "",
                                 at if at is not None else "",
                                 time.strftime("%Y-%m-%d", time.localtime(note["created_at"] or time.time()))])
        return buffer.getvalue().encode(), "text/csv", "sieve-readwise.csv"

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for v in videos:
            url = f"https://www.youtube.com/watch?v={v['id']}"
            if fmt == "obsidian":
                lines = ["---", f"title: \"{v['title'].replace(chr(34), chr(39))}\"",
                         f"channel: \"{(v['author'] or '').replace(chr(34), chr(39))}\"",
                         f"url: {url}", f"topics: [{', '.join(v['topics'][:8])}]"]
                if v["progress"] is not None:
                    lines.append(f"watched: {round(100 * v['progress'])}%")
                lines += ["---", "", f"# {v['title']}", "", f"[Watch]({url})", ""]
                for note in v["notes"]:
                    at = note["at_second"]
                    stamp = f"[{_clock(at)}]({url}&t={at}) " if at is not None else ""
                    lines.append(f"- {stamp}{note['text']}")
            else:   # logseq: properties, then an outline
                lines = [f"title:: {v['title']}", f"channel:: {v['author'] or ''}", f"url:: {url}",
                         f"tags:: {', '.join(v['topics'][:8])}", ""]
                for note in v["notes"]:
                    at = note["at_second"]
                    stamp = f"[{_clock(at)}]({url}&t={at}) " if at is not None else ""
                    lines.append(f"- {stamp}{note['text']}")
                if not v["notes"]:
                    lines.append(f"- [Watch]({url})")
            z.writestr(f"{_safe(v['title'])} ({v['id']}).md", "\n".join(lines) + "\n")
    return archive.getvalue(), "application/zip", f"sieve-{fmt}.zip"


# ------------------------------------------------------- homepage search


def match_subset(db: Database, query: str, ids: list[str]) -> list[str]:
    """Which of `ids` (the videos on your homepage) match `query`, by the
    same term-vector similarity as the catalogue search, plus words in the
    title or channel name. Returns them in the order given: searching finds
    videos on the page, it never re-ranks them. Reads only."""
    query = (query or "").strip()
    if not query or not ids:
        return []
    q = T.term_vector([(query, 1.0)])
    words = set(T.content_tokens(query)) or {w.lower() for w in query.split()}
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in db.query(
        f"SELECT v.id, v.title, v.author, s.vector FROM videos v LEFT JOIN scores s ON s.video_id = v.id "
        f"WHERE v.id IN ({marks})", ids)}
    sims: dict[str, float] = {}
    for vid, row in rows.items():
        text_words = set(T.content_tokens(f"{row['title'] or ''} {row['author'] or ''}"))
        lowered = f"{row['title'] or ''} {row['author'] or ''}".lower()
        literal = any(w in lowered for w in words)
        sim = T.cosine(q, json.loads(row["vector"] or "{}")) if q else 0.0
        if literal or words & text_words:
            sim += 0.5
        sims[vid] = sim
    best = max(sims.values(), default=0.0)
    keep = {vid for vid, sim in sims.items() if sim >= 0.5 or (sim > 0.05 and sim >= 0.35 * best)}
    return [vid for vid in ids if vid in keep]
