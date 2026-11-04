'use strict';
const crypto = require('crypto');
const path = require('path');

// A 512 MiB file cannot be represented by one base64 string in V8. Pass the
// local File into the library's existing document upload pipeline instead.
async function prepare(client, filePath, MessageMedia, info) {
  const page=client.pupPage;
  if (!page) throw new Error('WhatsApp ещё не готов к загрузке файла');
  const key='queue_file_'+crypto.randomBytes(12).toString('hex');
  let input=null;
  async function dispose() {
    await page.evaluate(key=>{
      const state=window[key];
      if (state && window.WWebJS?.mediaDataToFile===state.wrapper) window.WWebJS.mediaDataToFile=state.original;
      document.getElementById(key)?.remove(); delete window[key];
    },key).catch(()=>{});
    if (input) await input.dispose().catch(()=>{});
  }
  try {
    await page.evaluate(key=>{
      if (typeof window.WWebJS?.mediaDataToFile !== 'function') throw new Error('Эта версия WhatsApp не поддерживает передачу больших файлов');
      const input=document.createElement('input');input.type='file';input.id=key;input.hidden=true;
      document.body.append(input);
    },key);
    input=await page.$('#'+key);
    if (!input) throw new Error('Не удалось подготовить файл');
    await input.uploadFile(path.resolve(filePath));
    await page.evaluate(({key,name,mime,size})=>{
      const source=document.getElementById(key)?.files?.[0];
      if (!source || source.size !== size) throw new Error('WhatsApp не смог прочитать файл целиком');
      const file=new File([source],name,{type:mime});
      const original=window.WWebJS.mediaDataToFile;
      const wrapper=function(media) {return media?.data===key ? file : original.apply(this,arguments);};
      window[key]={original,wrapper}; window.WWebJS.mediaDataToFile=wrapper;
    },{key,name:info.name,mime:info.mime,size:info.size});
    return {content:new MessageMedia(info.mime,key,info.name,info.size),dispose};
  } catch(error) {await dispose();throw error;}
}

module.exports={prepare,threshold:32*1024*1024};
