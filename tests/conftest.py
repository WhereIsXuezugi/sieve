"""Shared test configuration.

Tests must never contact an external service. That used to be a sentence in
CONTRIBUTING.md; then a fixture enabled SponsorBlock without redirecting it, and
the suite quietly made real requests to sponsor.ajay.app on every run — slow,
flaky, and rude to a volunteer-run service.

This makes the rule executable: any real HTTP request to a host other than this
machine fails the test that made it, with the URL in the message. Requests to
127.0.0.1 are allowed, because tests deliberately point Sieve at a closed local
port to exercise "the instance is down". FastAPI's TestClient is unaffected: it
talks to the app in-process through its own transport, not the network.
"""

from __future__ import annotations

import httpx
import pytest

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}


class ExternalRequestError(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _no_external_requests(monkeypatch):
    """Refuse the request *and* fail the test afterwards.

    Refusing alone is not enough: Sieve's community and upstream clients catch
    every exception on purpose, so an outage never takes the homepage down.
    They would swallow the refusal too, and the leak would pass silently — so
    every attempt is recorded and reported when the test finishes.
    """
    attempts: list[str] = []
    original = httpx.HTTPTransport.handle_request

    def guarded(self, request: httpx.Request):
        if request.url.host not in LOCAL_HOSTS:
            attempts.append(str(request.url.copy_with(query=None)))
            raise ExternalRequestError(f"blocked external request to {request.url.host}")
        return original(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded)

    # yt-dlp does its own networking, invisible to the httpx guard above.
    try:
        import yt_dlp
    except ImportError:
        pass
    else:
        def no_ytdlp(self, url, *args, **kwargs):
            attempts.append(f"yt-dlp {url}")
            raise ExternalRequestError(f"blocked yt-dlp request for {url}")

        monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", no_ytdlp)
    yield
    if attempts:
        hosts = sorted({a.split()[0] if a.startswith("yt-dlp") else httpx.URL(a).host
                        for a in attempts})
        pytest.fail(
            f"this test tried to contact {', '.join(hosts)} ({len(attempts)} requests, e.g. "
            f"{attempts[0]}). Tests must not reach external services: point the Config's "
            "instances, sponsorblock_url, dearrow_url and youtube_url at http://127.0.0.1:9, "
            "and set use_ytdlp=False.",
            pytrace=False)
