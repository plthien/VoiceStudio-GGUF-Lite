import * as api from './api.js';
const $=id=>document.getElementById(id);
const nav=[...document.querySelectorAll('.nav-button')];
const titles={clone:['Voice Cloning','Create a voice from an owned reference recording.'],
  library:['Voice Library','Manage your saved profiles and import legacy voices.'],
  tts:['Text to Speech','Generate a native verified WAV from any saved voice.'],
  runtime:['Runtime & Settings','Inspect pinned model and local inference readiness.']};
let voices=[],chosen='',editor='',controller=null,ttsUrl=null,refUrl=null,cloneUrl=null,diagnostic=null,diagnosticController=null,licenceFingerprint=null,working=false;
const errors={INVALID_REQUEST:'Check required fields and the exact transcript.',INVALID_AUDIO:'Reference could not be decoded. Use an owned WAV or MP3.',AUDIO_TOO_LARGE:'Recording exceeds the local size limit.',PROFILE_NOT_FOUND:'Voice no longer exists. Refresh the library.',PROFILE_ACTIVE:'Voice is currently in use. Try again after generation.',REFERENCE_INVALID:'Saved reference changed or failed integrity checks.',NATIVE_BUSY:'GPU is already processing a request.',LICENCE_NOT_READY:'Model provenance and explicit terms acceptance are required.',RUNTIME_NOT_READY:'Q8_0, native executable or Vulkan0 did not pass readiness.',NATIVE_TIMEOUT:'Native generation timed out.',CLIENT_DISCONNECTED:'Generation cancelled after browser disconnect.',NATIVE_FAILURE:'Native generation failed. No WAV was published.',NATIVE_PROOF_MISSING:'Device verification was missing or did not match NVIDIA Vulkan0.',INVALID_WAV:'The server response was not a valid 24 kHz PCM16 WAV.',MIGRATION_FAILED:'Legacy voice could not be migrated. No successful import was reported.',PURGE_INCOMPLETE:'Profile removed, but owned audio cleanup requires manual local review.',MIGRATION_SOURCE_UNAVAILABLE:'Close VoiceStudio and check %APPDATA%/OmniVoice/omnivoice.db, or configure the trusted legacy root on the server.'};
const safeError=e=>errors[e.code]||((e.name==='AbortError')?'Request cancelled.':'Local request failed. Check service status.');
function notice(message,bad=false){const n=$('notice');n.textContent=message;n.className='notice'+(bad?' error':'');n.hidden=false;}
function busy(button,state){button.disabled=state;button.setAttribute('aria-busy',String(state));}
function revoke(id){const a=$(id);if(a.src){URL.revokeObjectURL(a.src);a.removeAttribute('src');a.load();}a.hidden=true;}
function clearOutput(){if(ttsUrl){URL.revokeObjectURL(ttsUrl);ttsUrl=null;}$('tts-player').removeAttribute('src');$('tts-player').load();$('tts-output').hidden=true;}
function setVoice(id){chosen=voices.some(p=>p.id===id)?id:'';const v=voices.find(p=>p.id===chosen);$('selected-voice').textContent=v?v.name+' · '+v.id:'No voice selected';$('tts-voice').value=chosen;}
function openView(view){if(!titles[view])return;
  if(diagnosticController){diagnosticController.abort();diagnosticController=null;busy($('diagnostic-start'),false);$('diagnostic-export').disabled=true;if(diagnostic)diagnostic.status='cancelled';$('diagnostic-state').textContent='UI diagnostic cancelled on navigation.';}
  if(controller){controller.abort();controller=null;working=false;busy($('tts-submit'),false);busy($('tts-cancel'),true);$('tts-state').textContent='Request cancelled.';}
  if(view!=='tts')clearOutput();
  for(const n of nav){const active=n.dataset.view===view;n.classList.toggle('active',active);if(active)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');}
  for(const key of Object.keys(titles))$('view-'+key).hidden=key!==view;
  $('page-title').textContent=titles[view][0];$('page-subtitle').textContent=titles[view][1];
  if(view==='library')loadVoices();if(view==='runtime')refreshRuntime();
  window.location.hash=view;
}
nav.forEach(x=>x.addEventListener('click',()=>openView(x.dataset.view)));
document.querySelectorAll('[data-goto]').forEach(x=>x.addEventListener('click',()=>openView(x.dataset.goto)));
function renderVoices(){const list=$('voice-list');list.replaceChildren();$('library-empty').hidden=voices.length>0;
  for(const v of voices){const row=document.createElement('div');row.className='voice-item';
    const meta=document.createElement('div');const name=document.createElement('strong');name.textContent=v.name;
    const detail=document.createElement('span');detail.className='voice-meta';detail.textContent=v.id+' · '+v.language+' · version '+v.version+' · '+new Date(v.updated*1000).toLocaleString();
    meta.append(name,detail);const actions=document.createElement('div');actions.className='actions';
    const select=document.createElement('button');select.className='secondary';select.type='button';select.textContent='Use in TTS';select.addEventListener('click',()=>{setVoice(v.id);openView('tts');});
    const edit=document.createElement('button');edit.className='secondary';edit.type='button';edit.textContent='Edit';edit.addEventListener('click',()=>editVoice(v.id));
    actions.append(select,edit);row.append(meta,actions);list.append(row);
  }
  const selector=$('tts-voice');selector.replaceChildren(new Option('Select a saved voice',''));
  for(const v of voices)selector.add(new Option(v.name+' · '+v.id,v.id));
  if(!voices.some(v=>v.id===chosen))chosen=voices[0]?.id||'';
  setVoice(chosen);
}
async function loadVoices(){try{voices=await api.profiles();renderVoices();await cheapStatus();}catch(e){notice(safeError(e),true)}}
async function cheapStatus(){try{const v=await api.request('/lite/status');$('status-short').textContent=v.busy?'Native inference busy':'Local API connected';$('status-dot').className='status-dot'+(v.busy?'':' ok');}catch{$('status-short').textContent='Service unavailable';$('status-dot').className='status-dot bad';}}
$('refresh-voices').addEventListener('click',loadVoices);$('library-refresh').addEventListener('click',loadVoices);
$('tts-voice').addEventListener('change',e=>{chosen=e.target.value;setVoice(chosen);});
$('clone-file').addEventListener('change',e=>{if(cloneUrl)URL.revokeObjectURL(cloneUrl);const f=e.target.files[0];$('clone-preview').hidden=!f;if(f){cloneUrl=URL.createObjectURL(f);$('clone-preview').src=cloneUrl;}});
$('clone-form').addEventListener('submit',async e=>{e.preventDefault();const b=$('clone-submit');busy(b,true);$('clone-result').textContent='Normalizing local audio…';
  try{if(!$('clone-consent').checked)throw new Error('Consent required');const data=new FormData(e.target);data.set('consent','owner-confirmed-local');
    const result=await api.clone(data);await loadVoices();setVoice(result.id);
    $('clone-result').textContent='Created '+result.id+' · version '+result.version;
    notice('Voice profile saved locally. Select it in Text to Speech.');
    openView('tts');
  }catch(e){notice(safeError(e),true);$('clone-result').textContent='Not created';}finally{busy(b,false);}});
function editVoice(id){editor=id;const v=voices.find(p=>p.id===id);if(!v)return;
  const f=$('edit-form');f.elements.name.value=v.name;f.elements.language.value=v.language;f.elements.ref_text.value=v.ref_text;
  $('editor-id').textContent=v.id+' · version '+v.version;
  $('editor-consent').textContent=v.consent?'Current reference: owner acknowledged':'Current reference: UNCONFIRMED (no owner acknowledgement)';
  $('replace-consent').checked=false;$('editor-panel').hidden=false;revoke('saved-preview');
  $('editor-panel').scrollIntoView({behavior:'smooth',block:'start'});
}
$('edit-form').addEventListener('submit',async e=>{e.preventDefault();const b=$('edit-save');busy(b,true);
  try{const data={name:e.target.elements.name.value,language:e.target.elements.language.value,ref_text:e.target.elements.ref_text.value};
    await api.update(editor,data);await loadVoices();notice('Profile metadata saved.');}catch(err){notice(safeError(err),true)}finally{busy(b,false);}});
$('reference-play').addEventListener('click',async ()=>{try{const r=await api.previewReference(editor);const bytes=await r.arrayBuffer();api.checkWav(bytes);
  revoke('saved-preview');refUrl=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));
  $('saved-preview').src=refUrl;$('saved-preview').hidden=false;notice('Validated saved reference ready for playback.');
}catch(e){notice(safeError(e),true);}});
$('replace-form').addEventListener('submit',async e=>{e.preventDefault();const b=$('replace-submit');busy(b,true);
  try{if(!$('replace-consent').checked){notice('Confirm permission for the NEW recording.',true);return;}
    const form=new FormData(e.target);form.set('consent','owner-confirmed-local');
    const result=await api.replaceAudio(editor,form);await loadVoices();editVoice(result.id);notice('Reference updated to version '+result.version+' (stable voice ID unchanged).');e.target.reset();
  }catch(err){notice(safeError(err),true)}finally{$('replace-consent').checked=false;busy(b,false);}});
