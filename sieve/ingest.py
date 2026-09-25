"""Catalogue ingestion and background scoring.

Everything expensive happens here, off the request path. A homepage render is
pure SQLite; the network only gets touched by `sync` and `score_pending`, which
run on a timer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import nullcontext
from typing import Any

from . import backups, scoring
from . import channels as channel_policy
from . import interests as interest_store
from .community import CommunityData
from .config import Config
from .db import Database
from .invidious import NotFound, UpstreamUnavailable, normalise_video
from .pulls import PullLimitReached
from .upstream import Upstream

log = logging.getLogger("sieve.ingest")


# Where each sync step's videos came from, as stored in videos.origin.
ORIGIN_OF = {"subscriptions": "subscription", "followed": "followed", "starter": "starter"}
# Found by Sieve rather than brought by you: what "Reset pulled videos" deletes.
PULLED_ORIGINS = ("followed", "starter", "search", "trending")


def tagged(raw_videos, origin: str) -> list[dict]:
    """Normalise backend videos and record how they arrived."""
    return [{**normalise_video(v), "origin": origin} for v in raw_videos]


def _plain_failure(exc: Exception) -> str:
    """A network failure in words a person can act on."""
    text = str(exc)
    lowered = text.lower()
    for needle, meaning in (
        ("name or service not known", "no internet connection, or DNS is not working"),
        ("nodename nor servname", "no internet connection, or DNS is not working"),
        ("temporary failure in name resolution", "no internet connection, or DNS is not working"),
        ("connection refused", "the connection was refused"),
        ("timed out", "it took too long to answer"),
        ("certificate", "a secure-connection (certificate) error"),
        ("429", "YouTube is rate-limiting this address; try again later"),
        ("consent or bot check", "YouTube showed a consent or bot check instead of results"),
        ("outage", "YouTube's feeds are failing for every channel right now"),
    ):
        if needle in lowered:
            return f"could not reach YouTube: {meaning}"
    return f"could not reach YouTube: {text}"


class Ingestor:
    def __init__(self, cfg: Config, db: Database, api: Upstream, community: CommunityData):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.community = community
        self._stop = threading.Event()
        # Set by the web app when the homepage finds the catalogue short, so
        # the worker pulls now instead of at its next scheduled sync. Setting
        # an event is all the request path does: it never waits on the network.
        self._kick = threading.Event()
        # The worker and "Sync now" must not run two syncs at once: they would
        # fetch every channel twice and double-count the pull limit.
        self._sync_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_sync = 0
        self.last_attempt = 0
        self.last_result: dict[str, Any] = {}
        self.status = "idle"
        self._quality_stale = False
        self._retrain_lock = threading.Lock()
        self._retrain_again = False
        # What a fetch or sync is doing right now, for the progress bar.
        self.progress: dict[str, Any] = {"running": False, "kind": "", "done": 0, "total": 0, "label": ""}
        self._deep = False
        self._quiet = 0

    # -- settings, read fresh each time so a change applies on the next pass --

    def _settings(self) -> dict:
        from .config import resolve_settings

        return resolve_settings(self.db.get_setting("settings", {}) or {})

    def compute(self) -> dict:
        """The user's effort settings (Controls, Computation)."""
        return self._settings().get("compute", {})

    def pull_settings(self) -> dict:
        """Controls, Finding videos."""
        return self._settings().get("pull", {})

    def automatic(self):
        """Mark work as Sieve acting on its own, for the pull limit. Tolerates
        a stand-in upstream without a budget (the tests' fakes)."""
        marker = getattr(self.api, "automatic", None)
        return marker() if marker else nullcontext()

    def _budget_tight(self) -> bool:
        budget = getattr(self.api, "budget", None)
        return bool(budget and budget.tight())

    def request_pull(self) -> None:
        self._kick.set()

    # -- fetch state ----------------------------------------------------------
    #
    # Nothing is pulled until you ask. Fetch (the button on the homepage, in
    # the header and on Controls, or `sieve fetch`) finds videos for your
    # topics; Sync refreshes what you already have. The background worker
    # only ever syncs, and only once there is something to sync: a fetch has
    # happened, or you imported subscriptions or a playlist. So a fresh
    # install makes no requests at all until you have chosen your topics and
    # limits and pressed Fetch. The state is one row in `settings`.

    def pull_state(self) -> dict[str, Any]:
        state = self.db.get_setting("pull_state", {}) or {}
        return {"first_fetch_at": int(state.get("first_fetch_at") or 0),
                "last_fetch_at": int(state.get("last_fetch_at") or 0),
                "fetched_topics": list(state.get("fetched_topics") or state.get("started_topics") or [])}

    def has_fetched(self) -> bool:
        return bool(self.pull_state()["first_fetch_at"])

    def has_sources(self) -> bool:
        """Something a sync could refresh: a past Fetch, subscriptions or
        playlists you imported, or interests (from watch history) to search."""
        return self.has_fetched() or bool(self.db.scalar(
            "SELECT (SELECT COUNT(*) FROM subscriptions) + (SELECT COUNT(*) FROM playlists) "
            "+ (SELECT COUNT(*) FROM interests)", default=0))

    # -- catalogue ---------------------------------------------------------

    def sync(self, deep: bool = False) -> dict[str, Any]:
        """Refresh what you already have, in priority order, until done or the
        pull limit says stop:

        1. your subscriptions, least recently pulled first
        2. your playlists
        3. channels Sieve follows for you (after your first Fetch, while
           "keep finding videos" is on)
        4. searches: your interests, and your own search phrases
        5. trending, where the backend still has it

        Never your topics' starter channels: that is Fetch.
        """
        with self._sync_lock:
            if not self.has_sources():
                counts = self._result({}, 0, 0)
                counts["stopped"] = ("nothing to sync yet: press Fetch to find videos for your "
                                     "topics, or import subscriptions or a playlist")
                self.last_result = {"at": int(time.time()), **counts}
                return counts
            pull = self.pull_settings()
            finding = bool(pull.get("auto", True)) and self.has_fetched()

            def steps(found: dict[str, int], per: int) -> None:
                self._pull_channels(self._subscription_order(), "subscriptions", found, per=30)
                self._pull_playlists(found)
                if finding:
                    self._pull_channels(self._followed_order(pull), "followed", found, per)
                self._pull_searches(found, pull, per, deep, phrases=finding)
                self._pull_next_episodes(found)
                self._pull_trending(found)

            self._deep = deep
            try:
                return self._run(steps)
            finally:
                self._deep = False

    def fetch(self, topics: list[str] | None = None) -> dict[str, Any]:
        """Find videos for your topics: each topic's starter channels (when
        "start from well-known channels" is on) and searches for the topics and
        your own phrases (any backend: see ytsearch.py). What the Fetch buttons and
        `sieve fetch` do; never run by itself."""
        from . import starter

        pull = self.pull_settings()
        chosen = [t for t in (topics if topics is not None else pull.get("topics") or [])
                  if t in starter.TOPICS]
        custom = [str(t) for t in pull.get("custom_topics") or [] if str(t).strip()]
        if not chosen and not custom:
            from .actions import ActionError

            raise ActionError("choose at least one topic, or add a search phrase, under "
                              "Controls, Finding videos")
        with self._sync_lock:
            def steps(found: dict[str, int], per: int) -> None:
                if chosen and pull.get("starter_channels", True):
                    self._pull_channels(self._starter_order(chosen), "starter", found, per)
                # Your own phrases always (you typed them); topic phrases as
                # many as Controls, Computation, discovery searches allows.
                topic_phrases = [t for t in starter.searches_for(chosen) if t not in custom]
                wanted = int(self.compute().get("discover_terms", 4))
                keywords = custom[:12] + topic_phrases[:wanted]
                self._forget_removed_keywords(keywords)
                # Only keywords not fetched before, or whose videos are gone,
                # are searched again; the rest are reused as they are.
                fresh = [k for k in keywords if not self._keyword_intact(k)]
                self._reused_keywords = len(keywords) - len(fresh)
                self._search_phrases(fresh, found, per, newest=False, remember=True)

            self._reused_keywords = 0
            self._keywords_removed = 0
            counts = self._run(steps, "fetch")
            if self._reused_keywords:
                counts["keywords_reused"] = self._reused_keywords
            if self._keywords_removed:
                counts["removed_with_keywords"] = self._keywords_removed
            state = self.db.get_setting("pull_state", {}) or {}
            now = int(time.time())
            state["last_fetch_at"] = now
            if counts["seen"]:
                state.setdefault("first_fetch_at", now)
                state["first_fetch_at"] = state["first_fetch_at"] or now
                state["fetched_topics"] = sorted(set(state.get("fetched_topics") or []) | set(chosen))
            self.db.set_setting("pull_state", state)
            return counts

    def _run(self, steps, kind: str = "sync") -> dict[str, Any]:
        """Run pull steps, counting what they found and why they stopped."""
        self.progress = {"running": True, "kind": kind, "done": 0, "total": 0, "label": "Starting…"}
        found: dict[str, int] = {"subscriptions": 0, "playlists": 0, "followed": 0,
                                 "search": 0, "starter": 0, "trending": 0}
        pull = self.pull_settings()
        per = max(5, min(50, int(pull.get("per_pull", 20))))
        stopped = ""
        self.status = "syncing"
        self.last_attempt = int(time.time())
        before = self.db.playable_count()
        pulls_before = self._pulls_so_far()
        self._succeeded = False
        self._quiet = 0
        try:
            try:
                steps(found, per)
            except PullLimitReached as exc:
                stopped = str(exc)
            except UpstreamUnavailable as exc:
                stopped = _plain_failure(exc)
        finally:
            self.last_sync = int(time.time())
            self.status = "idle"
            self.progress = {**self.progress, "running": False, "done": self.progress["total"],
                             "label": "Done"}
        counts = self._result(found, before, pulls_before)
        # Real videos arrived: the synthetic demo catalogue has done its job.
        if counts["seen"] > 0:
            from . import demo

            if demo.demo_count(self.db):
                counts["demo_removed"] = demo.remove_demo(self.db)["videos"]
        if self._quiet:
            counts["not_due"] = self._quiet   # quiet channels, checked on a later sync
        # New uploads from channels you asked to hear about.
        from . import notify

        if notify.collect(self.db):
            counts["alerts"] = len(notify.unseen(self.db))
        if stopped:
            counts["stopped"] = stopped
        self.last_result = {"at": self.last_sync, **counts}
        return counts

    def _result(self, found: dict[str, int], before: int, pulls_before: int) -> dict[str, Any]:
        # What a person reading the result wants first: how many new videos.
        return {
            "new": max(0, self.db.playable_count() - before) if found else 0,
            "seen": sum(found.values()),       # including ones already known
            "pulls": self._pulls_so_far() - pulls_before if found else 0,
            "from": found or {"subscriptions": 0, "playlists": 0, "followed": 0,
                              "search": 0, "starter": 0, "trending": 0},
        }

    def _expect(self, n: int) -> None:
        self.progress["total"] += n

    def _tick(self, label: str) -> None:
        self.progress["done"] = min(self.progress["done"] + 1, max(self.progress["total"], 1))
        self.progress["label"] = label

    def _pulls_so_far(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM pulls", default=0))

    # Each step. A channel that does not exist is skipped and remembered; an
    # outage is tolerated for a few channels in a row, then the step gives up
    # rather than paying every remaining channel's timeout.

    def _pull_channels(self, channels: list[tuple[str, str]], origin: str,
                       counts: dict[str, Any], per: int) -> None:
        failures = 0
        missing: list[str] = []
        self._expect(len(channels))
        for channel_id, name in channels:
            self._tick(f"Checking {name or channel_id}")
            try:
                videos = self.api.channel_videos(channel_id)
            except PullLimitReached:
                raise
            except NotFound:
                missing.append(channel_id)
                # "No such channel" three times running, with nothing found
                # yet, is YouTube's feeds being down, not three deleted
                # channels. Say so, and blame no channel for it.
                if len(missing) >= 3 and not self._succeeded:
                    raise UpstreamUnavailable(
                        "YouTube answered \"no such channel\" for every channel asked about, "
                        "which is an outage on its side, not deleted channels") from None
                continue
            except UpstreamUnavailable as exc:
                failures += 1
                log.info("%s channel %s failed: %s", origin, channel_id, exc)
                if failures >= 3:
                    # Three in a row is an outage, not three bad channels:
                    # stop the sync rather than wait out every timeout.
                    raise
                continue
            except Exception as exc:
                log.warning("channel %s failed: %s", channel_id, exc)
                continue
            failures = 0
            kept = videos[:per]
            self.db.upsert_videos(tagged(kept, ORIGIN_OF.get(origin, origin)))
            self._mark_fetched(channel_id, origin, ok=True)
            counts[origin] += len(kept)
            self._succeeded = True
        # Only now, knowing the backend works, is "no such channel" believed.
        for channel_id in missing:
            if self._succeeded:
                self._mark_fetched(channel_id, origin, ok=False)

    def _pull_playlists(self, counts: dict[str, Any]) -> None:
        rows = self.db.query("SELECT id, title FROM playlists ORDER BY updated_at ASC")
        self._expect(len(rows))
        for row in rows:
            self._tick(f"Refreshing playlist {row['title'] or row['id']}")
            try:
                data = self.api.playlist(row["id"])
            except PullLimitReached:
                raise
            except Exception:
                continue
            videos = data.get("videos") or []
            self.db.upsert_videos(tagged(videos, "playlist"))
            self.db.execute(
                "UPDATE playlists SET title = ?, video_ids = ?, updated_at = ? WHERE id = ?",
                (data.get("title", ""), json.dumps([v.get("videoId") for v in videos if v.get("videoId")]),
                 int(time.time()), row["id"]),
            )
            counts["playlists"] += len(videos)

    def _pull_searches(self, counts: dict[str, Any], pull: dict, per: int, deep: bool,
                       phrases: bool = False) -> None:
        """Your strongest interests, and — with `phrases` — your own search
        phrases first. How many searches is Controls, Computation, discovery
        searches; --deep asks for at least eight."""
        from . import textutil as T

        wanted = int(self.compute().get("discover_terms", 4))
        if deep:
            wanted = max(wanted, 8)
        if not wanted:
            return
        positive, _ = interest_store.interest_vector(self.db)
        terms = T.top_terms(positive, wanted)
        if phrases:
            custom = [str(t) for t in pull.get("custom_topics") or [] if str(t).strip()]
            terms = custom + [t for t in terms if t not in custom]
        self._search_phrases(terms[:wanted], counts, per)

    def _keyword_intact(self, keyword: str) -> bool:
        """Fetched before, and every video it found is still here."""
        rows = self.db.query("SELECT video_id FROM keyword_results WHERE keyword = ?", (keyword,))
        if not rows:
            return False
        ids = [r["video_id"] for r in rows]
        have = self.db.scalar(
            f"SELECT COUNT(*) FROM videos WHERE id IN ({','.join('?' * len(ids))})", ids, 0)
        return have == len(ids)

    def _forget_removed_keywords(self, keywords: list[str]) -> int:
        """A keyword you removed takes its videos with it — unless another
        keyword, a subscription, a playlist or your own history or feedback
        still wants them."""
        keep = set(keywords)
        removed = [r["keyword"] for r in self.db.query("SELECT DISTINCT keyword FROM keyword_results")
                   if r["keyword"] not in keep]
        if not removed:
            return 0
        marks = ",".join("?" * len(removed))
        candidates = [r["video_id"] for r in self.db.query(
            f"SELECT DISTINCT video_id FROM keyword_results WHERE keyword IN ({marks})", removed)]
        self.db.execute(f"DELETE FROM keyword_results WHERE keyword IN ({marks})", removed)
        gone = 0
        for vid in candidates:
            still_wanted = self.db.scalar(
                "SELECT (SELECT COUNT(*) FROM keyword_results WHERE video_id = :v) "
                "+ (SELECT COUNT(*) FROM history WHERE video_id = :v) "
                "+ (SELECT COUNT(*) FROM feedback WHERE video_id = :v) "
                "+ (SELECT COUNT(*) FROM notes WHERE video_id = :v) "
                "+ (SELECT COUNT(*) FROM downloads WHERE video_id = :v) "
                "+ (SELECT COUNT(*) FROM videos WHERE id = :v AND (origin != 'search' "
                "   OR author_id IN (SELECT channel_id FROM subscriptions)))", {"v": vid}, 0)
            in_playlist = any(vid in json.loads(r["video_ids"] or "[]")
                              for r in self.db.query("SELECT video_ids FROM playlists"))
            if not still_wanted and not in_playlist:
                for table, column in (("scores", "video_id"), ("impressions", "video_id"), ("videos", "id")):
                    self.db.execute(f"DELETE FROM {table} WHERE {column} = ?", (vid,))
                gone += 1
        self._keywords_removed = gone
        return gone

    def _search_phrases(self, terms: list[str], counts: dict[str, Any], per: int,
                        newest: bool = True, remember: bool = False) -> None:
        if not terms:
            return
        status = getattr(self.api, "status", None)
        if status is not None and not status().get("can_search", True):
            return  # YouTube without yt-dlp cannot search; do not pretend
        failures = 0
        self._expect(len(terms))
        for term in terms:
            self._tick(f"Searching “{term}”")
            try:
                # Fetch looks for the best videos on a topic (relevance); a
                # sync looks for what is new.
                found = self.api.search(term, **({"sort_by": "upload_date"} if newest else {}))
            except PullLimitReached:
                raise
            except Exception:
                failures += 1
                if failures >= 3:
                    return
                continue
            kept = tagged(found[:per], "search")
            self.db.upsert_videos(kept)
            counts["search"] += len(kept)
            if remember:
                now = int(time.time())
                self.db.execute("DELETE FROM keyword_results WHERE keyword = ?", (term,))
                self.db.executemany(
                    "INSERT OR IGNORE INTO keyword_results(keyword, video_id, fetched_at) VALUES(?,?,?)",
                    [(term, v["id"], now) for v in kept])
            if found:
                self._succeeded = True

    def _pull_next_episodes(self, counts: dict[str, Any]) -> None:
        """Search for the next episode of a series you are watching when the
        catalogue does not have it yet. At most three series per sync; only
        results from the same channel are kept."""
        from . import series

        if not self._settings().get("homepage", {}).get("next_episode", True):
            return
        wanted = list(series.missing_next(self.db))
        self._expect(len(wanted))
        for channel, key, number in wanted:
            self._tick(f"Looking for episode {number} of “{key}”")
            try:
                results = self.api.search(f"{key} {number}")
            except PullLimitReached:
                raise
            except Exception:
                continue
            same = [v for v in results if v.get("authorId") == channel]
            if same:
                self.db.upsert_videos(tagged(same[:5], "lookup"))
                counts["search"] += len(same[:5])

    def _pull_trending(self, counts: dict[str, Any]) -> None:
        region = str(self.pull_settings().get("region") or "US")[:2].upper()
        try:
            trending = self.api.trending(region)
        except PullLimitReached:
            raise
        except Exception as exc:
            log.info("trending unavailable: %s", exc)
            return
        self.db.upsert_videos(tagged(trending[:60], "trending"))
        counts["trending"] = len(trending[:60])

    # Which channels, in which order.

    REST_AFTER_FAILURES = 3
    REST_SECONDS = 7 * 86400

    def _order(self, channels: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Least recently pulled first, so a sync cut short by the pull limit
        resumes with the channels it missed. Channels that have said "no such
        channel" three times in a row rest for a week."""
        fetched = {r["channel_id"]: (r["fetched_at"], r["failures"])
                   for r in self.db.query("SELECT channel_id, fetched_at, failures FROM channel_fetches")}
        now = time.time()
        usable = [
            (cid, name) for cid, name in channels
            if not (fetched.get(cid, (0, 0))[1] >= self.REST_AFTER_FAILURES
                    and now - fetched[cid][0] < self.REST_SECONDS)
        ]
        return sorted(usable, key=lambda c: fetched.get(c[0], (0, 0))[0])

    def _subscription_order(self) -> list[tuple[str, str]]:
        rows = self.db.query("SELECT channel_id, name FROM subscriptions")
        return self._due(self._order([(r["channel_id"], r["name"]) for r in rows]))

    def _due(self, channels: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Channels worth checking now, at a pace that fits each one.

        A channel's pace is its uploads over the last 90 days: one that posts
        daily is checked every few hours, one that posts monthly about every
        ten days. A channel never checked is always due. Off, or on a deep
        sync, every channel is checked every time."""
        if self._deep or not self.compute().get("adaptive_sync", True):
            return channels
        now = time.time()
        fetched = {r["channel_id"]: r["fetched_at"] for r in self.db.query(
            "SELECT channel_id, fetched_at FROM channel_fetches")}
        # The typical gap between a channel's uploads, from the span of the
        # ones Sieve has. Not uploads per 90 days: a feed returns only the
        # latest 15, so a daily uploader would look like one every six days.
        gaps = {r["author_id"]: (r["newest"] - r["oldest"]) / (r["n"] - 1) for r in self.db.query(
            "SELECT author_id, COUNT(*) AS n, MIN(published) AS oldest, MAX(published) AS newest "
            "FROM videos WHERE published > ? GROUP BY author_id HAVING n >= 2",
            (int(now - 180 * 86400),))}
        due = []
        for cid, name in channels:
            last = fetched.get(cid, 0)
            if not last:
                due.append((cid, name))
                continue
            gap = gaps.get(cid) or 30 * 86400        # unknown: assume monthly
            # A third of the gap, between an hour and a week.
            check_every = min(7 * 86400, max(3600, gap / 3))
            if now - last >= check_every:
                due.append((cid, name))
        self._quiet += len(channels) - len(due)
        return due

    def _followed_order(self, pull: dict) -> list[tuple[str, str]]:
        """Channels you do not subscribe to that the ranking rates well: ones
        you gave a positive priority or allowed, ones your watch time favours,
        and ones whose own catalogue scores well on your criteria. A channel
        of average quality (40 or more out of 100) qualifies while nothing
        better is known, so the catalogue keeps moving after the first fetch;
        as better ones appear they take the places."""
        wanted = int(pull.get("follow_channels", 10))
        if wanted <= 0:
            return []
        rows = self.db.query(
            """
            SELECT id, name, score FROM (
                SELECT c.id, c.name,
                       COALESCE(p.priority, 0) * 0.5
                       + CASE WHEN p.listing = 'allow' THEN 1.0 ELSE 0 END
                       + c.affinity * 2.0 + (c.quality - 50.0) / 50.0 AS score,
                       COALESCE(p.listing, 'neutral') AS listing
                FROM channels c LEFT JOIN channel_prefs p ON p.channel_id = c.id
                UNION ALL
                SELECT p.channel_id, p.name,
                       p.priority * 0.5 + CASE WHEN p.listing = 'allow' THEN 1.0 ELSE 0 END,
                       p.listing
                FROM channel_prefs p WHERE p.channel_id NOT IN (SELECT id FROM channels)
            )
            WHERE score > -0.2 AND listing != 'block' AND id LIKE 'UC%' AND length(id) = 24
              AND id NOT IN (SELECT channel_id FROM subscriptions)
            ORDER BY score DESC LIMIT ?
            """, (wanted,))
        return self._due(self._order([(r["id"], r["name"]) for r in rows]))

    def _starter_order(self, topics: list[str]) -> list[tuple[str, str]]:
        from . import starter

        blocked = {r["channel_id"] for r in self.db.query(
            "SELECT channel_id FROM channel_prefs WHERE listing = 'block'")}
        subscribed = {r["channel_id"] for r in self.db.query("SELECT channel_id FROM subscriptions")}
        return self._order([c for c in starter.channels_for(topics)
                            if c[0] not in blocked and c[0] not in subscribed])

    def _mark_fetched(self, channel_id: str, origin: str, ok: bool) -> None:
        self.db.execute(
            "INSERT INTO channel_fetches(channel_id, fetched_at, failures, origin) VALUES(?,?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET fetched_at = excluded.fetched_at, "
            "failures = CASE WHEN ? THEN 0 ELSE channel_fetches.failures + 1 END, "
            "origin = excluded.origin",
            (channel_id, int(time.time()), 0 if ok else 1, origin, 1 if ok else 0))

    def bootstrap(self, topics: list[str] | None = None) -> dict[str, Any]:
        """Fetch, then score what arrived so it can be ranked straight away.
        What `sieve fetch` and `sieve demo` do."""
        if topics is not None:
            from .actions import save_settings

            save_settings(self.db, {"pull": {"topics": list(topics)}})
        counts = self.fetch(topics)
        self.score_pending(limit=300, fetch_transcripts=False)
        channel_policy.recompute_quality(self.db)
        return counts

    # -- scoring -----------------------------------------------------------

    def rescore_ids(self, video_ids: list[str]) -> int:
        """Rescore these videos now, without the network."""
        return self.score_pending(limit=len(video_ids), fetch_transcripts=False, only=video_ids)

    def retrain_corrections_async(self) -> None:
        """Retrain the correction model from your overrides, then rescore the
        catalogue with it — in the background, one run at a time."""
        from . import corrections

        def work() -> None:
            if not self._retrain_lock.acquire(blocking=False):
                self._retrain_again = True
                return
            try:
                while True:
                    self._retrain_again = False
                    corrections.train(self.db)
                    self.rescore_all()
                    if not self._retrain_again:
                        break
            except Exception as exc:
                log.warning("retraining corrections failed: %s", exc)
            finally:
                self._retrain_lock.release()

        threading.Thread(target=work, name="sieve-corrections", daemon=True).start()

    def score_pending(self, limit: int | None = None, fetch_transcripts: bool | None = None,
                      only: list[str] | None = None) -> int:
        """Score videos that have no current scorecard (or exactly `only`)."""
        compute = self.compute()
        limit = limit or int(compute.get("score_batch", self.cfg.score_batch))
        # The config's use_transcripts is a hard off switch for the machine;
        # the compute setting chooses within it.
        wanted = bool(compute.get("transcripts", True)) and self.cfg.use_transcripts
        transcripts = wanted if fetch_transcripts is None else fetch_transcripts
        if transcripts and self._budget_tight():
            transcripts = False   # captions wait; new videos come first
        if only:
            marks = ",".join("?" * len(only))
            rows = self.db.query(f"SELECT v.* FROM videos v WHERE v.id IN ({marks})", only)
        else:
            rows = self.db.query(
                "SELECT v.* FROM videos v LEFT JOIN scores s ON s.video_id = v.id "
                "WHERE s.video_id IS NULL OR s.version < ? ORDER BY v.published DESC LIMIT ?",
                (scoring.SCORER_VERSION, limit),
            )
        if not rows:
            return 0

        ids = [r["id"] for r in rows]
        settings = self.db.get_setting("settings", {}) or {}
        from .config import resolve_settings
        resolved = resolve_settings(settings)
        try:
            self.community.enrich(ids, resolved)
        except Exception as exc:
            log.debug("community enrichment skipped: %s", exc)
        branding = self.community.branding_map(ids)
        segments = self.community.segments_map(ids)

        from . import corrections

        channel_means = self._channel_means({r["author_id"] for r in rows if r["author_id"]})
        auto_captions = bool(compute.get("auto_captions", True))
        auto_tries = 0
        correction_model = corrections.load(self.db)
        overrides = corrections.overrides_for(self.db, ids)

        written = []
        for row in rows:
            video = dict(row)
            transcript = video.get("transcript") or ""
            known = video.get("caption_langs") not in (None, "[]")
            # Captions YouTube told us about; and, once per video, its
            # automatic captions for videos that listed none.
            if (transcripts and not transcript and not video.get("captions_tried")
                    and (known or (auto_captions and auto_tries < 8))):
                auto_tries += 0 if known else 1
                transcript = self.api.captions(video["id"])
                if transcript:
                    self.db.execute(
                        "UPDATE videos SET transcript = ? WHERE id = ?", (transcript, video["id"])
                    )
                else:
                    self.db.execute("UPDATE videos SET captions_tried = 1 WHERE id = ?", (video["id"],))
            seg = segments.get(video["id"], {})
            extra = {
                "dearrow_retitled": 1.0 if branding.get(video["id"], {}).get("title") else 0.0,
                "sponsor_ratio": seg.get("sponsor_ratio", 0.0),
                "filler_ratio": seg.get("filler_ratio", 0.0),
                "channel_means": channel_means.get(video.get("author_id") or "", {}),
                "correction_model": correction_model,
                "overrides": overrides.get(video["id"], {}),
            }
            card = scoring.score_video(video, transcript, extra)
            written.append(scoring.card_to_row(card))

        self.db.executemany(scoring.INSERT_SCORE, written)
        return len(written)

    def _channel_means(self, channels: set[str]) -> dict[str, dict[str, float]]:
        """Each channel's average *base* score per axis, over channels with
        enough scored videos (scoring.CHANNEL_PRIOR_MIN)."""
        if not channels:
            return {}
        axes = list(scoring.MODELS)
        # A score from before base scores were stored had no prior in it, so it
        # already is a base score.
        columns = ", ".join(f"AVG(COALESCE(json_extract(s.base, '$.{a}'), s.{a})) AS {a}" for a in axes)
        out: dict[str, dict[str, float]] = {}
        channel_list = list(channels)
        for i in range(0, len(channel_list), 400):
            chunk = channel_list[i:i + 400]
            for row in self.db.query(
                    f"SELECT v.author_id, COUNT(*) AS n, {columns} FROM scores s "
                    f"JOIN videos v ON v.id = s.video_id "
                    f"WHERE v.author_id IN ({','.join('?' * len(chunk))}) GROUP BY v.author_id", chunk):
                if row["n"] >= scoring.CHANNEL_PRIOR_MIN:
                    out[row["author_id"]] = {a: float(row[a]) for a in axes if row[a] is not None}
        return out

    def rescore_all(self) -> int:
        self.db.execute("UPDATE scores SET version = 0")
        total = 0
        while True:
            done = self.score_pending(limit=200, fetch_transcripts=False)
            total += done
            if done == 0:
                break
        return total

    def backfill(self, limit: int = 20) -> int:
        """Fetch details for watched videos the catalogue does not have yet.

        Imports record history immediately and leave this to fill in the
        videos a few at a time, so a large Takeout file never blocks a request.
        Waits while the pull limit is tight: a Takeout file of thousands of
        videos used to spend the whole limit before a sync got any.
        """
        if self._budget_tight():
            return 0
        # Videos that arrived without a title or channel name (a search
        # result in YouTube's newer layout, a lookup that failed): fill in.
        for row in self.db.query("SELECT id FROM videos WHERE (title = '' OR author = '') "
                                 "AND length(id) = 11 AND meta_tries < 2 LIMIT 5"):
            try:
                video = self.api.video(row["id"])
                if video.get("title"):
                    self.db.upsert_videos(tagged([video], "lookup"))
            except Exception:
                pass
            self.db.execute("UPDATE videos SET meta_tries = meta_tries + 1 WHERE id = ? "
                            "AND (title = '' OR author = '')", (row["id"],))
        rows = self.db.query(
            "SELECT DISTINCT h.video_id FROM history h LEFT JOIN videos v ON v.id = h.video_id "
            "WHERE v.id IS NULL AND length(h.video_id) = 11 LIMIT ?", (limit,))
        done = 0
        for row in rows:
            try:
                self.db.upsert_videos(tagged([self.api.video(row["video_id"])], "history"))
                done += 1
            except UpstreamUnavailable:
                break
            except Exception as exc:
                log.debug("backfill skipped %s: %s", row["video_id"], exc)
        return done

    # -- worker ------------------------------------------------------------

    def start(self, interval: int = 900) -> None:
        if self._thread is not None:
            return

        def due() -> bool:
            # Only ever the ordinary schedule, and only once there is
            # something to refresh: never a fetch, never on a fresh install.
            minutes = int(self.compute().get("sync_minutes", interval // 60))
            if minutes <= 0:
                return False   # "never": only the Sync button
            return (time.time() - max(self.last_sync, self.last_attempt) > minutes * 60
                    and self.has_sources())

        def loop() -> None:
            while not self._stop.is_set():
                self._kick.clear()
                try:
                    with self.automatic():
                        if due():
                            self.sync()
                            channel_policy.recompute_affinity(self.db)
                            channel_policy.recompute_quality(self.db)
                            interest_store.derive_from_history(self.db)
                            continue
                        if backups.auto_due(self.db):
                            backups.create(self.db, "auto")
                        from . import autotune, llm, notify

                        if autotune.due(self.db):
                            autotune.run(llm.effective(self.cfg, self.db), self.db)
                            self.rescore_all()
                        if notify.digest_due(self.db):
                            notify.build_digest(self.db)
                        notify.deliver_pending(self.db)
                        scored = self.score_pending()
                        if scored:
                            self._quality_stale = True
                        elif self._quality_stale:
                            # Scoring just caught up: channel quality — which
                            # decides the channels Sieve follows for you — can
                            # now see the new videos. Computing it only after a
                            # sync, before they were scored, left the first
                            # fetch's channels unfollowed for two syncs.
                            channel_policy.recompute_quality(self.db)
                            self._quality_stale = False
                        if scored == 0 and self.backfill() == 0:
                            self._wait(30)
                            continue
                except Exception as exc:
                    log.exception("ingest worker error: %s", exc)
                    self._wait(60)
                    continue
                self._wait(5)

        self._thread = threading.Thread(target=loop, name="sieve-ingest", daemon=True)
        self._thread.start()

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake early when stopped or asked to pull."""
        deadline = time.time() + seconds
        while not self._stop.is_set() and time.time() < deadline:
            if self._kick.wait(min(1.0, max(0.0, deadline - time.time()))):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # -- single video ------------------------------------------------------

    def ensure_video(self, video_id: str) -> dict | None:
        row = self.db.get_video(video_id)
        if row is None:
            try:
                self.db.upsert_videos(tagged([self.api.video(video_id)], "lookup"))
            except Exception as exc:
                log.warning("could not fetch %s: %s", video_id, exc)
                return None
            row = self.db.get_video(video_id)
        if row is not None and self.db.one("SELECT 1 FROM scores WHERE video_id = ?", (video_id,)) is None:
            self.score_pending(limit=1)
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Importers
# --------------------------------------------------------------------------


def import_subscriptions(db: Database, payload: Any) -> int:
    """Accept Upstream, NewPipe, FreeTube or OPML-derived subscription lists."""
    entries: list[tuple[str, str]] = []

    if isinstance(payload, dict):
        if "subscriptions" in payload:  # Upstream / NewPipe export
            for item in payload["subscriptions"]:
                if isinstance(item, str):
                    entries.append((item, ""))
                elif isinstance(item, dict):
                    url = item.get("url", "")
                    cid = item.get("id") or item.get("channelId") or _channel_from_url(url)
                    if cid:
                        entries.append((cid, item.get("name") or item.get("author") or ""))
        elif "profiles" in payload:  # FreeTube
            for profile in payload.get("profiles", []):
                for sub in profile.get("subscriptions", []):
                    if sub.get("id"):
                        entries.append((sub["id"], sub.get("name", "")))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                entries.append((item, ""))
            elif isinstance(item, dict):
                cid = item.get("id") or item.get("channelId") or _channel_from_url(item.get("url", ""))
                if cid:
                    entries.append((cid, item.get("name") or item.get("author") or ""))

    now = int(time.time())
    db.executemany(
        "INSERT INTO subscriptions(channel_id, name, weight, added_at) VALUES(?,?,1.0,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), subscriptions.name)",
        [(cid, name, now) for cid, name in entries if cid],
    )
    return len(entries)


IMPORT_FETCH_LIMIT = 50


def import_history(db: Database, payload: Any, api: Upstream | None = None) -> int:
    """Import watch history.

    Accepts Invidious' `watch_history` export (a list of video ids), and Google
    Takeout's `watch-history.json` (which carries timestamps but no progress —
    those rows are recorded as a conservative 0.6 completion so they inform the
    model without pretending to precision we do not have).
    """
    events: list[tuple[str, int, float]] = []

    if isinstance(payload, dict) and "watch_history" in payload:
        now = int(time.time())
        for index, vid in enumerate(payload["watch_history"]):
            events.append((vid, now - index * 600, 0.6))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                events.append((item, int(time.time()), 0.6))
                continue
            if not isinstance(item, dict):
                continue
            vid = item.get("videoId") or item.get("id") or _video_from_url(
                item.get("titleUrl") or item.get("url") or ""
            )
            if not vid:
                continue
            when = item.get("time") or item.get("watched_at") or ""
            events.append((vid, _parse_time(when), float(item.get("progress", 0.6))))

    known = set(db.get_videos([e[0] for e in events]))
    rows = []
    fetched = 0
    for vid, when, progress in events:
        rows.append((vid, when, progress, 0, "import"))
        # Fetch a few details now and leave the rest to Ingestor.backfill: a
        # Takeout file can hold thousands of videos, and fetching each one
        # inside the upload request made the page hang until it timed out.
        if vid in known or api is None or fetched >= IMPORT_FETCH_LIMIT:
            continue
        try:
            db.upsert_videos(tagged([api.video(vid)], "history"))
            known.add(vid)
            fetched += 1
        except UpstreamUnavailable:
            api = None  # nothing is answering; do not wait on it once per video
        except Exception:
            continue

    db.executemany(
        "INSERT INTO history(video_id, watched_at, progress, dwell, origin) VALUES(?,?,?,?,?)", rows
    )
    return len(rows)


def import_playlist(db: Database, api: Upstream, playlist_id: str) -> int:
    data = api.playlist(playlist_id)
    videos = data.get("videos") or []
    db.upsert_videos(tagged(videos, "playlist"))
    db.execute(
        "INSERT INTO playlists(id, title, video_ids, updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, video_ids=excluded.video_ids, "
        "updated_at=excluded.updated_at",
        (playlist_id, data.get("title", ""),
         json.dumps([v.get("videoId") for v in videos if v.get("videoId")]), int(time.time())),
    )
    return len(videos)


def _channel_from_url(url: str) -> str:
    if not url:
        return ""
    for marker in ("/channel/", "/c/", "/user/"):
        if marker in url:
            return url.split(marker, 1)[1].split("/")[0].split("?")[0]
    return ""


def _video_from_url(url: str) -> str:
    if "v=" in url:
        return url.split("v=", 1)[1].split("&")[0]
    if "youtu.be/" in url:
        return url.split("youtu.be/", 1)[1].split("?")[0]
    return ""


def _parse_time(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(time.mktime(time.strptime(value[:26], fmt)))
            except ValueError:
                continue
    return int(time.time())
