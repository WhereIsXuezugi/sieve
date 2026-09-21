"""Settings resolution.

The merge order is defaults <- stored <- active mood, and getting it wrong is
invisible until a page renders something nobody chose.
"""

from sieve import rules
from sieve.config import DEFAULT_SETTINGS, deep_merge, default_settings, resolve_settings


def test_defaults_are_never_mutated():
    first = default_settings()
    first["filters"]["max_brainrot"] = 1
    assert default_settings()["filters"]["max_brainrot"] != 1
    assert DEFAULT_SETTINGS["filters"]["max_brainrot"] != 1


def test_stored_settings_win_over_defaults():
    resolved = resolve_settings({"novelty": 90, "filters": {"hide_shorts": False}})
    assert resolved["novelty"] == 90
    assert resolved["filters"]["hide_shorts"] is False
    # untouched keys still come from the defaults
    assert resolved["filters"]["shorts_seconds"] == DEFAULT_SETTINGS["filters"]["shorts_seconds"]


def test_active_mood_overlays_the_stored_settings():
    resolved = resolve_settings({
        "novelty": 10,
        "active_mood": "Study",
        "moods": {"Study": {"novelty": 80, "filters": {"max_brainrot": 5}}},
    })
    assert resolved["novelty"] == 80
    assert resolved["filters"]["max_brainrot"] == 5


def test_an_unknown_mood_is_ignored_rather_than_fatal():
    assert resolve_settings({"active_mood": "Nonexistent"})["novelty"] == \
        DEFAULT_SETTINGS["novelty"]


def test_a_rule_expression_is_replaced_not_merged():
    """Regression: merging a new rule into an old one produced a tree with two
    competing combinators, and the evaluator silently obeyed whichever it
    checked first — so saving a rule appeared to do nothing."""
    stored = {"rules": {"enabled": True, "expr": {"all": [
        {"field": "education", "op": ">", "value": 70},
    ]}}}
    patched = deep_merge(stored, {"rules": {"expr": {"any": [
        {"field": "brainrot", "op": "<", "value": 10},
    ]}}})
    expr = patched["rules"]["expr"]
    assert set(expr) == {"any"}, f"expected only the new combinator, got {set(expr)}"
    assert rules.evaluate(expr, {"brainrot": 5})
    assert not rules.evaluate(expr, {"brainrot": 50})


def test_replacing_a_rule_with_an_empty_one_actually_clears_it():
    stored = {"rules": {"enabled": True, "expr": {"none": [
        {"field": "music", "op": ">", "value": 50},
    ]}}}
    cleared = deep_merge(stored, {"rules": {"expr": {"all": []}}})
    assert cleared["rules"]["expr"] == {"all": []}
    assert rules.evaluate(cleared["rules"]["expr"], {"music": 99})


def test_ordinary_nested_settings_still_merge():
    merged = deep_merge({"filters": {"a": 1, "b": 2}}, {"filters": {"b": 3}})
    assert merged["filters"] == {"a": 1, "b": 3}
