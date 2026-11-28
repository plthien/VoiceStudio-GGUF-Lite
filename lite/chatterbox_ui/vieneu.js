/* Isolated VieNeu controller for the EXISTING VoiceStudio UI. No framework, no reload. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const ENGINE = 'vieneu-v3-nano', CONTRACT = 'vieneu_cpu_onnx_api_v1';
  const state = {mode: 'chatterbox', voices: [], selected: '', editing: '',
                 profiles: [], generation: null, sequence: 0, sound: null, reference: null};
  const say = (id, message) => { $(id).textContent = message; };
  function selected() { return state.voices.find(v => v.id === state.selected); }
  function clearSound() {
    if (state.sound) URL.revokeObjectURL(state.sound);
    state.sound = null; $('vi-tts-output').hidden = true;
    $('vi-tts-player').removeAttribute('src');
    $('vi-download').removeAttribute('href');
  }
  function setReference(url, target) {
    if (state.reference) URL.revokeObjectURL(state.reference);
    state.reference = url;
    target.src = url; target.hidden = false;
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {cache: 'no-store', ...options});
    if (!response.ok) {
      let code = 'HTTP_' + response.status;
      try { const x = await response.json(); code = x.error?.code || code; } catch (_) {}
      throw Error(code);
    }
    return response;
  }
  async function json(path, options) { return (await request(path, options)).json(); }
  const payload = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  function selectVoice(value) {
    if (state.selected !== value) clearSound();
    state.selected = value;
    $('vi-tts-voice').value = value;
    if (state.mode === 'vieneu') {
      const voice = selected();
      $('selected-voice').textContent = voice ? voice.name + ' · ' + voice.id : 'No VieNeu voice selected';
    }
  }
  function engineSelected() {
    const mode = $('provider-select').value;
    if (state.generation && mode !== state.mode) {
      say('vi-tts-status', 'Generation still in progress; changing view will not cancel it.');
    }
    state.mode = mode;
    document.body.dataset.provider = mode;
    // Existing Chatterbox controls and backend retain their original independent state.
    if (mode === 'vieneu') {
      say('status-short', 'VieNeu CPU/ONNX mode');
      selectVoice(state.selected);
      loadVoices();
      refreshRuntime();
    } else {
      $('selected-voice').textContent = 'Chatterbox voice selected in English workspace';
      fetch('/lite/worker/status', {cache:'no-store'}).then(r => r.json()).then(x => {
        if (state.mode === 'chatterbox')
          say('status-short', x.busy ? 'Nano worker busy' : 'Local Nano service connected');
      }).catch(() => {});
    }
  }
  $('provider-select').addEventListener('change', engineSelected);
  function formatVoice(v) {
    return v.name + ' · ' + (v.voice_type === 'preset' ? 'preset' : 'cloned v' + v.voice_version);
  }
  function displayVoices() {
    const container = $('vi-library-list');
    container.replaceChildren();
    const combo = $('vi-tts-voice');
    combo.replaceChildren(new Option('Choose a VieNeu voice', ''));
    for (const v of state.voices) {
      const row = document.createElement('div'); row.className = 'voice-item';
      const meta = document.createElement('span');
      meta.textContent = formatVoice(v);
      const actions = document.createElement('div'); actions.className = 'actions';
      const use = document.createElement('button');
      use.type = 'button'; use.className = 'secondary'; use.textContent = 'Use in TTS';
      use.addEventListener('click', () => {
        selectVoice(v.id);
        const nav = document.querySelector('.nav-button[data-view="tts"]');
        nav.click();
      });
      actions.append(use);
      if (v.voice_type === 'cloned') {
        const edit = document.createElement('button');
        edit.type = 'button'; edit.className = 'secondary'; edit.textContent = 'Manage';
        edit.addEventListener('click', () => showEditor(v));
        actions.append(edit);
      } else {
        const preview = document.createElement('button');
        preview.type = 'button'; preview.className = 'secondary'; preview.textContent = 'Preview';
        preview.addEventListener('click', () => previewVoice(v.id));
        actions.append(preview);
      }
      row.append(meta, actions); container.append(row);
      combo.add(new Option(formatVoice(v), v.id));
    }
    if (!state.voices.some(v => v.id === state.selected))
      state.selected = state.voices[0]?.id || '';
    selectVoice(state.selected);
    $('vi-generate').disabled = !state.voices.length || !!state.generation;
    if (state.editing && !state.profiles.some(v => v.id === state.editing)) {
      state.editing = ''; $('vi-edit').hidden = true;
    }
    if (!state.voices.length)
      say('vi-library-status', 'No VieNeu voices available. Install and verify the CPU model first.');
  }
  async function loadVoices() {
    const n = ++state.sequence;
    const result = await Promise.allSettled([json('/vieneu/profiles'), json('/vieneu/presets')]);
    if (n !== state.sequence) return;
    const profiles = result[0].status === 'fulfilled' ? result[0].value : [];
    const presets = result[1].status === 'fulfilled' ? result[1].value : [];
    state.profiles = profiles;
    const oldVoice = selected();
    state.voices = [...presets, ...profiles];
    const newVoice = selected();
    if (oldVoice && (!newVoice ||
         newVoice.voice_version !== oldVoice.voice_version ||
         newVoice.reference_sha256 !== oldVoice.reference_sha256))
      clearSound();
    displayVoices();
    if (result[1].status === 'rejected')
      say('vi-library-status', 'Preset loading: ' + result[1].reason.message);
    else
      say('vi-library-status', profiles.length + ' cloned voices · ' + presets.length + ' presets');
  }
  $('vi-library-refresh').addEventListener('click', loadVoices);
  $('vi-tts-voice').addEventListener('change', e => selectVoice(e.target.value));
  function fileCheck(input) {
    const f = input.files?.[0];
    if (!f || f.size < 64 || f.size > 24 * 1024 * 1024 || !/\.(wav|mp3)$/i.test(f.name))
      throw Error('INVALID_AUDIO');
    return f;
  }
  $('vi-clone-file').addEventListener('change', () => {
    const f = $('vi-clone-file').files?.[0];
    if (!f) return;
    try { fileCheck($('vi-clone-file')); setReference(URL.createObjectURL(f), $('vi-clone-audio')); }
    catch (error) { say('vi-clone-status', error.message); }
  });
  $('vi-clone-form').addEventListener('submit', async e => {
    e.preventDefault();
    const button = $('vi-clone-submit');
    try {
      const file = fileCheck($('vi-clone-file'));
      if (!$('vi-clone-consent').checked) throw Error('VOICE_CONSENT_REQUIRED');
      button.disabled = true;
      say('vi-clone-status', 'Encoding speaker embedding and style tokens…');
      const data = new FormData();
      data.set('name', $('vi-clone-name').value.trim());
      data.set('consent', 'confirmed'); data.set('ref_audio', file);
      const voice = await json('/vieneu/profiles', {method: 'POST', body: data});
      $('vi-clone-form').reset();
      say('vi-clone-status', 'Saved ' + voice.name + ' · v' + voice.voice_version);
      await loadVoices();
      selectVoice(voice.id);
      document.querySelector('.nav-button[data-view="library"]').click();
      previewVoice(voice.id);
    } catch (error) { say('vi-clone-status', error.message); }
    finally { button.disabled = false; }
  });
  function showEditor(voice) {
    state.editing = voice.id;
    $('vi-edit').hidden = false;
    $('vi-edit-id').textContent = voice.id + ' · version ' + voice.voice_version;
    $('vi-rename-name').value = voice.name;
  }
  $('vi-rename-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (!state.editing) return;
    try {
      const v = await json('/vieneu/profiles/' + state.editing, payload('PUT', {name: $('vi-rename-name').value.trim()}));
      await loadVoices(); showEditor(v);
      say('vi-library-status', 'Profile renamed.');
    } catch (error) { say('vi-library-status', error.message); }
  });
  $('vi-verify').addEventListener('click', async () => {
    if (!state.editing) return;
    try {
      const result = await json('/vieneu/profiles/' + state.editing + '/integrity');
      say('vi-library-status', result.verified ? 'SHA-256 integrity verified · voice v' + result.voice_version : 'Not verified');
    } catch (error) { say('vi-library-status', error.message); }
  });
  $('vi-play-reference').addEventListener('click', async () => {
    if (!state.editing) return;
    try {
      const response = await request('/vieneu/profiles/' + state.editing + '/reference');
      const blob = await response.blob();
      setReference(URL.createObjectURL(blob), $('vi-reference-player'));
    } catch (error) { say('vi-library-status', error.message); }
  });
  $('vi-replace-form').addEventListener('submit', async e => {
    e.preventDefault();
    const button = $('vi-replace-submit');
    try {
      if (!state.editing || !$('vi-replace-consent').checked) throw Error('VOICE_CONSENT_REQUIRED');
      const file = fileCheck($('vi-replace-file'));
      button.disabled = true; say('vi-library-status', 'Encoding new reference…');
      const data = new FormData(); data.set('consent', 'confirmed'); data.set('ref_audio', file);
      const updated = await json('/vieneu/profiles/' + state.editing + '/reference', {method: 'PUT', body: data});
      clearSound(); await loadVoices(); showEditor(updated);
      $('vi-replace-form').reset();
      say('vi-library-status', 'Updated voice v' + updated.voice_version + '. Previous conditioning invalidated.');
    } catch (error) { say('vi-library-status', error.message); }
    finally { button.disabled = false; }
  });
  $('vi-delete').addEventListener('click', async () => {
    if (!state.editing || !window.confirm('Permanently delete this VieNeu voice and its stored audio?')) return;
    try {
      const deleted = state.editing;
      await json('/vieneu/profiles/' + deleted, {method: 'DELETE'});
      state.editing = ''; $('vi-edit').hidden = true;
      if (state.selected === deleted) state.selected = '';
      clearSound(); await loadVoices();
    } catch (error) { say('vi-library-status', error.message); }
  });
  function wavCheck(buffer) {
    if (buffer.byteLength < 46) throw Error('INVALID_WAV');
    const d = new DataView(buffer);
    if (d.getUint32(0, false) !== 0x52494646 || d.getUint32(8, false) !== 0x57415645)
      throw Error('INVALID_WAV');
    let fmt = false, data = false, pos = 12;
    while (pos + 8 <= d.byteLength) {
      const id = d.getUint32(pos, false), size = d.getUint32(pos + 4, true), start = pos + 8;
      if (start + size > d.byteLength) throw Error('INVALID_WAV');
      if (id === 0x666d7420) {
        if (size < 16 || d.getUint16(start, true) !== 1 || d.getUint16(start + 2, true) !== 1 ||
            d.getUint32(start + 4, true) !== 24000 || d.getUint16(start + 14, true) !== 16)
          throw Error('INVALID_WAV');
        fmt = true;
      }
      if (id === 0x64617461 && size > 0 && size % 2 === 0) data = true;
      pos = start + size + (size % 2);
    }
    if (!fmt || !data) throw Error('INVALID_WAV');
  }
  function newCancel() {
    const bytes = new Uint8Array(24);
    crypto.getRandomValues(bytes);
    const token = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
    return {request_id: crypto.randomUUID ? crypto.randomUUID() : token, cancel_token: token,
            cancelled: false};
  }
  async function synthesize(input, voice, options, isPreview=false) {
    if (state.generation) throw Error('WORKER_BUSY');
    clearSound();
    const owner = newCancel();
    const startingVoice = selected();
    const initialIdentity = startingVoice ? [startingVoice.id, startingVoice.voice_version, startingVoice.reference_sha256] : null;
    state.generation = owner;
    $('vi-generate').disabled = true; $('vi-cancel').disabled = false;
    say('vi-tts-status', 'Generating VieNeu CPU audio…');
    try {
      const response = await request('/vieneu/v1/audio/speech', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-TTS-Request-Id': owner.request_id,
                  'X-TTS-Cancel-Token': owner.cancel_token},
        body: JSON.stringify({model: ENGINE, language: 'vi', response_format: 'wav', voice, input, ...options})
      });
      if (response.headers.get('X-TTS-Engine') !== ENGINE ||
          response.headers.get('X-TTS-Backend') !== 'CPUExecutionProvider' ||
          response.headers.get('X-VieNeu-Contract') !== CONTRACT ||
          !response.headers.get('X-VieNeu-Receipt'))
        throw Error('INVALID_VIENEU_RECEIPT');
      const bytes = await response.arrayBuffer();
      wavCheck(bytes);
      const latest = selected();
      if (owner.cancelled || state.generation !== owner || state.selected !== voice ||
          !latest || !initialIdentity || latest.voice_version !== initialIdentity[1] ||
          latest.reference_sha256 !== initialIdentity[2]) {
        say('vi-tts-status', 'Discarded: voice selection or reference changed');
        return;
      }
      state.sound = URL.createObjectURL(new Blob([bytes], {type:'audio/wav'}));
      $('vi-tts-player').src = state.sound;
      $('vi-download').href = state.sound;
      $('vi-download').download = 'vieneu-' + voice.replace(/[^A-Za-z0-9_-]/g, '-') + '.wav';
      $('vi-tts-output').hidden = false;
      say('vi-output-summary', '24 kHz PCM16 · ' + response.headers.get('X-TTS-Audio-S') +
          ' s · approx. ' + response.headers.get('X-TTS-Approx-Syllables-Per-Minute') + ' syllables/minute');
      say('vi-tts-status', 'Complete · independently verified WAV and VieNeu receipt');
      if (isPreview) $('vi-tts-player').play().catch(() => {});
    } catch (error) {
      if (state.generation === owner)
        say('vi-tts-status', owner.cancelled ? 'Cancelled' : error.message);
      throw error;
    } finally {
      if (state.generation === owner) state.generation = null;
      $('vi-generate').disabled = !state.voices.length;
      $('vi-cancel').disabled = true;
    }
  }
  async function previewVoice(voice) {
    try {
      document.querySelector('.nav-button[data-view="tts"]').click();
      selectVoice(voice);
      await synthesize('Xin chào. Đây là bản nghe thử giọng nói tiếng Việt.', voice,
                       {speed: 1, steps: 16, cfg: 3, sway: 0, seed: null}, true);
    } catch (error) { say('vi-tts-status', error.message); }
  }
  $('vi-speed').addEventListener('input', () => say('vi-speed-label', Number($('vi-speed').value).toFixed(2) + '×'));
  $('vi-insert-pause').addEventListener('click', () => {
    const element = $('vi-tts-text');
    const at = element.selectionStart;
    const ms = $('vi-pause-length').value;
    const tag = ms === '350' ? '[pause]' : '[pause ' + ms + 'ms]';
    if (element.value.length + tag.length > 24000) return;
    element.value = element.value.slice(0, at) + tag + element.value.slice(at);
    element.focus(); element.setSelectionRange(at + tag.length, at + tag.length);
  });
  $('vi-tts-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const voice = $('vi-tts-voice').value, input = $('vi-tts-text').value;
      if (!voice || !input.trim()) throw Error('INVALID_REQUEST');
      const number = id => Number($(id).value);
      const seed = $('vi-seed').value === '' ? null : number('vi-seed');
      await synthesize(input, voice, {speed:number('vi-speed'),steps:number('vi-steps'),
                                      cfg:number('vi-cfg'),sway:number('vi-sway'),seed});
    } catch (error) { say('vi-tts-status', error.message); }
  });
  $('vi-cancel').addEventListener('click', async () => {
    const owner = state.generation;
    if (!owner) return;
    owner.cancelled = true; clearSound(); $('vi-cancel').disabled = true;
    say('vi-tts-status', 'Cancelling…');
    try {
      const result = await json('/vieneu/worker/cancel', payload('POST', owner));
      say('vi-tts-status', result.cancelled ? 'Cancelled; no output published' : 'Cancellation unconfirmed');
    } catch (error) { say('vi-tts-status', 'Cancellation not confirmed: ' + error.message); }
  });
  async function refreshRuntime() {
    if (state.mode !== 'vieneu') return;
    const results = await Promise.allSettled([
      json('/vieneu/readiness'),json('/vieneu/worker/status'),json('/vieneu/capability'),json('/vieneu/rights')
    ]);
    if (results.some(x => x.status === 'rejected')) {
      say('vi-runtime-status', 'VieNeu runtime unavailable');
      return;
    }
    const [ready, worker, capability, rights] = results.map(x => x.value);
    const metrics = [
      ['Eligible', ready.ready ? 'Yes · preflight only' : 'No'],
      ['Worker', worker.resident ? 'Resident' : 'Unloaded'],
      ['Busy', String(worker.busy)], ['Backend', worker.backend],
      ['CPU threads', worker.cpu_threads], ['Generations', worker.generation_count],
      ['Model revision', capability.model_revision], ['Runtime issues', (ready.issues || []).join(', ') || 'None']
    ];
    const root = $('vi-runtime-summary'); root.replaceChildren();
    metrics.forEach(([a,b]) => {
      const el = document.createElement('div'); el.className = 'metric';
      const label = document.createElement('span'); label.textContent = a;
      const value = document.createElement('strong'); value.textContent = b;
      el.append(label, value); root.append(el);
    });
    const details = $('vi-runtime-details'); details.replaceChildren();
    for (const [a,b] of [['Model repository', rights.model_repo],
                         ['SDK pinned revision', capability.sdk_revision],
                         ['Model licence', rights.model_license],
                         ['Last error', worker.last_error || 'None']]) {
      const el = document.createElement('div'); el.className = 'detail';
      const label = document.createElement('span'); label.textContent = a;
      const value = document.createElement('strong'); value.textContent = b;
      el.append(label, value); details.append(el);
    }
    $('vi-unload').disabled = !!worker.busy || !worker.resident;
    say('vi-runtime-status', ready.ready ? 'Offline preflight eligible' : 'Offline preflight incomplete');
  }
  $('vi-runtime-refresh').addEventListener('click', refreshRuntime);
  $('vi-unload').addEventListener('click', async () => {
    try {
      await json('/vieneu/worker/unload', {method:'POST'});
      await refreshRuntime();
    } catch (error) { say('vi-runtime-status', error.message); }
  });
  window.addEventListener('pagehide', () => {
    clearSound();
    if (state.reference) URL.revokeObjectURL(state.reference);
  });
  engineSelected();
})();
