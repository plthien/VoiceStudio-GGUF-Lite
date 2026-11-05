/* Separate Chatterbox UI: strict same-origin routes, no remote dependencies. */
'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  let voices = [], busy = false, currentWav = null, generationToken = 0;
  function notice(msg, error = false) { const el = $('#notice'); el.textContent = msg || ''; el.dataset.error = String(error); }
  async function call(path, opts = {}) {
    const r = await fetch(path, {cache:'no-store', ...opts});
    if (!r.ok) {
      let code = 'HTTP_'+r.status;
      try { const j = await r.json(); code = j.error?.code || code; } catch (_e) {}
      throw new Error(code);
    }
    return r;
  }
  async function json(path, opts = {}) { return (await call(path, opts)).json(); }
  function show(tab) {
    $$('[role=tab]').forEach(el => el.setAttribute('aria-selected', String(el.dataset.tab === tab)));
    $$('[data-panel]').forEach(el => el.hidden = el.dataset.panel !== tab);
    if (tab === 'library' || tab === 'generate') refreshVoices().catch(e=>notice(e.message,true));
    if (tab === 'runtime') refreshRuntime().catch(e=>notice(e.message,true));
  }
  $$('[role=tab]').forEach(el => el.addEventListener('click', () => show(el.dataset.tab)));
  const safe = value => String(value == null ? '' : value);
  async function refreshVoices() {
    voices = await json('/profiles');
    const select = $('#voice'), prior = select.value;
    select.replaceChildren();
    voices.forEach(v => { const op = document.createElement('option'); op.value = safe(v.id); op.textContent = safe(v.name)+' · v'+safe(v.version); select.append(op); });
    if (voices.some(v=>v.id===prior)) select.value = prior;
    $('#generate').disabled = busy || voices.length === 0;
    const list = $('#voice-list'); list.replaceChildren();
    if (!voices.length) list.textContent='No owned voices yet. Import one in Voice Cloning.';
    voices.forEach(v=>{
      const card=document.createElement('article');card.className='voice-card';
      const title=document.createElement('strong');title.textContent=safe(v.name)+' · version '+safe(v.version);
      card.append(title, document.createElement('p'));card.lastChild.textContent='English · consent: '+safe(v.consent);
      function button(label, action) {const b=document.createElement('button');b.type='button';b.textContent=label;b.addEventListener('click',()=>action(v).catch(e=>notice(e.message,true)));card.append(b);}
      button('Preview', async v=>{const response=await call('/profiles/'+encodeURIComponent(v.id)+'/reference'); const blob=await response.blob(); const url=URL.createObjectURL(blob); const player=new Audio(url); const release=()=>URL.revokeObjectURL(url);player.onended=release;player.onerror=release;try{await player.play();}catch(error){release();throw error;}});
      button('Replace reference',async v=>{const pane=$('#replace-panel');pane.hidden=false;pane.querySelector('[name=voice_id]').value=v.id;pane.scrollIntoView({behavior:'smooth'});});
      button('Edit name', async v=>{const name=prompt('Voice name',v.name);if(!name)return;await json('/profiles/'+encodeURIComponent(v.id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});notice('Voice updated');await refreshVoices();});
      button('Delete record', async v=>{if(!confirm('Delete the library record but retain owned files?'))return;await json('/profiles/'+encodeURIComponent(v.id),{method:'DELETE'});notice('Voice record deleted');await refreshVoices();});
      button('Delete + purge', async v=>{if(!confirm('Permanently purge this voice and owned files?'))return;await json('/profiles/'+encodeURIComponent(v.id)+'?purge=true',{method:'DELETE'});notice('Voice and owned files purged');await refreshVoices();});
      list.append(card);
    });
  }
  $('#refresh-library').addEventListener('click',()=>refreshVoices().catch(e=>notice(e.message,true)));
  $('#clone-form').addEventListener('submit',async e=>{
    e.preventDefault();const form=e.currentTarget;const data=new FormData(form);if(!$('#consent').checked){notice('Explicit consent required',true);return;}
    data.set('consent','confirmed');
    try{const p=await json('/profiles',{method:'POST',body:data});notice('Imported '+p.name);form.reset();await refreshVoices();show('library');}
    catch(ex){notice(ex.message,true);}
  });
  $('#replace-form').addEventListener('submit',async e=>{
    e.preventDefault();const data=new FormData(e.currentTarget);const id=data.get('voice_id');data.delete('voice_id');
    if(!$('#replace-consent').checked){notice('Explicit consent required',true);return;}data.set('consent','confirmed');data.set('same_words',String($('#same-words').checked));
    try{await json('/profiles/'+encodeURIComponent(id)+'/audio',{method:'PUT',body:data});notice('Reference replaced');$('#replace-panel').hidden=true;await refreshVoices();}
    catch(ex){notice(ex.message,true);}
  });
  $('#speed').addEventListener('input',()=>$('#speed-label').textContent=Number($('#speed').value).toFixed(2)+'×');
  function parsePauseDuration(raw) {
    const match = /^([0-9]+(?:\.[0-9]+)?)(ms|s)$/i.exec(raw.trim());
    if (!match) throw new Error('INVALID_PAUSE_MARKER');
    if (match[2].toLowerCase() === 'ms' && match[1].includes('.')) throw new Error('INVALID_PAUSE_MARKER');
    if (match[2].toLowerCase() === 's' && (match[1].split('.')[1] || '').length > 3) throw new Error('INVALID_PAUSE_MARKER');
    const milliseconds = Number(match[1]) * (match[2].toLowerCase() === 's' ? 1000 : 1);
    if (!Number.isInteger(milliseconds) || milliseconds < 1 || milliseconds > 10000) throw new Error('INVALID_PAUSE_MARKER');
    return milliseconds;
  }
  function insertPause(marker) {
    const el=$('#input'),start=el.selectionStart,end=el.selectionEnd;
    const next=el.value.slice(0,start)+marker+el.value.slice(end);
    if (next.length>4096) throw new Error('INPUT_TOO_LONG');
    const starts=[...next.matchAll(/\[[ \t]*pause(?=[ \t\]\r\n]|$)/gi)];
    const valid=[...next.matchAll(/\[[ \t]*pause(?:[ \t]+([0-9]+(?:\.[0-9]+)?)[ \t]*(ms|s))?[ \t]*\]/gi)];
    if (starts.length!==valid.length) throw new Error('INVALID_PAUSE_MARKER');
    let total=0;
    for (const match of valid) total+=match[1] ? parsePauseDuration(match[1]+match[2]) : 350;
    if (valid.length>16 || total>60000) throw new Error('PAUSE_LIMIT_EXCEEDED');
    el.value=next;el.focus();el.setSelectionRange(start+marker.length,start+marker.length);
    notice('Pause inserted');
  }
  $$('[data-pause]').forEach(b=>b.addEventListener('click',()=>{
    try { insertPause(b.dataset.pause); } catch(e) { notice(e.message,true); }
  }));
  $('#insert-custom-pause').addEventListener('click',()=>{
    try {
      const raw=$('#custom-pause').value.trim();
      parsePauseDuration(raw);
      insertPause('[pause '+raw+']');
    } catch(e) { notice(e.message,true); }
  });
  function setBusy(value){busy=value;$('#generate').disabled=value||voices.length===0;$('#cancel').disabled=!value;$('#progress').textContent=value?'Generating (single CUDA task)...':'';}
  $('#generate-form').addEventListener('submit', async e=>{
    e.preventDefault();if(busy)return;
    const token=++generationToken, frozenVoice=$('#voice').value;setBusy(true);notice('');
    try{
      const payload={model:'chatterbox-nano',voice:$('#voice').value,input:$('#input').value,
        response_format:'wav',language:'en',speed:Number($('#speed').value),seed:Number($('#seed').value)};
      const response=await call('/v1/audio/speech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.blob();if(token!==generationToken||$('#voice').value!==frozenVoice){notice('Result discarded because the selected voice changed',true);return;}if(currentWav)URL.revokeObjectURL(currentWav);currentWav=URL.createObjectURL(data);
      $('#playback').src=currentWav;$('#download').href=currentWav;$('#download').hidden=false;
      const header=k=>response.headers.get(k)||'—';
      $('#metrics').textContent='Speech WPM: '+header('X-TTS-Speech-WPM')+' · Final WPM: '+header('X-TTS-Final-WPM')+
        '\nElapsed: '+header('X-TTS-Elapsed-S')+' s · Audio: '+header('X-TTS-Audio-S')+' s · RTF: '+header('X-TTS-RTF')+
        '\nSpeech: '+header('X-Lite-Speech-S')+' s · pauses: '+header('X-Lite-Pause-S')+' s · native chunks: '+header('X-Lite-Speech-Segments');
      notice('Generation complete');
    }catch(ex){notice(ex.message,true);}finally{setBusy(false);}
  });
  $('#cancel').addEventListener('click',async()=>{generationToken++;try{await json('/lite/worker/cancel',{method:'POST'});notice('Cancellation requested');}catch(e){notice(e.message,true);}});
  async function refreshRuntime(){
    const [state,settings,ready,rights]=await Promise.all([json('/lite/worker/status'),json('/lite/worker/settings'),json('/lite/readiness'),json('/lite/rights')]);
    $('#runtime-status').textContent=JSON.stringify({state:state.state,cuda:state.cuda,loaded:state.resident,
      pid:state.pid,loads:state.load_count,generations:state.generation_count,cache_hits:state.condition_cache_hits,
      idle_remaining_seconds:state.idle_remaining_seconds,model_revision:state.model_revision,
      source_revision:ready.source_revision,issues:ready.issues},null,2);$('#rights').textContent=JSON.stringify(rights,null,2);
    $('#idle').value=settings.idle_timeout_seconds===null?'never':String(settings.idle_timeout_seconds);
  }
  $('#refresh-runtime').addEventListener('click',()=>refreshRuntime().catch(e=>notice(e.message,true)));
  $('#save-settings').addEventListener('click',async()=>{
    const raw=$('#idle').value,timeout=raw==='never'?null:Number(raw);
    try{await json('/lite/worker/settings',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idle_timeout_seconds:timeout})});notice('Idle setting saved');await refreshRuntime();}catch(ex){notice(ex.message,true);}
  });
  $('#unload').addEventListener('click',async()=>{try{await json('/lite/worker/unload',{method:'POST'});notice('Worker unloaded');await refreshRuntime();}catch(e){notice(e.message,true);}});
  $('#migration-preview').addEventListener('click',async()=>{try{const result=await json('/lite/migration/preview');const select=$('#migration-choice');select.replaceChildren(new Option('Choose an eligible voice',''));result.eligible.forEach(v=>select.append(new Option(v.name,v.id)));$('#migration-feedback').textContent=result.configured?(result.eligible.length+' eligible · '+result.invalid.length+' invalid'):'Legacy source is not configured';$('#migration-apply').disabled=true;}catch(e){notice(e.message,true);}});
  $('#migration-choice').addEventListener('change',()=>{$('#migration-apply').disabled=!$('#migration-choice').value;});
  $('#migration-apply').addEventListener('click',async()=>{try{const id=$('#migration-choice').value;const result=await json('/lite/migration/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({voice_id:id})});$('#migration-feedback').textContent='Imported '+result.migrated+' profile';await refreshVoices();}catch(e){notice(e.message,true);}});
  $('#diagnostic').addEventListener('click',async()=>{const out=$('#diagnostic-status');out.textContent='Running three sequential requests…';let passed=0;try{for(let i=0;i<3;i++){const r=await call('/v1/audio/speech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'chatterbox-nano',voice:$('#voice').value,input:'Nano diagnostic request '+(i+1)+'.',response_format:'wav',language:'en',speed:1,seed:42+i})});await r.blob();passed++;}out.textContent='Diagnostic PASS: '+passed+'/3';}catch(e){out.textContent='Diagnostic failed after '+passed+'/3: '+e.message;}});
  addEventListener('pagehide',()=>{if(currentWav){URL.revokeObjectURL(currentWav);currentWav=null;}});
  refreshVoices().catch(e=>notice(e.message,true));
})();
