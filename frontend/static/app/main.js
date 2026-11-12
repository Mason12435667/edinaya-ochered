// Список заявок, контекстное меню заявки, тема, обновление в реальном времени и запуск остальных
// частей в прежнем порядке: просмотр медиа, чат, уведомления.
(() => {
  const {queueAlert, postForm} = window.QueueCommon;
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
        tableBody.innerHTML = window.QueueTickets.rows(data.rows, data.options.priorities);
        if (pagination) pagination.innerHTML = window.QueueTickets.pagination(data.pagination);
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
        contactSearchResults.innerHTML = window.QueueAdmin.contactRows(data.rows);
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
  window.QueueLightbox.setup();
  window.QueueConversation.start();
  window.QueueNotificationCenter.start();
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
