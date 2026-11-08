// Карточка заявки: рисует всю страницу из /api/ticket-page.
(() => {
  "use strict";
  if (!window.queuePage) return;
  const {context, escapeHtml, getJson} = window.queuePage;
  const render = window.QueueTickets;
  const main = document.querySelector("main.page");

  const data = getJson(`/api/ticket-page${location.search}`);
  if (!data) return;

  function drawMissing() {
    document.title = "Не найдено · Единая очередь";
    main.innerHTML = '<section class="panel empty">Заявка не найдена</section>';
  }

  if (!data.found) {
    drawMissing();
    return;
  }

  const id = Number(data.id);
  // Every form of the card sends the revision, so a stale page cannot overwrite newer changes.
  const ticketFields = `<input type="hidden" name="ticket_id" value="${id}"><input type="hidden" name="revision" value="${Number(data.revision)}"><input type="hidden" name="csrf_token" value="${escapeHtml(context.csrf_token)}">`;

  function mediaCard(media) {
    const name = escapeHtml(media.name);
    const url = escapeHtml(media.url);
    const preview = {
      image: `<button class="media-zoom-button" type="button" data-media-zoom="${url}" data-media-name="${name}"><img class="ticket-media-image" src="${url}" alt="${name}"></button>`,
      audio: `<audio class="ticket-media-audio" controls preload="metadata" src="${url}"></audio>`,
      video: `<video class="ticket-media-video" controls preload="metadata" src="${url}"></video>`,
      file: `<div class="media-file-actions"><a class="button" target="_blank" href="${url}">Открыть</a><a class="button ghost" href="${escapeHtml(`${media.url}&download=1`)}">Скачать</a></div>`,
    }[media.kind];
    const transcript = media.transcript ? `<div class="media-transcript"><strong>Расшифровка:</strong><p>${escapeHtml(media.transcript)}</p></div>` : "";
    return `<article class="ticket-media-card"><div class="attachment-name">${name}</div><small class="ticket-media-meta">${escapeHtml(media.meta)}</small>${preview}${transcript}</article>`;
  }

  function attachments() {
    if (data.media.length) {
      return `<div class="ticket-media-block"><h3>Файлы и медиа по заявке</h3><p class="muted">Все медиа, связанные с этой заявкой, доступны прямо здесь. Фото открываются поверх текущей страницы.</p><div class="ticket-media-grid">${data.media.map(mediaCard).join("")}</div></div>`;
    }
    return data.attachment_name
      ? `<div class="attachment">Вложение: ${escapeHtml(data.attachment_name)}. Медиафайл не был сохранён старой версией системы.</div>` : "";
  }

  function contextFields() {
    if (!data.context_fields.length) return "";
    const rows = data.context_fields.map((field) =>
      `<div class="context-field-row"><span>${escapeHtml(field.label)}</span><strong>${escapeHtml(field.value)}</strong><small>${Number(field.confidence)}% · ${escapeHtml(field.source)}</small></div>`).join("");
    return `<section class="panel context-fields-panel"><h2>Контекстные поля</h2><p class="muted">Данные, накопленные из нескольких сообщений. ФИО и подразделение могут оставаться необязательными.</p><div class="context-field-list">${rows}</div></section>`;
  }

  function historyChange(item) {
    let change = "";
    if (item.field_label) {
      if (item.old_value && item.new_value) change = `<p><strong>${escapeHtml(item.field_label)}</strong>: <del>${escapeHtml(item.old_value)}</del> → ${escapeHtml(item.new_value)}</p>`;
      else if (item.new_value) change = `<p><strong>${escapeHtml(item.field_label)}</strong>: ${escapeHtml(item.new_value)}</p>`;
    } else if (item.text) {
      change = `<p class="context-history-text">${escapeHtml(item.text)}</p>`;
    }
    return item.media_name ? `${change}<p class="context-history-media">Файл: ${escapeHtml(item.media_name)}</p>` : change;
  }

  function historyActions(item) {
    const fieldOptions = data.field_options.map(([key, label]) => `<option value="${escapeHtml(key)}">${escapeHtml(label)}</option>`).join("");
    const form = (mode, controls) => `<form class="context-history-apply" method="post" action="/context-history/apply">${ticketFields}<input type="hidden" name="history_id" value="${Number(item.id)}"><input type="hidden" name="mode" value="${mode}">${controls}</form>`;
    return (item.can_apply_field ? form("field", `<select name="field_name">${fieldOptions}</select><button class="button" type="submit">В поле</button>`) : "")
      + (item.can_attach ? form("media", '<button class="button ghost" type="submit">Привязать сообщение / файл</button>') : "");
  }

  function history() {
    if (!data.history.length) return "";
    const cards = data.history.map((item) =>
      `<article class="context-history-card"><div class="context-history-head"><strong>${escapeHtml(item.event)}</strong><small>${escapeHtml(item.created_at)} · ${escapeHtml(item.source)}</small></div>${historyChange(item)}<div class="context-history-actions">${historyActions(item)}</div></article>`).join("");
    return `<section class="panel context-history-panel"><h2>Подробная история данных</h2><p class="muted">Исходные сообщения, уточнения, исправления и источник каждого значения. Нужную запись можно перенести в поле заявки.</p><div class="context-history-list">${cards}</div></section>`;
  }

  function statusActions() {
    if (data.status_locked) {
      return '<div class="notice warning-notice status-lock-note">Заявка отработана. Изменить её статус может только администратор.</div>';
    }
    return data.statuses.map(([key, label]) =>
      `<button name="status" value="${escapeHtml(key)}" class="status-action action-${escapeHtml(key)}" type="submit">${escapeHtml(label)}</button>`).join("");
  }

  function slaPanel() {
    if (!data.sla) return "";
    if (data.sla.badge === undefined) {
      return `<section class="panel sla-panel"><h2>SLA реакции</h2><strong>Реакция через ${Number(data.sla.reacted_minutes)} мин.</strong></section>`;
    }
    return `<section class="panel sla-panel ${data.sla.overdue ? "sla-panel-overdue" : ""}"><h2>SLA реакции</h2><div>${render.slaBadge(data.sla.badge)}</div><p class="muted">Лимит для приоритета «${escapeHtml(data.sla.priority_label)}»: ${Number(data.sla.limit_minutes)} мин.</p></section>`;
  }

  function handoffPanel() {
    if (!data.handoff) return "";
    const past = data.handoff.history.map((item) =>
      `<p><strong>${escapeHtml(item.actor)}</strong>: ${escapeHtml(item.note)}<br><small>${item.accepted_by ? `Принял: ${escapeHtml(item.accepted_by)}` : "Ожидает принятия"}</small></p>`).join("");
    const heading = `<h2>Передача смене</h2>${past}`;
    if (data.handoff.pending) {
      return `<section class="panel handoff-panel">${heading}<div class="notice warning-notice">Передано следующей смене: ${escapeHtml(data.handoff.from)}<br><small>${escapeHtml(data.handoff.at)}</small></div><form method="post" action="/handoff">${ticketFields}<button class="button primary" name="action" value="accept" type="submit">Принять со смены</button></form></section>`;
    }
    return `<section class="panel handoff-panel">${heading}<p class="muted">Если не успеваете закончить заявку, отметьте её для следующей смены.</p><form method="post" action="/handoff">${ticketFields}<label>Что сделано и что осталось<textarea name="handoff_note" maxlength="2000" required></textarea></label><button class="button" name="action" value="transfer" type="submit">Передать следующей смене</button></form></section>`;
  }

  function adminPanel() {
    if (!context.show_admin) return "";
    return `<section class="panel danger-panel"><h2>Администратор</h2><p class="muted">Удаление происходит только внутри системы. Пользователю ничего не отправляется.</p><form method="post" action="/admin/ticket-delete" data-confirm-delete>${ticketFields}<button class="button danger" type="submit">Удалить заявку</button></form></section>`;
  }

  function notificationPanel() {
    return `<section class="panel"><h2>Уведомление пользователю</h2><strong>${escapeHtml(data.notification.label)}</strong><p>${escapeHtml(data.notification.detail)}</p><small>«Отправлено» не означает, что сообщение прочитано</small></section>`;
  }

  const chatButton = data.chat_id
    ? `<a class="button whatsapp-button" href="/whatsapp?chat_id=${encodeURIComponent(data.chat_id)}">Открыть чат WhatsApp</a>`
    : '<span class="button disabled" title="WhatsApp не передал телефонный номер">Номер для связи не определён</span>';
  const detail = (label, value) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`;
  const events = data.events.map((event) => `<li><span>${escapeHtml(event.action)}</span><small>${escapeHtml(event.actor)} · ${escapeHtml(event.created_at)}</small></li>`).join("");

  document.title = `Заявка #${id} · Единая очередь`;
  main.innerHTML = `<a class="back" href="/">← Все заявки</a>
    <div data-page-notice></div>
    <section class="ticket-header">
      <div><p class="eyebrow">Заявка #${id}</p><h1>${escapeHtml(data.title)}</h1><p>${escapeHtml(data.summary)}</p></div>
      <div class="ticket-badges">${render.priorityBadge(data.priority, data.priority_label)}${render.statusBadge(data.status, data.status_label)}${data.handoff_badge ? '<span class="handoff-badge large">Следующая смена</span>' : ""}</div>
    </section>
    <div class="ticket-layout">
      <section class="panel ticket-main">
        <div class="ticket-main-heading"><h2>Данные запроса</h2>${chatButton}</div>
        <div class="detail-grid">
          ${detail("Источник", data.source)}${detail("Отправитель", data.sender)}${detail("Телефон", data.phone)}${detail("Категория", data.category_label)}${detail("Приоритет", data.priority_label)}${detail("Создана", data.created_at)}
        </div>
        <h3>Исходное сообщение</h3>
        <pre class="message">${escapeHtml(data.original_text)}</pre>
        ${attachments()}${contextFields()}${history()}
      </section>
      <aside>${notificationPanel()}
        <section class="panel actions-panel">
          <h2>Изменить статус</h2>
          <form method="post" action="/status">${ticketFields}<label>Сотрудник<select name="employee">${render.employeeOptions(data.employees)}</select></label><div class="status-actions">${statusActions()}</div></form>
        </section>
        ${slaPanel()}${handoffPanel()}
        <section class="panel classification-panel">
          <h2>Приоритет</h2>
          <form method="post" action="/classification">${ticketFields}<label>Приоритет<select name="priority">${render.options(data.priorities, data.priority)}</select></label><button class="button" type="submit">Сохранить</button></form>
        </section>
        ${adminPanel()}
        <section class="panel history-panel"><h2>История заявки</h2><ul>${events}</ul></section>
      </aside>
    </div>`;
  render.showNotice(main.querySelector("[data-page-notice]"));
  main.addEventListener("submit", (event) => {
    if (event.target.matches("[data-confirm-delete]") && !confirm(`Удалить заявку №${id}? Пользователь не получит уведомление.`)) event.preventDefault();
  });
})();