async function deleteVoice(purge){if(!editor)return;const id=editor;const words=purge?'delete this Lite profile AND its owned reference files':'remove this profile record while RETAINING its reference files';
  if(!window.confirm('Do you want to '+words+'? This never removes original VoiceStudio data.'))return;
  try{const result=await api.remove(id,purge);$('editor-panel').hidden=true;editor='';revoke('saved-preview');await loadVoices();
    notice(result.owned_reference_files_purged?'Lite profile and owned reference files deleted.':'Profile record removed. Reference files remain on disk.');
  }catch(e){notice(safeError(e),true)}}
$('delete-purge').addEventListener('click',()=>deleteVoice(true));$('delete-only').addEventListener('click',()=>deleteVoice(false));
$('tts-cancel').addEventListener('click',()=>{if(controller)controller.abort();});
async function performSpeech(voice,text,language,seed,signal){const now=performance.now();const response=await api.speech(voice,text,language,seed,signal);
  const proof=api.attest(response);if(response.headers.get('content-type')?.split(';')[0]!=='audio/wav')throw new api.ApiError(500,'INVALID_WAV');
  const bytes=await response.arrayBuffer();const seconds=api.checkWav(bytes);
  return {blob:new Blob([bytes],{type:'audio/wav'}),proof,seconds,clientSeconds:(performance.now()-now)/1000};}
