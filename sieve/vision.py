"""Optional visual NSFW scoring.

The brief suggested NudeNet or OpenNSFW2 over extracted video frames. Frame
extraction is the single most expensive thing that was proposed: it means
downloading video for every candidate, running ffmpeg, and scoring N frames —
minutes of CPU and hundreds of megabytes of transfer per video, on a box that is
also meant to be serving pages.

So this module does the cheap 90%: it scores the **thumbnail**, which the
instance already serves, is already being fetched to render the page, and is the
image the uploader chose to represent the video. For the filter people actually
want — "do not put that on my homepage" — the thumbnail is the thing being
filtered.

It is entirely optional. Without the `vision` extra installed this module
reports unavailable and the text-based `nsfw` score carries on alone. Nothing
else in the codebase imports it at module scope.

    pip install 'sieve[vision]'
    sieve vision --limit 500
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence

import httpx

from .config import Config
from .db import Database

log = logging.getLogger("sieve.vision")

NEVER_SCORED = -1.0


class ThumbnailScorer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._client = httpx.Client(timeout=cfg.request_timeout, follow_redirects=True)
        self.error = ""

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import opennsfw2

            self._model = opennsfw2
        except ImportError as exc:
            self.error = (f"vision extra not installed ({exc}). "
                          "pip install 'sieve[vision]' to enable thumbnail scoring.")
            return None
        return self._model

    def score_url(self, url: str) -> float | None:
        """Return an NSFW probability 0..1, or None if it could not be scored."""
        model = self._load()
        if model is None:
            return None
        try:
            from PIL import Image

            response = self._client.get(url)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            return float(model.predict_images([image])[0])
        except Exception as exc:
            log.debug("could not score %s: %s", url, exc)
            return None

    def close(self) -> None:
        self._client.close()


def score_pending(db: Database, cfg: Config, limit: int = 200,
                  thumbnail_url=None) -> dict[str, int]:
    """Score thumbnails for videos that have never been looked at.

    Results are written to ``videos.nsfw_vision`` and folded into the stored
    ``scores.nsfw`` by taking the higher of the two: text evidence and visual
    evidence are independent, and for a filter whose failure mode is "something
    unwanted appeared", either one firing is enough.
    """
    scorer = ThumbnailScorer(cfg)
    if not scorer.available:
        scorer.close()
        return {"scored": 0, "skipped": 0, "error": scorer.error}

    rows = db.query(
        "SELECT id FROM videos WHERE nsfw_vision < 0 ORDER BY fetched_at DESC LIMIT ?",
        (limit,),
    )
    # Wherever thumbnails currently come from — the Invidious proxy or YouTube.
    if thumbnail_url is None:
        from .youtube import THUMBNAIL

        def thumbnail_url(video_id: str) -> str:
            return THUMBNAIL.format(id=video_id)
    scored = skipped = 0
    for row in rows:
        value = scorer.score_url(thumbnail_url(row["id"]))
        if value is None:
            skipped += 1
            continue
        db.execute("UPDATE videos SET nsfw_vision = ? WHERE id = ?", (value, row["id"]))
        db.execute(
            "UPDATE scores SET nsfw = MAX(nsfw, ?) WHERE video_id = ?",
            (round(value * 100, 1), row["id"]),
        )
        scored += 1
    scorer.close()
    return {"scored": scored, "skipped": skipped}


def vision_scores(db: Database, video_ids: Sequence[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    ids = list(video_ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for row in db.query(
            f"SELECT id, nsfw_vision FROM videos WHERE id IN ({marks}) AND nsfw_vision >= 0",
            chunk,
        ):
            out[row["id"]] = float(row["nsfw_vision"])
    return out
