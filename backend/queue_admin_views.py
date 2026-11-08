"""Вкладки админки и данные страницы «Доступ»: пользователи, сессии, запросы на сброс пароля, история входов."""
from __future__ import annotations

from typing import Any, Callable, Iterable


ADMIN_TABS = [
    ("home", "/admin/", "Обзор"),
    ("users", "/admin/users", "Доступ"),
    ("employees", "/admin/employees", "Сотрудники"),
    ("shift", "/admin/shift", "Объявление о смене"),
    ("manual", "/admin/manual", "Ручная заявка"),
    ("menu", "/admin/menu", "Тексты WhatsApp"),
    ("categories", "/admin/categories", "Категории"),
    ("errors", "/admin/errors", "Ошибки"),
    ("contacts", "/admin/contacts", "Контакты WhatsApp"),
    ("audit", "/admin/audit", "Журнал"),
    ("analytics", "/admin/analytics", "Аналитика"),
    ("manager", "/admin/manager", "Dashboard"),
    ("api_keys", "/admin/api-keys", "API-ключи"),
    ("backups", "/admin/backups", "Бэкапы"),
    ("workflow", "/admin/workflow", "Хранение / процессы"),
    ("reliability", "/admin/reliability", "Надёжность"),
    ("system", "/admin/system", "Система"),
]


LOGIN_REASONS = {
    "ok": "Успешный вход",
    "bad_password": "Неверный пароль",
    "lockout": "Блокировка после попыток",
    "locked": "Попытка во время блокировки",
    "disabled": "Отключённая учётная запись",
    "unknown_user": "Неизвестный логин",
}


def admin_users_data(*, auth: Any, employees: Iterable[str], human_time: Callable[[str], str]) -> dict[str, object]:
    users = []
    for user in auth.list_users():
        user_id = int(user["id"])
        role = str(user["role"])
        enabled = bool(user["enabled"])
        state = "Активен" if enabled else "Отключён"
        if user.get("must_change_password") and enabled:
            state += " · временный пароль"
        theme = str(auth.get_user_preferences(user_id).get("theme") or "dark").strip().lower()
        users.append({
            "user_id": user_id,
            "display_name": str(user["display_name"]),
            "username": str(user["username"]),
            "linked": str(user.get("employee_name") or ""),
            "role": role,
            "role_label": "Администратор" if role == "admin" else "Сотрудник",
            "enabled_text": "is-on" if enabled else "is-off",
            "enabled_text_2": "0" if enabled else "1",
            "enabled_text_3": "Отключить" if enabled else "Включить",
            "state": state,
            "last_login": human_time(str(user.get("last_login_at") or "")) if user.get("last_login_at") else "Никогда",
            "next_role": "employee" if role == "admin" else "admin",
            "next_role_label": "Сделать сотрудником" if role == "admin" else "Сделать администратором",
            "theme": theme if theme in {"light", "dark", "pink"} else "dark",
        })
    return {
        "title": "Доступ", "active": "users",
        "employees": list(employees),
        "requests": [{
            "user_id": int(request["user_id"]),
            "display_name": str(request.get("display_name") or request.get("username") or ""),
            "username": str(request.get("username") or ""),
            "requested_at": human_time(str(request.get("requested_at") or "")) if request.get("requested_at") else "",
        } for request in auth.list_password_reset_requests()],
        "users": users,
        "sessions": [{
            "display_name": str(session.get("display_name") or session.get("username") or ""),
            "username": str(session.get("username") or ""),
            "client_ip": str(session.get("client_ip") or "—"),
            "last_seen_at": human_time(str(session.get("last_seen_at") or "")),
            "user_agent": str(session.get("user_agent") or "")[:90] or "—",
            "session_hash": str(session.get("token_hash") or ""),
        } for session in auth.list_active_sessions(100)],
        "logins": [{
            "created_at": human_time(str(event.get("created_at") or "")),
            "username": str(event.get("username") or ""),
            "success": bool(event.get("success")),
            "reason": LOGIN_REASONS.get(str(event.get("reason") or ""), str(event.get("reason") or "")),
            "client_ip": str(event.get("client_ip") or "—"),
        } for event in auth.list_login_events(120)],
    }