$('tts-form').addEventListener('submit',async e=>{e.preventDefault();if(working)return;const voice=$('tts-voice').value,text=$('tts-text').value;
  if(!voice||!text.trim()){notice('Select a saved voice and enter text.',true);return;}
  const value=$('tts-seed').value,seed=value===''?null:Number(value);if(seed!==null&&(!Number.isInteger(seed)||seed<0||seed>2147483647)){notice('Seed must be a nonnegative integer.',true);return;}
  clearOutput();controller=new AbortController();const current=controller;working=true;busy($('tts-submit'),true);busy($('tts-cancel'),false);$('tts-state').textContent='Generating…';$('tts-native-summary').textContent='Waiting for native attestation…';
  try{const result=await performSpeech(voice,text,$('tts-language').value,seed,current.signal);
    if(current.signal.aborted)return;
    ttsUrl=URL.createObjectURL(result.blob);$('tts-player').src=ttsUrl;$('tts-download').href=ttsUrl;$('tts-download').download='lite-'+voice+'.wav';
    $('tts-proof').textContent='Verified · '+result.proof.device+' · Vulkan0';
    $('tts-native-summary').textContent='Verified from native response headers: '+result.proof.device+' / '+result.proof.backend;
    $('tts-timing').textContent='Audio '+result.seconds.toFixed(2)+' s · Client '+result.clientSeconds.toFixed(2)+' s'+(result.proof.serverRtf?' · Server RTF '+result.proof.serverRtf:'');
    $('tts-output').hidden=false;$('tts-state').textContent='Completed';notice('Verified 24 kHz WAV is ready.');
  }catch(err){$('tts-state').textContent=err.name==='AbortError'?'Cancelled':'Failed';notice(safeError(err)+(err.retryAfter?' Retry after '+err.retryAfter+'s.':''),true);
  }finally{if(controller===current){controller=null;working=false;busy($('tts-submit'),false);busy($('tts-cancel'),true);}}});
