'use strict';
const https = require('https');
const MAX = 1024 * 1024;
function trusted(source) {
  const u = new URL(source);
  if (u.protocol !== 'https:' || u.username || u.password || (u.port && u.port !== '443') ||
      !['whatsapp.net','whatsapp.com','fbcdn.net','facebook.com'].some(h => u.hostname === h || u.hostname.endsWith('.'+h))) throw new Error('Untrusted avatar URL');
  return u;
}
function image(data) {
  return Buffer.isBuffer(data) && data.length <= MAX && (
    data.subarray(0,3).equals(Buffer.from([255,216,255])) ||
    data.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ||
    (data.toString('ascii',0,4)==='RIFF' && data.toString('ascii',8,12)==='WEBP'));
}
function download(source, redirects=0) {
  return new Promise((resolve,reject)=>{
    let u;try {u=trusted(source);}catch(e){reject(e);return;}
    const req=https.get(u,{headers:{'User-Agent':'Mozilla/5.0','Accept':'image/*'}},res=>{
      if ([301,302,303,307,308].includes(res.statusCode) && redirects<3 && res.headers.location) {
        res.resume();download(new URL(res.headers.location,u).href,redirects+1).then(resolve,reject);return;
      }
      if(res.statusCode!==200){res.resume();reject(new Error('CDN HTTP '+res.statusCode));return;}
      let size=0;const parts=[];
      res.on('data',chunk=>{size+=chunk.length;if(size>MAX){req.destroy(new Error('Avatar exceeds 1 MiB'));return;}parts.push(chunk);});
      res.on('error',reject);res.on('aborted',()=>reject(new Error('CDN response aborted')));
      res.on('end',()=>{const data=Buffer.concat(parts);image(data)?resolve(data):reject(new Error('CDN returned non-image'));});
    });
    // Total timeout includes DNS and connection, unlike socket inactivity alone.
    const timer=setTimeout(()=>req.destroy(new Error('CDN timeout')),8000);
    req.on('close',()=>clearTimeout(timer));req.on('error',reject);
  });
}
async function downloadAuthenticated(client, source) {
  const blob=String(source).startsWith('blob:https://web.whatsapp.com/');
  if(!blob) trusted(source);
  const encoded=await client.pupPage.evaluate(async(url)=>{
    let last;
    // Public CDNs often disallow credentials even in an authenticated page.
    for(const credentials of ['omit','include']) {
      const abort=new AbortController();const timer=setTimeout(()=>abort.abort(),8000);
      try {
        const response=await fetch(url,{credentials,signal:abort.signal,redirect:'error'});
        if(!response.ok)throw new Error('HTTP '+response.status);
        const reader=response.body.getReader();const parts=[];let size=0;
        while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>1048576){await reader.cancel();throw new Error('Avatar too large');}parts.push(value);}
        let binary='';for(const part of parts)for(let i=0;i<part.length;i+=8192)binary+=String.fromCharCode(...part.subarray(i,i+8192));
        return btoa(binary);
      }catch(e){last=String(e.message||e);}finally{clearTimeout(timer);}
    }
    throw new Error(last||'Avatar fetch failed');
  },source);
  const data=Buffer.from(encoded||'','base64');if(!image(data))throw new Error('Browser returned non-image');return data;
}
const idText=v=>typeof v==='string'?v:String(v?._serialized||'');
async function candidates(client,id) {
  const result=[id];
  if(!id.endsWith('@g.us') && typeof client.getContactLidAndPhone==='function') {
    try {for(const row of await client.getContactLidAndPhone([id])||[]){const pair=[idText(row.lid),idText(row.pn)];if(!pair.includes(id))continue;for(const key of pair)if(key && !result.includes(key))result.push(key);}}catch(_){}
  }
  return result;
}
// getProfilePicUrl and Contact.getProfilePicUrl use the same library path.
// Instead of calling it twice, read the actual WhatsApp model/thumbnail.
async function cachedPictures(keys) {
  const urls=[];const errors=[];
  const add=value=>{if(typeof value==='string' && /^(https:|blob:https:\/\/web\.whatsapp\.com\/)/.test(value) && !urls.includes(value))urls.push(value);};
  const thumb=value=>{if(!value)return;for(const k of ['eurl','imgFull','img','url'])add(value[k]);};
  try {
    const c=window.require('WAWebCollections');
    for(const key of keys){
      const chat=c.Chat?.get(key);const contact=c.Contact?.get(key);
      thumb(chat?.profilePicThumb);thumb(contact?.profilePicThumb);thumb(contact?.profilePicThumbObj);
      // Unlike the public wrapper, pass a Chat model, not a serialized object.
      if(!urls.length && chat)try{thumb(await window.require('WAWebContactProfilePicThumbBridge').requestProfilePicFromServer(chat));}catch(e){errors.push(String(e.message||e));}
    }
  }catch(e){errors.push(String(e.message||e));}
  return {urls,errors};
}
async function pictureUrls(client,keys) {
  const urls=[];const errors=[];
  // Read cached thumbnails first, including blob URLs of already loaded pictures.
  if(client.pupPage?.evaluate)try{const r=await client.pupPage.evaluate(cachedPictures,keys);urls.push(...(r.urls||[]));errors.push(...(r.errors||[]));}catch(e){errors.push(String(e.message||e));}
  for(const key of keys) {
    try {const value=await client.getProfilePicUrl(key);if(value){urls.unshift(String(value));break;}}catch(e){errors.push(String(e.message||e));}
  }
  if(!urls.length)throw new Error('URL unavailable (privacy/no photo/WhatsApp API). '+errors.join('; ').slice(0,160));
  return [...new Set(urls)];
}
function createWorker(deps={}) {
  const waiting=new Set();const states=new Map();let busy=false;
  const now=deps.now||Date.now;const log=deps.log||console;
  const valid=id=>/^[\w.:-]+@(c\.us|g\.us|lid)$/.test(id);
  function enqueue(ids){for(const value of ids||[]){const id=String(value||'');if(valid(id) && waiting.size<1800 && now()>=(states.get(id)?.next||0))waiting.add(id);}}
  async function sync(client,post,priority=[]) {
    enqueue(priority);if(busy)return;busy=true;
    try {
      const ordered=[...new Set([...priority.filter(id=>waiting.has(id)),...waiting])].filter(id=>now()>=(states.get(id)?.next||0)).slice(0,4);
      for(const id of ordered) {
        let stage='lookup';
        try {
          const keys=await (deps.candidates||candidates)(client,id);
          const urls=await (deps.pictureUrls||pictureUrls)(client,keys);
          stage='download';let data;let last;
          for(const url of urls) {
            try{data=await(deps.download||download)(url);if(!image(data))throw new Error('Invalid image');break;}
            catch(e){last=e;try{data=await(deps.downloadAuthenticated||downloadAuthenticated)(client,url);if(!image(data))throw new Error('Invalid image');break;}catch(e2){last=e2;}}
          }
          if(!data || !image(data))throw last||new Error('No image bytes');
          stage='save';
          // Save against every known alias. The UI may use PN while sender uses LID.
          for(const key of new Set([id,...keys])) {
            if(!valid(key))continue;
            const result=await post('/api/avatar-sync',{chat_id:key,image_base64:data.toString('base64')});
            if(result?.updated!==true)throw new Error('Server did not confirm cache write');
          }
          states.set(id,{next:now()+6*3600000,failures:0});waiting.delete(id);
          log.info('Аватар: сохранён '+id);
        }catch(e){
          const failures=(states.get(id)?.failures||0)+1;
          const delay=Math.min(300000,20000*2**Math.min(failures-1,4));
          states.set(id,{next:now()+delay,failures});
          // No signed URL or cookies in diagnostics.
          const reason=String(e.message||e).replace(/https?:\/\/\S+/g,'[URL]').slice(0,200);
          if(failures===1 || failures%5===0)log.warn(`Аватар: ${stage}, ${id}: ${reason}. Повтор через ${delay/1000} сек.`);
        }
      }
      if(states.size>7000)for(const [id,s]of states)if(s.next<now()&&!waiting.has(id))states.delete(id);
    }finally{busy=false;}
  }
  return {enqueue,sync};
}
const worker=createWorker();
module.exports={...worker,createWorker,cachedPictures,candidates,pictureUrls,download,downloadAuthenticated,trusted,image};
