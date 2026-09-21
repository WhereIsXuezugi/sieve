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
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function flash(element, text, ok) {
  const note = document.createElement('span');
  note.textContent = text;
  note.className = ok === false ? 'muted' : 'tag allow';
  note.style.marginLeft = '0.4rem';
  element.after(note);
  setTimeout(() => note.remove(), 2600);
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

/* -- maintenance buttons ------------------------------------------------- */

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Working…';
    try {
      const result = await post(button.dataset.action, {});
      button.textContent = original;
      flash(button, JSON.stringify(result).slice(0, 60));
    } catch (err) {
      button.textContent = original;
      flash(button, 'failed', false);
    } finally {
      button.disabled = false;
    }
  });
});

/* -- file imports -------------------------------------------------------- */

document.querySelectorAll('[data-upload]').forEach((input) => {
  input.addEventListener('change', async () => {
    if (!input.files.length) return;
    const data = new FormData();
    data.append('file', input.files[0]);
    const response = await fetch(input.dataset.upload, { method: 'POST', body: data });
    const result = await response.json();
    flash(input, `imported ${result.imported}`);
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
      status.textContent = 'Could not import that profile.';
    } finally {
      profileFetch.disabled = false;
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
