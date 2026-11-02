export class ApiError extends Error {
  constructor(status, code, retryAfter='') {
    super(code || 'REQUEST_FAILED'); this.status=status;this.code=code;this.retryAfter=retryAfter;
  }
}
export async function request(path,options={}) {
  const r=await fetch(path,{cache:'no-store',credentials:'same-origin',...options});
  if(!r.ok){
    let data=null;
    try {data=await r.json()}catch{}
    throw new ApiError(r.status,data?.error?.code || 'HTTP_'+r.status,r.headers.get('retry-after')||'');
  }
  if(options.raw) return r;
  return r.json();
}
export const json=(path,method,data)=>request(path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
export const profiles=()=>request('/profiles');
export const profile=id=>request('/profiles/'+encodeURIComponent(id));
export const clone=form=>request('/profiles',{method:'POST',body:form});
export const update=(id,payload)=>json('/profiles/'+encodeURIComponent(id),'PUT',payload);
export const replaceAudio=(id,form)=>request('/profiles/'+encodeURIComponent(id)+'/audio',{method:'PUT',body:form});
export const remove=(id,purge)=>request('/profiles/'+encodeURIComponent(id)+(purge?'?purge=true':''),{method:'DELETE'});
export const previewReference=id=>request('/profiles/'+encodeURIComponent(id)+'/reference',{raw:true});
export async function speech(voice,text,language,seed,signal,speed=1.0){
  return request('/v1/audio/speech',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({model:'omnivoice-gguf',voice,input:text,response_format:'wav',speed,
      ...(language?{language}:{}),...(seed!==null?{seed}:{})}),raw:true,signal});
}
export function attest(response){
  const h=response.headers;
  const backend=h.get('x-lite-verified-backend');
  const device=h.get('x-lite-observed-device')||'';
  const status=h.get('x-lite-verification-status');
  if(status!=='verified'||backend!=='Vulkan0'||!device.includes('NVIDIA')||!device.includes('GTX 1650')){
    throw new ApiError(500,'NATIVE_PROOF_MISSING');
  }
  return {backend,device,status,serverElapsed:h.get('x-lite-elapsed-s'),serverAudio:h.get('x-lite-audio-s'),serverRtf:h.get('x-lite-rtf'),requestedSpeed:h.get('x-lite-requested-speed')};
}
export function checkWav(bytes){
  if(bytes.byteLength<46) throw new ApiError(500,'INVALID_WAV');
  const v=new DataView(bytes),ascii=(start,n)=>Array.from({length:n},(_,i)=>String.fromCharCode(v.getUint8(start+i))).join('');
  if(ascii(0,4)!=='RIFF'||ascii(8,4)!=='WAVE') throw new ApiError(500,'INVALID_WAV');
  let pos=12,seen=false,seconds=0;
  while(pos+8<=v.byteLength){
    const kind=ascii(pos,4),len=v.getUint32(pos+4,true),begin=pos+8;
    if(begin+len>v.byteLength) throw new ApiError(500,'INVALID_WAV');
    if(kind==='fmt '){
      if(len<16||v.getUint16(begin,true)!==1||v.getUint16(begin+2,true)!==1||v.getUint32(begin+4,true)!==24000||v.getUint16(begin+14,true)!==16)throw new ApiError(500,'INVALID_WAV');
      seen=true;
    }
    if(kind==='data'){seconds=len/48000;break;}
    pos=begin+len+(len%2);
  }
  if(!seen||seconds<=0)throw new ApiError(500,'INVALID_WAV');
  return seconds;
}
