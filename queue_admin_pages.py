from __future__ import annotations

from typing import Any

def bind(context: dict[str, Any]) -> None:
    protected = {"bind", "Any"}
    for name, value in context.items():
        if name.startswith("__") or name in protected:
            continue
        globals()[name] = value

def render_admin_home() -> str:
    contacts = len(STORE.list_manual_whatsapp_contacts())
    groups = len(STORE.list_whatsapp_groups())
    error_counts = STORE.error_report_counts()
    open_errors = int(error_counts.get("new", 0)) + int(error_counts.get("in_progress", 0))
    cards = f"""
      <section class="admin-card-grid">
        <a class="panel admin-card" href="/admin/users"><span>Доступ</span><strong>{len(AUTH.list_users())}</strong><small>Логины, пароли и роли пользователей</small></a>
        <a class="panel admin-card" href="/admin/employees"><span>Сотрудники</span><strong>{len(EMPLOYEES)}</strong><small>Имена и количество сотрудников</small></a>
        <a class="panel admin-card" href="/admin/manual"><span>Ручная заявка</span><strong>+</strong><small>Создать запрос от имени сотрудника</small></a>
        <a class="panel admin-card" href="/admin/menu"><span>Тексты WhatsApp</span><strong>RU/KZ</strong><small>Актуальный пользовательский сценарий и системные ответы</small></a>
        <a class="panel admin-card" href="/admin/categories"><span>Категории</span><strong>{len(active_ticket_category_items())}</strong><small>Добавить, убрать или переименовать виды заявок</small></a>
        <a class="panel admin-card" href="/admin/errors"><span>Ошибки</span><strong>{open_errors}</strong><small>Отдельные репорты пользователей, не являющиеся заявками</small></a>
        <a class="panel admin-card" href="/admin/contacts"><span>Контакты WhatsApp</span><strong>{contacts}</strong><small>Добавление пользователя по номеру</small></a>
        <a class="panel admin-card" href="/groups"><span>Группы WhatsApp</span><strong>{groups}</strong><small>Общая переписка и отправка сообщений</small></a>
        <a class="panel admin-card" href="/admin/audit"><span>Журнал</span><strong>≡</strong><small>Действия сотрудников, сообщения и ошибки</small></a>
        <a class="panel admin-card" href="/admin/analytics"><span>Аналитика</span><strong>↗</strong><small>Сотрудники, смены, категории и время обработки</small></a>
        <a class="panel admin-card" href="/admin/manager"><span>Dashboard руководителя</span><strong>▦</strong><small>Нагрузка, скорость обработки и частые проблемы</small></a>
        <a class="panel admin-card" href="/admin/api-keys"><span>API-ключи</span><strong>⌘</strong><small>Read-only ключи для будущих интеграций</small></a>
        <a class="panel admin-card" href="/admin/backups"><span>Бэкапы</span><strong>⟳</strong><small>Создание, проверка и восстановление базы</small></a>
        <a class="panel admin-card" href="/admin/workflow"><span>Хранение / процессы</span><strong>⚙</strong><small>Сроки хранения, очистка медиа и правила закрытия</small></a>
        <a class="panel admin-card" href="/admin/reliability"><span>Надёжность</span><strong>🛡</strong><small>Диагностика, очередь ошибок, бэкапы и обслуживание</small></a>
        <a class="panel admin-card" href="/admin/system"><span>Система</span><strong>●</strong><small>Состояние, очередь отправки и хранилище</small></a>
      </section>
      <section class="panel integration-status">
        <h2>{'Локальный тестовый режим' if LOCAL_MODE else 'Авторизация приложения'}</h2>
        <div><span>Учётных записей</span><strong>{len(AUTH.list_users())}</strong></div>
        <p class="muted">{'Windows-копия использует отдельную тестовую базу и отдельные логины.' if LOCAL_MODE else 'Пользователи входят через /login. Существующий Nginx-администратор сохранён как аварийный путь для первичной настройки и восстановления доступа.'}</p>
      </section>
    """
    return admin_layout("Админка", cards, "home")


def error_report_status_badge(status: str) -> str:
    label = ERROR_REPORT_STATUS_LABELS.get(status, status or "Новая")
    css = {
        "new": "status-new",
        "in_progress": "status-in_progress",
        "resolved": "status-done",
        "rejected": "status-invalid",
    }.get(status, "status-new")
    return f'<span class="status {css}">{e(label)}</span>'


