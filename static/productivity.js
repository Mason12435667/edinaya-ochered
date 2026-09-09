(() => {
  "use strict";
  const BUILD = "3.3.90";
  const csrf = () => document.querySelector('meta[name="queue-csrf"]')?.content || "";
  const qs = (s, root=document) => root.querySelector(s);
  const qsa = (s, root=document) => [...root.querySelectorAll(s)];
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

  async function post(path, values={}) {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"fetch"},
      body: new URLSearchParams({...values, csrf_token: csrf()}),
      cache: "no-store",
    });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(data.error || "Не удалось сохранить изменение");
    return data;
  }
  function toast(text) {
    if (window.QueueAppNotice) return window.QueueAppNotice(String(text || ""), "info");
    if (window.QueueUI?.toast) return window.QueueUI.toast(text);
    const note=document.createElement("div"); note.className="queue-inline-notice info"; note.textContent=String(text || ""); document.body.append(note); setTimeout(()=>note.remove(),5000);
  }
  function selectedChatId() { return String(qs("[data-chat-id]")?.value || "").trim(); }
  function isGroupChat(id=selectedChatId()) { return id.endsWith("@g.us"); }

  let overlay = null;
  function closeModal() { if (overlay) { overlay.remove(); overlay=null; } }
  function modal(title, body, options={}) {
    closeModal();
    overlay=document.createElement("div"); overlay.className="productivity-overlay";
    const card=document.createElement("section"); card.className=`productivity-modal ${options.wide ? "wide" : ""}`;
    card.innerHTML=`<header><div><small>${escapeHtml(options.eyebrow || "Единая очередь")}</small><h2>${escapeHtml(title)}</h2></div><button type="button" class="productivity-close" aria-label="Закрыть">×</button></header><div class="productivity-modal-body"></div>`;
    card.querySelector(".productivity-modal-body").append(body);
    overlay.append(card); document.body.append(overlay);
    card.querySelector(".productivity-close").onclick=closeModal;
    overlay.addEventListener("pointerdown", e => { if (e.target===overlay) closeModal(); });
    return {overlay, card, body:card.querySelector(".productivity-modal-body")};
  }
  document.addEventListener("keydown", e => { if (e.key==="Escape") closeModal(); });

  // Styled confirmation for database restore; avoids the native browser confirm dialog.
  document.addEventListener("submit", (event) => {
    const form=event.target.closest?.("[data-backup-restore-form]");
    if(!form || form.dataset.confirmed==="1") return;
    event.preventDefault();
    const name=form.querySelector('input[name="name"]')?.value || "эту копию";
    const body=document.createElement("div"); body.className="backup-confirm-body";
    body.innerHTML=`<div class="backup-confirm-icon">↶</div><p>Восстановить <strong>${escapeHtml(name)}</strong>?</p><small>Перед восстановлением система автоматически создаст страховочную копию текущей базы.</small>`;
    const actions=document.createElement("div"); actions.className="productivity-modal-actions";
    const cancel=button("Отмена","button compact ghost");
    const confirm=button("Восстановить базу","button compact primary danger-soft");
    actions.append(cancel,confirm); body.append(actions);
    modal("Подтверждение восстановления",body,{eyebrow:"Резервные копии"});
    cancel.onclick=closeModal;
    confirm.onclick=()=>{confirm.disabled=true;form.dataset.confirmed="1";closeModal();form.requestSubmit();};
  }, true);

  function formField(label, input) {
    const wrap=document.createElement("label"); wrap.className="productivity-field";
    const span=document.createElement("span"); span.textContent=label; wrap.append(span,input); return wrap;
  }
  function button(label, cls="button compact") { const b=document.createElement("button"); b.type="button"; b.className=cls; b.textContent=label; return b; }

  // ----- Contact tags / notes -----
  let lastProductivityChat="";
  async function loadContactState(chatId) {
    if (!chatId) return null;
    try {
      const r=await fetch(`/api/productivity-state?chat_id=${encodeURIComponent(chatId)}`,{cache:"no-store"});
      if (!r.ok) return null; return await r.json();
    } catch (_) { return null; }
  }
  async function openContactMeta() {
    const chatId=selectedChatId(); if (!chatId || isGroupChat(chatId)) return toast("Выберите личный чат");
    const state=await loadContactState(chatId); const data=state?.contact || {};
    const form=document.createElement("form"); form.className="productivity-form-grid";
    const org=document.createElement("input"); org.value=data.organization || ""; org.maxLength=160; org.placeholder="Компания / организация";
    const postInput=document.createElement("input"); postInput.value=data.post || ""; postInput.maxLength=160; postInput.placeholder="Пост / подразделение";
    const note=document.createElement("textarea"); note.value=data.note || ""; note.maxLength=3000; note.rows=5; note.placeholder="Служебная заметка, видна только сотрудникам";
    const important=document.createElement("input"); important.type="checkbox"; important.checked=Boolean(data.important);
    const block=document.createElement("input"); block.type="checkbox"; block.checked=Boolean(data.auto_reply_blocked);
    form.append(formField("Организация",org),formField("Пост",postInput),formField("Служебная заметка",note));
    const c1=document.createElement("label"); c1.className="productivity-check"; c1.append(important,document.createTextNode(" Важный контакт")); form.append(c1);
    const c2=document.createElement("label"); c2.className="productivity-check"; c2.append(block,document.createTextNode(" Исключить из автоответов")); form.append(c2);
    const save=document.createElement("button"); save.className="button primary"; save.textContent="Сохранить"; form.append(save);
    form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{await post("/api/contact-meta",{chat_id:chatId,organization:org.value,post:postInput.value,note:note.value,important:important.checked?"1":"0",auto_reply_blocked:block.checked?"1":"0"});toast("Данные контакта сохранены");closeModal();refreshContactBadge();}catch(err){toast(err.message);}finally{save.disabled=false;}};
    modal("Теги и заметка контакта",form,{eyebrow:"Личный чат"});
  }
  async function refreshContactBadge() {
    const chatId=selectedChatId(); const header=qs(".chat-header > div:first-child"); if (!header) return;
    header.querySelector(".productivity-contact-badge")?.remove();
    if (!chatId || isGroupChat(chatId)) return;
    const state=await loadContactState(chatId); const m=state?.contact || {};
    const parts=[m.important?"★ Важный":"",m.organization||"",m.post||""].filter(Boolean); if (!parts.length) return;
    const badge=document.createElement("small"); badge.className="productivity-contact-badge"; badge.textContent=parts.join(" · "); header.append(badge);
  }

  // ----- Mute / mark unread -----
  async function openMuteMenu() {
    const chatId=selectedChatId(); if (!chatId) return toast("Выберите чат");
    const state=await loadContactState(chatId); const current=state?.mute || {};
    const box=document.createElement("div"); box.className="productivity-choice-list";
    const choices=[
      ["hour","На 1 час"],["shift","До конца смены"],["permanent","Постоянно"],["off","Включить уведомления"],
    ];
    const info=document.createElement("p"); info.className="muted"; info.textContent=current.muted?`Сейчас уведомления отключены (${current.mode || ""})`:"Сейчас уведомления включены"; box.append(info);
    choices.forEach(([mode,label])=>{const b=button(label,"button");b.onclick=async()=>{b.disabled=true;try{await post("/api/chat-mute",{chat_id:chatId,mode});toast(mode==="off"?"Уведомления включены":"Уведомления отключены");closeModal();}catch(err){toast(err.message);}finally{b.disabled=false;}};box.append(b);});
    modal("Уведомления чата",box,{eyebrow:isGroupChat(chatId)?"Группа":"Личный чат"});
  }
  async function markUnread() {
    const chatId=selectedChatId(); if (!chatId) return toast("Выберите чат");
    try { await post("/api/chat-mark-unread",{chat_id:chatId}); sessionStorage.setItem(`queue-manual-unread:${chatId}`,"1"); toast("Чат помечен как непрочитанный"); } catch(err) { toast(err.message); }
  }

  // ----- Media gallery -----
  async function openGallery() {
    const chatId=selectedChatId(); if (!chatId) return toast("Выберите чат");
    const holder=document.createElement("div"); holder.innerHTML='<p class="muted">Загрузка медиа...</p>';
    const ui=modal("Медиа чата",holder,{wide:true,eyebrow:"Фото · Видео · Файлы · Голосовые"});
    try {
      const r=await fetch(`/api/chat-gallery?chat_id=${encodeURIComponent(chatId)}`,{cache:"no-store"}); const data=await r.json();
      const items=Array.isArray(data.items)?data.items:[]; holder.replaceChildren();
      const tabs=document.createElement("div"); tabs.className="productivity-tabs"; const grid=document.createElement("div"); grid.className="productivity-gallery";
      const defs=[["all","Все"],["photo","Фото"],["video","Видео"],["file","Файлы"],["voice","Голосовые"]]; let active="all";
      const render=()=>{grid.replaceChildren();const filtered=items.filter(x=>active==="all"||x.kind===active); if(!filtered.length){grid.innerHTML='<p class="muted">В этом разделе ничего нет</p>';return;} filtered.forEach(item=>{
        const card=document.createElement("a"); card.className=`productivity-media-card kind-${item.kind}`; card.href=item.media_url; card.target="_blank"; card.rel="noopener";
        if(item.kind==="photo"){const img=document.createElement("img");img.src=item.media_url;img.loading="lazy";img.alt=item.media_name||"Фото";card.append(img);} else {const icon=document.createElement("span");icon.className="productivity-media-icon";icon.textContent=item.kind==="video"?"▶":item.kind==="voice"?"♫":"📄";card.append(icon);}
        const text=document.createElement("strong");text.textContent=item.media_name||item.body||({photo:"Фото",video:"Видео",voice:"Голосовое",file:"Файл"}[item.kind]);card.append(text);grid.append(card);
      });};
      defs.forEach(([id,label])=>{const b=button(label,"button compact ghost");b.dataset.galleryTab=id;b.onclick=()=>{active=id;qsa("[data-gallery-tab]",tabs).forEach(x=>x.classList.toggle("active",x.dataset.galleryTab===id));render();};tabs.append(b);});
      holder.append(tabs,grid); tabs.firstElementChild?.classList.add("active"); render();
    } catch(err) { holder.innerHTML=`<p class="error">${escapeHtml(err.message || "Не удалось загрузить медиа")}</p>`; }
  }

  // ----- Search inside current chat -----
  async function openChatSearch() {
    const chatId=selectedChatId(); if (!chatId) return toast("Выберите чат");
    const box=document.createElement("div"); box.className="chat-find-box";
    const top=document.createElement("div"); top.className="chat-find-controls";
    const input=document.createElement("input"); input.type="search"; input.placeholder="Текст сообщения, имя или расшифровка";
    const find=button("Найти","button primary"); const prev=button("←","button compact"); const next=button("→","button compact"); const counter=document.createElement("span"); counter.className="muted";
    top.append(input,find,prev,next,counter); const list=document.createElement("div"); list.className="chat-find-results"; box.append(top,list);
    modal("Поиск в текущем чате",box,{wide:true}); let results=[], index=-1;
    const focus=idx=>{if(!results.length)return; index=(idx+results.length)%results.length;counter.textContent=`${index+1} / ${results.length}`;qsa(".chat-find-result",list).forEach((x,i)=>x.classList.toggle("active",i===index));const target=results[index];const row=qsa("[data-message-id]").find(x=>x.dataset.messageId===String(target.message_key));if(row){row.scrollIntoView({behavior:"smooth",block:"center"});row.classList.add("search-message-target");setTimeout(()=>row.classList.remove("search-message-target"),4000);closeModal();}else{const u=new URL(location.href);u.searchParams.set("chat_id",chatId);u.searchParams.set("message_id",target.message_key);location.href=u.toString();}};
    const run=async()=>{const q=input.value.trim();if(!q)return;find.disabled=true;try{const r=await fetch(`/api/chat-find?chat_id=${encodeURIComponent(chatId)}&q=${encodeURIComponent(q)}`,{cache:"no-store"});const data=await r.json();results=Array.isArray(data.results)?data.results:[];index=results.length?0:-1;counter.textContent=results.length?`1 / ${results.length}`:"0";list.replaceChildren();results.forEach((item,i)=>{const b=button((item.sender?item.sender+": ":"")+(item.body||item.transcript||item.media_name||"Сообщение"),"chat-find-result");b.onclick=()=>focus(i);list.append(b);});if(!results.length)list.innerHTML='<p class="muted">Совпадений нет</p>';}catch(err){toast(err.message);}finally{find.disabled=false;}};
    find.onclick=run; input.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();run();}}; prev.onclick=()=>focus(index-1); next.onclick=()=>focus(index+1); setTimeout(()=>input.focus(),50);
  }

  // ----- Message actions: bookmark / create or link ticket -----
  async function openCreateTicket(chatId,messageId) {
    let categories=[]; try { const r=await fetch("/api/productivity-categories",{cache:"no-store"}); categories=(await r.json()).categories||[]; } catch(_) {}
    if(!categories.length) categories=[["general","Другая проблема"],["support","Поддержка"]];
    const form=document.createElement("form"); const select=document.createElement("select"); categories.forEach(item=>{const o=document.createElement("option");o.value=item[0];o.textContent=item[1];select.append(o);});
    const save=document.createElement("button");save.className="button primary";save.textContent="Создать заявку";form.className="productivity-form-grid";form.append(formField("Категория",select),save);
    form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{const data=await post("/api/message-ticket-create",{chat_id:chatId,message_id:messageId,category:select.value});toast(`Создана заявка #${data.ticket_id}`);closeModal();if(confirm("Открыть созданную заявку?"))location.href=data.href;}catch(err){toast(err.message);}finally{save.disabled=false;}};
    modal("Создать заявку из сообщения",form,{eyebrow:"WhatsApp → Заявка"});
  }
  async function openLinkTicket(chatId,messageId) {
    const holder=document.createElement("div");holder.innerHTML='<p class="muted">Загрузка заявок...</p>';modal("Привязать к заявке",holder,{wide:true});
    try{const r=await fetch("/api/open-tickets",{cache:"no-store"});const data=await r.json();const tickets=Array.isArray(data.tickets)?data.tickets:[];holder.replaceChildren();const search=document.createElement("input");search.type="search";search.placeholder="Поиск по №, названию или пользователю";const list=document.createElement("div");list.className="ticket-link-list";holder.append(search,list);const render=()=>{const q=search.value.trim().toLocaleLowerCase("ru");list.replaceChildren();tickets.filter(t=>!q||`${t.id} ${t.title} ${t.sender} ${t.category}`.toLocaleLowerCase("ru").includes(q)).slice(0,100).forEach(t=>{const b=button(`#${t.id} · ${t.title} · ${t.sender}`,"ticket-link-item");b.onclick=async()=>{b.disabled=true;try{await post("/api/message-ticket-link",{chat_id:chatId,message_id:messageId,ticket_id:t.id});toast(`Сообщение привязано к заявке #${t.id}`);closeModal();}catch(err){toast(err.message);}finally{b.disabled=false;}};list.append(b);});};search.oninput=render;render();}catch(err){holder.innerHTML=`<p class="error">${escapeHtml(err.message)}</p>`;}
  }
  async function toggleBookmark(chatId,messageId) { try{const d=await post("/api/message-bookmark",{chat_id:chatId,message_id:messageId});toast(d.bookmarked?"Сообщение добавлено в закладки":"Сообщение убрано из закладок");}catch(err){toast(err.message);} }

  function augmentMessageContext(event) {
    const row=event.target.closest?.("[data-message-id]"); if(!row) return; const messageId=row.dataset.messageId; const chatId=selectedChatId(); if(!messageId||!chatId)return;
    setTimeout(()=>{
      const menu=qsa(".message-context-menu").find(x=>x.isConnected); if(!menu||menu.querySelector(".productivity-context"))return;
      const sep=document.createElement("div");sep.className="message-context-separator productivity-context";menu.append(sep);
      const add=(label,icon,fn)=>{const b=document.createElement("button");b.type="button";b.className="message-context-item productivity-context";b.innerHTML=`<span class="message-context-icon">${icon}</span><span>${escapeHtml(label)}</span>`;b.onclick=e=>{e.stopPropagation();menu.remove();fn();};menu.append(b);};
      add("Закладка","★",()=>toggleBookmark(chatId,messageId)); add("Создать заявку","＋",()=>openCreateTicket(chatId,messageId)); add("Привязать к заявке","🔗",()=>openLinkTicket(chatId,messageId));
    },0);
  }
  document.addEventListener("contextmenu",augmentMessageContext,true);

  // Bookmark / reminder page buttons.
  document.addEventListener("click", async e=>{
    const link=e.target.closest?.("[data-bookmark-link]");
    if(link){openLinkTicket(link.dataset.chatId,link.dataset.messageId);return;}
    const b=e.target.closest?.("[data-bookmark-remove]");
    if(b){try{await toggleBookmark(b.dataset.chatId,b.dataset.messageId);b.closest(".bookmark-card")?.remove();}catch(_){} return;}
    const done=e.target.closest?.("[data-reminder-done]");
    if(done){try{await post("/api/reminder-done",{id:done.dataset.reminderDone});done.closest(".reminder-card")?.remove();toast("Напоминание закрыто");}catch(err){toast(err.message);}}
  });

  // ----- Ticket reminder -----
  function openReminder(ticketId) {
    const form=document.createElement("form");form.className="productivity-form-grid";
    const presets=document.createElement("div");presets.className="productivity-preset-row";
    const when=document.createElement("input");when.type="datetime-local";const note=document.createElement("textarea");note.rows=3;note.maxLength=1000;note.placeholder="Что нужно напомнить";
    const setMinutes=min=>{const d=new Date(Date.now()+min*60000);d.setMinutes(d.getMinutes()-d.getTimezoneOffset());when.value=d.toISOString().slice(0,16);};
    [[30,"Через 30 минут"],[60,"Через 1 час"],[24*60,"Завтра"]].forEach(([min,label])=>{const b=button(label,"button compact ghost");b.onclick=()=>setMinutes(min);presets.append(b);});setMinutes(30);
    const save=document.createElement("button");save.className="button primary";save.textContent="Поставить напоминание";form.append(presets,formField("Когда",when),formField("Заметка",note),save);
    form.onsubmit=async e=>{e.preventDefault();const local=new Date(when.value);if(Number.isNaN(local.getTime()))return toast("Выберите время");save.disabled=true;try{await post("/api/ticket-reminder",{ticket_id:ticketId,remind_at:local.toISOString(),note:note.value});toast("Напоминание сохранено");closeModal();}catch(err){toast(err.message);}finally{save.disabled=false;}};
    modal(`Напоминание по заявке #${ticketId}`,form,{eyebrow:"Заявки"});
  }
  function installTicketReminderButton() {
    if(location.pathname!=="/ticket")return;const id=new URL(location.href).searchParams.get("id");if(!id)return;const aside=qs(".ticket-layout aside")||qs("main aside");if(!aside||qs("[data-ticket-reminder-button]"))return;const panel=document.createElement("section");panel.className="panel productivity-reminder-panel";panel.innerHTML='<h2>Напоминание</h2><p class="muted">Вернуться к заявке позже</p>';const b=button("Поставить напоминание","button");b.dataset.ticketReminderButton="1";b.onclick=()=>openReminder(id);const link=document.createElement("a");link.href="/reminders";link.className="button compact ghost";link.textContent="Все напоминания";panel.append(b,link);aside.append(panel);
  }

  // ----- Delivery ticks -----
  function enhanceDelivery(root=document) {
    qsa(".chat-delivery",root).forEach(el=>{
      const map={"Отправляется":"◷","Отправлено":"✓","Доставлено":"✓✓","Прочитано":"✓✓"};
      const visible=(el.textContent||"").trim();
      const raw=map[visible]?visible:String(el.dataset.originalDelivery||visible).trim();
      if(!map[raw])return;
      el.dataset.originalDelivery=raw;el.textContent=map[raw];el.title=raw;el.classList.toggle("delivery-read",raw==="Прочитано");el.classList.add("delivery-ticks");
    });
  }

  // ----- Optional automatic transcription for short voice messages -----
  const AUTO_VOICE_KEY="queue-auto-transcribe-short-voice";
  const autoVoiceSeen=new Set();
  const autoVoiceEnabled=()=>{try{return localStorage.getItem(AUTO_VOICE_KEY)==="1";}catch(_){return false;}};
  function autoTranscribe(root=document) {
    if(!autoVoiceEnabled())return;
    qsa(".chat-message-row",root).forEach(row=>{
      const btn=qs(".voice-transcribe",row);const id=row.dataset.messageId;const chatId=selectedChatId();if(!btn||!id||!chatId||autoVoiceSeen.has(`${chatId}|${id}`))return;
      autoVoiceSeen.add(`${chatId}|${id}`);
      const audio=new Audio();audio.preload="metadata";let finished=false;
      const stop=()=>{if(finished)return;finished=true;audio.src="";};
      const timer=setTimeout(stop,7000);
      audio.addEventListener("loadedmetadata",()=>{clearTimeout(timer);const duration=Number(audio.duration||0);stop();if(duration>0&&duration<=60&&btn.isConnected&&!btn.disabled){btn.dataset.autoTranscribed="1";btn.click();}},{once:true});
      audio.addEventListener("error",()=>{clearTimeout(timer);stop();},{once:true});
      audio.src=`/api/chat-media?chat_id=${encodeURIComponent(chatId)}&message_id=${encodeURIComponent(id)}`;
    });
  }

  // ----- Chat header productivity buttons -----
  function installChatTools() {
    const page=qs("[data-conversation-page]"); const actions=qs(".chat-header-actions",page||document); if(!page||!actions)return;
    if(!qs(".productivity-chat-tools",actions)){
      const wrap=document.createElement("div");wrap.className="productivity-chat-tools";
      const search=button("🔎 Поиск","button compact ghost");search.onclick=openChatSearch;
      const media=button("Медиа","button compact ghost");media.onclick=openGallery;
      const unread=button("Непрочитано","button compact ghost");unread.onclick=markUnread;
      const mute=button("🔕 Уведомления","button compact ghost");mute.onclick=openMuteMenu;
      const autoVoice=button(autoVoiceEnabled()?"🎙 Авто-голос: вкл":"🎙 Авто-голос: выкл","button compact ghost");
      autoVoice.dataset.autoVoiceToggle="1";autoVoice.onclick=()=>{const enabled=!autoVoiceEnabled();try{localStorage.setItem(AUTO_VOICE_KEY,enabled?"1":"0");}catch(_){}autoVoice.textContent=enabled?"🎙 Авто-голос: вкл":"🎙 Авто-голос: выкл";if(enabled){autoVoiceSeen.clear();autoTranscribe();}toast(enabled?"Авторасшифровка коротких голосовых включена":"Авторасшифровка выключена");};
      wrap.append(search,media,unread,mute,autoVoice);
      if(page.hasAttribute("data-whatsapp-page")){const meta=button("Теги / заметка","button compact ghost");meta.onclick=openContactMeta;wrap.append(meta);}
      actions.prepend(wrap);
    }
    const chatId=selectedChatId();
    if(chatId!==lastProductivityChat){lastProductivityChat=chatId;refreshContactBadge();}
  }

  // Generic observer because the legacy chat UI re-renders rows every few seconds.
  const observer=new MutationObserver(records=>{
    for(const rec of records){for(const node of rec.addedNodes){if(node.nodeType===1){enhanceDelivery(node);autoTranscribe(node);}}}
    installChatTools();
  });
  observer.observe(document.documentElement,{subtree:true,childList:true});
  enhanceDelivery(); installChatTools(); installTicketReminderButton(); autoTranscribe();

  // Update selected-chat-dependent contact badge even when only hidden input value changes.
  setInterval(()=>{installChatTools();enhanceDelivery();autoTranscribe();},1600);
  document.documentElement.dataset.productivityBuild=BUILD;
})();
