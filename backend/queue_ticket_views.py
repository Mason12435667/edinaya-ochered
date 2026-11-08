from __future__ import annotations

import sqlite3
import json
from collections.abc import Callable, Mapping

import queue_auth
import queue_reliability
from queue_auth_views import script_tags, stylesheet_tags
from queue_core import e, human_time, log_suppressed
from queue_work_context import active_employee, employee_identity, status_badge, work_actor
from ticketing import TicketStore
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
PAGE_SCRIPTS = ("shell.js", "groups-page.js", "ticket-render.js", "dashboard-page.js", "ticket-page.js", "admin-page.js")


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
