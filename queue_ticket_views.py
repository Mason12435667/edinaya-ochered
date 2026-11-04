from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import queue_auth
import queue_contextual_tickets
import queue_operations
import queue_reliability
from queue_auth_views import script_tags, stylesheet_tags
from queue_core import display_phone, e, human_time, log_suppressed, normalize_phone, parse_iso_datetime
from queue_work_context import active_employee, category_options, employee_identity, employee_options, priority_badge, priority_options, query_page, render_pagination, render_ticket_rows, sla_badge, status_badge, ticket_sla_state, work_actor

# Application module (app.py): shared services, settings and helpers it defines.
app: Any = None


def bind(app_module: Any) -> None:
    global app
    app = app_module


# Load order is the CSS cascade order: later files override earlier ones.
MAIN_STYLESHEETS = (
    "style.css", "ui.css", "instruction.css", "global-theme.css", "productivity.css", "workflow.css",
    "notifications.css", "chat.css", "auth.css", "pink-theme.css", "polish.css", "user-ui.css",
)
MAIN_SCRIPTS = (
    "http.js", "ui.js", "app.js", "productivity.js", "workflow.js", "reliability.js",
    "chat-layout.js", "performance.js", "attachments.js", "account.js",
)


def layout(
    title: str,
    content: str,
    active: str = "tickets",
    show_admin: bool = False,
) -> str:
    nav_items = [
        ("tickets", "/", "Заявки", ""),
        ("whatsapp", "/whatsapp", "WhatsApp", ""),
        ("groups", "/groups", "Группы", ""),
        ("instruction", app.INSTRUCTION_URL, "Инструкция", ""),
    ]
    if os.getenv('QUEUE_PROFILE', 'generic').strip().lower() == 'generic':
        nav_items = [item for item in nav_items if item[0] != 'instruction']
    auth_user = queue_auth.current_user() or {}
    if show_admin:
        nav_items.append(("admin", "/admin/", "Админка", ""))
    nav_items.append(("logout", "/logout", "Выйти", ""))
    nav = "".join(
        f'<a class="nav-link {"active" if key == active else ""}" href="{e(href)}"{attributes}>{e(label)}</a>'
        for key, href, label, attributes in nav_items
    )
    current_employee = active_employee()
    auth_name = str(auth_user.get("display_name") or auth_user.get("username") or "Администратор")
    auth_role = "Администратор" if str(auth_user.get("role")) == "admin" else "Сотрудник"
    user_id = int(auth_user.get("id", 0) or 0)
    user_prefs = app.AUTH.get_user_preferences(user_id) if user_id > 0 else {}
    preferred_theme = str(user_prefs.get("theme") or "").strip()
    if preferred_theme not in {"light", "dark", "pink"}:
        preferred_theme = ""
    shift_note = "Работает от имени аккаунта" if str(auth_user.get("role")) == "admin" else f"На смене автоматически: {e(employee_identity(auth_user) or current_employee)}"
    legacy_note = bool(auth_user.get("legacy"))
    password_form = "" if legacy_note else f'''<form class="auth-profile-password" method="post" action="/account/password">
          <input type="hidden" name="csrf_token" value="{e(app.csrf_token())}">
          <input type="hidden" name="next" value="/">
          <label>Текущий пароль<input type="password" name="current_password" autocomplete="current-password" required></label>
          <label>Новый пароль<input type="password" name="new_password" minlength="10" autocomplete="new-password" required></label>
          <label>Повторите пароль<input type="password" name="new_password2" minlength="10" autocomplete="new-password" required></label>
          <button class="button primary" type="submit">Сменить пароль</button>
        </form>'''
    auth_profile = f'''<details class="auth-profile-menu">
      <summary class="auth-user-chip" title="Профиль"><strong>{e(auth_name)}</strong><small>{e(auth_role)}</small></summary>
      <div class="auth-profile-popover">
        <div class="auth-profile-head"><strong>{e(auth_name)}</strong><span>@{e(str(auth_user.get("username") or ""))}</span><small>{shift_note}</small></div>
        {password_form}
        <a class="button ghost auth-profile-page" href="/me">Моя страница</a>
        <a class="button ghost auth-profile-page" href="/accounts">Сменить аккаунт</a>
        <a class="button ghost auth-profile-logout" href="/logout">Выйти</a>
      </div>
    </details>'''
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="queue-csrf" content="{e(app.csrf_token())}">
  <title>{e(title)} · Единая очередь</title>
  <meta name="theme-color" content="#112a25">
  <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon.png?v=3.3.79">
  <link rel="apple-touch-icon" href="/static/favicon.png?v=3.3.79">
  <script>document.documentElement.dataset.theme = {json.dumps(preferred_theme)} || localStorage.getItem("queue-theme") || "light";</script>
{stylesheet_tags(MAIN_STYLESHEETS)}
</head>
<body data-admin="{'1' if show_admin else '0'}" data-auth-user-id="{user_id}">
  <header class="topbar">
    <a class="brand" href="/"><span class="brand-mark">Q</span><span>Единая очередь</span></a>
    {'<span class="local-mode-badge">WINDOWS · ТЕСТ</span>' if app.LOCAL_MODE else ''}
    <nav>{nav}</nav>
    <button class="notification-button" type="button" data-notification-center aria-label="Уведомления">
      <span aria-hidden="true">🔔</span><b data-notification-count hidden>0</b>
    </button>
    <div class="notification-popover" data-notification-popover hidden>
      <strong>Уведомления</strong><div data-notification-items>Проверка новых событий...</div>
    </div>
    {auth_profile}
    <button class="theme-toggle" type="button" data-theme-toggle aria-label="Сменить оформление">Тёмная тема</button>
  </header>
  <div class="toast-stack" data-toast-stack aria-live="polite" aria-atomic="false"></div>
  {'<div class="maintenance-banner">Режим обслуживания включён: просмотр доступен, изменения сотрудников временно заблокированы.</div>' if queue_reliability.maintenance_active(app.STORE) else ''}
  <main class="page">{content}</main>
  <footer>Единая очередь · рабочая система обработки заявок</footer>
{script_tags(MAIN_SCRIPTS)}
</body>
</html>"""


def render_instruction(show_admin: bool = False, query: dict[str, list[str]] | None = None) -> str:
    """Generic profile: per-organization knowledge base is not bundled."""
    return layout(
        "База знаний",
        '<section class="card"><h1>База знаний</h1><p>Материалы пока не настроены.</p></section>',
        show_admin=show_admin,
    )




def render_dashboard(query: dict[str, list[str]], show_admin: bool = False) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    selected_status = query.get("status", [""])[0]
    selected_category = query.get("category", [""])[0]
    selected_priority = query.get("priority", [""])[0]
    selected_view = query.get("view", [""])[0]
    if selected_view not in {"", "mine", "handoff", "open", "urgent"}:
        selected_view = ""
    search = query.get("q", [""])[0]
    try:
        no_answer_minutes = max(0, min(10080, int(query.get("no_answer_minutes", ["0"])[0] or 0)))
    except ValueError:
        no_answer_minutes = 0
    current_employee = work_actor()
    if no_answer_minutes:
        all_filtered = app.STORE.list_tickets(
            selected_status, selected_category, selected_priority, search, 5000, 0, selected_view, current_employee,
        )
        all_filtered = [item for item in all_filtered if ticket_sla_state(item).get("active") and int(ticket_sla_state(item).get("elapsed_seconds",0) or 0) >= no_answer_minutes * 60]
        total = len(all_filtered)
        pages = max(1, (total + app.PAGE_SIZE - 1) // app.PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = all_filtered[(page - 1) * app.PAGE_SIZE:page * app.PAGE_SIZE]
    else:
        total = app.STORE.count_tickets(
            selected_status, selected_category, selected_priority, search, selected_view, current_employee,
        )
        pages = max(1, (total + app.PAGE_SIZE - 1) // app.PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = app.STORE.list_tickets(
            selected_status,
            selected_category,
            selected_priority,
            search,
            app.PAGE_SIZE,
            (page - 1) * app.PAGE_SIZE,
            selected_view,
            current_employee,
        )
    counts = app.STORE.counts()
    shift_summary = app.STORE.shift_summary(current_employee)
    cards = "".join(
        f"""<a class="metric metric-{key}" href="/{'?status=' + key if key != 'all' else ''}">
          <span>{label}</span><strong data-count-status="{key}">{counts.get(key, 0)}</strong>
        </a>"""
        for key, label in [
            ("all", "Всего"),
            ("new", "Не тронуты"),
            ("in_progress", "В работе"),
            ("done", "Сделано"),
            ("invalid", "Недействительные"),
        ]
    )
    shift_views = [
        ("mine", "Мои заявки", int(shift_summary.get("mine", 0) or 0)),
        ("handoff", "От предыдущей смены", int(shift_summary.get("handoff", 0) or 0)),
        ("open", "Незакрытые", int(shift_summary.get("open", 0) or 0)),
        ("urgent", "Срочные", int(shift_summary.get("urgent", 0) or 0)),
    ]
    shift_cards = "".join(
        f'<a class="shift-metric {"active" if selected_view == key else ""}" href="/?view={key}"><span>{e(label)}</span><strong>{amount}</strong></a>'
        for key, label, amount in shift_views
    )
    previous_employee = str(shift_summary.get("previous_employee", "") or "")
    shift_note = f'<small>Предыдущая смена: {e(previous_employee)}</small>' if previous_employee else '<small>Передача начнёт заполняться при смене сотрудника</small>'
    status_options = '<option value="">Все статусы</option>' + "".join(
        f'<option value="{e(key)}" {"selected" if key == selected_status else ""}>{e(label)}</option>'
        for key, label in app.STATUSES.items()
    )
    table_body = render_ticket_rows(tickets)
    handoff_tickets = [item for item in app.STORE.list_tickets("", "", "", "", 500, 0) if int(item.get("shift_handoff", 0) or 0)][:20]
    if handoff_tickets:
        handoff_items = "".join(
            f'<a class="handoff-item" href="/ticket?id={int(item["id"])}"><strong>#{int(item["id"])} · {e(str(item.get("title", "Заявка")))}</strong><span>{e(str(item.get("sender", "")))}</span><small>Передал: {e(str(item.get("handoff_from", "")))} · {e(human_time(str(item.get("handoff_at", ""))))}</small></a>'
            for item in handoff_tickets
        )
        handoff_board = f'<section class="panel handoff-board"><div class="section-heading"><div><p class="eyebrow">Передача смены</p><h2>Переданные заявки <span class="handoff-badge large">{len(handoff_tickets)}</span></h2><p>Отдельный список заявок, которые ждут следующую смену</p></div></div><div class="handoff-list">{handoff_items}</div></section>'
    else:
        handoff_board = ""
    content = f"""
      <section class="page-heading">
        <div><p class="eyebrow">Service Desk</p><h1>Заявки</h1><p>WhatsApp-запросы и ручные заявки в одной очереди</p></div>
        <a class="button primary" href="/whatsapp">Открыть WhatsApp</a>
      </section>
      {notice_html}
      <section class="metrics">{cards}</section>
      <section class="smart-shift-panel"><div><strong>Текущая смена: {e(current_employee)}</strong>{shift_note}</div><div class="shift-metrics">{shift_cards}</div></section>
      {handoff_board}
      <section class="panel filters-panel">
        <form class="filters" method="get" action="/" data-dashboard-filter-form>
          <input type="hidden" name="view" value="{e(selected_view)}">
          <input type="search" name="q" value="{e(search)}" placeholder="№ заявки, ФИО, телефон, подразделение, текст..." data-live-ticket-search autocomplete="off">
          <select name="status">{status_options}</select>
          <select name="category">{category_options(selected_category, True)}</select>
          <select name="priority">{priority_options(selected_priority, True)}</select>
          <input type="number" name="no_answer_minutes" value="{no_answer_minutes or ''}" min="0" max="10080" step="5" placeholder="Без ответа > N мин" title="Показать заявки без первого ответа дольше N минут">
          <button class="button" type="submit">Применить</button>
          <a class="button ghost" href="/">Сбросить</a>
        </form>
      </section>
      <section class="panel table-panel" data-dashboard data-dashboard-version="{e(app.STORE.dashboard_version())}">
        <div class="table-hint"><span>Приоритет меняется в левом столбце. Правой кнопкой мыши по заявке можно быстро изменить статус.</span><span id="auto-refresh-state">Автообновление включено</span></div>
        <div class="table-scroll"><table>
          <thead><tr><th>Приоритет</th><th>№</th><th>Источник</th><th>Отправитель</th><th>Запрос</th><th>Статус</th><th>Время</th></tr></thead>
          <tbody id="ticket-table-body">{table_body}</tbody>
        </table></div>
        <div class="pagination" id="ticket-pagination">{render_pagination(query, total, page)}</div>
      </section>
      <section class="panel manual-panel">
        <div><p class="eyebrow">Ручная заявка</p><h2>Быстро добавить запрос</h2><p>Для обращений, поступивших по телефону, почте или из других каналов</p></div>
        <form class="manual-form" method="post" action="/manual">
          <input type="hidden" name="csrf_token" value="{e(app.csrf_token())}">
          <label>Тип запроса<input name="title" value="" placeholder="Например, сброс пароля" required></label>
          <label>Номер / идентификатор<input name="reference" placeholder="Например, 784512"></label>
          <label class="wide">Комментарий<textarea name="comment" rows="2" placeholder="Дополнительная информация"></textarea></label>
          <label>Приоритет<select name="priority">{priority_options('normal')}</select></label>
          <label>Сотрудник<select name="employee">{employee_options()}</select></label>
          <label>Начальный статус<select name="status"><option value="new">Не сделано</option><option value="done">Сделано</option></select></label>
          <button class="button primary" type="submit">Добавить</button>
        </form>
      </section>
      <div id="ticket-context-menu" class="context-menu" hidden>
        <div class="context-heading"><small>Изменить статус</small><strong id="context-ticket-title">Заявка</strong></div>
        <button type="button" data-quick-status="new"><span class="context-dot dot-new"></span>Не тронута</button>
        <button type="button" data-quick-status="in_progress"><span class="context-dot dot-in_progress"></span>В работе</button>
        <button type="button" data-quick-status="done"><span class="context-dot dot-done"></span>Сделано</button>
        <button type="button" data-quick-status="invalid"><span class="context-dot dot-invalid"></span>Недействительная</button>
        <button type="button" data-quick-handoff="1"><span class="context-dot dot-handoff"></span>Передать следующей смене</button>
      </div>
    """
    return layout("Заявки", content, show_admin=show_admin)


def render_ticket(
    ticket_id: int,
    query: dict[str, list[str]],
    show_admin: bool = False,
) -> str:
    ticket = app.STORE.get_ticket(ticket_id)
    if not ticket:
        return layout(
            "Не найдено",
            '<section class="panel empty">Заявка не найдена</section>',
            show_admin=show_admin,
        )
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    events = app.STORE.ticket_events(ticket_id)
    event_rows = "".join(
        f'<li><span>{e(event["action"])}</span><small>{e(event["actor"])} · {e(human_time(event["created_at"]))}</small></li>'
        for event in events
    )
    context_fields = queue_contextual_tickets.ticket_fields(app.STORE, ticket_id)
    context_history = queue_contextual_tickets.ticket_history(app.STORE, ticket_id, 120)
    context_field_rows = "".join(
        f'<div class="context-field-row"><span>{e(queue_contextual_tickets.FIELD_LABELS.get(str(item.get("field_name","")), str(item.get("field_name",""))))}</span>'
        f'<strong>{e(str(item.get("value", "")))}</strong>'
        f'<small>{int(float(item.get("confidence",0) or 0)*100)}% · {e(str(item.get("source", "")))}</small></div>'
        for item in context_fields if str(item.get("value", "")).strip()
    )
    context_fields_panel = (
        '<section class="panel context-fields-panel"><h2>Контекстные поля</h2>'
        '<p class="muted">Данные, накопленные из нескольких сообщений. ФИО и подразделение могут оставаться необязательными.</p>'
        f'<div class="context-field-list">{context_field_rows}</div></section>'
        if context_field_rows else ""
    )
    field_option_html = "".join(
        f'<option value="{e(key)}">{e(label)}</option>'
        for key, label in queue_contextual_tickets.FIELD_LABELS.items()
    )
    history_cards: list[str] = []
    for item in context_history:
        event_type = str(item.get("event_type", ""))
        field_name = str(item.get("field_name", ""))
        old_value = str(item.get("old_value", ""))
        new_value = str(item.get("new_value", ""))
        text_value = str(item.get("text", ""))
        media_value = str(item.get("media_name", ""))
        display_value = new_value or text_value or media_value
        field_label = queue_contextual_tickets.FIELD_LABELS.get(field_name, field_name) if field_name else ""
        change_html = ""
        if field_name:
            if old_value and new_value:
                change_html = f'<p><strong>{e(field_label)}</strong>: <del>{e(old_value)}</del> → {e(new_value)}</p>'
            elif new_value:
                change_html = f'<p><strong>{e(field_label)}</strong>: {e(new_value)}</p>'
        elif text_value:
            change_html = f'<p class="context-history-text">{e(text_value)}</p>'
        if media_value:
            change_html += f'<p class="context-history-media">Файл: {e(media_value)}</p>'
        actions = ""
        if display_value:
            actions += (
                f'<form class="context-history-apply" method="post" action="/context-history/apply">'
                f'<input type="hidden" name="ticket_id" value="{ticket_id}">'
                f'<input type="hidden" name="history_id" value="{int(item.get("id",0) or 0)}">'
                '<input type="hidden" name="mode" value="field">'
                f'<select name="field_name">{field_option_html}</select>'
                '<button class="button" type="submit">В поле</button></form>'
            )
        if str(item.get("message_key", "")) and str(item.get("chat_id", "")):
            actions += (
                f'<form class="context-history-apply" method="post" action="/context-history/apply">'
                f'<input type="hidden" name="ticket_id" value="{ticket_id}">'
                f'<input type="hidden" name="history_id" value="{int(item.get("id",0) or 0)}">'
                '<input type="hidden" name="mode" value="media">'
                '<button class="button ghost" type="submit">Привязать сообщение / файл</button></form>'
            )
        history_cards.append(
            f'<article class="context-history-card"><div class="context-history-head"><strong>{e(event_type.replace("_", " "))}</strong>'
            f'<small>{e(human_time(str(item.get("created_at", ""))))} · {e(str(item.get("source", "")))}</small></div>'
            f'{change_html}<div class="context-history-actions">{actions}</div></article>'
        )
    context_history_panel = (
        '<section class="panel context-history-panel"><h2>Подробная история данных</h2>'
        '<p class="muted">Исходные сообщения, уточнения, исправления и источник каждого значения. Нужную запись можно перенести в поле заявки.</p>'
        f'<div class="context-history-list">{"".join(history_cards)}</div></section>'
        if history_cards else ""
    )
    chat_id = str(ticket.get("chat_id", "") or "").strip()
    if not chat_id:
        phone_digits = re.sub(r"\D", "", normalize_phone(str(ticket.get("phone", ""))))
        chat_id = f"{phone_digits}@c.us" if phone_digits else ""

    attachment = ""
    external_id = str(ticket.get("external_id", "") or "")
    media_items = app.STORE.list_ticket_whatsapp_media(ticket_id, chat_id, external_id, 24)
    if media_items:
        media_cards: list[str] = []
        for media_item in media_items:
            message_key = str(media_item.get("message_key", "") or "")
            if not message_key:
                continue
            media_url = f"/api/chat-media?chat_id={quote(str(media_item.get('chat_id', chat_id) or chat_id))}&message_id={quote(message_key)}"
            mime = str(media_item.get("media_mime", "") or "")
            name = str(media_item.get("media_name", "") or "Вложение")
            sender_name = str(media_item.get("sender", "") or ("Рабочий WhatsApp" if media_item.get("from_me") else ticket.get("sender", "Пользователь")))
            when = ""
            try:
                when = datetime.fromtimestamp(int(media_item.get("message_timestamp", 0) or 0), tz=timezone.utc).astimezone(app.LOCAL_TIMEZONE).strftime("%d.%m %H:%M")
            except (ValueError, TypeError, OSError):
                when = ""
            if mime.startswith("image/"):
                preview = (
                    f'<button class="media-zoom-button" type="button" data-media-zoom="{e(media_url)}" '
                    f'data-media-name="{e(name)}"><img class="ticket-media-image" src="{e(media_url)}" alt="{e(name)}"></button>'
                )
            elif mime.startswith("audio/"):
                preview = f'<audio class="ticket-media-audio" controls preload="metadata" src="{e(media_url)}"></audio>'
            elif mime.startswith("video/"):
                preview = f'<video class="ticket-media-video" controls preload="metadata" src="{e(media_url)}"></video>'
            else:
                preview = (
                    f'<div class="media-file-actions"><a class="button" target="_blank" href="{e(media_url)}">Открыть</a>'
                    f'<a class="button ghost" href="{e(media_url + "&download=1")}">Скачать</a></div>'
                )
            transcript = str(media_item.get("transcript", "") or "") if mime.startswith("audio/") else ""
            transcript_html = f'<div class="media-transcript"><strong>Расшифровка:</strong><p>{e(transcript)}</p></div>' if transcript else ""
            meta = " · ".join(part for part in (sender_name, when) if part)
            media_cards.append(
                f'<article class="ticket-media-card"><div class="attachment-name">{e(name)}</div>'
                f'<small class="ticket-media-meta">{e(meta)}</small>{preview}{transcript_html}</article>'
            )
        if media_cards:
            attachment = (
                '<div class="ticket-media-block"><h3>Файлы и медиа по заявке</h3>'
                '<p class="muted">Все медиа, связанные с этой заявкой, доступны прямо здесь. Фото открываются поверх текущей страницы.</p>'
                f'<div class="ticket-media-grid">{"".join(media_cards)}</div></div>'
            )
    elif ticket.get("attachment_name"):
        attachment = f'<div class="attachment">Вложение: {e(ticket["attachment_name"])}. Медиафайл не был сохранён старой версией системы.</div>'

    terminal_status = str(ticket["status"]) in {"done", "invalid"}
    if terminal_status and not show_admin:
        status_buttons = (
            '<div class="notice warning-notice status-lock-note">'
            'Заявка отработана. Изменить её статус может только администратор.'
            '</div>'
        )
    else:
        status_buttons = "".join(
            f'<button name="status" value="{e(key)}" class="status-action action-{e(key)}" type="submit">{e(label)}</button>'
            for key, label in app.STATUSES.items()
        )

    chat_button = (
        f'<a class="button whatsapp-button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат WhatsApp</a>'
        if chat_id
        else '<span class="button disabled" title="WhatsApp не передал телефонный номер">Номер для связи не определён</span>'
    )

    sla_state = ticket_sla_state(ticket)
    if sla_state.get("active"):
        sla_panel = f'''
          <section class="panel sla-panel {'sla-panel-overdue' if sla_state.get('overdue') else ''}">
            <h2>SLA реакции</h2>
            <div>{sla_badge(ticket)}</div>
            <p class="muted">Лимит для приоритета «{e(app.PRIORITIES.get(str(ticket.get('priority','normal')), str(ticket.get('priority','normal'))))}»: {int(sla_state.get('limit_minutes',0) or 0)} мин.</p>
          </section>'''
    else:
        first_response = parse_iso_datetime(ticket.get("first_response_at"))
        created = parse_iso_datetime(ticket.get("created_at"))
        if first_response and created:
            reacted_minutes = max(0, int((first_response - created).total_seconds() // 60))
            sla_panel = f'<section class="panel sla-panel"><h2>SLA реакции</h2><strong>Реакция через {reacted_minutes} мин.</strong></section>'
        else:
            sla_panel = ""
    handoff_enabled = bool(int(ticket.get("shift_handoff", 0) or 0))
    if terminal_status:
        handoff_panel = ""
    elif handoff_enabled:
        handoff_panel = f"""
          <section class="panel handoff-panel">
            <h2>Передача смене</h2>
            <div class="notice warning-notice">Передано следующей смене: {e(ticket.get('handoff_from', ''))}<br><small>{e(human_time(str(ticket.get('handoff_at', ''))))}</small></div>
            <form method="post" action="/handoff">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <button class="button primary" name="action" value="accept" type="submit">Принять со смены</button>
            </form>
          </section>"""
    else:
        handoff_panel = f"""
          <section class="panel handoff-panel">
            <h2>Передача смене</h2>
            <p class="muted">Если не успеваете закончить заявку, отметьте её для следующей смены.</p>
            <form method="post" action="/handoff">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <button class="button" name="action" value="transfer" type="submit">Передать следующей смене</button>
            </form>
          </section>"""

    handoff_badge = '<span class="handoff-badge large">Следующая смена</span>' if handoff_enabled else ""
    admin_delete_panel = ""
    if show_admin:
        admin_delete_panel = f"""
          <section class="panel danger-panel">
            <h2>Администратор</h2>
            <p class="muted">Удаление происходит только внутри системы. Пользователю ничего не отправляется.</p>
            <form method="post" action="/admin/ticket-delete" onsubmit="return confirm('Удалить заявку №{ticket['id']}? Пользователь не получит уведомление.')">
              <input type="hidden" name="csrf_token" value="{e(app.csrf_token())}">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <button class="button danger" type="submit">Удалить заявку</button>
            </form>
          </section>"""
    content = f"""
      <a class="back" href="/">← Все заявки</a>
      {notice_html}
      <section class="ticket-header">
        <div><p class="eyebrow">Заявка #{ticket['id']}</p><h1>{e(ticket['title'])}</h1><p>{e(ticket['summary'])}</p></div>
        <div class="ticket-badges">{priority_badge(ticket['priority'])}{status_badge(ticket['status'])}{handoff_badge}</div>
      </section>
      <div class="ticket-layout">
        <section class="panel ticket-main">
          <div class="ticket-main-heading"><h2>Данные запроса</h2>{chat_button}</div>
          
          <div class="detail-grid">
            <div><span>Источник</span><strong>{e(ticket['source'])}</strong></div>
            <div><span>Отправитель</span><strong>{e(ticket['sender'])}</strong></div>
            <div><span>Телефон</span><strong>{e(display_phone(ticket['phone']))}</strong></div>
            <div><span>Категория</span><strong>{e(app.CATEGORIES.get(ticket['category'], ticket['category']))}</strong></div>
            <div><span>Приоритет</span><strong>{e(app.PRIORITIES.get(ticket['priority'], ticket['priority']))}</strong></div>
            <div><span>Создана</span><strong>{e(human_time(ticket['created_at']))}</strong></div>
          </div>
          
          <h3>Исходное сообщение</h3>
          <pre class="message">{e(ticket['original_text'] or ticket['summary'])}</pre>
          {attachment}
          {context_fields_panel}
          {context_history_panel}
        </section>
        <aside>
          <section class="panel actions-panel">
            <h2>Изменить статус</h2>
            <form method="post" action="/status">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <label>Сотрудник<select name="employee">{employee_options(ticket['assigned_to'])}</select></label>
              <div class="status-actions">{status_buttons}</div>
            </form>
          </section>
          {sla_panel}
          {handoff_panel}
          <section class="panel classification-panel">
            <h2>Приоритет</h2>
            <form method="post" action="/classification">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <label>Приоритет<select name="priority">{priority_options(ticket['priority'])}</select></label>
              <button class="button" type="submit">Сохранить</button>
            </form>
          </section>
          {admin_delete_panel}
          <section class="panel history-panel"><h2>История заявки</h2><ul>{event_rows}</ul></section>
        </aside>
      </div>
    """
    revision_field = f'<input type="hidden" name="revision" value="{int(ticket.get("revision",0))}"><input type="hidden" name="csrf_token" value="{e(app.csrf_token())}">'
    content = content.replace(f'<input type="hidden" name="ticket_id" value="{ticket_id}">', f'<input type="hidden" name="ticket_id" value="{ticket_id}">'+revision_field)
    content = content.replace('<button class="button" name="action" value="transfer"', '<label>Что сделано и что осталось<textarea name="handoff_note" maxlength="2000" required></textarea></label><button class="button" name="action" value="transfer"')
    content = content.replace('<h2>Передача смене</h2>', '<h2>Передача смене</h2>'+queue_operations.handoff_history(app.STORE,ticket_id))
    content = content.replace('<aside>', '<aside>'+queue_operations.notification_panel(app.STORE,ticket_id),1)
    return layout(f"Заявка #{ticket_id}", content, show_admin=show_admin)


def render_my_page(query: dict[str, list[str]], show_admin: bool = False) -> str:
    user = queue_auth.current_user() or {}
    user_id = int(user.get("id", 0) or 0)
    username = str(user.get("username") or "")
    display_name = str(user.get("display_name") or username or "Пользователь")
    role = str(user.get("role") or "employee")
    role_label = "Администратор" if role == "admin" else "Сотрудник"
    employee_name = employee_identity(user) if role == "employee" else display_name
    actor = work_actor()
    try:
        mine = app.STORE.list_tickets("", "", "", "", 400, 0, "mine", actor)
    except Exception:
        log_suppressed("queue_ticket_views.render_my_page")
        mine = []
    active = [item for item in mine if str(item.get("status", "")) not in {"done", "invalid"}]
    done = [item for item in mine if str(item.get("status", "")) == "done"]

    def ticket_cards(items: list[dict[str, object]], empty: str) -> str:
        cards: list[str] = []
        for item in items[:20]:
            ticket_id = int(item.get("id", 0) or 0)
            title = str(item.get("title") or f"Заявка #{ticket_id}")
            summary = str(item.get("summary") or "")
            status = str(item.get("status") or "new")
            created = human_time(str(item.get("created_at") or ""))
            cards.append(
                f'<a class="my-ticket-card" href="/ticket?id={ticket_id}">'
                f'<span><strong>#{ticket_id} · {e(title)}</strong><small>{e(summary[:140])}</small></span>'
                f'<span class="my-ticket-meta">{status_badge(status)}<small>{e(created)}</small></span></a>'
            )
        return "".join(cards) or f'<p class="empty">{e(empty)}</p>'

    login_rows: list[str] = []
    reasons = {
        "ok": "Успешный вход", "bad_password": "Неверный пароль",
        "lockout": "Блокировка после попыток", "locked": "Попытка во время блокировки",
        "disabled": "Отключённая учётная запись", "unknown_user": "Неизвестный логин",
    }
    for event in app.AUTH.list_user_login_events(user_id, 12):
        ok = bool(event.get("success"))
        state_class = "is-on" if ok else "is-off"
        state_label = "Успешно" if ok else "Ошибка"
        login_rows.append(
            f'<tr><td>{e(human_time(str(event.get("created_at") or "")))}</td>'
            f'<td><span class="auth-state {state_class}">{state_label}</span></td>'
            f'<td>{e(reasons.get(str(event.get("reason") or ""), str(event.get("reason") or "")))}</td>'
            f'<td>{e(str(event.get("client_ip") or "—"))}</td></tr>'
        )
    login_table = "".join(login_rows) or '<tr><td colspan="4" class="empty">История входов пока пуста</td></tr>'

    activity_rows: list[str] = []
    for event in app.AUTH.list_user_audit_events(user_id, 16):
        object_label = " · ".join(
            part for part in [str(event.get("object_type") or ""), str(event.get("object_id") or "")] if part
        ) or "—"
        activity_rows.append(
            f'<tr><td>{e(human_time(str(event.get("created_at") or "")))}</td>'
            f'<td><strong>{e(str(event.get("action") or ""))}</strong><small>{e(str(event.get("details") or ""))}</small></td>'
            f'<td>{e(object_label)}</td></tr>'
        )
    activity_table = "".join(activity_rows) or '<tr><td colspan="3" class="empty">Действий аккаунта пока нет</td></tr>'
    session_count = app.AUTH.active_session_count_for_user(user_id)
    shift_text = "Не используется для администратора" if role == "admin" else f"Автоматически на смене: {employee_name or '—'}"

    content = f"""
      <section class="page-heading my-account-heading"><div><p class="eyebrow">Учётная запись</p><h1>{e(display_name)}</h1><p>@{e(username)} · {e(role_label)}</p></div></section>
      <section class="my-account-grid">
        <div class="panel my-account-summary-card"><span>Роль</span><strong>{e(role_label)}</strong><small>{e(shift_text)}</small></div>
        <div class="panel my-account-summary-card"><span>Активные заявки</span><strong>{len(active)}</strong><small>Назначены на вас сейчас</small></div>
        <div class="panel my-account-summary-card"><span>Выполнено</span><strong>{len(done)}</strong><small>Заявки со статусом «Сделано»</small></div>
        <div class="panel my-account-summary-card"><span>Активные сессии</span><strong>{session_count}</strong><small>Ваши текущие входы в систему</small></div>
      </section>
      <section class="panel my-account-panel"><div class="section-heading"><div><h2>Текущие заявки</h2><p>Заявки, которые сейчас закреплены за вашим аккаунтом.</p></div></div><div class="my-ticket-list">{ticket_cards(active, "Активных заявок нет")}</div></section>
      <section class="panel my-account-panel"><div class="section-heading"><div><h2>Последние выполненные</h2><p>Последние заявки, завершённые от вашего имени.</p></div></div><div class="my-ticket-list">{ticket_cards(done, "Выполненных заявок пока нет")}</div></section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>Мои входы</h2><p>Последние успешные и неуспешные попытки авторизации.</p></div></div><div class="table-scroll"><table><thead><tr><th>Время</th><th>Результат</th><th>Причина</th><th>IP</th></tr></thead><tbody>{login_table}</tbody></table></div></section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>Моя активность</h2><p>Последние действия, которые система связала с вашим аккаунтом.</p></div></div><div class="table-scroll"><table><thead><tr><th>Время</th><th>Действие</th><th>Объект</th></tr></thead><tbody>{activity_table}</tbody></table></div></section>
    """
    return layout("Мой аккаунт", content, "", show_admin)





def render_account_switcher(
    query: dict[str, list[str]],
    show_admin: bool,
    sessions: list[dict[str, object]],
    active_slot: int,
) -> str:
    notice = str(query.get("notice", [""])[0] or "").strip()
    notice_html = f'<div class="panel"><strong>{e(notice)}</strong></div>' if notice else ""
    cards: list[str] = []
    for item in sessions:
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        slot = int(item.get("slot", 0) or 0)
        is_active = bool(item.get("active")) or slot == active_slot
        display_name = str(user.get("display_name") or user.get("username") or "Пользователь")
        username = str(user.get("username") or "")
        role = "Администратор" if str(user.get("role") or "") == "admin" else "Сотрудник"
        if is_active:
            action = '<span class="status-badge done">Текущий аккаунт</span>'
        else:
            action = f'''<form method="post" action="/accounts/switch">
          <input type="hidden" name="csrf_token" value="{e(app.csrf_token())}">
          <input type="hidden" name="slot" value="{slot}">
          <button class="button primary" type="submit">Переключиться</button>
        </form>'''
        cards.append(f'''<article class="panel my-account-summary-card">
          <span>{e(role)}</span>
          <strong>{e(display_name)}</strong>
          <small>@{e(username)}</small>
          <div>{action}</div>
        </article>''')
    if not cards:
        cards.append('<div class="panel empty">Сохранённых аккаунтов нет</div>')
    content = f'''
      <section class="page-heading"><div><p class="eyebrow">Общий компьютер</p><h1>Аккаунты на этом устройстве</h1><p>Можно сохранить несколько входов в одном браузере и переключаться между ними без выхода из остальных аккаунтов.</p></div>
        <div class="instruction-live-actions"><a class="button primary" href="/login?add=1&next=/accounts">Добавить существующий</a><a class="button ghost" href="/register">Создать новый аккаунт</a></div>
      </section>
      {notice_html}
      <section class="my-account-grid">{''.join(cards)}</section>
      <section class="panel"><div class="section-heading"><div><h2>Как это работает</h2><p>Активным остаётся один аккаунт. При переключении заявки, сообщения и действия будут записываться от имени выбранного пользователя.</p></div></div></section>
    '''
    return layout("Аккаунты", content, "", show_admin)