def render_admin_errors(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    selected_status = query.get("status", [""])[0]
    search = query.get("q", [""])[0]
    reports = STORE.list_error_reports(selected_status, search, 300)
    rows: list[str] = []
    for report in reports:
        report_id = int(report.get("id", 0) or 0)
        description = normalize_message(str(report.get("description", "")))
        sender = str(report.get("sender", "") or "Неизвестный пользователь")
        phone = display_phone(str(report.get("phone", ""))) if report.get("phone") else "—"
        attachment = str(report.get("attachment_name", "") or "")
        rows.append(
            f'<tr><td><a class="ticket-title-link" href="/admin/error?id={report_id}">#{report_id}</a></td>'
            f'<td>{error_report_status_badge(str(report.get("status", "new")))}</td>'
            f'<td><strong>{e(sender)}</strong><small>{e(phone)}</small></td>'
            f'<td><strong>{e(description[:150] or "Без текста")}</strong>'
            f'<small>{e(("Вложение: " + attachment) if attachment else "")}</small></td>'
            f'<td>{e(human_time(str(report.get("created_at", ""))) or "—")}</td></tr>'
        )
    table_rows = "".join(rows) or '<tr><td colspan="5" class="empty">Репортов по этому фильтру нет</td></tr>'
    options = '<option value="">Все статусы</option>' + "".join(
        f'<option value="{key}" {"selected" if selected_status == key else ""}>{e(label)}</option>'
        for key, label in ERROR_REPORT_STATUS_LABELS.items()
    )
    content = f'''\
      {notice_html}
      <section class="panel table-panel">
        <div class="section-heading"><div><p class="eyebrow">Отдельно от заявок</p><h2>Репорты об ошибках</h2><p>Сообщения пользователей о багах системы. Они не участвуют в SLA и не попадают в список заявок.</p></div></div>
        <form class="filters" method="get" action="/admin/errors">
          <input type="search" name="q" value="{e(search)}" placeholder="Текст, имя, телефон, номер репорта...">
          <select name="status">{options}</select>
          <button class="button" type="submit">Найти</button><a class="button ghost" href="/admin/errors">Сбросить</a>
        </form>
        <div class="table-scroll"><table><thead><tr><th>ID</th><th>Статус</th><th>Пользователь</th><th>Описание</th><th>Создан</th></tr></thead><tbody>{table_rows}</tbody></table></div>
      </section>
    '''
    return admin_layout("Ошибки", content, "errors")


def render_admin_error(report_id: int, query: dict[str, list[str]]) -> str:
    report = STORE.get_error_report(report_id)
    if not report:
        return admin_layout("Ошибка не найдена", '<section class="panel empty"><h2>Репорт не найден</h2><a class="button" href="/admin/errors">К списку ошибок</a></section>', "errors")
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    chat_id = str(report.get("chat_id", "") or "")
    external_id = str(report.get("external_id", "") or "")
    message = STORE.get_whatsapp_message(chat_id, external_id) if chat_id and external_id else None
    attachment_html = ""
    if message and str(message.get("media_path", "") or ""):
        media_url = f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(external_id)}"
        mime = str(message.get("media_mime", "") or "")
        name = str(message.get("media_name", "") or report.get("attachment_name", "") or "Вложение")
        if mime.startswith("image/"):
            attachment_html = f'<div class="panel"><h3>Вложение</h3><a href="{e(media_url)}" target="_blank" rel="noopener"><img src="{e(media_url)}" alt="{e(name)}" style="max-width:720px;max-height:520px;width:auto;height:auto;border-radius:12px"></a><p>{e(name)}</p></div>'
        else:
            attachment_html = f'<div class="panel"><h3>Вложение</h3><a class="button" href="{e(media_url)}" target="_blank" rel="noopener">Открыть / скачать {e(name)}</a></div>'
    elif report.get("attachment_name"):
        attachment_html = f'<div class="panel"><h3>Вложение</h3><p>{e(str(report.get("attachment_name", "")))}</p><p class="muted">Файл указан в репорте, но локальная копия сейчас недоступна.</p></div>'
    chat_link = f'<a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат WhatsApp</a>' if chat_id else ""
    status_options = "".join(
        f'<option value="{key}" {"selected" if str(report.get("status", "new")) == key else ""}>{e(label)}</option>'
        for key, label in ERROR_REPORT_STATUS_LABELS.items()
    )
    content = f'''\
      {notice_html}
      <section class="section-heading"><div><p class="eyebrow">Репорт #{int(report.get("id", 0) or 0)}</p><h2>Ошибка пользователя</h2><p>{e(human_time(str(report.get("created_at", ""))))}</p></div>{chat_link}</section>
      <section class="panel ticket-details">
        <div><span>Статус</span><strong>{error_report_status_badge(str(report.get("status", "new")))}</strong></div>
        <div><span>Пользователь</span><strong>{e(str(report.get("sender", "") or "Неизвестный"))}</strong></div>
        <div><span>Телефон</span><strong>{e(display_phone(str(report.get("phone", ""))) if report.get("phone") else "—")}</strong></div>
        <div><span>Описание</span><strong style="white-space:pre-wrap">{e(str(report.get("description", "") or "Без текста"))}</strong></div>
      </section>
      {attachment_html}
      <section class="panel">
        <div class="section-heading"><div><h2>Обработка репорта</h2><p>Статус и служебная заметка видны только внутри «Единой очереди».</p></div></div>
        <form class="manual-form" method="post" action="/admin/errors">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <input type="hidden" name="report_id" value="{int(report.get("id", 0) or 0)}">
          <label>Статус<select name="status">{status_options}</select></label>
          <label class="wide">Заметка<textarea name="admin_note" rows="4" maxlength="3000" placeholder="Что проверили / как исправили">{e(str(report.get("admin_note", "") or ""))}</textarea></label>
          <button class="button primary" type="submit">Сохранить</button>
          <a class="button ghost" href="/admin/errors">К списку ошибок</a>
        </form>
      </section>
    '''
    return admin_layout(f"Ошибка #{report_id}", content, "errors")


