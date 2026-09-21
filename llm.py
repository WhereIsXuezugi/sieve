"""Natural-language brief compiler.

The user types something like

    "Hard engineering projects with low production value made by people who
     clearly know what they are doing. No podcasts, nothing over an hour."

and the LLM turns it into a *settings patch* — interests, filters, score
targets, source weights — which is then validated, shown as a diff, and applied
only on confirmation. The model never touches the ranking at request time: it
compiles preferences once, and the deterministic engine does the work. That
keeps the homepage fast, keeps recommendations reproducible, and means a local
7B model on a consumer GPU is entirely sufficient.

Providers: ollama (default, local), openai-compatible, anthropic, or none.
With `none`, `heuristic_compile` extracts what it can from keywords so the text
box still does something useful on a machine with no model at all.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from . import textutil as T
from .config import SCORE_KEYS, SOURCE_KEYS, Config

log = logging.getLogger("sieve.llm")

SYSTEM_PROMPT = """You convert a person's description of what they want to watch into a JSON \
configuration for a YouTube recommendation engine. Reply with JSON only, no prose, no code fences.

Schema:
{
  "summary": "one sentence paraphrase of what they asked for",
  "interests": [{"tag": "lowercase single word or short phrase", "weight": -1.0 to 1.0}],
  "targets": {"<score>": {"enabled": true, "target": 0-100, "weight": 0.5-2.0}},
  "filters": {"hide_shorts": bool, "min_duration": seconds, "max_duration": seconds,
              "max_brainrot": 0-100, "max_clickbait": 0-100, "max_music": 0-100,
              "max_ai_generated": 0-100, "min_education": 0-100, "max_subs": int},
  "sources": {"<source>": 0-100},
  "rules": {"all": [{"field": "...", "op": "...", "value": ...}]}
}

Valid scores: education, entertainment, stimulation, brainrot, clickbait, info_density,
technical_depth, production, ai_generated, nsfw, music.
Valid sources: subscriptions, history, playlists, discovery, interests, niche.
Rule fields: education, entertainment, brainrot, clickbait, info_density, technical_depth,
production, ai_generated, music, nsfw, duration_min, views, subs, age_days, like_ratio,
title, description, author, keywords, topics, is_short, sponsor_ratio, channel_affinity.
Rule operators: >, >=, <, <=, ==, !=, between, contains, not_contains, in, not_in, matches,
is_true, is_false, any_of, none_of.

