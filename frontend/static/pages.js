// Страницы админки и аккаунта: заполняет шаблоны из static/templates данными из API.
(() => {
  "use strict";
  if (!window.queuePage) return;
  const {context, escapeHtml, getJson, getText} = window.queuePage;
  const main = document.querySelector("main.page");
  const query = new URLSearchParams(location.search);

  class Raw {
    constructor(html) { this.html = html; }
  }
  const raw = (html) => new Raw(html);
  const cache = new Map();

  // Replaces {{ name }} once; every value is escaped unless it was marked raw().
  function fill(name, values) {
    if (!cache.has(name)) cache.set(name, getText(`/static/templates/${name}.html`).replace(/\n$/, ""));
    return cache.get(name).replace(/\{\{\s*(\w+)\s*\}\}/g, (_, key) => {
      if (!(key in values)) throw new Error(`Шаблон ${name}: нет значения ${key}`);
      const value = values[key];
      return value instanceof Raw ? value.html : escapeHtml(value);
    });
  }

  const csrf = () => context.csrf_token;
  const options = (pairs, selected, first = "") => raw(first + pairs.map(([key, label]) =>
    `<option value="${escapeHtml(key)}" ${key === selected ? "selected" : ""}>${escapeHtml(label)}</option>`).join(""));
  const nameOptions = (names, selected, first = "") => options(names.map((name) => [name, name]), selected, first);
  const orEmpty = (rows, columns, text) => raw(rows.join("") || `<tr><td colspan="${columns}" class="empty">${text}</td></tr>`);

  function noticeHtml(kind = "") {
    const text = query.get("notice");
    return raw(text ? `<div class="notice${kind ? ` ${kind}` : ""}">${escapeHtml(text)}</div>` : "");
  }

  function hiddenForm(action, fields, button, extra = "") {
    const inputs = Object.entries({csrf_token: csrf(), ...fields}).map(([name, value]) =>
      `<input type="hidden" name="${name}" value="${escapeHtml(value)}">`).join("");
    return `<form method="post" action="${action}"${extra}>${inputs}${button}</form>`;
  }

  const avatar = (item) =>
    `<span class="chat-avatar table-avatar">${escapeHtml(item.initials)}${item.avatar_url ? `<img src="${escapeHtml(item.avatar_url)}" alt="" loading="lazy" onerror="this.remove()">` : ""}</span>`;
  const chatLink = (chatId, label) => `<a class="button" href="/whatsapp?chat_id=${encodeURIComponent(chatId)}">${label}</a>`;

  function contactRows(rows) {
    return orEmpty(rows.map((item) => {
      const action = item.duplicate
        ? '<span class="duplicate-badge">Уже добавлен</span>'
        : hiddenForm("/admin/contacts", {action: "save", name: item.name, phone: item.phone}, '<button class="button primary" type="submit">Добавить</button>');
      return `<tr><td><span class="avatar-name">${avatar(item)}<strong>${escapeHtml(item.name)}</strong></span></td><td>${escapeHtml(item.phone_display)}</td><td>${escapeHtml(item.saved_text)}</td><td class="table-actions">${chatLink(item.chat_id, "Открыть чат")}${action}</td></tr>`;
    }), 4, "Ничего не найдено. Попробуйте имя, часть номера или обновите контакты.").html;
  }
  window.QueueAdmin = Object.freeze({contactRows});

  const badge = (item) => raw(`<span class="status ${escapeHtml(item.class)}">${escapeHtml(item.label)}</span>`);

  const PAGES = {
    home: (data) => fill("home", data),

    errors(data) {
      const rows = data.rows.map((row) => fill("error_row", {...row, error_report_status_badge: badge(row.badge)}));
      return fill("errors", {notice_html: noticeHtml(), search: data.search,
        options: options(data.statuses, data.status, '<option value="">Все статусы</option>'),
        table_rows: orEmpty(rows, 5, "Репортов по этому фильтру нет")});
    },

    error(data) {
      if (!data.found) return '<section class="panel empty"><h2>Репорт не найден</h2><a class="button" href="/admin/errors">К списку ошибок</a></section>';
      const file = data.attachment;
      let attachment = "";
      if (file && file.kind === "image") attachment = `<div class="panel"><h3>Вложение</h3><a href="${escapeHtml(file.url)}" target="_blank" rel="noopener"><img src="${escapeHtml(file.url)}" alt="${escapeHtml(file.name)}" style="max-width:720px;max-height:520px;width:auto;height:auto;border-radius:12px"></a><p>${escapeHtml(file.name)}</p></div>`;
      else if (file && file.kind === "file") attachment = `<div class="panel"><h3>Вложение</h3><a class="button" href="${escapeHtml(file.url)}" target="_blank" rel="noopener">Открыть / скачать ${escapeHtml(file.name)}</a></div>`;
      else if (file) attachment = `<div class="panel"><h3>Вложение</h3><p>${escapeHtml(file.name)}</p><p class="muted">Файл указан в репорте, но локальная копия сейчас недоступна.</p></div>`;
      return fill("error", {...data, notice_html: noticeHtml(), csrf_token: csrf(),
        chat_link: raw(data.chat_id ? chatLink(data.chat_id, "Открыть чат WhatsApp") : ""),
        error_report_status_badge: badge(data.badge), attachment_html: raw(attachment),
        status_options: options(data.statuses, data.status)});
    },

    audit(data) {
      const actionCell = (item) => `<td><strong>${escapeHtml(item.action)}</strong><small>${escapeHtml(item.details)}</small></td>`;
      return fill("audit", {
        search: data.search, actor_filter: data.actor_filter, action_filter: data.action_filter,
        levels: options(data.levels, data.level, '<option value="">Все уровни</option>'),
        audit_rows: orEmpty(data.audit.map((item) => `<tr class="audit-${escapeHtml(item.level)}"><td>${escapeHtml(item.created_at)}</td><td><span class="audit-level">${escapeHtml(item.level_name)}</span></td><td>${escapeHtml(item.actor)}</td>${actionCell(item)}<td>${escapeHtml(item.object)}</td></tr>`), 5, "Записей по этому фильтру нет"),
        auth_audit_html: orEmpty(data.auth.map((item) => `<tr><td>${escapeHtml(item.created_at)}</td><td>${escapeHtml(item.actor)}</td>${actionCell(item)}<td>${escapeHtml(item.object)}</td></tr>`), 4, "Событий доступа пока нет"),
        unified_audit_rows: orEmpty(data.unified.map((item) => `<tr><td>${escapeHtml(item.created_at)}</td><td>${escapeHtml(item.actor)}</td><td>${escapeHtml(item.action)}</td><td>${escapeHtml(item.object)}</td><td>${escapeHtml(item.source)}</td></tr>`), 5, "Событий нет"),
        monitor_html: raw(data.monitor.map((line) => `<li><code>${escapeHtml(line)}</code></li>`).join("") || '<li class="empty">Свежих системных ошибок нет</li>'),
      });
    },

    manager(data) {
      const f = data.filters;
      const filters = `<form class="filters analytics-final-filters" method="get" action="${escapeHtml(f.action)}"><input type="date" name="date_from" value="${escapeHtml(f.date_from)}"><input type="date" name="date_to" value="${escapeHtml(f.date_to)}"><select name="employee">${nameOptions(f.employees, f.employee, '<option value="">Все сотрудники</option>').html}</select><select name="status">${options(f.statuses, f.status, '<option value="">Все статусы</option>').html}</select><select name="category">${options(f.categories, f.category, '<option value="">Все категории</option>').html}</select><button class="button" type="submit">Применить</button><a class="button ghost" href="${escapeHtml(f.action)}">Сбросить</a></form>`;
      const kpi = (label, value) => `<div class="panel manager-kpi"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`;
      const employees = data.employees.map((item) => `<div class="manager-load-row"><span><strong>${escapeHtml(item.name)}</strong><small>открыто ${Number(item.open)} · сделано ${Number(item.done)}</small></span><b>${Number(item.total)}</b></div>`).join("") || '<p class="empty">Нет данных</p>';
      const categories = data.categories.map(([name, count]) => `<div class="manager-problem-row"><span>${escapeHtml(name)}</span><strong>${Number(count)}</strong></div>`).join("") || '<p class="empty">Нет данных</p>';
      return `<section class="page-heading"><div><p class="eyebrow">Руководитель</p><h1>Dashboard</h1><p>Сводная нагрузка и ключевые показатели</p></div></section>${filters}<section class="manager-kpi-grid">${kpi("Заявок", data.total)}${kpi("Открыто", data.open)}${kpi("Первый ответ", `${data.avg_first} мин`)}${kpi("Решение", `${data.avg_resolution} мин`)}${kpi("Передано сменой", data.handoffs)}</section><section class="manager-dashboard-grid"><div class="panel"><div class="section-heading"><div><h2>Нагрузка по сотрудникам</h2></div></div><div class="manager-list">${employees}</div></div><div class="panel"><div class="section-heading"><div><h2>Частые проблемы</h2></div></div><div class="manager-list">${categories}</div></div></section>`;
    },

    system(data) {
      const rows = data.queue.map((row) => `<tr><td>#${Number(row.id)}</td><td>${escapeHtml(row.status)}</td><td>${escapeHtml(row.attempts)}</td><td>${escapeHtml(row.target)}</td><td>${escapeHtml(row.error)}</td><td>${escapeHtml(row.updated_at)}</td></tr>`);
      return fill("system", {...data.values, csrf_token: csrf(), notice_html: noticeHtml(),
        cards: raw(fill("system_cards", data.cards)),
        queue_table: orEmpty(rows, 6, "Очередь пока пуста"),
        logs: raw(data.logs.map((line) => `<li><code>${escapeHtml(line)}</code></li>`).join("") || '<li class="empty">Журнал мониторинга пока пуст</li>')});
    },

    employees(data) {
      const fields = data.names.map((name, index) => `<label>Сотрудник ${index + 1}<input name="employee_${index + 1}" value="${escapeHtml(name)}" maxlength="60" required></label>`).join("");
      return fill("employees", {...data, notice_html: noticeHtml(), csrf_token: csrf(), fields: raw(fields)});
    },

    manual: (data) => fill("manual", {notice_html: noticeHtml(), csrf_token: csrf(),
      category_options: options(data.categories, "general"),
      priority_options: options(data.priorities, "normal"),
      employee_options: nameOptions(data.employees, data.employee)}),

    menu(data) {
      const card = (title, text, note = "") => `<article class="panel admin-flow-card"><div class="admin-flow-card-head"><h3>${escapeHtml(title)}</h3>${note ? `<small class="admin-flow-note">${escapeHtml(note)}</small>` : ""}</div><pre class="admin-flow-message">${escapeHtml(text)}</pre></article>`;
      const column = (item) => fill("flow_language_column", {
        language: item.language, upper: item.language.toUpperCase(), title: item.title,
        admin_flow_message: raw(card("Шаг 2 · Подразделение и должность", item.profile)),
        admin_flow_message_2: raw(card("Шаг 3 · Главное меню", item.menu)),
        category_cards: raw(item.categories.map(([label, prompt]) => card(label, prompt, "После выбора пункта меню")).join("")),
        admin_flow_message_3: raw(card("Есть активные заявки", item.active)),
        admin_flow_message_4: raw(card("Активных заявок нет", item.active_empty)),
        admin_flow_message_5: raw(card("Заявка принята", item.accepted)),
        admin_flow_message_6: raw(card("Не хватает данных", item.missing)),
        admin_flow_message_7: raw(card("Дублирующаяся заявка", item.duplicate)),
        admin_flow_message_8: raw(card("Заявка выполнена", item.done)),
        admin_flow_message_9: raw(card("Заявка закрыта / отклонена", item.invalid)),
      });
      return fill("menu", {notice_html: noticeHtml(),
        admin_flow_message: raw(card("Шаг 1 · Выбор языка", data.language_text, "Общее сообщение RU/KZ")),
        admin_flow_language_column: raw(column(data.columns[0])),
        admin_flow_language_column_2: raw(column(data.columns[1]))});
    },

    categories(data) {
      const FIELDS = [["problem", "описание"], ["reference", "номер / идентификатор"], ["email", "email"]];
      const editor = (row) => {
        const isNew = row.suffix === "new";
        return fill("category_editor_row", {
          key_field: raw(isNew ? "" : `<input type="hidden" name="key_${escapeHtml(row.suffix)}" value="${escapeHtml(row.key)}">`),
          suffix: row.suffix, label_ru: row.label_ru, label_kz: row.label_kz, prompt_ru: row.prompt_ru, prompt_kz: row.prompt_kz,
          options: raw(row.numbers.map((n) => `<option value="${escapeHtml(n)}" ${n === row.number ? "selected" : ""}>${escapeHtml(n)}</option>`).join("")),
          required_attr: raw(isNew ? "" : "required"),
          checks: raw(FIELDS.map(([name, label]) => `<label class="checkbox-label"><input type="checkbox" name="req_${name}_${escapeHtml(row.suffix)}" value="1" ${row.required.includes(name) ? "checked" : ""}>${label}</label>`).join("")),
          delete: raw(isNew ? "" : `<label class="checkbox-label danger-choice"><input type="checkbox" name="delete_${escapeHtml(row.suffix)}" value="1">Удалить категорию</label>`),
        });
      };
      return fill("categories", {notice_html: noticeHtml(), csrf_token: csrf(), greeting_ru: data.greeting_ru, greeting_kz: data.greeting_kz,
        editors: raw(data.editors.map(editor).join("")),
        add_row: raw(data.new_row ? editor(data.new_row) : '<p class="muted">Все номера меню заняты.</p>')});
    },

    contacts(data) {
      const rows = data.contacts.map((item) => `<tr><td><span class="avatar-name">${avatar(item)}<strong>${escapeHtml(item.name)}</strong></span></td><td>${escapeHtml(item.phone_display)}</td><td>${escapeHtml(item.created_at)}</td><td class="table-actions">${chatLink(item.chat_id, "Профиль / чат")}${hiddenForm("/admin/contacts", {action: "delete", chat_id: item.chat_id}, '<button class="button ghost" type="submit">Удалить</button>', ` onsubmit="return confirm('Удалить контакт из системы?')"`)}</td></tr>`);
      return fill("contacts", {notice_html: noticeHtml(), csrf_token: csrf(),
        table: orEmpty(rows, 4, "Контакты вручную ещё не добавлены"),
        search_raw: raw(encodeURIComponent(data.search)), search_raw_2: data.search,
        found_table: raw(contactRows(data.found))});
    },

    users(data) {
      const notice = query.get("notice") || "";
      const pending = data.requests.map((item) => fill("pending_user_row", {...item, csrf_token: csrf()}));
      const recovery = pending.length ? `<section class="panel table-panel auth-recovery-panel"><div class="section-heading"><div><h2>Запросы на восстановление пароля</h2><p>При временном пароле пользователь обязан сменить его сразу после входа.</p></div></div><div class="table-scroll"><table><thead><tr><th>Пользователь</th><th>Запрошено</th><th>Действие</th></tr></thead><tbody>${pending.join("")}</tbody></table></div></section>` : "";
      const THEMES = [["dark", "Тёмная"], ["light", "Светлая"], ["pink", "Pink / Pick Me 🌸"]];
      const users = data.users.map((user) => {
        const linkForm = user.role !== "employee" ? "" : `<details class="auth-reset-details"><summary>Привязка</summary>${hiddenForm("/admin/users", {action: "employee_link", user_id: user.user_id}, `<select name="employee_name">${nameOptions(data.employees, user.linked, '<option value="">Без привязки</option>').html}</select><button class="button" type="submit">Сохранить</button>`)}</details>`;
        return fill("user_row", {...user, csrf_token: csrf(),
          linked_text: raw(user.linked ? `<small>Привязан: ${escapeHtml(user.linked)}</small>` : ""),
          theme_select: options(THEMES, user.theme), link_form: raw(linkForm)});
      });
      const sessions = data.sessions.map((item) => `<tr><td><strong>${escapeHtml(item.display_name)}</strong><small>@${escapeHtml(item.username)}</small></td><td>${escapeHtml(item.client_ip)}</td><td>${escapeHtml(item.last_seen_at)}</td><td><small>${escapeHtml(item.user_agent)}</small></td><td>${hiddenForm("/admin/users", {action: "revoke_session", session_hash: item.session_hash}, '<button class="button ghost" type="submit">Завершить</button>')}</td></tr>`);
      const logins = data.logins.map((item) => `<tr><td>${escapeHtml(item.created_at)}</td><td><strong>@${escapeHtml(item.username)}</strong></td><td><span class="auth-state ${item.success ? "is-on" : "is-off"}">${item.success ? "Успешно" : "Ошибка"}</span><small>${escapeHtml(item.reason)}</small></td><td>${escapeHtml(item.client_ip)}</td></tr>`);
      return fill("users", {notice_html: noticeHtml(notice.toLowerCase().startsWith("ошибка:") ? "error" : "success"),
        recovery_panel: raw(recovery), csrf_token: csrf(),
        table_rows: raw(users.join("") || '<tr><td colspan="5">Пользователей пока нет</td></tr>'),
        session_table: orEmpty(sessions, 5, "Активных сессий нет"),
        login_table: orEmpty(logins, 4, "История входов пуста")});
    },
  };

  const noticeText = () => noticeHtml();
  const pairRows = (items) => orEmpty(items.map(([name, count]) => `<tr><td>${escapeHtml(name)}</td><td>${Number(count)}</td></tr>`), 2, "Нет данных");

  Object.assign(PAGES, {
    analytics: (data) => fill("analytics_page", {...data, rows: pairRows(data.by_category), rows_2: pairRows(data.by_employee)}),

    backups(data) {
      const rows = data.rows.map((row) => `<tr class="backup-row"><td><div class="backup-name"><span class="backup-icon">DB</span><strong>${escapeHtml(row.name)}</strong></div></td><td>${escapeHtml(row.size)}</td><td><span class="backup-status ${row.ok ? "ok" : "error"}"><i></i>${row.ok ? "Исправна" : "Ошибка"}</span></td><td><span class="backup-detail">${escapeHtml(row.detail)}</span></td><td>${hiddenForm("/admin/backups", {action: "restore", name: row.name}, `<button class="button compact backup-restore" ${row.ok ? "" : "disabled"}>Восстановить</button>`, " data-backup-restore-form")}</td></tr>`);
      return fill("backups_page", {notice_text: noticeText(), csrf_token: csrf(), join: orEmpty(rows, 5, "Копий пока нет")});
    },

    reliability(data) {
      const retry = (id, action, button) => hiddenForm("/admin/reliability", {action, message_id: id}, button, ' class="inline-form"');
      const problems = data.problems.map((item) => `<tr><td>#${Number(item.id)}</td><td>${escapeHtml(item.status)}</td><td>${escapeHtml(item.chat_id)}</td><td>${escapeHtml(item.body)}</td><td>${escapeHtml(item.error)}</td><td>${retry(item.id, "retry", '<button class="button compact" type="submit">Повторить</button>')}${item.has_media ? retry(item.id, "retry_media", '<button class="button compact ghost" type="submit">Только файл</button>') : ""}</td></tr>`);
      const backups = data.backups.map((item) => `<tr><td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.size)}</td><td>${escapeHtml(item.modified)}</td><td><span class="status ${item.ok ? "status-done" : "status-invalid"}">${item.ok ? "OK" : escapeHtml(item.detail)}</span></td></tr>`);
      const muted = (columns, text) => raw(`<tr><td colspan="${columns}" class="muted">${text}</td></tr>`);
      return fill("reliability", {...data.values, notice_text: noticeText(), csrf_token: csrf(),
        problem_rows: problems.length ? raw(problems.join("")) : muted(6, "Ошибочных или зависших отправок нет"),
        backup_rows: backups.length ? raw(backups.join("")) : muted(4, "Резервные копии tickets.db не найдены в стандартных каталогах")});
    },

    workflow(data) {
      const cards = data.media_cards.map((item) => `<article class="stat-card"><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.size)}</strong><small>${Number(item.count)} файлов</small></article>`).join("");
      const checks = data.categories.map(([key, label, checked]) => `<label class="workflow-check"><input type="checkbox" name="required_category__${escapeHtml(key)}" value="1" ${checked ? "checked" : ""}><span>${escapeHtml(label)}</span></label>`).join("");
      return fill("workflow_admin", {notice_text: noticeText(), csrf_token: csrf(), messages: data.messages, media: data.media, tickets: data.tickets,
        media_cards: raw(cards), checks: raw(checks || '<span class="muted">Категории не найдены</span>')});
    },

    shift(data) {
      const groups = options(data.groups, data.selected, '<option value="">Не отправлять</option>').html;
      return `<h2>Объявление о смене</h2><form method="post" class="panel manual-form"><input type="hidden" name="csrf_token" value="${escapeHtml(csrf())}"><label>Группа<select name="group">${groups}</select></label><label>Текст<textarea name="text" maxlength="800" required>${escapeHtml(data.text)}</textarea></label><p>Используйте {employee} для имени сотрудника. Отправка только при изменении сотрудника на смене, не при сохранении настроек.</p><button class="button primary">Сохранить</button></form>`;
    },

    "api-keys"(data) {
      const secret = data.new_token ? `<section class="panel api-key-secret"><h2>Скопируйте ключ сейчас</h2><p>Полный секрет больше не показывается.</p><code>${escapeHtml(data.new_token)}</code></section>` : "";
      const rows = data.keys.map((item) => {
        const revoke = item.enabled ? hiddenForm("/admin/api-keys", {action: "revoke", key_id: item.id}, '<button class="button ghost" type="submit">Отключить</button>') : '<span class="muted">Отключён</span>';
        return `<tr><td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.token_prefix)}…</small></td><td>${escapeHtml(item.scope)}</td><td>${item.enabled ? "Активен" : "Отключён"}</td><td>${escapeHtml(item.last_used_at)}</td><td>${escapeHtml(item.created_by)}</td><td>${revoke}</td></tr>`;
      });
      const form = hiddenForm("/admin/api-keys", {action: "create"}, '<label class="wide">Название<input name="name" maxlength="80" required></label><button class="button primary" type="submit">Создать ключ</button>', ' class="manual-form"');
      return `${noticeHtml().html}${secret}<section class="panel manual-panel"><div><p class="eyebrow">Интеграции</p><h2>Создать API-ключ</h2><p>Только read-only доступ.</p></div>${form}</section><section class="panel table-panel"><div class="section-heading"><div><h2>Ключи интеграций</h2><p>Секрет хранится только как SHA-256 хэш.</p></div></div><div class="table-scroll"><table><thead><tr><th>Название</th><th>Scope</th><th>Статус</th><th>Использован</th><th>Создал</th><th></th></tr></thead><tbody>${orEmpty(rows, 6, "API-ключей пока нет").html}</tbody></table></div></section><section class="panel history-panel"><h2>Endpoints</h2><ul><li><code>GET /api/integration/ping</code></li><li><code>GET /api/integration/tickets?limit=100&amp;status=in_progress</code></li></ul></section>`;
    },

    me(data) {
      const ticketCards = (items, empty) => raw(items.map((item) => `<a class="my-ticket-card" href="/ticket?id=${Number(item.id)}"><span><strong>#${Number(item.id)} · ${escapeHtml(item.title)}</strong><small>${escapeHtml(item.summary)}</small></span><span class="my-ticket-meta"><span class="status status-${escapeHtml(item.status)}">${escapeHtml(item.status_label)}</span><small>${escapeHtml(item.created_at)}</small></span></a>`).join("") || `<p class="empty">${empty}</p>`);
      const logins = data.logins.map((item) => `<tr><td>${escapeHtml(item.created_at)}</td><td><span class="auth-state ${item.success ? "is-on" : "is-off"}">${item.success ? "Успешно" : "Ошибка"}</span></td><td>${escapeHtml(item.reason)}</td><td>${escapeHtml(item.client_ip)}</td></tr>`);
      const activity = data.activity.map((item) => `<tr><td>${escapeHtml(item.created_at)}</td><td><strong>${escapeHtml(item.action)}</strong><small>${escapeHtml(item.details)}</small></td><td>${escapeHtml(item.object)}</td></tr>`);
      return fill("my_page", {...data,
        ticket_cards: ticketCards(data.active, "Активных заявок нет"),
        ticket_cards_2: ticketCards(data.done, "Выполненных заявок пока нет"),
        login_table: orEmpty(logins, 4, "История входов пока пуста"),
        activity_table: orEmpty(activity, 3, "Действий аккаунта пока нет")});
    },

    accounts(data) {
      const notice = query.get("notice");
      const cards = data.accounts.map((item) => fill("account_card", {role: item.role, display_name: item.display_name, username: item.username,
        action: raw(item.active ? '<span class="status-badge done">Текущий аккаунт</span>' : fill("account_switch_form", {csrf_token: csrf(), slot: item.slot}))}));
      return fill("account_switcher", {
        registration_enabled_text: raw(data.registration_enabled ? '<a class="button ghost" href="/register">Создать новый аккаунт</a>' : ""),
        notice_html: raw(notice ? `<div class="panel"><strong>${escapeHtml(notice)}</strong></div>` : ""),
        join: raw(cards.join("") || '<div class="panel empty">Сохранённых аккаунтов нет</div>')});
    },

    reminders(data) {
      const cards = data.rows.map((item) => `<article class="panel reminder-card"><div><strong>Заявка #${Number(item.ticket_id)}</strong><time>${escapeHtml(item.due)}</time></div><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.note)}</p><div><a class="button compact" href="/ticket?id=${Number(item.ticket_id)}">Открыть</a><button class="button compact ghost" data-reminder-done="${Number(item.id)}">Выполнено</button></div></article>`).join("");
      return `<section class="page-heading"><div><p class="eyebrow">Заявки</p><h1>Напоминания</h1><p>Запланированные напоминания сотрудников</p></div></section><section class="reminder-grid">${cards || '<p class="muted">Активных напоминаний нет</p>'}</section>`;
    },

    recent(data) {
      const cards = data.rows.map((item) => `<a class="workflow-recent-card" href="${escapeHtml(item.href)}"><span class="workflow-recent-icon">${item.group ? "Г" : "Л"}</span><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.last_message)}</small></span>${item.unread ? `<b>${Number(item.unread)}</b>` : ""}</a>`).join("");
      return `<section class="page-heading"><div><p class="eyebrow">WhatsApp</p><h1>Недавние чаты</h1><p>Последние открытые диалоги текущего сотрудника</p></div></section><section class="workflow-recent-grid">${cards || '<div class="panel empty">Откройте несколько чатов, и они появятся здесь.</div>'}</section>`;
    },
  });

  const HEADING = `<section class="page-heading admin-heading">
        <div><p class="eyebrow">Управление системой</p><h1>Админка</h1><p>Сотрудники, контакты, группы и настройки обращений</p></div>
      </section>`;

  function tabs(items, active) {
    return `<nav class="admin-tabs">${items.map((item) => `<a class="admin-tab ${item.key === active ? "active" : ""}" href="${escapeHtml(item.href)}">${escapeHtml(item.label)}</a>`).join("")}</nav>`;
  }

  const isAdmin = location.pathname.startsWith("/admin");
  const page = isAdmin ? (location.pathname.replace(/^\/admin\/?/, "") || "home") : location.pathname.slice(1);
  const data = getJson(`/api/${isAdmin ? "admin" : "app"}-page?page=${encodeURIComponent(page)}&${location.search.slice(1)}`);
  if (!data) return;
  document.title = `${data.title} · Единая очередь`;
  const content = PAGES[page](data);
  main.innerHTML = (isAdmin ? HEADING + tabs(data.tabs, data.active) : "") + (content instanceof Raw ? content.html : content);
})();
