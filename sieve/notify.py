"""Telling you things: new uploads from chosen channels, and a digest.

Alerts. Turn on the bell for a channel (Channels page, or a video's page) and
each sync records its new uploads as alerts. Turning it on marks the channel's
existing videos as already seen, so you hear about new uploads, not its back
catalogue.

Digest. Every N hours, a short summary: how many new videos, your top picks,
new uploads from alerted channels. Useful with "new videos every X": the week's
gate opens with a note of what is waiting.

Delivery, all optional: shown on the homepage; RSS at /feeds/alerts.xml and
/feeds/digest.xml; and a webhook — ntfy.sh style (a POST of plain text, which
the free ntfy app turns into a phone notification) or JSON for anything else.
Webhooks are sent from the background worker, never from a page load.
"""

from __future__ import annotations

import logging
import time
from email.utils import formatdate
from typing import Any
from xml.sax.saxutils import escape

from .config import resolve_settings
from .db import Database

log = logging.getLogger("sieve.notify")


def settings(db: Database) -> dict[str, Any]:
    return resolve_settings(db.get_setting("settings", {}) or {}).get("notify", {})


# ---------------------------------------------------------------- alerts


def set_alert(db: Database, channel_id: str, on: bool, name: str = "") -> None:
    db.execute("INSERT INTO channel_prefs(channel_id, name, alert, updated_at) VALUES(?,?,?,?) "
               "ON CONFLICT(channel_id) DO UPDATE SET alert = excluded.alert, updated_at = excluded.updated_at",
               (channel_id, name, int(on), int(time.time())))
    if on:
        # What is already in the catalogue is not news.
        db.execute("INSERT OR IGNORE INTO alerts(video_id, channel_id, created_at, seen, sent) "
                   "SELECT id, author_id, ?, 1, 1 FROM videos WHERE author_id = ?",
                   (int(time.time()), channel_id))


def alerted_channels(db: Database) -> set[str]:
    return {r["channel_id"] for r in db.query("SELECT channel_id FROM channel_prefs WHERE alert = 1")}


def collect(db: Database) -> int:
    """Record new uploads from alerted channels. Returns how many."""
    now = int(time.time())
    cur = db.execute(
        "INSERT OR IGNORE INTO alerts(video_id, channel_id, created_at) "
        "SELECT v.id, v.author_id, ? FROM videos v JOIN channel_prefs p ON p.channel_id = v.author_id "
        "WHERE p.alert = 1 AND (v.published = 0 OR v.published > ?)", (now, now - 14 * 86400))
    return cur.rowcount or 0


def unseen(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT a.video_id, a.created_at, v.title, v.author FROM alerts a JOIN videos v ON v.id = a.video_id "
        "WHERE a.seen = 0 ORDER BY a.created_at DESC LIMIT ?", (limit,))]


def mark_seen(db: Database) -> int:
    return db.execute("UPDATE alerts SET seen = 1 WHERE seen = 0").rowcount or 0


# ---------------------------------------------------------------- digest


def build_digest(db: Database) -> dict[str, Any]:
    from . import ranking

    last = db.get_setting("digest_last", {}) or {}
    since = int(last.get("at") or time.time() - 7 * 86400)
    new = db.scalar("SELECT COUNT(*) FROM videos WHERE fetched_at > ? AND length(id) = 11", (since,), 0)
    picks = ranking.recommend(db, resolve_settings(db.get_setting("settings", {}) or {}),
                              record=False, limit=5).items
    alerts = unseen(db, 10)
    lines = [f"{new} new video{'' if new == 1 else 's'} since {time.strftime('%a %d %b', time.localtime(since))}."]
    if picks:
        lines.append("Top picks:")
        lines += [f"• {c.video['title']} — {c.video.get('author') or ''}" for c in picks[:3]]
    if alerts:
        lines.append(f"{len(alerts)} new from channels you follow closely.")
    digest = {"at": int(time.time()), "since": since, "new": new, "text": "\n".join(lines),
              "picks": [{"id": c.id, "title": c.video["title"]} for c in picks[:5]],
              "alerts": len(alerts), "sent": False}
    db.set_setting("digest_last", digest)
    return digest


