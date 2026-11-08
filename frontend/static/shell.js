// Frame of a static page: draws the header from /api/page-context. Runs first of the
// deferred scripts, which expect the CSRF token and the header to be in place.
(() => {
  "use strict";

  // Synchronous on purpose. app.js asks for chat data as soon as it runs, and the
  // scripts after it must already listen for the answer: with deferred scripts they
  // all run before any response arrives. Waiting for the context asynchronously and
  // loading them afterwards loses that order.
  function getJson(url) {
    const request = new XMLHttpRequest();
    request.open("GET", url, false);
    request.send();
    const finalUrl = new URL(request.responseURL || "/login", location.href);
    if (finalUrl.pathname !== url || request.status !== 200) {
      if (finalUrl.pathname === "/login") finalUrl.search = `?next=${encodeURIComponent(location.pathname + location.search)}`;
      location.replace(finalUrl.href);
      return null;
    }
    return JSON.parse(request.responseText);
  }

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

  function passwordForm(context) {
    if (!context.user.can_change_password) return "";
    return `<form class="auth-profile-password" method="post" action="/account/password">
      <input type="hidden" name="csrf_token" value="${escapeHtml(context.csrf_token)}">
      <input type="hidden" name="next" value="/">
      <label>Текущий пароль<input type="password" name="current_password" autocomplete="current-password" required></label>
      <label>Новый пароль<input type="password" name="new_password" minlength="10" autocomplete="new-password" required></label>
      <label>Повторите пароль<input type="password" name="new_password2" minlength="10" autocomplete="new-password" required></label>
      <button class="button primary" type="submit">Сменить пароль</button>
    </form>`;
  }

  function header(context, active) {
    const nav = context.nav.map((item) =>
      `<a class="nav-link ${item.key === active ? "active" : ""}" href="${escapeHtml(item.href)}">${escapeHtml(item.label)}</a>`).join("");
    const user = context.user;
    return `<header class="topbar">
      <a class="brand" href="/"><span class="brand-mark">Q</span><span>Единая очередь</span></a>
      ${context.local_mode ? '<span class="local-mode-badge">WINDOWS · ТЕСТ</span>' : ""}
      <nav>${nav}</nav>
      <button class="notification-button" type="button" data-notification-center aria-label="Уведомления">
        <span aria-hidden="true">🔔</span><b data-notification-count hidden>0</b>
      </button>
      <div class="notification-popover" data-notification-popover hidden>
        <strong>Уведомления</strong><div data-notification-items>Проверка новых событий...</div>
      </div>
      <details class="auth-profile-menu">
        <summary class="auth-user-chip" title="Профиль"><strong>${escapeHtml(user.name)}</strong><small>${escapeHtml(user.role_label)}</small></summary>
        <div class="auth-profile-popover">
          <div class="auth-profile-head"><strong>${escapeHtml(user.name)}</strong><span>@${escapeHtml(user.username)}</span><small>${escapeHtml(user.shift_note)}</small></div>
          ${passwordForm(context)}
          <a class="button ghost auth-profile-page" href="/me">Моя страница</a>
          <a class="button ghost auth-profile-page" href="/accounts">Сменить аккаунт</a>
          <a class="button ghost auth-profile-logout" href="/logout">Выйти</a>
        </div>
      </details>
      <button class="theme-toggle" type="button" data-theme-toggle aria-label="Сменить оформление">Тёмная тема</button>
    </header>
    <div class="toast-stack" data-toast-stack aria-live="polite" aria-atomic="false"></div>
    ${context.maintenance ? '<div class="maintenance-banner">Режим обслуживания включён: просмотр доступен, изменения сотрудников временно заблокированы.</div>' : ""}`;
  }

  function initialChatId() {
    const value = (new URLSearchParams(location.search).get("chat_id") || "").trim().slice(0, 120);
    if (document.body.dataset.page !== "groups") return value;
    return /^[A-Za-z0-9_.:-]+@g\.us$/.test(value) ? value : "";
  }

  function drawFrame(context) {
    document.querySelector('meta[name="queue-csrf"]').content = context.csrf_token;
    if (context.theme) document.documentElement.dataset.theme = context.theme;
    document.body.dataset.admin = context.show_admin ? "1" : "0";
    document.body.dataset.authUserId = String(context.user.id);
    document.querySelector("main.page").insertAdjacentHTML("beforebegin", header(context, document.body.dataset.page));
    document.querySelector("main.page").insertAdjacentHTML("afterend", "<footer>Единая очередь · рабочая система обработки заявок</footer>");
    document.querySelector("[data-conversation-page]").dataset.initialChatId = initialChatId();
  }

  const context = getJson("/api/page-context");
  if (!context) return;
  window.queuePage = Object.freeze({context, getJson});
  drawFrame(context);
})();
