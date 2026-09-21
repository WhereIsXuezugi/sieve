# Configuration

Sieve has two kinds of settings, and the split matters:

| | Deployment config | User settings |
|---|---|---|
| Lives in | `config.toml` or `SIEVE_*` environment variables | the database |
| Changed by | editing a file and restarting | the Controls page, the CLI, or the API |
| Covers | where to find things: instances, ports, model endpoints | what you want to watch |
| Travels | with the machine | with your [profile](#profiles) |

If you are looking for "hide Shorts" or "maximum brainrot", those are user
settings and they are not in the TOML file.

## Contents

- [Deployment config](#deployment-config)
- [Where the config file lives](#where-the-config-file-lives)
- [User settings](#user-settings)
  - [Homepage](#homepage)
  - [Sources and weights](#sources-and-weights)
  - [Score targets](#score-targets)
  - [Filters](#filters)
  - [Channels](#channels)
  - [DeArrow](#dearrow)
  - [SponsorBlock](#sponsorblock)
  - [Composition and diversity](#composition-and-diversity)
  - [Moods](#moods)
- [Profiles](#profiles)

---

## Deployment config

Every key can be set in TOML or as an environment variable with a `SIEVE_`
prefix, uppercased: `instances` becomes `SIEVE_INSTANCES`.

### Core

| Key | Default | Notes |
|---|---|---|
| `data_dir` | `$XDG_DATA_HOME/sieve`, usually `~/.local/share/sieve` | Holds `sieve.db` and nothing else. `~` is expanded |
| `host` | `127.0.0.1` | See the [exposure warning](install.md#a-note-on-exposure) before changing |
| `port` | `8377` | |
| `instances` | `["http://127.0.0.1:3000"]` | Failover list, tried in order |
| `watch_base` | `http://127.0.0.1:3000/watch?v=` | Where Watch links point |
| `request_timeout` | `8.0` | Seconds, per upstream request |
| `cache_ttl` | `3600` | Seconds to cache search and listing responses |
| `catalog_ttl` | `604800` | Seconds a video's metadata stays fresh (a week) |

### Scoring

| Key | Default | Notes |
|---|---|---|
| `use_transcripts` | `true` | Fetch caption tracks. Turn off on a very small host; scoring falls back to metadata with lower confidence |
| `transcript_max_chars` | `20000` | |
| `score_batch` | `24` | Videos per background pass |
| `workers` | `2` | |

### DeArrow and SponsorBlock

These only say *which server to ask*. Whether to use them at all is a user
setting, because it changes what you see.

| Key | Default |
|---|---|
| `sponsorblock_url` | `https://sponsor.ajay.app` |
| `dearrow_url` | `https://sponsor.ajay.app` |
| `dearrow_thumbnail_url` | `https://dearrow-thumb.ajay.app` |
| `community_ttl` | `259200` (three days) |

Point them at a self-hosted mirror if you run one.

> [!NOTE]
> Both are queried by four-character `sha256(videoID)` prefix. The server
> receives a bucket containing a few hundred videos and cannot tell which one
> you wanted. This is also the cheaper path, since one request covers many
> videos.

### Similarity vectors

| Key | Default | Notes |
|---|---|---|
| `embed_provider` | `hashed` | `hashed`, `ollama` or `openai` |
| `embed_model` | `nomic-embed-text` | Ignored when provider is `hashed` |
| `embed_base_url` | (empty) | Defaults to `llm_base_url` |

`hashed` is pure Python with no model and no download. It is what the
performance numbers in the README assume, and it is good enough for
"more like this" on a personal library.

Switching to a real embedding model costs roughly eight times the database size
and requires `sieve score --all` afterwards, because vectors from one backend
are meaningless to another. Sieve refuses to compare them rather than returning
a plausible wrong number.

> [!TIP]
> `sieve doctor` tells you if `embed_provider` is set to something that is not
> answering — otherwise scoring silently falls back to hashed vectors and you
> get a database of two incompatible kinds.

### Language model

Used to compile a written brief into settings, and to answer "why am I getting
these videos". **The ranking never calls it.** A small local model is plenty.

| Key | Default | Notes |
|---|---|---|
| `llm_provider` | `none` | `none`, `ollama`, `openai` or `anthropic` |
| `llm_model` | `qwen2.5:7b-instruct` | |
| `llm_base_url` | `http://127.0.0.1:11434` | |
| `llm_api_key` | (empty) | Not needed for Ollama |

With `none`, the brief box falls back to keyword matching. Noticeably dumber,
but instant and deterministic, and the text box is never dead weight.

### Interface

| Key | Default | Notes |
|---|---|---|
| `webfonts` | `true` | Loads IBM Plex from Google Fonts. Set `false` to stay fully offline; the UI falls back to system fonts |

---

## Where the config file lives

Searched in this order, first hit wins:

1. `--config /path/to/config.toml` — an error if that file does not exist
2. `$SIEVE_CONFIG`
3. `./sieve.toml`, in the directory you run `sieve` from
4. `$XDG_CONFIG_HOME/sieve/config.toml`, usually `~/.config/sieve/config.toml`
5. `/etc/sieve/config.toml`

Environment variables override the file, so a container can ship a config and
still be reconfigured at run time.

> [!TIP]
> If you edited a config and nothing changed, run `sieve doctor --quick`. Its
> `config.file` field shows which file was actually loaded — or that none was.

---

## User settings

These live in the database and are edited from the Controls page. They are
listed here because reading them as a set is easier than clicking through, and
because the API and imported profiles use the same names.

### Homepage

| Setting | Default | Notes |
|---|---|---|
| `count` | 36 | Videos on the page |
| `columns` | 3 | |
| `density` | `comfortable` | `comfortable`, `compact` or `list` |
| `mode` | `blend` | `blend`, `playlist`, `continue` or `subscriptions` |
| `playlist_id` | — | Used when `mode` is `playlist` |
| `refresh_seed` | `daily` | `daily`, `session` or `fixed`. How often the order is allowed to change |
| `show_explanations` | true | The why-bar under each video |
| `show_scores` | true | |
| `continue_first` | true | Unfinished videos at the top |
| `shuffle` | false | Ignore ranking entirely and draw at random |

`mode: playlist` with a single playlist is the "100% Watch Later, nothing else"
homepage. `mode: continue` is "only things I started".

### Sources and weights

Six sources, weighted relative to each other. The Controls page shows each one's
actual percentage beside the slider, because a weight that does not read as a
share is a weight nobody can reason about.

| Source | Draws from |
|---|---|
| `subscriptions` | Recent videos from channels you follow |
| `history` | Similarity to what you have watched |
| `playlists` | Anything in a playlist you imported |
| `discovery` | Popular videos outside your usual topics |
| `interests` | Topic overlap with your interest graph |
| `niche` | Small channels in your topics |

Separately, `weights` control how much each *component* moves the ranking once
candidates are gathered: `source`, `interest`, `preference`, `learned`,
`freshness`, `novelty`, `continue`, `channel_priority`, `channel_quality`,
`penalty`.

`novelty` (0–100) is the odd one out: it is both a slider of its own and a
weight. It rewards distance from your interest centroid, so high values
deliberately surface things you have no history with.

### Score targets

Twelve axes. Each can be off, or aimed at a value with a weight:

`education`, `entertainment`, `stimulation`, `brainrot`, `clickbait`,
`info_density`, `technical_depth`, `production`, `ai_generated`, `nsfw`,
`music`, `profanity`.

A target is an *aim*, not a threshold: setting `production` to 25 asks for
rough, unpolished video, and will rank a 90 down as hard as a 5.

> [!IMPORTANT]
> A disabled target is not a neutral target — it is ignored entirely and costs
> nothing. Leaving every axis enabled at 50 is not the same as leaving them off.

### Filters

Hard cuts. Anything failing one never reaches the ranking, and the debugger
reports how many each removed and why.

| Setting | Notes |
|---|---|
| `hide_shorts`, `shorts_seconds` | Default 180 seconds |
| `hide_live`, `hide_upcoming`, `hide_watched` | |
| `min_duration`, `max_duration` | Seconds; 0 means no limit |
| `min_views`, `max_views` | |
| `min_subs`, `max_subs` | Set `max_subs` low for "small channels only" |
| `max_age_days` | |
| `min_like_ratio` | |
| `languages` | Caption language codes, e.g. `["en", "de"]` |
| `max_brainrot`, `max_clickbait`, `max_nsfw`, `max_music`, `max_ai_generated`, `max_profanity` | Ceilings, 0–100 |
| `min_education`, `min_info_density` | Floors, 0–100 |

**Filters that cannot see their data abstain.** A video with no subscriber count
is not hidden by a subscriber floor; one with no captions is not hidden by a
language filter. The alternative is an empty homepage with no visible cause,
which is the failure this project exists to avoid.

Everything the sliders cannot express goes in [rules](rules.md).

### Channels

Per-channel policy lives on the Channels page. These settings control how much
weight it carries.

| Setting | Default | Notes |
|---|---|---|
| `whitelist_only` | false | Show nothing but channels marked allow |
| `manual_strength` | 0.18 | Score added per priority point |
| `affinity_enabled` | true | Let watch time raise channels on its own |
| `affinity_strength` | 0.5 | How much derived affinity is worth |
| `affinity_half_life_days` | 45 | Recency decay on watch time |
| `blocked_hidden` | true | False buries blocked channels instead of removing them |

Three independent axes per channel:

- **Priority**, −5 to +5. A standing instruction. It never moves on its own.
- **Listing**: `allow`, `block` or `neutral`.
- **Affinity**, 0 to 1. Derived from watch seconds weighted by completion and
  decayed by recency, scaled against your most-watched channel. You do not set
  this; you can see its arithmetic.

Priority and affinity are stored in separate columns and reported separately in
every explanation. "I asked for this channel" and "you apparently love this
channel" are different claims, and blending them is how feeds become
unexplainable.

A fourth number, **quality**, is computed from the channel's own catalogue —
mean education and information density, topic consistency, minus clickbait and
brainrot. It is a separate ranking weight (`channel_quality`) because it says
nothing about what you asked for.

### DeArrow

| Setting | Default |
|---|---|
| `enabled` | false |
| `replace_titles` | true |
| `replace_thumbnails` | true |
| `show_original` | true |
| `min_votes` | 0 |
| `score_from_titles` | true |

`score_from_titles` is the interesting one: a video whose title the community
felt the need to rewrite is direct evidence of clickbait, so it forces the
clickbait score to at least 72 rather than leaving a heuristic to guess from
capital letters.

### SponsorBlock

| Setting | Default | Notes |
|---|---|---|
| `enabled` | false | |
| `categories` | most | Which segment types to fetch |
| `skip` | sponsor, selfpromo, interaction, music_offtopic | Which to skip during playback |
| `max_sponsor_ratio` | 1.0 | Hide videos above this fraction of sponsor read |
| `max_filler_ratio` | 1.0 | |
| `hide_exclusive_access` | false | Videos flagged as paid access |
| `score_penalty` | 0.0 | Rank down proportionally to sponsor load |

Segment data does double duty: the player skips what you ask it to (see
[player integration](player.md)), and sponsor load becomes a ranking signal,
because a video that is 40% ad read is a worse video.

### Composition and diversity

```
budget.enabled          enforce a mix rather than letting ranking decide
budget.quotas           relative shares per bucket
budget.daily_caps       absolute counts per bucket per day
```

Buckets are `education`, `entertainment`, `hobby`, `meme`, `music`, `other`.
`daily_caps` is the "at most three memes a day" control.

```
diversity.enabled         spread results and warn about rabbit holes
diversity.max_per_channel default 3
diversity.mmr_lambda      1.0 is pure relevance, lower trades it for variety
diversity.warn_below      topic entropy below this triggers the warning
```

Rabbit-hole detection measures normalised topic entropy across the rendered
page. When it collapses, the homepage says so and offers to raise novelty.

### Moods

Named overlays: Study, Relax, Explore ship by default, and you can save your
own from the Controls page.

A mood stores **only what differs** from your base settings. Change a filter
later and it still reaches every mood that did not deliberately override it.
Resolution order is defaults, then your settings, then the active mood.

---

## Profiles

A profile is the whole user-owned configuration — settings, interests, channel
policy — as one JSON document.

```bash
sieve profile export no-brainrot.json
sieve profile import someone-elses.json
```

Or from a URL, on the Controls page. That is the "recommendation market" without
a market: a profile is a file, so a gist or a repo is already a distribution
channel.

> [!WARNING]
> An imported profile is untrusted input and goes through the same whitelist as
> language-model output. Unknown keys are dropped, values are clamped to their
> documented ranges, and an invalid rule expression is discarded rather than
> stored. A profile cannot make Sieve do something the Controls page cannot.
