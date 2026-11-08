from __future__ import annotations

import sqlite3
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from urllib.parse import quote

import queue_auth
import queue_contextual_tickets
import queue_operations
import queue_reliability
from queue_auth_views import script_tags, stylesheet_tags
from queue_core import LOCAL_TIMEZONE, display_phone, e, human_time, log_suppressed, normalize_phone, parse_iso_datetime
from queue_work_context import active_employee, category_options, employee_identity, employee_options, priority_badge, priority_options, query_page, render_pagination, render_ticket_rows, sla_badge, status_badge, ticket_sla_state, work_actor
from ticketing import PRIORITIES, STATUSES, TicketStore
from queue_templates import render_template

# Load order is the CSS cascade order: later files override earlier ones.
MAIN_STYLESHEETS = (
    "style.css", "ui.css", "instruction.css", "global-theme.css", "productivity.css", "workflow.css",
    "notifications.css", "chat.css", "auth.css", "pink-theme.css", "polish.css", "user-ui.css",
)
MAIN_SCRIPTS = (
    "http.js", "ui.js", "app.js", "productivity.js", "workflow.js", "reliability.js",
    "chat-layout.js", "performance.js", "attachments.js", "account.js",
)
# Scripts of the static pages in frontend/pages.
PAGE_SCRIPTS = ("shell.js", "groups-page.js")


