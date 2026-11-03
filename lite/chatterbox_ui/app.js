/* Separate Chatterbox UI: strict same-origin routes, no remote dependencies. */
'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  let voices = [], busy = false, currentWav = null;
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
      button('Preview', async v=>{const response=await call('/profiles/'+encodeURIComponent(v.id)+'/reference'); const blob=await response.blob(); const url=URL.createObjectURL(blob); const player=new Audio(url); player.onended=()=>URL.revokeObjectURL(url); await player.play();});
      button('Replace reference',async v=>{const pane=$('#replace-panel');pane.hidden=false;pane.querySelector('[name=voice_id]').value=v.id;pane.scrollIntoView({behavior:'smooth'});});
      button('Delete', async v=>{if(!confirm('Delete voice '+v.name+'?'))return;await json('/profiles/'+encodeURIComponent(v.id)+'?purge=true',{method:'DELETE'});notice('Voice deleted');await refreshVoices();});
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
    if(!$('#replace-consent').checked){notice('Explicit consent required',true);return;}data.set('consent','confirmed');
    try{await json('/profiles/'+encodeURIComponent(id)+'/audio',{method:'PUT',body:data});notice('Reference replaced');$('#replace-panel').hidden=true;await refreshVoices();}
    catch(ex){notice(ex.message,true);}
  });
  $('#speed').addEventListener('input',()=>$('#speed-label').textContent=Number($('#speed').value).toFixed(2)+'×');
  $$('[data-pause]').forEach(b=>b.addEventListener('click',()=>{
    const el=$('#input'),index=el.selectionStart,text=b.dataset.pause;
    el.value=el.value.slice(0,index)+text+el.value.slice(el.selectionEnd);
    el.focus();el.setSelectionRange(index+text.length,index+text.length);
  }));
  function setBusy(value){busy=value;$('#generate').disabled=value||voices.length===0;$('#cancel').disabled=!value;$('#progress').textContent=value?'Generating (single CUDA task)...':'';}
  $('#generate-form').addEventListener('submit', async e=>{
    e.preventDefault();if(busy)return;
    setBusy(true);notice('');
    try{
      const payload={model:'chatterbox-turbo',voice:$('#voice').value,input:$('#input').value,
        response_format:'wav',language:'en',speed:Number($('#speed').value),seed:42};
      const response=await call('/v1/audio/speech',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.blob();if(currentWav)URL.revokeObjectURL(currentWav);currentWav=URL.createObjectURL(data);
      $('#playback').src=currentWav;$('#download').href=currentWav;$('#download').hidden=false;
      const header=k=>response.headers.get(k)||'—';
      $('#metrics').textContent='Speech WPM: '+header('X-TTS-Speech-WPM')+' · Final WPM: '+header('X-TTS-Final-WPM')+
        '\nElapsed: '+header('X-TTS-Elapsed-S')+' s · Audio: '+header('X-TTS-Audio-S')+' s · RTF: '+header('X-TTS-RTF')+
        '\nPauses: '+header('X-Lite-Pause-Count')+' · Speed mode: '+header('X-TTS-Speed-Mode');
      notice('Generation complete');
    }catch(ex){notice(ex.message,true);}finally{setBusy(false);}
  });
  $('#cancel').addEventListener('click',async()=>{try{await json('/chatterbox/runtime/cancel',{method:'POST'});notice('Cancellation requested');}catch(e){notice(e.message,true);}});
  async function refreshRuntime(){
    const [state,settings,ready]=await Promise.all([json('/chatterbox/runtime/status'),json('/chatterbox/runtime/settings'),json('/chatterbox/readiness')]);
    $('#runtime-status').textContent=JSON.stringify({state:state.state,cuda:state.cuda,loaded:state.resident,
      pid:state.pid,loads:state.load_count,generations:state.generation_count,cache_hits:state.condition_cache_hits,
      idle_remaining_seconds:state.idle_remaining_seconds,model_revision:state.model_revision,
      source_revision:ready.source_revision,issues:ready.issues},null,2);
    $('#idle').value=settings.idle_timeout_seconds===null?'never':String(settings.idle_timeout_seconds);
  }
  $('#refresh-runtime').addEventListener('click',()=>refreshRuntime().catch(e=>notice(e.message,true)));
  $('#save-settings').addEventListener('click',async()=>{
    const raw=$('#idle').value,timeout=raw==='never'?null:Number(raw);
    try{await json('/chatterbox/runtime/settings',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({idle_timeout_seconds:timeout})});notice('Idle setting saved');await refreshRuntime();}catch(ex){notice(ex.message,true);}
  });
  $('#unload').addEventListener('click',async()=>{try{await json('/chatterbox/runtime/unload',{method:'POST'});notice('Worker unloaded');await refreshRuntime();}catch(e){notice(e.message,true);}});
  refreshVoices().catch(e=>notice(e.message,true));
})();
