## What this changes

<!-- One or two sentences. -->

## Why

<!-- The problem, not the patch. If it closes an issue, link it here. -->

## Checklist

- [ ] `ruff check .` passes
- [ ] `pytest -q` passes
- [ ] New behaviour has a test, and the test fails without the change
- [ ] No new required dependency, or the PR explains why it earns its place
- [ ] Anything a user can set is reachable from the UI **and** the CLI
- [ ] Anything that changes ranking is visible in the debugger

## If this adds a signal or a filter

- [ ] It abstains when it has no data, rather than hiding videos silently
- [ ] Its contribution is reported in the explanation, not folded into a total