class TicketViews:
    """The page frame, the ticket list, a ticket, the account pages."""

    def __init__(self, store: TicketStore, auth: queue_auth.AuthStore, *, csrf_token: Callable[[], str],
                 categories: Mapping[str, str], local_mode: bool, page_size: int):
        self.store = store
        self.auth = auth
        self.csrf_token = csrf_token
        self.categories = categories
        self.local_mode = local_mode
        self.page_size = page_size

    @staticmethod
    def _nav_items(show_admin: bool) -> list[tuple[str, str, str, str]]:
        items = [
            ("tickets", "/", "Заявки", ""),
            ("whatsapp", "/whatsapp", "WhatsApp", ""),
            ("groups", "/groups", "Группы", ""),
        ]
        if show_admin:
            items.append(("admin", "/admin/", "Админка", ""))
        items.append(("logout", "/logout", "Выйти", ""))
        return items

    def _profile(self) -> dict[str, object]:
        user = queue_auth.current_user() or {}
        user_id = int(user.get("id", 0) or 0)
        is_admin = str(user.get("role")) == "admin"
        theme = str((self.auth.get_user_preferences(user_id) if user_id > 0 else {}).get("theme") or "").strip()
        return {
            "id": user_id,
            "name": str(user.get("display_name") or user.get("username") or "Администратор"),
            "username": str(user.get("username") or ""),
            "role_label": "Администратор" if is_admin else "Сотрудник",
            "shift_note": "Работает от имени аккаунта" if is_admin
            else f"На смене автоматически: {employee_identity(user) or active_employee()}",
            "theme": theme if theme in {"light", "dark", "pink"} else "",
            "can_change_password": not user.get("legacy"),
        }

    def page_context(self, show_admin: bool) -> dict[str, object]:
        """What a static page needs to draw its frame: see frontend/static/shell.js."""
        profile = self._profile()
        return {
            "csrf_token": self.csrf_token(),
            "show_admin": show_admin,
            "local_mode": self.local_mode,
            "maintenance": queue_reliability.maintenance_active(self.store),
            "theme": profile.pop("theme"),
            "nav": [{"key": key, "href": href, "label": label} for key, href, label, _ in self._nav_items(show_admin)],
            "user": profile,
        }

    def layout(
        self,
        title: str,
        content: str,
        active: str = "tickets",
        show_admin: bool = False,
    ) -> str:
        nav_items = self._nav_items(show_admin)
        profile = self._profile()
        nav = "".join(
            f'<a class="nav-link {"active" if key == active else ""}" href="{e(href)}"{attributes}>{e(label)}</a>'
            for key, href, label, attributes in nav_items
        )
        legacy_note = not profile["can_change_password"]
        password_form = "" if legacy_note else render_template(
            "tickets/profile_password_form.html",
            csrf_token=e(self.csrf_token()),
        )
        auth_profile = render_template(
            "tickets/profile_menu.html",
            auth_name=e(profile["name"]),
            auth_role=e(profile["role_label"]),
            username=e(profile["username"]),
            shift_note=e(profile["shift_note"]),
            password_form=password_form,
        )
        return render_template(
            "tickets/layout.html",
            csrf_token=e(self.csrf_token()),
            title=e(title),
            dumps=json.dumps(profile["theme"]),
            stylesheet_tags=stylesheet_tags(MAIN_STYLESHEETS),
            show_admin_text='1' if show_admin else '0',
            user_id=profile["id"],
            local_mode_text='<span class="local-mode-badge">WINDOWS · ТЕСТ</span>' if self.local_mode else '',
            nav=nav,
            auth_profile=auth_profile,
            maintenance_active_text='<div class="maintenance-banner">Режим обслуживания включён: просмотр доступен, изменения сотрудников временно заблокированы.</div>' if queue_reliability.maintenance_active(self.store) else '',
            content=content,
            script_tags=script_tags(MAIN_SCRIPTS),
        )

    def render_dashboard(self, query: dict[str, list[str]], show_admin: bool = False) -> str:
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
            all_filtered = self.store.list_tickets(
                selected_status, selected_category, selected_priority, search, 5000, 0, selected_view, current_employee,
            )
            all_filtered = [item for item in all_filtered if ticket_sla_state(item).get("active") and int(ticket_sla_state(item).get("elapsed_seconds",0) or 0) >= no_answer_minutes * 60]
            total = len(all_filtered)
            pages = max(1, (total + self.page_size - 1) // self.page_size)
            page = min(query_page(query), pages)
            tickets = all_filtered[(page - 1) * self.page_size:page * self.page_size]
        else:
            total = self.store.count_tickets(
                selected_status, selected_category, selected_priority, search, selected_view, current_employee,
            )
            pages = max(1, (total + self.page_size - 1) // self.page_size)
            page = min(query_page(query), pages)
            tickets = self.store.list_tickets(
                selected_status,
                selected_category,
                selected_priority,
                search,
                self.page_size,
                (page - 1) * self.page_size,
                selected_view,
                current_employee,
            )
        counts = self.store.counts()
        shift_summary = self.store.shift_summary(current_employee)
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
            for key, label in STATUSES.items()
        )
        table_body = render_ticket_rows(tickets)
        handoff_tickets = [item for item in self.store.list_tickets("", "", "", "", 500, 0) if int(item.get("shift_handoff", 0) or 0)][:20]
        if handoff_tickets:
            handoff_items = "".join(
                f'<a class="handoff-item" href="/ticket?id={int(item["id"])}"><strong>#{int(item["id"])} · {e(str(item.get("title", "Заявка")))}</strong><span>{e(str(item.get("sender", "")))}</span><small>Передал: {e(str(item.get("handoff_from", "")))} · {e(human_time(str(item.get("handoff_at", ""))))}</small></a>'
                for item in handoff_tickets
            )
            handoff_board = f'<section class="panel handoff-board"><div class="section-heading"><div><p class="eyebrow">Передача смены</p><h2>Переданные заявки <span class="handoff-badge large">{len(handoff_tickets)}</span></h2><p>Отдельный список заявок, которые ждут следующую смену</p></div></div><div class="handoff-list">{handoff_items}</div></section>'
        else:
            handoff_board = ""
        content = render_template(
            "tickets/dashboard.html",
            notice_html=notice_html,
            cards=cards,
            current_employee=e(current_employee),
            shift_note=shift_note,
            shift_cards=shift_cards,
            handoff_board=handoff_board,
            selected_view=e(selected_view),
            search=e(search),
            status_options=status_options,
            category_options=category_options(selected_category, True),
            priority_options=priority_options(selected_priority, True),
            no_answer_minutes=no_answer_minutes or '',
            dashboard_version=e(self.store.dashboard_version()),
            table_body=table_body,
            render_pagination=render_pagination(query, total, page),
            csrf_token=e(self.csrf_token()),
            priority_options_2=priority_options('normal'),
            employee_options=employee_options(),
        )
        return self.layout("Заявки", content, show_admin=show_admin)

    def render_ticket(

        self,
        ticket_id: int,
        query: dict[str, list[str]],
        show_admin: bool = False,
    ) -> str:
        ticket = self.store.get_ticket(ticket_id)
        if not ticket:
            return self.layout(
                "Не найдено",
                '<section class="panel empty">Заявка не найдена</section>',
                show_admin=show_admin,
            )
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        events = self.store.ticket_events(ticket_id)
        event_rows = "".join(
            f'<li><span>{e(event["action"])}</span><small>{e(event["actor"])} · {e(human_time(event["created_at"]))}</small></li>'
            for event in events
        )
        context_fields = queue_contextual_tickets.ticket_fields(self.store, ticket_id)
        context_history = queue_contextual_tickets.ticket_history(self.store, ticket_id, 120)
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
                    render_template(
                        "tickets/context_field_form.html",
                        ticket_id=ticket_id,
                        id=int(item.get("id",0) or 0),
                        field_option_html=field_option_html,
                    )
                )
            if str(item.get("message_key", "")) and str(item.get("chat_id", "")):
                actions += (
                    render_template(
                        "tickets/context_attach_form.html",
                        ticket_id=ticket_id,
                        id=int(item.get("id",0) or 0),
                    )
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
        media_items = self.store.list_ticket_whatsapp_media(ticket_id, chat_id, external_id, 24)
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
                    when = datetime.fromtimestamp(int(media_item.get("message_timestamp", 0) or 0), tz=timezone.utc).astimezone(LOCAL_TIMEZONE).strftime("%d.%m %H:%M")
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
                for key, label in STATUSES.items()
            )

        chat_button = (
            f'<a class="button whatsapp-button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат WhatsApp</a>'
            if chat_id
            else '<span class="button disabled" title="WhatsApp не передал телефонный номер">Номер для связи не определён</span>'
        )

        sla_state = ticket_sla_state(ticket)
        if sla_state.get("active"):
            sla_panel = render_template(
                "tickets/sla_panel.html",
                overdue_text='sla-panel-overdue' if sla_state.get('overdue') else '',
                sla_badge=sla_badge(ticket),
                get=e(PRIORITIES.get(str(ticket.get('priority','normal')), str(ticket.get('priority','normal')))),
                limit_minutes=int(sla_state.get('limit_minutes',0) or 0),
            )
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
            handoff_panel = render_template(
                "tickets/handoff_pending_panel.html",
                handoff_from=e(ticket.get('handoff_from', '')),
                human_time=e(human_time(str(ticket.get('handoff_at', '')))),
                id=ticket['id'],
            )
        else:
            handoff_panel = render_template(
                "tickets/handoff_form_panel.html",
                id=ticket['id'],
            )

        handoff_badge = '<span class="handoff-badge large">Следующая смена</span>' if handoff_enabled else ""
        admin_delete_panel = ""
        if show_admin:
            admin_delete_panel = render_template(
                "tickets/admin_delete_panel.html",
                id=ticket['id'],
                csrf_token=e(self.csrf_token()),
            )
        content = render_template(
            "tickets/ticket.html",
            notice_html=notice_html,
            id=ticket['id'],
            title=e(ticket['title']),
            summary=e(ticket['summary']),
            priority_badge=priority_badge(ticket['priority']),
            status_badge=status_badge(ticket['status']),
            handoff_badge=handoff_badge,
            chat_button=chat_button,
            source=e(ticket['source']),
            sender=e(ticket['sender']),
            display_phone=e(display_phone(ticket['phone'])),
            get=e(self.categories.get(ticket['category'], ticket['category'])),
            get_2=e(PRIORITIES.get(ticket['priority'], ticket['priority'])),
            human_time=e(human_time(ticket['created_at'])),
            original_text=e(ticket['original_text'] or ticket['summary']),
            attachment=attachment,
            context_fields_panel=context_fields_panel,
            context_history_panel=context_history_panel,
            employee_options=employee_options(ticket['assigned_to']),
            status_buttons=status_buttons,
            sla_panel=sla_panel,
            handoff_panel=handoff_panel,
            priority_options=priority_options(ticket['priority']),
            admin_delete_panel=admin_delete_panel,
            event_rows=event_rows,
        )
        revision_field = f'<input type="hidden" name="revision" value="{int(ticket.get("revision",0))}"><input type="hidden" name="csrf_token" value="{e(self.csrf_token())}">'
        content = content.replace(f'<input type="hidden" name="ticket_id" value="{ticket_id}">', f'<input type="hidden" name="ticket_id" value="{ticket_id}">'+revision_field)
        content = content.replace('<button class="button" name="action" value="transfer"', '<label>Что сделано и что осталось<textarea name="handoff_note" maxlength="2000" required></textarea></label><button class="button" name="action" value="transfer"')
        content = content.replace('<h2>Передача смене</h2>', '<h2>Передача смене</h2>'+queue_operations.handoff_history(self.store,ticket_id))
        content = content.replace('<aside>', '<aside>'+queue_operations.notification_panel(self.store,ticket_id),1)
        return self.layout(f"Заявка #{ticket_id}", content, show_admin=show_admin)

    def render_my_page(self, query: dict[str, list[str]], show_admin: bool = False) -> str:
        user = queue_auth.current_user() or {}
        user_id = int(user.get("id", 0) or 0)
        username = str(user.get("username") or "")
        display_name = str(user.get("display_name") or username or "Пользователь")
        role = str(user.get("role") or "employee")
        role_label = "Администратор" if role == "admin" else "Сотрудник"
        employee_name = employee_identity(user) if role == "employee" else display_name
        actor = work_actor()
        try:
            mine = self.store.list_tickets("", "", "", "", 400, 0, "mine", actor)
        except sqlite3.Error:
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
        for event in self.auth.list_user_login_events(user_id, 12):
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
        for event in self.auth.list_user_audit_events(user_id, 16):
            object_label = " · ".join(
                part for part in [str(event.get("object_type") or ""), str(event.get("object_id") or "")] if part
            ) or "—"
            activity_rows.append(
                f'<tr><td>{e(human_time(str(event.get("created_at") or "")))}</td>'
                f'<td><strong>{e(str(event.get("action") or ""))}</strong><small>{e(str(event.get("details") or ""))}</small></td>'
                f'<td>{e(object_label)}</td></tr>'
            )
        activity_table = "".join(activity_rows) or '<tr><td colspan="3" class="empty">Действий аккаунта пока нет</td></tr>'
        session_count = self.auth.active_session_count_for_user(user_id)
        shift_text = "Не используется для администратора" if role == "admin" else f"Автоматически на смене: {employee_name or '—'}"

        content = render_template(
            "tickets/my_page.html",
            display_name=e(display_name),
            username=e(username),
            role_label=e(role_label),
            shift_text=e(shift_text),
            active_count=len(active),
            done_count=len(done),
            session_count=session_count,
            ticket_cards=ticket_cards(active, "Активных заявок нет"),
            ticket_cards_2=ticket_cards(done, "Выполненных заявок пока нет"),
            login_table=login_table,
            activity_table=activity_table,
        )
        return self.layout("Мой аккаунт", content, "", show_admin)

    def render_account_switcher(

        self,
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
                action = render_template(
                    "tickets/account_switch_form.html",
                    csrf_token=e(self.csrf_token()),
                    slot=slot,
                )
            cards.append(render_template(
                "tickets/account_card.html",
                role=e(role),
                display_name=e(display_name),
                username=e(username),
                action=action,
            ))
        if not cards:
            cards.append('<div class="panel empty">Сохранённых аккаунтов нет</div>')
        content = render_template(
            "tickets/account_switcher.html",
            registration_enabled_text='<a class="button ghost" href="/register">Создать новый аккаунт</a>' if queue_auth.self_registration_enabled() else '',
            notice_html=notice_html,
            join=''.join(cards),
        )
        return self.layout("Аккаунты", content, "", show_admin)
