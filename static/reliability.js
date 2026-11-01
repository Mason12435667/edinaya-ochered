(() => {
  "use strict";
  const BUILD="3.3.99";
  const qs=(s,r=document)=>r.querySelector(s), qsa=(s,r=document)=>[...r.querySelectorAll(s)];
  const csrf=()=>qs('meta[name="queue-csrf"]')?.content||"";
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const chatId=()=>String(qs('[data-chat-id]')?.value||'').trim();
  const post=async(path,values={})=>{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'fetch'},body:new URLSearchParams({...values,csrf_token:csrf()}),cache:'no-store'});let d={};try{d=await r.json();}catch(_){}if(!r.ok)throw new Error(d.error||'Не удалось выполнить действие');return d;};
  const get=async url=>{const r=await fetch(url,{cache:'no-store'});let d={};try{d=await r.json();}catch(_){}if(!r.ok)throw new Error(d.error||'Не удалось загрузить данные');return d;};
  const toast=(text,kind='info')=>{if(window.QueueUI?.toast)window.QueueUI.toast(String(text));else if(window.QueueAppNotice)window.QueueAppNotice(String(text),kind);else console.log(text);};


  let uploadPolicy={max_bytes:12*1024*1024,blocked_extensions:[]};
  get('/api/reliability-upload-policy').then(x=>uploadPolicy=x).catch(()=>{});
  const validateLocalFile=file=>{if(!file)return '';const name=String(file.name||'').toLowerCase();const dot=name.lastIndexOf('.');const ext=dot>=0?name.slice(dot):'';if((uploadPolicy.blocked_extensions||[]).includes(ext))return `Файл ${ext} заблокирован: потенциально опасное расширение`;if(Number(file.size||0)>Number(uploadPolicy.max_bytes||12582912))return `Файл превышает лимит ${Math.round(Number(uploadPolicy.max_bytes||0)/1048576)} МБ`;return '';};
  document.addEventListener('change',e=>{const input=e.target.closest?.('[data-media-input]');if(!input)return;const err=validateLocalFile(input.files?.[0]);if(err){e.preventDefault();e.stopImmediatePropagation();input.value='';toast(err,'error');}},true);
  document.addEventListener('drop',e=>{const shell=e.target.closest?.('[data-conversation-page]');if(!shell)return;const files=[...(e.dataTransfer?.files||[])];const err=files.map(validateLocalFile).find(Boolean);if(err){e.preventDefault();e.stopImmediatePropagation();toast(err,'error');}},true);

  let overlay=null;
  function closeModal(){overlay?.remove();overlay=null;}
  function modal(title,html,wide=false){closeModal();overlay=document.createElement('div');overlay.className='reliability-overlay';overlay.innerHTML=`<section class="reliability-modal${wide?' wide':''}"><header><div><small>Единая очередь</small><h2>${esc(title)}</h2></div><button type="button" data-close>×</button></header><div class="reliability-modal-body">${html}</div></section>`;document.body.append(overlay);qs('[data-close]',overlay).onclick=closeModal;overlay.addEventListener('pointerdown',e=>{if(e.target===overlay)closeModal();});return qs('.reliability-modal-body',overlay);}

  function installChatTools(){
    const header=qs('.chat-header-actions'); const id=chatId(); if(!header||!id)return;
    if(!qs('[data-reliability-delay]',header)){
      const b=document.createElement('button');b.type='button';b.className='button compact ghost';b.dataset.reliabilityDelay='1';b.textContent='⏱ Отложить';b.onclick=openSchedule;header.prepend(b);
    }
    if(!id.endsWith('@g.us')&&!qs('[data-reliability-profile-refresh]',header)){
      const b=document.createElement('button');b.type='button';b.className='button compact ghost';b.dataset.reliabilityProfileRefresh='1';b.textContent='↻ Имя / аватар';b.onclick=async()=>{try{await post('/api/reliability-profile-refresh',{chat_id:chatId()});toast('Обновление профиля запрошено');setTimeout(()=>qs('[data-chat-refresh]')?.click(),1400);}catch(e){toast(e.message,'error');}};header.append(b);
    }
  }

  async function openSchedule(){
    const id=chatId(); const text=String(qs('[data-chat-composer] textarea[name="message"]')?.value||'').trim();
    if(!id)return toast('Выберите чат');
    let body=modal('Отложенная отправка',`<div class="reliability-schedule"><label>Текст сообщения<textarea data-schedule-text rows="5" maxlength="32000">${esc(text)}</textarea></label><div class="reliability-delay-buttons"><button class="button" data-delay="5">Через 5 секунд</button><button class="button" data-delay="300">Через 5 минут</button><button class="button" data-delay="1800">Через 30 минут</button><button class="button" data-delay="3600">Через 1 час</button></div><label>Или точное время<input type="datetime-local" data-schedule-at></label><button type="button" class="button primary" data-schedule-save>Запланировать</button><hr><div data-scheduled-list><span class="muted">Загрузка запланированных...</span></div></div>`,true);
    let delay=300; qsa('[data-delay]',body).forEach(b=>b.onclick=()=>{delay=Number(b.dataset.delay||300);qsa('[data-delay]',body).forEach(x=>x.classList.toggle('primary',x===b));});
    const reload=async()=>{try{const d=await get(`/api/reliability-scheduled?chat_id=${encodeURIComponent(id)}`);const box=qs('[data-scheduled-list]',body);box.innerHTML=(d.items||[]).map(x=>`<article class="scheduled-item"><div><strong>${esc(String(x.body||'').slice(0,90))}</strong><small>${esc(String(x.due_at||'').replace('T',' ').slice(0,19))}</small></div><button class="button compact danger" data-cancel-schedule="${Number(x.id)}">Отменить</button></article>`).join('')||'<span class="muted">Запланированных сообщений нет</span>';qsa('[data-cancel-schedule]',box).forEach(b=>b.onclick=async()=>{try{await post('/api/reliability-scheduled-cancel',{id:b.dataset.cancelSchedule});toast('Отложенная отправка отменена');reload();}catch(e){toast(e.message,'error');}});}catch(e){toast(e.message,'error');}};
    qs('[data-schedule-save]',body).onclick=async()=>{const msg=String(qs('[data-schedule-text]',body).value||'').trim();const exact=qs('[data-schedule-at]',body).value;const due=exact?new Date(exact):new Date(Date.now()+delay*1000);if(!msg)return toast('Введите текст сообщения','error');try{await post('/api/reliability-schedule',{chat_id:id,body:msg,due_at:due.toISOString()});toast(delay===5&&!exact?'Сообщение задержано на 5 секунд. Его ещё можно отменить.':'Отложенная отправка сохранена');const ta=qs('[data-chat-composer] textarea[name="message"]');if(ta&&ta.value.trim()===msg)ta.value='';reload();}catch(e){toast(e.message,'error');}};reload();
  }

  function installMessageInfo(){
    qsa('[data-message-id]').forEach(row=>{
      const actions=qs('.chat-message-actions',row);if(!actions||qs('[data-reliability-message-info]',actions))return;
      const b=document.createElement('button');b.type='button';b.dataset.reliabilityMessageInfo='1';b.textContent='Инфо';b.title='Техническая информация сообщения';
      b.onclick=async()=>{try{const d=await get(`/api/reliability-message-info?chat_id=${encodeURIComponent(chatId())}&message_id=${encodeURIComponent(row.dataset.messageId||'')}`);const m=d.message||{};modal('Техническая информация',`<dl class="message-tech"><dt>ID WhatsApp</dt><dd>${esc(m.message_key||'')}</dd><dt>Источник</dt><dd>${esc(m.source||'')}</dd><dt>Тип</dt><dd>${esc(m.message_type||'')}</dd><dt>Получено</dt><dd>${esc(String(m.received_at||'').replace('T',' ').slice(0,25))}</dd><dt>ACK</dt><dd>${esc(m.ack??'')}</dd><dt>Медиа</dt><dd>${esc(m.media_name||m.media_mime||'Нет')}</dd><dt>Связанная заявка</dt><dd>${esc(m.ticket_id||'Нет')}</dd></dl>`,true);}catch(e){toast(e.message,'error');}};
      actions.append(b);
    });
  }

  function maintenanceGuard(){
    const banner=qs('.maintenance-banner');if(!banner||document.body.dataset.admin==='1')return;
    qsa('[data-chat-composer] textarea,[data-chat-composer] button,.filters button').forEach(x=>{if(x.closest('.theme-toggle'))return;x.title='Режим обслуживания';});
  }

  function tick(){installChatTools();installMessageInfo();maintenanceGuard();}
  // EO_PERFORMANCE_20260930: refresh after accepted chat state, with a slow fallback.
  let reliabilityRefreshTimer=0;
  const scheduleTick=()=>{if(document.hidden)return;clearTimeout(reliabilityRefreshTimer);reliabilityRefreshTimer=setTimeout(tick,250);};
  document.addEventListener('DOMContentLoaded',tick);
  window.addEventListener('queue-chat-state-accepted',scheduleTick);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick();});
  setInterval(()=>{if(!document.hidden)tick();},10000);
  document.documentElement.dataset.reliabilityBuild=BUILD;
})();
