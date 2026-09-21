# Contributing

Bug reports, rule cookbook entries, shared profiles and documentation fixes are
all welcome. So is disagreement about the design decisions, provided it comes
with a reason.

## Getting set up

```bash
git clone https://github.com/yourname/sieve && cd sieve
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

ruff check .
pytest -q
```

`sieve demo` builds a synthetic catalogue with no network access, so you can
work on ranking, scoring and the interface without an Invidious instance. Use a
throwaway data directory:

```bash
SIEVE_DATA_DIR=/tmp/sieve-dev sieve demo
SIEVE_DATA_DIR=/tmp/sieve-dev sieve serve
```

## The two constraints

Almost every design argument in this project comes back to one of these.

**It runs on a single consumer machine.** No GPU on the request path, no second
daemon, no build step. A feature that has to touch every candidate is the
expensive case and needs a good answer for what it costs per video. The homepage
render is pure SQLite and should stay that way.

**It explains itself.** Nothing may affect the ranking without saying so. This
is why scores are linear models rather than an ensemble, why manual and derived
channel signals live in separate columns, and why every rejection is recorded
with a reason. A change that improves ranking quality but moves the reasoning
somewhere the debugger cannot reach is a change that loses the point.

## Things this project believes

You do not have to agree, but a PR that contradicts one of these should say why.

- **Prefer wiring up a mature tool to writing a worse version of it.** DeArrow
  and SponsorBlock exist because humans already voted on those questions.
- **But read the tool's shape, not its category.** Several suggestions in the
  original brief are services designed for a fleet; the
  [README table](README.md#reused-and-replaced) records what was replaced and
  why, and that table is open to argument.
- **A filter that cannot see its data abstains.** No subscriber count means a
  subscriber floor does not apply. Silently emptying the homepage is worse than
  showing one video too many.
- **Untrusted input gets whitelisted, not validated.** Model output and imported
  profiles go through `profiles.sanitise_settings`. Unknown keys are dropped
  rather than rejected loudly, because a stranger's profile with one unfamiliar
  key should still work.
- **Everything reachable from the web app is reachable from the API**, and
  the other way round. `tests/test_parity.py` fails if a route is added to one
  surface without being declared against the other; if something genuinely
  belongs on one side only, it goes in `ONE_SIDED` with the reason. Put the
  logic in `actions.py` and call it from both, so they cannot drift.

## Tests

Every behavioural change needs a test, and the test should fail without the
change. A few kinds are especially welcome:

- **Regression tests that name the bug.** `test_a_rule_expression_is_replaced_not_merged`
  exists because merging rule trees produced a hybrid the evaluator silently
  misread. The docstring says so.
- **Tests that hold an invariant.** `test_every_score_survives_the_load_path`
  exists because a hand-written column list dropped a new score and the symptom
  was a setting that did nothing.
- **Stubs that model the real thing.** The community-API stub answers only for
  the hash prefix it was asked about, because a stub that returns everything
  hides batching bugs.

External services are never contacted from tests.

## Style

`ruff check .` is the whole style guide; the rule set is pinned in
`pyproject.toml` so a ruff upgrade cannot turn CI red on code nobody touched.

Beyond that: comments explain *why*, not *what*. The interesting comments in
this codebase are the ones recording a decision — why SQLite and not Postgres,
why the profanity denominator has a floor, why `s.*` and not a column list.
Those are the ones worth writing.

## Adding a content score

1. Add the feature to `extract_features` in `scoring.py`
2. Add the model — a bias and named weights — to `MODELS`
3. Add human-readable text for each feature to `LABELS`
4. Add the column to `schema.sql` **and** to `MIGRATIONS` in `db.py`
5. Add the key to `SCORE_KEYS` in `config.py`
6. Bump `SCORER_VERSION` so existing rows get rescored

Steps 4 and 6 are the ones people forget, and both fail quietly.

## Adding a user-facing feature

1. Put the operation in `actions.py` (or the domain module it belongs to), so
   there is exactly one implementation
2. Expose it in `api.py` with a `summary=` — `test_docs.py` rejects endpoints
   that only have an auto-generated one
3. Add the web control, calling the API endpoint
4. Declare the pair in `API_TO_WEB` in `tests/test_parity.py`, and give it a
   call in `CALLS`
5. Add the endpoint to `docs/api.md`; `test_docs.py` fails until you do

## Adding a rule field

Register it in `FIELDS` in `rules.py` and populate it in `_rule_record` in
`ranking.py`. A field registered but never populated abstains on every video,
which looks like the rule doing nothing.

## Commits and pull requests

Present tense, and say what changed rather than what you did: "record the
rejection reason for language filters", not "fixed stuff". Small PRs get read;
large ones get read eventually.

If your change is a judgement call — a different weight, a different default, a
different trade — the PR description is the right place to make the argument.
Those are the interesting ones.
