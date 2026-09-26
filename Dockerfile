# Sieve — a programmable recommendation layer for YouTube, with or without Invidious.
#
#   docker compose up -d                      # see docker-compose.yml
#   docker build -t sieve . && docker run -p 127.0.0.1:8377:8377 -v sieve-data:/data sieve
#
# No compiler is needed: every dependency ships wheels and Sieve is pure Python.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Time-zone data, so TZ works: daily analytics roll over at your midnight.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sieve ./sieve

# The youtube extra adds yt-dlp, which gives videos fetched from YouTube their
# durations, whole playlists, search and captions. YouTube changes often and
# yt-dlp follows; rebuild with --pull --no-cache to pick up a new release.
ARG EXTRAS=youtube
RUN if [ -n "$EXTRAS" ]; then pip install ".[${EXTRAS}]"; else pip install .; fi

# Run as an unprivileged user. /data is created here so a named volume
# inherits this ownership on first mount.
RUN useradd --system --uid 10001 --home-dir /data sieve \
 && mkdir -p /data && chown sieve:sieve /data
USER sieve

ENV SIEVE_DATA_DIR=/data
VOLUME /data
EXPOSE 8377

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, httpx; sys.exit(0 if httpx.get('http://127.0.0.1:8377/api/status', timeout=4).status_code == 200 else 1)"

CMD ["sieve", "serve", "--host", "0.0.0.0", "--port", "8377"]
