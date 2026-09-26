"""What a number on a slider means.

Each score gets a reference scale — what a video at 10, 40 or 80 is like — and
the help popover adds real examples from your own catalogue at the slider's
current value, plus how much of the catalogue a limit there would hide. The
bands describe what the model responds to (see scoring.MODELS), so they are
a guide to *this* engine, not a universal truth.
"""

from __future__ import annotations

from typing import Any

from .db import Database

SCALES: dict[str, dict[str, Any]] = {
    "brainrot": {
        "what": "Attention-farming content: memes, frantic Shorts, shouting titles.",
        "bands": [(0, 20, "Calm and substantive: lectures, long explainers."),
                  (20, 45, "Ordinary videos with a catchy title or two."),
                  (45, 70, "Meme-heavy, very short, or loud."),
                  (70, 101, "Near-pure attention bait: meme compilations, reaction Shorts.")]},
    "clickbait": {
        "what": "How hard the title and thumbnail oversell the video.",
        "bands": [(0, 20, "A plain description: “Implementing a B-tree in Rust”."),
                  (20, 40, "Mild hooks: “The trick most tutorials skip”."),
                  (40, 65, "Teasing: caps, “you won't believe”, withheld answers."),
                  (65, 101, "Sensational, or retitled by DeArrow contributors.")]},
    "nsfw": {
        "what": "Sexual or adult content, from wording and YouTube's age flag.",
        "bands": [(0, 15, "Nothing adult detected."),
                  (15, 40, "Suggestive wording, usually harmless."),
                  (40, 70, "Adult themes, or flagged not family-safe."),
                  (70, 101, "Explicitly adult.")]},
    "music": {
        "what": "Whether the video is music rather than talk.",
        "bands": [(0, 20, "Talk, with at most background music."),
                  (20, 50, "Music-related talk: reviews, theory, gear."),
                  (50, 80, "Mostly music: live sessions, covers."),
                  (80, 101, "A song or album upload, or an auto-generated music channel.")]},
    "ai_generated": {
        "what": "Signs the video was made by AI: declared, or synthetic narration.",
        "bands": [(0, 20, "No sign of AI generation."),
                  (20, 50, "Some signs; often a false alarm."),
                  (50, 101, "Declares AI generation, or shows several strong signs.")]},
    "profanity": {
        "what": "How much swearing, measured from captions and title.",
        "bands": [(0, 15, "Clean."), (15, 40, "The odd swear word."),
                  (40, 70, "Regular swearing."), (70, 101, "Constant.")]},
    "education": {
        "what": "How much you would learn: lecture vocabulary, citations, structure.",
        "bands": [(0, 25, "Entertainment, vlogs, music."),
                  (25, 50, "Some learning along the way."),
                  (50, 75, "Explainers and tutorials."),
                  (75, 101, "Lectures and courses, with sources.")]},
    "info_density": {
        "what": "Information per minute: fast, varied, technical narration.",
        "bands": [(0, 25, "Slow or padded."), (25, 50, "Relaxed pace."),
                  (50, 75, "Brisk and to the point."), (75, 101, "Dense: pause often.")]},
    "entertainment": {
        "what": "How much it is made to entertain.",
        "bands": [(0, 30, "Purely informative."), (30, 60, "Informative with personality."),
                  (60, 101, "Made to entertain first.")]},
    "stimulation": {
        "what": "How mentally engaging: ideas, variety, depth.",
        "bands": [(0, 30, "Background viewing."), (30, 60, "Engaging."),
                  (60, 101, "Demanding; needs your attention.")]},
    "technical_depth": {
        "what": "How deep into the technical detail it goes.",
        "bands": [(0, 30, "Non-technical, or an overview."), (30, 60, "Some technical detail."),
                  (60, 101, "Expert level.")]},
    "production": {
        "what": "Production polish: editing, sponsors, reach.",
        "bands": [(0, 35, "Raw or homemade."), (35, 65, "Tidy."), (65, 101, "Studio-grade.")]},
}

