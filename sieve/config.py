"""Runtime configuration and the default user settings tree.

Two separate things live here:

* ``Config``  – deployment concerns (ports, database path, Invidious instances,
  LLM endpoint).  Read from a TOML file and environment variables at boot.
* ``DEFAULT_SETTINGS`` – the user's recommendation profile.  Stored in SQLite,
  editable from the UI, exportable as JSON.  This is the thing the whole
  application is really about, so it is deliberately declarative and flat
  enough to diff.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Deployment config
# --------------------------------------------------------------------------


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: default_data_dir())
    host: str = "127.0.0.1"
    port: int = 8377

    # Invidious instances, tried in order. The first one that answers wins and
    # stays sticky until it fails. Point this at your own instance.
    instances: list[str] = field(
        default_factory=lambda: ["http://127.0.0.1:3000", "https://yewtu.be"]
    )
    request_timeout: float = 8.0
    cache_ttl: int = 3600          # seconds, for Invidious API responses
    catalog_ttl: int = 7 * 86400   # how long a video row stays fresh

    # Where the "watch" links point. Usually your own Invidious front end.
    watch_base: str = "http://127.0.0.1:3000/watch?v="

    # LLM: "none" | "ollama" | "openai" | "anthropic"
    llm_provider: str = "none"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_api_key: str = ""

    # Embeddings: "hashed" (pure-python TF-IDF, no model, default) or "ollama".
    embed_provider: str = "hashed"
    embed_model: str = "nomic-embed-text"
    # Defaults to llm_base_url when empty, since usually the same Ollama serves both.
    embed_base_url: str = ""

    # Transcripts come from Invidious' caption endpoint, not Whisper. Set this
    # to false on very small hosts; scoring degrades gracefully to metadata.
    use_transcripts: bool = True
    transcript_max_chars: int = 20000

    # DeArrow + SponsorBlock (both from sponsor.ajay.app). Requests go out by
    # 4-char sha256 prefix, so the server cannot tell which video you wanted.
    # Point these at a self-hosted mirror if you run one.
    sponsorblock_url: str = "https://sponsor.ajay.app"
    dearrow_url: str = "https://sponsor.ajay.app"
    dearrow_thumbnail_url: str = "https://dearrow-thumb.ajay.app"
    community_ttl: int = 3 * 86400

    # Offline hosting: skip the Google Fonts link and use system faces.
    webfonts: bool = True

    # Background scoring worker
    workers: int = 2
    score_batch: int = 24

    # Which config file was loaded, if any. Set by `load`; reported by doctor.
    source: str = ""

    def __post_init__(self) -> None:
        # Accept a str from TOML, the environment or a caller, and expand `~`:
        # without this, `data_dir = "~/.local/share/sieve"` creates a directory
        # literally named "~" in whatever folder you happened to be in.
        self.data_dir = Path(self.data_dir).expanduser()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sieve.db"

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> Config:
        cfg = cls()
        found = config_path(path)
        if path and found is None:
            raise FileNotFoundError(f"no config file at {path}")
        if found is not None:
            with found.open("rb") as fh:
                _apply(cfg, tomllib.load(fh))
        cfg.source = str(found) if found else ""
        _apply_env(cfg)
        cfg.data_dir = Path(cfg.data_dir).expanduser()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg


def default_data_dir() -> Path:
    """`$XDG_DATA_HOME/sieve`, usually `~/.local/share/sieve`.

    Deliberately not relative to the working directory: a relative default
    means running `sieve serve` from two folders uses two databases, which
    looks exactly like losing your history.
    """
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "sieve"


def config_path(explicit: str | os.PathLike | None = None) -> Path | None:
    """Find the config file. First match wins:

    1. the path passed with `--config`
    2. `$SIEVE_CONFIG`
    3. `./sieve.toml`
    4. `$XDG_CONFIG_HOME/sieve/config.toml`, usually `~/.config/sieve/config.toml`
    5. `/etc/sieve/config.toml`
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p.resolve() if p.is_file() else None
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    for candidate in (
        os.environ.get("SIEVE_CONFIG"),
        "sieve.toml",
        f"{xdg}/sieve/config.toml",
        "/etc/sieve/config.toml",
    ):
        if candidate:
            p = Path(candidate).expanduser()
            if p.is_file():
                # Absolute, so `sieve doctor` can say *which* sieve.toml.
                return p.resolve()
    return None


def _apply(cfg: Config, raw: dict[str, Any]) -> None:
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            for sub, subvalue in value.items():
                flat[f"{key}_{sub}"] = subvalue
        else:
            flat[key] = value
    for key, value in flat.items():
        if hasattr(cfg, key) and key != "source":
            setattr(cfg, key, value)


