// Общее для скриптов сайта: всплывающее сообщение, диалоги вместо alert/confirm/prompt и отправка формы.
window.QueueCommon = (() => {
  function queueNotice(message, kind = "info") {
    const note=document.createElement("div");
    note.className=`queue-inline-notice ${kind}`;
    const strong=document.createElement("strong"); strong.textContent=kind==="error"?"Ошибка":"Единая очередь";
    const span=document.createElement("span"); span.textContent=String(message || "");
    note.append(strong,span); document.body.append(note);
    requestAnimationFrame(()=>note.classList.add("show"));
    window.setTimeout(()=>{note.classList.remove("show");window.setTimeout(()=>note.remove(),180);}, kind==="error"?7000:4500);
    return note;
  }
  window.QueueAppNotice = queueNotice;

  function queueDialogShell(title, message, kind = "info") {
    const overlay=document.createElement("div"); overlay.className="queue-dialog-overlay";
    const card=document.createElement("section"); card.className=`queue-dialog ${kind}`; card.setAttribute("role","dialog"); card.setAttribute("aria-modal","true");
    const head=document.createElement("div"); head.className="queue-dialog-head";
    const symbol=document.createElement("span"); symbol.className="queue-dialog-symbol"; symbol.textContent=kind==="danger"?"!":"✓";
    const titles=document.createElement("div"); const h=document.createElement("h3"); h.textContent=String(title || "Единая очередь"); const small=document.createElement("small"); small.textContent="Единая очередь"; titles.append(h,small); head.append(symbol,titles);
    const body=document.createElement("div"); body.className="queue-dialog-body"; const p=document.createElement("p"); p.textContent=String(message || ""); body.append(p);
    const actions=document.createElement("div"); actions.className="queue-dialog-actions";
    card.append(head,body,actions); overlay.append(card); document.body.append(overlay);
    return {overlay,card,body,actions};
  }
  function queueAlert(message, title = "Единая очередь", kind = "danger") {
    const dlg=queueDialogShell(title,message,kind);
    const ok=document.createElement("button"); ok.type="button"; ok.className="button primary"; ok.textContent="Понятно"; dlg.actions.append(ok);
    const close=()=>dlg.overlay.remove(); ok.onclick=close; dlg.overlay.addEventListener("pointerdown",e=>{if(e.target===dlg.overlay)close();});
    ok.focus(); return dlg;
  }
  function queueConfirm(message, title = "Подтверждение") {
    return new Promise(resolve=>{
      const dlg=queueDialogShell(title,message,"danger");
      const cancel=document.createElement("button"); cancel.type="button"; cancel.className="button ghost"; cancel.textContent="Отмена";
      const ok=document.createElement("button"); ok.type="button"; ok.className="button primary"; ok.textContent="Удалить";
      dlg.actions.append(cancel,ok);
      let done=false; const finish=value=>{if(done)return;done=true;dlg.overlay.remove();resolve(value);};
      cancel.onclick=()=>finish(false); ok.onclick=()=>finish(true); dlg.overlay.addEventListener("pointerdown",e=>{if(e.target===dlg.overlay)finish(false);}); cancel.focus();
    });
  }
  function queuePrompt(message, initial = "", title = "Изменить сообщение") {
    return new Promise(resolve=>{
      const dlg=queueDialogShell(title,message,"info");
      const input=document.createElement("textarea"); input.rows=4; input.maxLength=32000; input.value=String(initial || ""); dlg.body.append(input);
      const cancel=document.createElement("button"); cancel.type="button"; cancel.className="button ghost"; cancel.textContent="Отмена";
      const ok=document.createElement("button"); ok.type="button"; ok.className="button primary"; ok.textContent="Сохранить"; dlg.actions.append(cancel,ok);
      let done=false; const finish=value=>{if(done)return;done=true;dlg.overlay.remove();resolve(value);};
      cancel.onclick=()=>finish(null); ok.onclick=()=>finish(input.value); dlg.overlay.addEventListener("pointerdown",e=>{if(e.target===dlg.overlay)finish(null);});
      input.addEventListener("keydown",e=>{if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)){e.preventDefault();finish(input.value);}}); input.focus(); input.select();
    });
  }

  async function postForm(endpoint, values) {
    if (["/quick-status","/quick-priority"].includes(endpoint)) {
      const row = document.querySelector(`[data-ticket-row][data-ticket-id="${Number(values.ticket_id)}"]`);
      values.revision = row?.dataset.revision ?? "-1";
      values.csrf_token = document.querySelector('meta[name="queue-csrf"]')?.content || "";
    }
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: new URLSearchParams(values),
    });
    if (!response.ok) {
      const error = await response.json().catch(()=>({}));
      throw new Error(error.error || "Не удалось сохранить изменение");
    }
    return response;
  }

  return {queueAlert, queueConfirm, queuePrompt, postForm};
})();
