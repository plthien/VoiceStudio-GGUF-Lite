/* Nano Lite: VoiceStudio Lite presentation, canonical Nano API v1 only. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const nodes = selector => [...document.querySelectorAll(selector)];
  const pages = {
    clone: ['Voice Cloning', 'Create an English voice from an owned reference recording.'],
    library: ['Voice Library', 'Manage saved Nano profiles and import legacy voices.'],
    tts: ['Text to Speech', 'Generate a local English 24 kHz WAV from a selected voice.'],
    runtime: ['Runtime & Settings', 'Inspect Chatterbox-Nano, CUDA0 evidence, and model residency.']
  };
  let voices = [], chosen = '', editor = '', loadSequence = 0, generationEpoch = 0;
  let generating = false, activeCancel = null, wavUrl = null, cloneUrl = null, replaceUrl = null, storedUrl = null;
  let currentView = 'clone', runtimeSequence = 0;
  const maxAudio = 24 * 1024 * 1024;
  const errors = {
    INVALID_REQUEST: 'Check required fields and use English only.',
    ENGLISH_ONLY: 'Chatterbox-Nano supports English only.',
    INVALID_AUDIO: 'Use an owned WAV or MP3 with valid duration.',
    REFERENCE_TOO_SHORT: 'The recording must be longer than five seconds.',
    AUDIO_TOO_LARGE: 'Reference audio exceeds the size limit.',
    TRANSCRIPT_REQUIRED: 'Enter the exact transcript of the new recording.',
    TRANSCRIPT_CONFIRMATION_CONFLICT: 'Same-words confirmation conflicts with the supplied transcript.',
    PROFILE_NOT_FOUND: 'The saved profile no longer exists.',
    PROFILE_ACTIVE: 'The active profile cannot be modified during generation.',
    VOICE_CONSENT_REQUIRED: 'Explicit permission for this recording is required.',
    TRANSCRIPT_CHANGE_CONFIRMATION_REQUIRED: 'Confirm the transcript change before saving.',
    WORKER_BUSY: 'The CUDA worker is busy. Try again after cleanup.',
    REFERENCE_CHANGED: 'Reference identity changed during synthesis; do not use the old result.',
    REFERENCE_INVALID: 'Reference integrity check failed.',
    CUDA_UNAVAILABLE: 'CUDA0 is unavailable; no CPU fallback was attempted.',
    GGUF_PROBE_UNAVAILABLE: 'Other local worker isolation could not be verified.',
    CANCELLED: 'Generation was cancelled.',
    NO_ACTIVE_REQUEST: 'The request is no longer active.',
    INVALID_PAUSE_MARKER: 'Use [pause], [pause 800ms], or [pause 1.5s].',
    PAUSE_LIMIT_EXCEEDED: 'A maximum of 16 pauses / 60 seconds is allowed.'
  };
  function errorText(error) { return errors[error.code || error.message] || error.code || error.message || 'Unknown local service error'; }
  function notice(message, invalid = false) {
    const box = $('notice'); box.textContent = message || ''; box.className = invalid ? 'notice error' : 'notice'; box.hidden = !message;
  }
  function busy(button, state) { button.disabled = state; button.setAttribute('aria-busy', String(state)); }
  function asText(value) { return value == null ? 'Not verified' : String(value); }
  async function call(path, options = {}) {
    const response = await fetch(path, {cache: 'no-store', ...options});
    if (!response.ok) {
      let code = 'HTTP_' + response.status;
      try { const body = await response.json(); code = body.error?.code || body.code || code; } catch (_) {}
      const failure = new Error(code); failure.code = code; failure.status = response.status; throw failure;
    }
    return response;
  }
  async function json(path, options = {}) { return (await call(path, options)).json(); }
  function jsonOptions(method, body) { return {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}; }
  function metric(name, value) {
    const root = document.createElement('div'); root.className = 'metric';
    const label = document.createElement('span'); label.textContent = name;
    const data = document.createElement('strong'); data.textContent = asText(value);
    root.append(label, data); return root;
  }
  function detail(name, value) {
    const root = document.createElement('div'); root.className = 'detail';
    const label = document.createElement('span'); label.textContent = name;
    const data = document.createElement('strong'); data.textContent = asText(value);
    root.append(label, data); return root;
  }
  function releaseAudio(elementId, urlName) {
    const element = $(elementId);
    if (urlName === 'clone' && cloneUrl) { URL.revokeObjectURL(cloneUrl); cloneUrl = null; }
    if (urlName === 'replace' && replaceUrl) { URL.revokeObjectURL(replaceUrl); replaceUrl = null; }
    if (urlName === 'stored' && storedUrl) { URL.revokeObjectURL(storedUrl); storedUrl = null; }
    if (urlName === 'tts' && wavUrl) { URL.revokeObjectURL(wavUrl); wavUrl = null; }
    element.removeAttribute('src'); element.load(); element.hidden = true;
  }
  function clearOutput() {
    generationEpoch++;
    releaseAudio('tts-player', 'tts');
    $('tts-output').hidden = true; $('tts-proof').textContent = ''; $('tts-timing').textContent = '';
    $('tts-breakdown').textContent = ''; $('tts-download').removeAttribute('href');
  }
  function setVoice(id) {
    const old = chosen; chosen = voices.some(v => v.id === id) ? id : '';
    $('tts-voice').value = chosen;
    const voice = voices.find(v => v.id === chosen);
    $('selected-voice').textContent = voice ? voice.name + ' · ' + voice.id : 'No voice selected';
    if (old !== chosen) clearOutput();
  }
  function openView(page) {
    if (!pages[page]) return;
    currentView = page;
    nodes('.nav-button').forEach(button => {
      const active = button.dataset.view === page; button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    Object.keys(pages).forEach(key => { $('view-' + key).hidden = key !== page; });
    $('page-title').textContent = pages[page][0]; $('page-subtitle').textContent = pages[page][1];
    if (page !== 'library') releaseAudio('saved-preview', 'stored');
    if (page !== 'clone') clearCloneAudio(true);
    if (page !== 'tts') clearOutput();
    if (page === 'library' || page === 'tts') loadVoices();
    if (page === 'runtime') refreshRuntime();
    if (location.hash !== '#' + page) location.hash = page;
  }
  nodes('.nav-button').forEach(button => button.addEventListener('click', () => openView(button.dataset.view)));
  nodes('[data-goto]').forEach(button => button.addEventListener('click', () => openView(button.dataset.goto)));
  addEventListener('hashchange', () => { const page = location.hash.slice(1); if (pages[page] && page !== currentView) openView(page); });
  async function cheapStatus() {
    try {
      const status = await json('/lite/worker/status');
      $('status-short').textContent = status.busy || status.admission_busy ? 'Nano worker busy' : 'Local Nano service connected';
      $('status-dot').className = status.busy ? 'status-dot' : 'status-dot ok';
    } catch (_) { $('status-short').textContent = 'Service unavailable'; $('status-dot').className = 'status-dot bad'; }
  }
  function renderVoices() {
    const list = $('voice-list'); list.replaceChildren(); $('library-empty').hidden = voices.length > 0;
    for (const v of voices) {
      const row = document.createElement('div'); row.className = 'voice-item';
      const description = document.createElement('div'), title = document.createElement('strong');
      title.textContent = v.name;
      const meta = document.createElement('span'); meta.className = 'voice-meta';
      meta.textContent = v.id + ' · English · version ' + v.version + ' · consent ' + v.consent;
      description.append(title, meta);
      const actions = document.createElement('div'); actions.className = 'actions';
      const use = document.createElement('button'); use.type = 'button'; use.className = 'secondary'; use.textContent = 'Use in TTS';
      use.addEventListener('click', () => { setVoice(v.id); openView('tts'); });
      const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'secondary'; edit.textContent = 'Edit';
      edit.addEventListener('click', () => editVoice(v.id));
      actions.append(use, edit); row.append(description, actions); list.append(row);
    }
    const select = $('tts-voice'); select.replaceChildren(new Option('Select a saved voice', ''));
    voices.forEach(v => select.add(new Option(v.name + ' · v' + v.version + ' · ' + v.id, v.id)));
    if (!voices.some(v => v.id === chosen)) chosen = voices[0]?.id || '';
    setVoice(chosen);
    $('tts-submit').disabled = generating || voices.length === 0;
    if (editor && !voices.some(v => v.id === editor)) { editor = ''; $('editor-panel').hidden = true; releaseAudio('saved-preview', 'stored'); }
  }
  async function loadVoices() {
    const sequence = ++loadSequence;
    try {
      const next = await json('/profiles');
      if (sequence !== loadSequence) return;
      if (!Array.isArray(next)) throw new Error('INVALID_PROFILE_LIST');
      const old = voices.find(v => v.id === chosen);
      const updated = next.find(v => v.id === chosen);
      voices = next;
      if (old && (!updated || updated.version !== old.version || updated.ref_sha256 !== old.ref_sha256)) clearOutput();
      renderVoices(); await cheapStatus();
    } catch (error) { if (sequence === loadSequence) notice(errorText(error), true); }
  }
  $('refresh-voices').addEventListener('click', loadVoices);
  $('library-refresh').addEventListener('click', loadVoices);
  $('tts-voice').addEventListener('change', event => setVoice(event.target.value));
  function validateFile(file) {
    if (!file || file.size < 64 || file.size > maxAudio || !/\.(wav|mp3)$/i.test(file.name)) throw new Error('INVALID_AUDIO');
  }
  function clearCloneAudio(clearInput = false) {
    const player = $('clone-preview'); player.onloadedmetadata = null; player.onerror = null;
    releaseAudio('clone-preview', 'clone');
    $('clone-meta').textContent = 'No reference selected.';
    if (clearInput) $('clone-file').value = '';
  }
  $('clone-file').addEventListener('change', () => {
    clearCloneAudio(false);
    const file = $('clone-file').files[0]; if (!file) return;
    try {
      validateFile(file); cloneUrl = URL.createObjectURL(file);
      const ownedUrl = cloneUrl, player = $('clone-preview');
      player.src = cloneUrl; player.hidden = false;
      $('clone-meta').textContent = file.name + ' · ' + Math.ceil(file.size / 1024) + ' KiB · metadata pending';
      player.onloadedmetadata = () => {
        if (cloneUrl !== ownedUrl) return;
        if (!Number.isFinite(player.duration) || player.duration <= 5 || player.duration > 60) {
          clearCloneAudio(true); notice('REFERENCE_TOO_SHORT / INVALID_AUDIO_DURATION', true); return;
        }
        $('clone-meta').textContent = file.name + ' · ' + Math.ceil(file.size / 1024) + ' KiB · ' + player.duration.toFixed(2) + ' s';
      };
      player.onerror = () => { if (cloneUrl === ownedUrl) { clearCloneAudio(true); notice('INVALID_AUDIO', true); } };
    } catch (error) { clearCloneAudio(true); notice(errorText(error), true); }
  });
  $('clone-clear').addEventListener('click', () => clearCloneAudio(true));
  $('clone-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('clone-submit'), form = event.currentTarget;
    if (!$('clone-consent').checked) { notice('Explicit reference permission is required.', true); return; }
    try { validateFile($('clone-file').files[0]); } catch (error) { notice(errorText(error), true); return; }
    busy(button, true); $('clone-result').textContent = 'Saving local reference…';
    try {
      const data = new FormData(form); data.set('consent', 'confirmed'); data.set('language', 'en');
      const result = await json('/profiles', {method: 'POST', body: data});
      form.reset(); clearCloneAudio(true);
      await loadVoices(); setVoice(result.id);
      $('clone-result').textContent = 'Created ' + result.id + ' · version ' + result.version;
      notice('Voice saved locally.'); openView('library');
    } catch (error) { $('clone-result').textContent = 'Not created'; notice(errorText(error), true); }
    finally { busy(button, false); }
  });
  function resetReplacement() {
    $('replace-form').reset();
    releaseAudio('replace-preview', 'replace');
    $('replace-file').value = ''; $('replace-text').disabled = false; $('replace-text').required = true;
    $('replace-state').textContent = 'No new recording selected.';
  }
  $('replace-file').addEventListener('change', () => {
    releaseAudio('replace-preview', 'replace');
    const file = $('replace-file').files[0];
    if (!file) { $('replace-state').textContent = 'No new recording selected.'; return; }
    try {
      validateFile(file); replaceUrl = URL.createObjectURL(file);
      $('replace-preview').src = replaceUrl; $('replace-preview').hidden = false;
      $('replace-state').textContent = file.name + ' · ' + Math.ceil(file.size / 1024) + ' KiB · new recording';
    } catch (error) { $('replace-file').value = ''; notice(errorText(error), true); }
  });
  $('replace-same').addEventListener('change', event => {
    $('replace-text').disabled = event.target.checked; $('replace-text').required = !event.target.checked;
    if (event.target.checked) $('replace-text').value = '';
  });
  function editVoice(id) {
    const voice = voices.find(v => v.id === id); if (!voice) return;
    if (editor !== id) resetReplacement();
    editor = id; $('editor-panel').hidden = false; $('editor-id').textContent = id + ' · version ' + voice.version;
    $('editor-consent').textContent = voice.consent === 'confirmed' ? 'Current reference: permission acknowledged' : 'Reference consent not verified';
    $('edit-form').elements.name.value = voice.name;
    $('edit-form').elements.language.value = 'en';
    $('edit-form').elements.ref_text.value = voice.ref_text;
    releaseAudio('saved-preview', 'stored');
    $('editor-panel').scrollIntoView({block: 'start', behavior: 'smooth'});
  }
  $('edit-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!editor) return;
    const voice = voices.find(v => v.id === editor); if (!voice) return;
    const form = event.currentTarget;
    const name = form.elements.name.value.trim(), ref_text = form.elements.ref_text.value.trim();
    if (!name || !ref_text) { notice('Name and exact transcript are required.', true); return; }
    const changed = ref_text !== voice.ref_text;
    if (changed && !confirm('Changing the exact transcript increments reference identity version and invalidates conditioning cache. Continue?')) return;
    const button = $('edit-save'); busy(button, true);
    try {
      const patch = {name}; if (changed) { patch.ref_text = ref_text; patch.confirm_transcript_change = true; }
      await json('/profiles/' + encodeURIComponent(editor), jsonOptions('PUT', patch));
      clearOutput(); await loadVoices(); editVoice(editor); notice('Profile saved.');
    } catch (error) { notice(errorText(error), true); }
    finally { busy(button, false); }
  });
  $('reference-play').addEventListener('click', async () => {
    if (!editor) return;
    try {
      const response = await call('/profiles/' + encodeURIComponent(editor) + '/reference');
      const blob = await response.blob();
      releaseAudio('saved-preview', 'stored'); storedUrl = URL.createObjectURL(blob);
      $('saved-preview').src = storedUrl; $('saved-preview').hidden = false;
      notice('Stored reference ready for preview.');
    } catch (error) { notice(errorText(error), true); }
  });
  $('replace-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!editor) return;
    const button = $('replace-submit'), id = editor, same = $('replace-same').checked;
    if (!$('replace-consent').checked) { notice('Consent for this NEW recording is required.', true); return; }
    if (!same && !$('replace-text').value.trim()) { notice('Enter exact transcript for the new recording.', true); return; }
    try { validateFile($('replace-file').files[0]); } catch (error) { notice(errorText(error), true); return; }
    busy(button, true);
    try {
      const data = new FormData(event.currentTarget);
      data.set('consent', 'confirmed'); data.set('same_words', String(same));
      if (same) data.delete('ref_text'); else data.set('ref_text', $('replace-text').value.trim());
      const updated = await json('/profiles/' + encodeURIComponent(id) + '/audio', {method: 'PUT', body: data});
      clearOutput(); resetReplacement(); await loadVoices(); editVoice(id);
      $('replace-state').textContent = 'Saved version ' + updated.version;
      notice('Reference replaced; stable voice ID unchanged.');
    } catch (error) { $('replace-state').textContent = 'Replacement failed; prior version unchanged.'; notice(errorText(error), true); }
    finally { busy(button, false); }
  });
  async function removeVoice(purge) {
    if (!editor) return;
    const id = editor;
    if (!confirm(purge ? 'Delete this profile AND its owned audio files?' : 'Delete only the database record and keep its owned files?')) return;
    const button = $(purge ? 'delete-purge' : 'delete-only'); busy(button, true);
    try {
      const result = await json('/profiles/' + encodeURIComponent(id) + (purge ? '?purge=true' : ''), {method: 'DELETE'});
      if (!result.deleted) throw new Error('DELETE_UNCONFIRMED');
      editor = ''; $('editor-panel').hidden = true; resetReplacement(); releaseAudio('saved-preview', 'stored');
      clearOutput(); await loadVoices();
      notice(result.owned_reference_files_purged ? 'Profile and owned audio deleted.' : 'Profile record deleted; owned files retained.');
    } catch (error) { notice(errorText(error), true); } finally { busy(button, false); }
  }
  $('delete-purge').addEventListener('click', () => removeVoice(true));
  $('delete-only').addEventListener('click', () => removeVoice(false));
  $('migration-preview').addEventListener('click', async () => {
    try {
      const data = await json('/lite/migration/preview'), select = $('migration-choice');
      const eligible = data.eligible || [], invalid = data.invalid || [];
      select.replaceChildren(new Option('Choose eligible voice', ''));
      const list = $('migration-list'); list.replaceChildren();
      eligible.forEach(v => {
        select.add(new Option(v.name + ' · ' + v.id, v.id));
        const row = document.createElement('div'); row.className = 'voice-item';
        row.textContent = v.name + ' · ' + v.id + ' · English · eligible'; list.append(row);
      });
      invalid.forEach(v => {
        const row = document.createElement('div'); row.className = 'voice-item';
        row.textContent = (v.id || 'Unknown ID') + ' · invalid: ' + (v.reason || 'Not eligible');
        list.append(row);
      });
      $('migration-feedback').textContent = data.configured ?
        (eligible.length + ' eligible · ' + invalid.length + ' invalid · no import performed') :
        'Legacy source not configured; no import performed';
      $('migration-apply').disabled = true;
    } catch (error) { notice(errorText(error), true); }
  });
  $('migration-choice').addEventListener('change', () => { $('migration-apply').disabled = !$('migration-choice').value; });
  $('migration-apply').addEventListener('click', async () => {
    const id = $('migration-choice').value; if (!id) return;
    if (!confirm('Import only voice ' + id + ' from the trusted server-configured legacy source?')) return;
    const button = $('migration-apply'); busy(button, true);
    try {
      const result = await json('/lite/migration/apply', jsonOptions('POST', {voice_id: id}));
      $('migration-feedback').textContent = result.migrated ? 'Imported ' + id :
        (result.skipped ? 'Already imported: ' + id : 'No profile imported');
      await loadVoices();
      if (result.migrated || result.skipped) { setVoice(id); notice('Legacy voice selected.'); }
      else notice('No new profile imported.', true);
    } catch (error) { notice(errorText(error), true); } finally { $('migration-choice').value = ''; button.disabled = true; }
  });
  $('tts-pause-preset').addEventListener('change', () => {
    $('tts-pause-custom').hidden = $('tts-pause-preset').value !== 'custom';
  });
  function parsePause(text) {
    const regex = /\[[ \t]*pause(?:[ \t]+([0-9]+(?:\.[0-9]+)?)[ \t]*(ms|s))?[ \t]*\]/gi;
    const starts = [...text.matchAll(/\[[ \t]*pause(?=[ \t\]\r\n]|$)/gi)];
    const valid = [...text.matchAll(regex)]; if (starts.length !== valid.length) throw new Error('INVALID_PAUSE_MARKER');
    let duration = 0;
    for (const match of valid) {
      const raw = match[1] || '350', unit = match[2] || 'ms';
      if (unit.toLowerCase() === 'ms' && raw.includes('.')) throw new Error('INVALID_PAUSE_MARKER');
      if (unit.toLowerCase() === 's' && (raw.split('.')[1] || '').length > 3) throw new Error('INVALID_PAUSE_MARKER');
      const amount = Number(raw) * (unit.toLowerCase() === 's' ? 1000 : 1);
      if (!Number.isInteger(amount) || amount < 1 || amount > 10000) throw new Error('INVALID_PAUSE_MARKER');
      duration += amount;
    }
    if (valid.length > 16 || duration > 60000) throw new Error('PAUSE_LIMIT_EXCEEDED');
  }
  $('tts-insert-pause').addEventListener('click', () => {
    const preset = $('tts-pause-preset').value;
    const input = preset === 'custom' ? $('tts-pause-custom').value.trim() : preset;
    const amount = Number(input);
    if (!/^\d+$/.test(input) || !Number.isInteger(amount) || amount < 1 || amount > 10000) {
      notice('Pause must be 1–10000 whole milliseconds.', true); return;
    }
    const tag = amount === 350 ? '[pause]' : '[pause ' + amount + 'ms]';
    const editor = $('tts-text'), start = editor.selectionStart, end = editor.selectionEnd;
    // Insert before selection, never erase the user's selected words.
    const next = editor.value.slice(0, start) + tag + editor.value.slice(start);
    if (next.length > 4096) { notice('Narration length exceeds 4096 characters.', true); return; }
    try { parsePause(next); } catch (error) { notice(errorText(error), true); return; }
    editor.value = next; editor.focus(); editor.setSelectionRange(start + tag.length, end + tag.length);
    editor.dispatchEvent(new Event('input', {bubbles: true})); notice('Pause inserted.');
  });
  $('tts-speed').addEventListener('input', () => {
    $('tts-speed-value').textContent = Number($('tts-speed').value).toFixed(2) + '×';
  });
  function randomToken(count = 32) {
    const bytes = new Uint8Array(count); crypto.getRandomValues(bytes);
    return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }
  function readWav(buffer) {
    const view = new DataView(buffer), size = buffer.byteLength;
    if (size < 44 || view.getUint32(0, false) !== 0x52494646 || view.getUint32(8, false) !== 0x57415645) throw new Error('INVALID_WAV');
    let cursor = 12, fmt = false, data = false;
    while (cursor + 8 <= size) {
      const id = view.getUint32(cursor, false), length = view.getUint32(cursor + 4, true), at = cursor + 8;
      if (length > size - at) throw new Error('INVALID_WAV');
      if (id === 0x666d7420) {
        if (length < 16 || view.getUint16(at, true) !== 1 || view.getUint16(at + 2, true) !== 1 ||
            view.getUint32(at + 4, true) !== 24000 || view.getUint16(at + 14, true) !== 16) throw new Error('INVALID_WAV');
        fmt = true;
      }
      if (id === 0x64617461) data = length > 0;
      cursor = at + length + (length % 2);
    }
    if (!fmt || !data) throw new Error('INVALID_WAV');
  }
  function generationState(state) { $('tts-state').textContent = state; }
  $('tts-form').addEventListener('submit', async event => {
    event.preventDefault(); if (generating) return;
    const voice = $('tts-voice').value, text = $('tts-text').value;
    if (!voice || !voices.some(v => v.id === voice) || !text.trim()) {
      notice('Select a saved English voice and enter text.', true); return;
    }
    try { parsePause(text); } catch (error) { notice(errorText(error), true); return; }
    const seedRaw = $('tts-seed').value.trim(), seed = seedRaw === '' ? null : Number(seedRaw);
    const speed = Number($('tts-speed').value);
    if ((seed !== null && (!Number.isInteger(seed) || seed < 0 || seed > 2147483647)) ||
        speed < .75 || speed > 1.25) {
      notice('Invalid seed or speech speed.', true); return;
    }
    clearOutput(); const epoch = generationEpoch, profile = voices.find(v => v.id === voice);
    const identity = {id: profile.id, version: profile.version, reference: profile.ref_sha256};
    const cancel = {request_id: crypto.randomUUID ? crypto.randomUUID() : randomToken(18), cancel_token: randomToken(), acknowledged: false};
    activeCancel = cancel; generating = true; busy($('tts-submit'), true); $('tts-cancel').disabled = false;
    generationState('Generating…'); notice('');
    try {
      const response = await call('/v1/audio/speech', {
        method: 'POST', headers: {'Content-Type': 'application/json',
          'X-TTS-Request-Id': cancel.request_id, 'X-TTS-Cancel-Token': cancel.cancel_token},
        body: JSON.stringify({model: 'chatterbox-nano', voice, input: text,
          language: 'en', response_format: 'wav', speed, seed})
      });
      const headers = key => response.headers.get(key);
      if (!String(response.headers.get('content-type') || '').startsWith('audio/wav') ||
          headers('X-TTS-Engine') !== 'chatterbox-nano' || headers('X-TTS-Backend') !== 'CUDA0' ||
          !headers('X-TTS-Execution-Receipt')) throw new Error('INVALID_NANO_RECEIPT');
      const content = await response.arrayBuffer(); readWav(content);
      const latest = voices.find(v => v.id === identity.id);
      if (epoch !== generationEpoch || activeCancel !== cancel || chosen !== voice || !latest ||
          latest.version !== identity.version || latest.ref_sha256 !== identity.reference) {
        generationState(cancel.acknowledged ? 'Cancelled' : 'Discarded stale result'); notice('Voice, reference or cancellation state changed; discarded stale WAV.', true); return;
      }
      wavUrl = URL.createObjectURL(new Blob([content], {type: 'audio/wav'}));
      $('tts-player').src = wavUrl; $('tts-player').hidden = false;
      $('tts-download').href = wavUrl; $('tts-download').download = 'nano-' + voice + '.wav';
      $('tts-proof').textContent = 'Nano receipt present · engine Chatterbox-Nano · CUDA0 (server-reported)';
      $('tts-native-summary').textContent = 'CUDA execution proof is independent of readiness; inspect Runtime & Settings for live worker evidence.';
      $('tts-timing').textContent = 'WAV ' + asText(headers('X-TTS-Audio-S')) + ' s · Speech ' +
        asText(headers('X-Lite-Speech-S')) + ' s · Pauses ' + asText(headers('X-Lite-Pause-S')) + ' s';
      $('tts-breakdown').textContent = 'Speech WPM ' + asText(headers('X-TTS-Speech-WPM')) +
        ' · Final WPM ' + asText(headers('X-TTS-Final-WPM')) +
        ' · Speech spans ' + asText(headers('X-Lite-Speech-Segments')) +
        ' · Native chunks ' + asText(headers('X-TTS-Native-Chunks')) +
        ' · atempo ' + speed.toFixed(2) + '×';
      $('tts-output').hidden = false; generationState('Completed'); notice('Local 24 kHz WAV ready.');
    } catch (error) {
      if (epoch === generationEpoch) { generationState(error.code === 'CANCELLED' ? 'Cancelled' : error.code === 'WORKER_BUSY' ? 'Busy' : 'Failed'); notice(errorText(error), true); }
      else { generationState(cancel.acknowledged ? 'Cancelled' : 'Unknown'); }
    } finally {
      if (activeCancel === cancel) activeCancel = null;
      generating = false; busy($('tts-submit'), voices.length === 0); $('tts-cancel').disabled = true;
    }
  });
  $('tts-cancel').addEventListener('click', async () => {
    const cancel = activeCancel; if (!cancel || !generating) return;
    generationEpoch++; clearOutput(); $('tts-cancel').disabled = true; generationState('Cancelling…');
    try {
      const result = await json('/lite/worker/cancel', jsonOptions('POST', cancel));
      if (activeCancel !== cancel) return;
      if (result.cancelled && result.request_id === cancel.request_id) {
        cancel.acknowledged = true; generationState('Cancelled (cleanup pending)'); notice('Request-owned cancellation acknowledged; no WAV will be published.');
      } else { generationState('Unknown'); notice('Cancellation not confirmed by worker.', true); }
    } catch (error) { if (activeCancel === cancel) { generationState('Unknown'); notice('Cancellation could not be confirmed: ' + errorText(error), true); } }
  });
  async function refreshRuntime() {
    const sequence = ++runtimeSequence, button = $('runtime-refresh');
    busy(button, true); $('runtime-checked').textContent = 'Reading local Nano runtime…';
    try {
      const [status, settings, ready, capability, rights] = await Promise.all([
        json('/lite/worker/status'), json('/lite/worker/settings'), json('/lite/readiness'),
        json('/lite/model-capability'), json('/lite/rights')
      ]);
      if (sequence !== runtimeSequence) return;
      const proof = status.cuda && typeof status.cuda === 'object' ? status.cuda : {};
      const confirmed = yes => yes === true ? 'Verified' : 'Not verified';
      $('runtime-summary').replaceChildren(
        metric('Eligible to start', ready.cold_load_eligible === true ? 'Yes · not GPU proof' : 'No'),
        metric('Model hash verified', confirmed(status.model_hash_verified && ready.model_hash_verified)),
        metric('CUDA verified', confirmed(status.cuda_verified && ready.cuda_verified)),
        metric('Worker resident', status.resident === true ? 'Yes' : 'No'),
        metric('Live generation verified', confirmed(status.live_generation_verified && ready.live_generation_verified)),
        metric('Worker state', status.state), metric('Generations', status.generation_count ?? 0),
        metric('Cache hits', status.condition_cache_hits ?? 0)
      );
      $('runtime-details').replaceChildren(
        detail('Model', capability.model || status.model),
        detail('Backend', capability.backend),
        detail('Pinned model revision', capability.model_revision),
        detail('Pinned source revision', capability.source_revision),
        detail('Model manifest SHA-256', capability.weights_manifest_sha256),
        detail('CUDA evidence type', proof.evidence || 'Not measured'),
        detail('CUDA device', proof.name || proof.device || 'Not measured'),
        detail('GPU memory', JSON.stringify(status.memory || {state: 'NOT_MEASURED'})),
        detail('Runtime issues', (ready.issues || []).join(', ') || 'None reported'),
        detail('Last error', status.last_error || 'None reported')
      );
      $('worker-status').replaceChildren(
        metric('State', status.state), metric('Process', status.pid || 'Not running'),
        metric('Cold loads', status.load_count ?? 0), metric('Generations', status.generation_count ?? 0),
        metric('Cache hits', status.condition_cache_hits ?? 0),
        metric('Idle remaining', status.idle_remaining_seconds == null ? 'Not counting' : Math.round(status.idle_remaining_seconds) + ' s')
      );
      $('worker-idle').value = settings.idle_timeout_seconds === null ? 'never' : String(settings.idle_timeout_seconds);
      $('worker-unload').disabled = status.busy || !status.resident;
      $('worker-feedback').textContent = status.last_error ? 'Worker error: ' + status.last_error : (status.resident ? 'Model resident' : 'Model not loaded');
      $('rights-details').replaceChildren(
        detail('Application licence', rights.application_license),
        detail('Model licence', rights.model_license),
        detail('Model repository', rights.model_repo),
        detail('Model revision', rights.model_revision),
        detail('Source revision', rights.source_revision),
        detail('Voice consent required', rights.voice_consent_required === true ? 'Yes' : 'Not verified')
      );
      $('rights-notice').textContent = 'Local only. Model licence metadata does not prove ownership of reference voices or automatically grant rights to every generated output.';
      $('runtime-checked').textContent = 'Last refreshed ' + new Date().toLocaleString() + ' · eligibility is NOT CUDA verification.';
      $('status-short').textContent = status.busy ? 'Nano worker busy' : 'Local Nano service connected';
      $('status-dot').className = status.busy ? 'status-dot' : 'status-dot ok';
    } catch (error) {
      if (sequence === runtimeSequence) {
        $('runtime-checked').textContent = 'Status unavailable: ' + errorText(error);
        $('runtime-summary').replaceChildren(metric('Runtime status', 'Unknown / not verified'));
        $('worker-status').replaceChildren(metric('Worker', 'Unknown'));
        $('runtime-details').replaceChildren(detail('Runtime evidence', 'Unavailable'));
        $('rights-details').replaceChildren(detail('Rights evidence', 'Unavailable'));
        $('worker-unload').disabled = true;
        $('rights-notice').textContent = 'Rights information unavailable.';
        notice(errorText(error), true);
      }
    } finally { if (sequence === runtimeSequence) busy(button, false); }
  }
  $('runtime-refresh').addEventListener('click', refreshRuntime);
  $('worker-settings-form').addEventListener('submit', async event => {
    event.preventDefault(); const button = $('worker-save'), raw = $('worker-idle').value;
    busy(button, true);
    try {
      await json('/lite/worker/settings', jsonOptions('PUT', {idle_timeout_seconds: raw === 'never' ? null : Number(raw)}));
      await refreshRuntime(); notice('Idle timeout saved.');
    } catch (error) { notice(errorText(error), true); } finally { busy(button, false); }
  });
  $('worker-unload').addEventListener('click', async () => {
    const button = $('worker-unload'); busy(button, true);
    try {
      const response = await json('/lite/worker/unload', {method: 'POST'});
      if (!response.unloaded) throw new Error('UNLOAD_UNCONFIRMED');
      await refreshRuntime(); notice('Nano worker unloaded.');
    } catch (error) { notice(errorText(error), true); } finally { busy(button, false); }
  });
  addEventListener('pagehide', () => {
    clearCloneAudio(false); releaseAudio('replace-preview', 'replace'); releaseAudio('saved-preview', 'stored');
    clearOutput();
  });
  $('tts-output').hidden = true;
  $('tts-cancel').disabled = true;
  loadVoices();
  openView(pages[location.hash.slice(1)] ? location.hash.slice(1) : 'clone');
})();