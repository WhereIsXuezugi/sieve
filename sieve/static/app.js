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
  const note = document.createElement('span');
  note.textContent = text;
  note.className = ok === false ? 'tag block' : 'tag allow';
  note.style.marginLeft = '0.4rem';
  note.setAttribute('role', 'status');
  element.after(note);
  setTimeout(() => note.remove(), ok === false ? 6000 : 2600);
}

// Turn an API result into a short human line: {"ok":true,"scored":40} -> "scored 40".
function summarise(result) {
  if (!result || typeof result !== 'object') return 'done';
  const parts = [];
  for (const [key, value] of Object.entries(result)) {
    if (key === 'ok') continue;
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

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-feedback]');
  if (!button) return;
  const card = button.closest('[data-video]');
  const kind = button.dataset.feedback;
  button.disabled = true;
  try {
    const result = await post('/api/feedback', { video_id: card.dataset.video, kind });
    button.dataset.done = '1';
    const applied = Object.entries(result.applied || {});
    if (applied.length) {
      flash(button, applied.map(([k, v]) => `${k} → ${v}`).join(', '));
    }
    if (kind === 'less' || kind === 'block_channel') {
      card.style.opacity = '0.35';
    }
  } catch (err) {
    button.disabled = false;
    flash(button, 'failed', false);
  }
});

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-hide]');
  if (!button) return;
  const card = button.closest('[data-video]');
  await post('/api/hide', { kind: 'video', value: card.dataset.video });
  card.style.opacity = '0.35';
  button.dataset.done = '1';
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
    clearTimeout(timer);
    timer = setTimeout(validate, 400);
  });
  validate();

  document.querySelector('#rule-save').addEventListener('click', async () => {
    try {
      await post('/api/rules', {
        expr: ruleBox.value,
        enabled: document.querySelector('#rule-enabled').checked
      });
      location.href = '/';
    } catch (err) {
      status.textContent = 'save failed: ' + err.message;
      status.className = 'tag block';
    }
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

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', async () => {
    if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Working…';
    try {
      const body = button.dataset.body ? JSON.parse(button.dataset.body) : {};
      const result = await post(button.dataset.action, body);
      button.textContent = original;
      flash(button, summarise(result));
      if ('reload' in button.dataset) setTimeout(() => location.reload(), 700);
    } catch (err) {
      button.textContent = original;
      flash(button, err.message, false);
    } finally {
      button.disabled = false;
    }
  });
});

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

/* -- watch progress ------------------------------------------------------ */
// Sieve does not host the player; Invidious does. When you open a video from
// here we record the click as a partial watch, and the player page can post
// real progress back to /api/progress if you wire it up (see docs/player.md).

document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-watch]');
  if (!link) return;
  navigator.sendBeacon(
    '/api/progress',
    new Blob([JSON.stringify({ video_id: link.dataset.watch, progress: 0.02 })],
      { type: 'application/json' })
  );
});