def digest_due(db: Database) -> bool:
    every = int(settings(db).get("digest_hours", 0) or 0)
    if every <= 0:
        return False
    last = db.get_setting("digest_last", {}) or {}
    return time.time() - int(last.get("at") or 0) >= every * 3600


# -------------------------------------------------------------- delivery


def send(db: Database, title: str, text: str, click: str = "", payload: dict | None = None,
         client=None) -> bool:
    """POST to the webhook, if one is set. True when it was accepted."""
    cfg = settings(db)
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        return False
    import httpx

    own = client is None
    client = client or httpx.Client(timeout=8)
    try:
        if cfg.get("webhook_style", "ntfy") == "json":
            response = client.post(url, json={"title": title, "text": text, "url": click, **(payload or {})})
        else:
            headers = {"Title": title.encode("utf-8").decode("latin-1", "replace")}
            if click:
                headers["Click"] = click
            response = client.post(url, content=text.encode("utf-8"), headers=headers)
        response.raise_for_status()
        return True
    except Exception as exc:
        log.info("webhook failed: %s", exc)
        return False
    finally:
        if own:
            client.close()


def deliver_pending(db: Database, base_url: str = "", client=None) -> int:
    """Send unsent alerts (one message) and an unsent digest."""
    cfg = settings(db)
    sent = 0
    if cfg.get("push_alerts", True):
        rows = db.query(
            "SELECT a.video_id, v.title, v.author FROM alerts a JOIN videos v ON v.id = a.video_id "
            "WHERE a.sent = 0 ORDER BY a.created_at LIMIT 20")
        if rows:
            text = "\n".join(f"{r['author']}: {r['title']}" for r in rows)
            first = rows[0]["video_id"]
            title = f"{len(rows)} new upload{'' if len(rows) == 1 else 's'}"
            click = f"{base_url}/video/{first}" if base_url else f"https://www.youtube.com/watch?v={first}"
            if send(db, title, text, click, {"videos": [r["video_id"] for r in rows]}, client):
                marks = ",".join("?" * len(rows))
                db.execute(f"UPDATE alerts SET sent = 1 WHERE video_id IN ({marks})", [r["video_id"] for r in rows])
                sent += 1
    digest = db.get_setting("digest_last", {}) or {}
    if digest and not digest.get("sent") and send(db, "Sieve digest", digest.get("text", ""),
                                                  base_url or "", {"digest": digest}, client):
        db.set_setting("digest_last", {**digest, "sent": True})
        sent += 1
    return sent


# ------------------------------------------------------------------- RSS


def rss(db: Database, which: str, base_url: str) -> str:
    items = []
    if which == "alerts":
        for r in db.query("SELECT a.video_id, a.created_at, v.title, v.author FROM alerts a "
                          "JOIN videos v ON v.id = a.video_id ORDER BY a.created_at DESC LIMIT 50"):
            link = f"https://www.youtube.com/watch?v={r['video_id']}"
            items.append((f"{r['author']}: {r['title']}", link, "", r["created_at"], r["video_id"]))
        title = "Sieve: new uploads"
    else:
        d = db.get_setting("digest_last", {}) or {}
        if d:
            items.append((f"Digest, {time.strftime('%d %b', time.localtime(d['at']))}", base_url or "",
                          d.get("text", ""), d["at"], f"digest-{d['at']}"))
        title = "Sieve: digest"
    body = "".join(
        f"<item><title>{escape(t)}</title><link>{escape(link)}</link>"
        f"<description>{escape(desc)}</description><pubDate>{formatdate(at)}</pubDate>"
        f"<guid isPermaLink=\"false\">{escape(guid)}</guid></item>"
        for t, link, desc, at, guid in items)
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><rss version=\"2.0\"><channel>"
            f"<title>{escape(title)}</title><link>{escape(base_url)}</link>"
            f"<description>{escape(title)}</description>{body}</channel></rss>")
