"""Helpers shared by the tests."""

from __future__ import annotations

from sieve.config import Config

# A local port nothing listens on: every upstream call fails fast and cleanly,
# which is how tests simulate "Invidious and YouTube are both down".
CLOSED = "http://127.0.0.1:9"


def offline_config(data_dir, **extra) -> Config:
    """A Config whose every upstream is unreachable and whose yt-dlp is off.

    Use this for any test that builds an app: it guarantees nothing reaches
    the internet, and `tests/conftest.py` fails any test that tries.
    """
    settings = {
        "data_dir": str(data_dir), "instances": [CLOSED], "request_timeout": 1,
        "sponsorblock_url": CLOSED, "dearrow_url": CLOSED, "dearrow_thumbnail_url": CLOSED,
        "youtube_url": CLOSED, "use_ytdlp": False,
    }
    settings.update(extra)
    return Config(**settings)
