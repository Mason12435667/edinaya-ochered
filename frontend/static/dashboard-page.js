// Страница списка заявок: заполняет статическую разметку из /api/dashboard-data.
(() => {
  "use strict";
  if (!window.queuePage) return;
  const {context, escapeHtml, getJson} = window.queuePage;
  const render = window.QueueTickets;
  const qs = (selector) => document.querySelector(selector);

  const METRICS = [["all", "Всего"], ["new", "Не тронуты"], ["in_progress", "В работе"], ["done", "Сделано"], ["invalid", "Недействительные"]];

  function metrics(counts) {
    return METRICS.map(([key, label]) => `<a class="metric metric-${key}" href="/${key === "all" ? "" : `?status=${key}`}">
      <span>${label}</span><strong data-count-status="${key}">${Number(counts[key] || 0)}</strong>
    </a>`).join("");
  }

  function shift(data) {
    const previous = data.shift.previous_employee
      ? `<small>Предыдущая смена: ${escapeHtml(data.shift.previous_employee)}</small>`
      : "<small>Передача начнёт заполняться при смене сотрудника</small>";
    const cards = data.shift.views.map((view) =>
      `<a class="shift-metric ${view.key === data.filters.view ? "active" : ""}" href="/?view=${view.key}"><span>${escapeHtml(view.label)}</span><strong>${Number(view.count)}</strong></a>`).join("");
    return `<div><strong>Текущая смена: ${escapeHtml(data.shift.employee)}</strong>${previous}</div><div class="shift-metrics">${cards}</div>`;
  }

  function handoffBoard(items) {
    if (!items.length) return "";
    const links = items.map((item) =>
      `<a class="handoff-item" href="/ticket?id=${Number(item.id)}"><strong>#${Number(item.id)} · ${escapeHtml(item.title)}</strong><span>${escapeHtml(item.sender)}</span><small>Передал: ${escapeHtml(item.handoff_from)} · ${escapeHtml(item.handoff_at)}</small></a>`).join("");
    return `<section class="panel handoff-board"><div class="section-heading"><div><p class="eyebrow">Передача смены</p><h2>Переданные заявки <span class="handoff-badge large">${items.length}</span></h2><p>Отдельный список заявок, которые ждут следующую смену</p></div></div><div class="handoff-list">${links}</div></section>`;
  }

  function fillFilters(data) {
    const form = qs("[data-dashboard-filter-form]");
    const {filters, options} = data;
    form.elements.view.setAttribute("value", filters.view);
    form.elements.q.setAttribute("value", filters.q);
    form.elements.no_answer_minutes.setAttribute("value", filters.no_answer_minutes || "");
    form.elements.status.innerHTML = render.options(options.statuses, filters.status, '<option value="">Все статусы</option>');
    form.elements.category.innerHTML = render.options(options.categories, filters.category, '<option value="">Все категории</option>');
    form.elements.priority.innerHTML = render.options(options.priorities, filters.priority, '<option value="">Все приоритеты</option>');
  }

  function fillManualForm(options) {
    const form = qs(".manual-form");
    form.elements.csrf_token.setAttribute("value", context.csrf_token);
    form.elements.priority.innerHTML = render.options(options.priorities, "normal");
    form.elements.employee.innerHTML = render.employeeOptions(options.employees);
  }

  const data = getJson(`/api/dashboard-data${location.search}`);
  if (!data) return;
  render.showNotice(qs("[data-page-notice]"));
  qs(".metrics").innerHTML = metrics(data.counts);
  qs(".smart-shift-panel").innerHTML = shift(data);
  qs("[data-handoff-board]").outerHTML = handoffBoard(data.handoff);
  fillFilters(data);
  qs("[data-dashboard]").dataset.dashboardVersion = data.version;
  qs("#ticket-table-body").innerHTML = render.rows(data.rows, data.options.priorities);
  qs("#ticket-pagination").innerHTML = render.pagination(data.pagination);
  fillManualForm(data.options);
})();
