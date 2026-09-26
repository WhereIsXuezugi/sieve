"""Where videos open.

Sieve does not play video; it hands you to something that does. This module is
the one place that knows how to build that link, for:

    invidious   any Invidious instance              {base}/watch?v=ID
    piped       any Piped instance                  {base}/watch?v=ID
    youtube     youtube.com                         https://www.youtube.com/watch?v=ID
    nocookie    Sieve's own player page, embedding  /play/ID
                YouTube's privacy-enhanced player
    freetube    the FreeTube desktop app            freetube://https://www.youtube.com/watch?v=ID
    custom      your own template, with {id} and optionally {t}

Every link on every page goes through `/open/{id}` rather than straight to the
provider. That keeps the choice in one setting instead of baked into rendered
HTML, lets Sieve record that you opened a video — as an *open*, not as a watch
with a made-up completion figure — and means a video that cannot be played
anywhere, like the synthetic demo catalogue, gets an explanation instead of a
provider's 404 page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .config import Config

# A real YouTube video id is exactly eleven characters from this alphabet.
# Anything else — including the demo catalogue's "demo0003" ids — cannot be
# opened by any provider, and saying so beats sending you to a 404.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

# A known-good video for the "test this provider" link: the first video ever
# uploaded to YouTube. Short, stable, and harmless.
TEST_VIDEO = "jNQXAC9IVRw"

# Schemes a custom template may use. http(s) for web players; the rest are
# desktop apps that register a protocol handler. Never javascript:, data:,
# file: or vbscript: — a custom template can arrive inside a shared profile.
SAFE_SCHEMES = {"http", "https", "freetube", "mpv", "vlc", "iina", "potplayer", "stremio"}
SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):", re.I)

DEFAULT_PIPED = "https://piped.video"


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    note: str
    app: bool = False          # opens a desktop app rather than a web page
    configurable: bool = False  # has an instance URL or template to set


PROVIDERS: dict[str, Provider] = {
    "invidious": Provider("invidious", "Invidious",
                          "Your Invidious instance, or any public one.", configurable=True),
    "piped": Provider("piped", "Piped",
                      "A Piped instance. The official one is often busy; pick any from the Piped instance list.",
                      configurable=True),
    "youtube": Provider("youtube", "YouTube", "youtube.com itself, with its tracking and recommendations."),
    # Kept under its old key so existing settings and shared profiles still
    # work. It used to send you to a bare youtube-nocookie.com/embed URL, which
    # YouTube refuses to play on its own ("Error 153") because Sieve strips the
    # referrer; the embed now lives inside a Sieve page that sends one.
    "nocookie": Provider("nocookie", "Sieve player",
                         "YouTube's privacy-enhanced player inside a Sieve page: skips sponsor "
                         "segments, resumes where you left off, and records how much you watched."),
    "freetube": Provider("freetube", "FreeTube",
                         "Opens in the FreeTube desktop app, if it is installed.", app=True),
    "custom": Provider("custom", "Custom",
                       "Your own URL template with {id}, and optionally {t} for the start time.",
                       configurable=True),
}


class ProviderError(ValueError):
    pass


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def clean_base(url: str) -> str:
    """Validate an instance URL and reduce it to scheme://host[:port][/path]."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderError("an instance address must be an http or https URL, like https://yewtu.be")
    path = parsed.path
    # People paste a watch link; keep only the instance.
    for suffix in ("/watch", "/feed/popular", "/feed/trending"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")


def clean_template(template: str) -> str:
    template = (template or "").strip()
    if not template:
        return ""
    if "{id}" not in template:
        raise ProviderError("a custom template needs {id} where the video id goes")
    match = SCHEME.match(template)
    if not match or match.group(1).lower() not in SAFE_SCHEMES:
        allowed = ", ".join(sorted(SAFE_SCHEMES))
        raise ProviderError(f"a custom template must start with one of: {allowed}")
    stray = set(re.findall(r"\{([^}]*)\}", template)) - {"id", "t"}
    if stray:
        raise ProviderError(f"unknown placeholder {{{sorted(stray)[0]}}}; only {{id}} and {{t}} exist")
    return template


def sanitise(raw: dict[str, Any]) -> dict[str, Any]:
    """Whitelist a playback settings patch. Used for forms, the API and imports."""
    out: dict[str, Any] = {}
    if "provider" in raw:
        if raw["provider"] not in PROVIDERS:
            raise ProviderError(f"unknown provider {raw['provider']!r}")
        out["provider"] = raw["provider"]
    if "invidious_url" in raw:
        out["invidious_url"] = clean_base(str(raw["invidious_url"]))
    if "piped_url" in raw:
        out["piped_url"] = clean_base(str(raw["piped_url"]))
    if "custom_url" in raw:
        out["custom_url"] = clean_template(str(raw["custom_url"]))
    if "menu" in raw:
        menu = raw["menu"] if isinstance(raw["menu"], list) else []
        out["menu"] = [k for k in PROVIDERS if k in menu]
    for key in ("resume", "new_tab"):
        if key in raw:
            out[key] = bool(raw[key])
    return out


# --------------------------------------------------------------------------
# Building links
# --------------------------------------------------------------------------


def playable(video_id: str) -> bool:
    return bool(VIDEO_ID.match(video_id or ""))


def invidious_base(settings: dict, cfg: Config) -> str:
    """The configured instance, else whatever `watch_base` in the config names."""
    chosen = (settings.get("playback") or {}).get("invidious_url")
    if chosen:
        return chosen
    base = cfg.watch_base.split("/watch", 1)[0]
    return base.rstrip("/")


def available(settings: dict, cfg: Config) -> list[str]:
    """Providers that are usable as configured. Custom needs a template."""
    playback = settings.get("playback") or {}
    return [k for k in PROVIDERS if k != "custom" or playback.get("custom_url")]


def default_provider(settings: dict, cfg: Config) -> str:
    """The provider links use. Until you choose one, Invidious if your config
    names a real instance, and otherwise Sieve's own player: the shipped
    default guesses an Invidious on this machine's port 3000, and sending
    someone who never set one up to a connection error — every video, from
    their very first homepage — is the worst possible first impression."""
    playback = settings.get("playback") or {}
    chosen = playback.get("provider") or ""
    if chosen in available(settings, cfg):
        return chosen
    if _is_local_guess(settings, cfg):
        return "nocookie"
    return "invidious"


def _is_local_guess(settings: dict, cfg: Config) -> bool:
    host = urlparse(invidious_base(settings, cfg)).hostname or ""
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def url_for(provider: str, video_id: str, settings: dict, cfg: Config,
            start: int | None = None) -> str:
    if not playable(video_id):
        raise ProviderError(f"{video_id!r} is not a YouTube video id, so no provider can open it")
    playback = settings.get("playback") or {}
    t = max(0, int(start)) if start else 0

    if provider == "invidious":
        url = f"{invidious_base(settings, cfg)}/watch?v={video_id}"
        return f"{url}&t={t}" if t else url
    if provider == "piped":
        url = f"{playback.get('piped_url') or DEFAULT_PIPED}/watch?v={video_id}"
        return f"{url}&t={t}" if t else url
    if provider == "youtube":
        url = f"https://www.youtube.com/watch?v={video_id}"
        return f"{url}&t={t}s" if t else url
    if provider == "nocookie":
        url = f"/play/{video_id}"   # relative: it is a page of this Sieve
        return f"{url}?t={t}" if t else url
    if provider == "freetube":
        url = f"freetube://https://www.youtube.com/watch?v={video_id}"
        return f"{url}&t={t}" if t else url
    if provider == "custom":
        template = playback.get("custom_url")
        if not template:
            raise ProviderError("the custom provider has no template yet; set one in Controls, Playback")
        return template.replace("{id}", video_id).replace("{t}", str(t))
    raise ProviderError(f"unknown provider {provider!r}")


def resume_at(progress: float, duration: int) -> int | None:
    """Where to pick up a partly watched video, backing off a few seconds so
    the sentence you stopped in is not cut in half."""
    if not duration or not 0.05 <= progress <= 0.95:
        return None
    return max(0, int(progress * duration) - 5)


def links(video_id: str, settings: dict, cfg: Config,
          start: int | None = None) -> list[dict[str, Any]]:
    """Every usable provider's link for one video, default first."""
    if not playable(video_id):
        return []
    default = default_provider(settings, cfg)
    out = []
    for key in available(settings, cfg):
        provider = PROVIDERS[key]
        out.append({
            "provider": key, "label": provider.label, "app": provider.app,
            "default": key == default,
            "url": url_for(key, video_id, settings, cfg, start),
        })
    out.sort(key=lambda item: not item["default"])
    return out


def menu(settings: dict, cfg: Config) -> list[Provider]:
    """The providers to offer under "open in", in a stable order."""
    playback = settings.get("playback") or {}
    wanted = playback.get("menu") or list(PROVIDERS)
    usable = set(available(settings, cfg))
    return [PROVIDERS[k] for k in PROVIDERS if k in wanted and k in usable]


def looks_unconfigured(settings: dict, cfg: Config, stored: dict) -> bool:
    """True while no provider has been chosen and the config's Invidious is
    the shipped local guess. Videos then play in Sieve's own player (see
    default_provider), and the homepage says so once, in case you do run
    Invidious and would rather use it."""
    if (stored.get("playback") or {}).get("provider"):
        return False
    return _is_local_guess(settings, cfg)