def _apply_env(cfg: Config) -> None:
    for key in vars(cfg):
        if key == "source":
            continue
        env = os.environ.get("SIEVE_" + key.upper())
        if env is None:
            continue
        current = getattr(cfg, key)
        if isinstance(current, bool):
            setattr(cfg, key, env.strip().lower() in {"1", "true", "yes", "on"})
        elif isinstance(current, int):
            setattr(cfg, key, int(env))
        elif isinstance(current, float):
            setattr(cfg, key, float(env))
        elif isinstance(current, list):
            setattr(cfg, key, [s.strip() for s in env.split(",") if s.strip()])
        else:
            setattr(cfg, key, env)


# --------------------------------------------------------------------------
# User settings (the recommendation profile)
# --------------------------------------------------------------------------

SCORE_KEYS = [
    "education",
    "entertainment",
    "stimulation",
    "brainrot",
    "clickbait",
    "info_density",
    "technical_depth",
    "production",
    "ai_generated",
    "nsfw",
    "music",
    "profanity",
]

SOURCE_KEYS = ["subscriptions", "history", "playlists", "discovery", "interests", "niche"]

BUCKETS = ["education", "entertainment", "hobby", "meme", "music", "other"]

DEFAULT_SETTINGS: dict[str, Any] = {
    # ---- homepage layout -------------------------------------------------
    "homepage": {
        "count": 36,
        "columns": 3,
        "density": "comfortable",      # comfortable | compact | list
        "mode": "blend",               # blend | playlist | continue | subscriptions
        "playlist_id": "",             # used when mode == "playlist"
        "show_explanations": True,
        "show_scores": True,
        "continue_first": True,
        "shuffle": False,              # ignore ranking, draw uniformly at random
        "refresh_seed": "daily",       # daily | session | fixed
    },
    # ---- where candidates come from, as relative weights -----------------
    "sources": {
        "subscriptions": 40,
        "history": 20,
        "playlists": 15,
        "discovery": 10,
        "interests": 10,
        "niche": 5,
    },
    # ---- how strongly each ranking component counts ----------------------
    "weights": {
        "source": 1.0,
        "interest": 1.2,
        "preference": 1.0,
        "learned": 0.8,
        "freshness": 0.4,
        "novelty": 0.5,
        "continue": 1.5,
        "channel_quality": 0.4,
        "channel_priority": 1.0,
        "penalty": 1.0,
    },
    # 0 = only familiar topics, 100 = actively seek unrelated ones
    "novelty": 30,
    # ---- soft targets: "I want videos that look like this" ---------------
    # enabled=false means the axis is ignored entirely.
    "targets": {
        key: {"enabled": False, "target": 70, "weight": 1.0} for key in SCORE_KEYS
    },
    # ---- hard filters ----------------------------------------------------
    "filters": {
        "hide_shorts": True,
        "shorts_seconds": 180,
        "min_duration": 0,
        "max_duration": 0,             # 0 = no ceiling
        "max_brainrot": 100,
        "max_clickbait": 100,
        "max_nsfw": 25,
        "max_music": 100,
        "max_ai_generated": 100,
        "max_profanity": 100,
        "min_education": 0,
        "min_info_density": 0,
        "min_views": 0,
        "max_views": 0,
        "min_subs": 0,
        "max_subs": 0,                 # 0 = no ceiling; set low for "niche only"
        "max_age_days": 0,
        "min_like_ratio": 0.0,
        "hide_live": False,
        "hide_upcoming": True,
        "hide_watched": True,
        "languages": [],               # empty = any
    },
    # ---- channel policy --------------------------------------------------
    # Manual priority lives in the channel_prefs table (-5..+5). These knobs
    # decide how much that priority and the derived watch-time affinity move
    # the ranking.
    "channels": {
        "whitelist_only": False,       # show nothing but channels marked "allow"
        "manual_strength": 0.18,       # score added per priority point
        "affinity_strength": 0.5,      # weight of derived watch-time affinity
        "affinity_enabled": True,
        "affinity_half_life_days": 45, # recency decay on watch time
        "blocked_hidden": True,        # false = keep them, just bury them
    },
    # ---- DeArrow: crowd-sourced, non-clickbait titles --------------------
    "dearrow": {
        "enabled": False,
        "replace_titles": True,
        "replace_thumbnails": True,
        "show_original": True,         # keep the original title as a tooltip
        "min_votes": 0,
        "score_from_titles": True,     # a corrected title is a clickbait signal
    },
    # ---- SponsorBlock: segment data, used for skipping and for ranking ---
    "sponsorblock": {
        "enabled": False,
        "categories": ["sponsor", "selfpromo", "interaction", "intro", "outro",
                       "preview", "music_offtopic", "filler"],
        "skip": ["sponsor", "selfpromo", "interaction", "music_offtopic"],
        "max_sponsor_ratio": 1.0,      # 0.15 = hide videos >15% sponsor read
        "max_filler_ratio": 1.0,
        "hide_exclusive_access": False,
        "score_penalty": 0.0,          # how much sponsor load lowers the rank
    },
    # ---- JSONLogic-style rules, evaluated after the simple filters -------
    "rules": {"enabled": False, "expr": {"all": []}},
    # ---- homepage composition quotas -------------------------------------
    "budget": {
        "enabled": False,
        "quotas": {"education": 50, "entertainment": 20, "hobby": 20, "meme": 10},
        "daily_caps": {"meme": 3, "music": 5},
    },
    # ---- diversity / rabbit-hole guard -----------------------------------
    "diversity": {
        "enabled": True,
        "max_per_channel": 3,
        "mmr_lambda": 0.75,            # 1.0 = pure relevance, 0 = pure diversity
        "warn_below": 0.35,            # diversity index that triggers the prompt
    },
    # ---- named overrides, deep-merged over everything above --------------
    "moods": {
        "Study": {
            "sources": {"subscriptions": 25, "history": 15, "playlists": 35, "interests": 20, "discovery": 5, "niche": 0},
            "targets": {
                "education": {"enabled": True, "target": 90, "weight": 1.6},
                "info_density": {"enabled": True, "target": 85, "weight": 1.2},
            },
            "filters": {"hide_shorts": True, "max_brainrot": 25, "max_music": 40, "min_duration": 600},
            "novelty": 15,
        },
        "Relax": {
            "sources": {"subscriptions": 45, "history": 25, "discovery": 20, "playlists": 10, "interests": 0, "niche": 0},
            "targets": {"entertainment": {"enabled": True, "target": 80, "weight": 1.2}},
            "filters": {"max_brainrot": 70, "max_duration": 2400},
            "novelty": 45,
        },
        "Explore": {
            "sources": {"discovery": 40, "niche": 30, "interests": 20, "subscriptions": 10, "history": 0, "playlists": 0},
            "novelty": 85,
            "diversity": {"max_per_channel": 1, "mmr_lambda": 0.45},
        },
    },
    "active_mood": "",
    # ---- natural-language brief, compiled by the LLM ---------------------
    "brief": {"text": "", "compiled_at": 0, "summary": ""},
    "learning": {"enabled": True, "rate": 0.08, "l2": 0.001},
}


