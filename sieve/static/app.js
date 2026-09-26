// Sieve — the only JavaScript on the page.
// Everything renders server-side; this handles the interactions that would
// otherwise need a round trip: feedback buttons, live slider readouts, channel
// priority, and the rule editor's validate-as-you-type.

async function post(url, body, method) {
  const response = await fetch(url, {
    method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) {
    // FastAPI puts the reason in `detail`. Surface it: "that playlist is
    // private" is useful, "failed" is not.
    let message = `request failed (${response.status})`;
    try {
      const data = await response.json();
      if (typeof data.detail === 'string') message = data.detail;
      else if (Array.isArray(data.detail) && data.detail[0]) message = data.detail[0].msg;
    } catch (ignored) { /* not JSON; keep the status line */ }
    throw new Error(message);
  }
  return response.json();
}

function flash(element, text, ok) {
  // One message per control: a second click replaces it rather than stacking.
  const previous = element.nextElementSibling;
  if (previous && previous.classList.contains('flash')) previous.remove();
  const note = document.createElement('span');
  note.textContent = text;
  note.className = ok === false ? 'flash bad' : 'flash good';
  note.setAttribute('role', 'status');
  element.after(note);
  // Long enough to read: about a second per six words, never under 3 s.
  const words = String(text).split(/\s+/).length;
  setTimeout(() => note.remove(), Math.max(ok === false ? 6000 : 3000, words * 170 + 1500));
}

// Turn an API result into a short human line: {"ok":true,"scored":40} -> "scored 40".
function summarise(result) {
  if (!result || typeof result !== 'object') return 'done';
  const parts = [];
  // A restore, a backup, a reset of pulled videos: say what happened.
  if (result.restored) return `Restored ${result.restored}. The state before it is saved as ${result.safety_backup}.`;
  if ('backup' in result && Object.keys(result).length <= 2) return result.backup ? `Backed up as ${result.backup}.` : 'Backed up.';
  if (typeof result.videos === 'number' && 'channels_forgotten' in result) {
    return `Deleted ${result.videos} pulled video${result.videos === 1 ? '' : 's'}. The next Fetch starts from scratch.`;
  }
  if (typeof result.forgotten === 'number') return 'Request count reset to zero.';
  if (typeof result.rescored === 'number') {
    return `Re-checked ${result.rescored} videos: ${result.on_page} on the homepage, ${result.pass} pass your filters, ${result.filtered} filtered out.`;
  }
  // A fetch or sync: how many new videos, then why it stopped early, if it did.
  if (typeof result.new === 'number' && typeof result.pulls === 'number') {
    let line = `${result.new} new video${result.new === 1 ? '' : 's'}`;
    if (!result.new && result.seen) line += ` (${result.seen} found, all already known)`;
    line += `, ${result.pulls} request${result.pulls === 1 ? '' : 's'}`;
    return result.stopped ? `${line}. Stopped early: ${result.stopped}` : line;
  }
  for (const [key, value] of Object.entries(result)) {
    if (key === 'ok') continue;
    if (value === 0 && parts.length) continue;   // "playlists 0" says nothing
    if (typeof value === 'number' || typeof value === 'string') {
      parts.push(`${key.replace(/_/g, ' ')} ${value}`);
    } else if (value && typeof value === 'object' && !Array.isArray(value)) {
      const inner = Object.entries(value)
        .filter(([, v]) => typeof v === 'number')
        .map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`);
      if (inner.length) parts.push(inner.join(', '));
    }
  }
  return parts.slice(0, 3).join(' · ') || 'done';
}

function setStatus(element, text, ok) {
  if (!element) return;
  element.textContent = text;
  element.style.color = ok === false ? 'var(--clay)' : '';
}

/* -- feedback ------------------------------------------------------------ */
// More and Less are one choice per video: choosing one clears the other, and
// choosing the chosen one again takes it back. The server answers with the
// resulting state, which every open Sieve page is told about at once — so a
// choice made in the player shows on the homepage without a reload.

const feedbackChannel = 'BroadcastChannel' in window ? new BroadcastChannel('sieve-feedback') : null;

function showFeedback(videoId, state) {
  document.querySelectorAll(`[data-video="${CSS.escape(videoId)}"] [data-feedback="more"], ` +
                            `[data-video="${CSS.escape(videoId)}"] [data-feedback="less"], ` +
                            `[data-feedback-for="${CSS.escape(videoId)}"] [data-feedback="more"], ` +
                            `[data-feedback-for="${CSS.escape(videoId)}"] [data-feedback="less"]`)
    .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.feedback === state)));
}

if (feedbackChannel) feedbackChannel.onmessage = (e) => showFeedback(e.data.video, e.data.state);
window.addEventListener('storage', (e) => {    // browsers without BroadcastChannel
  if (e.key === 'sieve-feedback' && e.newValue) {
    const data = JSON.parse(e.newValue);
    showFeedback(data.video, data.state);
  }
});

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-feedback]');
  if (!button) return;
  const holder = button.closest('[data-video], [data-feedback-for]');
  const videoId = holder.dataset.video || holder.dataset.feedbackFor;
  const kind = button.dataset.feedback;
  button.disabled = true;
  try {
    const result = await post('/api/feedback', { video_id: videoId, kind });
    if (kind === 'more' || kind === 'less') {
      showFeedback(videoId, result.state);
      const message = { video: videoId, state: result.state };
      if (feedbackChannel) feedbackChannel.postMessage(message);
      try { localStorage.setItem('sieve-feedback', JSON.stringify({ ...message, at: Date.now() })); } catch (ignored) { /* private */ }
    } else {
      button.setAttribute('aria-pressed', 'true');
    }
    const applied = Object.entries(result.applied || {});
    if (applied.length) flash(button, applied.map(([k, v]) => `${k} → ${v}`).join(', '));
  } catch (err) {
    flash(button, err.message, false);
  } finally {
    button.disabled = false;
  }
});

// Hide: gone from the homepage now, and recorded so it stays gone.
document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-hide]');
  if (!button) return;
  const card = button.closest('[data-video]');
  button.disabled = true;
  try {
    await post('/api/hide', { kind: 'video', value: card.dataset.video });
    card.classList.add('leaving');
    setTimeout(() => card.remove(), 250);
    toast('Hidden. It will not come back.');
  } catch (err) {
    button.disabled = false;
    flash(button, err.message, false);
  }
});

/* -- sliders ------------------------------------------------------------- */

function bindRange(input) {
  const output = input.parentElement.querySelector('output');
  if (!output) return;
  const suffix = input.dataset.suffix || '';
  const update = () => { output.textContent = input.value + suffix; };
  input.addEventListener('input', update);
  update();
}
document.querySelectorAll('input[type="range"]').forEach(bindRange);

// Source sliders are relative weights; show each as a live percentage so the
// numbers mean what people assume they mean.
function updateSourceShares() {
  const inputs = Array.from(document.querySelectorAll('[data-source-slider]'));
  if (!inputs.length) return;
  const total = inputs.reduce((sum, el) => sum + Number(el.value), 0) || 1;
  inputs.forEach((el) => {
    const share = document.querySelector(`[data-share="${el.dataset.sourceSlider}"]`);
    if (share) share.textContent = Math.round((Number(el.value) / total) * 100) + '%';
  });
}
document.querySelectorAll('[data-source-slider]').forEach((el) => {
  el.addEventListener('input', updateSourceShares);
});
updateSourceShares();

/* -- channel priority ---------------------------------------------------- */

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-priority]');
  if (button) {
    const row = button.closest('[data-channel]');
    const value = Number(button.dataset.priority);
    await post('/api/channels/' + row.dataset.channel, { priority: value });
    row.querySelectorAll('[data-priority]').forEach((b) => {
      b.setAttribute('aria-pressed', String(Number(b.dataset.priority) === value));
    });
    return;
  }

  const listing = event.target.closest('[data-listing]');
  if (listing) {
    const row = listing.closest('[data-channel]');
    const next = listing.dataset.listing;
    const current = row.dataset.currentListing || 'neutral';
    const value = current === next ? 'neutral' : next;
    await post('/api/channels/' + row.dataset.channel, { listing: value });
    row.dataset.currentListing = value;
    row.querySelectorAll('[data-listing]').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.listing === value));
    });
    const exempt = row.querySelector('[data-channel-exempt]');
    if (exempt) exempt.disabled = value !== 'allow';
    flash(listing, value === 'neutral' ? 'cleared' : value);
  }
});

/* -- interests ----------------------------------------------------------- */

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-interest-action]');
  if (!button) return;
  const row = button.closest('[data-tag]');
  const action = button.dataset.interestAction;
  if (action === 'remove') {
    await post('/api/interests', { action: 'remove', tag: row.dataset.tag });
    row.remove();
  } else {
    const weight = Number(button.dataset.weight);
    await post('/api/interests', { action: 'set', tag: row.dataset.tag, weight });
    const cell = row.querySelector('[data-weight-cell]');
    if (cell) cell.textContent = weight.toFixed(2);
    flash(button, 'saved');
  }
});

const interestForm = document.querySelector('#add-interest');
if (interestForm) {
  interestForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const tag = interestForm.querySelector('[name=tag]').value.trim();
    const weight = Number(interestForm.querySelector('[name=weight]').value);
    if (!tag) return;
    await post('/api/interests', { action: 'set', tag, weight });
    location.reload();
  });
}

/* -- rule editor --------------------------------------------------------- */

const ruleBox = document.querySelector('#rule-json');
if (ruleBox) {
  const status = document.querySelector('#rule-status');
  const preview = document.querySelector('#rule-preview');
  let timer = null;

  const validate = async () => {
    try {
      const result = await post('/api/rules/validate', { expr: ruleBox.value });
      if (result.ok) {
        status.textContent = result.summary || 'empty rule — nothing is filtered';
        status.className = 'tag allow';
        if (validate.edited) document.dispatchEvent(new CustomEvent('sieve:rule-valid'));
        const kept = result.preview.kept.map((v) => `<li>${escapeHtml(v.title)}</li>`).join('');
        const dropped = result.preview.dropped.map((v) => `<li>${escapeHtml(v.title)}</li>`).join('');
        preview.innerHTML =
          `<div class="cols-2"><div><h3>Would keep (${Math.round(result.preview.kept_share * 100)}% of catalogue)</h3><ul>${kept || '<li class="muted">nothing</li>'}</ul></div>` +
          `<div><h3>Would drop</h3><ul>${dropped || '<li class="muted">nothing</li>'}</ul></div></div>`;
      } else {
        status.textContent = result.error;
        status.className = 'tag block';
      }
    } catch (err) {
      status.textContent = 'could not validate';
      status.className = 'tag block';
    }
  };

  ruleBox.addEventListener('input', () => {
    validate.edited = true;
    clearTimeout(timer);
    timer = setTimeout(validate, 600);
  });
  validate();

  // Autosave: a rule that validates is saved; one that does not never is.
  const saved = document.querySelector('#rule-saved');
  const enabled = document.querySelector('#rule-enabled');
  let lastSaved = null;

  const saveRule = async () => {
    const state = JSON.stringify([ruleBox.value, enabled.checked]);
    if (state === lastSaved) return;
    try {
      await post('/api/rules', { expr: ruleBox.value, enabled: enabled.checked });
      lastSaved = state;
      saved.textContent = enabled.checked ? 'Saved and applied.' : 'Saved, not applied.';
    } catch (err) {
      saved.textContent = err.message;
    }
  };
  lastSaved = JSON.stringify([ruleBox.value, enabled.checked]);
  document.addEventListener('sieve:rule-valid', saveRule);
  enabled.addEventListener('change', async () => {
    const result = await post('/api/rules/validate', { expr: ruleBox.value }).catch(() => ({ ok: false }));
    if (result.ok) saveRule();
    else saved.textContent = 'Fix the rule first; the switch saves with it.';
  });

  document.querySelectorAll('[data-snippet]').forEach((button) => {
    button.addEventListener('click', () => {
      ruleBox.value = button.dataset.snippet;
      ruleBox.dispatchEvent(new Event('input'));
    });
  });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/* -- critique ------------------------------------------------------------ */

const critiqueButton = document.querySelector('#critique');
if (critiqueButton) {
  critiqueButton.addEventListener('click', async () => {
    const target = document.querySelector('#critique-text');
    critiqueButton.disabled = true;
    target.textContent = 'Thinking…';
    try {
      const result = await post('/api/critique', {});
      target.textContent = result.text;
    } finally {
      critiqueButton.disabled = false;
    }
  });
}

/* -- action buttons ------------------------------------------------------ */
// <button data-action="/api/..." data-body='{"all":true}' data-confirm="…" data-reload>
//   data-method="DELETE"   another HTTP method
//   data-prompt="…"        ask for a typed word, sent as `confirm` in the body
//   data-toast             report in the corner rather than beside the button
//                          (the header's buttons, where there is no room)
//   data-reload            reload afterwards; the message survives the reload

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', async () => {
    if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
    const body = button.dataset.body ? JSON.parse(button.dataset.body) : {};
    if (button.dataset.prompt) {
      const typed = window.prompt(button.dataset.prompt);
      if (typed === null) return;
      body.confirm = typed;
    }
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Working…';
    const tracked = /^\/api\/(fetch|sync)/.test(button.dataset.action);
    if (tracked) watchProgress();
    const report = (text, ok) => ('toast' in button.dataset ? toast(text, ok) : flash(button, text, ok));
    try {
      const result = await post(button.dataset.action, body, button.dataset.method);
      button.textContent = original;
      const message = summarise(result);
      if ('reload' in button.dataset) {
        // Shown again after the reload: a result that vanished with the page
        // after 0.7 s could never be read.
        try { sessionStorage.setItem('sieve-toast', message); } catch (ignored) { /* private mode */ }
        setTimeout(() => location.reload(), 150);
      } else {
        report(message);
      }
    } catch (err) {
      button.textContent = original;
      report(err.message, false);
    } finally {
      button.disabled = false;
    }
  });
});

/* -- toasts --------------------------------------------------------------- */

function toast(text, ok) {
  let box = document.querySelector('.toast');
  if (!box) {
    box = document.createElement('div');
    box.className = 'toast';
    box.setAttribute('role', 'status');
    document.body.append(box);
  }
  box.textContent = text;
  box.classList.toggle('bad', ok === false);
  box.classList.add('show');
  clearTimeout(box.timer);
  const words = String(text).split(/\s+/).length;
  box.timer = setTimeout(() => box.classList.remove('show'),
                         Math.max(ok === false ? 7000 : 4000, words * 200 + 2000));
}

try {
  const carried = sessionStorage.getItem('sieve-toast');
  if (carried) {
    sessionStorage.removeItem('sieve-toast');
    toast(carried);
  }
} catch (ignored) { /* storage unavailable */ }

document.querySelectorAll('form[data-confirm]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
/* -- file imports -------------------------------------------------------- */

document.querySelectorAll('[data-upload]').forEach((input) => {
  input.addEventListener('change', async () => {
    if (!input.files.length) return;
    const data = new FormData();
    data.append('file', input.files[0]);
    try {
      const response = await fetch(input.dataset.upload, { method: 'POST', body: data });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `upload failed (${response.status})`);
      flash(input, `imported ${result.imported}`);
    } catch (err) {
      flash(input, err.message, false);
    } finally {
      input.value = '';
    }
  });
});

/* -- import a shared profile from a URL ---------------------------------- */

const profileFetch = document.querySelector('#profile-fetch');
if (profileFetch) {
  profileFetch.addEventListener('click', async () => {
    const url = document.querySelector('#profile-url').value.trim();
    const status = document.querySelector('#profile-fetch-status');
    if (!url) return;
    profileFetch.disabled = true;
    status.textContent = 'Fetching…';
    try {
      const result = await post('/api/profile/fetch', { url, merge: true });
      const a = result.applied;
      status.textContent =
        `Imported ${result.name || 'profile'}: ${a.settings} settings, ` +
        `${a.interests} interests, ${a.channels} channel rules. Reloading…`;
      setTimeout(() => location.reload(), 1200);
    } catch (err) {
      status.textContent = err.message;
    } finally {
      profileFetch.disabled = false;
    }
  });
}

/* -- playlists ------------------------------------------------------------ */

const playlistImport = document.querySelector('#playlist-import');
if (playlistImport) {
  const input = document.querySelector('#playlist-ref');
  const status = document.querySelector('#playlist-status');
  const run = async () => {
    const ref = input.value.trim();
    if (!ref) { input.focus(); return; }
    playlistImport.disabled = true;
    setStatus(status, 'Fetching from your instance…');
    try {
      const result = await post('/api/import/playlist', { playlist: ref });
      setStatus(status, `Imported “${result.title || result.id}”: ${result.imported} videos. Reloading…`);
      setTimeout(() => location.reload(), 900);
    } catch (err) {
      setStatus(status, err.message, false);
    } finally {
      playlistImport.disabled = false;
    }
  };
  playlistImport.addEventListener('click', run);
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') run(); });
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-playlist-action]');
  if (!button) return;
  const row = button.closest('[data-playlist]');
  const id = row.dataset.playlist;
  const action = button.dataset.playlistAction;
  button.disabled = true;
  try {
    if (action === 'refresh') {
      const result = await post('/api/import/playlist', { playlist: id });
      flash(button, `${result.imported} videos`);
    } else if (action === 'remove') {
      if (!window.confirm('Forget this playlist? Its videos stay in the catalogue.')) return;
      await post('/api/playlists/' + encodeURIComponent(id), undefined, 'DELETE');
      row.remove();
    } else if (action === 'homepage') {
      await post('/api/settings', { homepage: { mode: 'playlist', playlist_id: id } });
      location.href = '/';
    }
  } catch (err) {
    flash(button, err.message, false);
  } finally {
    button.disabled = false;
  }
});

/* -- saved profiles --------------------------------------------------------- */

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-profile-delete]');
  if (!button) return;
  const row = button.closest('[data-profile]');
  if (!window.confirm(`Delete the saved profile “${row.dataset.profile}”?`)) return;
  try {
    await post('/api/profiles/' + encodeURIComponent(row.dataset.profile), undefined, 'DELETE');
    row.remove();
  } catch (err) {
    flash(button, err.message, false);
  }
});

const profileFile = document.querySelector('#profile-file');
if (profileFile) {
  profileFile.addEventListener('change', async () => {
    if (!profileFile.files.length) return;
    const status = document.querySelector('#profile-fetch-status');
    try {
      const profile = JSON.parse(await profileFile.files[0].text());
      const result = await post('/api/profile/import', { profile, merge: true });
      const a = result.applied;
      setStatus(status, `Imported: ${a.settings} settings, ${a.interests} interests, ` +
        `${a.channels} channel rules. Reloading…`);
      setTimeout(() => location.reload(), 1200);
    } catch (err) {
      setStatus(status, err instanceof SyntaxError ? 'That file is not valid JSON.' : err.message, false);
    } finally {
      profileFile.value = '';
    }
  });
}

/* -- blocklist: title terms and hidden videos ---------------------------- */

const termForm = document.querySelector('#add-term');
if (termForm) {
  termForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const field = termForm.querySelector('[name=term]');
    const status = document.querySelector('#term-status');
    try {
      await post('/api/blocklist', { kind: 'term', value: field.value });
      location.reload();
    } catch (err) {
      setStatus(status, err.message, false);
    }
  });
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-unblock]');
  if (!button) return;
  const holder = button.closest('[data-block-kind]');
  const url = '/api/blocklist/' + encodeURIComponent(holder.dataset.blockKind) +
              '/' + encodeURIComponent(holder.dataset.blockValue);
  try {
    await post(url, undefined, 'DELETE');
    holder.remove();
  } catch (err) {
    flash(button, err.message, false);
  }
});

/* -- health check ----------------------------------------------------------- */

const doctorFull = document.querySelector('#doctor-full');
if (doctorFull) {
  doctorFull.addEventListener('click', async () => {
    const target = document.querySelector('#doctor-services');
    doctorFull.disabled = true;
    target.innerHTML = '<p class="hint">Checking… this can take a few seconds.</p>';
    try {
      const response = await fetch('/api/doctor?quick=false');
      const report = await response.json();
      const rows = Object.entries(report.services || {}).map(([name, state]) =>
        `<tr><td>${escapeHtml(name)}</td><td><span class="tag ${state === 'ok' ? 'allow' : 'block'}">` +
        `${escapeHtml(state)}</span></td></tr>`).join('');
      const problems = (report.problems || []).map((p) =>
        `<div class="notice bad">${escapeHtml(p)}</div>`).join('');
      target.innerHTML = problems + `<table><tbody>${rows}</tbody></table>`;
    } catch (err) {
      target.innerHTML = `<p class="hint">${escapeHtml(err.message)}</p>`;
    } finally {
      doctorFull.disabled = false;
    }
  });
}

/* -- channel notes, exemption, list import --------------------------------- */

document.addEventListener('change', async (event) => {
  const field = event.target;
  const row = field.closest('[data-channel]');
  if (!row) return;
  let body = null;
  if (field.matches('[data-channel-note]')) body = { note: field.value };
  if (field.matches('[data-channel-exempt]')) body = { exempt_filters: field.checked };
  if (!body) return;
  try {
    await post('/api/channels/' + encodeURIComponent(row.dataset.channel), body);
    flash(field, 'saved');
  } catch (err) {
    flash(field, err.message, false);
  }
});

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-channel-clear]');
  if (!button) return;
  const row = button.closest('[data-channel]');
  if (!window.confirm('Forget your priority, listing and note for this channel?')) return;
  try {
    await post('/api/channels/' + encodeURIComponent(row.dataset.channel), undefined, 'DELETE');
    location.reload();
  } catch (err) {
    flash(button, err.message, false);
  }
});

const channelsFile = document.querySelector('#channels-file');
if (channelsFile) {
  channelsFile.addEventListener('change', async () => {
    if (!channelsFile.files.length) return;
    const status = document.querySelector('#channels-status');
    try {
      const lists = JSON.parse(await channelsFile.files[0].text());
      const body = {
        allow: (lists.allow || []).map((c) => (typeof c === 'string' ? c : c.id)).filter(Boolean),
        block: (lists.block || []).map((c) => (typeof c === 'string' ? c : c.id)).filter(Boolean),
        priorities: lists.priorities || {}
      };
      const result = await post('/api/channels/import', body);
      setStatus(status, `Imported ${summarise(result)}. Reloading…`);
      setTimeout(() => location.reload(), 900);
    } catch (err) {
      setStatus(status, err instanceof SyntaxError ? 'That file is not valid JSON.' : err.message, false);
    } finally {
      channelsFile.value = '';
    }
  });
}

/* -- opening videos ------------------------------------------------------ */
// Every watch link points at /open/{id}, which records the open server-side and
// redirects to your provider. No beacon: the old one recorded each click as a
// 2% watch, which the learner read as a bounce.

document.addEventListener('toggle', (event) => {
  // Only one "open in" menu at a time.
  const menu = event.target;
  if (!menu.matches || !menu.matches('details.openwith') || !menu.open) return;
  document.querySelectorAll('details.openwith[open]').forEach((other) => {
    if (other !== menu) other.open = false;
  });
}, true);

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  document.querySelectorAll('details.openwith[open]').forEach((menu) => { menu.open = false; });
});

/* -- reset ------------------------------------------------------------------ */

document.querySelectorAll('[data-reset]').forEach((button) => {
  button.addEventListener('click', async () => {
    const scope = button.dataset.reset;
    const status = document.querySelector('#reset-status');
    const what = scope === 'everything'
      ? 'This erases EVERYTHING: videos, settings, history, subscriptions, channel lists, interests and saved profiles.'
      : 'This deletes every video and score. Your subscriptions, history and settings are kept.';
    const typed = window.prompt(`${what}\n\nA backup is made first.\n\nType "reset" to continue.`);
    if (typed === null) return;
    button.disabled = true;
    setStatus(status, 'Backing up and resetting…');
    try {
      const result = await post('/api/reset', { scope, confirm: typed });
      setStatus(status, `Deleted ${result.rows} rows. Backup: ${result.backup || 'none'}`);
      setTimeout(() => { location.href = '/settings?reset=1#danger'; }, 900);
    } catch (err) {
      setStatus(status, err.message, false);
    } finally {
      button.disabled = false;
    }
  });
});

/* -- funnel: filter by stage, reason or text ------------------------------ */

const funnelTable = document.querySelector('.funnel-table');
if (funnelTable) {
  const rows = Array.from(funnelTable.querySelectorAll('tbody tr[data-stage]'));
  const search = document.querySelector('#funnel-search');
  const empty = document.querySelector('#funnel-empty');
  let stage = '';

  const apply = () => {
    const needle = (search.value || '').trim().toLowerCase();
    let visible = 0;
    rows.forEach((row) => {
      const show = (!stage || row.dataset.stage === stage) && (!needle || row.dataset.text.includes(needle));
      row.hidden = !show;
      if (show) visible += 1;
    });
    empty.hidden = visible > 0;
  };

  document.querySelectorAll('[data-stage]').forEach((button) => {
    if (button.tagName !== 'BUTTON') return;
    button.addEventListener('click', () => {
      stage = button.dataset.stage;
      document.querySelectorAll('button[data-stage]').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
      apply();
    });
  });
  document.querySelectorAll('[data-reason]').forEach((chip) => {
    chip.addEventListener('click', () => { search.value = chip.dataset.reason.toLowerCase(); apply(); });
  });
  search.addEventListener('input', apply);
}

/* -- history: delete a record, or forget them all ------------------------- */

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-record-delete]');
  if (!button) return;
  const row = button.closest('[data-record]');
  button.disabled = true;
  try {
    await post(`/api/records/${row.dataset.kind}/${row.dataset.record}`, undefined, 'DELETE');
    row.remove();
  } catch (err) {
    button.disabled = false;
    flash(button, err.message, false);
  }
});

document.querySelectorAll('[data-forget]').forEach((button) => {
  button.addEventListener('click', async () => {
    const kind = button.dataset.forget;
    const status = document.querySelector('#forget-status');
    const typed = window.prompt(`This deletes every record in ${kind}, with no undo.\n\nType "forget" to continue.`);
    if (typed === null) return;
    try {
      const result = await post(`/api/records/${kind}/forget`, { confirm: typed });
      setStatus(status, `Deleted ${result.deleted}.`);
      setTimeout(() => location.reload(), 700);
    } catch (err) {
      setStatus(status, err.message, false);
    }
  });
});

/* -- computation: a preset rewrites the numbers; a number makes it custom -- */

document.addEventListener('sieve:saved', (event) => {
  const { name, settings } = event.detail || {};
  if (!name || !name.startsWith('compute.') || !settings || !settings.compute) return;
  const compute = settings.compute;
  document.querySelectorAll('[name^="compute."]').forEach((control) => {
    const key = control.name.slice('compute.'.length);
    if (!(key in compute)) return;
    if (control.type === 'radio') control.checked = control.value === compute[key];
    else if (control.type === 'checkbox') control.checked = !!compute[key];
    else {
      control.value = compute[key];
      const output = control.parentElement.querySelector('output');
      if (output) output.textContent = compute[key];
    }
  });
  const estimate = document.querySelector('#compute-estimate');
  if (estimate && window.SieveComputeEstimate) estimate.textContent = window.SieveComputeEstimate(compute);
});

/* -- a catalogue that is still filling ----------------------------------- */
// The empty homepage, and the "still building" notice, reload when more real
// videos have arrived. The worker pulls in the background; this only watches.

const watcher = document.querySelector('[data-watch-catalogue]');
if (watcher) {
  const start = Number(watcher.dataset.watchCatalogue || 0);
  let checks = 0;
  const check = async () => {
    checks += 1;
    try {
      const response = await fetch('/api/status');
      const status = await response.json();
      // On an empty page any video is news; otherwise wait for a real batch.
      const needed = start === 0 ? 1 : start + 12;
      if (status.playable_videos >= needed || (start === 0 && status.stats.videos > 0)) {
        location.reload();
        return;
      }
    } catch (ignored) { /* the server is restarting; try again later */ }
    // Every 5 s for the first minute, then every 30 s, then give up after an hour.
    if (checks < 130) setTimeout(check, checks < 12 ? 5000 : 30000);
  };
  setTimeout(check, 5000);
}

/* -- correcting a score (video page) --------------------------------------- */

document.querySelectorAll('[data-override]').forEach((row) => {
  const range = row.querySelector('input[type=range]');
  const out = row.querySelector('output');
  range.addEventListener('input', () => { out.textContent = range.value; });
  const send = async (value, button) => {
    button.disabled = true;
    try {
      await post(`/api/videos/${row.dataset.video}/scores`, { [row.dataset.override]: value }, 'PUT');
      try {
        sessionStorage.setItem('sieve-toast', value === null
          ? 'Back to the engine\'s score. Similar videos are being rescored.'
          : 'Saved. Similar videos are being rescored with what this taught Sieve.');
      } catch (ignored) { /* private mode */ }
      location.reload();
    } catch (err) {
      button.disabled = false;
      flash(button, err.message, false);
    }
  };
  row.querySelector('[data-override-save]').addEventListener('click', (e) => send(Number(range.value), e.currentTarget));
  const clear = row.querySelector('[data-override-clear]');
  if (clear) clear.addEventListener('click', (e) => send(null, e.currentTarget));
});

/* -- progress of a fetch or sync -------------------------------------------- */
// The fetch itself is one request; this polls /api/fetch/progress beside it. It
// also picks up a sync the background worker is already running.

let progressTimer = null;

function watchProgress() {
  const box = document.querySelector('#progress');
  if (!box || progressTimer) return;
  const bar = box.querySelector('.progress-track i');
  const label = box.querySelector('.progress-label');
  let seenRunning = false;
  const tick = async () => {
    try {
      const p = await (await fetch('/api/fetch/progress')).json();
      if (p.running) {
        seenRunning = true;
        box.hidden = false;
        const pct = p.total ? Math.round(100 * p.done / p.total) : 5;
        bar.style.width = `${Math.max(5, pct)}%`;
        box.setAttribute('aria-valuenow', String(pct));
        const verb = p.kind === 'fetch' ? 'Fetching' : 'Syncing';
        label.textContent = p.total ? `${verb}: ${p.label} (${p.done} of ${p.total})` : `${verb}…`;
      } else if (seenRunning || !box.hidden) {
        bar.style.width = '100%';
        setTimeout(() => { box.hidden = true; bar.style.width = '0'; }, 500);
        clearInterval(progressTimer);
        progressTimer = null;
        return;
      }
    } catch (ignored) { /* the server is busy; try again */ }
  };
  tick();
  progressTimer = setInterval(tick, 400);
  // Stop polling eventually if nothing ever started.
  setTimeout(() => { if (!seenRunning && progressTimer) { clearInterval(progressTimer); progressTimer = null; } }, 4000);
}

// A sync the worker started before this page loaded shows too.
fetch('/api/fetch/progress').then((r) => r.json()).then((p) => { if (p.running) watchProgress(); }).catch(() => {});

/* -- "?" beside a slider: what its numbers mean ----------------------------- */
// Opens a small panel under the slider: a reference scale, real titles from
// your catalogue at the current value, and how much a limit there would hide.
// It follows the slider as you drag.

const HUMAN_UNITS = {
  seconds: (s) => (s >= 3600 ? `${(s / 3600).toFixed(1)} h` : s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`),
  views: (n) => compactNumber(n),
  subscribers: (n) => compactNumber(n),
};

function compactNumber(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}K`;
  return String(n);
}

function helpText(data, direction, value) {
  const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let html = `<p class="help-what">${esc(data.what)}</p>`;
  if (data.bands) {
    html += '<ul class="help-bands">' + data.bands.map((b) =>
      `<li class="${b.current ? 'current' : ''}"><span class="mono">${b.from}–${b.to}</span> ${esc(b.text)}</li>`).join('') + '</ul>';
    if (!data.catalogue) return html + '<p class="hint">Examples appear once videos are scored.</p>';
    if (data.examples.length) {
      html += `<p class="help-sub">Your videos near ${Math.round(value)}:</p><ul class="help-examples">` +
        data.examples.map((e) => `<li><span class="mono">${e.score}</span> ${esc(e.title)}</li>`).join('') + '</ul>';
    }
    if (direction === 'max' && value >= 100) html += '<p class="help-sub">At 100 this limit is off.</p>';
    else if (direction === 'min' && value <= 0) html += '<p class="help-sub">At 0 this limit is off.</p>';
    else if (direction === 'max') html += `<p class="help-sub">At ${Math.round(value)}, this hides ${data.above}% of your videos.</p>`;
    else if (direction === 'min') html += `<p class="help-sub">At ${Math.round(value)}, this hides ${data.below}% of your videos.</p>`;
    else html += `<p class="help-sub">${data.above}% of your videos score above ${Math.round(value)}.</p>`;
    html += '<p><button class="btn ghost" type="button" data-range-open>Show every score, 0 to 100</button></p><div class="range-view"></div>';
    return html;
  }
  const fmt = HUMAN_UNITS[data.unit] || String;
  const p = data.percentiles;
  if (!data.catalogue) return html + '<p class="hint">Nothing to compare with yet.</p>';
  html += `<p class="help-sub">Half your videos are under ${fmt(p['50'])}; 90% are under ${fmt(p['90'])}. ` +
          `The shortest tenth are under ${fmt(p['10'])}.</p>`;
  return html;
}

let openHelp = null;

function closeHelp() {
  if (openHelp) { openHelp.pop.remove(); openHelp.button.setAttribute('aria-expanded', 'false'); openHelp = null; }
}

async function renderHelp(state) {
  const input = state.input;
  const value = input ? Number(input.value) : null;
  try {
    const url = `/api/scales/${state.button.dataset.help}` + (value !== null ? `?value=${value}` : '');
    const data = await (await fetch(url)).json();
    if (openHelp === state) state.pop.innerHTML = helpText(data, state.button.dataset.direction, value);
  } catch (ignored) { state.pop.textContent = 'Could not load the explanation.'; }
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-help]');
  if (!button) {
    if (openHelp && !event.target.closest('.help-pop')) closeHelp();
    return;
  }
  event.preventDefault();
  if (openHelp && openHelp.button === button) { closeHelp(); return; }
  closeHelp();
  const host = button.closest('.slider, .check, .dual, label') || button.parentElement;
  const pop = document.createElement('div');
  pop.className = 'help-pop';
  pop.setAttribute('role', 'dialog');
  pop.textContent = 'Loading…';
  host.after(pop);
  button.setAttribute('aria-expanded', 'true');
  const input = button.dataset.for ? document.querySelector(`[name="${CSS.escape(button.dataset.for)}"]`) : null;
  openHelp = { button, pop, input };
  renderHelp(openHelp);
});

document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeHelp(); });

let helpDebounce = null;
document.addEventListener('input', (event) => {
  if (openHelp && event.target === openHelp.input) {
    clearTimeout(helpDebounce);
    helpDebounce = setTimeout(() => renderHelp(openHelp), 200);
  }
});

/* -- two-handled range sliders (length, views, subscribers) ----------------- */
// The handles are positions 0–1000; the named number inputs beside them hold
// the real values and are what autosave stores. The top position means "no
// upper limit", stored as 0. Views and subscribers use a log scale.

document.querySelectorAll('[data-dual]').forEach((box) => {
  const top = Number(box.dataset.top);
  const scale = box.dataset.scale;   // log (views), sqrt (length) or linear
  const fmt = HUMAN_UNITS[box.dataset.unit] || String;
  const [loRange, hiRange] = box.querySelectorAll('input[type=range]');
  const [loNum, hiNum] = box.querySelectorAll('input[type=number]');
  const fill = box.querySelector('.dual-fill');
  const read = box.querySelector('.dual-read');

  const toValue = (pos) => {
    if (pos <= 0) return 0;
    const f = pos / 1000;
    const v = scale === 'log' ? Math.pow(10, f * Math.log10(top + 1)) - 1
            : scale === 'sqrt' ? f * f * top : f * top;
    // Round to something a person would type.
    const mag = Math.pow(10, Math.max(0, Math.floor(Math.log10(Math.max(v, 1))) - 1));
    return Math.round(v / mag) * mag;
  };
  const toPos = (value) => {
    if (value <= 0) return 0;
    const f = scale === 'log' ? Math.log10(value + 1) / Math.log10(top + 1)
            : scale === 'sqrt' ? Math.sqrt(value / top) : value / top;
    return Math.min(1000, Math.round(1000 * f));
  };
  const paint = () => {
    const a = Number(loRange.value), b = Number(hiRange.value);
    fill.style.left = `${a / 10}%`;
    fill.style.width = `${Math.max(0, b - a) / 10}%`;
    const lo = Number(loNum.value), hi = Number(hiNum.value);
    read.textContent = `${lo ? fmt(lo) : 'any'} – ${hi ? fmt(hi) : 'no limit'}`;
  };
  const fromNumbers = () => {
    loRange.value = toPos(Number(loNum.value));
    hiRange.value = Number(hiNum.value) ? toPos(Number(hiNum.value)) : 1000;
    paint();
  };
  const commit = (num) => num.dispatchEvent(new Event('change', { bubbles: true }));

  loRange.addEventListener('input', () => {
    if (Number(loRange.value) > Number(hiRange.value)) loRange.value = hiRange.value;
    loNum.value = toValue(Number(loRange.value)) || '';
    paint();
  });
  hiRange.addEventListener('input', () => {
    if (Number(hiRange.value) < Number(loRange.value)) hiRange.value = loRange.value;
    hiNum.value = Number(hiRange.value) >= 1000 ? '' : (toValue(Number(hiRange.value)) || '');
    paint();
  });
  // Save when a handle is let go, like every other slider.
  loRange.addEventListener('change', () => commit(loNum));
  hiRange.addEventListener('change', () => commit(hiNum));
  loNum.addEventListener('input', fromNumbers);
  hiNum.addEventListener('input', fromNumbers);
  fromNumbers();
});

/* -- sticky table headers sit just under the page header -------------------- */
function setStickyTop() {
  const head = document.querySelector('.masthead');
  const sticky = head && getComputedStyle(head).position === 'sticky';
  document.documentElement.style.setProperty('--sticky-top', sticky ? `${head.offsetHeight}px` : '0px');
}
function fitTables() {
  document.querySelectorAll('.scroll').forEach((box) => {
    const table = box.querySelector('table');
    box.classList.remove('fits');
    // Compare the table's natural width with the room it has.
    if (table && table.scrollWidth <= box.clientWidth + 1) box.classList.add('fits');
  });
}
function layout() { setStickyTop(); fitTables(); }
layout();
window.addEventListener('resize', layout);

/* -- notes, downloads, the offline player --------------------------------- */

function parseClock(text) {
  const parts = String(text || '').trim().split(':').map(Number);
  if (!text || parts.some((n) => !Number.isFinite(n))) return null;
  return parts.reduce((total, n) => total * 60 + n, 0);
}

document.querySelectorAll('[data-note-form]').forEach((form) => {
  const text = form.querySelector('textarea');
  const at = form.querySelector('input');
  const button = form.querySelector('button');
  button.addEventListener('click', async () => {
    const seconds = parseClock(at.value);
    if (at.value && seconds === null) { flash(button, 'Use m:ss for the moment, e.g. 4:05', false); return; }
    button.disabled = true;
    try {
      await post(`/api/videos/${form.dataset.noteForm}/notes`, { text: text.value, at_second: seconds });
      location.reload();
    } catch (err) {
      button.disabled = false;
      flash(button, err.message, false);
    }
  });
});

document.querySelectorAll('[data-download]').forEach((button) => {
  button.addEventListener('click', async () => {
    const quality = (document.querySelector('#dl-quality') || {}).value || '720';
    button.disabled = true;
    try {
      const result = await post('/api/downloads', { video_id: button.dataset.download, quality });
      try { sessionStorage.setItem('sieve-toast', result.note || 'Saving in the background. It will appear in Library, Offline.'); } catch (ignored) { /* private */ }
      location.reload();
    } catch (err) {
      button.disabled = false;
      flash(button, err.message, false);
    }
  });
});

// The offline player: chapters seek, and watching counts like anywhere else.
const localVideo = document.querySelector('[data-local-video]');
if (localVideo) {
  const session = `local-${localVideo.dataset.localVideo}-${Date.now()}`;
  document.querySelectorAll('[data-seek]').forEach((b) =>
    b.addEventListener('click', () => { localVideo.currentTime = Number(b.dataset.seek); localVideo.play(); }));
  const report = () => {
    if (!localVideo.duration) return;
    const body = JSON.stringify({ video_id: localVideo.dataset.localVideo,
                                  progress: Math.min(1, localVideo.currentTime / localVideo.duration), session });
    if (!(navigator.sendBeacon && navigator.sendBeacon('/api/progress', new Blob([body], { type: 'application/json' })))) {
      fetch('/api/progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true });
    }
  };
  setInterval(() => { if (!localVideo.paused) report(); }, 15000);
  localVideo.addEventListener('pause', report);
  localVideo.addEventListener('ended', report);
  window.addEventListener('pagehide', report);
}

/* -- the header's "More" menu closes on an outside click or Escape ---------- */
document.addEventListener('click', (e) => {
  document.querySelectorAll('.nav-more[open]').forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.nav-more[open]').forEach((d) => { d.open = false; });
});

document.querySelectorAll('.nav-more').forEach((d) => {
  d.addEventListener('toggle', () => {
    if (!d.open) return;
    const button = d.querySelector('summary').getBoundingClientRect();
    const menu = d.querySelector('.nav-menu');
    menu.style.top = `${button.bottom + 4}px`;
    // Under the button, but never past the window's right edge.
    const width = menu.offsetWidth || 176;
    menu.style.left = `${Math.max(8, Math.min(button.left, window.innerWidth - width - 8))}px`;
  });
});
window.addEventListener('scroll', () => document.querySelectorAll('.nav-more[open]').forEach((d) => { d.open = false; }), { passive: true });

/* -- on a phone, scroll the nav so the current page is in view -------------- */
(() => {
  const nav = document.querySelector('.masthead nav');
  const current = nav && nav.querySelector('[aria-current="page"]');
  if (!nav || !current || nav.scrollWidth <= nav.clientWidth) return;
  // Screen coordinates: offsetLeft would be relative to the "More" menu's
  // own box when the current page is one of its items.
  const overflow = current.getBoundingClientRect().right - nav.getBoundingClientRect().right;
  if (overflow > 0) nav.scrollLeft += overflow + 16;
})();

/* -- dismissing notices ----------------------------------------------------- */
document.addEventListener('click', async (event) => {
  const button = event.target.closest('.dismiss');
  if (!button) return;
  const notice = button.closest('[data-dismiss]');
  if (!notice) return;
  notice.remove();
  try { await post(`/api/notices/${encodeURIComponent(notice.dataset.dismiss)}/dismiss`, {}); } catch (ignored) { /* shown again next time */ }
});

/* -- searching the homepage (#11) ------------------------------------------ */
// Finds videos already on the page; never reorders them and changes nothing
// about ranking, scores or feedback. Clearing it brings the page back.
const homeSearch = document.querySelector('[data-home-search]');
if (homeSearch) {
  const input = homeSearch.querySelector('input');
  const status = homeSearch.querySelector('.search-status');
  const cards = () => Array.from(document.querySelectorAll('.grid [data-video]'));
  let timer = null, latest = 0;
  const run = async () => {
    const q = input.value.trim();
    const all = cards();
    if (!q) {
      all.forEach((c) => { c.hidden = false; });
      status.textContent = '';
      return;
    }
    const ticket = ++latest;
    status.textContent = 'Searching…';
    try {
      const result = await post('/api/homepage/search', { q, ids: all.map((c) => c.dataset.video) });
      if (ticket !== latest) return;
      const keep = new Set(result.ids);
      all.forEach((c) => { c.hidden = !keep.has(c.dataset.video); });
      const how = result.mode === 'ai' ? 'AI' : 'word match';
      status.textContent = `${keep.size} of ${all.length} match · ${how}` + (result.note ? ` (${result.note})` : '');
    } catch (err) {
      status.textContent = err.message;
    }
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 450); });
  input.addEventListener('search', run);   // the clear (×) button
  homeSearch.addEventListener('submit', (e) => { e.preventDefault(); clearTimeout(timer); run(); });
}

/* -- "tell Sieve why" (#12) ------------------------------------------------- */
document.querySelectorAll('[data-explain]').forEach((box) => {
  const text = box.querySelector('textarea');
  const button = box.querySelector('button');
  const out = box.querySelector('.explain-result');
  button.addEventListener('click', async () => {
    button.disabled = true;
    out.textContent = 'Thinking…';
    try {
      const r = await post(`/api/videos/${box.dataset.explain}/explain`, { text: text.value });
      out.textContent = `${r.summary || 'Understood.'} ${r.via === 'ai' ? '' : '(No AI connected: matched your words.)'}`;
      if (r.state) showFeedback(box.dataset.explain, r.state);
      if (feedbackChannel) feedbackChannel.postMessage({ video: box.dataset.explain, state: r.state });
      text.value = '';
    } catch (err) {
      out.textContent = err.message;
    } finally {
      button.disabled = false;
    }
  });
});

/* -- channel status: what each channel is, at a glance --------------------- */
function channelStatus(row) {
  const badge = row.querySelector('[data-status]');
  if (!badge) return;
  const listing = row.dataset.currentListing || 'neutral';
  const exempt = row.querySelector('[data-channel-exempt]');
  if (exempt) {
    exempt.disabled = listing !== 'allow';
    if (listing !== 'allow') exempt.checked = false;
  }
  const label = { allow: 'Allowed', block: 'Blocked', neutral: 'Neutral' }[listing] || listing;
  badge.textContent = label + (exempt && exempt.checked && listing === 'allow' ? ' · exempt from filters' : '');
  badge.dataset.state = listing;
}
document.querySelectorAll('tr[data-channel]').forEach(channelStatus);
document.addEventListener('click', (e) => {
  const row = e.target.closest('tr[data-channel]');
  if (row && e.target.closest('[data-listing]')) setTimeout(() => channelStatus(row), 400);
});
document.addEventListener('change', (e) => {
  const row = e.target.closest('tr[data-channel]');
  if (row && e.target.matches('[data-channel-exempt]')) channelStatus(row);
});
const selectedChannel = document.querySelector('tr[data-channel].selected');
if (selectedChannel) selectedChannel.scrollIntoView({ block: 'center' });

/* -- the AI key: sent once, never shown back ------------------------------- */
const aiKeyButton = document.querySelector('[data-ai-key]');
if (aiKeyButton) {
  aiKeyButton.addEventListener('click', async () => {
    const input = document.querySelector('#ai-key');
    try {
      await post('/api/ai/key', { api_key: input.value });
      input.value = '';
      try { sessionStorage.setItem('sieve-toast', 'Key saved.'); } catch (ignored) { /* private */ }
      location.reload();
    } catch (err) { flash(aiKeyButton, err.message, false); }
  });
}

/* -- the full range of a score: a number line you can click (#6) ---------- */
// Bar height: how many of your videos have each score (log scale, so a few
// videos still show). Click a point to see its videos; the point before
// stays beside it, to compare. Empty points say so.

function escapeHtml(t) {
  return String(t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function openRange(pop, key, start) {
  const view = pop.querySelector('.range-view');
  const data = await (await fetch(`/api/scales/${key}/range`)).json();
  const max = Math.max(1, ...data.histogram);
  view.innerHTML =
    `<div class="numberline" role="listbox" aria-label="Scores 0 to 100">` +
    data.histogram.map((n, i) => `<button type="button" role="option" data-at="${i}" title="${i}: ${n} video${n === 1 ? '' : 's'}"
      style="--h:${n ? Math.max(8, Math.round(100 * Math.log(1 + n) / Math.log(1 + max))) : 0}%"
      class="${n ? '' : 'empty'}"></button>`).join('') +
    `</div><div class="numberline-scale"><span>0</span><span>25</span><span>50</span><span>75</span><span>100</span></div>
     <div class="range-compare"></div>`;
  const compare = view.querySelector('.range-compare');
  const columns = [];
  const show = async (at) => {
    view.querySelectorAll('[data-at]').forEach((b) => b.setAttribute('aria-selected', String(Number(b.dataset.at) === at)));
    const r = await (await fetch(`/api/scales/${key}/range?at=${at}`)).json();
    columns.unshift(r);
    if (columns.length > 2) columns.pop();
    compare.innerHTML = columns.map((c) => `<div><h4>${c.at} <span class="muted">${escapeHtml(c.band || '')}</span></h4>` +
      (c.videos.length
        ? '<ul>' + c.videos.map((v) => `<li><a href="/video/${v.id}">${escapeHtml(v.title || 'Untitled video')}</a> <span class="muted">${escapeHtml(v.author || '')}</span></li>`).join('') + '</ul>'
        : '<p class="muted">No video in your catalogue scores exactly this.</p>') + '</div>').join('');
  };
  view.addEventListener('click', (e) => {
    const b = e.target.closest('[data-at]');
    if (b) show(Number(b.dataset.at));
  });
  show(Math.max(0, Math.min(100, Math.round(start || 0))));
}

document.addEventListener('click', (e) => {
  const button = e.target.closest('[data-range-open]');
  if (!button || !openHelp) return;
  button.remove();
  const value = openHelp.input ? Number(openHelp.input.value) : 50;
  openRange(openHelp.pop, openHelp.button.dataset.help, value);
});
