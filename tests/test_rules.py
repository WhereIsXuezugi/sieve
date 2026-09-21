import pytest

from sieve import rules

RECORD = {
    "education": 82.0,
    "brainrot": 12.0,
    "duration_min": 34.0,
    "views": 12000,
    "title": "Writing a page allocator from scratch",
    "topics": ["kernel", "allocator", "systems"],
    "is_short": False,
    "channel_priority": 3,
    "sponsor_ratio": 0.04,
}


@pytest.mark.parametrize("expr,expected", [
    ({"field": "education", "op": ">", "value": 70}, True),
    ({"field": "education", "op": "<", "value": 70}, False),
    ({"field": "duration_min", "op": "between", "value": [15, 60]}, True),
    ({"field": "duration_min", "op": "between", "value": [60, 90]}, False),
    ({"field": "title", "op": "contains", "value": "ALLOCATOR"}, True),
    ({"field": "title", "op": "not_contains", "value": "reaction"}, True),
    ({"field": "topics", "op": "any_of", "value": ["kernel", "cooking"]}, True),
    ({"field": "topics", "op": "none_of", "value": ["cooking"]}, True),
    ({"field": "is_short", "op": "is_false"}, True),
    ({"field": "title", "op": "matches", "value": "page .*scratch"}, True),
    ({"field": "channel_priority", "op": ">=", "value": 3}, True),
])
def test_operators(expr, expected):
    assert rules.evaluate(expr, RECORD) is expected


def test_combinators():
    assert rules.evaluate({"all": [
        {"field": "education", "op": ">", "value": 70},
        {"field": "brainrot", "op": "<", "value": 20},
    ]}, RECORD)
    assert not rules.evaluate({"all": [
        {"field": "education", "op": ">", "value": 70},
        {"field": "brainrot", "op": ">", "value": 20},
    ]}, RECORD)
    assert rules.evaluate({"any": [
        {"field": "brainrot", "op": ">", "value": 90},
        {"field": "education", "op": ">", "value": 70},
    ]}, RECORD)
    assert rules.evaluate({"none": [{"field": "is_short", "op": "is_true"}]}, RECORD)
    assert rules.evaluate({"not": {"field": "education", "op": "<", "value": 10}}, RECORD)


def test_empty_rule_keeps_everything():
    assert rules.evaluate({"all": []}, RECORD)
    assert rules.evaluate({"any": []}, RECORD)


def test_validate_rejects_unknown_field():
    with pytest.raises(rules.RuleError):
        rules.validate({"field": "os.system", "op": ">", "value": 1})


def test_validate_rejects_unknown_operator():
    with pytest.raises(rules.RuleError):
        rules.validate({"field": "education", "op": "exec", "value": 1})


def test_validate_rejects_bad_regex_and_between():
    with pytest.raises(rules.RuleError):
        rules.validate({"field": "title", "op": "matches", "value": "[("})
    with pytest.raises(rules.RuleError):
        rules.validate({"field": "views", "op": "between", "value": 5})


def test_validate_rejects_deep_nesting():
    node = {"field": "education", "op": ">", "value": 1}
    for _ in range(12):
        node = {"not": node}
    with pytest.raises(rules.RuleError):
        rules.validate(node)


def test_missing_field_does_not_reject_the_video():
    """An unknown value must never silently filter everything out."""
    assert rules.evaluate({"field": "language", "op": "==", "value": "en"}, {})


def test_type_confusion_is_survivable():
    assert rules.evaluate({"field": "views", "op": ">", "value": "banana"}, RECORD)


def test_describe_reads_as_english():
    text = rules.describe({"all": [
        {"field": "education", "op": ">", "value": 70},
        {"field": "duration_min", "op": "between", "value": [15, 60]},
    ]})
    assert "education > 70" in text
    assert "between 15 and 60" in text
