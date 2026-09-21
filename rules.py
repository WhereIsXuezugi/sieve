"""A small declarative rule engine.

The obvious candidates here are JSONLogic, CEL and Open Policy Agent. All three
were considered and rejected for this particular job:

* OPA is a separate Go daemon plus a policy language — enormous for evaluating
  a dozen comparisons against a dict on a Raspberry Pi.
* cel-python pulls in protobuf and an ANTLR-generated parser.
* json-logic-py is genuinely small, but its operator set is generic (it has no
  notion of "duration between 15 and 60 minutes") and it evaluates arbitrary
  nested arithmetic we do not want in a config file that can be imported from a
  stranger's shared profile.

So this is ~120 lines implementing a closed set of operators over a flat field
namespace. No eval, no arbitrary recursion into user data, and the rule tree is
JSON that round-trips through the UI builder unchanged. The syntax is
deliberately JSONLogic-shaped so that swapping in the library later is a
drop-in if you ever want its full operator set.

    {"all": [
        {"field": "education", "op": ">", "value": 70},
        {"field": "brainrot", "op": "<", "value": 20},
        {"field": "duration_min", "op": "between", "value": [15, 60]},
        {"not": {"field": "title", "op": "contains", "value": "reaction"}}
    ]}
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

MAX_DEPTH = 8

FIELDS: dict[str, str] = {
    # scores
    "education": "number", "entertainment": "number", "stimulation": "number",
    "brainrot": "number", "clickbait": "number", "info_density": "number",
    "technical_depth": "number", "production": "number", "ai_generated": "number",
    "nsfw": "number", "music": "number", "profanity": "number",
    # metadata
    "duration": "number", "duration_min": "number", "views": "number",
    "likes": "number", "like_ratio": "number", "subs": "number",
    "age_days": "number", "published": "number",
    "title": "text", "description": "text", "author": "text", "author_id": "text",
    "genre": "text", "keywords": "list", "topics": "list", "language": "text",
    # booleans
    "is_live": "bool", "is_upcoming": "bool", "is_short": "bool",
    "family_safe": "bool", "watched": "bool", "subscribed": "bool",
    "has_transcript": "bool", "has_chapters": "bool",
    # community data
    "sponsor_ratio": "number", "filler_ratio": "number", "selfpromo_ratio": "number",
    "exclusive_access": "bool", "dearrow_retitled": "bool",
    # channel policy
    "channel_priority": "number", "channel_affinity": "number",
    "channel_quality": "number", "channel_consistency": "number",
    "channel_allowed": "bool", "channel_blocked": "bool",
    # ranking
    "source": "text", "score": "number",
}

OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">": lambda a, b: _num(a) > _num(b),
    ">=": lambda a, b: _num(a) >= _num(b),
    "<": lambda a, b: _num(a) < _num(b),
    "<=": lambda a, b: _num(a) <= _num(b),
    "==": lambda a, b: _eq(a, b),
    "!=": lambda a, b: not _eq(a, b),
    "between": lambda a, b: _num(b[0]) <= _num(a) <= _num(b[1]),
    "contains": lambda a, b: str(b).lower() in _text(a),
    "not_contains": lambda a, b: str(b).lower() not in _text(a),
    "in": lambda a, b: _eq_any(a, b),
    "not_in": lambda a, b: not _eq_any(a, b),
    "matches": lambda a, b: bool(re.search(str(b), _text(a), re.I)),
    "is_true": lambda a, b: bool(a),
    "is_false": lambda a, b: not bool(a),
    "any_of": lambda a, b: bool(set(_list(a)) & {str(x).lower() for x in _list(b)}),
    "none_of": lambda a, b: not (set(_list(a)) & {str(x).lower() for x in _list(b)}),
}


class RuleError(ValueError):
    pass


def validate(node: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise RuleError("rule nested too deeply")
    if not isinstance(node, dict):
        raise RuleError(f"expected an object, got {type(node).__name__}")
    for combinator in ("all", "any", "none"):
        if combinator in node:
            children = node[combinator]
            if not isinstance(children, list):
                raise RuleError(f"'{combinator}' takes a list of rules")
            for child in children:
                validate(child, depth + 1)
            return
    if "not" in node:
        validate(node["not"], depth + 1)
        return
    field = node.get("field")
    op = node.get("op")
    if field not in FIELDS:
        raise RuleError(f"unknown field {field!r}")
    if op not in OPERATORS:
        raise RuleError(f"unknown operator {op!r}")
    if op == "between":
        value = node.get("value")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise RuleError("'between' needs a [low, high] pair")
    if op == "matches":
        try:
            re.compile(str(node.get("value", "")))
        except re.error as exc:
            raise RuleError(f"bad regular expression: {exc}") from None


def evaluate(node: Any, record: Mapping[str, Any], depth: int = 0) -> bool:
    if depth > MAX_DEPTH or not isinstance(node, dict):
        return True
    if "all" in node:
        return all(evaluate(child, record, depth + 1) for child in node["all"])
    if "any" in node:
        children = node["any"]
        return True if not children else any(evaluate(c, record, depth + 1) for c in children)
    if "none" in node:
        return not any(evaluate(child, record, depth + 1) for child in node["none"])
    if "not" in node:
        return not evaluate(node["not"], record, depth + 1)

    field = node.get("field")
    op = node.get("op")
    handler = OPERATORS.get(op)
    if handler is None or field not in FIELDS:
        return True
    if field not in record:
        # We have no data for this field on this video. Silently dropping it
        # would empty the homepage with no visible cause, which is exactly the
        # failure mode this project exists to avoid, so abstain instead.
        return True
    try:
        return bool(handler(record.get(field), node.get("value")))
    except (TypeError, ValueError, IndexError, KeyError):
        return True


def describe(node: Any, depth: int = 0) -> str:
    """Render a rule tree as one line of English for the debugger."""
    if not isinstance(node, dict) or depth > MAX_DEPTH:
        return ""
    for combinator, joiner in (("all", " and "), ("any", " or ")):
        if combinator in node:
            parts = [describe(c, depth + 1) for c in node[combinator]]
            parts = [p for p in parts if p]
            if not parts:
                return ""
            body = joiner.join(parts)
            return f"({body})" if depth else body
    if "none" in node:
        parts = [describe(c, depth + 1) for c in node["none"]]
        return "none of: " + ", ".join(p for p in parts if p)
    if "not" in node:
        return "not " + describe(node["not"], depth + 1)
    field = node.get("field", "?")
    op = node.get("op", "?")
    value = node.get("value")
    if op == "between" and isinstance(value, (list, tuple)):
        return f"{field} between {value[0]} and {value[1]}"
    if op in {"is_true", "is_false"}:
        return f"{field} is {'true' if op == 'is_true' else 'false'}"
    return f"{field} {op} {value}"


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if value is None:
        return 0.0
    return float(value)


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value).lower()
    return str(value or "").lower()


def _list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).lower() for v in value]
    if value is None:
        return []
    return [str(value).lower()]


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return str(a).lower() == str(b).lower()


def _eq_any(a: Any, b: Any) -> bool:
    return any(_eq(a, item) for item in (b if isinstance(b, (list, tuple)) else [b]))