def render_admin_audit(query: dict[str, list[str]]) -> str:
    level = query.get("level", [""])[0]
    search = query.get("q", [""])[0]
    entries = STORE.list_audit_entries(300, level, search)
    rows: list[str] = []
    for item in entries:
        level_name = {"info": "Инфо", "warning": "Внимание", "error": "Ошибка"}.get(str(item.get("level", "")), str(item.get("level", "")))
        obj = " · ".join(part for part in [str(item.get("object_type", "")), str(item.get("object_id", ""))] if part)
        rows.append(
            f'<tr class="audit-{e(str(item.get("level","info")))}"><td>{e(human_time(str(item.get("created_at",""))))}</td>'
            f'<td><span class="audit-level">{e(level_name)}</span></td><td>{e(str(item.get("actor","") or "Система"))}</td>'
            f'<td><strong>{e(str(item.get("action","")))}</strong><small>{e(str(item.get("details","") or ""))}</small></td>'
            f'<td>{e(obj or "—")}</td></tr>'
        )
    audit_rows = "".join(rows) or '<tr><td colspan="5" class="empty">Записей по этому фильтру нет</td></tr>'
    monitor_errors = [line for line in monitor_log_entries(100) if " ERROR " in f" {line} " or " WARN " in f" {line} "][:30]
    monitor_html = "".join(f'<li><code>{e(line)}</code></li>' for line in monitor_errors) or '<li class="empty">Свежих системных ошибок нет</li>'
    auth_rows=[]
    for item in AUTH.list_audit_events(200):
        actor=str(item.get("actor_display_name") or item.get("actor_username") or "Система")
        auth_rows.append(f'<tr><td>{e(human_time(str(item.get("created_at") or "")))}</td><td>{e(actor)}</td><td><strong>{e(str(item.get("action") or ""))}</strong><small>{e(str(item.get("details") or ""))}</small></td><td>{e(" · ".join(part for part in [str(item.get("object_type") or ""),str(item.get("object_id") or "")] if part) or "—")}</td></tr>')
    auth_audit_html=''.join(auth_rows) or '<tr><td colspan="4" class="empty">Событий доступа пока нет</td></tr>'
    levels = '<option value="">Все уровни</option>' + "".join(
        f'<option value="{key}" {"selected" if level == key else ""}>{label}</option>'
        for key, label in [("info","Инфо"),("warning","Внимание"),("error","Ошибка")]
    )
    content = f'''
      <section class="panel audit-panel">
        <div class="section-heading"><div><p class="eyebrow">Аудит</p><h2>Журнал действий и ошибок</h2><p>Кто менял заявки, отправлял сообщения, переключал автоответчик и какие ошибки возникали</p></div></div>
        <form class="filters audit-filters" method="get" action="/admin/audit">
          <input type="search" name="q" value="{e(search)}" placeholder="Сотрудник, действие, № заявки, чат...">
          <select name="level">{levels}</select><button class="button" type="submit">Найти</button><a class="button ghost" href="/admin/audit">Сбросить</a>
        </form>
        <div class="table-scroll"><table class="audit-table"><thead><tr><th>Время</th><th>Уровень</th><th>Кто</th><th>Действие</th><th>Объект</th></tr></thead><tbody>{audit_rows}</tbody></table></div>
      </section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>Доступ и учётные записи</h2><p>Изменения пользователей, паролей, сессий и блокировок диалогов</p></div></div><div class="table-scroll"><table><thead><tr><th>Время</th><th>Кто</th><th>Действие</th><th>Объект</th></tr></thead><tbody>{auth_audit_html}</tbody></table></div></section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>Общий журнал сотрудников</h2><p>Единая лента изменений заявок, сообщений, админки и учётных записей</p></div></div><form class="filters" method="get" action="/admin/audit"><input name="actor" value="{e(str(query.get('actor',[''])[0] or ''))}" placeholder="Сотрудник"><input name="action" value="{e(str(query.get('action',[''])[0] or ''))}" placeholder="Действие / объект"><button class="button" type="submit">Фильтр</button></form><div class="table-scroll"><table><thead><tr><th>Время</th><th>Кто</th><th>Действие</th><th>Объект</th><th>Источник</th></tr></thead><tbody>{_unified_audit_rows(query)}</tbody></table></div></section>
      <section class="panel history-panel"><h2>Системные предупреждения</h2><ul>{monitor_html}</ul></section>
    '''
    return admin_layout("Журнал", content, "audit")


def _analytics_filters(query: dict[str, list[str]], action: str) -> str:
    employee=str(query.get("employee",[""])[0] or ""); date_from=str(query.get("date_from",[""])[0] or ""); date_to=str(query.get("date_to",[""])[0] or ""); status=str(query.get("status",[""])[0] or "")
    employees=sorted({str(item.get("assigned_to") or "") for item in STORE.list_tickets("","","","",5000,0) if str(item.get("assigned_to") or "")},key=str.casefold)
    employee_html='<option value="">Все сотрудники</option>'+''.join(f'<option value="{e(name)}" {"selected" if name==employee else ""}>{e(name)}</option>' for name in employees)
    status_html='<option value="">Все статусы</option>'+''.join(f'<option value="{e(key)}" {"selected" if key==status else ""}>{e(label)}</option>' for key,label in STATUSES.items())
    category=str(query.get("category",[""])[0] or "")
    return f'''<form class="filters analytics-final-filters" method="get" action="{e(action)}"><input type="date" name="date_from" value="{e(date_from)}"><input type="date" name="date_to" value="{e(date_to)}"><select name="employee">{employee_html}</select><select name="status">{status_html}</select><select name="category">{category_options(category,True)}</select><button class="button" type="submit">Применить</button><a class="button ghost" href="{e(action)}">Сбросить</a></form>'''


