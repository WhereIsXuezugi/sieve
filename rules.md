# Rules

Rules are for everything the sliders cannot express. They run after the simple
filters, on every candidate, and the editor previews what they would keep and
drop from your actual catalogue as you type.

```json
{"all": [
  {"field": "education", "op": ">", "value": 70},
  {"field": "brainrot", "op": "<", "value": 20},
  {"field": "duration_min", "op": "between", "value": [15, 60]},
  {"not": {"field": "title", "op": "contains", "value": "reaction"}}
]}
```

## Shape

| Form | Meaning |
|---|---|
| `{"all": [...]}` | every condition must hold |
| `{"any": [...]}` | at least one must hold |
| `{"none": [...]}` | no condition may hold |
| `{"not": {...}}` | invert a single condition |
| `{"field": ..., "op": ..., "value": ...}` | a comparison |

Combinators nest, up to eight deep. An empty list passes everything, so
`{"all": []}` is how you clear a rule without disabling it.

## Operators

| Operator | Works on | Notes |
|---|---|---|
| `>` `>=` `<` `<=` | numbers | |
| `==` `!=` | anything | case-insensitive for text |
| `between` | numbers | takes `[low, high]`, inclusive |
| `contains` `not_contains` | text | case-insensitive substring |
| `in` `not_in` | anything | value is a list |
| `matches` | text | regular expression, case-insensitive |
| `is_true` `is_false` | booleans | no `value` needed |
| `any_of` `none_of` | lists | set intersection against `keywords`, `topics` |

## Fields

<details open>
<summary><b>Content scores</b> — 0 to 100</summary>

`education` · `entertainment` · `stimulation` · `brainrot` · `clickbait` ·
`info_density` · `technical_depth` · `production` · `ai_generated` · `nsfw` ·
`music` · `profanity`

</details>

<details>
<summary><b>Metadata</b></summary>

| Field | Type | Notes |
|---|---|---|
| `duration` | number | seconds |
| `duration_min` | number | minutes, for readability |
| `views` `likes` `subs` | number | |
| `like_ratio` | number | likes divided by views |
| `age_days` | number | |
| `published` | number | Unix timestamp |
| `title` `description` `author` `author_id` `genre` | text | |
| `keywords` `topics` | list | use with `any_of` / `none_of` |
| `language` | text | from the caption tracks |

</details>

<details>
<summary><b>Booleans</b></summary>

`is_live` · `is_upcoming` · `is_short` · `family_safe` · `watched` ·
`subscribed` · `has_transcript` · `has_chapters`

</details>

<details>
<summary><b>Community data</b> — needs SponsorBlock or DeArrow enabled</summary>

| Field | Type | Notes |
|---|---|---|
| `sponsor_ratio` | number | fraction of runtime, 0 to 1 |
| `filler_ratio` `selfpromo_ratio` | number | |
| `exclusive_access` | bool | flagged as paid access |
| `dearrow_retitled` | bool | the community rewrote the title |

</details>

<details>
<summary><b>Channel policy</b></summary>

| Field | Type | Notes |
|---|---|---|
| `channel_priority` | number | −5 to +5, what you set |
| `channel_affinity` | number | 0 to 1, derived from watch time |
| `channel_quality` | number | 0 to 100, from the channel's own catalogue |
| `channel_allowed` `channel_blocked` | bool | |

</details>

---

## Cookbook

**Deep educational, nothing short**

```json
{"all": [
  {"field": "education", "op": ">", "value": 70},
  {"field": "brainrot", "op": "<", "value": 20},
  {"field": "duration_min", "op": "between", "value": [15, 60]},
  {"field": "is_short", "op": "is_false"}
]}
```

**The brief's actual example: hard engineering, low production, people who know
what they are doing**

```json
{"all": [
  {"field": "technical_depth", "op": ">", "value": 65},
  {"field": "production", "op": "<", "value": 50},
  {"field": "subs", "op": "<", "value": 50000},
  {"field": "clickbait", "op": "<", "value": 35}
]}
```

**No reactions, no music, no ad reads**

```json
{"none": [
  {"field": "title", "op": "matches", "value": "reaction|tier list|ranking every"},
  {"field": "music", "op": ">", "value": 60},
  {"field": "sponsor_ratio", "op": ">", "value": 0.2}
]}
```

**Favourites always get through; everyone else has to earn it**

```json
{"any": [
  {"field": "channel_priority", "op": ">=", "value": 3},
  {"all": [
    {"field": "info_density", "op": ">", "value": 75},
    {"field": "clickbait", "op": "<", "value": 30}
  ]}
]}
```

**Nothing the community had to rename**

```json
{"field": "dearrow_retitled", "op": "is_false"}
```

**Lectures in a language I read, with chapters**

```json
{"all": [
  {"field": "language", "op": "in", "value": ["en", "de"]},
  {"field": "has_chapters", "op": "is_true"},
  {"field": "duration_min", "op": ">", "value": 25}
]}
```

---

## Design notes

**A field with no data abstains.** If a video carries no subscriber count, a
rule on `subs` passes it rather than dropping it. Silently emptying the homepage
because half the catalogue lacks a field is the failure this project exists to
avoid, and a filter that cannot see its data has no business acting.

**Type confusion is survivable.** Comparing a number to a string does not raise;
it passes. A malformed rule makes your feed wider, never empty.

**The operator set is closed.** There is no arithmetic, no function calls, and
no way to reach into Python. This matters because rules travel inside shared
profiles, and a profile from a stranger is the least trusted input the program
has. An invalid rule in an imported profile is discarded, not stored.

<details>
<summary>Why not JSONLogic, CEL or OPA</summary>

All three were considered.

- **OPA** is a separate Go daemon plus a policy language. Enormous for
  evaluating a dozen comparisons against a dict on a Raspberry Pi.
- **cel-python** pulls in protobuf and an ANTLR-generated parser.
- **json-logic-py** is genuinely small, but its operator set is generic — it has
  no notion of "duration between 15 and 60 minutes" — and it evaluates arbitrary
  nested arithmetic, which is not something you want in a config file that can
  arrive from a stranger.

So `rules.py` is about 120 lines implementing a closed operator set over a flat
field namespace. The syntax is deliberately JSONLogic-shaped, so swapping the
library back in later is a drop-in if you ever want its full operator set.

</details>
