# Architecture

## Why this is not a fork

Sieve is called a fork in casual conversation and is not one. It runs alongside
an Invidious instance and reads from its public JSON API.

Forking Invidious would mean maintaining roughly a hundred thousand lines of
Crystal that you did not write, merging upstream forever, and doing it in a
language with a small ecosystem for the things this project actually needs. None
of the recommendation logic needs to live inside Invidious. It needs three
things from it — the catalogue, the player, the proxy — and all three are
already exposed over a stable API.

The cost is real: Sieve cannot change the player page, which is why
[watch-progress reporting](player.md) is opt-in. That is a small price for never
having to rebase a fork.

```
        browser
           │
      reverse proxy
        ╱       ╲
    Sieve      Invidious
   :8377         :3000
      │             │
   sieve.db    the catalogue,
   (SQLite)    player and proxy
      │
  sponsor.ajay.app, by hash prefix
```

## The request path touches no network

A homepage render is pure SQLite. `ingest.py` keeps the catalogue warm on a
background thread; `ranking.py` reads from what is already there.

This is why the page stays fast on modest hardware, and why the homepage still
renders when your instance is down — you get the catalogue as of the last
successful sync instead of an error.

## The pipeline

```
gather_candidates   pull from each enabled source, tagged with its origin
      ↓
_gate               channel listings, then filters, then the rule tree
      ↓             every rejection recorded with a reason
_blend              weighted sum of named components, per video
      ↓
explain             positive components → percentages summing to exactly 100
      ↓
_arrange            MMR diversification, per-channel caps, budget quotas
      ↓
_diagnose           topic entropy, rabbit-hole warning, rejection tally
```

Rejections are recorded rather than discarded, because "why am I *not* seeing X"
is usually the more useful question and no conventional feed will answer it.

## Explainability is a constraint, not a feature

Several design choices exist only to keep the system able to account for itself.

**Scores are linear models.** Every content score is a logistic over a weighted
sum of named features. That is not a shortcut — it means each feature's exact
contribution is free to compute, so the debugger can say "82 on education, 31
points of it from citation links in the description" with no second model. A
gradient-boosted ensemble would score marginally better and cost an
approximation layer to explain itself.

**The learner is logistic regression.** The training set is one person's
feedback — hundreds of rows arriving one at a time. It fits in a few hundred
bytes, trains on the request thread, and hands back exact per-feature
contributions. `learner.py` documents the seam if you ever want to swap it.

**Explanation percentages use largest-remainder apportionment.** Rounding each
share independently lands on 99 or 101 often enough to show as a gap in the
stacked bar. A bar that claims to be a complete explanation should look like
one.

**Manual and derived signals never merge.** Channel priority is what you set;
channel affinity is what watch time implies; channel quality is what the
channel's own catalogue looks like. Three columns, three contributions, reported
separately.

## Storage

SQLite in WAL mode. One file, no daemon, readable with `sqlite3` — which matters
for a tool whose premise is that you can see what it believes about you.

```sql
SELECT tag, weight, confidence, origin FROM interests ORDER BY confidence DESC LIMIT 20;
SELECT reason, COUNT(*) FROM impressions GROUP BY reason;
```

Schema changes are applied on open from an explicit list in `db.py`, so
upgrading in place needs no dump and reload.

<details>
<summary>Tables</summary>

| Table | Holds |
|---|---|
| `videos` | catalogue metadata and transcripts |
| `scores` | one scorecard per video, with its feature signals and vector |
| `channels` | derived per-channel stats: affinity, quality, consistency |
| `channel_prefs` | what you set: priority, allow/block, notes |
| `history` | watch events with completion |
| `feedback` | explicit more/less/deeper/shorter signals |
| `interests` | the interest graph, with origin and confidence |
| `impressions` | what was shown, in which slot, and why |
| `dearrow`, `sponsor_segments`, `hash_prefix_log` | community data caches |
| `settings`, `saved_profiles`, `model` | configuration and the trained ranker |

</details>

## One implementation, two surfaces

Every user-facing operation exists once, in `actions.py` or its domain module.
The web form and the API endpoint that do the same thing both call it, so one
cannot quietly gain validation the other lacks.

`tests/test_parity.py` enforces the rest. It reads the app's OpenAPI schema,
and fails if any route on either surface lacks a declared counterpart on the
other, or if a declared web control is missing from its page. The schema rather
than `app.routes` is deliberate: recent FastAPI stores an included router as a
single lazy entry there, and a parity check that cannot see half the routes
passes without checking anything.

## Untrusted input

Three things reach Sieve from outside and all three go through the same
whitelist in `profiles.sanitise_settings`:

- language-model output, compiling a written brief into settings
- imported profiles, from a file or a URL
- rule expressions inside either of the above

Unknown keys are dropped, values are clamped, and an invalid rule is discarded
rather than stored. A profile from a stranger cannot make Sieve do anything the
Controls page cannot.

## Module map

| Module | Responsibility |
|---|---|
| `app.py` | page routes, forms, template context |
| `api.py` | the JSON API; see [api.md](api.md) |
| `actions.py` | operations shared by the web app and the API, so neither can drift |
| `present.py` | how a recommendation is titled and illustrated, for both surfaces |
| `doctor.py` | the health check behind `sieve doctor`, `/api/doctor` and the debugger |
| `ranking.py` | the pipeline above |
| `scoring.py` | twelve content scores, each linear and self-explaining |
| `channels.py` | priority, allow/block, affinity, quality |
| `community.py` | DeArrow and SponsorBlock clients |
| `rules.py` | the rule engine |
| `learner.py` | online logistic regression |
| `interests.py` | the editable interest graph |
| `llm.py` | brief compiler and critic, with a no-model fallback |
| `profiles.py` | export, import, and the whitelist |
| `embed.py` | pluggable similarity vectors |
| `vision.py` | optional thumbnail NSFW scoring |
| `analytics.py` | dashboard aggregations |
| `ingest.py` | sync, scoring worker, importers |
| `invidious.py` | API client with failover and caching |
| `db.py` | SQLite layer and migrations |
| `textutil.py` | tokenizer, lexicons, sparse vectors |

## Footprint

On a Raspberry Pi 4 with roughly 20,000 videos catalogued:

| | |
|---|---|
| Idle memory | ~90 MB |
| Homepage render | 40–120 ms |
| Scoring | ~35 videos/second without transcripts, ~4 with |
| Database | ~180 MB including transcripts |
| Runtime dependencies | 5 |
| GPU | none |
| Background daemons | none |