function appendDetail(root,key,val){const d=document.createElement('div');d.className='detail';const label=document.createElement('span');label.textContent=key;
  const value=document.createElement('strong');value.textContent=val==null?'Not verified':String(val);d.append(label,value);root.append(d);}
function metric(name,value){const d=document.createElement('div');d.className='metric';const a=document.createElement('span');a.textContent=name;const b=document.createElement('strong');b.textContent=String(value);d.append(a,b);return d;}
async function refreshRuntime(){const b=$('runtime-refresh');busy(b,true);$('runtime-checked').textContent='Checking local artifacts. This may read the full Q8_0 weights…';
  try{const [ready,quant,details,acceptance]=await Promise.all([
    api.request('/lite/readiness'),api.request('/engines/omnivoice-gguf/quants'),
    api.request('/models/licenses/details/Serveurperso/OmniVoice-GGUF'),
    api.request('/models/licenses/acceptance/Serveurperso/OmniVoice-GGUF')]);
    $('runtime-checked').textContent='Last checked: '+new Date().toLocaleString()+' · Manual refresh only';
    const metrics=$('runtime-summary');metrics.replaceChildren(metric('Readiness',ready.ready?'Ready':'Not ready'),metric('GPU',ready.gpu?.name||'Not verified'),metric('Quantization','Q8_0'),metric('Native',ready.binary?'Files verified':'Not ready'));
    const output=$('runtime-details');output.replaceChildren();
    [['Backend requested','Vulkan0'],['GPU0 vendor',ready.gpu?.vendor_id],['VRAM',ready.gpu?.vram_mib?ready.gpu.vram_mib+' MiB':null],['FFmpeg',ready.ffmpeg_installed===true?'Installed':ready.ffmpeg_installed===false?'Missing — install FFmpeg':'Not verified'],['Base',quant.base],['Tokenizer',quant.tokenizer],['Model revision',quant.revision],['Native revision',quant.runtime_revision],['Busy',ready.busy?'Yes':'No'],['Verified pair',quant.verified?'Yes':'No'],['Actual in use',quant.in_use||'Not running'],['Issues',ready.issues?.join('; ')||'None reported']].forEach(([key,value])=>appendDetail(output,key,value));
    const rights=$('licence-details');rights.replaceChildren();
    const model=details.info||{},reviewed=details.reviewed_terms||{};
    [['Model ID',reviewed.model||acceptance.repo_id],['Pinned revision',reviewed.model_revision||acceptance.model_revision],
     ['Historical evidence revision',reviewed.legacy_evidence_revision||acceptance.evidence_revision],
     ['Revision match',(reviewed.model_revision===reviewed.legacy_evidence_revision)?'Matching':'MISMATCH — review evidence'],
     ['Local acknowledgement',acceptance.accepted?'Accepted locally':acceptance.state],
     ['Licence fingerprint',acceptance.fingerprint],['Provenance receipt',acceptance.provenance_reconciled?'Reconciled by documented receipt':'NOT VERIFIED'],
     ['Commercial clearance','RESTRICTED / NOT CLEARED']].forEach(([key,value])=>appendDetail(rights,key,value));
    const source=$('licence-notice');source.replaceChildren();
    [['Model licence / components',model.license],['Reported use',model.review_status],
     ['Component closure',model.component_closure],['Commercial inference',model.commercial_inference],
     ['Commercial output rights',model.commercial_outputs],['Attribution',model.credit],
     ['Source documentation',model.notes],['Local human review',reviewed.reviewed_provenance?.reviewed_by||'No valid reviewed provenance receipt']].forEach(([key,value])=>appendDetail(source,key,value));
    for(const [label,raw] of [['Model source',model.source_url],['Evidence at historical revision',model.evidence_url],
      ['Pinned revision review',reviewed.reviewed_provenance?.evidence_url]]){
      if(typeof raw!=='string')continue;
      try{const url=new URL(raw);if(url.protocol!=='https:'||url.hostname!=='huggingface.co')continue;
        const a=document.createElement('a');a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';a.textContent=label+' · Open external evidence';
        source.append(a);
      }catch{}
    }
    licenceFingerprint=acceptance.fingerprint;$('licence-review').checked=false;
    $('licence-accept').disabled=acceptance.accepted;$('licence-withdraw').disabled=!acceptance.accepted;
    $('status-short').textContent=ready.ready?'Q8_0 runtime ready':'Runtime not ready';$('status-dot').className='status-dot'+(ready.ready?' ok':' bad');
  }catch(err){$('runtime-checked').textContent='Readiness failed';notice(safeError(err),true);}finally{busy(b,false);}}
