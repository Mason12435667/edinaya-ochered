"""Данные рамки страницы (меню, права, CSRF-токен) и страниц «Мой аккаунт» и «Аккаунты»."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

import queue_auth
import queue_reliability
from queue_core import human_time, log_suppressed
from queue_work_context import active_employee, employee_identity, work_actor
from ticketing import STATUSES, TicketStore

# Load order is the CSS cascade order: later files override earlier ones.
MAIN_STYLESHEETS = (
    "style.css", "ui.css", "instruction.css", "global-theme.css", "productivity.css", "workflow.css",
    "notifications.css", "chat.css", "auth.css", "pink-theme.css", "polish.css", "user-ui.css",
)
MAIN_SCRIPTS = (
    "http.js", "ui.js", "app.js", "productivity.js", "workflow.js", "reliability.js",
    "chat-layout.js", "performance.js", "attachments.js", "account.js",
)
# app.js is sent as these files of frontend/static/app joined in this order. One request instead of
# six: the scripts listed after app.js must be listening before its first chat data arrives.
APP_SCRIPT_PARTS = (
    "voice-player.js", "common.js", "media-lightbox.js", "conversation.js", "notification-center.js", "main.js",
)
# Scripts of the static pages in frontend/pages.
PAGE_SCRIPTS = ("shell.js", "groups-page.js", "ticket-render.js", "dashboard-page.js", "ticket-page.js", "pages.js")


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

    def my_page_data(self) -> dict[str, object]:
        user = queue_auth.current_user() or {}
        user_id = int(user.get("id", 0) or 0)
        username = str(user.get("username") or "")
        display_name = str(user.get("display_name") or username or "Пользователь")
        role = str(user.get("role") or "employee")
        employee_name = employee_identity(user) if role == "employee" else display_name
        try:
            mine = self.store.list_tickets("", "", "", "", 400, 0, "mine", work_actor())
        except sqlite3.Error:
            log_suppressed("queue_ticket_views.my_page_data")
            mine = []
        active = [item for item in mine if str(item.get("status", "")) not in {"done", "invalid"}]
        done = [item for item in mine if str(item.get("status", "")) == "done"]

        def cards(items: list[dict[str, object]]) -> list[dict[str, object]]:
            return [{
                "id": int(item.get("id", 0) or 0),
                "title": str(item.get("title") or f"Заявка #{int(item.get('id', 0) or 0)}"),
                "summary": str(item.get("summary") or "")[:140],
                "status": str(item.get("status") or "new"),
                "status_label": STATUSES.get(str(item.get("status") or "new"), str(item.get("status") or "new")),
                "created_at": human_time(str(item.get("created_at") or "")),
            } for item in items[:20]]

        reasons = {
            "ok": "Успешный вход", "bad_password": "Неверный пароль",
            "lockout": "Блокировка после попыток", "locked": "Попытка во время блокировки",
            "disabled": "Отключённая учётная запись", "unknown_user": "Неизвестный логин",
        }
        return {
            "title": "Мой аккаунт",
            "display_name": display_name,
            "username": username,
            "role_label": "Администратор" if role == "admin" else "Сотрудник",
            "shift_text": "Не используется для администратора" if role == "admin" else f"Автоматически на смене: {employee_name or '—'}",
            "active_count": len(active),
            "done_count": len(done),
            "session_count": self.auth.active_session_count_for_user(user_id),
            "active": cards(active),
            "done": cards(done),
            "logins": [{
                "created_at": human_time(str(event.get("created_at") or "")),
                "success": bool(event.get("success")),
                "reason": reasons.get(str(event.get("reason") or ""), str(event.get("reason") or "")),
                "client_ip": str(event.get("client_ip") or "—"),
            } for event in self.auth.list_user_login_events(user_id, 12)],
            "activity": [{
                "created_at": human_time(str(event.get("created_at") or "")),
                "action": str(event.get("action") or ""),
                "details": str(event.get("details") or ""),
                "object": " · ".join(part for part in [str(event.get("object_type") or ""), str(event.get("object_id") or "")] if part) or "—",
            } for event in self.auth.list_user_audit_events(user_id, 16)],
        }

    def accounts_data(self, sessions: list[dict[str, object]], active_slot: int) -> dict[str, object]:
        accounts = []
        for item in sessions:
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            slot = int(item.get("slot", 0) or 0)
            accounts.append({
                "slot": slot,
                "active": bool(item.get("active")) or slot == active_slot,
                "display_name": str(user.get("display_name") or user.get("username") or "Пользователь"),
                "username": str(user.get("username") or ""),
                "role": "Администратор" if str(user.get("role") or "") == "admin" else "Сотрудник",
            })
        return {"title": "Аккаунты", "accounts": accounts, "registration_enabled": queue_auth.self_registration_enabled()}
