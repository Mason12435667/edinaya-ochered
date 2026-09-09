(() => {
  "use strict";
  const BUILD = "3.3.98";
  const qs=(s,r=document)=>r.querySelector(s), qsa=(s,r=document)=>[...r.querySelectorAll(s)];
  const csrf=()=>qs('meta[name="queue-csrf"]')?.content||"";
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const post=async(path,values={})=>{const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"fetch"},body:new URLSearchParams({...values,csrf_token:csrf()}),cache:"no-store"});let d={};try{d=await r.json();}catch(_){}if(!r.ok)throw new Error(d.error||"Не удалось выполнить действие");return d;};
  const notice=(text,kind="info")=>{if(window.QueueUI?.toast)window.QueueUI.toast(String(text));else if(window.QueueAppNotice)window.QueueAppNotice(String(text),kind);else console.log(text);};
  const selectedChatId=()=>String(qs("[data-chat-id]")?.value||"").trim();
  const isGroup=id=>String(id||"").endsWith("@g.us");
  const activeEmployee=()=>String(qs(".shift-form select[name='employee']")?.value||"").trim();
  const clientId=(()=>{try{let x=sessionStorage.getItem("queue-workflow-client");if(!x){x=crypto.randomUUID?crypto.randomUUID():Date.now()+"-"+Math.random().toString(36).slice(2);sessionStorage.setItem("queue-workflow-client",x);}return x;}catch(_){return Date.now()+"-"+Math.random().toString(36).slice(2);}})();

  let overlay=null;
  function closeModal(){if(overlay){overlay.remove();overlay=null;}}
  function modal(title,body,{wide=false,eyebrow="Единая очередь"}={}){closeModal();overlay=document.createElement("div");overlay.className="workflow-overlay";const card=document.createElement("section");card.className="workflow-modal"+(wide?" wide":"");card.innerHTML=`<header><div><small>${esc(eyebrow)}</small><h2>${esc(title)}</h2></div><button type="button" aria-label="Закрыть">×</button></header><div class="workflow-modal-body"></div>`;card.querySelector(".workflow-modal-body").append(body);card.querySelector("header button").onclick=closeModal;overlay.append(card);document.body.append(overlay);overlay.addEventListener("pointerdown",e=>{if(e.target===overlay)closeModal();});return card;}
  function confirmBox(title,text,okLabel="Продолжить"){return new Promise(resolve=>{const box=document.createElement("div");box.className="workflow-confirm";box.innerHTML=`<p>${esc(text)}</p><div class="workflow-dialog-actions"><button type="button" class="button ghost" data-no>Отмена</button><button type="button" class="button primary" data-yes>${esc(okLabel)}</button></div>`;modal(title,box);const finish=v=>{closeModal();resolve(v);};qs("[data-no]",box).onclick=()=>finish(false);qs("[data-yes]",box).onclick=()=>finish(true);});}
  function field(label,input){const l=document.createElement("label");l.className="workflow-field";const s=document.createElement("span");s.textContent=label;l.append(s,input);return l;}
  function button(text,cls="button compact ghost"){const b=document.createElement("button");b.type="button";b.className=cls;b.textContent=text;return b;}

  // Topbar WhatsApp health + separate unread counters.
  let lastConnectedAt=0;
  async function refreshTopbar(){
    try{
      const [stateR,countR]=await Promise.all([fetch(`/api/chat-state?chat_id=${encodeURIComponent(selectedChatId())}`,{cache:"no-store"}),fetch("/api/workflow-unread-counts",{cache:"no-store"})]);
      const state=stateR.ok?await stateR.json():{};const counts=countR.ok?await countR.json():{};
      let pill=qs("[data-workflow-connection]");if(!pill){pill=document.createElement("span");pill.dataset.workflowConnection="1";pill.className="workflow-connection-pill";const top=qs(".topbar");top?.insertBefore(pill,qs(".notification-button",top)||null);}
      const connected=Boolean(state.connected);if(connected)lastConnectedAt=Date.now();const delayed=!connected&&lastConnectedAt&&Date.now()-lastConnectedAt<120000;
      pill.className="workflow-connection-pill "+(connected?"ok":delayed?"delay":"down");pill.textContent=connected?"● WhatsApp подключён":delayed?"● WhatsApp: задержка":"● Коннектор недоступен";
      qsa('.nav-link[href="/whatsapp"], .nav-link[href="/groups"]').forEach(a=>{const isGroups=a.getAttribute("href")==="/groups";let badge=qs(".workflow-nav-count",a);const n=isGroups?Number(counts.groups||0):Number(counts.personal||0);if(!badge){badge=document.createElement("b");badge.className="workflow-nav-count";a.append(badge);}badge.textContent=String(n);badge.hidden=!n;});
    }catch(_){const pill=qs("[data-workflow-connection]");if(pill){pill.className="workflow-connection-pill down";pill.textContent="● Коннектор недоступен";}}
  }

  // Recent chats and chat-scoped state.
  let lastChat="", workflowState={}, typingBaseline="";
  async function openChatHeartbeat(force=false){const chatId=selectedChatId();if(!chatId)return;if(force||chatId!==lastChat){lastChat=chatId;typingBaseline="";post("/api/workflow-recent-open",{chat_id:chatId}).catch(()=>{});}const textarea=qs("[data-chat-composer] textarea[name='message']");const state=(document.activeElement===textarea&&String(textarea?.value||"").trim())?"typing":"viewing";try{workflowState=await post("/api/workflow-presence",{chat_id:chatId,client_id:clientId,state});renderPresence(workflowState.presence||[]);}catch(_){}}
  function renderPresence(items){const composer=qs("[data-chat-composer]");if(!composer)return;let bar=qs(".workflow-presence-bar",composer.parentElement||document);if(!bar){bar=document.createElement("div");bar.className="workflow-presence-bar";composer.parentElement?.insertBefore(bar,composer);}const live=(items||[]).filter(x=>x&&x.actor);if(!live.length){bar.hidden=true;bar.textContent="";return;}bar.hidden=false;const typing=live.find(x=>x.state==="typing");bar.classList.toggle("typing",Boolean(typing));bar.textContent=typing?`${typing.actor} сейчас набирает ответ`:`${live[0].actor} уже работает с этим чатом`;}

  // Conflict protection: another browser/employee is in chat or sent after typing began.
  document.addEventListener("input",e=>{const ta=e.target.closest?.("[data-chat-composer] textarea[name='message']");if(!ta)return;if(!typingBaseline)typingBaseline=String(workflowState?.conflict?.last_outbound?.id||workflowState?.conflict?.latest_message_key||"");openChatHeartbeat().catch(()=>{});},true);
  document.addEventListener("submit",async e=>{const form=e.target.closest?.("[data-chat-composer]");if(!form||form.dataset.workflowBypass==="1") {if(form)delete form.dataset.workflowBypass;return;}const others=Array.isArray(workflowState?.presence)?workflowState.presence:[];const latest=String(workflowState?.conflict?.last_outbound?.id||workflowState?.conflict?.latest_message_key||"");const changed=Boolean(typingBaseline&&latest&&latest!==typingBaseline);if(!others.length&&!changed)return;e.preventDefault();e.stopImmediatePropagation();const who=others[0]?.actor||"другой сотрудник";const text=changed?"Пока вы печатали, из этого чата уже было отправлено новое сообщение. Проверьте переписку перед отправкой.":`${who} уже работает с этим чатом. Отправить сообщение всё равно?`;if(await confirmBox("Конфликт ответа",text,"Всё равно отправить")){form.dataset.workflowBypass="1";typingBaseline="";form.requestSubmit();}},true);

  // New message indicator while reading old history.
  let knownMessageIds=new Set(), pendingNew=0;
  function installNewMessageIndicator(){const scroller=qs("[data-chat-messages]");if(!scroller||scroller.dataset.workflowNewIndicator)return;scroller.dataset.workflowNewIndicator="1";knownMessageIds=new Set(qsa("[data-message-id]",scroller).map(x=>x.dataset.messageId));const badge=document.createElement("button");badge.type="button";badge.className="workflow-new-message";badge.hidden=true;scroller.parentElement?.append(badge);const nearBottom=()=>scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<110;const update=()=>{badge.hidden=!pendingNew;badge.textContent=`${pendingNew} новых сообщения ↓`;};badge.onclick=()=>{pendingNew=0;update();scroller.scrollTo({top:scroller.scrollHeight,behavior:"smooth"});};scroller.addEventListener("scroll",()=>{if(nearBottom()&&pendingNew){pendingNew=0;update();}},{passive:true});new MutationObserver(()=>{const now=qsa("[data-message-id]",scroller);const added=now.filter(x=>x.dataset.messageId&&!knownMessageIds.has(x.dataset.messageId));now.forEach(x=>knownMessageIds.add(x.dataset.messageId));if(added.length&&!nearBottom()){pendingNew+=added.length;update();}}).observe(scroller,{childList:true,subtree:true});}

  async function fetchJson(url){const r=await fetch(url,{cache:"no-store"});let d={};try{d=await r.json();}catch(_){}if(!r.ok)throw new Error(d.error||"Не удалось загрузить данные");return d;}

  function exportMenu(){const chatId=selectedChatId();if(!chatId)return notice("Выберите чат");const box=document.createElement("div");box.className="workflow-choice-list";[["txt","TXT · только история"],["pdf","PDF · история для печати"],["zip","ZIP · история + вложения"]].forEach(([fmt,label])=>{const a=document.createElement("a");a.className="button";a.textContent=label;a.href=`/api/workflow-export?chat_id=${encodeURIComponent(chatId)}&format=${fmt}`;a.onclick=()=>setTimeout(closeModal,250);box.append(a);});modal("Экспорт истории",box,{eyebrow:"Чат"});}
  async function historyModal(){const chatId=selectedChatId();if(!chatId)return;const box=document.createElement("div");box.innerHTML='<p class="muted">Загрузка...</p>';modal("История действий",box,{wide:true,eyebrow:"Сотрудники и система"});try{const d=await fetchJson(`/api/workflow-chat-history?chat_id=${encodeURIComponent(chatId)}`);box.replaceChildren();(d.events||[]).forEach(x=>{const row=document.createElement("article");row.className="workflow-event";row.innerHTML=`<strong>${esc(x.action||x.event_type||"Действие")}</strong><span>${esc(x.actor||"Система")}</span><small>${esc(String(x.created_at||"").replace("T"," ").slice(0,19))}${x.details?" · "+esc(x.details):""}</small>`;box.append(row);});if(!box.children.length)box.innerHTML='<p class="muted">История пока пустая</p>';}catch(err){box.innerHTML=`<p class="error">${esc(err.message)}</p>`;}}
  async function summaryModal(){const chatId=selectedChatId();if(!chatId)return;const d=await fetchJson(`/api/workflow-summary?chat_id=${encodeURIComponent(chatId)}`);const box=document.createElement("div");box.className="workflow-summary";box.innerHTML=`<div><span>Суть обращения</span><strong>${esc(d.essence||"Нет данных")}</strong></div><div><span>Последнее от пользователя</span><p>${esc(d.last_user||"—")}</p></div><div><span>Последний ответ сотрудника</span><p>${esc(d.last_staff||"—")}</p></div><div><span>Сейчас ожидается</span><strong>${esc(d.waiting||"—")}</strong></div><div><span>Открытые заявки</span><p>${(d.open_tickets||[]).map(t=>`#${t.id} ${esc(t.title)}`).join("<br>")||"Нет"}</p></div>`;modal("Краткая сводка переписки",box,{wide:true});}
  async function ticketsModal(){const chatId=selectedChatId();if(!chatId)return;const d=await fetchJson(`/api/workflow-tickets?chat_id=${encodeURIComponent(chatId)}&all=1`);const box=document.createElement("div");box.className="workflow-ticket-list";(d.tickets||[]).forEach(t=>{const a=document.createElement("a");a.href=`/ticket?id=${t.id}`;a.className="workflow-ticket-link";a.innerHTML=`<strong>#${t.id} · ${esc(t.title)}</strong><small>${esc(t.status)} · ${esc(t.priority)} · ${esc(String(t.updated_at||"").replace("T"," ").slice(0,16))}</small>`;box.append(a);});if(!box.children.length)box.innerHTML='<p class="muted">Обращений не найдено</p>';modal("История обращений",box,{wide:true,eyebrow:"Заявки этого пользователя"});}
  async function quickTicketModal(){const chatId=selectedChatId();if(!chatId)return;const d=await fetchJson(`/api/workflow-tickets?chat_id=${encodeURIComponent(chatId)}`);const tickets=d.tickets||[];if(!tickets.length)return notice("У этого чата нет открытых заявок");const ticket=tickets[0];const form=document.createElement("form");form.className="workflow-form";const status=document.createElement("select");[["","Не менять статус"],["new","Новая"],["in_progress","В работе"],["done","Выполнено"],["invalid","Недействительно"]].forEach(([v,l])=>status.add(new Option(l,v)));const priority=document.createElement("select");[["","Не менять приоритет"],["low","Низкий"],["normal","Обычный"],["high","Высокий"],["urgent","Срочный"]].forEach(([v,l])=>priority.add(new Option(l,v)));const reason=document.createElement("select");[["","Причина закрытия (если нужно)"],["resolved","Решено"],["duplicate","Дубль"],["invalid","Недействительно"],["user_error","Ошибка пользователя"],["transferred","Передано"],["other","Другое"]].forEach(([v,l])=>reason.add(new Option(l,v)));const comment=document.createElement("textarea");comment.rows=3;comment.placeholder="Комментарий при закрытии";const save=document.createElement("button");save.className="button primary";save.textContent="Сохранить";form.append(field("Статус",status),field("Приоритет",priority),field("Причина",reason),field("Комментарий",comment),save);form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{await post("/api/workflow-ticket-quick",{ticket_id:ticket.id,status:status.value,priority:priority.value,reason:reason.value,comment:comment.value});notice(`Заявка #${ticket.id} обновлена`);closeModal();}catch(err){notice(err.message,"error");}finally{save.disabled=false;}};modal(`Быстрые действия · заявка #${ticket.id}`,form,{eyebrow:"Не открывая карточку"});}

  function installChatButtons(){const page=qs("[data-conversation-page]");const actions=qs(".chat-header-actions",page||document);if(!page||!actions||qs(".workflow-chat-tools",actions))return;const wrap=document.createElement("div");wrap.className="workflow-chat-tools";[["Экспорт",exportMenu],["История",historyModal],["Сводка",summaryModal],["Обращения",ticketsModal],["Заявка",quickTicketModal]].forEach(([label,fn])=>{const b=button(label);b.onclick=()=>Promise.resolve(fn()).catch(err=>notice(err.message,"error"));wrap.append(b);});actions.append(wrap);}

  // Ticket page workflow panels.
  async function installTicketTools(){if(location.pathname!=="/ticket")return;const ticketId=Number(new URL(location.href).searchParams.get("id")||0);if(!ticketId||qs("[data-workflow-ticket-tools]"))return;const aside=qs(".ticket-layout aside");if(!aside)return;let d;try{d=await fetchJson(`/api/workflow-ticket-details?ticket_id=${ticketId}`);}catch(_){return;}const panel=document.createElement("section");panel.className="panel workflow-ticket-tools";panel.dataset.workflowTicketTools="1";panel.innerHTML='<h2>Работа с заявкой</h2><div class="workflow-ticket-action-row"></div>';const row=qs(".workflow-ticket-action-row",panel);const noteBtn=button("Внутренняя заметка","button");noteBtn.onclick=()=>openInternalNote(ticketId);const transferBtn=button("Передать сотруднику","button");transferBtn.onclick=()=>openTransfer(ticketId,d.employees||[]);const mergeBtn=button("Объединить","button ghost");mergeBtn.onclick=()=>openMerge(ticketId);const splitBtn=button("Разделить","button ghost");splitBtn.onclick=()=>openSplit(ticketId);row.append(noteBtn,transferBtn,mergeBtn,splitBtn);aside.insertBefore(panel,aside.firstChild?.nextSibling||null);
    const notes=document.createElement("section");notes.className="panel workflow-notes-panel";notes.innerHTML='<h2>Внутренние заметки</h2><div class="workflow-notes-list"></div>';const list=qs(".workflow-notes-list",notes);(d.notes||[]).forEach(n=>{const el=document.createElement("article");el.innerHTML=`<strong>${esc(n.actor||"Сотрудник")}</strong><p>${esc(n.note)}</p><small>${esc(String(n.created_at||"").replace("T"," ").slice(0,16))}</small>`;list.append(el);});if(!list.children.length)list.innerHTML='<p class="muted">Заметок пока нет</p>';aside.append(notes);
    if(d.close&&d.close.reason){const close=document.createElement("section");close.className="panel workflow-close-meta";const labels={resolved:"Решено",duplicate:"Дубль",invalid:"Недействительно",user_error:"Ошибка пользователя",transferred:"Передано",other:"Другое"};close.innerHTML=`<h2>Закрытие заявки</h2><strong>${esc(labels[d.close.reason]||d.close.reason)}</strong>${d.close.comment?`<p>${esc(d.close.comment)}</p>`:""}<small>${esc(d.close.actor||"")} · ${esc(String(d.close.closed_at||"").replace("T"," ").slice(0,16))}</small>`;aside.append(close);}
    if((d.related||[]).length){const rel=document.createElement("section");rel.className="panel workflow-related-panel";rel.innerHTML='<h2>Предыдущие обращения</h2>';(d.related||[]).slice(0,10).forEach(t=>{const a=document.createElement("a");a.href=`/ticket?id=${t.id}`;a.textContent=`#${t.id} · ${t.title}`;rel.append(a);});aside.append(rel);}
    installClosePrompts(d);
  }
  function openInternalNote(ticketId){const form=document.createElement("form");const ta=document.createElement("textarea");ta.rows=5;ta.maxLength=5000;ta.placeholder="Эту заметку видят только сотрудники";const save=document.createElement("button");save.className="button primary";save.textContent="Добавить заметку";form.append(field("Служебная заметка",ta),save);form.onsubmit=async e=>{e.preventDefault();try{await post("/api/workflow-internal-note",{ticket_id:ticketId,note:ta.value});location.reload();}catch(err){notice(err.message,"error");}};modal("Внутренняя заметка",form,{eyebrow:"Не отправляется в WhatsApp"});}
  function openTransfer(ticketId,employees){const form=document.createElement("form");const sel=document.createElement("select");(employees||[]).forEach(x=>sel.add(new Option(x,x)));const reason=document.createElement("textarea");reason.rows=4;reason.placeholder="Почему передаём заявку";const save=document.createElement("button");save.className="button primary";save.textContent="Передать";form.append(field("Сотрудник",sel),field("Причина передачи",reason),save);form.onsubmit=async e=>{e.preventDefault();try{await post("/api/workflow-transfer",{ticket_id:ticketId,employee:sel.value,reason:reason.value});location.reload();}catch(err){notice(err.message,"error");}};modal("Передать заявку",form);}
  async function loadOpenTickets(){const r=await fetch("/api/open-tickets",{cache:"no-store"});const d=await r.json();return d.tickets||[];}
  async function openMerge(sourceId){const tickets=(await loadOpenTickets()).filter(x=>Number(x.id)!==Number(sourceId));const box=document.createElement("div");const sel=document.createElement("select");tickets.forEach(t=>sel.add(new Option(`#${t.id} · ${t.title}`,t.id)));const note=document.createElement("textarea");note.rows=3;note.placeholder="Комментарий к объединению";const b=button("Объединить","button primary");b.onclick=async()=>{if(!(await confirmBox("Объединить заявки",`Заявка #${sourceId} станет недействительной, а её связанные сообщения перейдут в выбранную заявку.`,"Объединить")))return;try{await post("/api/workflow-ticket-merge",{source_id:sourceId,target_id:sel.value,note:note.value});location.href=`/ticket?id=${sel.value}`;}catch(err){notice(err.message,"error");}};box.append(field("Основная заявка",sel),field("Комментарий",note),b);modal("Объединение заявок",box,{wide:true});}
  function openSplit(sourceId){const form=document.createElement("form");const title=document.createElement("input");title.maxLength=160;title.placeholder="Название новой заявки";const summary=document.createElement("textarea");summary.rows=5;summary.maxLength=2000;summary.placeholder="Какая часть проблемы станет отдельной заявкой";const save=document.createElement("button");save.className="button primary";save.textContent="Создать отдельную заявку";form.append(field("Название",title),field("Описание отдельной проблемы",summary),save);form.onsubmit=async e=>{e.preventDefault();try{const d=await post("/api/workflow-ticket-split",{source_id:sourceId,title:title.value,summary:summary.value});location.href=d.href;}catch(err){notice(err.message,"error");}};modal("Разделить заявку",form,{wide:true});}
  function installClosePrompts(details){qsa('form[action="/status"] button[name="status"][value="done"], form[action="/status"] button[name="status"][value="invalid"]').forEach(btn=>{if(btn.dataset.workflowClose)return;btn.dataset.workflowClose="1";btn.addEventListener("click",e=>{const form=btn.form;if(form.dataset.workflowCloseReady==="1"){delete form.dataset.workflowCloseReady;return;}e.preventDefault();e.stopImmediatePropagation();const box=document.createElement("form");const reason=document.createElement("select");[["resolved","Решено"],["duplicate","Дубль"],["invalid","Недействительно"],["user_error","Ошибка пользователя"],["transferred","Передано"],["other","Другое"]].forEach(([v,l])=>reason.add(new Option(l,v)));reason.value=btn.value==="invalid"?"invalid":"resolved";const comment=document.createElement("textarea");comment.rows=4;comment.placeholder=details.comment_required?"Комментарий обязателен":"Комментарий (необязательно)";const save=document.createElement("button");save.className="button primary";save.textContent="Закрыть заявку";box.append(field("Причина закрытия",reason),field("Комментарий",comment),save);box.onsubmit=ev=>{ev.preventDefault();if(details.comment_required&&!comment.value.trim())return notice("Для этой категории комментарий обязателен","error");[...form.querySelectorAll("input[data-workflow-close-hidden]")].forEach(x=>x.remove());[["close_reason",reason.value],["close_comment",comment.value],["status",btn.value]].forEach(([name,value])=>{const i=document.createElement("input");i.type="hidden";i.name=name;i.value=value;i.dataset.workflowCloseHidden="1";form.append(i);});form.dataset.workflowCloseReady="1";closeModal();form.requestSubmit();};modal("Закрытие заявки",box,{eyebrow:"Причина сохранится в истории"});},true);});}

  // 3.3.108: terminal quick status from the dashboard must ask for the same
  // close reason/comment as the ticket page. The reason is then sent to the
  // WhatsApp user by the server together with the closure notification.
  let quickContextTicketId = 0;
  window.addEventListener("contextmenu", event => {
    const row = event.target?.closest?.("[data-ticket-row]");
    if (row) quickContextTicketId = Number(row.dataset.ticketId || 0);
  }, true);

  async function openQuickClose(ticketId, status){
    if (!ticketId || !["done","invalid"].includes(status)) return;
    let details = {};
    try { details = await fetchJson(`/api/workflow-ticket-details?ticket_id=${ticketId}`); }
    catch (err) { notice(err.message || "Не удалось загрузить данные заявки", "error"); return; }

    const box = document.createElement("form");
    const reason = document.createElement("select");
    const reasons = details.close_reasons && typeof details.close_reasons === "object"
      ? Object.entries(details.close_reasons)
      : [["resolved","Решено"],["duplicate","Дубль"],["invalid","Недействительно"],["user_error","Ошибка пользователя"],["transferred","Передано"],["other","Другое"]];
    reasons.forEach(([value,label]) => reason.add(new Option(String(label), String(value))));
    reason.value = status === "invalid" ? "invalid" : "resolved";
    if (!reason.value && reason.options.length) reason.selectedIndex = 0;

    const comment = document.createElement("textarea");
    comment.rows = 4;
    comment.maxLength = 5000;
    comment.placeholder = details.comment_required ? "Комментарий обязателен" : "Комментарий (необязательно)";

    const save = document.createElement("button");
    save.className = "button primary";
    save.type = "submit";
    save.textContent = "Закрыть заявку";
    box.append(field("Причина закрытия", reason), field("Комментарий", comment), save);

    box.onsubmit = async event => {
      event.preventDefault();
      if (details.comment_required && !comment.value.trim()) {
        notice("Для этой категории комментарий обязателен", "error");
        return;
      }
      save.disabled = true;
      try {
        await post("/quick-status", {
          ticket_id: ticketId,
          status,
          close_reason: reason.value,
          close_comment: comment.value,
        });
        closeModal();
        const menu = qs("#ticket-context-menu");
        if (menu) menu.hidden = true;
        notice("Заявка закрыта. Причина отправлена пользователю.");
        setTimeout(() => location.reload(), 250);
      } catch (err) {
        save.disabled = false;
        notice(err.message || "Не удалось закрыть заявку", "error");
      }
    };
    modal("Закрытие заявки", box, {eyebrow:"Причина сохранится в истории и отправится пользователю"});
  }

  window.addEventListener("click", event => {
    const btn = event.target?.closest?.("#ticket-context-menu [data-quick-status]");
    if (!btn) return;
    const status = String(btn.dataset.quickStatus || "");
    if (!["done","invalid"].includes(status)) return;
    const menu = btn.closest("#ticket-context-menu");
    const ticketId = quickContextTicketId || Number(menu?.dataset.ticketId || 0);
    if (!ticketId) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    if (menu) menu.hidden = true;
    openQuickClose(ticketId, status);
  }, true);

  // Bulk actions on dashboard.
  function installBulkActions(){if(location.pathname!=="/")return;const table=qs("table");const rows=qsa("[data-ticket-row]");if(!table||!rows.length||qs("[data-workflow-bulk-bar]"))return;const head=qs("thead tr",table);if(head){const th=document.createElement("th");th.innerHTML='<input type="checkbox" data-workflow-select-all aria-label="Выбрать все">';head.prepend(th);}rows.forEach(row=>{const td=document.createElement("td");td.className="workflow-select-cell";td.innerHTML=`<input type="checkbox" data-workflow-ticket-check value="${esc(row.dataset.ticketId)}" aria-label="Выбрать заявку">`;row.prepend(td);});const bar=document.createElement("div");bar.dataset.workflowBulkBar="1";bar.className="workflow-bulk-bar";bar.innerHTML='<strong><span data-workflow-selected>0</span> выбрано</strong>';const act=button("Массовое действие","button primary compact");bar.append(act);table.parentElement?.parentElement?.insertBefore(bar,table.parentElement);const update=()=>{qs("[data-workflow-selected]",bar).textContent=String(qsa("[data-workflow-ticket-check]:checked").length);};qs("[data-workflow-select-all]")?.addEventListener("change",e=>{qsa("[data-workflow-ticket-check]").forEach(x=>x.checked=e.target.checked);update();});document.addEventListener("change",e=>{if(e.target.matches?.("[data-workflow-ticket-check]"))update();});act.onclick=()=>openBulk();}
  function openBulk(){const ids=qsa("[data-workflow-ticket-check]:checked").map(x=>Number(x.value)).filter(Boolean);if(!ids.length)return notice("Выберите заявки");const form=document.createElement("form");const status=document.createElement("select");[["","Статус не менять"],["new","Новая"],["in_progress","В работе"],["done","Выполнено"],["invalid","Недействительно"]].forEach(x=>status.add(new Option(x[1],x[0])));const priority=document.createElement("select");[["","Приоритет не менять"],["low","Низкий"],["normal","Обычный"],["high","Высокий"],["urgent","Срочный"]].forEach(x=>priority.add(new Option(x[1],x[0])));const employee=document.createElement("select");employee.add(new Option("Ответственного не менять",""));qsa(".shift-form select[name='employee'] option").forEach(o=>employee.add(new Option(o.textContent,o.value)));const reason=document.createElement("select");[["","Причина закрытия"],["resolved","Решено"],["duplicate","Дубль"],["invalid","Недействительно"],["user_error","Ошибка пользователя"],["transferred","Передано"],["other","Другое"]].forEach(x=>reason.add(new Option(x[1],x[0])));const comment=document.createElement("textarea");comment.rows=3;comment.placeholder="Комментарий для выбранных заявок";const save=document.createElement("button");save.className="button primary";save.textContent=`Применить к ${ids.length}`;form.append(field("Статус",status),field("Приоритет",priority),field("Ответственный",employee),field("Причина",reason),field("Комментарий",comment),save);form.onsubmit=async e=>{e.preventDefault();try{const d=await post("/api/workflow-bulk",{ticket_ids:JSON.stringify(ids),status:status.value,priority:priority.value,employee:employee.value,reason:reason.value,comment:comment.value});notice(`Обновлено: ${d.updated}; ошибок: ${d.failed}`);setTimeout(()=>location.reload(),500);}catch(err){notice(err.message,"error");}};modal("Массовые действия",form,{wide:true});}

  // Ask for a handover summary automatically after the active shift changes.
  document.addEventListener("change",e=>{if(e.target.matches?.(".shift-form select[name='employee']")){try{sessionStorage.setItem("queue-workflow-open-handover","1");}catch(_){}}},true);

  // Handover summary button near shift selector.
  function installHandover(){const shift=qs(".shift-form");if(!shift||qs("[data-workflow-handover]",shift.parentElement||document))return;const b=button("Сводка смены","workflow-handover-button");b.dataset.workflowHandover="1";b.onclick=async()=>{try{const d=await fetchJson("/api/workflow-handover");const box=document.createElement("div");box.className="workflow-handover-list";(d.tickets||[]).forEach(t=>{const a=document.createElement("a");a.href=`/ticket?id=${t.id}`;a.innerHTML=`<strong>#${t.id} · ${esc(t.title)}</strong><small>${esc(t.priority)} · ${esc(t.assigned_to||"Не назначен")}</small>`;box.append(a);});if(!box.children.length)box.innerHTML='<p class="muted">Открытых заявок нет</p>';modal("Сводка для передачи смены",box,{wide:true});}catch(err){notice(err.message,"error");}};shift.parentElement?.insertBefore(b,shift);}

  // Hotkeys: Alt+N unread, Ctrl+K chat search, Alt+1 tickets, Alt+W personal, Alt+G groups, Alt+R recent.
  document.addEventListener("keydown",async e=>{if(e.defaultPrevented)return;const tag=(e.target?.tagName||"").toLowerCase();if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"){e.preventDefault();const existing=qsa("button").find(x=>(x.textContent||"").includes("Поиск")&&x.closest(".chat-header-actions"));if(existing)existing.click();else location.href="/search";return;}if(!e.altKey)return;const key=e.key.toLowerCase();const routes={"1":"/","w":"/whatsapp","g":"/groups","r":"/recent","s":"/search"};if(routes[key]){e.preventDefault();location.href=routes[key];return;}if(key==="n"){e.preventDefault();try{const d=await fetchJson("/api/workflow-next-unread");if(d.href)location.href=d.href;else notice("Непрочитанных чатов нет");}catch(err){notice(err.message,"error");}}});

  function tick(){installChatButtons();installNewMessageIndicator();installBulkActions();installHandover();installTicketTools();const chatId=selectedChatId();if(chatId)openChatHeartbeat(chatId!==lastChat);}
  document.addEventListener("DOMContentLoaded",()=>{tick();refreshTopbar();try{if(sessionStorage.getItem("queue-workflow-open-handover")==="1"){sessionStorage.removeItem("queue-workflow-open-handover");setTimeout(()=>qs("[data-workflow-handover]")?.click(),500);}}catch(_){}});
  setInterval(tick,3000);setInterval(refreshTopbar,2000);setInterval(()=>openChatHeartbeat(),10000);
  window.addEventListener("focus",refreshTopbar);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)refreshTopbar();});
  window.addEventListener("beforeunload",()=>{const chatId=selectedChatId();if(chatId){try{navigator.sendBeacon?.("/api/workflow-presence",new URLSearchParams({chat_id:chatId,client_id:clientId,state:"closed",csrf_token:csrf()}));}catch(_){}}});
  document.documentElement.dataset.workflowBuild=BUILD;
})();
