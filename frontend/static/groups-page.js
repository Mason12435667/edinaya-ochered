// The list of WhatsApp groups and its administrator forms on the static groups page.
(() => {
  "use strict";
  if (!window.queuePage) return;
  const {context, getJson} = window.queuePage;
  const query = new URLSearchParams(location.search);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

  function adminForm(action, button, chatId = "") {
    const chat = chatId ? `<input type="hidden" name="chat_id" value="${escapeHtml(chatId)}">` : "";
    return `<form method="post" action="/admin/groups"${action === "delete" ? " data-confirm-delete" : ""}>
      <input type="hidden" name="csrf_token" value="${escapeHtml(context.csrf_token)}">
      <input type="hidden" name="action" value="${action}">
      ${chat}${button}
    </form>`;
  }

  function syncStatus(status) {
    if (!status) return "";
    const errors = status.errors.length
      ? `<small class="muted">Часть способов поиска вернула ошибку: ${escapeHtml(status.errors.join(" | "))}</small>` : "";
    return `<div class="notice">Последнее сканирование WhatsApp: найдено групп <strong>${status.groups}</strong>. Чаты: ${status.chats_groups}/${status.chats_total}, контакты: ${status.contacts_groups}/${status.contacts_total}. ${escapeHtml(status.updated_at)}${errors}</div>`;
  }

  function discoveredTable(groups) {
    if (!groups.length) return '<p class="muted">Доступных для добавления групп пока нет. Нажмите «Обновить список из WhatsApp».</p>';
    const rows = groups.map((group) => `<tr>
      <td><strong>${escapeHtml(group.name)}</strong></td>
      <td>${group.participant_count || "Не определено"}</td>
      <td>${adminForm("add_discovered", '<button class="button primary" type="submit">Добавить</button>', group.chat_id)}</td>
    </tr>`).join("");
    return `<div class="table-scroll"><table><thead><tr><th>Группа WhatsApp</th><th>Участники</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function adminPanel(admin) {
    return `<section class="panel manual-panel admin-contact-form">
      <div class="section-heading">
        <div><h2>Добавить группу из WhatsApp</h2><p>Система сама получает группы рабочего аккаунта. ID искать и вводить не нужно.</p></div>
        ${adminForm("refresh", '<button class="button primary" type="submit">Обновить список из WhatsApp</button>')}
      </div>
      ${syncStatus(admin.sync_status)}
      ${discoveredTable(admin.discovered)}
    </section>`;
  }

  function avatar(group) {
    const image = group.avatar_url ? `<img src="${escapeHtml(group.avatar_url)}" alt="" loading="lazy">` : "";
    return `<span class="chat-avatar table-avatar">${escapeHtml(group.initials)}${image}</span>`;
  }

  function groupRow(group) {
    const remove = context.show_admin ? adminForm("delete", '<button class="button ghost" type="submit">Удалить</button>', group.chat_id) : "";
    return `<tr><td><a class="ticket-title-link avatar-name" href="/groups?chat_id=${encodeURIComponent(group.chat_id)}">${avatar(group)}<strong>${escapeHtml(group.name)}</strong></a></td><td>${group.participant_count || "Не определено"}</td><td>${escapeHtml(group.updated_at)}</td><td>${remove}</td></tr>`;
  }

  function drawNotice() {
    const slot = document.querySelector("[data-groups-notice]");
    const text = query.get("notice");
    if (!text) {
      slot.remove();
      return;
    }
    slot.removeAttribute("data-groups-notice");
    slot.className = "notice";
    slot.textContent = text;
  }

  function bindTable(table) {
    table.addEventListener("error", (event) => {
      if (event.target.tagName === "IMG") event.target.remove();
    }, true);
    table.addEventListener("submit", (event) => {
      if (event.target.matches("[data-confirm-delete]") && !confirm("Удалить группу из системы?")) event.preventDefault();
    });
  }

  function draw() {
    drawNotice();
    if (context.show_admin && query.get("refresh") === "1") {
      setTimeout(() => { location.href = "/groups"; }, 7000);
    }
    const data = getJson("/api/groups-page");
    if (!data) return;
    const panel = document.querySelector("[data-groups-admin]");
    if (data.admin) panel.outerHTML = adminPanel(data.admin);
    else panel.remove();
    const table = document.querySelector("[data-groups-table]");
    table.innerHTML = data.groups.map(groupRow).join("")
      || '<tr><td colspan="4" class="empty">Группы пока не добавлены</td></tr>';
    bindTable(table);
  }

  draw();
})();
