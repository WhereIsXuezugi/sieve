"""Shareable recommendation profiles.

A profile is the whole user-owned configuration — settings, interests, channel
policy — as one JSON document. Exporting gives you a file; importing merges
someone else's into yours. This is the "recommendation market" idea from the
brief, minus the market: it is just files, so you can put them in a git repo,
post them in a forum thread, or keep a personal set and swap between them.

Every import goes through the same whitelist as the LLM compiler, because an
imported profile is exactly as untrusted as model output.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import channels as channel_policy
from . import interests as interest_store
from .config import (
    FILL_MODES,
    HOMEPAGE_COUNT,
    PULL_LIMITS,
    SCORE_KEYS,
    SOURCE_KEYS,
    deep_merge,
    default_settings,
)
from .db import Database

FORMAT_VERSION = 1

EXPORTABLE_SECTIONS = [
    "homepage", "sources", "weights", "novelty", "targets", "filters",
    "channels", "dearrow", "sponsorblock", "rules", "budget", "diversity",
    "moods", "active_mood", "brief", "playback", "source", "learning", "compute", "pull",
    "backups", "notify", "ai", "ai_tune",
]


def export_profile(db: Database, name: str = "", *, include_channels: bool = True,
                   include_interests: bool = True, author: str = "") -> dict[str, Any]:
    stored = db.get_setting("settings", {}) or {}
    settings = deep_merge(default_settings(), stored)
    body: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "name": name or "sieve profile",
        "author": author,
        "exported_at": int(time.time()),
        "settings": {key: settings[key] for key in EXPORTABLE_SECTIONS if key in settings},
    }
    if include_interests:
        body["interests"] = [
            {"tag": row["tag"], "weight": row["weight"], "confidence": row["confidence"]}
            for row in db.query(
                "SELECT tag, weight, confidence FROM interests "
                "WHERE pinned = 1 OR origin IN ('manual','llm') OR confidence > 0.3"
            )
        ]
    if include_channels:
        body["channels"] = channel_policy.export_lists(db)
    return body


def import_profile(db: Database, body: dict[str, Any], *, merge: bool = True,
                   apply_channels: bool = True, apply_interests: bool = True) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("profile must be a JSON object")
    if int(body.get("format", 0)) > FORMAT_VERSION:
        raise ValueError("this profile was made by a newer version of Sieve")

    clean = sanitise_settings(body.get("settings") or {})
    current = db.get_setting("settings", {}) or {}
    merged = deep_merge(current, clean) if merge else clean
    db.set_setting("settings", merged)

    applied = {"settings": len(clean), "interests": 0, "channels": 0}

    if apply_interests:
        for item in body.get("interests") or []:
            if not isinstance(item, dict) or not item.get("tag"):
                continue
            try:
                weight = float(item.get("weight", 0.6))
            except (TypeError, ValueError):
                continue
            interest_store.set_interest(
                db, str(item["tag"])[:48], weight,
                origin="imported", confidence=float(item.get("confidence", 0.6) or 0.6),
                pinned=False,
            )
            applied["interests"] += 1

    if apply_channels:
        channels = body.get("channels") or {}
        counts = channel_policy.import_lists(
            db,
            allow=[c["id"] for c in channels.get("allow", []) if isinstance(c, dict) and c.get("id")],
            block=[c["id"] for c in channels.get("block", []) if isinstance(c, dict) and c.get("id")],
            priorities=dict((channels.get("priorities") or {}).items()),
        )
        applied["channels"] = sum(counts.values())

    return applied


def sanitise_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Whitelist an imported settings tree down to keys we recognise."""
    from . import rules as rule_engine

    defaults = default_settings()
    out: dict[str, Any] = {}

    def copy_scalars(section: str, spec: dict[str, type]) -> None:
        source = raw.get(section)
        if not isinstance(source, dict):
            return
        kept = {}
        for key, caster in spec.items():
            if key not in source:
                continue
            try:
                kept[key] = caster(source[key])
            except (TypeError, ValueError):
                continue
        if kept:
            out[section] = kept

    copy_scalars("homepage", {
        "count": _ranged(*HOMEPAGE_COUNT), "columns": _ranged(1, 6),
        "density": _choice("comfortable", "compact", "list"),
        "mode": _choice("blend", "playlist", "continue", "subscriptions"),
        "playlist_id": _text(64),
        "show_explanations": _bool, "show_scores": _bool, "continue_first": _bool,
"refresh_seed": _choice("daily", "session", "fixed"),
        "fill": _choice(*FILL_MODES), "next_episode": _bool,
        "new_every": _ranged(0, 43200),
        "recent_boost": _bool, "recent_strength": _ranged(0, 3, float),
        "recent_half_life": _ranged(1, 24 * 30), "search_mode": _choice("ai", "math"),
    })
    if isinstance(raw.get("ai"), dict):
        from .llm import CONNECTIONS

        ai: dict[str, Any] = {}
        if raw["ai"].get("provider") in ("", *CONNECTIONS):
            ai["provider"] = raw["ai"]["provider"]
        for key in ("model", "base_url"):
            if isinstance(raw["ai"].get(key), str):
                value = raw["ai"][key].strip()[:200]
                if key == "base_url" and value and not value.startswith(("http://", "https://")):
                    continue
                ai[key] = value
        if ai:
            out["ai"] = ai
    copy_scalars("ai_tune", {"enabled": _bool, "videos_per_hour": _ranged(5, 50)})
    copy_scalars("notify", {
        "webhook_url": _url_or_blank, "webhook_style": _choice("ntfy", "json"),
        "push_alerts": _bool, "digest_hours": _ranged(0, 24 * 30),
    })
    copy_scalars("backups", {
        "auto": _bool, "every_hours": _ranged(1, 24 * 30), "keep": _ranged(1, 50),
    })
    if isinstance(raw.get("pull"), dict):
        pull_raw = raw["pull"]
        pull: dict[str, Any] = {}
        for key in ("auto", "starter_channels", "limit_enabled", "limit_manual"):
            if key in pull_raw:
                pull[key] = _bool(pull_raw[key])
        for key, (low, high) in PULL_LIMITS.items():
            if key in pull_raw and _numeric(pull_raw[key]):
                pull[key] = max(low, min(high, int(float(pull_raw[key]))))
        if isinstance(pull_raw.get("topics"), list):
            from .starter import TOPICS

            pull["topics"] = [t for t in pull_raw["topics"] if t in TOPICS]
        if isinstance(pull_raw.get("custom_topics"), list):
            pull["custom_topics"] = [str(t).strip()[:60] for t in pull_raw["custom_topics"]
                                     if str(t).strip()][:12]
        if isinstance(pull_raw.get("region"), str):
            region = pull_raw["region"].strip().upper()[:2]
            if len(region) == 2 and region.isalpha():
                pull["region"] = region
        if pull:
            out["pull"] = pull
    copy_scalars("channels", {
        "whitelist_only": _bool, "manual_strength": _ranged(0, 1, float),
        "affinity_strength": _ranged(0, 2, float), "affinity_enabled": _bool,
        "affinity_half_life_days": _ranged(1, 3650, float), "blocked_hidden": _bool,
    })
    copy_scalars("dearrow", {
        "enabled": _bool, "replace_titles": _bool, "replace_thumbnails": _bool,
        "show_original": _bool, "min_votes": _ranged(0, 100000), "score_from_titles": _bool,
    })
    if isinstance(raw.get("source"), dict):
        from .ytauth import BROWSERS

        source: dict[str, Any] = {}
        if raw["source"].get("backend") in ("auto", "invidious", "youtube"):
            source["backend"] = raw["source"]["backend"]
        if raw["source"].get("cookies_browser") in ("", *BROWSERS):
            source["cookies_browser"] = raw["source"]["cookies_browser"]
        if source:
            out["source"] = source
    if isinstance(raw.get("compute"), dict):
        # A preset expands to its values; changing any single value makes the
        # profile "custom". Every number is clamped: a stranger's profile must
        # not be able to ask for a 10-million-candidate pool.
        from .config import COMPUTE_LIMITS, COMPUTE_PRESETS

        compute: dict[str, Any] = {}
        preset = raw["compute"].get("preset")
        if preset in COMPUTE_PRESETS:
            compute = {"preset": preset, **COMPUTE_PRESETS[preset]}
        for key, (low, high) in COMPUTE_LIMITS.items():
            if key in raw["compute"] and _numeric(raw["compute"][key]):
                compute[key] = max(low, min(high, int(float(raw["compute"][key]))))
                if preset not in COMPUTE_PRESETS:
                    compute["preset"] = "custom"
        for flag in ("transcripts", "adaptive_sync", "auto_captions"):
            if flag in raw["compute"]:
                compute[flag] = _bool(raw["compute"][flag])
            if preset not in COMPUTE_PRESETS:
                compute["preset"] = "custom"
        if compute:
            out["compute"] = compute
    if isinstance(raw.get("learning"), dict) and "enabled" in raw["learning"]:
        out["learning"] = {"enabled": _bool(raw["learning"]["enabled"])}
    if isinstance(raw.get("playback"), dict):
        # A custom template can arrive in a stranger's profile, so it goes
        # through the same scheme whitelist as the Controls page. One bad field
        # drops that field, not the whole section.
        from . import providers

        kept: dict[str, Any] = {}
        for key, value in raw["playback"].items():
            try:
                kept.update(providers.sanitise({key: value}))
            except providers.ProviderError:
                continue
        if kept:
            out["playback"] = kept
    copy_scalars("diversity", {
        "enabled": _bool, "max_per_channel": _ranged(1, 100),
        "mmr_lambda": _ranged(0, 1, float), "warn_below": _ranged(0, 1, float),
    })

    if isinstance(raw.get("sponsorblock"), dict):
        sb = raw["sponsorblock"]
        kept: dict[str, Any] = {}
        for key, caster in (("enabled", _bool), ("max_sponsor_ratio", _ranged(0, 1, float)),
                            ("max_filler_ratio", _ranged(0, 1, float)),
                            ("hide_exclusive_access", _bool),
                            ("score_penalty", _ranged(0, 5, float))):
            if key in sb:
                try:
                    kept[key] = caster(sb[key])
                except (TypeError, ValueError):
                    pass
        from .community import SPONSOR_CATEGORIES
        for key in ("categories", "skip"):
            if isinstance(sb.get(key), list):
                kept[key] = [c for c in sb[key] if c in SPONSOR_CATEGORIES]
        if kept:
            out["sponsorblock"] = kept

    if isinstance(raw.get("sources"), dict):
        sources = {}
        for key in SOURCE_KEYS:
            if key in raw["sources"]:
                try:
                    sources[key] = max(0, min(100, int(float(raw["sources"][key]))))
                except (TypeError, ValueError):
                    continue
        if sources:
            out["sources"] = sources

    if isinstance(raw.get("weights"), dict):
        weights = {}
        for key in defaults["weights"]:
            if key in raw["weights"]:
                try:
                    weights[key] = max(0.0, min(5.0, float(raw["weights"][key])))
                except (TypeError, ValueError):
                    continue
        if weights:
            out["weights"] = weights

    if "novelty" in raw:
        try:
            out["novelty"] = max(0, min(100, int(float(raw["novelty"]))))
        except (TypeError, ValueError):
            pass

    if isinstance(raw.get("targets"), dict):
        targets = {}
        for key, value in raw["targets"].items():
            if key not in SCORE_KEYS or not isinstance(value, dict):
                continue
            # Only the fields actually sent. Filling in defaults here meant that
            # moving one target slider also switched the target off and reset
            # its weight — a patch must not say things it was not told.
            kept: dict[str, Any] = {}
            try:
                if "enabled" in value:
                    kept["enabled"] = _bool(value["enabled"])
                if "target" in value:
                    kept["target"] = max(0, min(100, int(float(value["target"]))))
                if "weight" in value:
                    kept["weight"] = max(0.1, min(3.0, float(value["weight"])))
            except (TypeError, ValueError):
                continue
            if kept:
                targets[key] = kept
        if targets:
            out["targets"] = targets

    if isinstance(raw.get("filters"), dict):
        filters = {}
        for key, reference in defaults["filters"].items():
            if key not in raw["filters"]:
                continue
            value = raw["filters"][key]
            try:
                if isinstance(reference, bool):
                    filters[key] = _bool(value)
                elif isinstance(reference, str):
                    # The only text filter: video_language_mode.
                    if value not in ("prefer", "only"):
                        continue
                    filters[key] = value
                elif isinstance(reference, list):
                    filters[key] = [str(v)[:8] for v in value][:12] if isinstance(value, list) else []
                elif isinstance(reference, float):
                    filters[key] = max(0.0, min(1.0, float(value)))
                elif key.endswith(("_brainrot", "_clickbait", "_nsfw", "_music", "_generated",
                                   "_profanity", "_education", "_density")):
                    filters[key] = max(0, min(100, int(float(value))))   # score cutoffs
                else:
                    filters[key] = max(0, int(float(value)))   # durations, counts: never negative
            except (TypeError, ValueError):
                continue
        if filters:
            out["filters"] = filters

    if isinstance(raw.get("budget"), dict):
        budget: dict[str, Any] = {}
        if "enabled" in raw["budget"]:
            budget["enabled"] = _bool(raw["budget"]["enabled"])
        for key in ("quotas", "daily_caps"):
            if isinstance(raw["budget"].get(key), dict):
                budget[key] = {
                    str(k)[:24]: max(0, int(float(v)))
                    for k, v in raw["budget"][key].items()
                    if _numeric(v)
                }
        if budget:
            out["budget"] = budget

    if isinstance(raw.get("rules"), dict):
        expr = raw["rules"].get("expr")
        if isinstance(expr, dict):
            try:
                rule_engine.validate(expr)
                out["rules"] = {"enabled": _bool(raw["rules"].get("enabled")), "expr": expr}
            except rule_engine.RuleError:
                pass

    if isinstance(raw.get("moods"), dict):
        moods = {}
        for name, patch in list(raw["moods"].items())[:24]:
            if isinstance(patch, dict):
                moods[str(name)[:40]] = sanitise_settings(patch)
        if moods:
            out["moods"] = moods
    if isinstance(raw.get("active_mood"), str):
        out["active_mood"] = raw["active_mood"][:40]

    if isinstance(raw.get("brief"), dict) and isinstance(raw["brief"].get("text"), str):
        out["brief"] = {
            "text": raw["brief"]["text"][:2000],
            "summary": str(raw["brief"].get("summary", ""))[:300],
            "compiled_at": int(raw["brief"].get("compiled_at") or 0),
        }

    return out