def default_settings() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_SETTINGS)


# Keys whose values are whole documents rather than namespaces of settings.
# Merging into one of these produces a hybrid that means something nobody asked
# for: merging {"any": [...]} into a stored {"all": []} yields an object with
# both keys, and the evaluator picks whichever it checks first. These are
# replaced wholesale instead.
OPAQUE_KEYS = frozenset({"expr"})


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursive dict merge. Patch values win; nested dicts are merged."""
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if key not in OPAQUE_KEYS and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


MOOD_SECTIONS = ("sources", "targets", "filters", "novelty", "weights", "diversity",
                 "budget", "channels")


def diff_settings(base: Any, current: Any, sections: tuple[str, ...]) -> dict:
    """Return only what `current` changes relative to `base`, section by section."""
    out: dict[str, Any] = {}
    for section in sections:
        if section not in current:
            continue
        delta = diff_value(base.get(section), current[section])
        if delta not in (None, {}, []):
            out[section] = delta
    return out


def diff_value(base: Any, current: Any) -> Any:
    if isinstance(base, dict) and isinstance(current, dict):
        nested = {}
        for key, value in current.items():
            delta = diff_value(base.get(key), value)
            if delta is not None:
                nested[key] = delta
        return nested or None
    return None if base == current else current





def drop_deleted_moods(settings: dict) -> dict:
    """Remove moods marked deleted.

    Built-in moods come from DEFAULT_SETTINGS, so deleting one from storage
    would not stick: the next merge would bring it back. Deletion is recorded
    as a `None` marker instead, and removed here after merging.
    """
    moods = settings.get("moods")
    if isinstance(moods, dict):
        settings["moods"] = {k: v for k, v in moods.items() if v is not None}
        if settings.get("active_mood") and settings["active_mood"] not in settings["moods"]:
            settings["active_mood"] = ""
    return settings


def resolve_settings(stored: dict | None) -> dict:
    """Defaults <- stored settings <- active mood."""
    merged = drop_deleted_moods(deep_merge(DEFAULT_SETTINGS, stored or {}))
    mood = merged.get("active_mood") or ""
    if mood and mood in merged.get("moods", {}):
        merged = deep_merge(merged, merged["moods"][mood])
        merged["active_mood"] = mood
    return merged
