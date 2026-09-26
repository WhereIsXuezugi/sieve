# Changelog

Notable changes, newest first. This project follows
[semantic versioning](https://semver.org) once it reaches 1.0; until then, minor
versions may change behaviour.

## Unreleased

### Documentation

- README rewritten: light/dark screenshots of the real app, a feature tour, three install
  paths, a documentation map; 663 lines down to about 200 — the depth lives in docs/
- Android: download from Releases, build with one click on GitHub, or locally; a version
  tag now builds the APK and attaches it to the release as sieve-android.apk
- docs/configuration.md: video language, recency lift, homepage search, AI connection and
  AI tuning; screenshots regenerate with docs/images/make_screenshots.py

### 0.7.0

- **AI tuning** (optional, off = manual): hourly, the AI rates homepage videos and corrects
  scores it disagrees with (as "by AI" overrides; yours always win); daily, it adjusts score
  targets and weights from your More / Less. A computation slider (videos per hour), Run now,
  and Undo AI changes
- **AI connections** in Controls: Ollama, Claude, OpenAI, any OpenAI-compatible API
- **Homepage search** (AI with word-match fallback); never reorders or changes anything
- **Tell Sieve why** on a video, turned into feedback for similar videos
- **Video language** preference: rank other languages lower, or hide them
- **Full score range** in each "?" panel: a clickable 0–100 number line of your videos
- **Recency lift** for newly fetched videos, fading by half-life, never stored in scores
- **Incremental keyword fetching**: only new keywords are searched; removed ones take
  only their own videos
- **Backend problems explained** in the page (YouTube bot check, SponsorBlock timeouts),
  with technical details and help links; every notice can be dismissed
- Channel status badges; Exempt explained; contribution labels beside their own bars
- Fixed: More and Less could both be selected; Hide only dimmed the card; a rated video
  was moved by its own label rather than similar ones; "0% is about ' '"; blank titles
  and channel names; the player's choice did not reach the homepage
- 0.6.2's "Sign in to confirm you're not a bot" handling and cookie sign-in are included

### 0.6.2 — "Sign in to confirm you're not a bot"

- YouTube's bot check is recognised: yt-dlp pauses for 30 minutes and Sieve
  keeps working without it (feeds, results and watch pages, oEmbed); downloads
  wait instead of failing, with a Retry and an "Add cookies" link
- Sign yt-dlp in with an uploaded cookies.txt (Controls, Source) or a local
  browser's cookies; used by every yt-dlp call, including downloads
- Android: file inputs did nothing in the app's WebView, so imports and
  uploads were impossible on the phone; the app now opens a file picker

### 0.6.1 — review

- Fixed: offline videos could not seek or jump to chapters on Android — the
  Starlette it pins ignores Range requests; /media now answers them itself
- Fixed: twelve endpoints (feedback, hide, settings, rules, progress…) crashed
  with a 500 on an empty, non-JSON or list body; they now answer 400
- Fixed: the header's "More" menu was clipped on phones; the current page is
  scrolled into view in the phone navigation
- Changed: the header shows five everyday pages; Rules, Brief, Funnel,
  History, Debugger and Analytics are under "More"
- Changed: quota boxes appear only when quotas are on; performance internals
  are under "Advanced numbers"
- Removed: "Random order (ignore ranking)" — it discarded the ranking that is
  the point of Sieve; the homepage's Reshuffle button remains
- Added: installable as a web app (manifest), with Share → Sieve on phones

### Added — 0.6

- **Android app** (`android/`): Sieve itself on the phone, or a window onto your
  server; Share → Sieve; built by GitHub Actions (see android/README.md)
- **Library page**: search your own catalogue by meaning (related wording too),
  offline videos, new uploads, digest, notes export
- **Offline**: save videos (360p–1080p, no ffmpeg needed); they play in Sieve's
  player, with chapters and progress
- **Alerts**: a bell per channel for new uploads, on the Library page, as RSS,
  and pushed to a webhook (ntfy for phone notifications, or JSON)
- **Digest**: daily, every 3 days or weekly summary of what's new and your top
  picks; homepage, RSS and webhook
- **Chapters** in the player and on the video page, from descriptions
  (YouTube's rule: at least two, the first at 0:00)
- **Notes** on any video, at a moment or for the whole video, exported to
  Obsidian, Logseq (Markdown) or Readwise (CSV)
- **Watching elsewhere**: a userscript reports what you watch on YouTube or
  Invidious back to Sieve
- **Automatic captions** without yt-dlp, from the watch page; tried once per
  video
- **Per-channel check frequency**: busy channels checked more often than quiet
  ones, estimated from the gap between uploads (feeds only list the latest 15,
  so counting uploads would under-check busy channels)
- Runs on pydantic 1 as well as 2

### Added

- **Progress bar** for Fetch and Sync, under the page header: what it is doing
  and how far along ("Checking Veritasium, 5 of 19"). Also shows a sync the
  background worker is running. `GET /api/fetch/progress`
- **Next episode first**: series are recognised from their titles (and
  imported playlists), and the next episode of one you are watching goes near
  the top; syncs search for it when it is missing. `homepage.next_episode`
- **"?" beside every score slider**: what 20 or 30 actually means, with a
  reference scale, real titles from your catalogue at that value, and how much
  a limit there hides. Length, views and subscribers show your catalogue's
  spread. `GET /api/scales/{key}`
- **New videos every X**: the homepage adds nothing new until the interval
  passes, from an hour to 30 days, to limit viewing. `homepage.new_every`
- **Correct any score** on a video's page. Your value is used for that video,
  and your corrections train a model that adjusts similar videos.
  `PUT /api/videos/{id}/scores`
- **More accurate scores for sparse videos**: a video with little text leans on
  its channel's other videos (the channel prior)
- **Sync: never** — only the Sync button syncs
- Range sliders for length, views and subscribers (log scale for counts); an
  empty "highest" box means no limit
- Table headers stay in view while scrolling long tables

### Fixed

- A fetch blocked every other request to the server until it finished (it ran
  on the event loop)
- Phones and mid-width windows: wide tables scrolled the whole page sideways
- The score breakdown on a video's page had stray bullets and showed two raw
  feature names ("title len")

- **Your own searches did nothing without yt-dlp or Invidious**, silently:
  YouTube search needed one of them, and a plain install has neither. Sieve
  now reads YouTube's results page itself (`ytsearch.py`), handling both of
  the layouts YouTube currently serves, so niche topics — "heap
  exploitation", "algebraic topology" — work on any install
- Fetch searched for the newest uploads; it now searches by relevance, which
  finds the good videos on a niche topic. Sync still looks for new uploads
- Videos found by searching your topics had no ranking source of their own
  and rarely reached the homepage; they now do ("matches a topic you chose")
- Settings saves ran SponsorBlock lookups over the network

### Added

- **Re-check all videos** (Controls, top): rescores every video, recomputes
  channel quality, interests and the learned model, and forgets what was
  already shown. Every setting change now also reports its effect ("36 on the
  homepage, 312 pass your filters, 88 filtered out")
- Cyber security topic; more mathematics channels and searches

### Changed

- Every explanation on the site rewritten to be short and plain; internal
  names (`preference`, `channel_priority`, "pull") replaced with plain words;
  ranking weights moved under "Advanced"
- Removed options that did not need to be options: "replace the demo
  catalogue" (always happens), "region for trending" (YouTube retired
  Trending), and buttons made redundant by Re-check (rescore, retrain,
  rederive, recompute affinity)
- Network failures are reported in plain words ("could not reach YouTube: no
  internet connection, or DNS is not working")

- **Nothing is fetched until you ask.** The automatic first fetch is gone: a
  fresh install makes no requests until you press **Fetch** (empty homepage,
  every page's header, Controls, or `sieve fetch`), so there is time to set
  topics and the pull limit first. The empty homepage says what Fetch will do
  and roughly how many requests it takes. The background worker only syncs,
  and only once there is something to sync
- Fetch and Sync are separate: Fetch finds videos for your topics; Sync
  refreshes subscriptions, playlists and the channels Sieve follows

### Added

- **Fetch and Sync buttons** in the header of every page; results appear in a
  corner message that survives the page reload
- **Backups**: make one now, back up automatically on a schedule, delete, and
  **roll back** in place without a restart (the replaced state is backed up
  first). Each kind keeps its own newest N. Controls, Backups;
  `sieve backup`; `/api/backups`
- **Delete pulled videos**: removes what Sieve found for you and starts
  finding over, keeping everything that is yours. Every video now records how
  it arrived (`videos.origin`). `sieve reset pulled`;
  `POST /api/catalogue/reset-pulled`
- **Reset the pull counter**: `sieve pulls reset`; `POST /api/pulls/reset`
- `sieve --version`, which also prints where Sieve is installed

- **No import needed.** Sieve finds videos by itself: your interests once it
  knows them, the channels your ranking rates well (without subscribing), and
  until then topics you pick, with built-in starter channels whose public feeds
  need neither Invidious nor yt-dlp. A fresh install fills its own homepage.
  Controls, Finding videos; the `pull` settings section
- **Pull limit**: at most N requests to YouTube or Invidious in any rolling
  window from 15 minutes to 30 days. Cache hits are free. Binds what Sieve does
  by itself, or everything. `GET /api/pulls`, `sieve pulls`
- **Always a full homepage**: 1 to 100 videos, topped up from the catalogue
  when the ranking finds too few; optionally past soft filters, with every
  filler labelled with the filter it fails. `homepage.fill`
- **Real demo**: `sieve demo` pulls real videos for chosen topics; the
  synthetic catalogue is `sieve demo --synthetic`, and real videos replace it
  automatically. `POST /api/catalogue/remove-demo`
- The empty homepage says what is happening — fetching, unreachable, pull limit
  spent, or filtered out — offers the fix, and updates itself when videos arrive
- Syncs rotate channels least-recently-pulled first, so one cut short resumes
  where it stopped; a channel that no longer exists rests for a week
- Sync intervals up to 30 days

### Fixed

- Nothing was fetched when something other than Invidious — a dev server —
  answered on port 3000: its 404 pages were read as "no such channel", every
  channel was marked dead, and the pull limit ran out with nothing to show.
  Only an Invidious' own "not found" is believed now, and a health check skips
  a missing Invidious without spending a pull
- A YouTube feed outage (404 for every channel) rested every channel for a
  week; it is now reported as an outage after three, blaming no channel
- The early pull on a fresh install repeated every two minutes for as long as
  the catalogue was small, spending the pull limit on failures. It is now a
  one-time first fetch with backoff (see configuration.md, Finding videos);
  `min_catalogue` and `starter_per_sync` are gone
- Starter channels were re-pulled on every sync; now once per topic
- Backfill of imported history and caption fetching could spend the whole
  pull limit before a sync; they now wait while it is under half spent
- Channels the algorithm follows had no source of their own and reached the
  page only by view count, labelled "popular outside your usual topics"
- Videos with an unknown upload date never reached the subscriptions source
- Composition quotas were silently dropped whenever the per-channel cap blocked
  everything else ("memes: 1%" could show six); only relaxed fill relaxes them
- "Put unfinished videos first" could drop the video you were watching when
  its channel hit the per-channel cap; up to a quarter of the page is now kept
  for what you watched in the last 30 days
- Channel quality, which decides the followed channels, was computed before
  new videos were scored, so the first fetch's channels went unfollowed
- Phones got a three-column grid of 110px cards (a CSS specificity slip); long
  titles, channel names and status messages overflowed their boxes; action
  messages vanished before they could be read
- Health-check and Sift messages showed as lowercase fragments without a full
  stop; "At most / pulls per" read as two half-sentences; "0 views" on the
  player page
- A sync's `fetched` count included videos already known, so it read as if
  fetched videos were missing; it is now `new` and `seen`
- One deleted or renamed channel stopped the rest of the subscription sync
- "Ignore ranking, draw at random" did nothing
- "Raise novelty" on the rabbit-hole warning posted to a page that only
  answers GET (405)
- Opening the Debugger or asking for a critique recorded impressions, using up
  daily caps
- A video that had just arrived was hidden until scored: its blank scorecard
  read as 50 on every axis, above the default nudity limit
- The minimum like ratio hid videos whose like count is unknown
- `"false"` sent to the API as a string was stored as true; filters accepted
  negative values and the homepage any count
- The empty homepage's text sat off-centre, and showed raw Markdown backticks
- The readout's dividers were invisible; counts read "1000K" instead of "1M";
  cards said "0 views · never" when those were unknown
- Two syncs could run at once — the worker and Sync now — fetching everything
  twice
- With no provider chosen, every video opened at a guessed Invidious on
  `127.0.0.1:3000`; it now opens in Sieve's own player until you choose

- **Sift**: run the whole algorithm over a search, a channel (address or
  @handle), a playlist, one video ("more like this"), or fresh searches for
  your interests — ranked, explained, and with every rejection listed.
  `POST /api/sift`
- **Funnel**: every candidate of a homepage render and its fate — shown,
  ranked but left off (with why), or rejected (with the filter). `GET /api/funnel`
- **History**: every watch, open and feedback record, deletable one by one or
  all at once. `GET /api/records/{kind}` and friends
- **Computation**: Light, Balanced and Thorough presets, or each number by
  hand, with the cost shown in requests per hour
- **Sieve's player**: YouTube's privacy-enhanced player in a Sieve page, with
  SponsorBlock skipping, resume, and real watch progress
- Discovery searches in the background sync, pulling in videos from channels
  you do not follow; how many is a Computation setting

- **YouTube directly, no Invidious needed.** Channels sync from YouTube's RSS
  feeds; yt-dlp, when installed (`sieve[youtube]`, and in the Docker image),
  adds durations, whole playlists, search and captions. Choose Automatic,
  Invidious only or YouTube only under Controls, Getting videos
- **No Save button.** Every control saves itself as it changes, reports a
  refused value instead of claiming a save, and snaps a refused choice back.
  Valid rules save themselves too
- Shorts from YouTube's feeds are recognised by their link, with no duration
- Docker instructions in the README, and an image that includes yt-dlp, runs
  as an unprivileged user, and needs no Invidious

- Choose where videos open: Invidious or Piped at any address, YouTube,
  YouTube's no-cookie domain, the FreeTube app, or a custom URL template.
  Controls, Playback; or the `playback` settings section
- An "open in" menu on every card, and every provider on the video page
- Partly watched videos resume where you left off, on every provider
- `/open/{id}`: every watch link goes through Sieve, which records the open and
  redirects with no referrer
- Delete all videos, and factory reset: Controls, Danger zone;
  `POST /api/reset`; `sieve reset catalogue|everything`. Both require typing
  `reset` and write a backup first; the newest five are kept
- `GET /api/videos/{id}/links`, `GET /api/providers`, `GET /api/backups`
- `tests/conftest.py` fails any test that contacts an external host
- `LICENSE` now holds the full AGPL-3.0 text, taken from GitHub's own licence
  reference, so GitHub detects the licence; the project notice is in `NOTICE`

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

- **The YouTube (no-cookie) option failed with "Error 153"**: it opened
  YouTube's embed player on its own, with the referrer Sieve strips everywhere,
  which YouTube refuses. It is now Sieve's player page
- Deleting your watch history kept everything learned from it: interests and
  channel affinity returned early when no history was left, before clearing
- Channel-quality contributions drew as a blank gap in explanation bars
- The yt-dlp status ignored `use_ytdlp = false`, giving wrong advice
- A player's repeated progress reports would each have added a history row
- Unknown durations drew an empty badge; "1 views"; failed thumbnails showed
  the browser's broken-image icon
- Invalid progress reports were a 500, not a 400

- **The YouTube warning stayed after choosing YouTube**, because the choice
  only took effect after the Save button at the very bottom of the page
- **Thumbnails of real videos were broken without Invidious**: every one
  pointed at the instance. They now come from whichever backend is live
- Rendering: card buttons did not line up; the warning banner's button broke
  across two lines; the "demo" label sat off-baseline; the "open in" menu could
  run off the screen; on phones the header took a sixth of the screen, the
  Debugger scrolled sideways, and buttons were too small to tap
- Every sync overwrote every stored field, erasing likes, keywords and
  category that a fuller lookup had found
- Sending one field of a score target — as moving its slider does — switched
  the target off and reset its weight
- `learning.enabled` could not be changed through the API
- An invalid rule sent to `/api/rules` was a 500, not a 400
- Saving a rule sent you to the homepage
- A large history import fetched every video inside the upload request; it now
  fetches 50 and backfills the rest in the background
- `sieve doctor` reported every Invidious instance as whichever one answered
- The default instances included a public Invidious instance, so a fresh
  install sent your subscription list to a third party nobody chose
- The container ran as root; `--build-arg EXTRAS=` produced an invalid install

- **Watch links gave a 404.** Every link pointed at a fixed Invidious address
  on `127.0.0.1:3000`, which nothing in the web app could change; without an
  instance there, videos failed to open, or opened a 404 from whatever else
  used that port. The provider is now a setting, and the homepage says when the
  default has not been confirmed
- **Demo videos gave a 404 on every provider**, because their ids are invented.
  They now open their Sieve page, which explains
- **Opening a video taught Sieve you disliked it.** Each click was recorded as
  a 2% watch, which the interest graph read as a bounce, the ranker as a
  negative example, and channel affinity as a failed watch. Opens now have
  their own table, and existing click rows are migrated out of the history
- The test suite contacted sponsor.ajay.app on every run, because a fixture
  enabled SponsorBlock without redirecting it

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
