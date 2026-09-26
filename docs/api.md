# HTTP API

Everything the web interface can do is also available as JSON. Use it to script
Sieve, drive it from a different front end, or query it from a shell.

The web app and the API share one implementation: a form on the Controls page
and the endpoint that does the same thing call the same function. A test
(`tests/test_parity.py`) fails if either surface gains a feature the other
lacks.

> [!TIP]
> A running instance serves interactive documentation at
> **`/api/docs`**, with every request body, every parameter, and a button to try
> each call. This page is the overview; that one is the exhaustive reference.

## Contents

- [Conventions](#conventions)
- [Quick tour](#quick-tour)
- [Endpoints](#endpoints)
- [Errors](#errors)
- [Recipes](#recipes)
- [Authentication](#authentication)

---

## Conventions

The base URL is wherever Sieve runs — `http://127.0.0.1:8377` by default.
Request and response bodies are JSON, except the two file imports, which take a
multipart upload.

| Method | Means |
|---|---|
| `GET` | Reads. Never changes anything |
| `POST` | Performs an action, or creates something |
| `PUT` | Saves something under a name you chose — a mood, a profile |
| `DELETE` | Removes it |

Write endpoints answer `{"ok": true, ...}` with whatever they changed.

---

## Quick tour

What is on my homepage, and why?

```bash
curl -s localhost:8377/api/recommendations?limit=3 | jq '.items[] | {title, explanation}'
```

Each item has `open_url` — Sieve's own link, which records that you opened it
and redirects to your provider — and `links`, the direct URL for every provider
you can use, default first. Each item also carries the same `explanation` the why-bar draws — components with
percentages that sum to 100 — plus the raw `components`, every content score,
and the channel `notes` behind the channel component. The response's
`diagnostics` holds the pool size, the rejection tally with reasons, topic
diversity and any rabbit-hole warning.

Why was something hidden?

```bash
curl -s localhost:8377/api/recommendations | jq '.diagnostics.rejected'
```

Turn a channel up, and block a word:

```bash
curl -s -X POST localhost:8377/api/channels/UCxxxxxxxxxxxxxxxxxxxxxx \
     -H 'Content-Type: application/json' -d '{"priority": 4, "note": "the good lectures"}'

curl -s -X POST localhost:8377/api/blocklist \
     -H 'Content-Type: application/json' -d '{"kind": "term", "value": "reaction"}'
```

Import a playlist and make it the whole homepage:

```bash
curl -s -X POST localhost:8377/api/import/playlist \
     -H 'Content-Type: application/json' \
     -d '{"playlist": "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx"}'

curl -s -X POST localhost:8377/api/settings \
     -H 'Content-Type: application/json' \
     -d '{"homepage": {"mode": "playlist", "playlist_id": "PLxxxxxxxxxxxxxxxx"}}'
```

`playlist` accepts a bare id or any URL containing `list=`. Importing a playlist
that is already imported refreshes it.

Open videos in Piped rather than Invidious, and see where one would go:

```bash
curl -s -X POST localhost:8377/api/settings -H 'Content-Type: application/json' \
     -d '{"playback": {"provider": "piped", "piped_url": "https://piped.example.org"}}'

curl -s localhost:8377/api/videos/dQw4w9WgXcQ/links | jq '.links[] | {provider, url}'
```

A bad playback value is a `400` with the reason — for instance a custom
template without `{id}`, or one starting with `javascript:`.

Run your algorithm over a channel, and see what it threw out and why:

```bash
curl -s -X POST localhost:8377/api/sift -H 'Content-Type: application/json' \
     -d '{"query": "https://www.youtube.com/@3blue1brown"}' \
  | jq '{label, fetched, top: [.items[:5][] | .title], rejected: [.ledger[] | select(.stage=="rejected") | {title, reason}]}'
```

The whole funnel of your homepage, without adding impressions:

```bash
curl -s 'localhost:8377/api/funnel?stage=ranked' | jq '.entries[] | {title, reason}'
```

Start over. The word `reset` is required, and a backup is written first:

```bash
curl -s -X POST localhost:8377/api/reset -H 'Content-Type: application/json' \
     -d '{"scope": "everything", "confirm": "reset"}' | jq '{rows, backup}'
```

`catalogue` instead of `everything` deletes only the videos and what was
computed from them, keeping your subscriptions, history and settings.

---

## Endpoints

### Recommendations

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/critique` | Explain the current homepage in prose |
| `POST` | `/api/feedback` | Tell the ranker what you thought of a video |
| `POST` | `/api/hide` | Hide a video (same as POST /api/blocklist) |
| `POST` | `/api/progress` | Report how much of a video was watched |
| `GET` | `/api/recommendations` `?limit&mood&refresh` | The homepage, as data |
| `GET` | `/api/sponsorblock/{video_id}` | SponsorBlock segments for a video, and which to skip |
| `GET` | `/api/videos/{video_id}` | One video, with its full score breakdown |
| `GET` | `/api/videos/{video_id}/links` `?t` | Where a video can be opened, per provider |
| `GET` | `/api/providers` | Every provider, and how each is configured |

### Sift, funnel and records

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/sift` | Run the algorithm over a search, channel, playlist or video |
| `GET` | `/api/funnel` `?stage&limit` | Every candidate for the homepage and what became of it |
| `GET` | `/api/records/{kind}` `?limit&offset&video_id` | Your watch history, opens or feedback, newest first |
| `DELETE` | `/api/records/{kind}/{record}` | Delete one record |
| `POST` | `/api/records/{kind}/forget` | Delete every record of one kind |

`kind` is `watches`, `opens` or `feedback`.

### Settings and moods

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/moods` | Every mood, and which is active |
| `POST` | `/api/moods/active` | Switch mood; an empty name clears it |
| `PUT` | `/api/moods/{name}` | Save the current settings as a mood |
| `DELETE` | `/api/moods/{name}` | Delete a mood, built-in ones included |
| `GET` | `/api/settings` | Stored settings, and what they resolve to |
| `POST` | `/api/settings` | Merge a settings patch into yours |
| `POST` | `/api/settings/reset` | Reset every control to its default |

### Channels

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/channels` `?q&limit` | Every channel: priority, listing, affinity, quality |
| `GET` | `/api/channels/export` | Allow list, block list and priorities |
| `POST` | `/api/channels/import` | Merge channel lists into yours |
| `POST` | `/api/channels/quality` | Rescore channels from their own catalogues |
| `POST` | `/api/channels/recompute` | Recompute channel affinity from watch time |
| `POST` | `/api/channels/{channel_id}` | Set priority, listing, exemption or note for a channel |
| `DELETE` | `/api/channels/{channel_id}` | Forget everything you set for a channel |

### Interests, model and blocklist

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/blocklist` `?kind` | Hidden videos and blocked title terms |
| `POST` | `/api/blocklist` | Hide a video, or block every title containing a term |
| `DELETE` | `/api/blocklist/{kind}/{value}` | Unhide a video, or unblock a term |
| `GET` | `/api/interests` `?limit` | What the system thinks you like, with confidence |
| `POST` | `/api/interests` | Set or remove one interest |
| `POST` | `/api/interests/rederive` | Rebuild derived interests from watch history |
| `GET` | `/api/model` | The learned ranker and its coefficients |
| `POST` | `/api/model/reset` | Forget everything the ranker learned |
| `POST` | `/api/model/retrain` | Rebuild the ranker from history and feedback |

### Brief and rules

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/brief/apply` | Compile a brief and apply it |
| `POST` | `/api/brief/compile` | Turn a written brief into a settings patch, without applying it |
| `GET` | `/api/rules` | The active rule, and what it means in English |
| `POST` | `/api/rules` | Save and enable or disable the rule |
| `POST` | `/api/rules/validate` | Check a rule and preview what it keeps and drops |

### Playlists and imports

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/import/history` | Import watch history from Invidious or Google Takeout |
| `POST` | `/api/import/playlist` | Import or refresh a playlist, by id or URL |
| `POST` | `/api/import/subscriptions` | Import subscriptions from an Invidious, NewPipe or FreeTube export |
| `GET` | `/api/playlists` | Imported playlists |
| `DELETE` | `/api/playlists/{playlist_id}` | Forget an imported playlist |

### Profiles

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/profile/export` `?name&author` | Download your whole configuration as JSON |
| `POST` | `/api/profile/fetch` | Import a profile from a URL |
| `POST` | `/api/profile/import` | Import a profile document |
| `GET` | `/api/profiles` | Configurations saved on this machine |
| `PUT` | `/api/profiles/{name}` | Save the current configuration under a name |
| `DELETE` | `/api/profiles/{name}` | Delete a saved configuration |
| `POST` | `/api/profiles/{name}/load` | Replace the current configuration with a saved one |

### Analytics and maintenance

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/analytics` `?days` | Watch time, completion, channels, trends |
| `GET` | `/api/doctor` `?quick` | Why is my homepage empty |
| `POST` | `/api/maintenance/prune` | Drop stale rows and vacuum |
| `POST` | `/api/reset` | Delete every video, or factory-reset everything |
| `GET` | `/api/backups` | Backups written before each reset, newest first |
| `POST` | `/api/score` | Score unscored videos, or rescore everything |
| `GET` | `/api/status` | Counts, sync state, configured services |
| `POST` | `/api/sync` `?deep` | Pull recent videos now, then rescore and rederive |
| `POST` | `/api/fetch` | Find videos for your topics now |
| `GET` | `/api/fetch/progress` | What a running fetch or sync is doing |
| `POST` | `/api/ai/tune` | Let the AI tune scores, targets and weights now |
| `POST` | `/api/ai/tune/undo` | Undo everything the AI tuning changed |
| `POST` | `/api/notices/{key}/dismiss` | Dismiss a notice or banner |
| `GET` | `/api/problems` | Recent backend problems, explained |
| `POST` | `/api/homepage/search` | Find videos on your homepage |
| `POST` | `/api/videos/{video_id}/explain` | Tell Sieve in words why (not) this video |
| `GET` | `/api/scales/{key}/range` | Every score on one axis, and the videos at one |
| `GET` | `/api/ai` | The AI connection |
| `POST` | `/api/ai/key` | Store the AI provider's API key |
| `DELETE` | `/api/ai/key` | Forget the AI provider's API key |
| `POST` | `/api/ai/test` | Check the AI connection answers |
| `GET` | `/api/library/search` `?q&unwatched&min_duration&max_duration&channel` | Search your own catalogue by meaning |
| `GET` | `/api/videos/{video_id}/notes` | Your notes on a video |
| `POST` | `/api/videos/{video_id}/notes` | Add a note, optionally at a moment |
| `DELETE` | `/api/notes/{note_id}` | Delete a note |
| `GET` | `/api/export/notes` `?format&watched` | Export notes to Obsidian, Logseq or Readwise |
| `POST` | `/api/downloads` | Save a video for offline viewing |
| `GET` | `/api/downloads` | Saved and queued videos |
| `DELETE` | `/api/downloads/{video_id}` | Delete a saved video |
| `POST` | `/api/channels/{channel_id}/alert` | Alerts for a channel's new uploads |
| `GET` | `/api/alerts` | New uploads from alerted channels |
| `POST` | `/api/alerts/seen` | Mark every alert as seen |
| `GET` | `/api/digest` | The latest digest |
| `POST` | `/api/digest` | Write a digest now (and send it, if a webhook is set) |
| `POST` | `/api/notify/test` | Send a test message to your webhook |
| `POST` | `/api/source/cookies` | Sign yt-dlp in: upload YouTube cookies |
| `DELETE` | `/api/source/cookies` | Forget the uploaded YouTube cookies |
| `GET` | `/api/scales/{key}` | What a slider's numbers mean |
| `POST` | `/api/reevaluate` | Re-check every video against your current settings |
| `PUT` | `/api/videos/{video_id}/scores` | Correct a video's scores |
| `GET` | `/api/pulls` | Pulls made to YouTube or Invidious, and the limit |
| `POST` | `/api/pulls/reset` | Forget recorded pulls, so the pull limit starts from zero |
| `POST` | `/api/catalogue/reset-pulled` | Delete the videos Sieve found for you, and start finding over |
| `POST` | `/api/backups` | Back up the database now |
| `DELETE` | `/api/backups/{name}` | Delete one backup |
| `POST` | `/api/backups/{name}/restore` | Roll back to a backup, in place |
| `POST` | `/api/catalogue/remove-demo` | Delete the synthetic demo catalogue and what was learned from it |
| `POST` | `/api/vision/scan` `?limit` | Score thumbnails for visual NSFW (optional extra) |

### Request bodies

The write endpoints that take a body, with the fields that matter. `/api/docs`
has the full schemas.

| Endpoint | Body |
|---|---|
| `POST /api/feedback` | `{"video_id", "kind"}` — kind is `more`, `less`, `deeper`, `lighter`, `shorter`, `longer`, `higher_quality` or `block_channel` |
| `POST /api/progress` | `{"video_id", "progress"}` — progress is 0 to 1 |
| `POST /api/settings` | any settings patch, merged into yours: `{"novelty": 60, "filters": {"hide_shorts": true}}` |
| `POST /api/channels/{id}` | any of `priority` (−5 to 5), `listing` (`neutral`, `allow`, `block`), `exempt_filters`, `note` |
| `POST /api/channels/import` | `{"allow": [...], "block": [...], "priorities": {"UC…": 4}}` |
| `POST /api/interests` | `{"tag", "weight"}` to set, or `{"action": "remove", "tag"}` |
| `POST /api/blocklist` | `{"kind": "term" or "video", "value"}` |
| `POST /api/rules` | `{"expr": {...}, "enabled": true}` — see [rules](rules.md) |
| `POST /api/rules/validate` | `{"expr": {...}}` — returns a summary and a keep/drop preview |
| `POST /api/brief/compile` | `{"text"}` — returns the patch without applying it |
| `POST /api/brief/apply` | `{"text"}` |
| `POST /api/moods/active` | `{"name"}` — an empty name clears the mood |
| `POST /api/import/playlist` | `{"playlist"}` — an id or a URL |
| `POST /api/profile/import` | `{"profile": {...}, "merge": true}` |
| `POST /api/profile/fetch` | `{"url", "merge": true}` |
| `PUT /api/profiles/{name}` | optional `{"author"}` |
| `POST /api/score` | optional `{"all": false, "limit": 200, "transcripts": true}` |
| `POST /api/sift` | `{"query": "…", "kind": "auto", "filters": true}` — `kind` is `auto`, `search`, `channel`, `playlist`, `video` or `interests`; an empty query with `interests` searches for what you like |
| `POST /api/records/{kind}/forget` | `{"confirm": "forget"}` |
| `POST /api/progress` with a session | `{"video_id", "progress", "session"}` — reports from one viewing update one history row |
| `POST /api/reset` | `{"scope": "catalogue" or "everything", "confirm": "reset"}`, optional `"backup": true` |

The two file imports take a multipart field called `file`:

```bash
curl -s -F file=@subscriptions.json localhost:8377/api/import/subscriptions
curl -s -F file=@watch-history.json localhost:8377/api/import/history
```

---

## Errors

Failures return a status and a sentence in `detail`, written to be shown to a
person — the web interface displays it verbatim.

| Status | Means | Example `detail` |
|---|---|---|
| `400` | Understood, but cannot be done | `that URL has no playlist in it (no list= parameter)` |
| `404` | The thing named does not exist | `no mood called 'Late night'` |
| `422` | The body has the wrong shape | FastAPI's validation report |
| `502` | Your Invidious instance did not answer | `Invidious did not answer: …` |

A few deliberate refusals worth knowing:

- `POST /api/blocklist` with `kind: "channel"` is a `400`. Channels are blocked
  with `POST /api/channels/{id}` and `{"listing": "block"}`, which keeps a
  channel's priority and listing in one place.
- `POST /api/import/playlist` with YouTube's Watch Later (`list=WL`) is a `400`
  explaining why: it is private to your Google account and Invidious cannot
  read it.
- A playlist that comes back empty is a `400`, not a successful import of zero
  videos — Invidious answers a private or deleted playlist with an empty list.

---

## Recipes

**Everything I have told it about channels, as one file I can keep in git**

```bash
curl -s localhost:8377/api/channels/export > channels.json
curl -s -X POST localhost:8377/api/channels/import \
     -H 'Content-Type: application/json' -d @channels.json
```

**Switch to a study mood on weekday mornings**

```cron
0 8 * * 1-5  curl -s -X POST localhost:8377/api/moods/active -H 'Content-Type: application/json' -d '{"name":"Study"}'
0 18 * * 1-5 curl -s -X POST localhost:8377/api/moods/active -H 'Content-Type: application/json' -d '{"name":""}'
```

**What does it think I like, most confident first**

```bash
curl -s 'localhost:8377/api/interests?limit=20' | jq -r '.interests[] | "\(.band)\t\(.weight)\t\(.tag)"'
```

**Which channels do I watch the most, and how good are they by my own criteria**

```bash
curl -s localhost:8377/api/channels | jq -r '.channels[] | [.name, .affinity, .quality] | @tsv' | head
```

**Is my homepage empty because of me or because of the server**

```bash
curl -s 'localhost:8377/api/doctor?quick=false' | jq '{problems, services}'
```

**Let it find videos on its own, but never more than 200 requests a day**

```bash
curl -s -X POST localhost:8377/api/settings -H 'Content-Type: application/json' \
     -d '{"pull": {"auto": true, "topics": ["science", "history"],
          "limit_enabled": true, "limit_count": 200, "limit_window": 1440}}'
curl -s localhost:8377/api/pulls | jq '{used, limit, window, by_kind}'
```

**Try a rule against my real catalogue before saving it**

```bash
curl -s -X POST localhost:8377/api/rules/validate -H 'Content-Type: application/json' \
     -d '{"expr": {"all": [{"field": "technical_depth", "op": ">", "value": 70}]}}' | jq '.preview'
```

---

## Authentication

There is none. Sieve is single-user and assumes that whoever can reach it is
you — which applies to the API exactly as it does to the pages. Anyone who can
send it a request can read your watch history and change every setting.

Keep it on `127.0.0.1`, or behind a VPN, an SSH tunnel, or your reverse proxy's
authentication. See [Security](../SECURITY.md).
