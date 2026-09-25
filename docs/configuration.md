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
  - [Getting videos](#getting-videos)
  - [Finding videos](#finding-videos)
  - [Computation](#computation)
  - [Playback](#playback)
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
| `watch_base` | `http://127.0.0.1:3000/watch?v=` | The Invidious address used when [Playback](#playback) names no instance. Choosing where videos open is a user setting now |
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

### YouTube

Used by the `youtube` and `auto` backends — see [Getting videos](#getting-videos).

| Key | Default | Notes |
|---|---|---|
| `youtube_url` | `https://www.youtube.com` | Where the feeds are fetched from; change only to route YouTube through a proxy |
| `use_ytdlp` | `true` | `false` to use YouTube's feeds only, even when yt-dlp is installed |

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
| `count` | 36 | Videos on the page, 1 to 100 |
| `columns` | 3 | 1 to 6 |
| `density` | `comfortable` | `comfortable`, `compact` or `list` |
| `mode` | `blend` | `blend`, `playlist`, `continue` or `subscriptions` |
| `playlist_id` | — | Used when `mode` is `playlist` |
| `refresh_seed` | `daily` | `daily`, `session` or `fixed`. How often the order is allowed to change |
| `show_explanations` | true | The why-bar under each video |
| `show_scores` | true | |
| `continue_first` | true | Unfinished videos at the top |
| `next_episode` | true | Put the next episode of a series you are watching near the top (see below) |
| `new_every` | 0 | Minutes between new videos appearing; 0 = any time. Between times the page only shows what it already showed |
| `recent_boost` | true | Lift newly fetched videos for a while |
| `recent_strength` | 1.0 | How much, 0 to 3, on the same scale as the ranking weights |
| `recent_half_life` | 24 | Hours for the lift to halve; it is never stored in a video's scores |
| `search_mode` | `ai` | Searching the homepage: `ai` (falls back to word match) or `math` |
| `fill` | `catalogue` | What to do when the ranking finds fewer videos than `count`: `off`, `catalogue` or `relaxed` |

`mode: playlist` with a single playlist is the "100% Watch Later, nothing else"
homepage. `mode: continue` is "only things I started".

**Filling the page.** With `fill: catalogue` (the default), a short page is
topped up first with what the page's own caps left off, then from the rest of
the catalogue, newest first — every filter, the per-channel cap, quotas and
daily caps still apply, and a page they leave short says which one did. With
`fill: relaxed` the page is always full: it may go past the per-channel cap,
composition quotas and daily caps, and past soft filters (duration, views, age, score cutoffs, Shorts, languages,
sponsor load, your rule). It never shows blocked channels, hidden videos,
blocked title words, what you have watched, or anything over your nudity
limit. Every filler says under its title which filter it fails, and the Funnel
lists it as "filled in despite …". `fill: off` shows what there is.

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

### Getting videos

Where the catalogue comes from. Controls page, Getting videos panel; or
`POST /api/settings` with `{"source": {"backend": "youtube"}}`.

| `source.backend` | Meaning |
|---|---|
| `auto` (default) | Invidious while it answers, YouTube directly when it does not |
| `invidious` | Your instances only. Nothing is fetched from Google directly |
| `youtube` | YouTube directly; no instance needed |

**YouTube directly** reads YouTube's own RSS feeds for channel uploads: no API
key, one request per channel, and Shorts are recognised from their links. What
a feed lacks, [yt-dlp](https://github.com/yt-dlp/yt-dlp) supplies when it is
installed (`pip install 'sieve[youtube]'`, and always in the Docker image):
durations, whole playlists, search and caption tracks. Without yt-dlp, videos
from YouTube have no duration — filters that need one abstain rather than guess
— and a playlist import stops at its latest fifteen entries.

In `auto`, one failed Invidious request makes Sieve skip Invidious for ten
minutes, so a sync of hundreds of channels does not wait on a dead instance for
each one. `GET /api/status` reports which backend answered last.

Thumbnails follow the backend: through the Invidious proxy while that is live,
from YouTube's image host otherwise. Every page links them through Sieve's
`/thumb/{id}`, so switching backend never leaves broken images.

**Series.** A title with an episode number — "Lecture 4", "Ep. 12", "Part 3",
"#7", "3 of 10", "S2E5" — belongs to a series: the same channel, the same title
without the number. A bare number never counts, so "Calculus 101" and "iPhone
15" are not episodes. Once you have watched most (60%) of an episode in the last
60 days, the lowest-numbered unwatched later episode is put near the top,
labelled "next episode of a series you're watching". In an imported playlist,
the next video in the playlist counts too. When the next episode is not in the
catalogue yet, each sync searches for it (up to three series per sync).

**New videos every X.** With `new_every` set, the homepage remembers the videos
it showed and, until that much time has passed, draws only from them: videos
you watch or filter out leave, nothing new arrives. Moods and settings changes
do not open it early.

### Finding videos

Sieve can fill the catalogue by itself, so importing subscriptions is optional.
Controls page, Finding videos panel; or `POST /api/settings` with a `pull`
section.

| Setting | Default | Notes |
|---|---|---|
| `auto` | true | After a Fetch, keep finding: each sync also follows well-rated channels and searches your phrases |
| `topics` | science, engineering, history, technology | Used until Sieve knows your interests, and for starter channels. See `sieve/starter.py` for the list |
| `custom_topics` | (none) | Your own searches, e.g. "heap exploitation"; searched first, by relevance |
| `starter_channels` | true | Start each topic from a few well-known channels, pulled once |
| `follow_channels` | 10 | Unsubscribed channels the ranking rates well, kept up with |
| `per_pull` | 20 | Videos kept from each channel or search |
| `limit_enabled` | false | Turn the pull limit on |
| `limit_count` | 300 | At most this many pulls… |
| `limit_window` | 1440 | …in any this many minutes: 15, 30, 60, 180, 360, 720, 1440, 4320, 10080, 20160 or 43200 (30 days) |
| `limit_manual` | false | Count what you ask for too (Sync now, Sift, imports), not only what Sieve does by itself |

**Nothing is pulled until you ask.** A fresh install makes no requests at all.
Choose your topics and limits, then press **Fetch** — on the empty homepage,
in the header of every page, under Controls, Finding videos, or `sieve fetch`.
Fetch pulls each chosen topic's starter channels (when "start each topic from
a few well-known channels" is on), searches your own phrases, and searches as
many topic phrases as `compute.discover_terms` allows. The homepage and the
Controls panel say beforehand roughly how many requests it will take.

**Sync** refreshes what you already have. The background worker syncs on the
schedule in `compute.sync_minutes`, but only once there is something to
refresh: a past Fetch, imported subscriptions or playlists, or interests from
watch history. It never fetches. The **Sync** button in the header runs one
now.

A sync works through its sources in this order, and stops cleanly where the
pull limit says so: subscriptions (least recently pulled first, so the next
sync resumes with the ones it missed), playlists, followed channels and your
own search phrases (after a Fetch, while `auto` is on), searches for your
interests, trending. Never starter channels: that is Fetch.

While the limit is under half spent, optional work waits so new videos always
have budget: captions for scoring, filling in videos from imported history,
and yt-dlp's second lookup per channel (a channel then costs one pull instead
of two).

`sieve fetch`, `sieve sync`, `POST /api/fetch` and `POST /api/sync` report `new` (videos not seen before),
`seen` (including ones already known), `pulls` (spent by this sync), `from`
(per source) and, if it stopped early, `stopped` with the reason.

**Followed channels** are the algorithm's own subscriptions: channels you gave
a positive priority or allowed, channels your watch time favours, and channels
whose own catalogue scores well on your criteria — never ones you blocked.

**A pull** is one request that leaves the machine: one channel's uploads, one
search, one playlist, one video's details, one caption track. Answers from
Sieve's cache are free and are not counted. The window rolls, so "300 per day"
means at most 300 in any 24 hours. `GET /api/pulls` and `sieve pulls` show the
usage and what it was spent on.

A channel that answers "no such channel" in three syncs in a row rests for a
week, so a stale subscription or starter entry costs almost nothing. When every
channel answers that at once, it is an outage on YouTube's side: the sync stops
after three and blames no channel.

With `source.backend` set to `auto`, a quick health check (not counted as a
pull) decides whether an Invidious is really there before any request is sent
to it. Something else listening on its port — a dev server on 3000, say — is
recognised as not an Invidious and skipped.

**Starting over.** Controls, Danger zone, *Delete pulled videos* (or `sieve
reset pulled`) removes what Sieve found for you — through Fetch, followed
channels, searches and trending — and forgets which channels it pulled. It
keeps your subscriptions' and playlists' videos and anything you watched,
opened, rated or hid. *Reset the pull counter* (`sieve pulls reset`) forgets
recorded pulls, so the limit starts from zero.

### Scores: channel prior and your corrections

Each score is a small linear model over named features (see `scoring.py`),
plus two things that make it more accurate over time:

- **The channel prior.** A video whose own text says little (a feed or search
  result with only a title) leans on how its channel's other videos score, once
  the channel has three or more. It shows in a video's breakdown as "this
  channel's other videos". Computed from scores *before* the prior, so it
  cannot feed back into itself.
- **Your corrections.** On a video's page, "Wrong? Correct it" sets your own
  value for any score. That video uses it exactly; all your corrections train
  a model of where the engine is wrong for you (from the same features, the
  channel and the title's words), which adjusts similar videos — shown as
  "learned from scores you corrected". `PUT /api/videos/{id}/scores`.

The **?** beside each score slider shows what its numbers mean: a reference
scale, real titles from your catalogue at the slider's value, and how much a
limit there would hide (`GET /api/scales/{key}`).

### Video language

`filters.video_languages` lists the languages you want videos in (`en`, `de`,
`ja`…); `filters.video_language_mode` is `prefer` (other languages rank lower)
or `only` (they are hidden). The language is told from the title and
description; a video whose language cannot be told is never hidden.

### AI

Optional; everything works without it, with word matching in its place.
Controls, AI connects Ollama, Claude, OpenAI or any OpenAI-compatible API
(`ai.provider`, `ai.model`, `ai.base_url`). The API key is stored apart from
the settings and never appears in profile exports or API answers. The config
file's `llm_*` values still work when no provider is chosen here.

An AI is used for homepage search, "Tell Sieve why" on a video, the Brief page,
and — if you switch it on — automatic tuning:

| Setting | Default | Notes |
|---|---|---|
| `ai_tune.enabled` | false | Let the AI tune for you; off is fully manual |
| `ai_tune.videos_per_hour` | 15 | The computation limit, 5 to 50: videos it checks each hourly run (about one request per 10) |

Each hour it rates some homepage videos and, where it disagrees with the engine
by 15 points or more, sets a "by AI" score — which also trains the correction
model. Your own corrections always win. Once a day it adjusts score targets and
ranking weights from your More / Less and "tell Sieve why" — never filters.
**Undo AI changes** removes every AI score and restores the settings from
before its last change.

### When YouTube blocks yt-dlp

YouTube sometimes answers yt-dlp with "Sign in to confirm you're not a bot",
especially from servers, VPNs and busy addresses. Sieve then pauses yt-dlp for
30 minutes (asking again at once gets an address flagged for longer) and keeps
working without it: RSS feeds, YouTube's results and watch pages, oEmbed.
Downloads wait rather than fail.

To sign yt-dlp in, under Controls, Source, *If YouTube blocks yt-dlp*:

- **Upload a cookies.txt** with your youtube.com cookies (Netscape format, as
  "cookies.txt" browser extensions export it — ideally from a private window
  you then close, so the session is not rotated away). Works everywhere,
  including on a phone. Stored as `youtube-cookies.txt` beside the database,
  readable only by you, never in backups. `POST /api/source/cookies`.
- Or set `source.cookies_browser` (firefox, chrome, …) when Sieve runs on the
  same computer as that browser. An uploaded file takes precedence.

Using an account's cookies ties those requests to that account; a spare
account is the cautious choice.

### Backups

A backup is a full copy of the database in `backups/` next to `sieve.db`.
Controls, Backups, or `sieve backup`:

| Setting | Default | Notes |
|---|---|---|
| `backups.auto` | false | Back up on a schedule |
| `backups.every_hours` | 24 | How often, 1 to 720 |
| `backups.keep` | 5 | Newest kept of each kind: by hand, automatic, before a reset, before a rollback |

Every reset and every rollback writes one first. **Roll back** replaces the
live database with a backup straight away, without a restart; the state it
replaces is saved first, so a rollback can be undone by rolling back to that.
A backup from an older Sieve is brought up to date as it is restored.

### Computation

How much work Sieve does. Controls page, Computation panel; or
`POST /api/settings` with a `compute` section. Choosing a preset sets every
number; changing any number makes it `custom`.

| Setting | Light | Balanced | Thorough | What it costs |
|---|---|---|---|---|
| `pool_size` | 200 | 600 | 1500 | Candidates ranked per page render |
| `mmr_window` | 6 | 12 | 30 | Recent picks each candidate is compared with for variety |
| `score_batch` | 8 | 24 | 60 | Videos scored per background pass |
| `transcripts` | off | on | on | One caption request per video scored |
| `sync_minutes` | 60 | 15 | 5 | How often the catalogue refreshes by itself; 0 = never (only the Sync button); up to 43200 (30 days) |
| `discover_terms` | 0 | 4 | 10 | Interest searches per sync, pulling in channels you do not follow |
| `sift_limit` | 20 | 40 | 100 | Videos fetched per Sift |

The deployment setting `use_transcripts = false` still switches captions off for
the machine, whatever the preset says.

### Playback

Where a video opens when you click it. Controls page, Playback panel; or
`POST /api/settings` with a `playback` section.

| Setting | Default | Notes |
|---|---|---|
| `provider` | (not chosen) | `invidious`, `piped`, `youtube`, `nocookie` (Sieve's player), `freetube` or `custom` |
| `invidious_url` | (empty) | Blank uses the host in `watch_base` |
| `piped_url` | `https://piped.video` | Any Piped instance; the official one is often busy |
| `custom_url` | (empty) | A template with `{id}`, and optionally `{t}` for the start second |
| `menu` | all | Which providers the per-video "open in" menu offers |
| `resume` | true | Start partly watched videos where you left off |
| `new_tab` | true | Desktop-app providers never open a tab |

**Sieve's player** (`nocookie`) embeds YouTube's privacy-enhanced player,
`youtube-nocookie.com`, inside a Sieve page at `/play/{id}`. YouTube's embed
refuses to play when opened on its own, or without a referrer — "Error 153" —
so this is the one page in Sieve that sends one: your Sieve address, to
YouTube's player. In exchange it skips the SponsorBlock categories you chose,
resumes where you left off, and reports how far you watched as one history row
per viewing. Videos whose uploader forbids embedding say so and offer the other
providers.

Every link goes through Sieve's `/open/{id}`, which records the open and
redirects to the provider with no referrer. Two consequences:

- Changing the provider changes every link at once, including on pages already
  open.
- A video no provider can play — anything that is not an eleven-character
  YouTube id, such as the demo catalogue — goes to its Sieve page with an
  explanation, instead of to a provider's 404.

> [!NOTE]
> Until you choose a provider, videos open in Invidious if `watch_base` names a
> real instance, and otherwise in Sieve's own player. The shipped `watch_base`
> is a guess at `127.0.0.1:3000`; sending every video there gave anyone without
> an Invidious a connection error on every click. The homepage mentions the
> choice until you make one.

Custom templates may use `http`, `https`, or a desktop player's protocol —
`freetube`, `mpv`, `vlc`, `iina`, `potplayer`, `stremio` — which works only if
that player's protocol handler is installed. `javascript:`, `data:` and
`file:` are refused, including inside imported profiles.

```text
https://my-player.example/watch?v={id}&t={t}
mpv://play/https://www.youtube.com/watch?v={id}
```

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
