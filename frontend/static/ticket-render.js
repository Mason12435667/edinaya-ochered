// Markup shared by the ticket list and the ticket card. Everything from the server is escaped here.
(() => {
  "use strict";
  if (!window.queuePage) return;
  const {escapeHtml} = window.queuePage;

  const options = (pairs, selected, first = "") => first + pairs.map(([key, label]) =>
    `<option value="${escapeHtml(key)}" ${key === selected ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");

  const statusBadge = (status, label) => `<span class="status status-${escapeHtml(status)}">${escapeHtml(label)}</span>`;

  const priorityBadge = (priority, label) => `<span class="priority priority-${escapeHtml(priority)}">${escapeHtml(label)}</span>`;

  const slaBadge = (sla) => sla
    ? `<span class="sla-badge ${escapeHtml(sla.class)}" data-sla-deadline="${Number(sla.due_epoch)}" data-sla-active="1">${escapeHtml(sla.label)}</span>`
    : "";

  function employeeOptions(employees) {
    return employees.names.map((name) =>
      `<option value="${escapeHtml(name)}" ${name === employees.selected ? "selected" : ""}>${escapeHtml(name)}</option>`).join("");
  }

  function row(ticket, priorities) {
    const id = Number(ticket.id);
    const title = escapeHtml(ticket.title);
    const assigned = escapeHtml(ticket.assigned_to);
    return `<tr class="${escapeHtml(ticket.row_class)}" data-ticket-row data-revision="${Number(ticket.revision)}" data-ticket-id="${id}" data-ticket-title="${title}" data-assigned-to="${assigned}" data-ticket-status="${escapeHtml(ticket.status)}">
      <td class="priority-cell"><select class="priority-select priority-select-${escapeHtml(ticket.priority)}" data-quick-priority data-ticket-id="${id}" aria-label="Приоритет заявки #${id}">${options(priorities, ticket.priority)}</select></td>
      <td class="ticket-number">#${id}</td>
      <td><span class="source source-${escapeHtml(ticket.source)}">${escapeHtml(ticket.source_label)}</span></td>
      <td><strong>${escapeHtml(ticket.sender)}</strong><small>${escapeHtml(ticket.phone)}</small></td>
      <td><a class="ticket-title-link" href="/ticket?id=${id}"><strong>${title}</strong></a><small class="summary">${escapeHtml(ticket.summary)}</small></td>
      <td>${statusBadge(ticket.status, ticket.status_label)}${ticket.shift_handoff ? '<span class="handoff-badge">Следующая смена</span>' : ""}${slaBadge(ticket.sla)}<small>${assigned}</small></td>
      <td><time>${escapeHtml(ticket.created_at)}</time></td>
    </tr>`;
  }

  function rows(tickets, priorities) {
    return tickets.map((ticket) => row(ticket, priorities)).join("")
      || '<tr><td colspan="7" class="empty">Заявок пока нет. Запусти WhatsApp QR-коннектор или добавь заявку вручную.</td></tr>';
  }

  function pagination(data) {
    if (data.pages <= 1) return `<span class="pagination-total">Всего заявок: ${Number(data.total)}</span>`;
    const edge = (url, label) => url
      ? `<a class="pagination-link" href="${escapeHtml(url)}">${label}</a>`
      : `<span class="pagination-link disabled">${label}</span>`;
    const links = data.items.map((item) => item.gap
      ? '<span class="pagination-gap">…</span>'
      : `<a class="pagination-link${item.current ? " current" : ""}" href="${escapeHtml(item.url)}">${Number(item.number)}</a>`).join("");
    return `<span class="pagination-total">Страница ${Number(data.page)} из ${Number(data.pages)} · заявок ${Number(data.total)}</span><div class="pagination-links">${edge(data.prev_url, "Назад")}${links}${edge(data.next_url, "Вперёд")}</div>`;
  }

  function showNotice(slot) {
    const text = new URLSearchParams(location.search).get("notice");
    if (!text) {
      slot.remove();
      return;
    }
    slot.removeAttribute("data-page-notice");
    slot.className = "notice";
    slot.textContent = text;
  }

  window.QueueTickets = Object.freeze({options, statusBadge, priorityBadge, slaBadge, employeeOptions, rows, pagination, showNotice});
})();
