# Changelog

Notable changes, newest first. This project follows
[semantic versioning](https://semver.org) once it reaches 1.0; until then, minor
versions may change behaviour.

## Unreleased

### Added

- A complete JSON API: every feature in the web app now has an endpoint, and
  `tests/test_parity.py` fails if the two surfaces diverge. Reference in
  `docs/api.md`, interactive docs at `/api/docs`
- Playlist import in the web app, by id or by pasting any URL with `list=`,
  with refresh, remove and use-as-homepage
- Blocked title terms, which the ranking always read but nothing could set,
  now managed from the debugger and `/api/blocklist`
- Health check on the debugger page, including a check of upstream services
- Channel notes, filter exemption, a Forget button, and channel list
  import/export on the Channels page
- Saved profiles can be deleted, and profiles imported from a file
- Maintenance buttons: score, rescore everything, prune, thumbnail scan
- `sieve doctor` reports which config file was loaded, and the data directory

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

- **The documented config location was never read.** The loader now searches
  `--config`, `$SIEVE_CONFIG`, `./sieve.toml`, `~/.config/sieve/config.toml`,
  then `/etc/sieve/config.toml`, as documented
- **The docs gave the wrong port.** Standardised on 8377, the real default
- The default data directory was `./data`, relative to wherever `sieve` ran, so
  two working directories meant two databases. It is now `~/.local/share/sieve`
- `~` in `data_dir` created a directory literally named `~`
- Five documented defaults disagreed with the code; `tests/test_docs.py` now
  holds the configuration reference to `Config`
- The debugger's Rederive button reported success and did nothing
- Deleting a built-in mood did not stick; the next merge brought it back
- Channels marked exempt skipped every filter, resurfacing already-watched
  videos and ignoring blocked terms. Exemption now skips only quality and shape
  filters
- `/api/hide` accepted any kind, including `channel`, which nothing reads
- Server error messages were replaced by "failed" in the UI, and a rejected
  upload reported "imported undefined"
- Building a `Config` by hand and calling `create_app` failed because only
  `Config.load` created the data directory
- One demo thumbnail colour was a five-digit hex value

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