def render_admin_analytics_final(query: dict[str, list[str]]) -> str:
    data=_analytics_snapshot(query)
    employee_rows=''.join(f'<tr><td><strong>{e(name)}</strong></td><td>{vals["total"]}</td><td>{vals["open"]}</td><td>{vals["done"]}</td><td>{vals["invalid"]}</td><td>{vals["handoff"]}</td></tr>' for name,vals in sorted(data["by_employee"].items(),key=lambda kv:(-kv[1]["total"],kv[0].casefold()))) or '<tr><td colspan="6" class="empty">Нет данных</td></tr>'
    category_rows=''.join(f'<tr><td>{e(name)}</td><td>{count}</td><td>{round(count/max(1,int(data["total"]))*100,1)}%</td></tr>' for name,count in list(data["by_category"].items())[:30]) or '<tr><td colspan="3" class="empty">Нет данных</td></tr>'
    q=urlencode({k:v[0] for k,v in query.items() if v and v[0]}); export_href='/admin/tickets-export.csv'+(('?'+q) if q else '')
    content=f'''<section class="page-heading"><div><p class="eyebrow">Аналитика</p><h1>Статистика работы</h1><p>Сотрудники, смены, категории и скорость обработки заявок</p></div><a class="button primary" href="{e(export_href)}">Экспорт CSV</a></section>{_analytics_filters(query,'/admin/analytics')}<section class="metrics analytics-final-metrics"><div class="metric"><span>Всего</span><strong>{data["total"]}</strong></div><div class="metric"><span>Открыты</span><strong>{data["open"]}</strong></div><div class="metric"><span>Выполнены</span><strong>{data["done"]}</strong></div><div class="metric"><span>Первый ответ</span><strong>{data["avg_first"]} мин</strong></div><div class="metric"><span>Решение</span><strong>{data["avg_resolution"]} мин</strong></div></section><section class="panel table-panel"><div class="section-heading"><div><h2>По сотрудникам и сменам</h2><p>Нагрузка и результат</p></div></div><div class="table-scroll"><table><thead><tr><th>Сотрудник</th><th>Всего</th><th>Открыты</th><th>Сделано</th><th>Недейств.</th><th>Передано</th></tr></thead><tbody>{employee_rows}</tbody></table></div></section><section class="panel table-panel"><div class="section-heading"><div><h2>По категориям</h2><p>Самые частые типы обращений</p></div></div><div class="table-scroll"><table><thead><tr><th>Категория</th><th>Заявки</th><th>Доля</th></tr></thead><tbody>{category_rows}</tbody></table></div></section>'''
    return admin_layout("Аналитика",content,"analytics")


def render_admin_manager_dashboard(query: dict[str, list[str]]) -> str:
    data=_analytics_snapshot(query)
    cats=''.join(f'<div class="manager-problem-row"><span>{e(name)}</span><strong>{count}</strong></div>' for name,count in list(data["by_category"].items())[:8]) or '<p class="empty">Нет данных</p>'
    emps=''.join(f'<div class="manager-load-row"><span><strong>{e(name)}</strong><small>открыто {vals["open"]} · сделано {vals["done"]}</small></span><b>{vals["total"]}</b></div>' for name,vals in sorted(data["by_employee"].items(),key=lambda kv:(-kv[1]["total"],kv[0].casefold()))[:12]) or '<p class="empty">Нет данных</p>'
    content=f'''<section class="page-heading"><div><p class="eyebrow">Руководитель</p><h1>Dashboard</h1><p>Сводная нагрузка и ключевые показатели</p></div></section>{_analytics_filters(query,'/admin/manager')}<section class="manager-kpi-grid"><div class="panel manager-kpi"><span>Заявок</span><strong>{data["total"]}</strong></div><div class="panel manager-kpi"><span>Открыто</span><strong>{data["open"]}</strong></div><div class="panel manager-kpi"><span>Первый ответ</span><strong>{data["avg_first"]} мин</strong></div><div class="panel manager-kpi"><span>Решение</span><strong>{data["avg_resolution"]} мин</strong></div><div class="panel manager-kpi"><span>Передано сменой</span><strong>{data["handoffs"]}</strong></div></section><section class="manager-dashboard-grid"><div class="panel"><div class="section-heading"><div><h2>Нагрузка по сотрудникам</h2></div></div><div class="manager-list">{emps}</div></div><div class="panel"><div class="section-heading"><div><h2>Частые проблемы</h2></div></div><div class="manager-list">{cats}</div></div></section>'''
    return admin_layout("Dashboard руководителя",content,"manager")


def render_admin_api_keys(query: dict[str, list[str]], *, new_token: str="", notice: str="") -> str:
    notice=notice or str(query.get("notice",[""])[0] or ""); notice_html=f'<div class="notice">{e(notice)}</div>' if notice else ''
    token_html=f'<section class="panel api-key-secret"><h2>Скопируйте ключ сейчас</h2><p>Полный секрет больше не показывается.</p><code>{e(new_token)}</code></section>' if new_token else ''
    rows=[]
    for item in AUTH.list_api_keys(200):
        enabled=bool(item.get("enabled")); revoke=f'<form method="post" action="/admin/api-keys"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="revoke"><input type="hidden" name="key_id" value="{int(item.get("id",0) or 0)}"><button class="button ghost" type="submit">Отключить</button></form>' if enabled else '<span class="muted">Отключён</span>'
        rows.append(f'<tr><td><strong>{e(str(item.get("name") or ""))}</strong><small>{e(str(item.get("token_prefix") or ""))}…</small></td><td>{e(str(item.get("scope") or "read"))}</td><td>{"Активен" if enabled else "Отключён"}</td><td>{e(human_time(str(item.get("last_used_at") or "")) or "—")}</td><td>{e(str(item.get("created_by_name") or "—"))}</td><td>{revoke}</td></tr>')
    table=''.join(rows) or '<tr><td colspan="6" class="empty">API-ключей пока нет</td></tr>'
    content=f'''{notice_html}{token_html}<section class="panel manual-panel"><div><p class="eyebrow">Интеграции</p><h2>Создать API-ключ</h2><p>Только read-only доступ.</p></div><form method="post" action="/admin/api-keys" class="manual-form"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="create"><label class="wide">Название<input name="name" maxlength="80" required></label><button class="button primary" type="submit">Создать ключ</button></form></section><section class="panel table-panel"><div class="section-heading"><div><h2>Ключи интеграций</h2><p>Секрет хранится только как SHA-256 хэш.</p></div></div><div class="table-scroll"><table><thead><tr><th>Название</th><th>Scope</th><th>Статус</th><th>Использован</th><th>Создал</th><th></th></tr></thead><tbody>{table}</tbody></table></div></section><section class="panel history-panel"><h2>Endpoints</h2><ul><li><code>GET /api/integration/ping</code></li><li><code>GET /api/integration/tickets?limit=100&amp;status=in_progress</code></li></ul></section>'''
    return admin_layout("API-ключи",content,"api_keys")


