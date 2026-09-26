// Sieve — autosave for the Controls and Rules pages.
//
// There is no Save button. Every control saves itself when it changes, as a
// patch containing only that control's setting, so nothing you touched can be
// lost by navigating away and nothing you did not touch is ever resent.
//
// Control names are setting paths: `filters.max_brainrot`,
// `targets.education.enabled`, `playback.menu`. Checkboxes sharing one name
// form a list. Numbers are integers unless the control's step allows decimals.
// `data-type="csv"` turns "en, de" into ["en", "de"].
//
// The pure functions at the top are exported for Node so they can be tested
// without a browser (tests/test_autosave.py runs them).

(function (root) {
  'use strict';

  function isDecimal(control) {
    const step = control.getAttribute && control.getAttribute('step');
    return !!step && step !== 'any' ? step.includes('.') : step === 'any';
  }

  // What a control means, as a JSON value.
  function valueOf(control, siblings) {
    const type = (control.type || '').toLowerCase();
    if (type === 'checkbox') {
      if (siblings && siblings.length > 1) {
        return siblings.filter((c) => c.checked).map((c) => c.value);
      }
      return !!control.checked;
    }
    if (type === 'radio') {
      const chosen = (siblings || [control]).find((c) => c.checked);
      return chosen ? chosen.value : null;
    }
    if (type === 'range' || type === 'number') {
      if (control.value === '') return 0;
      const n = Number(control.value);
      if (!Number.isFinite(n)) return undefined;
      return isDecimal(control) ? n : Math.round(n);
    }
    if (control.dataset && control.dataset.type === 'csv') {
      return control.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
    }
    return control.value;
  }

  // "filters.max_brainrot", 20  ->  {filters: {max_brainrot: 20}}
  function patchFor(name, value) {
    const parts = name.split('.');
    const patch = {};
    let cursor = patch;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) cursor[part] = value;
      else cursor = cursor[part] = {};
    });
    return patch;
  }

  function getPath(object, name) {
    return name.split('.').reduce((o, k) => (o && k in o ? o[k] : undefined), object);
  }

  // How long to wait after the last keystroke before saving free text.
  const TEXT_DELAY = 700;

  const api = { valueOf, patchFor, getPath, isDecimal, TEXT_DELAY };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
    return;
  }
  root.SieveAutosave = api;

  /* -- the browser part ---------------------------------------------------- */

  // Any number of [data-autosave] areas on a page (Controls has the main
  // one and Backups); one listener on <main> serves them all.
  if (!document.querySelector('[data-autosave]')) return;
  const scope = document.querySelector('main') || document.body;
  const saves = (control) => Boolean(control.name && control.closest('[data-autosave]') &&
                                     !control.closest('[data-no-autosave]'));

  const status = document.createElement('div');
  status.className = 'save-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  document.body.appendChild(status);
  status.addEventListener('click', () => status.classList.remove('visible'));
  let statusTimer = null;

  function show(text, state) {
    status.textContent = text;
    status.dataset.state = state;
    status.classList.add('visible');
    clearTimeout(statusTimer);
    const delay = state === 'error' ? 7000 : state === 'saving' ? 8000 : 1800;
    statusTimer = setTimeout(() => status.classList.remove('visible'), delay);
  }

  function fieldNote(control, text) {
    const holder = control.closest('.check, .slider, label, div') || control.parentElement;
    let note = holder.querySelector(':scope > .field-error');
    if (!text) { if (note) note.remove(); return; }
    if (!note) {
      note = document.createElement('p');
      note.className = 'field-error';
      holder.appendChild(note);
    }
    note.textContent = text;
  }

  // Saves are sent one at a time, in order, so a quick burst of changes
  // cannot land out of order and leave an older value stored.
  let queue = Promise.resolve();

  function save(control) {
    const name = control.name;
    const siblings = Array.from(scope.querySelectorAll(`[name="${CSS.escape(name)}"]`));
    const value = valueOf(control, siblings);
    if (value === undefined) {
      fieldNote(control, 'Not a number.');
      return;
    }
    const patch = patchFor(name, value);
    show('Saving…', 'saving');
    queue = queue.then(async () => {
      try {
        const response = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch)
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || `could not save (${response.status})`);
        // The server drops values it will not store. Say so, rather than
        // claiming a save that did not happen.
        if (getPath(body.applied || {}, name) === undefined) {
          throw new Error('That value was not accepted.');
        }
        fieldNote(control, '');
        remember(control);
        // Say what the change did, so a setting never seems to do nothing.
        const e = body.effect;
        show(e ? `Saved — ${e.on_page} on the homepage, ${e.pass} pass your filters, ${e.filtered} filtered out`
               : 'Saved', 'saved');
        document.dispatchEvent(new CustomEvent('sieve:saved', { detail: { name, value, settings: body.settings } }));
      } catch (err) {
        revert(control);
        fieldNote(control, err.message);
        show(`Not saved: ${err.message}`, 'error');
      }
    });
  }

  // The last value the server accepted for each checkbox and radio group, so
  // a refused change snaps back instead of showing a state that is not stored.
  const accepted = new Map();
  scope.querySelectorAll('input[type=checkbox][name], input[type=radio][name]').forEach((c) => {
    if (c.type === 'radio') { if (c.checked) accepted.set(c.name, c.value); }
    else accepted.set(c.name + '\u0000' + c.value, c.checked);
  });

  function remember(control) {
    if (control.type === 'radio') accepted.set(control.name, control.value);
    else if (control.type === 'checkbox') accepted.set(control.name + '\u0000' + control.value, control.checked);
  }

  function revert(control) {
    if (control.type === 'radio') {
      const previous = accepted.get(control.name);
      scope.querySelectorAll(`input[type=radio][name="${CSS.escape(control.name)}"]`)
        .forEach((r) => { r.checked = r.value === previous; });
    } else if (control.type === 'checkbox') {
      const previous = accepted.get(control.name + '\u0000' + control.value);
      if (previous !== undefined) control.checked = previous;
    }
  }

  const timers = new WeakMap();

  scope.addEventListener('change', (event) => {
    const control = event.target;
    if (!saves(control)) return;
    clearTimeout(timers.get(control));
    save(control);
  });

  scope.addEventListener('input', (event) => {
    const control = event.target;
    const type = (control.type || '').toLowerCase();
    if (!saves(control)) return;
    // Text saves shortly after typing stops; sliders save on release
    // (the change event), and only update their readout while dragging.
    if (type === 'text' || type === 'url' || type === 'number' || control.tagName === 'TEXTAREA') {
      clearTimeout(timers.get(control));
      timers.set(control, setTimeout(() => save(control), TEXT_DELAY));
    }
  });
})(typeof window !== 'undefined' ? window : globalThis);
