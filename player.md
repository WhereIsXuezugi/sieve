# Wiring the player to Sieve

Sieve does not host the player. Invidious does, and that is the right split —
but it means two things need a small bridge.

## 1. Watch progress

Completion is the single most useful signal Sieve has. "Watched 100%" and
"closed after eight seconds" are opposite opinions about the same video, and
without them the learner is working from clicks alone, which is how every feed
you dislike was trained.

Opening a video from Sieve records a 2% watch so the click is not lost. To get
real numbers, have the player page report progress:

```js
// Add to your Invidious instance, e.g. in a user script or a template override.
(function () {
  const SIEVE = 'http://127.0.0.1:8080';
  const player = document.querySelector('video');
  const videoId = new URLSearchParams(location.search).get('v');
  if (!player || !videoId) return;

  let best = 0;
  player.addEventListener('timeupdate', () => {
    if (!player.duration) return;
    best = Math.max(best, player.currentTime / player.duration);
  });

  const report = () => {
    navigator.sendBeacon(
      SIEVE + '/api/progress',
      new Blob([JSON.stringify({ video_id: videoId, progress: best })],
               { type: 'application/json' })
    );
  };
  window.addEventListener('pagehide', report);
  player.addEventListener('ended', report);
})();
```

`POST /api/progress` takes `{video_id, progress, dwell}` where `progress` is
0..1. It is idempotent per watch event; sending it repeatedly just appends
history rows, so report once on the way out.

If you would rather not touch Invidious at all, Sieve still works — the learner
simply has less to go on, and the "completion by length" analytics stay empty.

## 2. SponsorBlock skipping

Sieve already holds the segments (it fetches them by hash prefix, so no extra
privacy cost) and exposes them:

```
GET /api/sponsorblock/{video_id}
→ {
    "segments": [{"category": "sponsor", "start": 30.0, "end": 330.0,
                  "action": "skip", "votes": 12, "locked": 0, "uuid": "…"}],
    "sponsor_ratio": 0.33,
    "filler_ratio": 0.06,
    "selfpromo_ratio": 0.0,
    "has_exclusive_access": 0,
    "skip": ["sponsor", "selfpromo", "interaction", "music_offtopic"]
  }
```

`skip` is the list of categories you ticked in Controls. A minimal skipper:

```js
const data = await (await fetch(SIEVE + '/api/sponsorblock/' + videoId)).json();
const skip = data.segments.filter(s => data.skip.includes(s.category)
                                    && s.action === 'skip');
player.addEventListener('timeupdate', () => {
  const t = player.currentTime;
  const hit = skip.find(s => t >= s.start && t < s.end - 0.2);
  if (hit) player.currentTime = hit.end;
});
```

Invidious also has its own built-in SponsorBlock support. If you are already
using that, leave it on and treat Sieve's copy of the data purely as a ranking
signal — the two do not conflict, and both read from the same upstream.

## 3. What Sieve does with the data either way

Independent of playback, segment data feeds the ranking:

- `sponsor_ratio` and `filler_ratio` are rule fields, so you can write
  `{"field": "sponsor_ratio", "op": "<", "value": 0.15}`
- Controls has a hard cutoff and a soft penalty for both
- `exclusive_access` can be filtered outright, which is the useful signal for
  "this trip was paid for by the company being reviewed"
- filler ratio lowers the information-density score, because it should
