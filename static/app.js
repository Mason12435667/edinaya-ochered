// Persistent media transport: one player per page, independent of chat rendering.
window.QueueVoice = (() => {
  const audio = new Audio(); audio.preload = "metadata";
  let panel, source, seek, clock, playButton;
  const format = n => `${Math.floor((n || 0)/60)}:${String(Math.floor((n || 0)%60)).padStart(2,"0")}`;
  function ensure() {
    if (panel) return;
    panel = document.createElement("section"); panel.className = "queue-voice-dock"; panel.setAttribute("aria-label", "Голосовое сообщение");
    const make = (text, action) => { const b=document.createElement("button"); b.type="button"; b.textContent=text; b.onclick=action; panel.append(b); return b; };
    playButton=make("Пауза", () => audio.paused ? audio.play().catch(fail) : audio.pause());
    make("Стоп", () => { audio.pause(); audio.currentTime=0; });
    seek=document.createElement("input"); seek.type="range"; seek.min="0"; seek.max="100"; seek.value="0"; seek.step="0.1"; seek.setAttribute("aria-label","Позиция воспроизведения");
    seek.oninput=()=>{if(Number.isFinite(audio.duration)) audio.currentTime=Number(seek.value)/100*audio.duration;}; panel.append(seek);
    clock=document.createElement("span"); panel.append(clock);
    const speed=document.createElement("select"); speed.setAttribute("aria-label","Скорость воспроизведения");
    [1,1.5,2].forEach(v=>{const o=document.createElement("option"); o.value=v;o.textContent=v+"×";speed.append(o);}); speed.onchange=()=>audio.playbackRate=Number(speed.value); panel.append(speed);
    source=document.createElement("a");source.textContent="К сообщению";panel.append(source);
    make("Закрыть",()=>{audio.pause();panel.hidden=true;});document.body.append(panel);
  }
  function fail(){ if(clock) clock.textContent="Аудио недоступно"; }
  audio.addEventListener("error",fail);
  ["play","pause","timeupdate","loadedmetadata","ended"].forEach(e=>audio.addEventListener(e,()=>{
    if(!panel)return;playButton.textContent=audio.paused?"Продолжить":"Пауза";
    clock.textContent=format(audio.currentTime)+" / "+format(Number.isFinite(audio.duration)?audio.duration:0);
    seek.value=Number.isFinite(audio.duration)&&audio.duration>0?audio.currentTime/audio.duration*100:0;
  }));
  return {play(message,chat){
    ensure(); panel.hidden=false;
    document.querySelectorAll("audio,video").forEach(m=>m.pause());
    const url=new URL(message.media_url,location.origin).href;
    if(audio.src!==url){audio.src=url;audio.currentTime=0;}
    source.href=(chat.endsWith("@g.us")?"/groups":"/whatsapp")+"?chat_id="+encodeURIComponent(chat)+"&message_id="+encodeURIComponent(message.id);
    source.onclick=e=>{if(new URL(source.href).pathname===location.pathname){e.preventDefault();history.replaceState({},"",source.href);window.dispatchEvent(new Event("queue-open-message"));}};
    audio.play().catch(fail);
  }};
})();
(() => {
  const QUEUE_FRONTEND_BUILD = "1.00.6.36-reply-media-binding-fix";
  document.documentElement.dataset.queueBuild = QUEUE_FRONTEND_BUILD;
  const menu = document.getElementById("ticket-context-menu");
  const menuTitle = document.getElementById("context-ticket-title");
  const dashboard = document.querySelector("[data-dashboard]");
  const tableBody = document.getElementById("ticket-table-body");
  const refreshState = document.getElementById("auto-refresh-state");
  const pagination = document.getElementById("ticket-pagination");
  const shiftForm = document.querySelector("[data-shift-form]");
  const themeToggle = document.querySelector("[data-theme-toggle]");
  const conversationPage = document.querySelector("[data-conversation-page]");
  const autoPageRefresh = document.querySelector("[data-auto-page-refresh]");
  const notificationCenter = document.querySelector("[data-notification-center]");
  const notificationPopover = document.querySelector("[data-notification-popover]");
  const notificationCount = document.querySelector("[data-notification-count]");
  const notificationItems = document.querySelector("[data-notification-items]");
  const toastStack = document.querySelector("[data-toast-stack]");
  const dashboardFilterForm = document.querySelector("[data-dashboard-filter-form]");
  const liveTicketSearch = document.querySelector("[data-live-ticket-search]");
  const contactSearchForm = document.querySelector("[data-contact-search-form]");
  const liveContactSearch = document.querySelector("[data-live-contact-search]");
  const contactSearchResults = document.querySelector("[data-contact-search-results]");

  // В карточке заявки оставляем только рабочий приоритет. Старое поле «Важность»,
  // если оно осталось в браузере/старой разметке, больше не показываем.
  document.querySelectorAll(".detail-grid > div").forEach((item) => {
    const label = item.querySelector("span");
    if (label && label.textContent.trim() === "Важность") item.remove();
  });
  let activeTicketId = "";
  let activeEmployee = "";
  let refreshing = false;
  const adminMode = document.body && document.body.dataset.admin === "1";

  let queueRealtimeRevision = 0;
  let queueRealtimeStarted = false;
  async function queueRealtimeLoop() {
    let backoff = 250;
    while (queueRealtimeStarted) {
      try {
        const response = await fetch(`/api/realtime-wait?since=${encodeURIComponent(queueRealtimeRevision)}&timeout=25000`, {cache:"no-store"});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const state = await response.json();
        const nextRevision = Number(state.revision || 0);
        const changed = Boolean(state.changed) || nextRevision > queueRealtimeRevision;
        if (nextRevision > queueRealtimeRevision) queueRealtimeRevision = nextRevision;
        if (changed) window.dispatchEvent(new CustomEvent("queue-realtime", {detail:state}));
        backoff = 250;
        await new Promise(resolve => window.setTimeout(resolve, 20));
      } catch (_) {
        await new Promise(resolve => window.setTimeout(resolve, backoff));
        backoff = Math.min(5000, Math.round(backoff * 1.7));
      }
    }
  }
  function startQueueRealtime() {
    if (queueRealtimeStarted || !(conversationPage || dashboard || notificationCenter)) return;
    queueRealtimeStarted = true;
    void queueRealtimeLoop();
  }

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

  function closeMenu() {
    if (menu) menu.hidden = true;
    activeTicketId = "";
    activeEmployee = "";
  }

  function openMenu(event, row) {
    if (!menu || !menuTitle) return;
    const currentStatus = row.dataset.ticketStatus || "";
    if (!adminMode && (currentStatus === "done" || currentStatus === "invalid")) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    activeTicketId = row.dataset.ticketId;
    activeEmployee = row.dataset.assignedTo || "";
    menuTitle.textContent = `#${activeTicketId} · ${row.dataset.ticketTitle}`;
    menu.hidden = false;

    const margin = 10;
    const bounds = menu.getBoundingClientRect();
    const left = Math.min(event.clientX, window.innerWidth - bounds.width - margin);
    const top = Math.min(event.clientY, window.innerHeight - bounds.height - margin);
    menu.style.left = `${Math.max(margin, left)}px`;
    menu.style.top = `${Math.max(margin, top)}px`;
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

  function bindRows() {
    document.querySelectorAll("[data-ticket-row]:not([data-row-bound])").forEach((row) => {
      row.dataset.rowBound = "1";
      row.addEventListener("contextmenu", (event) => openMenu(event, row));
    });

    document.querySelectorAll("[data-quick-priority]:not([data-priority-bound])").forEach((select) => {
      select.dataset.priorityBound = "1";
      select.dataset.previousValue = select.value;
      select.addEventListener("click", (event) => event.stopPropagation());
      select.addEventListener("contextmenu", (event) => event.stopPropagation());
      select.addEventListener("change", async () => {
        const previous = select.dataset.previousValue;
        select.disabled = true;
        try {
          await postForm("/quick-priority", {
            ticket_id: select.dataset.ticketId,
            priority: select.value,
          });
          select.dataset.previousValue = select.value;
          await refreshDashboard(true);
        } catch (error) {
          select.value = previous;
          queueAlert(error.message);
        } finally {
          select.disabled = false;
        }
      });
    });
  }

  async function refreshDashboard(force = false) {
    if (!dashboard || !tableBody || refreshing || document.hidden) return;
    if (menu && !menu.hidden && !force) return;
    refreshing = true;
    try {
      const response = await fetch(`/api/dashboard-data${window.location.search}`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Ошибка автообновления");
      const data = await response.json();
      if (force || data.version !== dashboard.dataset.dashboardVersion) {
        tableBody.innerHTML = data.rows_html;
        if (pagination) pagination.innerHTML = data.pagination_html || "";
        Object.entries(data.counts || {}).forEach(([status, amount]) => {
          const counter = document.querySelector(`[data-count-status="${status}"]`);
          if (counter) counter.textContent = amount;
        });
        dashboard.dataset.dashboardVersion = data.version;
        bindRows();
      }
      if (refreshState) refreshState.textContent = "Очередь обновляется автоматически";
    } catch (error) {
      if (refreshState) refreshState.textContent = "Нет связи с автообновлением";
    } finally {
      refreshing = false;
    }
  }

  if (dashboardFilterForm && liveTicketSearch) {
    let searchTimer = 0;
    dashboardFilterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const params = new URLSearchParams(new FormData(dashboardFilterForm));
      [...params.keys()].forEach((key) => { if (!params.get(key)) params.delete(key); });
      params.delete("page");
      params.delete("notice");
      const query = params.toString();
      history.replaceState(null, "", query ? `/?${query}` : "/");
      refreshDashboard(true);
    });
    liveTicketSearch.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => dashboardFilterForm.requestSubmit(), 220);
    });
  }

  if (contactSearchForm && liveContactSearch && contactSearchResults) {
    let contactTimer = 0;
    let contactRequest = 0;
    async function runContactSearch() {
      const requestId = ++contactRequest;
      liveContactSearch.classList.add("search-loading");
      try {
        const q = liveContactSearch.value.trim();
        const response = await fetch(`/api/admin/contact-search?q=${encodeURIComponent(q)}`, {cache: "no-store"});
        if (!response.ok) throw new Error("Не удалось выполнить поиск");
        const data = await response.json();
        if (requestId !== contactRequest) return;
        contactSearchResults.innerHTML = data.rows_html || "";
        const params = new URLSearchParams(window.location.search);
        if (q) params.set("q", q); else params.delete("q");
        const query = params.toString();
        history.replaceState(null, "", `/admin/contacts${query ? `?${query}` : ""}`);
      } catch (_) {
        if (requestId === contactRequest) {
          contactSearchResults.innerHTML = '<tr><td colspan="4" class="empty">Не удалось обновить поиск</td></tr>';
        }
      } finally {
        if (requestId === contactRequest) liveContactSearch.classList.remove("search-loading");
      }
    }
    contactSearchForm.addEventListener("submit", (event) => { event.preventDefault(); runContactSearch(); });
    liveContactSearch.addEventListener("input", () => {
      window.clearTimeout(contactTimer);
      contactTimer = window.setTimeout(runContactSearch, 220);
    });
  }

  if (shiftForm) {
    const select = shiftForm.querySelector("select[name='employee']");
    shiftForm.addEventListener("submit", (event) => event.preventDefault());
    if (select) {
      select.addEventListener("change", async () => {
        select.disabled = true;
        try {
          const response = await fetch("/active-employee", {
            method: "POST",
            headers: {
              "Content-Type": "application/x-www-form-urlencoded",
              "X-Requested-With": "fetch",
            },
            body: new URLSearchParams({employee: select.value}),
          });
          if (!response.ok) throw new Error("Не удалось изменить сотрудника на смене");
          window.location.reload();
        } catch (error) {
          queueAlert(error.message);
          select.disabled = false;
        }
      });
    }
  }

  function applyTheme(theme) {
    const nextTheme = theme === "pink" ? "pink" : (theme === "dark" ? "dark" : "light");
    document.documentElement.dataset.theme = nextTheme;
    localStorage.setItem("queue-theme", nextTheme);
    if (themeToggle) {
      if (nextTheme === "pink") {
        themeToggle.textContent = "Розовая тема 🌸";
        themeToggle.title = "Персональная тема назначена администратором";
        themeToggle.setAttribute("aria-label", "Розовая персональная тема");
      } else {
        themeToggle.textContent = nextTheme === "dark" ? "Светлая тема" : "Тёмная тема";
        themeToggle.removeAttribute("title");
      }
    }
    return nextTheme;
  }

  if (themeToggle) {
    applyTheme(document.documentElement.dataset.theme);
    themeToggle.addEventListener("click", () => {
      if (document.documentElement.dataset.theme === "pink") return;
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });
  }

  function formatChatTime(timestamp) {
    if (!timestamp) return "";
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone: "Asia/Almaty",
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(Number(timestamp) * 1000));
  }

  function chatFallbackName() {
    return "Пользователь WhatsApp";
  }

  function cleanWhatsAppName(value) {
    const name = String(value || "").trim();
    if (!name) return "";
    if (["система", "system", "рабочий whatsapp"].includes(name.toLocaleLowerCase("ru"))) return "";
    if (/^[+\d\s().-]+$/.test(name)) return "";
    return name;
  }

  function avatarLetters(name, chatId) {
    const words = String(name || chatFallbackName(chatId))
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    return (words.slice(0, 2).map((word) => Array.from(word)[0]).join("") || "W").toLocaleUpperCase("ru");
  }

  function startConversationPage() {
    if (!conversationPage) return;
    const chatList = conversationPage.querySelector("[data-chat-list]");
    const chatSearch = conversationPage.querySelector("[data-chat-search]");
    const chatMessages = conversationPage.querySelector("[data-chat-messages]");
    const chatTitle = conversationPage.querySelector("[data-chat-title]");
    const contactPresence = conversationPage.querySelector("[data-contact-presence]");
    const chatIdInput = conversationPage.querySelector("[data-chat-id]");
    const composer = conversationPage.querySelector("[data-chat-composer]");
    const refreshButton = conversationPage.querySelector("[data-chat-refresh]");
    const connection = document.getElementById("whatsapp-connection");
    let manualModeButton = conversationPage.querySelector("[data-manual-mode-toggle]");
    let manualModeState = conversationPage.querySelector("[data-manual-mode-state]");
    let botResetButton = conversationPage.querySelector("[data-bot-reset]");
    const profileButton = conversationPage.querySelector("[data-profile-toggle]");
    // EO_BOT_RESTART_UI_FIX_20260930:
    // This control belongs only to personal WhatsApp dialogs.
    // Never attach it to the Groups sidebar / "Обновить" button.
    const botRestartPersonalPage = location.pathname === "/whatsapp";
    const botRestartHeaderActions = conversationPage.querySelector(".chat-header-actions");
    if (!manualModeButton && botRestartPersonalPage) {
      manualModeButton = document.createElement("button");
      manualModeButton.type = "button";
      manualModeButton.className = "button compact secondary";
      manualModeButton.dataset.manualModeToggle = "1";
      if (profileButton && profileButton.parentElement) {
        profileButton.insertAdjacentElement("afterend", manualModeButton);
      } else if (botRestartHeaderActions) {
        botRestartHeaderActions.append(manualModeButton);
      }
    }
    if (!botResetButton && botRestartPersonalPage) {
      botResetButton = document.createElement("button");
      botResetButton.type = "button";
      botResetButton.className = "button compact secondary toolbar-overflow-source";
      botResetButton.dataset.botReset = "1";
      botResetButton.textContent = "↻ Перезагрузить бота";
    }
    if (!manualModeState && botRestartPersonalPage) {
      manualModeState = document.createElement("small");
      manualModeState.dataset.manualModeState = "1";
      manualModeState.className = "manual-mode-state";
      const chatHeader = conversationPage.querySelector(".chat-header");
      const titleBlock = chatTitle?.parentElement;
      if (titleBlock && chatHeader && chatHeader.contains(titleBlock)) {
        titleBlock.append(manualModeState);
      } else if (manualModeButton?.parentElement) {
        manualModeButton.parentElement.insertAdjacentElement("afterend", manualModeState);
      }
    }

    // EO_CHAT_OVERFLOW_BOT_CONTROLS_V19_20261001
    // CSP-safe version:
    // - never writes element.style (strict style-src blocks inline styles);
    // - keeps the real v15 controls hidden in their original DOM location;
    // - creates menu proxy buttons which call the REAL control's .click();
    // - removes duplicate old auto-reply rows from the existing "..." menu.
    let botManualContactV17 = false;
    let botOverflowPanelV17 = null;
    let nameAvatarSourceV17 = null;

    const normalizeActionLabelV17 = (value) => String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .toLocaleLowerCase("ru-RU");

    const isVisibleV17 = (element) => {
      if (!element || !element.isConnected) return false;
      const style = getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) === 0) return false;
      const box = element.getBoundingClientRect();
      return box.width > 4 && box.height > 4;
    };

    function findNameAvatarSourceV17() {
      if (nameAvatarSourceV17 && nameAvatarSourceV17.isConnected) return nameAvatarSourceV17;
      const scope = botRestartHeaderActions || document;
      const found = [...scope.querySelectorAll("button, a, [role='button']")].find((item) => {
        if (item === manualModeButton || item === botResetButton) return false;
        if (item.dataset && item.dataset.eoOverflowProxyV19 === "1") return false;
        const label = normalizeActionLabelV17(item.textContent || item.getAttribute("aria-label") || item.title);
        return label.includes("имя") && label.includes("аватар");
      }) || null;
      if (found) nameAvatarSourceV17 = found;
      return found;
    }

    function parkHeaderBotControlsV17() {
      findNameAvatarSourceV17();
      // "hidden" is CSP-safe; unlike element.style it does not create an
      // inline style declaration.
      for (const button of [manualModeButton, botResetButton, nameAvatarSourceV17]) {
        if (!button) continue;
        button.classList.remove("toolbar-overflow-source", "bot-reload-visible");
        button.hidden = true;
      }
    }

    function panelLabelScoreV18(element) {
      const text = normalizeActionLabelV17(element && element.textContent);
      if (!text) return 0;
      const labels = ["теги / заметка", "экспорт", "обращения", "заявка"];
      return labels.reduce((score, label) => score + (text.includes(label) ? 1 : 0), 0);
    }

    function findExistingOverflowPanelV17() {
      const selectors = [
        "[role='menu']", "menu", "nav", "aside", "section", "details", "div"
      ].join(",");
      const candidates = [];
      for (const node of document.querySelectorAll(selectors)) {
        if (!isVisibleV17(node)) continue;
        const score = panelLabelScoreV18(node);
        if (score < 3) continue;
        const rect = node.getBoundingClientRect();
        if (rect.width < 120 || rect.width > 560 || rect.height < 80 || rect.height > 900) continue;
        candidates.push({node, score, area: rect.width * rect.height});
      }
      candidates.sort((a,b) => (b.score - a.score) || (a.area - b.area));
      return candidates[0]?.node || null;
    }

    function menuButtonClassV19(panel) {
      const sample = [...panel.querySelectorAll("button, [role='button'], a")].find((item) =>
        item !== manualModeButton &&
        item !== botResetButton &&
        item !== nameAvatarSourceV17 &&
        item.dataset?.eoOverflowProxyV19 !== "1"
      );
      return sample && sample.className ? String(sample.className) : "button compact";
    }

    function removeDuplicateBotRowsV19(panel) {
      const duplicateLabels = new Set([
        "выключить автоответчик",
        "включить автоответчик",
        "отключить автоответы",
        "включить автоответы",
        "⏸ отключить автоответы",
        "▶ включить автоответы",
        "↻ перезагрузить бота",
        "перезагрузить бота",
        "↻ имя / аватар",
        "имя / аватар",
      ]);
      [...panel.querySelectorAll("button, a, [role='button']")].forEach((item) => {
        if (item.dataset?.eoOverflowProxyV19 === "1") return;
        const label = normalizeActionLabelV17(item.textContent || item.getAttribute("aria-label") || item.title);
        if (duplicateLabels.has(label)) item.remove();
      });
    }

    function createProxyV19(panel, key, label, source) {
      if (!panel || !source) return null;
      let proxy = panel.querySelector(`[data-eo-overflow-proxy-v19="${key}"]`);
      if (!proxy) {
        proxy = document.createElement("button");
        proxy.type = "button";
        proxy.dataset.eoOverflowProxyV19 = key;
        proxy.className = menuButtonClassV19(panel);
        proxy.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();

          const activeSource =
            key === "manual" ? manualModeButton :
            key === "reset" ? botResetButton :
            findNameAvatarSourceV17();

          if (!activeSource) {
            window.QueueUI?.toast?.("Действие пока недоступно");
            return;
          }
          if (activeSource.disabled) {
            window.QueueUI?.toast?.("Сначала выберите пользователя");
            return;
          }

          // HTMLElement.click() invokes the already-tested v15 handler even
          // when the source control itself is hidden.
          activeSource.click();

          // Close the current overflow popover after the action.
          const moreButton = [...document.querySelectorAll("button, [role='button'], summary")].find((item) => {
            const text = normalizeActionLabelV17(item.textContent || item.getAttribute("aria-label") || item.title);
            return text === "..." || text === "…";
          });
          if (moreButton) window.setTimeout(() => moreButton.click(), 0);
        });
        panel.append(proxy);
      }
      proxy.textContent = label;
      proxy.disabled = !selectedChatId || Boolean(source.disabled);
      return proxy;
    }

    function syncOverflowBotActionsV17() {
      // Remove old broken v17 proxies and any stale v19 proxies which ended up
      // outside the currently visible panel.
      document.querySelectorAll("[data-eo-overflow-proxy-v17]").forEach((item) => item.remove());

      const panel = findExistingOverflowPanelV17();
      if (!panel) {
        parkHeaderBotControlsV17();
        return false;
      }
      botOverflowPanelV17 = panel;

      const allowed = Boolean(selectedChatId) &&
        !String(selectedChatId).endsWith("@g.us") &&
        !botManualContactV17;

      if (!allowed) {
        panel.querySelectorAll("[data-eo-overflow-proxy-v19]").forEach((item) => item.remove());
        parkHeaderBotControlsV17();
        return true;
      }

      parkHeaderBotControlsV17();
      removeDuplicateBotRowsV19(panel);

      createProxyV19(
        panel,
        "manual",
        manualModeActive ? "▶ Включить автоответы" : "⏸ Отключить автоответы",
        manualModeButton
      );
      createProxyV19(panel, "reset", "↻ Перезагрузить бота", botResetButton);

      const avatarAction = findNameAvatarSourceV17();
      if (avatarAction) createProxyV19(panel, "avatar", "↻ Имя / аватар", avatarAction);

      return true;
    }

    function scheduleOverflowSyncV17() {
      [0, 25, 70, 150, 300, 600].forEach((delay) => window.setTimeout(syncOverflowBotActionsV17, delay));
    }

    if (botRestartPersonalPage) {
      const titleBlock = chatTitle?.parentElement;
      if (manualModeState && titleBlock && manualModeState.parentElement !== titleBlock) titleBlock.append(manualModeState);
      parkHeaderBotControlsV17();

      document.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("button, summary, a, [role='button']") : null;
        if (!target) return;
        const label = normalizeActionLabelV17(target.textContent || target.getAttribute("aria-label") || target.title);
        if (label === "..." || label === "…" || label.includes("ещё") || label.includes("дополн")) {
          scheduleOverflowSyncV17();
        }
      }, true);

      if (!document.documentElement.dataset.botOverflowObserverV19) {
        document.documentElement.dataset.botOverflowObserverV19 = "1";
        let observerTimerV19 = 0;
        const observerV19 = new MutationObserver(() => {
          window.clearTimeout(observerTimerV19);
          observerTimerV19 = window.setTimeout(() => {
            const panel = findExistingOverflowPanelV17();
            if (panel) syncOverflowBotActionsV17();
          }, 40);
        });
        observerV19.observe(document.body, {
          childList: true,
          subtree: true,
          attributes: true,
          attributeFilter: ["hidden", "class", "aria-expanded"]
        });
      }
    }
    const profileSummary = conversationPage.querySelector("[data-contact-profile-summary]");
    const participantsList = conversationPage.querySelector("[data-group-participants]");
    const participantsCount = conversationPage.querySelector("[data-participants-count]");
    const groupMuteButton = conversationPage.querySelector("[data-group-mute-toggle]");
    const mentionsInput = conversationPage.querySelector("[data-mentions-input]");
    const replyInput = conversationPage.querySelector("[data-reply-to]");
    const replyPreview = conversationPage.querySelector("[data-reply-preview]");
    const replySender = conversationPage.querySelector("[data-reply-sender]");
    const replyBody = conversationPage.querySelector("[data-reply-body]");
    const replyCancel = conversationPage.querySelector("[data-reply-cancel]");
    const mediaInput = conversationPage.querySelector("[data-media-input]");
    const mediaName = conversationPage.querySelector("[data-media-name]");
    const emojiToggle = conversationPage.querySelector("[data-emoji-toggle]");
    const emojiPicker = conversationPage.querySelector("[data-emoji-picker]");
    let forwardToolbar = conversationPage.querySelector("[data-forward-toolbar]");
    if (!forwardToolbar) forwardToolbar = document.createElement("div");
    forwardToolbar.dataset.forwardToolbar = "1";
    forwardToolbar.classList.add("chat-forward-toolbar-repair");
    // Rebuild the selection toolbar instead of reusing possibly stale or
    // template-hidden controls. Keep its location directly between messages
    // and the composer so the send controls cannot be clipped by the scroller.
    forwardToolbar.replaceChildren();
    const forwardCount = document.createElement("span");
    forwardCount.dataset.forwardCount = "1";
    const forwardTarget = document.createElement("select");
    forwardTarget.dataset.forwardTarget = "1";
    forwardTarget.setAttribute("aria-label", "Чат для пересылки");
    const forwardCancel = document.createElement("button");
    forwardCancel.type = "button";
    forwardCancel.dataset.forwardCancel = "1";
    forwardCancel.className = "button compact ghost";
    forwardCancel.textContent = "Отмена";
    const forwardSend = document.createElement("button");
    forwardSend.type = "button";
    forwardSend.dataset.forwardSend = "1";
    forwardSend.className = "button compact primary";
    forwardSend.textContent = "Переслать";
    forwardToolbar.append(forwardCount, forwardTarget, forwardCancel, forwardSend);
    if (chatMessages?.parentElement) chatMessages.insertAdjacentElement("afterend", forwardToolbar);
    else if (composer?.parentElement) composer.parentElement.insertBefore(forwardToolbar, composer);
    else conversationPage.append(forwardToolbar);
    const stateEndpoint = conversationPage.dataset.stateEndpoint || "/api/chat-state";
    const sendEndpoint = conversationPage.dataset.sendEndpoint || "/chat-send";
    const mediaSendEndpoint = conversationPage.dataset.mediaSendEndpoint || "/chat-media-send";
    const forwardEndpoint = conversationPage.dataset.forwardEndpoint || "/chat-forward";
    const emptyLabel = conversationPage.dataset.listEmpty || "Личные чаты пока не загружены";
    const selectLabel = conversationPage.dataset.selectTitle || "Выберите пользователя";
    let selectedChatId = conversationPage.dataset.initialChatId || "";
    let chats = [];
    let lastChatsSignature = "";
    let stateSequence = 0;
    let forwardTargets = [];
    let forwardOptionsSignature = "";
    let forwardBusy = false;
    let participants = [];
    let loading = false;
    let lastMessageSignature = "";
    let manualModeActive = false;
    let groupMuted = false;
    const PROFILE_OPEN_STORAGE_KEY = "queue-whatsapp-open-profile";
    let profileOpenChatId = "";
    try { profileOpenChatId = sessionStorage.getItem(PROFILE_OPEN_STORAGE_KEY) || ""; } catch (_) {}
    let profileOpen = Boolean(selectedChatId && profileOpenChatId === selectedChatId);
    let currentProfile = {};
    const selectedMentionIds = new Set();
    const selectedForForward = new Set();
    const expandedLongMessages = new Set();
    let replyToMessage = null;
    let historyCursor = "";
    let historyHasMore = false;
    let olderMessages = [];
    let latestMessages = [];
    const reactionValues = ["👍","❤️","😂","😮","😢","🙏","🔥","🎉","✅","👏","🤔","👀","😡","💯","👌","😁","😍","🥳"];
    const mentionPicker = document.createElement("div");
    mentionPicker.className = "mention-picker";
    mentionPicker.hidden = true;
    if (composer) composer.append(mentionPicker);

    function placeholder(container, text) {
      if (!container) return;
      container.replaceChildren();
      const element = document.createElement("p");
      element.className = "chat-placeholder";
      element.textContent = text;
      container.append(element);
    }

    function selectedChatName() {
      const current = chats.find((chat) => chat.id === selectedChatId);
      return current ? (cleanWhatsAppName(current.name) || chatFallbackName(selectedChatId)) : chatFallbackName(selectedChatId);
    }

    // QUEUE_LINKIFY_3_3_79: render http(s), www and plain-domain links as safe clickable anchors.
    function appendMessageText(container, text, message) {
      const value = String(text || "");
      const exactMentions = [];
      (Array.isArray(message && message.mentions) ? message.mentions : []).forEach((mention) => {
        if (!mention || typeof mention !== "object") return;
        const name = String(mention.name || "").trim();
        if (name) exactMentions.push(`@${name}`);
      });
      exactMentions.sort((a, b) => b.length - a.length);
      const escapedMentions = exactMentions.map((item) => item.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
      const mentionSource = escapedMentions.length
        ? `(?:${escapedMentions.join("|")}|@[\\p{L}\\p{N}_.+:-]+)`
        : "(?:@[\\p{L}\\p{N}_.+:-]+)";
      const urlSource = "(?:(?:https?:\\/\\/|www\\.)[^\\s<>\\\"']+|(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?\\.)+[A-Za-z]{2,24}(?:\\/[^\\s<>\\\"']*)?)";
      let matcher;
      try { matcher = new RegExp(`${mentionSource}|${urlSource}`, "giu"); }
      catch (_) { matcher = /@[A-Za-zА-Яа-яЁё0-9_.+:-]+|https?:\/\/[^\s<>"']+|www\.[^\s<>"']+/gi; }
      let cursor = 0;
      for (const match of value.matchAll(matcher)) {
        const index = Number(match.index || 0);
        if (index > cursor) container.append(document.createTextNode(value.slice(cursor, index)));
        let token = String(match[0] || "");
        const isMention = token.startsWith("@");
        const previousChar = index > 0 ? value[index - 1] : "";
        const looksLikeBareDomain = !isMention && !/^https?:\/\//i.test(token) && !/^www\./i.test(token);
        if (!isMention && looksLikeBareDomain && previousChar === "@") {
          container.append(document.createTextNode(token));
          cursor = index + token.length;
          continue;
        }
        if (isMention) {
          const mention = document.createElement("span");
          mention.className = `chat-inline-mention${message && message.highlight_mention ? " mention-highlighted" : ""}`;
          mention.textContent = token;
          container.append(mention);
          cursor = index + token.length;
          continue;
        }
        let trailing = "";
        while (token && /[.,!?;:]$/.test(token)) {
          trailing = token.slice(-1) + trailing;
          token = token.slice(0, -1);
        }
        if (!token) {
          container.append(document.createTextNode(match[0]));
          cursor = index + String(match[0]).length;
          continue;
        }
        const link = document.createElement("a");
        link.className = "chat-inline-link";
        link.textContent = token;
        link.href = /^https?:\/\//i.test(token) ? token : `https://${token}`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.title = "Открыть ссылку";
        link.addEventListener("click", (event) => event.stopPropagation());
        container.append(link);
        if (trailing) container.append(document.createTextNode(trailing));
        cursor = index + String(match[0]).length;
      }
      if (cursor < value.length) container.append(document.createTextNode(value.slice(cursor)));
    }

    function isMentionOnlyMessage(message) {
      if (!message || message.deleted) return false;
      if (message.media_url || message.media_name || message.quoted_message_key) return false;
      const body = String(message.body || "").trim();
      if (!body || !body.includes("@")) return false;
      let rest = body;
      const tokens = [];
      (Array.isArray(message.mentions) ? message.mentions : []).forEach((mention) => {
        if (!mention || typeof mention !== "object") return;
        [mention.name, mention.id, mention.resolved_id].forEach((raw) => {
          let value = String(raw || "").trim();
          if (!value) return;
          if (value.includes("@") && !value.startsWith("@")) value = value.split("@", 1)[0];
          value = value.replace(/^@+/, "").trim();
          if (value) tokens.push(`@${value}`);
        });
      });
      [...new Set(tokens)].sort((a, b) => b.length - a.length).forEach((token) => {
        const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        rest = rest.replace(new RegExp(escaped, "giu"), "");
      });
      try { rest = rest.replace(/@[\p{L}\p{N}_.+:-]+/gu, ""); }
      catch (_) { rest = rest.replace(/@[A-Za-zА-Яа-яЁё0-9_.+:-]+/g, ""); }
      return rest.trim() === "";
    }

    function myReaction(message) {
      const rows = Array.isArray(message && message.reactions) ? message.reactions : [];
      const mine = rows.find((item) => item && item.me);
      return mine ? String(mine.emoji || "") : "";
    }

    let activeMessageContextMenu = null;

    function closeMessageContextMenu() {
      if (activeMessageContextMenu && activeMessageContextMenu.isConnected) activeMessageContextMenu.remove();
      activeMessageContextMenu = null;
    }

    function closeReactionPickers() {
      document.querySelectorAll(".message-reaction-picker").forEach((picker) => {
        const triggerId = picker.dataset.triggerId || "";
        if (triggerId) document.getElementById(triggerId)?.setAttribute("aria-expanded", "false");
        picker.remove();
      });
    }

    function clampFloatingPanel(panel, left, top, margin = 10) {
      if (!panel) return;
      panel.style.right = "auto";
      panel.style.bottom = "auto";
      panel.style.left = `${Math.max(margin, Number(left) || margin)}px`;
      panel.style.top = `${Math.max(margin, Number(top) || margin)}px`;
      const adjust = () => {
        if (!panel.isConnected) return;
        const rect = panel.getBoundingClientRect();
        let dx = 0, dy = 0;
        if (rect.left < margin) dx = margin - rect.left;
        else if (rect.right > window.innerWidth - margin) dx = (window.innerWidth - margin) - rect.right;
        if (rect.top < margin) dy = margin - rect.top;
        else if (rect.bottom > window.innerHeight - margin) dy = (window.innerHeight - margin) - rect.bottom;
        if (!dx && !dy) return;
        const scaleX = rect.width > 0 && panel.offsetWidth > 0 ? rect.width / panel.offsetWidth : 1;
        const scaleY = rect.height > 0 && panel.offsetHeight > 0 ? rect.height / panel.offsetHeight : scaleX;
        const currentLeft = Number.parseFloat(panel.style.left) || 0;
        const currentTop = Number.parseFloat(panel.style.top) || 0;
        panel.style.left = `${currentLeft + dx / (scaleX || 1)}px`;
        panel.style.top = `${currentTop + dy / (scaleY || 1)}px`;
      };
      adjust();
      requestAnimationFrame(adjust);
    }
    window.QueueClampFloatingPanel = clampFloatingPanel;

    function openReactionPicker(message, bubble, button, pointer = null) {
      const anchorRect = button?.getBoundingClientRect ? button.getBoundingClientRect() : bubble?.getBoundingClientRect();
      closeReactionPickers();
      closeMessageContextMenu();
      const picker = document.createElement("div");
      picker.className = "message-reaction-picker";
      picker.setAttribute("role", "menu");
      const triggerId = `reaction-trigger-${String(message?.id || "message").replace(/[^a-zA-Z0-9_-]/g, "-")}-${Date.now()}`;
      if (button) {
        button.id = button.id || triggerId;
        picker.dataset.triggerId = button.id;
        button.setAttribute("aria-expanded", "true");
      }
      const current = myReaction(message);
      reactionValues.forEach((emoji) => {
        const option = document.createElement("button");
        option.type = "button";
        option.textContent = emoji;
        option.title = emoji === current ? "Убрать реакцию" : `Реакция ${emoji}`;
        option.setAttribute("role", "menuitem");
        if (emoji === current) option.classList.add("selected");
        option.addEventListener("click", async (event) => {
          event.stopPropagation();
          picker.remove();
          if (button) button.setAttribute("aria-expanded", "false");
          await queueMessageAction(message, "react", emoji === current ? "" : emoji);
        });
        picker.append(option);
      });
      document.body.append(picker);

      // Position in the viewport instead of inside the message bubble. This keeps
      // the picker visible for the first/last message and prevents clipping by
      // the scrollable chat container.
      const margin = 10;
      const pickerRect = picker.getBoundingClientRect();
      const chatRect = chatMessages?.getBoundingClientRect ? chatMessages.getBoundingClientRect() : null;
      const safeTop = chatRect ? Math.max(margin, chatRect.top + margin) : margin;
      const safeBottom = chatRect ? Math.min(window.innerHeight - margin, chatRect.bottom - margin) : window.innerHeight - margin;
      let left;
      let top;
      if (pointer && Number.isFinite(pointer.clientX) && Number.isFinite(pointer.clientY)) {
        left = pointer.clientX;
        top = pointer.clientY + 8;
      } else if (anchorRect) {
        left = message?.from_me ? anchorRect.right - pickerRect.width : anchorRect.left;
        const roomAbove = anchorRect.top - safeTop;
        const roomBelow = safeBottom - anchorRect.bottom;
        const messageRows = chatMessages ? [...chatMessages.querySelectorAll("[data-message-id]")] : [];
        const isFirstMessage = Boolean(bubble?.parentElement && messageRows[0] === bubble.parentElement);
        // The first message should open downward like WhatsApp instead of
        // covering the chat header or being visually clipped at the top.
        if (isFirstMessage && roomBelow > 56) top = anchorRect.bottom + 8;
        else if (roomAbove >= pickerRect.height || roomAbove > roomBelow) top = anchorRect.top - pickerRect.height - 8;
        else top = anchorRect.bottom + 8;
      } else {
        left = margin;
        top = safeTop;
      }
      clampFloatingPanel(picker, left, top, margin);
      if (chatRect && chatRect.height >= pickerRect.height + margin * 2) {
        const minTop = Math.max(margin, chatRect.top + margin);
        const maxTop = Math.min(window.innerHeight - pickerRect.height - margin, chatRect.bottom - pickerRect.height - margin);
        const currentTop = Number.parseFloat(picker.style.top || String(top));
        picker.style.top = `${Math.min(Math.max(minTop, currentTop), Math.max(minTop, maxTop))}px`;
      }

      window.setTimeout(() => {
        const close = (event) => {
          if (picker.isConnected && !picker.contains(event.target) && event.target !== button) {
            picker.remove();
            if (button) button.setAttribute("aria-expanded", "false");
          }
          document.removeEventListener("click", close, true);
        };
        document.addEventListener("click", close, true);
      }, 0);
    }

    function openMessageContextMenu(event, message, bubble) {
      if (!message || !message.id || message.deleted) return;
      event.preventDefault();
      event.stopPropagation();
      closeReactionPickers();
      closeMessageContextMenu();

      const menu = document.createElement("div");
      menu.className = "message-context-menu";
      menu.setAttribute("role", "menu");
      activeMessageContextMenu = menu;

      const addItem = (label, icon, action, danger = false, disabled = false) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = `message-context-item${danger ? " danger" : ""}`;
        item.setAttribute("role", "menuitem");
        item.disabled = Boolean(disabled);
        const iconEl = document.createElement("span");
        iconEl.className = "message-context-icon";
        iconEl.textContent = icon;
        const labelEl = document.createElement("span");
        labelEl.textContent = label;
        item.append(iconEl, labelEl);
        item.addEventListener("click", (clickEvent) => {
          clickEvent.stopPropagation();
          action(item, clickEvent);
        });
        menu.append(item);
        return item;
      };

      addItem("Реакция", "☺", (item) => openReactionPicker(message, bubble, item));
      addItem("Ответить", "↩", () => { closeMessageContextMenu(); startReply(message); });
      addItem(messageIsBookmarked(selectedChatId, message.id) ? "Убрать из избранного" : "В избранное", "★",
        () => { closeMessageContextMenu(); toggleMessageBookmark(message).catch((err) => queueAlert(err.message)); });
      const forwardBlocked = isMentionOnlyMessage(message) && !selectedForForward.has(message.id);
      const forwardItem = addItem(
        selectedForForward.has(message.id) ? "Убрать из пересылки" : "Переслать",
        "↪",
        () => { closeMessageContextMenu(); toggleForward(message); },
        false,
        forwardBlocked,
      );
      if (forwardBlocked) forwardItem.title = "Нельзя пересылать сообщение, состоящее только из тега пользователя";
      if (message.from_me) {
        const separator = document.createElement("div");
        separator.className = "message-context-separator";
        menu.append(separator);
        addItem("Изменить", "✎", () => { closeMessageContextMenu(); queueMessageAction(message, "edit"); });
        addItem("Удалить", "⌫", () => { closeMessageContextMenu(); queueMessageAction(message, "delete"); }, true);
      }

      document.body.append(menu);
      clampFloatingPanel(menu, event.clientX + 2, event.clientY + 2, 8);
      menu.querySelector("button")?.focus({preventScroll: true});

      window.setTimeout(() => {
        const close = (clickEvent) => {
          if (menu.isConnected && !menu.contains(clickEvent.target)) closeMessageContextMenu();
          document.removeEventListener("click", close, true);
        };
        document.addEventListener("click", close, true);
      }, 0);
    }

    function selectChat(chatId) {
      if (composer.dataset.sending === "1") return;
      try { sessionStorage.removeItem(`queue-manual-unread:${chatId}`); } catch (_) {}
      saveDraft();
      const changed = selectedChatId !== chatId;
      if (changed && selectedChatId) void window.QueueConversationLock?.releaseIfOwned?.(selectedChatId);
      selectedChatId = chatId;
      if (changed) {
        profileOpen = false;
        profileOpenChatId = "";
        try { sessionStorage.removeItem(PROFILE_OPEN_STORAGE_KEY); } catch (_) {}
        currentProfile = {};
        if (profileSummary) { profileSummary.hidden = true; profileSummary.replaceChildren(); }
        selectedMentionIds.clear();
        selectedForForward.clear();
        replyToMessage = null;
        if (replyInput) replyInput.value = "";
        if (replyPreview) replyPreview.hidden = true;
        if (mentionsInput) mentionsInput.value = "";
        historyCursor = "";
        historyHasMore = false;
        olderMessages = [];
        latestMessages = [];
        clearAttachment();
        restoreDraft();
      }
      lastMessageSignature = "";
      updateForwardToolbar();
      chatIdInput.value = chatId;
      chatTitle.textContent = selectedChatName();
      const url = new URL(window.location.href);
      url.searchParams.set("chat_id", chatId);
      url.searchParams.delete("message_id");
      delete conversationPage.dataset.jumpLoaded;
      delete conversationPage.dataset.jumpScrolled;
      window.history.replaceState({}, "", url);
      renderChats();
      if (changed) placeholder(chatMessages, "Подгружаю последние сообщения...");
      loadChatState(true);
    }

    const supportsReplyQueue = false; // 1.00.6.31 fix4: obsolete reply queue removed
    let needsReplyOnly = supportsReplyQueue && sessionStorage.getItem("queue-needs-reply:" + location.pathname) === "1";
    const needsReplyButton = document.createElement("button");
    needsReplyButton.type = "button"; needsReplyButton.className = "button compact ghost reply-queue-toggle";
    if (supportsReplyQueue) {
      chatSearch.after(needsReplyButton);
      chatSearch.closest(".chat-sidebar")?.classList.add("has-reply-queue");
    }
    needsReplyButton.addEventListener("click", () => {
      if (!supportsReplyQueue) return;
      needsReplyOnly = !needsReplyOnly;
      sessionStorage.setItem("queue-needs-reply:" + location.pathname, needsReplyOnly ? "1" : "0");
      lastChatsSignature = ""; renderChats();
    });
    function replyWaitTimestamp(chat) {
      if (!chat || String(chat.id || "").endsWith("@g.us") || !chat.needs_reply) return 0;
      const value = Number(chat.waiting_since || 0);
      return Number.isFinite(value) && value > 0 ? value : 0;
    }
    // Saved messages are per logged-in employee and persisted in app_settings.
    let messageBookmarks = [];
    const bookmarkKey = (chatId, messageId) => `${String(chatId || '')}\u0001${String(messageId || '')}`;
    function messageIsBookmarked(chatId, messageId) {
      return messageBookmarks.some((item) => bookmarkKey(item.chat_id, item.message_id) === bookmarkKey(chatId, messageId));
    }
    async function refreshMessageBookmarks(refreshMessages = false) {
      const response = await fetch('/api/message-bookmarks', {cache: 'no-store'});
      if (!response.ok) throw new Error('Не удалось загрузить избранные сообщения');
      const data = await response.json();
      messageBookmarks = Array.isArray(data.bookmarks) ? data.bookmarks : [];
      if (refreshMessages && selectedChatId) {
        lastMessageSignature = '';
        renderMessages(mergedConversationMessages());
      }
      return messageBookmarks;
    }
    async function toggleMessageBookmark(message) {
      if (!message?.id || !selectedChatId) return;
      const newValue = !messageIsBookmarked(selectedChatId, message.id);
      const body = new URLSearchParams({
        chat_id: selectedChatId, message_id: String(message.id), favorite: newValue ? '1' : '0',
        preview: String(message.body || message.media_name || '[Вложение]').slice(0, 280),
        sender: String(message.sender || (message.from_me ? 'Вы' : '')).slice(0, 100),
        chat_name: selectedChatName(), timestamp: String(message.timestamp || 0),
      });
      const response = await fetch('/api/message-bookmarks', {
        method: 'POST', headers: {'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'}, body,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.updated) throw new Error(data.error || 'Не удалось изменить закладку');
      messageBookmarks = Array.isArray(data.bookmarks) ? data.bookmarks : [];
      lastMessageSignature = '';
      renderMessages(mergedConversationMessages());
      window.QueueUI?.toast?.(newValue ? 'Сообщение сохранено в избранное' : 'Сообщение удалено из избранного');
    }
    async function showMessageBookmarks() {
      try { await refreshMessageBookmarks(); }
      catch (error) { window.QueueUI?.toast?.(error.message); return; }
      const overlay = document.createElement('div');
      overlay.className = 'queue-bookmarks-overlay';
      const panel = document.createElement('section');
      panel.className = 'queue-bookmarks-panel';
      panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true');
      panel.setAttribute('aria-label', 'Избранные сообщения');
      const header = document.createElement('header');
      const heading = document.createElement('h2'); heading.textContent = `★ Избранные сообщения (${messageBookmarks.length})`;
      const close = document.createElement('button'); close.type = 'button'; close.className = 'button compact ghost'; close.textContent = 'Закрыть';
      const onKey = (event) => { if (event.key === 'Escape') dismiss(); };
      const dismiss = () => { overlay.remove(); document.removeEventListener('keydown', onKey); };
      close.addEventListener('click', dismiss); header.append(heading, close); panel.append(header);
      const list = document.createElement('div'); list.className = 'queue-bookmarks-list';
      if (!messageBookmarks.length) {
        const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = 'Пока нет сохранённых сообщений.'; list.append(empty);
      }
      for (const item of messageBookmarks) {
        const link = document.createElement('a'); link.className = 'queue-bookmark-entry';
        const isGroup = String(item.chat_id || '').endsWith('@g.us');
        const query = new URLSearchParams({chat_id: String(item.chat_id), message_id: String(item.message_id)});
        if (item.timestamp) query.set('before_ts', String(item.timestamp));
        link.href = (isGroup ? '/groups?' : '/whatsapp?') + query;
        const name = document.createElement('strong'); name.textContent = String(item.chat_name || item.chat_id || 'Чат');
        const preview = document.createElement('span'); preview.textContent = `${item.sender ? item.sender + ': ' : ''}${item.preview || '[Сообщение]'}`;
        link.append(name, preview); list.append(link);
      }
      panel.append(list); overlay.append(panel);
      overlay.addEventListener('click', (event) => { if (event.target === overlay) dismiss(); });
      document.addEventListener('keydown', onKey); document.body.append(overlay); close.focus();
    }
    const sidebarHead = conversationPage.querySelector('.chat-sidebar-head');
    if (sidebarHead && !sidebarHead.querySelector('[data-open-message-bookmarks]')) {
      const openBookmarks = document.createElement('button');
      openBookmarks.type = 'button'; openBookmarks.className = 'button compact ghost';
      openBookmarks.dataset.openMessageBookmarks = '1'; openBookmarks.textContent = '★ Закладки';
      openBookmarks.addEventListener('click', showMessageBookmarks); sidebarHead.append(openBookmarks);
    }
    refreshMessageBookmarks(true).catch(() => {});
    if (new URLSearchParams(location.search).get('bookmarks') === '1') {
      showMessageBookmarks();
    }

    // QUEUE_FAVORITES_3_3_79
    async function toggleChatFavorite(chat, nextFavorite) {
      if (!chat || !chat.id) return;
      const body = new URLSearchParams({chat_id: String(chat.id), favorite: nextFavorite ? "1" : "0"});
      const response = await fetch("/chat-favorite", {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        body,
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.updated) throw new Error("Не удалось изменить избранное");
      chat.favorite = Boolean(result.favorite);
      lastChatsSignature = "";
      renderChats();
      window.QueueUI?.toast?.(chat.favorite ? "Чат добавлен в избранное" : "Чат убран из избранного");
    }

    function renderChats() {
      const waitingChats = chats.filter(c => replyWaitTimestamp(c) > 0);
      needsReplyButton.textContent = `Нужен ответ (${waitingChats.length})`;
      needsReplyButton.setAttribute("aria-pressed", String(needsReplyOnly));
      const query = String(chatSearch.value || "").trim().toLocaleLowerCase("ru");
      let filtered = chats.filter((chat) => {
        const haystack = `${chat.name || ""} ${chat.id || ""} ${chat.last_message || ""}`.toLocaleLowerCase("ru");
        return (!needsReplyOnly || chat.needs_reply) && (!query || haystack.includes(query));
      });
      // Starred chats stay at the top, recent chats follow in timestamp order.
      filtered.sort((left, right) => Number(Boolean(right.favorite)) - Number(Boolean(left.favorite))
        || Number(right.timestamp || 0) - Number(left.timestamp || 0));
      const chatsSignature = selectedChatId + query + needsReplyOnly + JSON.stringify(filtered);
      if (lastChatsSignature === chatsSignature) return;
      lastChatsSignature = chatsSignature;
      chatList.replaceChildren();
      if (!filtered.length) {
        placeholder(chatList, query ? "Ничего не найдено" : emptyLabel);
        return;
      }
      filtered.forEach((chat) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `chat-list-item${chat.id === selectedChatId ? " active" : ""}`;
        button.addEventListener("click", () => selectChat(chat.id));

        const avatar = document.createElement("span");
        avatar.className = `chat-avatar${String(chat.id || "").endsWith("@g.us") ? " group" : ""}`;
        avatar.textContent = avatarLetters(chat.name, chat.id);
        if (chat.avatar_url) {
          const img = document.createElement("img");
          img.src = chat.avatar_url; img.alt = ""; img.loading = "lazy";
          img.addEventListener("error", () => img.remove());
          avatar.append(img);
        }

        const content = document.createElement("span");
        content.className = "chat-list-content";
        const top = document.createElement("span");
        top.className = "chat-list-top";
        const name = document.createElement("strong");
        name.textContent = cleanWhatsAppName(chat.name) || chatFallbackName(chat.id);
        const favoriteToggle = document.createElement("span");
        favoriteToggle.className = `chat-favorite-toggle${chat.favorite ? " active" : ""}`;
        favoriteToggle.textContent = chat.favorite ? "★" : "☆";
        favoriteToggle.setAttribute("role", "button");
        favoriteToggle.tabIndex = 0;
        favoriteToggle.title = chat.favorite ? "Убрать из избранного" : "Добавить в избранное";
        favoriteToggle.setAttribute("aria-label", favoriteToggle.title);
        const changeFavorite = async (event) => {
          event.preventDefault();
          event.stopPropagation();
          if (favoriteToggle.dataset.busy === "1") return;
          favoriteToggle.dataset.busy = "1";
          try { await toggleChatFavorite(chat, !Boolean(chat.favorite)); }
          catch (error) { window.QueueUI?.toast?.(error?.message || "Не удалось изменить избранное"); }
          finally { favoriteToggle.dataset.busy = "0"; }
        };
        favoriteToggle.addEventListener("pointerdown", (event) => event.stopPropagation());
        favoriteToggle.addEventListener("click", changeFavorite);
        favoriteToggle.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") changeFavorite(event);
        });
        if (String(chat.id || "").endsWith("@g.us") && chat.muted) {
          const mutedMark = document.createElement("span");
          mutedMark.className = "group-muted-mark";
          mutedMark.textContent = " 🔕";
          mutedMark.title = "Уведомления этой группы заглушены";
          name.append(mutedMark);
        }
        const presence = chat && chat.presence && typeof chat.presence === "object" ? chat.presence : {};
        if (!String(chat.id || "").endsWith("@g.us") && presence.known) {
          const dot = document.createElement("i");
          dot.className = `presence-dot ${presence.online ? "online" : "offline"}`;
          dot.title = presence.online ? "Онлайн" : "Не в сети";
          name.append(" ", dot);
        }
        const time = document.createElement("time");
        time.textContent = formatChatTime(chat.timestamp);
        top.append(name, favoriteToggle, time);
        const bottom = document.createElement("span");
        bottom.className = "chat-list-bottom";
        const preview = document.createElement("span");
        const previewText = chat.last_message || "Нет текстовых сообщений";
        preview.textContent = chat.last_from_me ? `Вы: ${previewText}` : (String(chat.id).endsWith("@g.us") && chat.last_sender ? `${chat.last_sender}: ${previewText}` : previewText);
        if (!chat.last_from_me && String(chat.id).endsWith("@g.us") && chat.last_sender_avatar_url) {
          const senderPic = document.createElement("img"); senderPic.className = "preview-author-avatar";
          senderPic.src = chat.last_sender_avatar_url; senderPic.alt = ""; senderPic.loading = "lazy";
          senderPic.addEventListener("error", () => senderPic.remove()); bottom.append(senderPic);
        }
        if (chat.last_from_me) {
          preview.classList.add("chat-preview-outgoing");
          const ack = Number(chat.last_ack || 0);
          const ticks = document.createElement("span");
          ticks.className = `chat-preview-ack${ack >= 3 ? " read" : ack <= 0 ? " pending" : ""}`;
          ticks.textContent = ack >= 2 ? "✓✓" : ack >= 1 ? "✓" : "◷";
          ticks.title = ack >= 3 ? "Прочитано" : ack >= 2 ? "Доставлено" : ack >= 1 ? "Отправлено" : "Отправляется";
          bottom.append(ticks);
        }
        bottom.append(preview);
        if (Number(chat.unread_count) > 0) {
          const unread = document.createElement("b");
          const isGroupMention = String(chat.id || "").endsWith("@g.us") && Boolean(chat.mentioned !== false);
          unread.className = isGroupMention ? "chat-mention" : "chat-unread";
          unread.textContent = isGroupMention ? "@" : String(chat.unread_count);
          if (isGroupMention) unread.title = "Вас упомянули в группе";
          bottom.append(unread);
        }
        content.append(top, bottom);
        button.append(avatar, content);
        chatList.append(button);
      });
    }

    function clearReply() {
      replyToMessage = null;
      if (replyInput) replyInput.value = "";
      if (replyPreview) replyPreview.hidden = true;
    }

    function startReply(message) {
      if (!message || !message.id) return;
      replyToMessage = message;
      if (replyInput) replyInput.value = message.id;
      if (replyPreview) replyPreview.hidden = false;
      if (replySender) replySender.textContent = `Ответ на сообщение · ${message.from_me ? "Вы" : (message.sender || selectedChatName())}`;
      if (replyBody) replyBody.textContent = String(message.body || message.media_name || "Вложение").slice(0, 180);
      const textarea = composer && composer.querySelector("textarea[name='message']");
      if (textarea) textarea.focus();
    }

    function updateForwardToolbar() {
      if (!forwardToolbar || !forwardCount || !forwardTarget) return;
      const count = selectedForForward.size;
      const visible = count > 0;
      forwardToolbar.hidden = !visible;
      forwardToolbar.style.setProperty("display", visible ? "flex" : "none", "important");
      forwardToolbar.style.setProperty("visibility", visible ? "visible" : "hidden", "important");
      [forwardCount, forwardTarget, forwardCancel, forwardSend].forEach((node) => {
        node.hidden = !visible;
        node.style.setProperty("display", visible ? "inline-flex" : "none", "important");
        node.style.setProperty("visibility", visible ? "visible" : "hidden", "important");
        node.style.setProperty("opacity", visible ? "1" : "0", "important");
      });
      forwardCount.textContent = `${count} ${count === 1 ? "сообщение выбрано" : "сообщения выбрано"}`;
      const targets = (forwardTargets.length ? forwardTargets : chats).filter(chat => String(chat?.id || '').trim());
      const optionsSignature = JSON.stringify(targets.map(chat => [String(chat.id), chat.name]));
      if (optionsSignature !== forwardOptionsSignature) {
        const previous = forwardTarget.value;
        forwardOptionsSignature = optionsSignature;
        const options = [new Option(targets.length ? 'Выберите чат для пересылки' : 'Нет доступных чатов', '')];
        targets.forEach(chat => options.push(new Option(cleanWhatsAppName(chat.name) || chatFallbackName(chat.id), String(chat.id))));
        forwardTarget.replaceChildren(...options);
        forwardTarget.value = options.some(o => o.value === previous) ? previous : '';
      }
      forwardSend.disabled = forwardBusy || !forwardTarget.value || forwardSend.dataset.lockDisabled === '1';
    }

    function toggleForward(message) {
      if (!message || !message.id) return;
      if (selectedForForward.has(message.id)) {
        selectedForForward.delete(message.id);
      } else {
        if (isMentionOnlyMessage(message)) {
          queueAlert("Нельзя пересылать сообщение, состоящее только из тега пользователя");
          return;
        }
        if (selectedForForward.size < 20) selectedForForward.add(message.id);
      }
      updateForwardToolbar();
      lastMessageSignature = "";
      loadChatState(true);
    }

    async function sendForwardSelection() {
      if (forwardBusy) return;
      if (!selectedChatId) {
        queueAlert("Сначала откройте чат с сообщением для пересылки");
        return;
      }
      if (!selectedForForward.size) {
        queueAlert("Сначала выберите сообщение для пересылки");
        return;
      }
      if (!forwardTarget || !forwardTarget.value) {
        queueAlert("Выберите чат назначения в списке перед пересылкой");
        forwardTarget?.focus();
        return;
      }
      forwardBusy = true;
      if (forwardSend) forwardSend.disabled = true;
      try {
        const response = await fetch(forwardEndpoint, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            source_chat_id: selectedChatId,
            target_chat_id: forwardTarget.value,
            message_ids: [...selectedForForward],
          }),
        });
        const result = await response.json();
        if (!response.ok || !result.queued) throw new Error(result.error || "Не удалось поставить пересылку в очередь");
        const selectedIds = [...selectedForForward];
        const actionIds = Array.isArray(result.action_ids) ? result.action_ids : [];
        if (actionIds.length) {
          const outcomes = await Promise.allSettled(actionIds.map((id) => waitForMessageAction(id)));
          const actionMessageIds = Array.isArray(result.action_message_ids) ? result.action_message_ids.map(String) : [];
          const queuedMessageIds = new Set(actionMessageIds);
          const failed = outcomes
            .map((outcome, index) => ({outcome, messageId: actionMessageIds[index] || selectedIds[index] || ""}))
            .filter(({outcome}) => outcome.status === "rejected");
          const notQueued = selectedIds.filter((id) => !queuedMessageIds.has(String(id)));
          selectedForForward.clear();
          [...failed.map((item) => item.messageId), ...notQueued].filter(Boolean).forEach((id) => selectedForForward.add(id));
          updateForwardToolbar();
          if (failed.length || notQueued.length) {
            const firstError = failed[0]?.outcome.reason;
            const detail = firstError?.message || firstError || "Сообщение отсутствует в очереди WhatsApp";
            throw new Error(`${failed.length + notQueued.length} сообщений не переслано. Ошибка: ${detail}`);
          }
        } else {
          selectedForForward.clear();
          updateForwardToolbar();
        }
        window.QueueUI?.toast?.(`Переслано сообщений: ${Number(result.count || actionIds.length || 0)}`);
      } catch (error) {
        queueAlert(error.message || "Не удалось переслать сообщения");
      } finally {
        forwardBusy = false;
        if (forwardSend) forwardSend.disabled = !forwardTarget.value || forwardSend.dataset.lockDisabled === "1";
      }
    }

    async function waitForMessageAction(actionId) {
      for (let attempt = 0; attempt < 18; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, attempt < 4 ? 500 : 900));
        const response = await fetch(`/api/chat-action-status?id=${encodeURIComponent(actionId)}`, {cache: "no-store"});
        if (!response.ok) continue;
        const result = await response.json();
        const state = result && result.action ? String(result.action.status || "") : "";
        if (state === "sent") return result.action;
        if (state === "failed") throw new Error(String(result.action.error || "WhatsApp не выполнил действие"));
      }
      throw new Error("WhatsApp не подтвердил действие вовремя. Проверьте чат и повторите");
    }

    async function queueMessageAction(message, action, actionBody = "") {
      if (!message || !message.id || !selectedChatId) return;
      let body = action === "react" ? String(actionBody || "") : "";
      if (action === "edit") {
        const editedBody = await queuePrompt("Введите новый текст сообщения", message.body || "", "Изменить сообщение");
        if (editedBody === null) return;
        body = String(editedBody || "");
        if (!body.trim() || body.trim() === String(message.body || "").trim()) return;
      } else if (action === "delete") {
        if (!(await queueConfirm("Удалить это сообщение в WhatsApp для всех? Это действие синхронизируется с WhatsApp.", "Удаление сообщения"))) return;
      }
      try {
        const response = await postForm("/chat-action", {
          chat_id: selectedChatId,
          message_id: message.id,
          action,
          message: body,
        });
        const result = await response.json();
        if (!response.ok || !result.queued || !result.action_id) throw new Error(result.error || "Действие не поставлено в очередь");
        await waitForMessageAction(result.action_id);
        lastMessageSignature = "";
        await loadChatState(true);
      } catch (error) {
        queueAlert(error.message || "Не удалось изменить сообщение");
      }
    }

    function appendMedia(bubble, message) {
      if (!message.media_url || message.deleted) return;
      const type = String(message.type || "");
      const mime = String(message.media_mime || "");
      if (type === "image" || mime.startsWith("image/")) {
        const zoom = document.createElement("button");
        zoom.type = "button";
        zoom.className = "chat-media-zoom";
        zoom.dataset.mediaZoom = message.media_url;
        zoom.dataset.mediaName = message.media_name || "Фото";
        zoom.title = "Нажмите, чтобы увеличить фото";
        const image = document.createElement("img");
        image.className = "chat-media-image";
        image.loading = "lazy";
        image.decoding = "async";
        if (mime === "image/gif" || String(message.media_name || "").toLowerCase().endsWith(".gif")) zoom.classList.add("is-gif");
        image.src = message.media_url;
        image.addEventListener("error", () => {
          image.hidden = true;
          zoom.textContent = "Фото недоступно";
        });
        image.alt = message.media_name || "Фото";
        zoom.append(image);
        bubble.append(zoom);
      } else if (["audio", "ptt"].includes(type) || mime.startsWith("audio/")) {
        const audio = document.createElement("button");
        audio.type = "button"; audio.className = "voice-source-button";
        audio.textContent = "▶ Голосовое сообщение";
        const sourceChat = selectedChatId;
        audio.addEventListener("click", () => window.QueueVoice.play(message, sourceChat));
        bubble.append(audio);
      } else if (type === "video" || mime.startsWith("video/")) {
        const video = document.createElement("video");
        video.className = "chat-media-video";
        video.controls = true;
        video.preload = "metadata";
        video.src = message.media_url;
        video.addEventListener("error", () => { video.title = "Видео недоступно или формат не поддерживается браузером"; });
        video.playsInline = true;
        video.addEventListener("play", () => {
          document.querySelectorAll("audio,video").forEach(other => { if (other !== video) other.pause(); });
        });
        bubble.append(video);
      } else {
        const box = document.createElement("span");
        box.className = "chat-media-file-box";
        const link = document.createElement("a");
        link.className = "chat-media-file";
        link.href = message.media_url;
        link.target = "_blank";
        link.textContent = message.media_name || "Открыть вложение";
        const download = document.createElement("a");
        download.className = "chat-media-download";
        download.href = `${message.media_url}&download=1`;
        download.textContent = "Скачать";
        box.append(link, download);
        bubble.append(box);
      }
    }

    function mergedConversationMessages() {
      const byId = new Map();
      [...olderMessages, ...latestMessages].forEach((message) => {
        const key = String(message && message.id || `${message.timestamp}:${message.from_me ? 1 : 0}:${message.body || ""}`);
        const previous = byId.get(key) || {};
        byId.set(key, {...previous, ...message, deleted: Boolean(previous.deleted || message.deleted)});
      });
      return [...byId.values()].sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0));
    }

    let historyLoading = false;
    async function loadOlderMessages() {
      if (historyLoading || !selectedChatId || !historyHasMore || !historyCursor) return;
      historyLoading = true;
      const historyChat = selectedChatId;
      const oldHeight = chatMessages.scrollHeight;
      const oldTop = chatMessages.scrollTop;
      try {
        const response = await fetch(`/api/chat-history?chat_id=${encodeURIComponent(selectedChatId)}&cursor=${encodeURIComponent(historyCursor)}`, {cache: "no-store"});
        if (!response.ok) throw new Error("Не удалось загрузить историю");
        const result = await response.json();
        if (historyChat !== selectedChatId) return;
        const pageMessages = Array.isArray(result.messages) ? result.messages : [];
        olderMessages = [...pageMessages, ...olderMessages];
        historyCursor = String(result.cursor || "");
        historyHasMore = Boolean(result.has_more);
        lastMessageSignature = "";
        renderMessages(mergedConversationMessages(), true, oldHeight, oldTop);
      } catch (error) {
        queueAlert(error.message || "Не удалось загрузить предыдущие сообщения");
      } finally { historyLoading = false; }
    }

    function focusMessageRow(messageId, smooth = true) {
      const wanted = String(messageId || "");
      if (!wanted || !chatMessages) return false;
      const targetRow = [...chatMessages.querySelectorAll("[data-message-id]")].find((row) => row.dataset.messageId === wanted);
      if (!targetRow) return false;
      targetRow.classList.remove("search-message-target");
      void targetRow.offsetWidth;
      targetRow.classList.add("search-message-target");
      window.setTimeout(() => targetRow.classList.remove("search-message-target"), 6000);
      targetRow.scrollIntoView({
        block: "center",
        behavior: smooth && !window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto",
      });
      return true;
    }


    function normalizeQuoteText(value) {
      return String(value || "")
        .replace(/[.…]+$/g, "")
        .replace(/\s+/g, " ")
        .trim()
        .toLocaleLowerCase("ru-RU");
    }

    function normalizedMessageKey(value) {
      return String(value || "").trim().replace(/\s+/g, "");
    }

    function messageKeysMatch(left, right) {
      const a = String(left || "").trim(), b = String(right || "").trim();
      if (!a || !b) return false;
      if (a === b) return true;
      const parse = value => { const m = /^(true|false)_([^_]+)_(.+)$/.exec(value); return m ? [m[1], m[3]] : ["", value]; };
      const [ad, aid] = parse(a), [bd, bid] = parse(b);
      return aid === bid && aid.length >= 8 && (!ad || !bd || ad === bd);
    }
    function quoteBodiesMatch() { return false; }
    function findQuotedMessageCandidate(reply, targetId) {
      const matches = mergedConversationMessages().filter(m => !m.deleted && messageKeysMatch(m.id, targetId));
      return matches.length === 1 ? matches[0] : null;
    }

    function renderMessages(messages, preserveScroll = false, oldHeight = 0, oldTop = 0) {
      // Do not destroy an audio/video player during background polling.

      // Auto-refresh must not throw the employee back to the newest message while
      // they are reading older history. If the user is already near the bottom,
      // keep following new messages like WhatsApp does.
      const anchor = [...chatMessages.querySelectorAll("[data-message-id]")].find(row => row.getBoundingClientRect().bottom > chatMessages.getBoundingClientRect().top);
      const anchorOffset = anchor ? anchor.getBoundingClientRect().top - chatMessages.getBoundingClientRect().top : 0;
      const currentTop = chatMessages.scrollTop;
      const currentHeight = chatMessages.scrollHeight;
      const currentClientHeight = chatMessages.clientHeight;
      const wasNearBottom = !new URL(location.href).searchParams.has("message_id") && currentHeight - (currentTop + currentClientHeight) <= 96;
      const signature = `${historyHasMore ? 1 : 0}:${historyCursor}|` + messages
        .map((message) => `${message.id}:${message.timestamp}:${message.ack || 0}:${message.edited ? 1 : 0}:${message.deleted ? 1 : 0}:${message.forwarded ? 1 : 0}:${message.sender || ""}:${message.sender_phone || ""}:${message.sender_avatar_url || ""}:${message.body || ""}:${message.media_url || ""}:${message.quoted_message_key || ""}:${message.quoted_sender || ""}:${message.quoted_body || ""}:${message.quote_preview_url || ""}:${message.quote_unavailable ? 1 : 0}:${message.transcript || ""}:${message.highlight_mention ? 1 : 0}:${message.media_name || ""}:${(Array.isArray(message.reactions) ? message.reactions : []).map((reaction) => `${reaction.emoji || ""},${reaction.count || 0},${reaction.me ? 1 : 0}`).join(";")}:${selectedForForward.has(message.id) ? 1 : 0}:${messageIsBookmarked(selectedChatId, message.id) ? 1 : 0}`)
        .join("|");
      if (signature === lastMessageSignature) return;
      lastMessageSignature = signature;
      const oldRows = new Map([...chatMessages.querySelectorAll("[data-message-id]")].map(row => [row.dataset.messageId, row]));
      const nodes = [];
      if (!selectedChatId) {
        placeholder(chatMessages, "Выберите чат слева, чтобы увидеть последние сообщения");
        return;
      }
      if (!messages.length) {
        placeholder(chatMessages, "В этом чате нет загруженных сообщений");
        return;
      }
      if (historyHasMore) {
        const historyBar = document.createElement("div");
        historyBar.className = "chat-history-more";
        const historyButton = document.createElement("button");
        historyButton.type = "button";
        historyButton.className = "button compact ghost";
        historyButton.textContent = "Загрузить ещё 50 сообщений";
        historyButton.addEventListener("click", loadOlderMessages);
        historyBar.append(historyButton);
        nodes.push(historyBar);
      }
      messages.forEach((message) => {
        const key = JSON.stringify(message) + selectedForForward.has(message.id) + ':' + messageIsBookmarked(selectedChatId, message.id);
        const existing = oldRows.get(String(message.id));
        if (existing && (existing.dataset.renderKey === key || (!message.deleted && [...existing.querySelectorAll("audio,video")].some(media => !media.paused && !media.ended)))) {
          nodes.push(existing);
          if (existing.dataset.renderKey !== key) lastMessageSignature = "";
          return;
        }
        const row = document.createElement("div");
        row.className = `chat-message-row ${message.from_me ? "outgoing" : "incoming"}${selectedForForward.has(message.id) ? " selected-forward" : ""}`;
        row.dataset.messageId = message.id;
        row.dataset.renderKey = key;
        if (message.highlight_mention) row.classList.add("mentioned-message");
        const bubble = document.createElement("div");
        bubble.className = "chat-bubble";
        let messageParticipant = null;
        if (!message.from_me && selectedChatId.endsWith("@g.us")) {
          const senderId = String(message.sender_id || "").trim();
          messageParticipant = participants.find((item) =>
            (senderId && [item.mention_id, item.resolved_id].map((value) => String(value || "").trim()).includes(senderId))
          ) || null;
          const senderAvatar = document.createElement("span");
          senderAvatar.className = "chat-message-author-avatar";
          senderAvatar.textContent = avatarLetters(message.sender, senderId);
          const avatarUrl = String(message.sender_avatar_url || (messageParticipant && messageParticipant.avatar_url) || "").trim();
          if (avatarUrl) {
            const img = document.createElement("img");
            img.src = avatarUrl;
            img.alt = "";
            img.loading = "lazy";
            img.addEventListener("error", () => img.remove());
            senderAvatar.append(img);
          }
          row.append(senderAvatar);
        }
        if (!message.deleted && message.id) {
          row.addEventListener("contextmenu", (event) => openMessageContextMenu(event, message, bubble));
        }
        if (!message.from_me && message.sender) {
          const sender = document.createElement("strong");
          sender.className = "chat-message-sender";
          sender.textContent = message.sender;
          bubble.append(sender);
          if (selectedChatId.endsWith("@g.us")) {
            const participant = messageParticipant || participants.find((item) => {
              const senderId = String(message.sender_id || "").trim();
              return (senderId && [item.mention_id, item.resolved_id].map((value) => String(value || "").trim()).includes(senderId));
            });
            const phoneValue = String(message.sender_phone || (participant && participant.phone) || "").trim();
            if (phoneValue) {
              const phoneLine = document.createElement("small");
              phoneLine.className = "chat-message-sender-phone";
              phoneLine.textContent = phoneValue;
              bubble.append(phoneLine);
            }
          }
        }
        if (message.forwarded && !message.deleted) {
          const forwarded = document.createElement("small");
          forwarded.className = "chat-forwarded-label";
          forwarded.textContent = "Переслано";
          bubble.append(forwarded);
        }
        if (message.quoted_message_key && !message.deleted) {
          const quote = document.createElement("div");
          quote.className = "chat-quote";
          const qSender = document.createElement("strong");
          qSender.textContent = message.quoted_sender || "Сообщение";
          const qBody = document.createElement("span");
          qBody.textContent = message.quoted_body || "Вложение";
          if (message.quote_preview_url) {
            const thumb = document.createElement("img");
            thumb.className = "quote-thumbnail"; thumb.alt = "Фото"; thumb.src = message.quote_preview_url;
            thumb.loading = "lazy"; thumb.addEventListener("error", () => thumb.remove()); quote.append(thumb);
          }
          quote.setAttribute("role", "button"); quote.tabIndex = 0;
          quote.title = "Перейти к сообщению";
          const jump = () => {
            if (message.quote_unavailable) { qBody.textContent = "Сообщение недоступно"; return; }
            const targetId = String(message.quoted_message_key || "").trim();
            const quotedBody = String(message.quoted_body || "").trim();
            const quotedSender = String(message.quoted_sender || "").trim();
            if (!targetId && !quotedBody) return;

            const candidate = findQuotedMessageCandidate(message, targetId, quotedBody, quotedSender);
            if (candidate && focusMessageRow(String(candidate.id || ""), true)) {
              const realId = String(candidate.id || "");
              conversationPage.dataset.jumpLoaded = realId;
              conversationPage.dataset.jumpResolved = realId;
              conversationPage.dataset.jumpScrolled = realId;
              const cleanUrl = new URL(window.location.href);
              ["message_id", "quoted_body", "quoted_sender", "before_ts"].forEach((key) => cleanUrl.searchParams.delete(key));
              window.history.replaceState({}, "", cleanUrl);
              return;
            }

            const url = new URL(window.location.href);
            if (targetId) url.searchParams.set("message_id", targetId); else url.searchParams.delete("message_id");
            if (quotedBody) url.searchParams.set("quoted_body", quotedBody); else url.searchParams.delete("quoted_body");
            if (quotedSender) url.searchParams.set("quoted_sender", quotedSender); else url.searchParams.delete("quoted_sender");
            if (message.timestamp) url.searchParams.set("before_ts", String(message.timestamp)); else url.searchParams.delete("before_ts");
            window.history.replaceState({}, "", url);
            delete conversationPage.dataset.jumpLoaded;
            delete conversationPage.dataset.jumpResolved;
            delete conversationPage.dataset.jumpScrolled;
            loadChatState(true);
          };
          quote.addEventListener("click", jump);
          quote.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") {event.preventDefault(); jump();} });
          quote.append(qSender, qBody);
          bubble.append(quote);
        }
        if (message.deleted) {
          const deleted = document.createElement("span");
          deleted.className = "chat-message-deleted";
          deleted.textContent = "Сообщение удалено";
          bubble.append(deleted);
        } else {
          appendMedia(bubble, message);
          const newMedia = bubble.querySelector("audio,video");
          const previousMedia = existing?.querySelector("audio,video");
          if (newMedia && previousMedia && newMedia.src === previousMedia.src) {
            newMedia.replaceWith(previousMedia.closest(".queue-player") || previousMedia);
          }
          const bodyText = String(message.body || "").trim();
          const genericMediaLabel = /^\[(?:Фото|Видео|Аудио|Голосовое сообщение|Документ|Вложение)\]$/.test(bodyText);
          if (bodyText && !(message.media_url && genericMediaLabel)) {
            const body = document.createElement("span");
            body.className = "chat-message-body";
            appendMessageText(body, bodyText, message);
            // QUEUE_LONG_MESSAGE_3_3_61: keep the full text in DOM, but collapse very large blocks by default.
            const longMessage = bodyText.length > 1800 || bodyText.split(/\r?\n/).length > 24;
            const messageKey = String(message.id || `${message.timestamp}:${bodyText.slice(0,80)}`);
            if (longMessage && !expandedLongMessages.has(messageKey)) body.classList.add("queue-long-message-collapsed");
            bubble.append(body);
            if (longMessage) {
              const toggleLong = document.createElement("button");
              toggleLong.type = "button";
              toggleLong.className = "queue-long-message-toggle";
              const syncLongState = () => {
                const expanded = expandedLongMessages.has(messageKey);
                body.classList.toggle("queue-long-message-collapsed", !expanded);
                toggleLong.textContent = expanded ? "Свернуть" : "Показать полностью";
              };
              toggleLong.addEventListener("click", () => {
                if (expandedLongMessages.has(messageKey)) expandedLongMessages.delete(messageKey);
                else expandedLongMessages.add(messageKey);
                syncLongState();
              });
              syncLongState();
              bubble.append(toggleLong);
            }
          } else if (!message.media_url && !bodyText) {
            const body = document.createElement("span");
            body.textContent = "[Сообщение]";
            bubble.append(body);
          }
        }
        if (!message.deleted && message.transcript && String(message.media_mime || "").startsWith("audio/")) {
          const details = document.createElement("details");
          details.className = "chat-file-text";
          details.open = true;
          const summary = document.createElement("summary");
          summary.textContent = "Расшифровка";
          const text = document.createElement("p");
          text.textContent = String(message.transcript || "").slice(0, 12000);
          details.append(summary, text);
          bubble.append(details);
        }
        if (!message.deleted && !message.transcript && message.media_url && String(message.media_mime || "").startsWith("audio/")) {
          const transcribe = document.createElement("button");
          transcribe.type = "button";
          transcribe.className = "button compact voice-transcribe";
          transcribe.textContent = "Расшифровать голосовое";
          const transcribeError = document.createElement("div");
          transcribeError.className = "voice-transcribe-error";
          transcribeError.hidden = true;
          transcribe.addEventListener("click", async () => {
            const voiceChat = selectedChatId;
            transcribeError.hidden = true;
            transcribeError.textContent = "";
            transcribe.disabled = true;
            transcribe.textContent = "В очереди распознавания...";
            try {
              const response = await postForm("/voice-transcribe", {
                chat_id: voiceChat, message_id: message.id,
                csrf_token: document.querySelector('meta[name="queue-csrf"]').content,
              });
              let result = await response.json();
              while (["queued", "running"].includes(result.status)) {
                await new Promise(resolve => setTimeout(resolve, 2000));
                const poll = await fetch("/api/voice-job?" + new URLSearchParams({chat_id:voiceChat,message_id:message.id}));
                if (!poll.ok) throw new Error("Не удалось проверить расшифровку");
                result = await poll.json();
                if (selectedChatId !== voiceChat) return;
              }
              if (result.status !== "done") throw new Error(result.reason || "Речь не обнаружена");
              lastMessageSignature = "";
              loadChatState(true);
            } catch (error) {
              const reason = String(error?.message || "Неизвестная ошибка распознавания");
              transcribe.textContent = "Не удалось расшифровать · повторить";
              transcribe.title = reason;
              transcribeError.textContent = `Причина: ${reason}`;
              transcribeError.hidden = false;
              transcribe.disabled = false;
            }
          });
          bubble.append(transcribe, transcribeError);
        }
        if (!message.deleted && Array.isArray(message.reactions) && message.reactions.length) {
          const reactionBar = document.createElement("div");
          reactionBar.className = "chat-reactions";
          message.reactions.forEach((reaction) => {
            if (!reaction || !reaction.emoji || Number(reaction.count || 0) <= 0) return;
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = `chat-reaction-chip${reaction.me ? " mine" : ""}`;
            chip.textContent = `${reaction.emoji}${Number(reaction.count || 0) > 1 ? ` ${reaction.count}` : ""}`;
            chip.title = reaction.me ? "Нажмите, чтобы убрать свою реакцию" : `Поставить ${reaction.emoji}`;
            chip.addEventListener("click", () => queueMessageAction(message, "react", reaction.me ? "" : reaction.emoji));
            reactionBar.append(chip);
          });
          if (reactionBar.childElementCount) bubble.append(reactionBar);
        }
        const meta = document.createElement("span");
        meta.className = "chat-message-meta";
        if (message.edited && !message.deleted) {
          const edited = document.createElement("span");
          edited.textContent = "изменено";
          meta.append(edited);
        }
        if (message.from_me) {
          const delivery = document.createElement("span");
          delivery.className = "chat-delivery";
          const ack = Number(message.ack || 0);
          delivery.textContent = ack >= 3 ? "Прочитано" : ack >= 2 ? "Доставлено" : ack >= 1 ? "Отправлено" : "Отправляется";
          meta.append(delivery);
        }
        const time = document.createElement("time");
        time.textContent = formatChatTime(message.timestamp);
        meta.append(time);
        bubble.append(meta);

        if (!message.deleted && message.id) {
          const actions = document.createElement("span");
          actions.className = "chat-message-actions";
          const reaction = document.createElement("button");
          reaction.type = "button";
          reaction.className = "chat-action-reaction";
          reaction.textContent = "Реакция";
          reaction.setAttribute("aria-expanded", "false");
          reaction.addEventListener("click", (event) => {
            event.stopPropagation();
            openReactionPicker(message, bubble, reaction);
          });
          const reply = document.createElement("button");
          reply.type = "button";
          reply.textContent = "Ответить";
          reply.addEventListener("click", () => startReply(message));
          const forward = document.createElement("button");
          forward.type = "button";
          forward.textContent = selectedForForward.has(message.id) ? "Выбрано ✓" : "Переслать";
          const forwardBlocked = isMentionOnlyMessage(message) && !selectedForForward.has(message.id);
          forward.disabled = forwardBlocked;
          if (forwardBlocked) forward.title = "Нельзя пересылать сообщение, состоящее только из тега пользователя";
          forward.addEventListener("click", () => toggleForward(message));
          const bookmark = document.createElement('button');
          bookmark.type = 'button'; bookmark.className = 'message-bookmark';
          bookmark.textContent = messageIsBookmarked(selectedChatId, message.id) ? '★ В избранном' : '☆ Избранное';
          bookmark.title = bookmark.textContent;
          bookmark.addEventListener('click', () => toggleMessageBookmark(message).catch((err) => queueAlert(err.message)));
          actions.append(reaction, reply, bookmark, forward);
          if (message.from_me) {
            const edit = document.createElement("button");
            edit.type = "button";
            edit.textContent = "Изменить";
            edit.addEventListener("click", () => queueMessageAction(message, "edit"));
            const remove = document.createElement("button");
            remove.type = "button";
            remove.textContent = "Удалить";
            remove.addEventListener("click", () => queueMessageAction(message, "delete"));
            actions.append(edit, remove);
          }
          bubble.append(actions);
        }
        row.append(bubble);
        nodes.push(row);
      });
      const keep = new Set(nodes);
      [...chatMessages.children].forEach(node => { if (!keep.has(node)) node.remove(); });
      nodes.forEach((node, index) => {
        if (chatMessages.children[index] !== node) chatMessages.insertBefore(node, chatMessages.children[index] || null);
      });
      window.QueueUI?.enhance(chatMessages);
      if (preserveScroll) {
        // Older history was inserted above the current viewport. Keep the same
        // visible messages in place instead of moving the reader.
        chatMessages.scrollTop = Math.max(0, chatMessages.scrollHeight - oldHeight + oldTop);
      } else if (wasNearBottom) {
        // The employee was following the latest messages, so continue doing so.
        chatMessages.scrollTop = chatMessages.scrollHeight;
      } else {
        // Background polling may update ack/status, quotes, names or add new
        // messages. Preserve the user's reading position in both personal chats
        // and groups instead of jumping to the bottom.
        const maxTop = Math.max(0, chatMessages.scrollHeight - chatMessages.clientHeight);
        chatMessages.scrollTop = Math.min(currentTop, maxTop);
        const restoredAnchor = anchor && [...chatMessages.querySelectorAll("[data-message-id]")].find(row => row.dataset.messageId === anchor.dataset.messageId);
        if (restoredAnchor) chatMessages.scrollTop += restoredAnchor.getBoundingClientRect().top - chatMessages.getBoundingClientRect().top - anchorOffset;
      }
    }

    function participantLabel(participant) {
      return String(participant.name || participant.phone || "Участник");
    }

    function insertMention(participant, textarea, start, end) {
      const mentionId = String(participant.mention_id || "");
      const mentionUser = mentionId.split("@")[0];
      if (!mentionUser || !textarea) return;
      selectedMentionIds.add(mentionId);
      if (mentionsInput) mentionsInput.value = [...selectedMentionIds].join(",");
      const before = textarea.value.slice(0, start);
      const after = textarea.value.slice(end);
      const replacement = `@${mentionUser} `;
      textarea.value = before + replacement + after;
      const caret = before.length + replacement.length;
      textarea.focus();
      textarea.setSelectionRange(caret, caret);
      mentionPicker.hidden = true;
    }

    function updateMentionPicker(textarea) {
      if (!textarea || !selectedChatId.endsWith("@g.us") || !participants.length) {
        mentionPicker.hidden = true;
        return;
      }
      const cursor = textarea.selectionStart ?? textarea.value.length;
      const before = textarea.value.slice(0, cursor);
      const match = before.match(/(^|\s)@([^\s@]*)$/);
      if (!match) {
        mentionPicker.hidden = true;
        return;
      }
      const query = String(match[2] || "").toLocaleLowerCase("ru");
      const start = cursor - match[2].length - 1;
      const visible = participants.filter((participant) => {
        const haystack = `${participantLabel(participant)} ${participant.phone || ""}`.toLocaleLowerCase("ru");
        return (!needsReplyOnly || chat.needs_reply) && (!query || haystack.includes(query));
      }).slice(0, 10);
      mentionPicker.replaceChildren();
      if (!visible.length) {
        mentionPicker.hidden = true;
        return;
      }
      visible.forEach((participant) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "mention-option";
        const name = document.createElement("strong");
        name.textContent = participantLabel(participant) + (participant.is_admin ? " · админ" : "");
        const phone = document.createElement("small");
        phone.textContent = participant.phone || "Номер WhatsApp пока не определён";
        option.append(name, phone);
        option.addEventListener("mousedown", (event) => {
          event.preventDefault();
          insertMention(participant, textarea, start, cursor);
        });
        mentionPicker.append(option);
      });
      mentionPicker.hidden = false;
    }

    function renderParticipants(items) {
      participants = Array.isArray(items) ? items : [];
      if (!participantsList) return;
      participantsList.replaceChildren();
      if (participantsCount) participantsCount.textContent = String(participants.length);
      if (!participants.length) {
        const note = document.createElement("span");
        note.className = "muted";
        note.textContent = selectedChatId ? "Участники загружаются..." : "Выберите группу";
        participantsList.append(note);
        return;
      }
      participants.forEach((participant) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `participant-chip${participant.is_me ? " is-me" : ""}`;
        const avatar = document.createElement("span");
        avatar.className = "participant-avatar";
        avatar.textContent = avatarLetters(participantLabel(participant), participant.resolved_id || participant.mention_id);
        if (participant.avatar_url) {
          const img = document.createElement("img");
          img.src = participant.avatar_url;
          img.alt = "";
          img.loading = "lazy";
          img.addEventListener("error", () => img.remove());
          avatar.append(img);
        }
        const details = document.createElement("span");
        details.className = "participant-details";
        const name = document.createElement("strong");
        name.textContent = `${participantLabel(participant)}${participant.is_admin ? " · админ" : ""}`;
        const presence = document.createElement("small");
        const participantPresence = participant && participant.presence && typeof participant.presence === "object" ? participant.presence : {};
        presence.className = `participant-presence ${participantPresence.known ? (participantPresence.online ? "online" : "offline") : "unknown"}`;
        presence.textContent = participantPresence.known ? (participantPresence.online ? "Онлайн" : "Не в сети") : "Статус недоступен";
        presence.title = participantPresence.known ? (participantPresence.online ? "Пользователь сейчас онлайн" : "Пользователь сейчас не отображается онлайн") : "WhatsApp не передал статус. Он может быть скрыт настройками приватности.";
        const phone = document.createElement("small");
        phone.className = "participant-phone";
        phone.textContent = participant.phone || "Номер WhatsApp пока не определён";
        phone.hidden = true;
        details.append(name, presence, phone);
        button.append(avatar, details);
        const phoneDigits = String(participant.phone || "").replace(/\D/g, "");
        const resolvedDirect = String(participant.resolved_id || "").trim();
        const mentionDirect = String(participant.mention_id || "").trim();
        const directChatId = resolvedDirect.endsWith("@c.us") ? resolvedDirect : phoneDigits ? `${phoneDigits}@c.us` : resolvedDirect || mentionDirect;
        if (!participant.is_me && directChatId && !directChatId.endsWith("@g.us")) {
          button.classList.add("can-open-dm");
          button.title = "Открыть личный чат";
          button.addEventListener("click", () => {
            window.location.href = `/whatsapp?chat_id=${encodeURIComponent(directChatId)}`;
          });
        } else {
          button.title = "Нажмите, чтобы посмотреть номер";
          button.addEventListener("click", () => { phone.hidden = !phone.hidden; });
        }
        participantsList.append(button);
      });
    }

    function renderManualMode(mode, manualContact = false) {
      const groupConversation = location.pathname === "/groups" || String(selectedChatId || "").endsWith("@g.us");
      if (groupConversation) {
        botManualContactV17 = false;
        if (manualModeButton) manualModeButton.hidden = true;
        if (botResetButton) botResetButton.hidden = true;
        if (manualModeState) manualModeState.hidden = true;
        manualModeActive = false;
        parkHeaderBotControlsV17();
        syncOverflowBotActionsV17();
        return;
      }
      if (!manualModeButton || !manualModeState) return;
      manualModeActive = Boolean(mode && typeof mode === "object" && Object.keys(mode).length);
      botManualContactV17 = Boolean(manualContact);
      if (manualContact) {
        manualModeButton.hidden = true;
        if (botResetButton) botResetButton.hidden = true;
        manualModeState.hidden = false;
        manualModeState.textContent = "Контакт из админки · автоответы отключены";
        manualModeState.className = "manual-mode-state";
        parkHeaderBotControlsV17();
        syncOverflowBotActionsV17();
        return;
      }
      manualModeButton.hidden = false;
      if (botResetButton) {
        botResetButton.hidden = false;
        botResetButton.disabled = !selectedChatId;
        botResetButton.textContent = "↻ Перезагрузить бота";
        botResetButton.title = "Очистить текущий сценарий бота только для этого пользователя и начать с главного меню.";
      }
      manualModeState.hidden = false;
      manualModeButton.disabled = !selectedChatId;
      manualModeButton.textContent = manualModeActive ? "▶ Включить автоответы" : "⏸ Отключить автоответы";
      manualModeButton.title = manualModeActive
        ? "Возобновить автоматические ответы для этого пользователя без сброса его текущего сценария."
        : "Остановить автоматические ответы только для этого пользователя. Сообщения продолжат приходить в систему.";
      const actor = String((mode && mode.actor) || "").trim();
      manualModeState.textContent = !selectedChatId
        ? ""
        : manualModeActive
          ? `Автоответы отключены${actor ? ` · ${actor}` : ""}`
          : "Автоответчик работает для обычного пользователя";
      manualModeState.className = `manual-mode-state${manualModeActive ? " active" : ""}`;
      parkHeaderBotControlsV17();
      syncOverflowBotActionsV17();
    }

    function drawProfileSummary() {
      if (!profileSummary) return;
      if (!profileOpen || profileOpenChatId !== selectedChatId) {
        profileSummary.hidden = true;
        profileSummary.replaceChildren();
        return;
      }
      profileSummary.hidden = false;
      profileSummary.replaceChildren();
      const data = currentProfile && typeof currentProfile === "object" ? currentProfile : {};
      const pic = String(data.profile_pic_url || "");
      if (pic) {
        const img = document.createElement("img");
        img.src = pic;
        img.alt = "Фото профиля";
        img.className = "contact-profile-avatar";
        profileSummary.append(img);
      }
      const details = document.createElement("span");
      details.className = "contact-profile-details";
      const name = cleanWhatsAppName(data.name) || cleanWhatsAppName(selectedChatName()) || "Пользователь WhatsApp";
      const nameLine = document.createElement("strong");
      nameLine.textContent = name;
      details.append(nameLine);

      const rawPhone = String(data.phone || "").trim() || (() => {
        const direct = String(selectedChatId || "").replace(/@(?:c\.us|lid)$/i, "").replace(/\D/g, "");
        return direct ? `+${direct}` : "";
      })();
      if (rawPhone) {
        const digits = rawPhone.replace(/\D/g, "");
        let prettyPhone = rawPhone;
        if (digits.length === 11 && digits.startsWith("7")) {
          prettyPhone = `+7 (${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7, 9)}-${digits.slice(9, 11)}`;
        } else if (digits.length === 12 && digits.startsWith("998")) {
          prettyPhone = `+998 (${digits.slice(3, 5)}) ${digits.slice(5, 8)}-${digits.slice(8, 10)}-${digits.slice(10, 12)}`;
        } else if (digits) {
          prettyPhone = `+${digits}`;
        }
        const phoneLine = document.createElement("small");
        phoneLine.className = "contact-profile-phone";
        phoneLine.textContent = prettyPhone;
        details.append(phoneLine);
      }

      const about = String(data.about || "").trim();
      if (about) {
        const aboutLine = document.createElement("small");
        aboutLine.className = "contact-profile-about";
        aboutLine.textContent = about;
        details.append(aboutLine);
      }
      profileSummary.append(details);
    }

    // QUEUE_GROUP_MUTE_3_3_73
    function renderGroupMute(state) {
      if (!groupMuteButton) return;
      const isGroup = Boolean(selectedChatId && selectedChatId.endsWith("@g.us"));
      groupMuteButton.hidden = !isGroup;
      if (!isGroup) return;
      const selected = chats.find((chat) => String(chat.id || "") === selectedChatId) || {};
      groupMuted = Boolean(state && Object.prototype.hasOwnProperty.call(state, "selected_muted") ? state.selected_muted : selected.muted);
      groupMuteButton.textContent = groupMuted ? "🔔 Включить уведомления" : "🔕 Заглушить";
      groupMuteButton.title = groupMuted
        ? "Размутить группу: реальные @упоминания снова будут давать уведомления"
        : "Заглушить группу: сообщения и счётчик останутся, уведомления пропадут";
      groupMuteButton.classList.toggle("is-muted", groupMuted);
    }

    function renderPresence(presence) {
      if (!contactPresence) return;
      const value = presence && typeof presence === "object" ? presence : {};
      if (!selectedChatId || selectedChatId.endsWith("@g.us")) {
        contactPresence.hidden = true;
        contactPresence.textContent = "";
        contactPresence.className = "contact-presence";
        return;
      }
      contactPresence.hidden = false;
      if (!value.known) {
        contactPresence.className = "contact-presence unknown";
        contactPresence.textContent = "Статус недоступен";
        contactPresence.title = "WhatsApp не передал статус. Он может быть скрыт настройками приватности.";
        return;
      }
      const presenceState = String(value.state || "").toLowerCase();
      contactPresence.className = `contact-presence ${value.online ? "online" : "offline"}${presenceState === "composing" ? " typing" : presenceState === "recording" ? " recording" : ""}`;
      contactPresence.textContent = presenceState === "composing" ? "печатает..." : presenceState === "recording" ? "записывает аудио..." : value.online ? "Онлайн" : "Не в сети";
      contactPresence.title = presenceState === "composing" ? "Пользователь набирает сообщение" : presenceState === "recording" ? "Пользователь записывает голосовое сообщение" : value.online ? "Пользователь сейчас онлайн" : "Пользователь сейчас не отображается онлайн";
    }

    function renderProfile(profile) {
      if (!profileButton || !profileSummary) return;
      currentProfile = profile && typeof profile === "object" ? profile : {};
      profileButton.disabled = !selectedChatId;
      // The button stays compact; the opened profile card shows the WhatsApp name and phone number.
      profileButton.textContent = "Профиль";
      drawProfileSummary();
    }

    if (profileButton && profileSummary) profileButton.addEventListener("click", () => {
      if (!selectedChatId) return;
      if (profileOpen && profileOpenChatId === selectedChatId) {
        profileOpen = false;
        profileOpenChatId = "";
        try { sessionStorage.removeItem(PROFILE_OPEN_STORAGE_KEY); } catch (_) {}
      } else {
        profileOpen = true;
        profileOpenChatId = selectedChatId;
        try { sessionStorage.setItem(PROFILE_OPEN_STORAGE_KEY, selectedChatId); } catch (_) {}
      }
      drawProfileSummary();
    });

    if (groupMuteButton) groupMuteButton.addEventListener("click", async () => {
      if (!selectedChatId || !selectedChatId.endsWith("@g.us") || groupMuteButton.dataset.busy === "1") return;
      groupMuteButton.dataset.busy = "1";
      groupMuteButton.disabled = true;
      try {
        const body = new URLSearchParams({chat_id: selectedChatId, muted: groupMuted ? "0" : "1"});
        const response = await fetch("/group-mute", {
          method: "POST",
          headers: {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
          body,
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || !result.updated) throw new Error("Не удалось изменить режим уведомлений");
        groupMuted = Boolean(result.muted);
        const row = chats.find((chat) => String(chat.id || "") === selectedChatId);
        if (row) row.muted = groupMuted;
        renderGroupMute({selected_muted: groupMuted});
        renderChats();
        window.QueueUI?.toast?.(groupMuted ? "Группа заглушена" : "Уведомления группы включены");
      } catch (error) {
        window.QueueUI?.toast?.(error && error.message ? error.message : "Не удалось изменить режим уведомлений");
      } finally {
        groupMuteButton.disabled = false;
        groupMuteButton.dataset.busy = "0";
      }
    });

    async function loadChatState(force = false) {
      if (loading && !force) return;
      loading = true;
      const sequence = ++stateSequence;
      const requestedChat = selectedChatId;
      try {
        let manualUnread = false;
        try { manualUnread = sessionStorage.getItem(`queue-manual-unread:${selectedChatId}`) === "1"; } catch (_) {}
        const isReading = !manualUnread && !document.hidden && document.hasFocus() && chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 96;
        const suffix = selectedChatId ? `?chat_id=${encodeURIComponent(selectedChatId)}&read=${isReading ? 1 : 0}` : "";
        const response = await fetch(`${stateEndpoint}${suffix}`, {cache: "no-store"});
        if (!response.ok) throw new Error("Нет связи с программой");
        const state = await response.json();
        if (sequence !== stateSequence || requestedChat !== selectedChatId) return;
        // Let access controls consume only the state snapshot accepted by this poll.
        window.dispatchEvent(new CustomEvent("queue-chat-state-accepted", {detail: state}));
        chats = Array.isArray(state.chats) ? state.chats : [];
        forwardTargets = Array.isArray(state.forward_targets) ? state.forward_targets : chats;
        if (!selectedChatId && state.selected_chat_id) selectedChatId = state.selected_chat_id;
        try {
          const storedProfileChatId = sessionStorage.getItem(PROFILE_OPEN_STORAGE_KEY) || "";
          if (storedProfileChatId && storedProfileChatId === selectedChatId) {
            profileOpen = true;
            profileOpenChatId = storedProfileChatId;
          }
        } catch (_) {}
        chatIdInput.value = selectedChatId;
        chatTitle.textContent = selectedChatId ? selectedChatName() : selectLabel;
        renderPresence(state.presence || (state.profile && state.profile.presence) || {});
        renderGroupMute(state);
        connection.textContent = state.connected ? "WhatsApp подключён" : "QR-коннектор не подключён";
        connection.className = `connection-badge ${state.connected ? "online" : "offline"}`;
        renderChats();
        updateForwardToolbar();
        latestMessages = Array.isArray(state.messages) ? state.messages : [];
        const jumpParams = new URLSearchParams(window.location.search);
        const jumpId = jumpParams.get("message_id") || "";
        const jumpBody = jumpParams.get("quoted_body") || "";
        const jumpSender = jumpParams.get("quoted_sender") || "";
        const jumpBeforeTs = jumpParams.get("before_ts") || "";
        const jumpKey = jumpId || (jumpBody ? `body:${jumpBody}:${jumpBeforeTs}` : "");
        let resolvedJumpId = jumpId;
        if (jumpKey && conversationPage.dataset.jumpLoaded !== jumpKey) {
          const targetQuery = new URLSearchParams({chat_id:selectedChatId});
          if (jumpId) targetQuery.set("message_id", jumpId);
          if (jumpBody) targetQuery.set("quoted_body", jumpBody);
          if (jumpSender) targetQuery.set("quoted_sender", jumpSender);
          if (jumpBeforeTs) targetQuery.set("before_ts", jumpBeforeTs);
          const target = await fetch("/api/message-window?" + targetQuery);
          conversationPage.dataset.jumpLoaded = jumpKey;
          if (target.ok) {
            const windowData = await target.json();
            if (sequence !== stateSequence || requestedChat !== selectedChatId) return;
            olderMessages = windowData.messages || [];
            historyCursor = windowData.cursor || "";
            historyHasMore = Boolean(windowData.has_more);
            resolvedJumpId = String(windowData.target_id || jumpId || "");
            conversationPage.dataset.jumpResolved = resolvedJumpId;
          } else {
            const reply = mergedConversationMessages().find((item) => {
              if (!item) return false;
              const sameQuoteId = jumpId && messageKeysMatch(item.quoted_message_key || "", jumpId);
              const sameQuoteBody = jumpBody && quoteBodiesMatch(item.quoted_body || "", jumpBody);
              return Boolean(sameQuoteId || sameQuoteBody);
            }) || null;
            const fallback = findQuotedMessageCandidate(reply || {timestamp:Number(jumpBeforeTs || 0)}, jumpId, jumpBody, jumpSender);
            if (fallback) {
              resolvedJumpId = String(fallback.id || "");
              conversationPage.dataset.jumpResolved = resolvedJumpId;
            } else {
              window.QueueUI?.toast?.("Сообщение недоступно");
            }
          }
        } else if (jumpKey) {
          resolvedJumpId = conversationPage.dataset.jumpResolved || jumpId;
        }
        if (!olderMessages.length) {
          historyCursor = String(state.history_cursor || "");
          historyHasMore = Boolean(state.history_has_more);
        }
        renderParticipants(state.participants || []);
        renderMessages(mergedConversationMessages());
        const jumpScrollId = resolvedJumpId || jumpId;
        if (jumpScrollId && conversationPage.dataset.jumpScrolled !== jumpScrollId) {
          if (focusMessageRow(jumpScrollId, true)) {
            conversationPage.dataset.jumpScrolled = jumpScrollId;
            const cleanUrl = new URL(window.location.href);
            cleanUrl.searchParams.delete("message_id");
            cleanUrl.searchParams.delete("quoted_body");
            cleanUrl.searchParams.delete("quoted_sender");
            cleanUrl.searchParams.delete("before_ts");
            window.history.replaceState({}, "", cleanUrl);
          }
        }
        renderManualMode(state.manual_mode || {}, Boolean(state.manual_contact));
        renderProfile(state.profile || {});
      } catch (error) {
        connection.textContent = "Нет связи с программой";
        connection.className = "connection-badge offline";
      } finally {
        if (sequence === stateSequence) loading = false;
      }
    }

    // EO_AUTOREPLY_TOGGLE_V15_20261001
    if (manualModeButton) {
      manualModeButton.addEventListener("click", async () => {
        if (!selectedChatId || manualModeButton.dataset.busy === "1") return;
        manualModeButton.dataset.busy = "1";
        manualModeButton.disabled = true;
        const action = manualModeActive ? "enable" : "disable";
        manualModeButton.textContent = manualModeActive ? "Включаю..." : "Отключаю...";
        try {
          const response = await postForm("/chat-mode", {
            chat_id: selectedChatId,
            action,
          });
          const result = await response.json().catch(() => ({}));
          if (!response.ok || !result.updated) throw new Error(result.error || "Не удалось изменить автоответы");
          renderManualMode(result.manual_mode || {}, false);
          window.QueueUI?.toast?.(result.message || (action === "enable" ? "Автоответы включены" : "Автоответы отключены"));
        } catch (error) {
          queueAlert(error.message || "Не удалось изменить автоответы");
          await loadChatState(true);
        } finally {
          manualModeButton.dataset.busy = "0";
          manualModeButton.disabled = !selectedChatId;
          parkHeaderBotControlsV17();
          syncOverflowBotActionsV17();
        }
      });
    }

    if (botResetButton) {
      botResetButton.addEventListener("click", async () => {
        if (!selectedChatId || botResetButton.dataset.busy === "1") return;
        botResetButton.dataset.busy = "1";
        botResetButton.disabled = true;
        const oldText = botResetButton.textContent;
        botResetButton.textContent = "Перезапуск...";
        try {
          const response = await postForm("/chat-mode", {
            chat_id: selectedChatId,
            action: "reset",
          });
          const result = await response.json().catch(() => ({}));
          if (!response.ok || !result.updated) throw new Error(result.error || "Не удалось перезапустить бота");
          renderManualMode({}, false);
          manualModeState.textContent = result.message || "Бот перезапущен. Автоответы включены.";
          window.QueueUI?.toast?.("Бот для этого пользователя перезапущен");
        } catch (error) {
          queueAlert(error.message || "Не удалось перезапустить бота");
        } finally {
          botResetButton.dataset.busy = "0";
          botResetButton.disabled = !selectedChatId;
          if (botResetButton.textContent === "Перезапуск...") botResetButton.textContent = oldText || "↻ Перезагрузить бота";
          parkHeaderBotControlsV17();
          syncOverflowBotActionsV17();
        }
      });
    }

    const searchStorageKey = "queue-chat-search:" + location.pathname;
    try { chatSearch.value = sessionStorage.getItem(searchStorageKey) || ""; } catch (_) {}
    chatSearch.addEventListener("input", () => {
      try { sessionStorage.setItem(searchStorageKey, chatSearch.value); } catch (_) {}
      renderChats();
    });
    refreshButton.addEventListener("click", () => loadChatState(true));
    const composerTextarea = composer.querySelector("textarea[name='message']");
    if (composerTextarea) {
      composerTextarea.addEventListener("input", () => updateMentionPicker(composerTextarea));
      composerTextarea.addEventListener("click", () => updateMentionPicker(composerTextarea));
      composerTextarea.addEventListener("keydown", (event) => {
        if (event.key === "Escape") mentionPicker.hidden = true;
      });
    }
    if (replyCancel) replyCancel.addEventListener("click", clearReply);
    if (forwardCancel) forwardCancel.addEventListener("click", () => { selectedForForward.clear(); updateForwardToolbar(); lastMessageSignature = ""; loadChatState(true); });
    if (forwardTarget) forwardTarget.addEventListener("change", () => {
      if (forwardSend) forwardSend.disabled = forwardBusy || !forwardTarget.value || forwardSend.dataset.lockDisabled === "1";
    });
    if (forwardSend) forwardSend.addEventListener("click", sendForwardSelection);

    const emojiValues = [
      "😀","😃","😄","😁","😆","😅","😂","🤣","😊","🙂","🙃","😉","😍","🥰","😘","😎","🤩","🥳","😇","🤗",
      "🤔","🫡","🤨","😐","😑","😶","🙄","😏","😴","🤤","😮","😲","😳","🥺","😢","😭","😤","😡","🤬","😱",
      "👍","👎","👌","✌️","🤞","🤟","🤘","🤙","👈","👉","👆","👇","☝️","👋","👏","🙌","👐","🤝","🙏","💪",
      "❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔","❣️","💕","💯","💥","✨","⭐","🔥","🎉","🎊","🎁",
      "✅","❌","⚠️","❗","❓","‼️","⁉️","🔴","🟢","🟡","🔵","📌","📎","📄","📁","📷","🎥","🎵","💬","📞",
      "🚗","🚕","🚚","🚌","🚦","🛠️","🔧","🔒","🔓","🔑","💡","🔔","⏰","⌛","📅","📍","🏢","🏠","🌍","☀️",
      "🌙","☁️","🌧️","❄️","☕","🍵","🍕","🍔","🍎","🍉","🐱","🐶","🦊","🐼","👀","🧠","🚀","🏆","🎯","💼"
    ];
    if (emojiPicker) {
      emojiValues.forEach((emoji) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = emoji;
        button.addEventListener("click", () => {
          const textarea = composer.querySelector("textarea[name='message']");
          const start = textarea.selectionStart ?? textarea.value.length;
          textarea.value = textarea.value.slice(0, start) + emoji + textarea.value.slice(start);
          textarea.focus();
          textarea.setSelectionRange(start + emoji.length, start + emoji.length);
        });
        emojiPicker.append(button);
      });
    }
    if (emojiToggle && emojiPicker) emojiToggle.addEventListener("click", () => { emojiPicker.hidden = !emojiPicker.hidden; });
    let uploadController = null;
    let uploadId = "";
    const attachmentRemove = document.createElement("button");
    attachmentRemove.type = "button";
    attachmentRemove.className = "attachment-remove";
    attachmentRemove.textContent = "× Убрать файл";
    attachmentRemove.hidden = true;
    const attachmentBar = document.createElement("div");
    attachmentBar.className = "attachment-bar";
    attachmentBar.hidden = true;
    if (mediaName) { mediaName.before(attachmentBar); attachmentBar.append(mediaName, attachmentRemove); }
    if (composerTextarea) {
      const field = document.createElement("div");
      field.className = "composer-field";
      composerTextarea.before(field); field.append(composerTextarea);
      const tools = composer.querySelector(".composer-tools");
      if (tools) field.append(tools);
    }
    const composeStatus = document.createElement("div");
    composeStatus.className = "compose-status";
    composeStatus.setAttribute("role", "status");
    composer.append(composeStatus);
    function clearAttachment() {
      if (mediaInput) mediaInput.value = "";
      if (mediaName) mediaName.textContent = "";
      attachmentRemove.hidden = true;
      attachmentBar.hidden = true;
    }
    attachmentRemove.addEventListener("click", () => {
      if (uploadController) uploadController.abort();
      clearAttachment();
    });
    function showAttachment() {
      const file = mediaInput?.files?.[0];
      if (file && (file.size > 512 * 1024 * 1024 || file.size === 0)) {
        composeStatus.textContent = "Выберите непустой файл размером до 512 МБ";
        clearAttachment();
        return;
      }
      if (mediaName) mediaName.textContent = file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} МБ` : "";
      attachmentRemove.hidden = !file;
      attachmentBar.hidden = !file;
    }
    mediaInput?.addEventListener("change", showAttachment);
    // QUEUE_CLIPBOARD_PASTE_3_3_62: Win+Shift+S / clipboard image paste into the current chat.
    const queueBuildClipboardFileName = (mimeType) => {
      const now = new Date();
      const pad = (value) => String(value).padStart(2, "0");
      const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
      const extMap = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"};
      const ext = extMap[mimeType || ""] || "";
      return `clipboard-${stamp}${ext}`;
    };
    const queueSetComposerFile = (file) => {
      if (!file || !mediaInput) return false;
      try {
        const transfer = new DataTransfer();
        transfer.items.add(file);
        mediaInput.files = transfer.files;
      } catch (_) {
        return false;
      }
      showAttachment();
      return true;
    };
    const queueTakeClipboardFile = (event) => {
      const clipboardItems = Array.from(event.clipboardData?.items || []);
      for (const item of clipboardItems) {
        if (item.kind !== "file") continue;
        const blob = item.getAsFile();
        if (!blob) continue;
        const mimeType = blob.type || item.type || "application/octet-stream";
        const originalName = typeof blob.name === "string" ? blob.name.trim() : "";
        // Win+Shift+S usually appears as generic image.png. Always wrap images
        // into a unique clipboard-* file so this send can be matched reliably.
        if (/^image\//.test(mimeType)) {
          try {
            return new File([blob], queueBuildClipboardFileName(mimeType), { type: mimeType, lastModified: Date.now() });
          } catch (_) {
            return blob;
          }
        }
        if (originalName) return blob;
        try {
          return new File([blob], queueBuildClipboardFileName(mimeType), { type: mimeType, lastModified: Date.now() });
        } catch (_) {
          return blob;
        }
      }
      return null;
    };
    const queueHandleClipboardPaste = (event) => {
      const file = queueTakeClipboardFile(event);
      if (!file) return;
      if (composer.dataset.sending === "1") {
        composeStatus.textContent = "Дождитесь завершения текущей отправки";
        return;
      }
      if (!selectedChatId) {
        composeStatus.textContent = "Сначала выберите чат или группу";
        return;
      }
      event.preventDefault();
      const ok = queueSetComposerFile(file);
      composeStatus.textContent = ok
        ? (/^image\//.test(file.type || "") ? "Скриншот из буфера прикреплён. Нажмите «Отправить»" : "Файл из буфера прикреплён. Нажмите «Отправить»")
        : "Не удалось прикрепить файл из буфера";
      composerTextarea?.focus();
    };
    composerTextarea?.addEventListener("paste", queueHandleClipboardPaste);
    // QUEUE_DRAG_DROP_3_3_61: drag a file from Explorer/Desktop directly into the open chat.
    let queueDragDepth = 0;
    const queueDropZone = conversationPage;
    const queueHasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
    const queueSetDroppedFile = (file) => {
      if (!file || !mediaInput) return false;
      try {
        const transfer = new DataTransfer();
        transfer.items.add(file);
        mediaInput.files = transfer.files;
      } catch (_) {
        return false;
      }
      showAttachment();
      return true;
    };
    queueDropZone?.addEventListener("dragenter", (event) => {
      if (!queueHasFiles(event)) return;
      event.preventDefault();
      queueDragDepth += 1;
      queueDropZone.classList.add("queue-file-drag-active");
    });
    queueDropZone?.addEventListener("dragover", (event) => {
      if (!queueHasFiles(event)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      queueDropZone.classList.add("queue-file-drag-active");
    });
    queueDropZone?.addEventListener("dragleave", (event) => {
      if (!queueHasFiles(event)) return;
      queueDragDepth = Math.max(0, queueDragDepth - 1);
      if (!queueDragDepth) queueDropZone.classList.remove("queue-file-drag-active");
    });
    queueDropZone?.addEventListener("drop", (event) => {
      if (!queueHasFiles(event)) return;
      event.preventDefault();
      queueDragDepth = 0;
      queueDropZone.classList.remove("queue-file-drag-active");
      if (composer.dataset.sending === "1") {
        composeStatus.textContent = "Дождитесь завершения текущей отправки";
        return;
      }
      if (!selectedChatId) {
        composeStatus.textContent = "Сначала выберите чат или группу";
        return;
      }
      const files = Array.from(event.dataTransfer?.files || []);
      if (!files.length) return;
      if (files.length > 1) composeStatus.textContent = "Будет прикреплён первый файл. Остальные можно отправить следующим сообщением";
      if (queueSetComposerFile(files[0]) && files.length === 1) composeStatus.textContent = "Файл прикреплён. Нажмите «Отправить»";
      composerTextarea?.focus();
    });
    const draftKey = () => "queue-draft:" + selectedChatId;
    function saveDraft() {
      if (!selectedChatId || !composerTextarea) return;
      try {
        if (composerTextarea.value) localStorage.setItem(draftKey(), composerTextarea.value);
        else localStorage.removeItem(draftKey());
      } catch (_) {}
    }
    function restoreDraft() {
      if (!composerTextarea) return;
      try { composerTextarea.value = localStorage.getItem(draftKey()) || ""; } catch (_) {}
    }
    restoreDraft();
    composerTextarea?.addEventListener("input", saveDraft);
    composerTextarea?.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && mentionPicker.hidden) {
        event.preventDefault();
        if (composer.dataset.sending !== "1") composer.requestSubmit();
      }
    });
    composerTextarea?.setAttribute("title", "Enter: отправить. Shift + Enter: новая строка");

    async function uploadRequest(route, body, signal, raw = false) {
      const response = await fetch(route, {
        method: "POST", signal,
        headers: {"Content-Type": raw ? "application/octet-stream" : "application/json",
          "X-CSRF-Token": document.querySelector('meta[name="queue-csrf"]')?.content || ""},
        body: raw ? body : JSON.stringify(body),
      });
      let result;
      try { result = await response.json(); } catch (_) { throw new Error("Сервер не принял загрузку. Проверьте соединение"); }
      if (!response.ok) throw new Error(result.error || "Ошибка загрузки файла");
      return result;
    }
    const requestKey = () => "queue-send-intent:" + selectedChatId;
    function sendIntent(text, file) {
      const signature=JSON.stringify([text,replyInput?.value || "",file ? [file.name,file.size,file.lastModified] : null]);
      let prior; try { prior=JSON.parse(localStorage.getItem(requestKey()) || "null"); } catch (_) {}
      if (prior && prior.signature === signature) return prior.id;
      const id=crypto.randomUUID ? crypto.randomUUID() : Date.now()+"-"+Math.random().toString(36).slice(2);
      try { localStorage.setItem(requestKey(),JSON.stringify({signature,id})); } catch (_) {}
      return id;
    }
    composer.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (composer.dataset.sending === "1") return;
      const textarea = composer.querySelector("textarea[name='message']");
      const button = composer.querySelector("button[type='submit']");
      const file = mediaInput?.files?.[0];
      if (!selectedChatId || (!textarea.value.trim() && !file)) return;
      if (file && file.size > 512 * 1024 * 1024) { showAttachment(); return; }
      composer.dataset.sending = "1";
      button.disabled = true; textarea.disabled = true;
      if (mediaInput) mediaInput.disabled = true;
      composeStatus.textContent = file ? "Подготовка файла…" : "Отправка…";
      let completed = false;
      const requestId=sendIntent(textarea.value,file);
      try {
        let result;
        if (file) {
          uploadController = new AbortController();
          const signal = uploadController.signal;
          const started = await uploadRequest("/upload/start", {
            chat_id: selectedChatId, message: textarea.value, request_id: requestId,
            reply_to: replyInput?.value || "", mimetype: file.type,
            filename: file.name, size: file.size, mentions: [...selectedMentionIds],
          }, signal);
          uploadId = started.upload_id;
          attachmentRemove.textContent = "× Отменить загрузку";
          for (let offset = 0; offset < file.size; offset += started.chunk_size) {
            const end = Math.min(file.size, offset + started.chunk_size);
            await uploadRequest(`/upload/chunk?id=${encodeURIComponent(uploadId)}&offset=${offset}`, file.slice(offset,end), signal, true);
            composeStatus.textContent = `Загрузка файла: ${Math.round(end/file.size*100)}%`;
          }
          // The commit is atomic. Cancellation is no longer offered once queued.
          attachmentRemove.hidden = true;
          result = await uploadRequest("/upload/finish", {upload_id:uploadId});
          completed = Boolean(result.queued);
        } else {
          const response = await postForm(sendEndpoint, {
            chat_id: selectedChatId, message: textarea.value, request_id: requestId,
            mentions: mentionsInput?.value || "", reply_to: replyInput?.value || "",
          });
          result = await response.json();
          if (!response.ok) throw new Error(result.error || "Не удалось отправить сообщение");
        }
        if (!result.queued) throw new Error(result.error || "Сообщение не поставлено в очередь");
        try { localStorage.removeItem(requestKey()); } catch (_) {}
        textarea.value = ""; saveDraft(); clearReply();
        selectedMentionIds.clear();
        if (mentionsInput) mentionsInput.value = "";
        clearAttachment();
        if (emojiPicker) emojiPicker.hidden = true;
        composeStatus.textContent = "Отправляется…";
        void loadChatState(true);
        window.setTimeout(() => { composeStatus.textContent = ""; }, 1800);
      } catch (error) {
        composeStatus.textContent = error.name === "AbortError" ? "Загрузка отменена" : (error.message || "Не удалось отправить сообщение");
        if (uploadId && !completed) uploadRequest("/upload/cancel", {upload_id:uploadId}).catch(() => {});
      } finally {
        uploadController = null; uploadId = "";
        attachmentRemove.textContent = "× Убрать файл";
        showAttachment();
        button.disabled = false; textarea.disabled = false;
        if (mediaInput) mediaInput.disabled = false;
        composer.dataset.sending = "0";
        textarea.focus();
      }
    });

    if (selectedChatId) chatIdInput.value = selectedChatId;
    // EO_PERFORMANCE_20260930: realtime-first, slow polling only as a safety net.
    let chatPollTimer = null;
    const scheduleChatPoll = () => {
      if (chatPollTimer) window.clearTimeout(chatPollTimer);
      chatPollTimer = window.setTimeout(async () => {
        await loadChatState(false);
        scheduleChatPoll();
      }, document.hidden ? 60000 : 15000);
    };
    window.addEventListener("queue-realtime", () => {
      if (!document.hidden) void loadChatState(false);
    });
    window.addEventListener("queue-open-message", () => {
      const wanted=new URL(location.href).searchParams.get("chat_id");
      if(wanted && wanted!==selectedChatId){if(selectedChatId) void window.QueueConversationLock?.releaseIfOwned?.(selectedChatId); saveDraft(); selectedChatId=wanted; olderMessages=[];latestMessages=[];historyCursor="";historyHasMore=false;restoreDraft();}
      delete conversationPage.dataset.jumpLoaded;delete conversationPage.dataset.jumpScrolled;
      loadChatState(true);
    });
    loadChatState(true).finally(scheduleChatPoll);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) loadChatState(true);
      scheduleChatPoll();
    });
  }

  function setupMediaLightbox() {
    let overlay = null;
    let activeCleanup = null;
    function close() {
      if (typeof activeCleanup === "function") {
        try { activeCleanup(); } catch (_) {}
      }
      activeCleanup = null;
      if (overlay) overlay.remove();
      overlay = null;
      document.body.classList.remove("media-lightbox-open");
    }
    function open(src, name) {
      close();
      overlay = document.createElement("div");
      overlay.className = "media-lightbox";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      const frame = document.createElement("div");
      frame.className = "media-lightbox-frame";
      const closeButton = document.createElement("button");
      closeButton.type = "button";
      closeButton.className = "media-lightbox-close";
      closeButton.textContent = "×";
      closeButton.title = "Закрыть";
      closeButton.addEventListener("click", close);

      const viewport = document.createElement("div");
      viewport.className = "media-lightbox-viewport";
      const image = document.createElement("img");
      image.src = src;
      image.alt = name || "Фото";
      image.draggable = false;
      viewport.append(image);

      const controls = document.createElement("div");
      controls.className = "media-lightbox-controls";
      const zoomOut = document.createElement("button");
      zoomOut.type = "button";
      zoomOut.className = "button secondary";
      zoomOut.textContent = "−";
      zoomOut.title = "Уменьшить";
      const zoomIn = document.createElement("button");
      zoomIn.type = "button";
      zoomIn.className = "button secondary";
      zoomIn.textContent = "+";
      zoomIn.title = "Увеличить";
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "button secondary";
      reset.textContent = "100%";
      reset.title = "Сбросить масштаб и позицию";
      const label = document.createElement("span");
      const hint = document.createElement("span");
      hint.className = "media-lightbox-hint";
      hint.textContent = "Зажмите левую кнопку мыши на фото и тяните в любую сторону";
      const range = document.createElement("input");
      range.type = "range";
      range.min = "40";
      range.max = "300";
      range.step = "10";
      range.value = "100";

      let zoom = 100;
      let panX = 0;
      let panY = 0;
      let drag = null;

      function clampZoom(value) {
        return Math.max(40, Math.min(300, Number(value || 100)));
      }
      function panBounds() {
        const scale = zoom / 100;
        const width = image.offsetWidth * scale;
        const height = image.offsetHeight * scale;
        // Разрешаем тянуть картинку при любом масштабе. Если изображение меньше
        // окна, оно двигается внутри свободного пространства. Если больше, границы
        // соответствуют области, которую реально можно осмотреть.
        return {
          x: Math.max(48, Math.abs(width - viewport.clientWidth) / 2),
          y: Math.max(48, Math.abs(height - viewport.clientHeight) / 2),
        };
      }
      function clampPan() {
        const bounds = panBounds();
        panX = Math.max(-bounds.x, Math.min(bounds.x, panX));
        panY = Math.max(-bounds.y, Math.min(bounds.y, panY));
        viewport.classList.add("is-draggable");
        return true;
      }
      function applyTransform() {
        clampPan();
        image.style.transform = `translate3d(${panX}px, ${panY}px, 0) scale(${zoom / 100})`;
        label.textContent = `Масштаб ${zoom}%`;
        range.value = String(zoom);
      }
      function updateZoom(value, resetPan = false) {
        const previous = zoom || 100;
        zoom = clampZoom(value);
        if (resetPan) {
          panX = 0;
          panY = 0;
        } else if (previous > 0) {
          const ratio = zoom / previous;
          panX *= ratio;
          panY *= ratio;
        }
        applyTransform();
      }
      function stopDragging(event) {
        if (!drag) return;
        if (event && drag.pointerId !== undefined && event.pointerId !== undefined && drag.pointerId !== event.pointerId) return;
        drag = null;
        viewport.classList.remove("is-dragging");
      }

      range.addEventListener("input", () => updateZoom(range.value));
      zoomOut.addEventListener("click", () => updateZoom(zoom - 10));
      zoomIn.addEventListener("click", () => updateZoom(zoom + 10));
      reset.addEventListener("click", () => updateZoom(100, true));
      image.addEventListener("dblclick", () => updateZoom(100, true));
      viewport.addEventListener("wheel", (event) => {
        event.preventDefault();
        updateZoom(zoom + (event.deltaY < 0 ? 10 : -10));
      }, { passive: false });

      viewport.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || !clampPan()) return;
        drag = {
          pointerId: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          panX,
          panY,
        };
        viewport.classList.add("is-dragging");
        try { viewport.setPointerCapture(event.pointerId); } catch (_) {}
        event.preventDefault();
      });
      viewport.addEventListener("pointermove", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        panX = drag.panX + (event.clientX - drag.x);
        panY = drag.panY + (event.clientY - drag.y);
        applyTransform();
        event.preventDefault();
      });
      viewport.addEventListener("pointerup", stopDragging);
      viewport.addEventListener("pointercancel", stopDragging);
      viewport.addEventListener("lostpointercapture", stopDragging);
      window.addEventListener("resize", applyTransform);
      image.addEventListener("load", () => updateZoom(100, true), { once: true });

      controls.append(zoomOut, zoomIn, reset, label, range, hint);
      const caption = document.createElement("div");
      caption.className = "media-lightbox-caption";
      caption.textContent = name || "Фото";
      frame.append(closeButton, viewport, controls, caption);
      overlay.append(frame);
      overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
      document.body.append(overlay);
      document.body.classList.add("media-lightbox-open");
      if (image.complete) updateZoom(100, true);
      activeCleanup = () => {
        drag = null;
        window.removeEventListener("resize", applyTransform);
      };
    }
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-media-zoom]");
      if (!button) return;
      event.preventDefault();
      open(button.dataset.mediaZoom || "", button.dataset.mediaName || "Фото");
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && overlay) close(); });
  }



  function startNotifications() {
    if (!notificationCenter || !notificationPopover || !notificationCount || !notificationItems) return;

    // EO_NOTIFICATION_TRIGGER_V14_20261001
    const loudSoundKey = "queue-loud-notification-sound-v14";
    let loudSoundEnabled = true;
    try { loudSoundEnabled = localStorage.getItem(loudSoundKey) !== "0"; } catch (_) {}
    let noticeAudioContext = null;
    let noticeAudioUnlocked = false;

    function ensureNoticeAudio(unlockOnly = false) {
      if (!loudSoundEnabled) return null;
      try {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) return null;
        if (!noticeAudioContext) noticeAudioContext = new AudioContextClass();
        if (noticeAudioContext.state === "suspended") {
          const resumed = noticeAudioContext.resume();
          if (resumed && typeof resumed.catch === "function") resumed.catch(() => {});
        }
        noticeAudioUnlocked = noticeAudioContext.state === "running" || noticeAudioUnlocked;
        return unlockOnly ? noticeAudioContext : (noticeAudioUnlocked ? noticeAudioContext : null);
      } catch (_) { return null; }
    }

    const unlockNoticeAudio = () => {
      const ctx = ensureNoticeAudio(true);
      if (!ctx) return;
      const mark = () => { noticeAudioUnlocked = ctx.state === "running"; };
      if (ctx.state === "running") mark();
      else if (ctx.resume) Promise.resolve(ctx.resume()).then(mark).catch(() => {});
    };
    document.addEventListener("pointerdown", unlockNoticeAudio, {capture:true, once:true});
    document.addEventListener("keydown", unlockNoticeAudio, {capture:true, once:true});

    function playTriggerNotice(item = {}) {
      if (!loudSoundEnabled) return;
      const ctx = ensureNoticeAudio(false);
      if (!ctx) {
        try { window.QueueUI?.playNotice?.(); } catch (_) {}
        return;
      }
      try {
        const now = ctx.currentTime + 0.015;
        const compressor = ctx.createDynamicsCompressor();
        compressor.threshold.value = -24;
        compressor.knee.value = 10;
        compressor.ratio.value = 12;
        compressor.attack.value = 0.003;
        compressor.release.value = 0.22;
        compressor.connect(ctx.destination);

        const urgent = ["system","sla"].includes(String(item.kind || "").toLowerCase());
        const tones = urgent
          ? [[880,0.00,0.13],[1320,0.16,0.13],[880,0.34,0.13],[1568,0.50,0.20],[1175,0.82,0.15],[1568,1.00,0.22]]
          : [[880,0.00,0.13],[1320,0.16,0.13],[1568,0.34,0.20],[1175,0.62,0.14]];

        tones.forEach(([frequency, offset, duration]) => {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = "square";
          osc.frequency.setValueAtTime(frequency, now + offset);
          gain.gain.setValueAtTime(0.0001, now + offset);
          gain.gain.exponentialRampToValueAtTime(0.34, now + offset + 0.012);
          gain.gain.setValueAtTime(0.34, now + offset + Math.max(0.025, duration - 0.035));
          gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + duration);
          osc.connect(gain);
          gain.connect(compressor);
          osc.start(now + offset);
          osc.stop(now + offset + duration + 0.02);
        });
      } catch (_) {
        try { window.QueueUI?.playNotice?.(); } catch (_) {}
      }
    }

    const loudSoundButton = document.createElement("button");
    loudSoundButton.type = "button";
    loudSoundButton.className = "notification-native-toggle";
    const syncLoudSoundButton = () => {
      loudSoundButton.textContent = loudSoundEnabled ? "Громкий сигнал: включён" : "Громкий сигнал: выключен";
    };
    loudSoundButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      loudSoundEnabled = !loudSoundEnabled;
      try { localStorage.setItem(loudSoundKey, loudSoundEnabled ? "1" : "0"); } catch (_) {}
      syncLoudSoundButton();
      if (loudSoundEnabled) {
        unlockNoticeAudio();
        window.setTimeout(() => playTriggerNotice({kind:"info"}), 80);
      }
    });
    notificationPopover.append(loudSoundButton);
    syncLoudSoundButton();

    let loading = false;
    const seenKey = "queue-toast-seen-v331";
    const initKey = "queue-toast-initialized-v331";
    let seen = new Set();
    let initialized = false;
    try {
      seen = new Set(JSON.parse(localStorage.getItem(seenKey) || "[]"));
      initialized = localStorage.getItem(initKey) === "1";
    } catch (_) {}

    let noticeDatabase;
    function claimNotice(id) {
      if (!window.indexedDB) return Promise.resolve(true);
      noticeDatabase ||= new Promise((resolve, reject) => {
        const request=indexedDB.open("queue-notifications",1);
        request.onupgradeneeded=()=>request.result.createObjectStore("shown");
        request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
      });
      return noticeDatabase.then(db=>new Promise(resolve=>{
        const transaction=db.transaction("shown","readwrite");
        const request=transaction.objectStore("shown").add(Date.now(),id);
        request.onerror=event=>{event.preventDefault();event.stopPropagation();};
        let added=false;request.onsuccess=()=>{added=true;};
        transaction.oncomplete=()=>resolve(added);transaction.onabort=()=>resolve(false);
      })).catch(()=>false);
    }

    function saveSeen() {
      try { localStorage.setItem(seenKey, JSON.stringify([...seen].slice(-120))); } catch (_) {}
    }

    function absoluteAssetUrl(value, fallback = "") {
      const raw = String(value || fallback || "").trim();
      if (!raw) return "";
      try { return new URL(raw, window.location.origin).href; } catch (_) { return ""; }
    }

    function showNativeNotice(item) {
      if (!("Notification" in window) || Notification.permission !== "granted") return;
      if (!document.hidden && document.hasFocus()) return;
      const title = String(item.title || "Единая очередь");
      const body = [String(item.source || "").trim(), String(item.detail || "").trim()].filter(Boolean).join("\n");
      const icon = absoluteAssetUrl(item.avatar_url, "/static/favicon.png?v=3.3.52");
      const image = absoluteAssetUrl(item.media_url);
      try {
        const notification = new Notification(title, {
          body,
          icon,
          badge: absoluteAssetUrl("/static/favicon.png?v=3.3.52"),
          image: image || undefined,
          tag: `queue-${String(item.id || Date.now())}`,
          renotify: true,
          silent: false,
        });
        notification.onclick = () => {
          try { window.focus(); } catch (_) {}
          const href = String(item.href || "#");
          if (href && href !== "#") window.location.href = href;
          notification.close();
        };
        window.setTimeout(() => notification.close(), item.kind === "system" ? 16000 : 12000);
      } catch (_) {}
    }

    function showToast(item) {
      if (!toastStack || !item || !item.id) return;
      const kind = String(item.kind || "info");
      const iconMap = {
        ticket: "🎫",
        tickets: "🎫",
        contact: "💬",
        contacts: "💬",
        group: "@",
        groups: "@",
        support: "🛟",
        system: "⚠",
        sla: "⏱",
        info: "🔔",
      };
      const ttl = kind === "system" || kind === "sla" ? 13000 : 9500;
      const card = document.createElement("div");
      card.className = `toast-card toast-${kind}`;
      const link = document.createElement("a");
      link.className = "toast-link";
      link.href = String(item.href || "#");
      const icon = document.createElement("span");
      icon.className = "toast-icon";
      const avatarUrl = String(item.avatar_url || "").trim();
      if (avatarUrl) {
        const avatar = document.createElement("img");
        avatar.src = avatarUrl;
        avatar.alt = "";
        avatar.loading = "eager";
        avatar.addEventListener("error", () => { avatar.remove(); icon.textContent = iconMap[kind] || iconMap.info; });
        icon.append(avatar);
      } else {
        icon.textContent = iconMap[kind] || iconMap.info;
      }
      const content = document.createElement("div");
      content.className = "toast-content";
      const meta = document.createElement("div");
      meta.className = "toast-meta";
      const source = document.createElement("small");
      source.textContent = String(item.source || "Единая очередь");
      const badge = document.createElement("b");
      badge.className = "toast-kind-badge";
      badge.textContent = kind === "group" || kind === "groups" ? "Упоминание" : kind === "contact" || kind === "contacts" ? "Сообщение" : kind === "ticket" || kind === "tickets" ? "Заявка" : kind === "support" ? "Поддержка" : kind === "sla" ? "SLA" : kind === "system" ? "Система" : "Событие";
      meta.append(source, badge);
      const title = document.createElement("strong");
      title.textContent = String(item.title || "Новое событие");
      const detail = document.createElement("span");
      detail.textContent = String(item.detail || "");
      content.append(meta, title, detail);
      const mediaUrl = String(item.media_url || "").trim();
      if (mediaUrl) {
        const thumb = document.createElement("img");
        thumb.className = "toast-media-preview";
        thumb.src = mediaUrl;
        thumb.alt = "Фото из сообщения";
        thumb.loading = "eager";
        thumb.addEventListener("error", () => thumb.remove());
        content.append(thumb);
      }
      link.append(icon, content);
      const close = document.createElement("button");
      close.type = "button";
      close.className = "toast-close";
      close.setAttribute("aria-label", "Закрыть уведомление");
      close.textContent = "×";
      const progress = document.createElement("div");
      progress.className = "toast-progress";
      progress.style.animationDuration = `${ttl}ms`;
      const remove = () => { card.classList.add("toast-leave"); window.setTimeout(() => card.remove(), 200); };
      close.addEventListener("click", remove);
      card.append(link, close, progress);
      toastStack.prepend(card);
      playTriggerNotice(item);
      showNativeNotice(item);
      while (toastStack.children.length > 4) toastStack.lastElementChild.remove();
      window.setTimeout(remove, ttl);
    }


    const nativeNoticeButton = document.createElement("button");
    nativeNoticeButton.type = "button";
    nativeNoticeButton.className = "notification-native-toggle";
    function syncNativeNoticeButton() {
      if (!("Notification" in window)) {
        nativeNoticeButton.textContent = "Уведомления браузера недоступны";
        nativeNoticeButton.disabled = true;
        return;
      }
      nativeNoticeButton.disabled = Notification.permission === "denied";
      nativeNoticeButton.textContent = Notification.permission === "granted"
        ? "Уведомления браузера: включены"
        : Notification.permission === "denied"
          ? "Уведомления браузера заблокированы"
          : "Включить уведомления браузера";
    }
    nativeNoticeButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!("Notification" in window) || Notification.permission === "denied") return;
      try {
        const permission = await Notification.requestPermission();
        if (permission === "granted") {
          unlockNoticeAudio();
          playTriggerNotice({kind:"info"});
          const test = new Notification("Единая очередь", {
            body: "Уведомления браузера включены. Новые события будут видны даже в другой вкладке.",
            icon: absoluteAssetUrl("/static/favicon.png?v=3.3.52"),
            badge: absoluteAssetUrl("/static/favicon.png?v=3.3.52"),
            tag: "queue-notification-test",
            renotify: true,
            silent: false,
          });
          window.setTimeout(() => test.close(), 6000);
        }
      } catch (_) {}
      syncNativeNoticeButton();
    });
    notificationPopover.append(nativeNoticeButton);
    syncNativeNoticeButton();

    notificationCenter.addEventListener("click", () => {
      notificationPopover.hidden = !notificationPopover.hidden;
    });
    document.addEventListener("click", (event) => {
      if (!notificationPopover.contains(event.target) && !notificationCenter.contains(event.target)) {
        notificationPopover.hidden = true;
      }
    });

    let queueSiteFailureCount = 0;
    let queueSiteFailureNotified = false;
    async function refresh() {
      if (loading) return;
      loading = true;
      try {
        const response = await fetch("/api/notifications", {cache: "no-store"});
        if (!response.ok) throw new Error("Нет связи");
        const state = await response.json();
        queueSiteFailureCount = 0;
        queueSiteFailureNotified = false;
        const counts = state.counts || {};
        const total = Number(state.total || 0);
        notificationCount.textContent = String(total);
        notificationCount.hidden = total <= 0;
        notificationCenter.classList.toggle("has-unread", total > 0);
        notificationItems.replaceChildren();
        const groupChatId = String(state.group_chat_id || "");
        const groupHref = groupChatId ? `/groups?chat_id=${encodeURIComponent(groupChatId)}` : "/groups";
        const contactChatId = String(state.contact_chat_id || "");
        const contactHref = contactChatId ? `/whatsapp?chat_id=${encodeURIComponent(contactChatId)}` : "/whatsapp";
        const rows = [
          ["Новые заявки", Number(counts.tickets || 0), "/"],
          ["Новые сообщения WhatsApp", Number(counts.contacts || 0), contactHref],
          ["@ Упоминания в группах", Number(counts.groups || 0), groupHref],
          ["Вопросы в поддержку", Number(counts.support || 0), "/?category=support&status=new"],
          ["Напоминания", Number(counts.reminders || 0), "/reminders"],
          ["Системные предупреждения", Number(counts.system || 0), "/admin/system"],
        ];
        rows.forEach(([label, amount, href]) => {
          const link = document.createElement("a");
          link.href = href;
          link.className = "notification-item";
          link.textContent = `${label}: ${amount}`;
          notificationItems.append(link);
        });

        // 3.3.104: show active system/SLA problems with explicit actions.
        // "Проверить" re-runs live diagnostics, "Открыть" takes the employee to
        // the relevant screen, and "Убрать" dismisses the current occurrence.
        const allNotificationEvents = Array.isArray(state.events) ? state.events : [];
        const systemEvents = [];
        const systemEventIds = new Set();
        for (const item of allNotificationEvents) {
          if (!item) continue;
          const kind = String(item.kind || "").toLowerCase();
          const eventId = String(item.id || item.event_id || "").trim();
          const isSystem = kind === "system" || kind === "sla" || eventId.startsWith("system:") || eventId.startsWith("reliability:") || eventId.startsWith("sla:");
          if (!isSystem) continue;
          const dedupeKey = eventId || `${kind}:${String(item.title || "")}:${String(item.detail || "")}`;
          if (systemEventIds.has(dedupeKey)) continue;
          systemEventIds.add(dedupeKey);
          systemEvents.push(item);
        }
        const postWarningAction = async (action, eventId = "") => {
          const response = await fetch("/api/system-warning-action", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({action, event_id:eventId}),
          });
          let payload = {};
          try { payload = await response.json(); } catch (_) {}
          if (!response.ok || payload.error) throw new Error(String(payload.error || "Не удалось выполнить действие"));
          return payload;
        };
        if (systemEvents.length) {
          const details = document.createElement("section");
          details.className = "notification-system-events";
          details.setAttribute("aria-label", "Активные системные предупреждения");
          const headingRow = document.createElement("div");
          headingRow.className = "notification-system-events-heading";
          const heading = document.createElement("div");
          heading.className = "notification-system-events-title";
          heading.textContent = "Что требует внимания";
          const headingActions = document.createElement("div");
          headingActions.className = "notification-system-heading-actions";
          const checkAll = document.createElement("button");
          checkAll.type = "button";
          checkAll.className = "notification-system-mini-action";
          checkAll.textContent = "Проверить все";
          checkAll.addEventListener("click", async (event) => {
            event.preventDefault(); event.stopPropagation();
            checkAll.disabled = true; checkAll.textContent = "Проверяю…";
            try { await postWarningAction("check_all"); await refresh(); }
            catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось проверить предупреждения", "error"); }
            finally { checkAll.disabled = false; checkAll.textContent = "Проверить все"; }
          });
          const dismissAll = document.createElement("button");
          dismissAll.type = "button";
          dismissAll.className = "notification-system-mini-action danger";
          dismissAll.textContent = "Убрать все";
          dismissAll.addEventListener("click", async (event) => {
            event.preventDefault(); event.stopPropagation();
            dismissAll.disabled = true;
            try { await postWarningAction("dismiss_all"); await refresh(); }
            catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось убрать предупреждения", "error"); }
            finally { dismissAll.disabled = false; }
          });
          headingActions.append(checkAll, dismissAll);
          headingRow.append(heading, headingActions);
          details.append(headingRow);
          systemEvents.slice(0, 8).forEach((item) => {
            const kind = String(item.kind || "").toLowerCase();
            const eventId = String(item.id || item.event_id || "").trim();
            const level = String(item.level || "warning").toLowerCase();
            const card = document.createElement("div");
            card.className = `notification-system-event ${level === "critical" ? "critical" : "warning"}`;
            let href = String(item.href || "").trim();
            if (!href && eventId.startsWith("sla:")) {
              const ticketId = eventId.split(":")[1] || "";
              href = /^\d+$/.test(ticketId) ? `/ticket?id=${encodeURIComponent(ticketId)}` : "/admin/system";
            }
            href = href || "/admin/system";
            const icon = document.createElement("span");
            icon.className = "notification-system-event-icon";
            icon.textContent = level === "critical" ? "●" : kind === "sla" ? "◷" : "!";
            const body = document.createElement("span");
            body.className = "notification-system-event-body";
            const title = document.createElement("strong");
            title.textContent = String(item.title || "Системное предупреждение");
            const detail = document.createElement("span");
            detail.textContent = String(item.detail || "Откройте раздел для подробностей");
            body.append(title, detail);
            const meta = document.createElement("small");
            const ts = Number(item.timestamp || 0);
            const timeText = ts > 0 ? new Date(ts * 1000).toLocaleString("ru-RU", {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"}) : "";
            meta.textContent = [kind === "sla" ? "SLA" : "Система", timeText].filter(Boolean).join(" · ");
            body.append(meta);
            const actions = document.createElement("div");
            actions.className = "notification-system-event-actions";
            const open = document.createElement("a");
            open.className = "notification-system-action";
            open.href = href;
            open.textContent = kind === "sla" ? "К заявке" : "Открыть";
            const check = document.createElement("button");
            check.type = "button";
            check.className = "notification-system-action";
            check.textContent = "Проверить";
            check.addEventListener("click", async (event) => {
              event.preventDefault(); event.stopPropagation();
              check.disabled = true; check.textContent = "Проверяю…";
              try { await postWarningAction("check", eventId); await refresh(); }
              catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось проверить предупреждение", "error"); }
              finally { check.disabled = false; check.textContent = "Проверить"; }
            });
            const dismiss = document.createElement("button");
            dismiss.type = "button";
            dismiss.className = "notification-system-action danger";
            dismiss.textContent = "Убрать";
            dismiss.title = "Скрыть текущее предупреждение. Если проблема исчезнет и возникнет снова, уведомление появится заново.";
            dismiss.addEventListener("click", async (event) => {
              event.preventDefault(); event.stopPropagation();
              dismiss.disabled = true;
              try { await postWarningAction("dismiss", eventId); await refresh(); }
              catch (error) { window.QueueUI?.toast?.(error.message || "Не удалось убрать предупреждение", "error"); dismiss.disabled = false; }
            });
            actions.append(open, check, dismiss);
            card.append(icon, body, actions);
            details.append(card);
          });
          const systemCount = Number(counts.system || 0);
          if (systemCount > systemEvents.slice(0, 8).length) {
            const more = document.createElement("a");
            more.className = "notification-system-more";
            more.href = "/admin/system";
            more.textContent = `Ещё ${systemCount - systemEvents.slice(0, 8).length} предупрежд.`;
            details.append(more);
          }
          notificationItems.append(details);
        }

        const notifyOnce = async () => {
          try { seen = new Set(JSON.parse(localStorage.getItem(seenKey) || "[]")); initialized = localStorage.getItem(initKey) === "1"; } catch (_) {}
        const events = Array.isArray(state.events) ? state.events : [];
        const currentIds = new Set(events.map((item) => String(item.id || item.event_id || "")).filter(Boolean));
        if (!currentIds.has("system:whatsapp-offline")) seen.delete("system:whatsapp-offline");
        if (!initialized) {
          for (const item of events) { const id = String(item && (item.id || item.event_id) || ""); if (id) { seen.add(id); await claimNotice(id); } }
          initialized = true;
          try { localStorage.setItem(initKey, "1"); } catch (_) {}
          saveSeen();
        } else {
          for (const item of [...events].reverse()) {
            const id = String(item && (item.id || item.event_id) || "");
            if (!id || seen.has(id)) continue;
            seen.add(id);
            if (await claimNotice(id)) showToast(item);
          }
          saveSeen();
        }
        };
        if (navigator.locks) await navigator.locks.request("queue-notifications-356", notifyOnce);
        else {
          // Older browsers use one elected tab; system Notification tag also replaces duplicates.
          const leaseKey = "queue-notice-lease";
          const tabId = window.queueNoticeTabId ||= Math.random().toString(36).slice(2);
          let lease = {}; try { lease = JSON.parse(localStorage.getItem(leaseKey) || "{}"); } catch (_) {}
          if (!lease.until || lease.until < Date.now() || lease.id === tabId) {
            localStorage.setItem(leaseKey, JSON.stringify({id:tabId, until:Date.now()+6000})); await notifyOnce();
          }
        }
      } catch (error) {
        notificationItems.textContent = "Не удалось обновить уведомления";
        queueSiteFailureCount += 1;
        if (queueSiteFailureCount >= 2 && !queueSiteFailureNotified) {
          queueSiteFailureNotified = true;
          showToast({id:"system:site-unavailable",kind:"system",title:"Связь с Единой очередью потеряна",detail:"Сайт или сервер временно недоступен. Проверяется автоматически.",source:"Система",href:"/admin/system"});
        }
      } finally {
        loading = false;
      }
    }
    refresh();
    document.addEventListener("visibilitychange", () => { void refresh(); });
    window.addEventListener("focus", () => { void refresh(); });
    window.addEventListener("queue-realtime", () => { void refresh(); });
    window.setInterval(() => { void refresh(); }, 15000);
  }

  if (menu) {
    menu.querySelectorAll("[data-quick-status]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!activeTicketId) return;
        menu.classList.add("busy");
        try {
          await postForm("/quick-status", {
            ticket_id: activeTicketId,
            status: button.dataset.quickStatus,
            employee: activeEmployee,
          });
          closeMenu();
          await refreshDashboard(true);
        } catch (error) {
          queueAlert(error.message);
        } finally {
          menu.classList.remove("busy");
        }
      });
    });

    menu.querySelectorAll("[data-quick-handoff]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!activeTicketId) return;
        menu.classList.add("busy");
        try {
          await postForm("/handoff", {
            ticket_id: activeTicketId,
            action: "transfer",
          });
          closeMenu();
          await refreshDashboard(true);
        } catch (error) {
          queueAlert(error.message);
        } finally {
          menu.classList.remove("busy");
        }
      });
    });

    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) closeMenu();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeMenu();
    });
    window.addEventListener("blur", closeMenu);
    window.addEventListener("resize", closeMenu);
    window.addEventListener("scroll", closeMenu, true);
  }

  bindRows();
  setupMediaLightbox();
  startConversationPage();
  startNotifications();
  startQueueRealtime();
  if (dashboard) {
    window.addEventListener("queue-realtime", () => { if (!document.hidden) void refreshDashboard(false); });
    window.setInterval(() => { if (!document.hidden) refreshDashboard(false); }, 30000);
  }
  if (autoPageRefresh) {
    const interval = Math.max(5000, Number(autoPageRefresh.dataset.autoPageRefresh) || 15000);
    window.setInterval(() => {
      if (!document.hidden) window.location.reload();
    }, interval);
  }
})();


// 1.00.6.129: compact chat banners and follow the logged-in employee as current shift.
(() => {
  const style = document.createElement("style");
  style.textContent = `
    .conversation-lock-banner { padding: 6px 10px !important; margin: 4px 0 !important; min-height: 0 !important; gap: 8px !important; align-items: center !important; }
    .conversation-lock-banner > div:first-child { display: flex !important; flex: 1 1 auto !important; flex-wrap: wrap !important; align-items: baseline !important; gap: 2px 9px !important; min-width: 0 !important; }
    .conversation-lock-banner > div:first-child strong, .conversation-lock-banner > div:first-child span { margin: 0 !important; line-height: 1.25 !important; }
    .conversation-lock-actions { display: flex !important; flex: 0 1 auto !important; flex-wrap: nowrap !important; align-items: center !important; gap: 6px !important; margin: 0 !important; }
    .conversation-lock-actions .lock-transfer-select { width: auto !important; max-width: min(240px, 32vw) !important; min-height: 32px !important; }
    .conversation-lock-actions .button { min-height: 32px !important; padding: 5px 10px !important; white-space: nowrap !important; }
    [data-forward-toolbar] { display: flex !important; flex-wrap: wrap !important; align-items: center !important; gap: 6px !important; padding: 6px 10px !important; min-height: 0 !important; overflow: visible !important; }
    .chat-forward-toolbar-repair > [data-forward-target], .chat-forward-toolbar-repair > [data-forward-cancel], .chat-forward-toolbar-repair > [data-forward-send] { display: inline-flex !important; visibility: visible !important; opacity: 1 !important; position: static !important; }
    [data-forward-toolbar][hidden] { display: none !important; }
    [data-forward-toolbar] [data-forward-count] { font-size: 12px !important; line-height: 1.2 !important; }
    [data-forward-toolbar] select, [data-forward-toolbar] button { min-height: 32px !important; padding: 5px 9px !important; }
    [data-forward-toolbar] select { max-width: min(300px, 48vw) !important; }
    @media (max-width: 700px) { .conversation-lock-banner { align-items: flex-start !important; flex-direction: column !important; } .conversation-lock-actions { width: 100% !important; flex-wrap: wrap !important; } .conversation-lock-actions .lock-transfer-select { flex: 1 1 160px !important; max-width: 100% !important; } }
  `;
  document.head.append(style);

})();