def _unified_audit_rows(query: dict[str, list[str]]) -> str:
    events = REPORTING.unified_audit_events(query, 400)
    return ''.join(
        f'<tr><td>{e(human_time(item["created_at"]))}</td><td>{e(item["actor"])}</td><td>{e(item["action"][:220])}</td><td>{e(item["object"] or "—")}</td><td>{e(item["source"])}</td></tr>'
        for item in events
    ) or '<tr><td colspan="5" class="empty">Событий нет</td></tr>'


def render_admin_system(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    state = system_status_snapshot()
    resources = process_resource_snapshot()
    backup_state = scheduled_backup_snapshot()
    restore_check=backup_state.get('restore_check') or {}
    restore_label = ('Проверка восстановления: успешно · '+str(restore_check.get('checked_at',''))+' · заявок: '+str(restore_check.get('tickets',0))+' · файлов: '+str(restore_check.get('files',0))) if restore_check.get('ok') else 'Проверка восстановления ещё не выполнялась'
    connector = state["connector"]
    database = state["database"]
    queue = state["queue"]
    counts = queue.get("counts", {}) if isinstance(queue, dict) else {}
    disk_total = int(state.get("disk_total", 0) or 0)
    disk_free = int(state.get("disk_free", 0) or 0)
    disk_percent = int(round((disk_free / disk_total) * 100)) if disk_total else 0
    connector_ok = bool(connector.get("connected")) if isinstance(connector, dict) else False
    connector_status = str(connector.get("status", "offline")) if isinstance(connector, dict) else "offline"
    heartbeat_age = state.get("heartbeat_age")
    local_monitor = state.get("monitor_mode") == "local"
    heartbeat_text = (
        "не используется в локальном Windows-режиме"
        if local_monitor
        else ("нет данных" if heartbeat_age is None else ("только что" if int(heartbeat_age) < 60 else f"{int(heartbeat_age)//60} мин назад"))
    )
    monitor_label = "Локальный режим" if local_monitor else ("Активно" if state.get("monitor_ok") else "Нет свежего сигнала")
    cards = f"""
      <section class="system-status-grid">
        <div class="panel system-card"><span>Сайт</span><strong class="system-ok">Работает</strong><small>HTTP-приложение отвечает</small></div>
        <div class="panel system-card"><span>WhatsApp</span><strong class="{'system-ok' if connector_ok else 'system-bad'}">{e('Подключён' if connector_ok else connector_status)}</strong><small>Последний сигнал: {e(str(connector.get('updated_at',''))[:19] if isinstance(connector,dict) else '')}</small></div>
        <div class="panel system-card"><span>База данных</span><strong class="{'system-ok' if database.get('ok') else 'system-bad'}">{e('OK' if database.get('ok') else 'Ошибка')}</strong><small>{e(str(database.get('result','')))}</small></div>
        <div class="panel system-card"><span>Автовосстановление</span><strong class="{'system-ok' if state.get('monitor_ok') else 'system-warn'}">{e(monitor_label)}</strong><small>{e('На Windows watchdog отключён' if local_monitor else 'Проверка: ' + heartbeat_text)}</small></div>
      </section>
    """
    queue_rows = []
    status_labels = {"pending":"ожидает", "processing":"отправляется", "sent":"отправлено", "failed":"ошибка", "sending":"отправляется", "uncertain":"проверьте переписку"}
    for row in queue.get("rows", []) if isinstance(queue, dict) else []:
        status = str(row.get("status", ""))
        target = str(row.get("chat_id") or row.get("phone") or "")
        err = str(row.get("last_error") or "")
        queue_rows.append(
            f"<tr><td>#{int(row.get('id',0) or 0)}</td><td>{e(status_labels.get(status,status))}</td>"
            f"<td>{int(row.get('attempt_count',0) or 0)}/{int(row.get('max_attempts',4) or 4)}</td>"
            f"<td>{e(target[:34])}</td><td>{e(err[:120] or '—')}</td><td>{e(human_time(str(row.get('updated_at') or row.get('created_at') or '')))}</td></tr>"
        )
    queue_table = "".join(queue_rows) or '<tr><td colspan="6" class="empty">Очередь пока пуста</td></tr>'
    logs = "".join(f"<li><code>{e(line)}</code></li>" for line in state.get("logs", [])) or '<li class="empty">Журнал мониторинга пока пуст</li>'
    content = f"""
      {notice_html}
      {cards}
      <section class="panel system-storage">
        <div class="section-heading"><div><h2>Хранилище</h2><p>Свободно {human_bytes(disk_free)} из {human_bytes(disk_total)} ({disk_percent}%)</p></div></div>
        <div class="system-metrics"><div><span>База</span><strong>{human_bytes(int(state.get('db_size',0) or 0))}</strong></div><div><span>Медиа чатов</span><strong>{human_bytes(int(state.get('media_size',0) or 0))}</strong></div><div><span>RAM процесса</span><strong>{human_bytes(int(resources.get('rss_bytes',0) or 0))}</strong></div><div><span>CPU</span><strong>{int(resources.get('cpu_count',1) or 1)} ядер</strong><small>{'Load: ' + e(str(resources.get('load'))) if resources.get('load') else 'CPU time: ' + e(str(resources.get('cpu_time'))) + ' с'}</small></div></div>
        <form class="system-actions" method="post" action="/admin/system"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="cleanup"><button class="button" type="submit" onclick="return confirm('Запустить очистку старых данных сейчас?')">Очистить сейчас</button></form>
      </section>
      <section class="panel"><div class="section-heading"><div><h2>Автоматические резервные копии</h2><p>{e(restore_label)}</p><p>Последняя: {e(str(backup_state.get('last_file') or 'ещё нет'))} · копий: {int(backup_state.get('count',0) or 0)}{(' · ошибка: ' + e(str(backup_state.get('last_error')))) if backup_state.get('last_error') else ''}</p></div></div>
        <form class="manual-form" method="post" action="/admin/system"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="backup_settings"><label>Интервал, часов<input type="number" name="backup_hours" min="1" max="168" value="{int(backup_state.get('hours',24) or 24)}"></label><label>Хранить копий<input type="number" name="backup_keep" min="2" max="90" value="{int(backup_state.get('keep',14) or 14)}"></label><button class="button" type="submit">Сохранить расписание</button></form>
        <form class="system-actions" method="post" action="/admin/system"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="backup_now"><button class="button primary" type="submit">Создать копию сейчас</button></form>
      </section>
      <section class="panel table-panel">
        <div class="section-heading"><div><h2>Очередь отправки</h2><p>Ожидает: {int(counts.get('pending',0) or 0)} · Отправляется: {int(counts.get('processing',0) or 0) + int(counts.get('sending',0) or 0)} · Отправлено: {int(counts.get('sent',0) or 0)} · Требует проверки: {int(counts.get('uncertain',0) or 0)} · Ошибка: {int(counts.get('failed',0) or 0)}</p></div>
        <form method="post" action="/admin/system"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="retry_failed"><button class="button" type="submit">Повторить ошибки</button></form></div>
        <div class="table-scroll"><table><thead><tr><th>ID</th><th>Статус</th><th>Попытки</th><th>Получатель</th><th>Ошибка</th><th>Обновлено</th></tr></thead><tbody>{queue_table}</tbody></table></div>
      </section>
      <section class="panel system-log"><div class="section-heading"><div><h2>Журнал мониторинга</h2><p>Последние проверки и автоматические перезапуски</p></div></div><ul>{logs}</ul></section>
    """
    return admin_layout("Система", content, "system")


def admin_employee_count(query: dict[str, list[str]]) -> int:
    try:
        requested = int(query.get("count", [str(len(EMPLOYEES))])[0])
    except ValueError:
        requested = len(EMPLOYEES)
    return max(1, min(MAX_EMPLOYEES, requested))


def render_admin_employees(query: dict[str, list[str]]) -> str:
    count = admin_employee_count(query)
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""

    proposed = list(EMPLOYEES[:count])
    while len(proposed) < count:
        proposed.append(f"Сотрудник {len(proposed) + 1}")

    fields = "".join(
        f'<label>Сотрудник {index}'
        f'<input name="employee_{index}" value="{e(name)}" maxlength="60" required>'
        f'</label>'
        for index, name in enumerate(proposed, start=1)
    )

    content = f"""
      <section class="section-heading"><div><h2>Сотрудники</h2><p>Количество, имена и список выбора сотрудника на смене</p></div></section>
      {notice_html}
      <section class="panel manual-panel">
        <div>
          <h2>Количество сотрудников</h2>
          <p>Сейчас в системе: <strong>{len(EMPLOYEES)}</strong></p>
        </div>
        <form class="manual-form" method="get" action="/admin/employees">
          <label>Количество
            <input type="number" name="count" value="{count}" min="1" max="{MAX_EMPLOYEES}" required>
          </label>
          <button class="button" type="submit">Применить количество</button>
        </form>
      </section>
      <section class="panel">
        <h2>Имена сотрудников</h2>
        <form class="manual-form" method="post" action="/admin/employees">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <input type="hidden" name="employee_count" value="{count}">
          {fields}
          <button class="button primary" type="submit">Сохранить</button>
        </form>
        <p class="muted">Новые имена применяются к спискам выбора и новым назначениям. История уже созданных заявок не переписывается.</p>
      </section>
    """
    return admin_layout("Сотрудники", content, "employees")


def render_admin_manual(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    content = f"""
      {notice_html}
      <section class="section-heading"><div><h2>Ручное создание заявки</h2><p>Для обращения, которое нужно завести в систему без WhatsApp</p></div></section>
      <section class="panel">
        <form class="manual-form" method="post" action="/admin/manual">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <label>Название<input name="title" maxlength="160" placeholder="Например, Открытие НП" required></label>
          <label>Категория<select name="category">{category_options('general')}</select></label>
          <label>Приоритет<select name="priority">{priority_options('normal')}</select></label>
          <label>Сотрудник<select name="employee">{employee_options()}</select></label>
          <label>Статус<select name="status"><option value="new">Не тронута</option><option value="in_progress">В работе</option><option value="done">Сделано</option><option value="invalid">Недействительная</option></select></label>
          <label class="wide">Описание<textarea name="summary" rows="4" maxlength="2000" placeholder="Номер НП, перевозки, БИН или подробности" required></textarea></label>
          <button class="button primary" type="submit">Создать заявку</button>
        </form>
      </section>
    """
    return admin_layout("Ручная заявка", content, "manual")


def _admin_flow_message(title: str, text: str, note: str = "") -> str:
    note_html = f'<small class="admin-flow-note">{e(note)}</small>' if note else ""
    return (
        '<article class="panel admin-flow-card">'
        f'<div class="admin-flow-card-head"><h3>{e(title)}</h3>{note_html}</div>'
        f'<pre class="admin-flow-message">{e(text)}</pre>'
        '</article>'
    )


def _admin_live_flow_snapshot(language: str) -> dict[str, object]:
    previous_language = queue_user_locale.current_language()
    queue_user_locale.set_language(language)
    try:
        labels = V3_MENU_LABELS_KZ if queue_user_locale.is_kz() else V3_MENU_LABELS_RU
        categories: list[tuple[str, str]] = []
        for number in range(1, 8):
            category = V3_MENU_CATEGORIES[str(number)]
            categories.append((f"{number}. {labels[str(number)]}", configured_category_prompt(category)))
        sample_ticket = {
            "id": 12345,
            "category": "seal",
            "status": "in_progress",
            "summary": tr("Пример активной заявки", "Белсенді өтінім мысалы"),
            "original_text": "",
        }
        close_reason = next(iter(queue_workflow.ALLOWED_CLOSE_REASONS.keys()), "")
        return {
            "profile": profile_prompt_text(),
            "menu": main_menu_text(),
            "categories": categories,
            "active": active_tickets_reply([sample_ticket]),
            "active_empty": active_tickets_reply([]),
            "accepted": ticket_accepted_reply(12345),
            "missing": v3_missing_fields_reply(["reference", "problem"]),
            "duplicate": v3_duplicate_reply(12345),
            "done": ticket_status_reply_text(
                "done", 12345,
                close_comment=tr("Комментарий специалиста", "Маманның пікірі"),
            ),
            "invalid": ticket_status_reply_text(
                "invalid", 12345,
                close_reason=close_reason,
                close_comment=tr("Комментарий специалиста", "Маманның пікірі"),
            ),
        }
    finally:
        queue_user_locale.set_language(previous_language)


def _admin_flow_language_column(language: str, title: str) -> str:
    snapshot = _admin_live_flow_snapshot(language)
    category_cards = "".join(
        _admin_flow_message(label, prompt, "После выбора пункта меню")
        for label, prompt in snapshot["categories"]
    )
    return f'''
      <section class="admin-flow-language" id="flow-{e(language)}">
        <div class="section-heading admin-flow-language-heading"><div><p class="eyebrow">{e(language.upper())}</p><h2>{e(title)}</h2><p>Тексты ниже формируются теми же функциями, которые отвечают пользователю WhatsApp.</p></div></div>
        {_admin_flow_message("Шаг 2 · Пост и должность", str(snapshot["profile"]))}
        {_admin_flow_message("Шаг 3 · Главное меню", str(snapshot["menu"]))}
        <details class="panel admin-flow-group" open><summary>Категории 1–7</summary><div class="admin-flow-stack">{category_cards}</div></details>
        <details class="panel admin-flow-group"><summary>Пункт 8 · Мои активные заявки</summary><div class="admin-flow-stack">{_admin_flow_message("Есть активные заявки", str(snapshot["active"]))}{_admin_flow_message("Активных заявок нет", str(snapshot["active_empty"]))}</div></details>
        <details class="panel admin-flow-group" open><summary>Системные ответы</summary><div class="admin-flow-stack">
          {_admin_flow_message("Заявка принята", str(snapshot["accepted"]))}
          {_admin_flow_message("Не хватает данных", str(snapshot["missing"]))}
          {_admin_flow_message("Дублирующаяся заявка", str(snapshot["duplicate"]))}
          {_admin_flow_message("Заявка выполнена", str(snapshot["done"]))}
          {_admin_flow_message("Заявка закрыта / отклонена", str(snapshot["invalid"]))}
        </div></details>
      </section>
    '''


def render_admin_menu(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    language_text = language_selection_text_v3()
    content = f'''
      {notice_html}
      <section class="section-heading admin-flow-heading">
        <div><p class="eyebrow">Живой сценарий</p><h2>Актуальные тексты WhatsApp</h2><p>Это не отдельная копия текста: предпросмотр строится из тех же функций, которые используются при реальном общении с пользователем.</p></div>
        <div class="admin-flow-jumps"><a class="button ghost" href="#flow-ru">Русский</a><a class="button ghost" href="#flow-kz">Қазақша</a></div>
      </section>
      <section class="notice success admin-flow-live-note"><strong>Источник истины один.</strong> Если пользовательский сценарий меняется в коде, эта страница автоматически показывает новую версию после обновления.</section>
      {_admin_flow_message("Шаг 1 · Выбор языка", language_text, "Общее сообщение RU/KZ")}
      <div class="admin-flow-columns">
        {_admin_flow_language_column(queue_user_locale.LANG_RU, "Русский сценарий")}
        {_admin_flow_language_column(queue_user_locale.LANG_KZ, "Қазақша сценарий")}
      </div>
      <section class="panel admin-flow-legacy-note"><h3>Старый редактор меню отключён на этой странице</h3><p class="muted">Новый пользовательский сценарий фиксирован как 1–8 и использует отдельные RU/KZ тексты. Поэтому старые поля request_menu_json больше не показываются как будто они управляют текущим WhatsApp-меню.</p></section>
    '''
    return admin_layout("Тексты WhatsApp", content, "menu")


def render_admin_categories(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows: list[str] = []
    ticket_count = 0
    for index, option in enumerate(MENU_OPTIONS, start=1):
        if str(option.get("action", MENU_ACTION_TICKET)) != MENU_ACTION_TICKET:
            continue
        ticket_count += 1
        checked = "checked" if bool(option.get("enabled")) else ""
        rows.append(
            f"""<article class="menu-editor-row panel">
              <div class="menu-editor-number">{ticket_count}</div>
              <input type="hidden" name="key_{index}" value="{e(str(option['key']))}">
              <label>Название категории<input name="label_{index}" maxlength="80" value="{e(str(option['label']))}" required></label>
              <label class="checkbox-label"><input type="checkbox" name="enabled_{index}" value="1" {checked}>Доступна для новых заявок</label>
              <label class="checkbox-label danger-choice"><input type="checkbox" name="delete_{index}" value="1">Удалить категорию</label>
            </article>"""
        )
    empty = '<div class="panel empty">Категорий заявок пока нет</div>' if not rows else ""
    content = f"""
      {notice_html}
      <section class="section-heading"><div><h2>Категории заявок</h2><p>Добавляйте, переименовывайте или убирайте виды категорий без изменения кода</p></div></section>
      <form class="menu-editor" method="post" action="/admin/categories">
        <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
        <input type="hidden" name="action" value="save">
        <input type="hidden" name="row_count" value="{len(MENU_OPTIONS)}">
        {''.join(rows)}
        {empty}
        <button class="button primary" type="submit">Сохранить категории</button>
      </form>
      <section class="panel add-menu-option">
        <div><h2>Добавить категорию</h2><p>Новая категория сразу появится в меню обращений, ручной заявке и выборе категории. Для неё используется универсальный сбор описания проблемы.</p></div>
        <form class="manual-form" method="post" action="/admin/categories">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <input type="hidden" name="action" value="add">
          <label>Название<input name="new_label" maxlength="80" placeholder="Например, Доступ к системе" required></label>
          <button class="button" type="submit">Добавить категорию</button>
        </form>
        <p class="muted">При удалении старые заявки не ломаются: название категории сохраняется в истории, но для новых обращений она больше не показывается.</p>
      </section>
    """
    return admin_layout("Категории", content, "categories")


def avatar_html(chat_id: str, name: str) -> str:
    url = queue_avatars.url(sys.modules[__name__],chat_id)
    letters = ''.join(word[0] for word in name.split()[:2]) or '?'
    pic = f'<img src="{e(url)}" alt="" loading="lazy" onerror="this.remove()">' if url else ''
    return f'<span class="chat-avatar table-avatar">{e(letters)}{pic}</span>'


def discovered_contact_rows(search_raw: str = "", limit: int = 80) -> str:
    with CHAT_LOCK:
        discovered = list(CHAT_STATE.get("discovered_contacts", [])) if isinstance(CHAT_STATE.get("discovered_contacts", []), list) else []
    found_rows: list[str] = []
    for item in discovered:
        if not isinstance(item, dict):
            continue
        name = normalize_message(str(item.get("name", "")))[:100]
        phone = normalize_phone(str(item.get("phone", "")))
        chat_id = valid_chat_id(str(item.get("chat_id", "")))
        if not phone or not chat_id or not flexible_contact_match(search_raw, name, phone, chat_id):
            continue
        saved_text = "Да" if item.get("saved") else "Нет"
        default_name = name or display_phone(phone)
        duplicate = bool(STORE.manual_whatsapp_contact(chat_id, phone))
        if duplicate:
            action = '<span class="duplicate-badge">Уже добавлен</span>'
        else:
            action = f"""<form method="post" action="/admin/contacts"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="save"><input type="hidden" name="name" value="{e(default_name)}"><input type="hidden" name="phone" value="{e(phone)}"><button class="button primary" type="submit">Добавить</button></form>"""
        found_rows.append(
            f"""<tr><td><span class="avatar-name">{avatar_html(chat_id,default_name)}<strong>{e(default_name)}</strong></span></td><td>{e(display_phone(phone))}</td><td>{e(saved_text)}</td><td class="table-actions"><a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат</a>{action}</td></tr>"""
        )
        if len(found_rows) >= max(1, min(limit, 200)):
            break
    return "".join(found_rows) or '<tr><td colspan="4" class="empty">Ничего не найдено. Попробуйте имя, часть номера или обновите контакты.</td></tr>'


def render_admin_contacts(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows = []
    for contact in STORE.list_manual_whatsapp_contacts():
        chat_id = str(contact["chat_id"])
        rows.append(f"""<tr><td><span class="avatar-name">{avatar_html(chat_id,str(contact['name']))}<strong>{e(contact['name'])}</strong></span></td><td>{e(display_phone(str(contact['phone'])))}</td><td>{e(human_time(str(contact['created_at'])))}</td><td class="table-actions"><a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Профиль / чат</a><form method="post" action="/admin/contacts" onsubmit="return confirm('Удалить контакт из системы?')"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="delete"><input type="hidden" name="chat_id" value="{e(chat_id)}"><button class="button ghost" type="submit">Удалить</button></form></td></tr>""")
    table = "".join(rows) or '<tr><td colspan="4" class="empty">Контакты вручную ещё не добавлены</td></tr>'
    search_raw = query.get("q", [""])[0]
    found_table = discovered_contact_rows(search_raw, 80)
    content = f"""
      {notice_html}
      <section class="panel manual-panel admin-contact-form"><div><h2>Добавить пользователя WhatsApp</h2><p>У добавленных контактов автоответчик всегда отключён. Они используются для обычной личной переписки.</p></div><form class="manual-form" method="post" action="/admin/contacts"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="save"><label>Имя<input name="name" maxlength="100" required></label><label>Мобильный номер<input name="phone" inputmode="tel" maxlength="30" placeholder="+7 777 123 45 67" required></label><button class="button primary" type="submit">Добавить контакт</button></form></section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>Добавленные контакты</h2><p>Для них нет переключателя автоответчика: автоматика выключена постоянно.</p></div></div><div class="table-scroll"><table><thead><tr><th>Имя</th><th>Номер</th><th>Добавлен</th><th>Действия</th></tr></thead><tbody>{table}</tbody></table></div></section>
      <section class="panel table-panel" data-contact-search-panel><div class="section-heading"><div><h2>Поиск контактов WhatsApp</h2><p>Можно вводить имя, часть номера, +7 или 8. Пробелы, скобки и дефисы не мешают поиску.</p></div><a class="button" href="/admin/contacts?q={quote(search_raw)}">Обновить</a></div><form class="filters" method="get" action="/admin/contacts" data-contact-search-form><input type="search" name="q" value="{e(search_raw)}" placeholder="Имя или номер" data-live-contact-search autocomplete="off"><button class="button primary" type="submit">Найти</button><a class="button ghost" href="/admin/contacts">Сбросить</a></form><div class="table-scroll"><table><thead><tr><th>Имя</th><th>Номер</th><th>Сохранён в WhatsApp</th><th>Действия</th></tr></thead><tbody data-contact-search-results>{found_table}</tbody></table></div></section>
    """
    return admin_layout("Контакты WhatsApp", content, "contacts")


