// Sieve player.
//
// Embeds YouTube's privacy-enhanced player (youtube-nocookie.com) inside a
// Sieve page. That fixes the embed error — YouTube will not play a bare
// /embed/ URL opened on its own, and shows "Error 153" when the referrer is
// missing, which Sieve strips everywhere else — and makes three things work
// without any change to Invidious:
//
//   * sponsor segments skip themselves, from Sieve's SponsorBlock data
//   * the video resumes where you left off
//   * how much you actually watched is recorded, as one history row per
//     viewing, so completion weighting runs on real numbers
//
// It talks to the player with the same postMessage protocol YouTube's IFrame
// API uses, without loading that API: the script lives on youtube.com and
// would contact Google even on the no-cookie domain.

(function () {
  'use strict';
  const root = document.querySelector('[data-player]');
  if (!root) return;

  const videoId = root.dataset.video;
  const start = Number(root.dataset.start || 0);
  const frame = root.querySelector('iframe');
  const note = document.querySelector('#player-note');
  const fallback = document.querySelector('#player-fallback');
  const EMBED = 'https://www.youtube-nocookie.com';
  const session = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2))
    .replace(/-/g, '').slice(0, 32);

  // Built here rather than in the template: `origin` must be the address the
  // browser sees, which behind a reverse proxy the server does not know.
  const params = new URLSearchParams({
    enablejsapi: '1', rel: '0', playsinline: '1', modestbranding: '1',
    origin: location.origin, widget_referrer: location.origin,
  });
  if (start > 0) params.set('start', String(Math.floor(start)));
  frame.src = `${EMBED}/embed/${encodeURIComponent(videoId)}?${params}`;

  let duration = Number(root.dataset.duration || 0);
  let furthest = 0;          // furthest point reached, in seconds
  let watched = 0;           // seconds actually spent playing
  let lastTick = null;
  let playing = false;
  let skip = [];

  function send(func, args) {
    frame.contentWindow.postMessage(JSON.stringify({ event: 'command', func, args: args || [] }), EMBED);
  }

  function say(text, kind) {
    if (!note) return;
    note.textContent = text;
    note.dataset.kind = kind || '';
    note.hidden = !text;
  }

  // -- sponsor segments -----------------------------------------------------

  fetch(`/api/sponsorblock/${encodeURIComponent(videoId)}`)
    .then((r) => (r.ok ? r.json() : { segments: [], skip: [] }))
    .then((data) => {
      skip = (data.segments || [])
        .filter((s) => (data.skip || []).includes(s.category) && s.action === 'skip' && s.end > s.start)
        .sort((a, b) => a.start - b.start);
      if (skip.length) {
        const total = Math.round(skip.reduce((n, s) => n + (s.end - s.start), 0));
        say(`${skip.length} segment${skip.length > 1 ? 's' : ''} (${total}s) will be skipped.`, 'info');
      }
    })
    .catch(() => {});

  // -- the player's messages ------------------------------------------------

  frame.addEventListener('load', () => {
    // Ask the player to start reporting state; it answers with infoDelivery.
    frame.contentWindow.postMessage(JSON.stringify({ event: 'listening', id: 'sieve', channel: 'widget' }), EMBED);
  });

  window.addEventListener('message', (event) => {
    if (event.origin !== EMBED) return;
    let data;
    try { data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data; }
    catch (ignored) { return; }
    if (!data || typeof data !== 'object') return;

    if (data.event === 'onError') {
      failed(Number(data.info));
      return;
    }
    const info = data.info || {};
    if (typeof info.duration === 'number' && info.duration > 0) duration = info.duration;
    // Time before state: the "ended" message carries the final time too, and
    // reporting before applying it recorded the previous tick (97.5%, not 100%).
    if (typeof info.currentTime === 'number') onTime(info.currentTime);
    if (typeof info.playerState === 'number') {
      const now = info.playerState === 1;   // 1 = playing
      if (now && !playing) lastTick = performance.now();
      if (!now && playing) tick();
      playing = now;
      if (info.playerState === 0) report(true); // 0 = ended
    }
  });

  function tick() {
    if (lastTick !== null) watched += (performance.now() - lastTick) / 1000;
    lastTick = playing ? performance.now() : null;
  }

  // Chapters: a click seeks; the chapter playing now is marked.
  const chapterButtons = Array.from(document.querySelectorAll('[data-seek]'));
  chapterButtons.forEach((b) => b.addEventListener('click', () => send('seekTo', [Number(b.dataset.seek), true])));
  function markChapter(t) {
    let current = null;
    for (const b of chapterButtons) if (Number(b.dataset.seek) <= t) current = b;
    chapterButtons.forEach((b) => b.classList.toggle('now', b === current));
  }

  function onTime(t) {
    markChapter(t);
    tick();
    furthest = Math.max(furthest, t);
    const segment = skip.find((s) => t >= s.start && t < s.end - 0.3);
    if (segment) {
      send('seekTo', [segment.end, true]);
      say(`Skipped ${segment.category.replace('_', ' ')} (${Math.round(segment.end - segment.start)}s).`, 'info');
    }
  }

  // Error codes from YouTube's player. 101 and 150 mean the owner does not
  // allow embedding; nothing Sieve can do fixes that, so say so and offer
  // the other ways to open the video.
  function failed(code) {
    const reasons = {
      2: 'YouTube did not recognise this video id.',
      5: 'The browser could not play this video.',
      100: 'This video is private or has been removed.',
      101: 'The uploader does not allow this video to be played outside YouTube.',
      150: 'The uploader does not allow this video to be played outside YouTube.',
      153: 'YouTube refused the embed because no referrer was sent. If a browser extension or setting strips referrers, allow them for this page.',
    };
    say(reasons[code] || `YouTube reported error ${code}.`, 'error');
    if (fallback) fallback.hidden = false;
  }

  // -- reporting progress --------------------------------------------------

  let lastSent = -1;
  function report(final) {
    tick();
    if (!duration || furthest <= 0) return;
    const progress = Math.min(1, furthest / duration);
    if (!final && Math.abs(progress - lastSent) < 0.01) return;
    lastSent = progress;
    const body = JSON.stringify({ video_id: videoId, progress, dwell: Math.round(watched), session });
    // sendBeacon survives the tab closing; fetch is the fallback.
    if (!(navigator.sendBeacon && navigator.sendBeacon('/api/progress', new Blob([body], { type: 'application/json' })))) {
      fetch('/api/progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true })
        .catch(() => {});
    }
  }

  setInterval(() => report(false), 15000);
  window.addEventListener('pagehide', () => report(true));
  document.addEventListener('visibilitychange', () => { if (document.hidden) report(true); });
})();
