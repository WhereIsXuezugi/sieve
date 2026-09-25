"""autosave.js, tested in Node.

The Controls page has no Save button: every control turns itself into a
settings patch. If that conversion is wrong — a decimal rounded away, a
checkbox group sent as a single boolean — a setting silently saves as the wrong
value. These run the real file's exported functions under Node, so the logic
the browser runs is the logic under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "sieve" / "static" / "autosave.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = r"""
const a = require(process.argv[1]);
const control = (o) => ({
  type: o.type, value: o.value ?? '', checked: !!o.checked, name: o.name || 'x',
  dataset: o.dataset || {}, getAttribute: (k) => (k === 'step' ? (o.step ?? null) : null),
});
const out = {
  rangeInt: a.valueOf(control({type: 'range', value: '36'})),
  rangeDecimal: a.valueOf(control({type: 'range', value: '1.2', step: '0.1'})),
  numberRatio: a.valueOf(control({type: 'number', value: '0.015', step: '0.001'})),
  numberEmpty: a.valueOf(control({type: 'number', value: ''})),
  numberJunk: a.valueOf(control({type: 'number', value: 'abc'})) === undefined,
  checkbox: a.valueOf(control({type: 'checkbox', checked: true})),
  group: a.valueOf(control({type: 'checkbox', value: 'piped', checked: true}), [
    control({type: 'checkbox', value: 'invidious', checked: true}),
    control({type: 'checkbox', value: 'piped', checked: true}),
    control({type: 'checkbox', value: 'youtube', checked: false}),
  ]),
  groupEmpty: a.valueOf(control({type: 'checkbox', value: 'a'}), [
    control({type: 'checkbox', value: 'a'}), control({type: 'checkbox', value: 'b'})]),
  radio: a.valueOf(control({type: 'radio', value: 'youtube', checked: true}), [
    control({type: 'radio', value: 'auto'}), control({type: 'radio', value: 'youtube', checked: true})]),
  csv: a.valueOf(control({type: 'text', value: ' en, de ;ja,, ', dataset: {type: 'csv'}})),
  text: a.valueOf(control({type: 'text', value: 'https://yewtu.be'})),
  patch: a.patchFor('targets.education.enabled', true),
  path: a.getPath({targets: {education: {target: 80}}}, 'targets.education.target'),
  missing: a.getPath({novelty: 60}, 'filters.nonsense') === undefined,
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result():
    completed = subprocess.run(
        ["node", "-e", HARNESS, str(SCRIPT)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(completed.stdout)


def test_integers_stay_integers(result):
    assert result["rangeInt"] == 36


def test_decimals_are_kept_when_the_step_allows_them(result):
    """A weight of 1.2 rounded to 1 would save the wrong value silently."""
    assert result["rangeDecimal"] == 1.2
    assert result["numberRatio"] == 0.015


def test_an_empty_number_means_zero_and_junk_is_refused(result):
    assert result["numberEmpty"] == 0
    assert result["numberJunk"] is True


def test_a_checkbox_group_is_a_list(result):
    assert result["checkbox"] is True
    assert result["group"] == ["invidious", "piped"]
    assert result["groupEmpty"] == [], "unticking every box must save an empty list, not false"


def test_radios_send_the_chosen_value(result):
    assert result["radio"] == "youtube"


def test_csv_fields_become_lists(result):
    assert result["csv"] == ["en", "de", "ja"]


def test_plain_text_is_sent_as_is(result):
    assert result["text"] == "https://yewtu.be"


def test_names_become_nested_patches(result):
    assert result["patch"] == {"targets": {"education": {"enabled": True}}}
    assert result["path"] == 80
    assert result["missing"] is True
