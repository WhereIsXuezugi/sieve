// ==UserScript==
// @name         Sieve: count what I watch here
// @namespace    sieve
// @version      1.0
// @description  Tells your Sieve how much of each video you watch on YouTube or Invidious, so its history, interests and "next episode" stay right.
// @match        https://www.youtube.com/*
// @match        https://m.youtube.com/*
// @match        https://www.youtube-nocookie.com/*
// __INVIDIOUS_MATCH__
// @grant        GM_xmlhttpRequest
// @connect      __SIEVE_HOST__
// @run-at       document-idle
// ==/UserScript==

// Served by Sieve at /userscript/sieve.user.js with your server's address
// filled in. It sends {video_id, progress, session} to /api/progress every 20
// seconds while a video plays, and when you pause or leave. Nothing else.

(function () {
  'use strict';
  const SIEVE = '__SIEVE_URL__';
  let session = null, lastId = null, lastSent = 0;

  function videoId() {
    const url = new URL(location.href);
    if (url.searchParams.get('v')) return url.searchParams.get('v');
    const m = url.pathname.match(/\/(?:shorts|embed|live)\/([\w-]{11})/);
    return m ? m[1] : null;
  }

  function send(final) {
    const video = document.querySelector('video');
    const id = videoId();
    if (!video || !id || !video.duration || !isFinite(video.duration)) return;
    if (id !== lastId) { lastId = id; session = `us-${id}-${Date.now()}`; }
    const now = Date.now();
    if (!final && now - lastSent < 19000) return;
    lastSent = now;
    GM_xmlhttpRequest({
      method: 'POST',
      url: `${SIEVE}/api/progress`,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ video_id: id, progress: Math.min(1, video.currentTime / video.duration),
                             dwell: Math.round(video.currentTime), session }),
    });
  }

  setInterval(() => {
    const video = document.querySelector('video');
    if (video && !video.paused) send(false);
  }, 5000);
  document.addEventListener('pause', () => send(true), true);
  document.addEventListener('ended', () => send(true), true);
  window.addEventListener('pagehide', () => send(true));
})();
