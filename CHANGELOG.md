# Changelog

Notable changes, newest first. This project follows
[semantic versioning](https://semver.org) once it reaches 1.0; until then, minor
versions may change behaviour.

## Unreleased

### Added

- Channel policy: manual priority (−5 to +5), allow list, block list, and
  whitelist-only mode
- Derived channel affinity from watch time, weighted by completion and decayed
  on a configurable half-life, kept in a separate column from manual priority
- Channel quality scoring from each channel's own catalogue: education,
  information density, topic consistency, minus clickbait and brainrot
- DeArrow integration for crowd-voted titles and thumbnails, queried by
  four-character hash prefix
- SponsorBlock integration, used both for playback skipping and as a ranking
  signal, likewise by hash prefix
- Profanity score, and filters for subscriber count and caption language
- `embed.py`: pluggable similarity vectors — `hashed` (default), `ollama`,
  `openai`
- `vision.py`: optional thumbnail NSFW scoring behind the `vision` extra
- Mood editor, storing only what differs from the base settings
- Profile import from a URL
- `sieve doctor`, which names the causes of an empty homepage
- Schema migrations applied on open, so upgrading needs no dump and reload

### Fixed

- Saving a rule silently did nothing: `deep_merge` merged the new expression
  into the old one, producing a tree with two combinators, and the evaluator
  obeyed whichever it checked first. Rule expressions are now opaque values
- A new content score reached the database but never the filter, because the
  ranking query listed score columns by hand. It now selects them all, with a
  test holding the invariant
- `/api/channels/recompute` was swallowed by the `{channel_id}` route and
  created a channel preference named "recompute"
- `sieve stats | head` raised `BrokenPipeError`

## 0.1.0

First working version: source weights, per-video explanations, content scores,
the rule engine, the interest graph, the online learner, composition budgets,
rabbit-hole detection, the brief compiler, analytics and shareable profiles.