Rules:
- Only include keys the person actually implied. Omit the rest.
- "low production value" means production target LOW, not high. Read the intent carefully.
- Negative preferences become negative interest weights or filters, never positive ones.
- Prefer a filter over a rule when a plain filter can express it."""


class LLMError(RuntimeError):
    pass


def compile_brief(cfg: Config, text: str, context: dict | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        return {"summary": "", "patch": {}, "provider": "none"}
    if cfg.llm_provider == "none":
        return heuristic_compile(text)
    raw = _call(cfg, text, context or {})
    parsed = _parse_json(raw)
    if parsed is None:
        log.warning("LLM returned unparseable output, falling back to keywords")
        result = heuristic_compile(text)
        result["warning"] = "The model did not return valid JSON. Fell back to keyword extraction."
        return result
    patch = sanitise(parsed)
    return {
        "summary": str(parsed.get("summary", ""))[:300],
        "patch": patch,
        "provider": cfg.llm_provider,
        "raw": raw[:4000],
    }


def _call(cfg: Config, text: str, context: dict) -> str:
    hint = ""
    if context.get("top_interests"):
        hint = f"\n\nFor reference, they currently watch: {', '.join(context['top_interests'][:12])}."
    user = f"{text}{hint}"
    timeout = max(30.0, cfg.request_timeout * 4)

    if cfg.llm_provider == "ollama":
        response = httpx.post(
            f"{cfg.llm_base_url.rstrip('/')}/api/chat",
            json={
                "model": cfg.llm_model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "")

    if cfg.llm_provider == "openai":
        response = httpx.post(
            f"{cfg.llm_base_url.rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json={
                "model": cfg.llm_model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    if cfg.llm_provider == "anthropic":
        response = httpx.post(
            f"{cfg.llm_base_url.rstrip('/')}/v1/messages",
            headers={"x-api-key": cfg.llm_api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": cfg.llm_model,
                "max_tokens": 1500,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=timeout,
        )
        response.raise_for_status()
        blocks = response.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    raise LLMError(f"unknown provider {cfg.llm_provider!r}")


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.M).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

ALLOWED_FILTERS = {
    "hide_shorts": bool, "hide_live": bool, "hide_watched": bool,
    "min_duration": int, "max_duration": int, "shorts_seconds": int,
    "max_brainrot": int, "max_clickbait": int, "max_nsfw": int, "max_music": int,
    "max_ai_generated": int, "min_education": int, "min_info_density": int,
    "min_views": int, "max_views": int, "min_subs": int, "max_subs": int,
    "max_age_days": int, "min_like_ratio": float,
}


def sanitise(parsed: dict) -> dict:
    """Whitelist everything. A compiled brief is a settings patch, and settings
    patches are also importable from other people, so nothing unrecognised is
    ever allowed through."""
    from . import rules as rule_engine

    patch: dict[str, Any] = {}

    interests = []
    for item in parsed.get("interests") or []:
        if isinstance(item, str):
            interests.append({"tag": item.strip().lower()[:48], "weight": 0.8})
        elif isinstance(item, dict) and item.get("tag"):
            try:
                weight = max(-1.0, min(1.0, float(item.get("weight", 0.8))))
            except (TypeError, ValueError):
                weight = 0.8
            interests.append({"tag": str(item["tag"]).strip().lower()[:48], "weight": weight})
    if interests:
        patch["interests"] = interests[:40]

    targets = {}
    for key, value in (parsed.get("targets") or {}).items():
        if key not in SCORE_KEYS or not isinstance(value, dict):
            continue
        try:
            targets[key] = {
                "enabled": bool(value.get("enabled", True)),
                "target": max(0, min(100, int(float(value.get("target", 70))))),
                "weight": max(0.1, min(3.0, float(value.get("weight", 1.0)))),
            }
        except (TypeError, ValueError):
            continue
    if targets:
        patch["targets"] = targets

    filters = {}
    for key, caster in ALLOWED_FILTERS.items():
        if key not in (parsed.get("filters") or {}):
            continue
        try:
            filters[key] = caster(parsed["filters"][key])
        except (TypeError, ValueError):
            continue
    if filters:
        patch["filters"] = filters

    sources = {}
    for key, value in (parsed.get("sources") or {}).items():
        if key in SOURCE_KEYS:
            try:
                sources[key] = max(0, min(100, int(float(value))))
            except (TypeError, ValueError):
                continue
    if sources:
        patch["sources"] = sources

    expr = parsed.get("rules")
    if isinstance(expr, dict) and expr:
        try:
            rule_engine.validate(expr)
            patch["rules"] = {"enabled": True, "expr": expr}
        except rule_engine.RuleError as exc:
            log.info("dropping invalid compiled rule: %s", exc)

    return patch


# --------------------------------------------------------------------------
# No-LLM fallback
# --------------------------------------------------------------------------

NEGATION = re.compile(r"\b(no|not|without|avoid|hide|exclude|less|fewer|skip)\b\s+([a-z ]{3,30})", re.I)
DURATION_MIN = re.compile(r"(?:over|longer than|at least|more than)\s+(\d+)\s*(min|minute|hour|hr)", re.I)
DURATION_MAX = re.compile(r"(?:under|shorter than|less than|no more than|max)\s+(\d+)\s*(min|minute|hour|hr)", re.I)

PHRASE_RULES = [
    (r"\b(no|hide|avoid)\b[^.]*\bshorts?\b", {"filters": {"hide_shorts": True}}),
    (r"\b(no|hide|avoid|without)\b[^.]*\bmusic\b", {"filters": {"max_music": 35}}),
    (r"\bbrainrot\b|\bbrain rot\b", {"filters": {"max_brainrot": 20}}),
    (r"\bclickbait\b", {"filters": {"max_clickbait": 30}}),
    (r"\bai[- ]?(generated|slop|voice)\b", {"filters": {"max_ai_generated": 35}}),
    (r"\b(educational|lectures?|academic|university|course)\b",
     {"targets": {"education": {"enabled": True, "target": 85, "weight": 1.4}}}),
    (r"\b(technical|engineering|low[- ]level|deep dive)\b",
     {"targets": {"technical_depth": {"enabled": True, "target": 85, "weight": 1.3}}}),
    (r"\blow production\b|\bunpolished\b|\bamateur\b|\braw\b",
     {"targets": {"production": {"enabled": True, "target": 25, "weight": 1.0}}}),
    (r"\b(small|tiny|niche|underrated|obscure)\s+(channels?|creators?)\b",
     {"sources": {"niche": 40, "discovery": 10}, "filters": {"max_views": 50000}}),
    (r"\b(dense|information dense|no fluff|to the point)\b",
     {"targets": {"info_density": {"enabled": True, "target": 85, "weight": 1.3}}}),
    (r"\b(relax|casual|entertaining|fun)\b",
     {"targets": {"entertainment": {"enabled": True, "target": 75, "weight": 1.0}}}),
]


def heuristic_compile(text: str) -> dict:
    """Keyword extraction, used when no model is configured.

    Much dumber than an LLM, but deterministic, instant, and good enough that
    the text box is never dead weight on a machine without a GPU.
    """
    from .config import deep_merge

    lowered = text.lower()
    patch: dict[str, Any] = {}
    applied: list[str] = []

    for pattern, fragment in PHRASE_RULES:
        if re.search(pattern, lowered):
            patch = deep_merge(patch, fragment)
            applied.append(pattern.strip("\\b"))

    match = DURATION_MIN.search(lowered)
    if match:
        seconds = int(match.group(1)) * (3600 if match.group(2).startswith(("hour", "hr")) else 60)
        patch = deep_merge(patch, {"filters": {"min_duration": seconds}})
    match = DURATION_MAX.search(lowered)
    if match:
        seconds = int(match.group(1)) * (3600 if match.group(2).startswith(("hour", "hr")) else 60)
        patch = deep_merge(patch, {"filters": {"max_duration": seconds}})

    negatives = set()
    for _, phrase in NEGATION.findall(lowered):
        negatives.update(T.content_tokens(phrase)[:2])

    interests = []
    for token in T.content_tokens(text):
        if token in negatives or len(token) < 4:
            continue
        interests.append({"tag": token, "weight": 0.7})
    for token in negatives:
        if len(token) >= 4:
            interests.append({"tag": token, "weight": -0.7})
    seen: set[str] = set()
    deduped = []
    for item in interests:
        if item["tag"] in seen:
            continue
        seen.add(item["tag"])
        deduped.append(item)
    if deduped:
        patch["interests"] = deduped[:24]

    return {
        "summary": f"Keyword match on {len(deduped)} terms"
                   + (f" and {len(applied)} phrase rules" if applied else ""),
        "patch": patch,
        "provider": "heuristic",
        "note": "No language model is configured, so this used keyword matching. "
                "Set llm_provider in your config for better results.",
    }


def critique(cfg: Config, settings: dict, diagnostics: dict, samples: list[str]) -> str:
    """Answer "why am I getting these videos?" in prose."""
    summary = {
        "sources": settings["sources"],
        "novelty": settings["novelty"],
        "active_targets": {k: v for k, v in settings["targets"].items() if v.get("enabled")},
        "filters": {k: v for k, v in settings["filters"].items() if v not in (0, False, 100, [], "")},
        "diversity": diagnostics.get("diversity"),
        "dominant_topic": diagnostics.get("dominant_topic"),
        "top_rejections": [r["reason"] for r in diagnostics.get("rejected", [])[:5]],
        "sample_titles": samples[:12],
    }
    if cfg.llm_provider == "none":
        return _offline_critique(summary)
    prompt = (
        "Here is the current state of a recommendation engine. In at most 120 words, "
        "explain plainly why the person is seeing what they are seeing, and name the one "
        "setting most worth changing.\n\n" + json.dumps(summary, indent=2)
    )
    try:
        original = SYSTEM_PROMPT
        globals()["SYSTEM_PROMPT"] = "You are a concise, technical assistant. Reply in plain prose."
        try:
            return _call(cfg, prompt, {}).strip()
        finally:
            globals()["SYSTEM_PROMPT"] = original
    except Exception as exc:
        log.warning("critique failed: %s", exc)
        return _offline_critique(summary)


def _offline_critique(summary: dict) -> str:
    sources = summary["sources"]
    top = sorted(sources.items(), key=lambda kv: -kv[1])[:2]
    lines = [
        "Most of this page comes from " + " and ".join(f"{k} ({v}%)" for k, v in top) + ".",
    ]
    if summary["active_targets"]:
        names = ", ".join(summary["active_targets"])
        lines.append(f"You have score targets active on {names}, which pushes the ranking toward them.")
    if summary.get("dominant_topic"):
        lines.append(
            f"The topic “{summary['dominant_topic']}” is the most common thread, and overall "
            f"diversity is {round((summary.get('diversity') or 0) * 100)}%."
        )
    if summary["top_rejections"]:
        lines.append("Most videos were dropped because: " + "; ".join(summary["top_rejections"][:3]) + ".")
    return " ".join(lines)


def timestamp() -> int:
    return int(time.time())
