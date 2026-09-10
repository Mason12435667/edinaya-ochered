(() => {
  "use strict";
  const VERSION = "3.3.106";
  const KEY = "queue-interface-settings";
  const clamp = (n, min, max) => Math.min(max, Math.max(min, Number(n) || min));
  const readSettings = () => {
    try { return {...JSON.parse(localStorage.getItem(KEY) || "{}")}; }
    catch (_) { return {}; }
  };
  const settings = readSettings();
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(settings)); } catch (_) {} };
  const toast = (message) => window.QueueUI?.toast?.(message) || console.info(message);

  function humanError(value) {
    const raw = String(value?.message || value || "").trim();
    const lower = raw.toLocaleLowerCase("ru");
    if (!raw) return "Неизвестная ошибка. Подробности сохранены в журнале.";
    if (value?.name === "AbortError" || lower.includes("aborted")) return "Операция отменена сотрудником.";
    if (lower.includes("failed to fetch") || lower.includes("networkerror") || lower.includes("нет связи")) return "Нет связи с сервером. Проверьте сеть и доступность «Единой очереди».";
    if (lower.includes("whatsapp") && (lower.includes("недоступ") || lower.includes("offline") || lower.includes("connector"))) return "WhatsApp недоступен. Проверьте состояние коннектора в верхней панели или админке.";
    if (lower.includes("413") || lower.includes("слишком большой") || lower.includes("превышает лимит")) return "Файл превышает разрешённый размер.";
    if (lower.includes("timeout") || lower.includes("истекло время")) return "Истекло время ожидания ответа. Попробуйте ещё раз.";
    if (lower.includes("database is locked") || lower.includes("база занята")) return "База данных занята другой операцией. Повторите действие через несколько секунд.";
    return raw;
  }
  window.QueueInterface105 = {version: VERSION, humanError};

  // Only translate uncaught network-style failures. Existing application dialogs are not replaced.
  window.addEventListener("unhandledrejection", (event) => {
    const text = String(event.reason?.message || event.reason || "");
    if (/failed to fetch|networkerror/i.test(text)) toast(humanError(event.reason));
  });

  function installScaleControl() {
    const topbar = document.querySelector(".topbar");
    if (!topbar || topbar.querySelector("[data-uq-scale]")) return;
    const wrap = document.createElement("label");
    wrap.className = "uq-scale-control";
    wrap.title = "Масштаб интерфейса";
    const select = document.createElement("select");
    select.dataset.uqScale = "1";
    select.setAttribute("aria-label", "Масштаб интерфейса");
    [90, 100, 110, 125].forEach((value) => {
      const option = document.createElement("option");
      option.value = String(value); option.textContent = `${value}%`; select.append(option);
    });
    const value = [90,100,110,125].includes(Number(settings.scale)) ? Number(settings.scale) : 100;
    select.value = String(value);
    const apply = () => {
      const zoom = Number(select.value) || 100;
      document.documentElement.style.zoom = `${zoom}%`;
      settings.scale = zoom; save();
    };
    select.addEventListener("change", apply);
    wrap.append(select);
    const theme = topbar.querySelector("[data-theme-toggle]");
    if (theme) theme.before(wrap); else topbar.append(wrap);
    apply();
  }
  installScaleControl();

  // 1.00.3: app.js is the only owner of chat context-menu positioning.
  // The old secondary repositioner could briefly place a fixed menu beyond the
  // viewport (especially with UI scale/zoom), which expanded the document and
  // made the conversation jump horizontally. We only suppress the browser's
  // native menu here and let the row handler in app.js place our menu safely.
  document.addEventListener("contextmenu", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest(".chat-message-row[data-message-id]")) event.preventDefault();
  }, true);

  const conversationPage = document.querySelector("[data-conversation-page]");
  if (!conversationPage) return;

  const sidebar = conversationPage.querySelector(".chat-sidebar");
  const main = conversationPage.querySelector(".chat-main");
  const list = conversationPage.querySelector("[data-chat-list]");
  const search = conversationPage.querySelector("[data-chat-search]");
  const messages = conversationPage.querySelector("[data-chat-messages]");
  const composer = conversationPage.querySelector("[data-chat-composer]");
  const headerActions = conversationPage.querySelector(".chat-header-actions");
  const isGroups = location.pathname === "/groups" || !conversationPage.hasAttribute("data-whatsapp-page");
  if (!sidebar || !main || !list || !messages || !composer) return;

  document.body.classList.add("uq-interface-105");
  const pathKey = isGroups ? "groups" : "whatsapp";
  settings.pages ||= {};
  settings.pages[pathKey] ||= {};
  const pageSettings = settings.pages[pathKey];

  function applyCompact() {
    conversationPage.classList.toggle("uq-compact", Boolean(pageSettings.compact));
    if (compactBtn) compactBtn.setAttribute("aria-pressed", String(Boolean(pageSettings.compact)));
  }
  function applySidebar() {
    conversationPage.classList.toggle("uq-sidebar-collapsed", Boolean(pageSettings.sidebarCollapsed));
    if (!pageSettings.sidebarCollapsed) {
      const width = clamp(pageSettings.sidebarWidth || 330, 240, Math.min(520, window.innerWidth * 0.42));
      conversationPage.style.setProperty("--uq-sidebar-width", `${width}px`);
    }
    collapseBtn.textContent = pageSettings.sidebarCollapsed ? "Показать чаты" : "Скрыть чаты";
    requestAnimationFrame(placeResizeHandle);
  }

  const head = sidebar.querySelector(".chat-sidebar-head") || sidebar;
  const controls = document.createElement("div");
  controls.className = "uq-view-controls";
  const compactBtn = document.createElement("button");
  compactBtn.type = "button"; compactBtn.className = "button compact ghost uq-view-button"; compactBtn.textContent = "Компактно";
  compactBtn.addEventListener("click", () => { pageSettings.compact = !pageSettings.compact; save(); applyCompact(); });
  const collapseBtn = document.createElement("button");
  collapseBtn.type = "button"; collapseBtn.className = "button compact ghost uq-view-button";
  collapseBtn.addEventListener("click", () => { pageSettings.sidebarCollapsed = !pageSettings.sidebarCollapsed; save(); applySidebar(); });
  controls.append(compactBtn, collapseBtn);
  head.append(controls);

  const resizeHandle = document.createElement("div");
  resizeHandle.className = "uq-sidebar-resize";
  resizeHandle.title = "Потяните мышью, чтобы изменить ширину списка";
  resizeHandle.setAttribute("role", "separator");
  resizeHandle.setAttribute("aria-orientation", "vertical");
  conversationPage.append(resizeHandle);
  let resizing = false;
  function placeResizeHandle() {
    if (pageSettings.sidebarCollapsed) { resizeHandle.hidden = true; return; }
    resizeHandle.hidden = false;
    const shellRect = conversationPage.getBoundingClientRect();
    const sideRect = sidebar.getBoundingClientRect();
    resizeHandle.style.left = `${Math.round(sideRect.right - shellRect.left - 3)}px`;
  }
  resizeHandle.addEventListener("pointerdown", (event) => {
    if (pageSettings.sidebarCollapsed) return;
    resizing = true; resizeHandle.setPointerCapture(event.pointerId); document.body.classList.add("uq-resizing");
  });
  resizeHandle.addEventListener("pointermove", (event) => {
    if (!resizing) return;
    const rect = conversationPage.getBoundingClientRect();
    const width = clamp(event.clientX - rect.left, 240, Math.min(520, rect.width * 0.48));
    pageSettings.sidebarWidth = Math.round(width);
    conversationPage.style.setProperty("--uq-sidebar-width", `${pageSettings.sidebarWidth}px`);
    placeResizeHandle();
  });
  const finishResize = () => { if (!resizing) return; resizing = false; document.body.classList.remove("uq-resizing"); save(); };
  resizeHandle.addEventListener("pointerup", finishResize);
  resizeHandle.addEventListener("pointercancel", finishResize);
  window.addEventListener("resize", placeResizeHandle);
  // The sidebar must always remain available. 3.3.105 could persist a hidden
  // state in localStorage; clear it once and remove the hide/show control.
  if (pageSettings.sidebarCollapsed) {
    pageSettings.sidebarCollapsed = false;
    save();
  }
  applyCompact(); applySidebar();
  conversationPage.classList.remove("uq-sidebar-collapsed");
  collapseBtn.remove();
  resizeHandle.hidden = false;
  requestAnimationFrame(placeResizeHandle);

  // Chat tabs. Personal/group navigation stays explicit; the rest are safe DOM filters.
  const tabs = document.createElement("div");
  tabs.className = "uq-chat-tabs";
  const tabDefs = [
    ["all", "Все"], ["private", "Личные"], ["groups", "Группы"], ["unread", "Непрочитанные"], ["needs", "Нужен ответ"], ["favorite", "Закреплённые"],
  ];
  let activeTab = String(pageSettings.tab || "all");
  const needsButton = sidebar.querySelector(".reply-queue-toggle");
  // Keep the existing live counter, but move it into normal document flow.
  // This prevents it from covering the first chat after the new tabs were added.
  if (needsButton) {
    needsButton.classList.add("uq-needs-flow");
    tabs.after(needsButton);
  }
  function setNeedsQueue(wanted) {
    if (!needsButton || isGroups) return;
    const current = needsButton.getAttribute("aria-pressed") === "true";
    if (current !== wanted) needsButton.click();
  }
  function applyListFilter() {
    const rows = [...list.querySelectorAll(".chat-list-item")];
    rows.forEach((row) => {
      let visible = true;
      if (activeTab === "unread") visible = Boolean(row.querySelector(".chat-unread,.chat-mention"));
      else if (activeTab === "favorite") visible = Boolean(row.querySelector(".chat-favorite-toggle.active"));
      row.classList.toggle("uq-filter-hidden", !visible);
    });
    tabs.querySelectorAll("button[data-tab]").forEach((button) => button.classList.toggle("active", button.dataset.tab === activeTab));
  }
  function chooseTab(tab) {
    if (tab === "private" && isGroups) { location.href = "/whatsapp"; return; }
    if (tab === "groups" && !isGroups) { location.href = "/groups"; return; }
    if (tab === "needs" && isGroups) { location.href = "/whatsapp"; return; }
    activeTab = tab === "private" || tab === "groups" ? "all" : tab;
    pageSettings.tab = activeTab; save();
    setNeedsQueue(activeTab === "needs");
    requestAnimationFrame(applyListFilter);
  }
  tabDefs.forEach(([id,label]) => {
    const button = document.createElement("button"); button.type = "button"; button.dataset.tab = id; button.textContent = label;
    if ((id === "private" && !isGroups) || (id === "groups" && isGroups)) button.classList.add("current-kind");
    button.addEventListener("click", () => chooseTab(id)); tabs.append(button);
  });
  (search || head).after(tabs);
  if (activeTab === "needs") setNeedsQueue(true);
  const listObserver = new MutationObserver(() => requestAnimationFrame(applyListFilter));
  listObserver.observe(list, {childList:true, subtree:true, attributes:true, attributeFilter:["class"]});
  applyListFilter();

  // Date separators and a floating date label. We only read existing message timestamps.
  const floatingDate = document.createElement("div");
  floatingDate.className = "uq-floating-date"; floatingDate.hidden = true;
  main.append(floatingDate);
  const dateLabel = (key) => {
    if (!key) return "";
    const [dd,mm] = key.split(".").map(Number);
    const now = new Date();
    const today = `${String(now.getDate()).padStart(2,"0")}.${String(now.getMonth()+1).padStart(2,"0")}`;
    const yesterdayDate = new Date(now.getFullYear(), now.getMonth(), now.getDate()-1);
    const yesterday = `${String(yesterdayDate.getDate()).padStart(2,"0")}.${String(yesterdayDate.getMonth()+1).padStart(2,"0")}`;
    if (key === today) return "Сегодня";
    if (key === yesterday) return "Вчера";
    return `${String(dd).padStart(2,"0")}.${String(mm).padStart(2,"0")}.${now.getFullYear()}`;
  };
  const rowDateKey = (row) => {
    const text = String(row.querySelector("time")?.textContent || "");
    const m = text.match(/(\d{2})\.(\d{2})/); return m ? `${m[1]}.${m[2]}` : "";
  };
  function decorateDates() {
    const rows = [...messages.querySelectorAll(":scope > .chat-message-row")];
    let previous = "";
    rows.forEach((row) => {
      const key = rowDateKey(row);
      const prior = row.previousElementSibling;
      const needs = key && key !== previous;
      if (needs) {
        if (!prior?.classList.contains("uq-date-separator") || prior.dataset.dateKey !== key) {
          const sep = document.createElement("div"); sep.className = "uq-date-separator"; sep.dataset.dateKey = key; sep.textContent = dateLabel(key);
          row.before(sep);
        } else prior.textContent = dateLabel(key);
      } else if (prior?.classList.contains("uq-date-separator")) prior.remove();
      if (key) previous = key;
    });
    [...messages.querySelectorAll(":scope > .uq-date-separator")].forEach((sep) => {
      const next = sep.nextElementSibling;
      if (!next?.classList.contains("chat-message-row") || rowDateKey(next) !== sep.dataset.dateKey) sep.remove();
    });
    updateFloatingDate();
  }
  function updateFloatingDate() {
    const box = messages.getBoundingClientRect();
    const rows = [...messages.querySelectorAll(":scope > .chat-message-row")];
    const visible = rows.find((row) => row.getBoundingClientRect().bottom > box.top + 8);
    const key = visible ? rowDateKey(visible) : "";
    floatingDate.hidden = !key; if (key) floatingDate.textContent = dateLabel(key);
  }
  const messageObserver = new MutationObserver((mutations) => {
    const relevant = mutations.some((m) => [...m.addedNodes, ...m.removedNodes].some((n) => n.nodeType === 1 && (n.classList?.contains("chat-message-row") || n.classList?.contains("uq-date-separator"))));
    if (relevant) requestAnimationFrame(decorateDates);
  });
  messageObserver.observe(messages, {childList:true});
  messages.addEventListener("scroll", updateFloatingDate, {passive:true});
  requestAnimationFrame(decorateDates);

  // Read-only information drawer and active-ticket strip.
  const infoButton = document.createElement("button");
  infoButton.type = "button"; infoButton.className = "button compact ghost"; infoButton.textContent = "Инфо";
  const infoPanel = document.createElement("aside");
  infoPanel.className = "uq-chat-info"; infoPanel.hidden = !Boolean(pageSettings.infoOpen);
  infoPanel.innerHTML = `<div class="uq-info-head"><strong>Информация о чате</strong><button type="button" data-uq-info-close aria-label="Закрыть">×</button></div><div data-uq-info-body class="uq-info-body"><span class="muted">Выберите чат</span></div>`;
  main.append(infoPanel);
  const setInfoOpen = (open) => { pageSettings.infoOpen = Boolean(open); save(); infoPanel.hidden = !open; infoButton.classList.toggle("active", Boolean(open)); };
  infoButton.addEventListener("click", () => setInfoOpen(infoPanel.hidden));
  infoPanel.querySelector("[data-uq-info-close]").addEventListener("click", () => setInfoOpen(false));
  headerActions?.append(infoButton);
  setInfoOpen(Boolean(pageSettings.infoOpen));

  const ticketsStrip = document.createElement("div");
  ticketsStrip.className = "uq-active-tickets-strip"; ticketsStrip.hidden = true;
  composer.before(ticketsStrip);
  let latestState = null;
  let lastContextSignature = "";
  const escapeText = (value) => String(value || "");
  function prettyPhone(value, chatId) {
    const digits = String(value || chatId || "").replace(/@.*$/,"").replace(/\D/g,"");
    return digits ? `+${digits}` : "Не указан";
  }
  function renderContext(state) {
    latestState = state || latestState || {};
    const profile = latestState.profile || {};
    const context = latestState.ui_context || {};
    const active = Array.isArray(context.active_tickets) ? context.active_tickets : [];
    const title = String(document.querySelector("[data-chat-title]")?.textContent || profile.name || "Чат");
    const signature = JSON.stringify([latestState.selected_chat_id, title, profile.name, profile.phone, context.phone, active]);
    if (signature === lastContextSignature) return;
    lastContextSignature = signature;
    const body = infoPanel.querySelector("[data-uq-info-body]");
    body.replaceChildren();
    const profileCard = document.createElement("section"); profileCard.className = "uq-info-card";
    const h = document.createElement("strong"); h.textContent = title;
    const p = document.createElement("small"); p.textContent = prettyPhone(profile.phone || context.phone, latestState.selected_chat_id);
    profileCard.append(h,p); body.append(profileCard);
    const quick = document.createElement("div"); quick.className = "uq-info-actions";
    [
      ["Медиа", "[data-media-browser-toggle],button", "медиа"],
      ["Теги / заметка", "button", "теги"],
      ["Обращения", "button", "обращения"],
      ["Новая заявка", "button", "заявка"],
    ].forEach(([label, selector, needle]) => {
      const source = [...document.querySelectorAll(selector)].find((el) => el !== infoButton && String(el.textContent || "").toLocaleLowerCase("ru").includes(needle));
      if (!source) return;
      const b = document.createElement("button"); b.type="button"; b.textContent=label; b.addEventListener("click",()=>source.click()); quick.append(b);
    });
    if (quick.children.length) body.append(quick);
    const tickets = document.createElement("section"); tickets.className = "uq-info-card";
    const th = document.createElement("strong"); th.textContent = `Открытые заявки: ${active.length}`; tickets.append(th);
    if (!active.length) { const none=document.createElement("small"); none.className="muted"; none.textContent="Открытых заявок нет"; tickets.append(none); }
    active.forEach((ticket) => {
      const a=document.createElement("a"); a.className="uq-ticket-link"; a.href=`/ticket?id=${encodeURIComponent(ticket.id)}`;
      const top=document.createElement("span"); top.textContent=`№${ticket.id} · ${escapeText(ticket.category || "Заявка")}`;
      const meta=document.createElement("small"); meta.textContent=[ticket.status,ticket.priority].filter(Boolean).join(" · ") || "Открыта";
      a.append(top,meta); tickets.append(a);
    });
    body.append(tickets);

    ticketsStrip.replaceChildren();
    if (active.length) {
      const label=document.createElement("strong"); label.textContent="Открытые заявки:"; ticketsStrip.append(label);
      active.slice(0,4).forEach((ticket)=>{ const a=document.createElement("a"); a.href=`/ticket?id=${encodeURIComponent(ticket.id)}`; a.textContent=`№${ticket.id} ${escapeText(ticket.category || "")}`.trim(); ticketsStrip.append(a); });
      ticketsStrip.hidden=false;
    } else ticketsStrip.hidden=true;
  }

  // Observe the same read-only state calls the existing UI already makes. The response returned to app.js is untouched.
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    try {
      const raw = typeof args[0] === "string" ? args[0] : args[0]?.url || "";
      const url = new URL(raw, location.origin);
      if (url.pathname === "/api/chat-state" || url.pathname === "/api/group-state") {
        response.clone().json().then((state) => {
          if (state && typeof state === "object") {
            renderContext(state);
            const selected = String(state.selected_chat_id || "");
            if (selected) { pageSettings.lastChatId = selected; save(); }
          }
        }).catch(()=>{});
      }
    } catch (_) {}
    return response;
  };

  // Upload progress presentation. The actual chunk upload/cancel remains in app.js.
  const fileInput = composer.querySelector("[data-media-input]");
  const composeStatus = composer.querySelector(".compose-status") || (() => {
    const el=document.createElement("div"); el.className="compose-status"; composer.append(el); return el;
  })();
  const progress = document.createElement("div"); progress.className="uq-upload-progress"; progress.hidden=true;
  progress.innerHTML = `<div class="uq-upload-progress-top"><span data-uq-upload-label>Вложение</span><b data-uq-upload-percent></b></div><div class="uq-upload-track"><i data-uq-upload-bar></i></div>`;
  composeStatus.before(progress);
  const progressLabel=progress.querySelector("[data-uq-upload-label]"); const progressPercent=progress.querySelector("[data-uq-upload-percent]"); const progressBar=progress.querySelector("[data-uq-upload-bar]");
  const humanSize=(bytes)=>{ let n=Number(bytes)||0; const units=["Б","КБ","МБ","ГБ"]; let i=0; while(n>=1024&&i<units.length-1){n/=1024;i++;} return `${i? n.toFixed(n>=10?1:2):Math.round(n)} ${units[i]}`; };
  function updateUploadProgress() {
    const file=fileInput?.files?.[0]; const text=String(composeStatus.textContent||""); const m=text.match(/Загрузка файла:\s*(\d+)%/i);
    if (!file && !m) { progress.hidden=true; return; }
    const percent=m ? clamp(Number(m[1]),0,100) : 0;
    progress.hidden=false; progressLabel.textContent=file ? `${file.name} · ${humanSize(file.size)}` : "Вложение";
    progressPercent.textContent=m ? `${percent}%` : ""; progressBar.style.width=`${percent}%`;
    progress.classList.toggle("indeterminate", !m && /подготовка|отправка/i.test(text));
    if (/в очереди|отменена|не удалось/i.test(text)) window.setTimeout(()=>{ if(!fileInput?.files?.[0]) progress.hidden=true; },2200);
  }
  fileInput?.addEventListener("change", updateUploadProgress);
  new MutationObserver(updateUploadProgress).observe(composeStatus,{childList:true,subtree:true,characterData:true});
  updateUploadProgress();

  // Keep layout controls in the saved state without touching business logic.
  document.querySelectorAll(".topbar nav a").forEach((link) => link.addEventListener("click", () => { settings.lastSection = link.getAttribute("href") || "/"; save(); }));
  requestAnimationFrame(placeResizeHandle);
})();