# Filters measured in real units: the popover shows your catalogue's spread.
UNITS = {
    "duration": ("videos.duration", "length", "seconds"),
    "views": ("videos.views", "views", "views"),
    "subs": ("videos.sub_count", "subscribers", "subscribers"),
}


def describe(db: Database, key: str, value: float | None) -> dict[str, Any]:
    """Everything the help popover shows for one slider at one value."""
    if key in SCALES:
        return _score_help(db, key, value)
    if key in UNITS:
        return _unit_help(db, key, value)
    raise KeyError(key)


def _score_help(db: Database, key: str, value: float | None) -> dict[str, Any]:
    scale = SCALES[key]
    total = db.scalar("SELECT COUNT(*) FROM scores", default=0)
    out: dict[str, Any] = {
        "key": key, "what": scale["what"],
        "bands": [{"from": lo, "to": min(hi, 100), "text": text,
                   "current": value is not None and lo <= value < hi} for lo, hi, text in scale["bands"]],
        "examples": [], "above": None, "below": None, "catalogue": total,
    }
    if value is None or not total:
        return out
    # Real titles scored close to this value, nearest first.
    for row in db.query(
            f"SELECT v.title, s.{key} AS score FROM scores s JOIN videos v ON v.id = s.video_id "
            f"WHERE s.{key} BETWEEN ? AND ? ORDER BY ABS(s.{key} - ?) LIMIT 3",
            (value - 6, value + 6, value)):
        out["examples"].append({"title": row["title"], "score": round(row["score"])})
    above = db.scalar(f"SELECT COUNT(*) FROM scores WHERE {key} > ?", (value,), 0)
    out["above"] = round(100 * above / total)
    out["below"] = 100 - out["above"]
    return out


def _unit_help(db: Database, key: str, value: float | None) -> dict[str, Any]:
    column, noun, unit = UNITS[key]
    values = sorted(r[0] for r in db.conn.execute(
        f"SELECT {column.split('.')[1]} FROM videos WHERE {column.split('.')[1]} > 0"))
    out: dict[str, Any] = {"key": key, "what": f"Your catalogue's {noun}.", "unit": unit,
                           "percentiles": {}, "above": None, "catalogue": len(values)}
    if values:
        for p in (10, 25, 50, 75, 90):
            out["percentiles"][str(p)] = values[min(len(values) - 1, len(values) * p // 100)]
        if value:
            out["above"] = round(100 * sum(1 for v in values if v > value) / len(values))
    return out


def full_range(db: Database, key: str, at: int | None = None) -> dict[str, Any]:
    """Every score from 0 to 100 on one axis: how many videos score each
    value, and the videos at one of them — the number line in the "?"
    panel. Uses the real scores; an empty point is simply empty."""
    if key not in SCALES:
        raise KeyError(key)
    histogram = [0] * 101
    for row in db.query(f"SELECT CAST(ROUND({key}) AS INTEGER) AS s, COUNT(*) AS n FROM scores GROUP BY s"):
        if row["s"] is not None and 0 <= row["s"] <= 100:
            histogram[row["s"]] = row["n"]
    out: dict[str, Any] = {"key": key, "what": SCALES[key]["what"], "histogram": histogram,
                           "total": sum(histogram), "at": at, "videos": []}
    if at is not None:
        at = max(0, min(100, int(at)))
        out["at"] = at
        out["videos"] = [dict(r) for r in db.query(
            f"SELECT v.id, v.title, v.author, s.{key} AS score FROM scores s JOIN videos v ON v.id = s.video_id "
            f"WHERE CAST(ROUND(s.{key}) AS INTEGER) = ? ORDER BY v.views DESC LIMIT 12", (at,))]
        band = next((b for b in SCALES[key]["bands"] if b[0] <= at < b[1]), None)
        out["band"] = band[2] if band else ""
    return out