$('runtime-refresh').addEventListener('click',refreshRuntime);
$('licence-form').addEventListener('submit',async e=>{e.preventDefault();
  if(!licenceFingerprint||!$('licence-review').checked){notice('Review the exact model terms before acceptance.',true);return;}
  const b=$('licence-accept');busy(b,true);try{await api.json('/models/licenses/accept','POST',{repo_id:'Serveurperso/OmniVoice-GGUF',fingerprint:licenceFingerprint,accepted:true});
  notice('Local terms acknowledgement recorded; this is not commercial permission.');await refreshRuntime();}catch(err){notice(safeError(err),true)}finally{busy(b,false);}});
$('licence-withdraw').addEventListener('click',async()=>{if(!licenceFingerprint)return;
  try{await api.json('/models/licenses/accept','POST',{repo_id:'Serveurperso/OmniVoice-GGUF',fingerprint:licenceFingerprint,accepted:false});notice('Local terms acknowledgement withdrawn.');await refreshRuntime();}catch(e){notice(safeError(e),true);}});
$('migration-preview').addEventListener('click',async()=>{const b=$('migration-preview');busy(b,true);$('migration-feedback').textContent='Inspecting the configured legacy source…';
  try{const result=await api.request('/lite/migration/preview');const choice=$('migration-choice');choice.replaceChildren(new Option('Choose a voice',''));const list=$('migration-list');list.replaceChildren();
    for(const item of result.eligible){choice.add(new Option(item.name+' · '+item.id,item.id));const div=document.createElement('div');div.className='voice-item';div.textContent=item.name+' · '+item.id+' · '+item.language+' · eligible';list.append(div);}
    for(const item of result.invalid){const div=document.createElement('div');div.className='voice-item';div.textContent=(item.id||'unknown')+' · invalid: '+item.reason;list.append(div);}
    $('migration-feedback').textContent=result.eligible.length+' eligible · '+result.invalid.length+' invalid. No import performed.';$('migration-apply').disabled=true;
  }catch(err){$('migration-feedback').textContent=safeError(err);notice(safeError(err),true);}finally{busy(b,false);}});
$('migration-choice').addEventListener('change',()=>{$('migration-apply').disabled=!$('migration-choice').value;});
$('migration-apply').addEventListener('click',async()=>{const id=$('migration-choice').value;if(!id)return;
  if(!confirm('Import only voice '+id+' from the server-configured read-only legacy source?'))return;
  const b=$('migration-apply');busy(b,true);
  try{const result=await api.json('/lite/migration/apply','POST',{voice_id:id});
    $('migration-feedback').textContent=result.migrated?'Imported profile '+id:result.skipped?'Already imported: '+id:'No profile imported';
    await loadVoices();if(result.migrated||result.skipped)setVoice(id);
  }catch(err){$('migration-feedback').textContent=safeError(err);notice(safeError(err),true);}finally{busy(b,false);}});
