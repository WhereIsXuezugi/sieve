# Command line

The command line covers running Sieve and looking after it: serving, syncing,
scoring, importing, channel policy and profiles. It is not a complete mirror of
the web interface.

For everything else — moods, rules, the blocklist, playlists, the brief — the
complete scripting surface is the [HTTP API](api.md), which *is* a complete
mirror, enforced by a test. Anything you can click, you can `curl`.

```
sieve [--config PATH] COMMAND
```

`--config` overrides the search path described in
[configuration](configuration.md#where-the-config-file-lives).

## Running

| Command | Does |
|---|---|
| `sieve serve` | Run the web app. `--host`, `--port`, `--no-worker` |
| `sieve doctor` | Diagnose an empty or wrong-looking homepage. `--quick` skips network checks |
| `sieve stats` | Counts of everything in the database |
| `sieve demo` | Fill the database with a synthetic catalogue. `--count N` |

`--no-worker` stops the background sync and scoring thread, for when you would
rather drive those from cron.

## Catalogue

| Command | Does |
|---|---|
| `sieve sync` | Pull recent videos for your subscriptions and trending. `--deep` also searches your top interests |
| `sieve score` | Score anything unscored. `--limit N`, `--no-transcripts`, `--all` to rescore everything |
| `sieve prune` | Drop stale rows and vacuum |

`sieve score --all` is required after changing `embed_provider`, since stored
vectors from one backend mean nothing to another.

## Importing

| Command | Accepts |
|---|---|
| `sieve import subs FILE` | Invidious, NewPipe or FreeTube subscription exports |
| `sieve import history FILE` | Invidious export or Google Takeout `watch-history.json` |
| `sieve import playlist ID` | A playlist id, pulled from your instance |
| `sieve import channels FILE` | `{"allow": [...], "block": [...], "priorities": {...}}` |

## Channels

```bash
sieve channel UC_xxxx --priority 5     # 1..5 boosts, -5..-1 buries
sieve channel UC_xxxx --allow          # whitelist
sieve channel UC_xxxx --block          # blacklist
sieve channel UC_xxxx --clear          # forget everything about this channel
sieve channel UC_xxxx --note "..."     # a reminder to future you
```

| Command | Does |
|---|---|
| `sieve affinity` | Recompute derived affinity from watch time |
| `sieve quality` | Rescore channels from their own catalogues |

## Learning

| Command | Does |
|---|---|
| `sieve train` | Rebuild the ranker from history and feedback |
| `sieve interests` | Rederive the interest graph |

Both are repair tools. Feedback is applied incrementally at request time, so you
do not normally run these by hand.

## Profiles

```bash
sieve profile export my-profile.json   # omit the path to write to stdout
sieve profile import someone-else.json
```

## Optional

| Command | Needs |
|---|---|
| `sieve vision` | `pip install 'sieve[vision]'`. Scores thumbnails, folds the result into `nsfw`. `--limit N` |

---

## Recipes

**First run against a real instance**

```bash
sieve import subs ~/Downloads/invidious-data.json
sieve import history ~/Downloads/watch-history.json
sieve sync && sieve score
sieve affinity && sieve quality
sieve doctor
sieve serve
```

**Scripted channel policy, from a list you keep in git**

```bash
while read -r id; do sieve channel "$id" --block; done < blocklist.txt
jq -r '.priorities | to_entries[] | "\(.key) \(.value)"' policy.json |
  while read -r id value; do sieve channel "$id" --priority "$value"; done
```

**Nightly maintenance, with the server running `--no-worker`**

```cron
*/30 * * * *  sieve sync && sieve score --limit 300
0    4 * * *  sieve affinity && sieve quality
0    5 * * 0  sieve prune
```

**Is it me or is it broken**

```bash
sieve doctor          # names the cause of an empty homepage
sieve stats           # is there anything in the catalogue at all
```

`sieve doctor` checks for things that fail silently: an unreachable instance, an
unscored catalogue, `min_duration` above `max_duration`, whitelist-only mode
with an empty allow list, every source set to zero, or an embedding backend
configured but not answering.