def save_profile(db: Database, name: str, body: dict, author: str = "") -> None:
    db.execute(
        "INSERT INTO saved_profiles(name, body, author, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET body=excluded.body, author=excluded.author, "
        "created_at=excluded.created_at",
        (name[:80], json.dumps(body), author[:80], int(time.time())),
    )


def list_profiles(db: Database) -> list[dict]:
    return [
        {"name": r["name"], "author": r["author"], "created_at": r["created_at"]}
        for r in db.query("SELECT name, author, created_at FROM saved_profiles ORDER BY created_at DESC")
    ]


def load_profile(db: Database, name: str) -> dict | None:
    row = db.one("SELECT body FROM saved_profiles WHERE name = ?", (name,))
    return json.loads(row["body"]) if row else None


def _bool(value: Any) -> bool:
    """A JSON boolean, or a string or number meaning one. `bool("false")` is
    True, which is how a client sending strings used to switch things on."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
        raise ValueError(f"not a yes or no: {value!r}")
    return bool(value)


def _ranged(low: float, high: float, kind: type = int):
    """A caster that clamps into [low, high]."""
    def cast(value: Any):
        return kind(max(low, min(high, kind(float(value)))))
    return cast


def _choice(*allowed: str):
    def cast(value: Any) -> str:
        if value not in allowed:
            raise ValueError(f"expected one of {allowed}")
        return value
    return cast


def _url_or_blank(value: Any) -> str:
    value = str(value or "").strip()
    if value and not value.startswith(("http://", "https://")):
        raise ValueError("the webhook must be an http(s) address")
    return value[:500]


def _text(limit: int):
    return lambda value: str(value)[:limit]


def _numeric(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