const DIAGNOSTIC_TEXT='You lift the lid, toss in a banana peel, and forget about it. But when Paris tried to get people to use covered garbage bins in the eighteen eighties, people got angry. Landlords complained. Newspapers made jokes. And workers who earned their living from discarded stuff worried they might lose their jobs.';
function diagnosticRow(row){
  const tr=document.createElement('tr');
  for(const val of [row.run,row.audio_s==null?'—':row.audio_s.toFixed(3),
    row.client_elapsed_s==null?'—':row.client_elapsed_s.toFixed(3),
    row.client_rtf==null?'—':row.client_rtf.toFixed(3),
    row.native_device||'Not verified',row.status||'Incomplete']){
    const td=document.createElement('td');td.textContent=String(val);tr.append(td);
  }
  $('diagnostic-rows').append(tr);
}
$('diagnostic-start').addEventListener('click',async()=>{
  if(diagnosticController)return;
  if(!chosen){notice('Choose a saved voice before running a diagnostic.',true);return;}
  const button=$('diagnostic-start'), current=new AbortController();
  diagnosticController=current;busy(button,true);$('diagnostic-export').disabled=true;
  $('diagnostic-rows').replaceChildren();
  diagnostic={type:'UI diagnostic — NOT authoritative CLI benchmark',voice_id:chosen,seed:42,
    character_count:DIAGNOSTIC_TEXT.length,comparable:false,runs:[],status:'running'};
  try{
    if(DIAGNOSTIC_TEXT.length!==306)throw new Error('Frozen diagnostic text mismatch');
    for(let i=1;i<=3;i++){
      if(current.signal.aborted)throw new DOMException('Cancelled','AbortError');
      $('diagnostic-state').textContent='Running '+i+' / 3…';
      try{
        const result=await performSpeech(diagnostic.voice_id,DIAGNOSTIC_TEXT,'en',42,current.signal);
        if(current.signal.aborted)throw new DOMException('Cancelled','AbortError');
        const row={run:i,audio_s:result.seconds,client_elapsed_s:result.clientSeconds,
          client_rtf:result.clientSeconds/result.seconds,native_device:result.proof.device,
          native_backend:result.proof.backend,verified:true,status:'Verified'};
        diagnostic.runs.push(row);diagnosticRow(row);
      }catch(error){
        if(current.signal.aborted)throw error;
        diagnosticRow({run:i,status:'Failed — '+(error.code||error.name||'Unknown')});
        throw error;
      }
    }
    if(current.signal.aborted)throw new DOMException('Cancelled','AbortError');
    diagnostic.status='complete';
    diagnostic.mean_client_rtf=diagnostic.runs.reduce((sum,row)=>sum+row.client_rtf,0)/3;
    const sorted=diagnostic.runs.map(x=>x.client_rtf).sort((a,b)=>a-b);
    diagnostic.median_client_rtf=sorted[1];
    $('diagnostic-state').textContent='3/3 verified. Mean client RTF '+diagnostic.mean_client_rtf.toFixed(3)+'; median '+diagnostic.median_client_rtf.toFixed(3)+'; NOT CLI-comparable.';
    $('diagnostic-export').disabled=false;
  }catch(error){
    diagnostic.status=current.signal.aborted?'cancelled':'failed';
    $('diagnostic-export').disabled=true;
    if(!current.signal.aborted)notice(safeError(error),true);
    $('diagnostic-state').textContent=current.signal.aborted?'Diagnostic cancelled — incomplete':('Diagnostic failed after '+diagnostic.runs.length+'/3');
  }finally{if(diagnosticController===current){diagnosticController=null;busy(button,false);}}
});
$('diagnostic-export').addEventListener('click',()=>{if(!diagnostic||diagnostic.status!=='complete')return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(diagnostic,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='lite-ui-diagnostic.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
window.addEventListener('beforeunload',()=>{if(controller)controller.abort();if(diagnosticController)diagnosticController.abort();if(ttsUrl)URL.revokeObjectURL(ttsUrl);if(refUrl)URL.revokeObjectURL(refUrl);if(cloneUrl)URL.revokeObjectURL(cloneUrl);});
loadVoices();cheapStatus();openView(['clone','library','tts','runtime'].includes(location.hash.slice(1))?location.hash.slice(1):'clone');
