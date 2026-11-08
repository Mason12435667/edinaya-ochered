"""Data for the static administration pages; frontend/static/admin-page.js draws them."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from urllib.parse import quote

import queue_generic_profile
import queue_user_locale
import queue_workflow
from queue_auth import AuthStore
from queue_avatars import AvatarCache
from queue_bot_menu import BotMenu
from queue_bot_texts import language_selection_text_v3, v3_duplicate_reply, v3_missing_fields_reply
from queue_chat_state import flexible_contact_match
from queue_contact_profiles import ContactProfiles
from queue_core import display_phone, e, human_bytes, human_time, normalize_message, normalize_phone, valid_chat_id
from queue_menu_config import MenuConfig
from queue_reporting import ReportingService
from queue_runtime import RuntimeServices
from queue_ticket_service import TicketService
from queue_user_locale import tr
from queue_work_context import assignment_names, monitor_log_entries, system_status_snapshot, work_actor
from ticketing import PRIORITIES, STATUSES, TicketStore

ERROR_REPORT_STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "resolved": "Исправлена",
    "rejected": "Отклонена",
}
ERROR_REPORT_STATUS_CLASSES = {
    "new": "status-new",
    "in_progress": "status-in_progress",
    "resolved": "status-done",
    "rejected": "status-invalid",
}


def _pairs(mapping) -> list[list[str]]:
    return [[str(key), str(label)] for key, label in dict(mapping).items()]


def _first(query: dict[str, list[str]], name: str) -> str:
    return str(query.get(name, [""])[0] or "")


def avatar_initials(name: str) -> str:
    return ''.join(word[0] for word in name.split()[:2]) or '?'


def _category_row(suffix: str, row: dict[str, object], used_numbers: set[str]) -> dict[str, object]:
    number = str(row.get("number", ""))
    return {
        "suffix": suffix,
        "key": "" if suffix == "new" else str(row["key"]),
        "number": number,
        "numbers": [n for n in queue_generic_profile.MENU_NUMBERS if n == number or n not in used_numbers],
        "label_ru": str(row.get("label_ru", "")),
        "label_kz": str(row.get("label_kz", "")),
        "prompt_ru": str(row.get("prompt_ru", "")),
        "prompt_kz": str(row.get("prompt_kz", "")),
        "required": list(row.get("required_fields") or ["problem"]),
    }


@dataclass(frozen=True)
class AdminPageDependencies:
    store: TicketStore
    auth: AuthStore
    menu: BotMenu
    menu_config: MenuConfig
    contacts: ContactProfiles
    tickets: TicketService
    reporting: ReportingService
    runtime: RuntimeServices
    avatars: AvatarCache
    employees: list[str]  # app.py edits this list in place
    max_employees: int
    local_mode: bool
    chat_lock: Lock
    chat_state: dict[str, object]
    csrf_token: Callable[[], str]
    admin_layout: Callable[[str, str, str], str]
    categories: dict[str, str]


class AdminPages:
    def __init__(self, deps: AdminPageDependencies):
        self.deps = deps

    def page_data(self, page: str, query: dict[str, list[str]]) -> dict[str, object] | None:
        builder = {
            "home": lambda: self.home_data(),
            "employees": lambda: self.employees_data(query),
            "manual": lambda: self.manual_data(),
            "menu": lambda: self.menu_data(),
            "categories": lambda: self.categories_data(),
            "errors": lambda: self.errors_data(query),
            "error": lambda: self.error_data(query),
            "contacts": lambda: self.contacts_data(query),
            "audit": lambda: self.audit_data(query),
            "manager": lambda: self.manager_data(query),
            "system": lambda: self.system_data(),
        }.get(page)
        return builder() if builder else None

    def home_data(self) -> dict[str, object]:
        error_counts = self.deps.store.error_report_counts()
        return {
            "title": "Админка", "active": "home",
            "users_count": len(self.deps.auth.list_users()),
            "employees_count": len(self.deps.employees),
            "categories_count": len(self.deps.menu.active_ticket_category_items()),
            "open_errors": int(error_counts.get("new", 0)) + int(error_counts.get("in_progress", 0)),
            "contacts": len(self.deps.store.list_manual_whatsapp_contacts()),
            "groups": len(self.deps.store.list_whatsapp_groups()),
            "local_mode_text": 'Локальный тестовый режим' if self.deps.local_mode else 'Авторизация приложения',
            "local_mode_text_2": 'Windows-копия использует отдельную тестовую базу и отдельные логины.' if self.deps.local_mode else 'Пользователи входят через /login. Существующий Nginx-администратор сохранён как аварийный путь для первичной настройки и восстановления доступа.',
        }

    @staticmethod
    def _error_badge(status: str) -> dict[str, str]:
        return {"class": ERROR_REPORT_STATUS_CLASSES.get(status, "status-new"), "label": ERROR_REPORT_STATUS_LABELS.get(status, status or "Новая")}

    def errors_data(self, query: dict[str, list[str]]) -> dict[str, object]:
        status, search = _first(query, "status"), _first(query, "q")
        rows = []
        for report in self.deps.store.list_error_reports(status, search, 300):
            attachment = str(report.get("attachment_name", "") or "")
            rows.append({
                "report_id": int(report.get("id", 0) or 0),
                "badge": self._error_badge(str(report.get("status", "new"))),
                "sender": str(report.get("sender", "") or "Неизвестный пользователь"),
                "phone": display_phone(str(report.get("phone", ""))) if report.get("phone") else "—",
                "description": normalize_message(str(report.get("description", "")))[:150] or "Без текста",
                "attachment_text": ("Вложение: " + attachment) if attachment else "",
                "human_time": human_time(str(report.get("created_at", ""))) or "—",
            })
        return {"title": "Ошибки", "active": "errors", "search": search, "status": status, "statuses": _pairs(ERROR_REPORT_STATUS_LABELS), "rows": rows}

    def error_data(self, query: dict[str, list[str]]) -> dict[str, object]:
        try:
            report_id = int(_first(query, "id") or 0)
        except ValueError:
            report_id = 0
        report = self.deps.store.get_error_report(report_id)
        if not report:
            return {"title": "Ошибка не найдена", "active": "errors", "found": False}
        chat_id = str(report.get("chat_id", "") or "")
        external_id = str(report.get("external_id", "") or "")
        message = self.deps.store.get_whatsapp_message(chat_id, external_id) if chat_id and external_id else None
        attachment = None
        if message and str(message.get("media_path", "") or ""):
            attachment = {
                "kind": "image" if str(message.get("media_mime", "") or "").startswith("image/") else "file",
                "url": f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(external_id)}",
                "name": str(message.get("media_name", "") or report.get("attachment_name", "") or "Вложение"),
            }
        elif report.get("attachment_name"):
            attachment = {"kind": "missing", "name": str(report.get("attachment_name", ""))}
        return {
            "title": f"Ошибка #{report_id}", "active": "errors", "found": True,
            "id": int(report.get("id", 0) or 0),
            "human_time": human_time(str(report.get("created_at", ""))),
            "chat_id": chat_id,
            "badge": self._error_badge(str(report.get("status", "new"))),
            "status": str(report.get("status", "new")),
            "statuses": _pairs(ERROR_REPORT_STATUS_LABELS),
            "sender": str(report.get("sender", "") or "Неизвестный"),
            "phone_text": display_phone(str(report.get("phone", ""))) if report.get("phone") else "—",
            "description": str(report.get("description", "") or "Без текста"),
            "attachment": attachment,
            "admin_note": str(report.get("admin_note", "") or ""),
        }

    def audit_data(self, query: dict[str, list[str]]) -> dict[str, object]:
        level, search = _first(query, "level"), _first(query, "q")
        names = {"info": "Инфо", "warning": "Внимание", "error": "Ошибка"}
        joined = lambda *parts: " · ".join(str(part) for part in parts if part)
        return {
            "title": "Журнал", "active": "audit",
            "search": search, "level": level, "levels": _pairs(names),
            "actor_filter": _first(query, "actor"), "action_filter": _first(query, "action"),
            "audit": [{
                "level": str(item.get("level", "info")),
                "level_name": names.get(str(item.get("level", "")), str(item.get("level", ""))),
                "created_at": human_time(str(item.get("created_at", ""))),
                "actor": str(item.get("actor", "") or "Система"),
                "action": str(item.get("action", "")),
                "details": str(item.get("details", "") or ""),
                "object": joined(item.get("object_type", ""), item.get("object_id", "")) or "—",
            } for item in self.deps.store.list_audit_entries(300, level, search)],
            "auth": [{
                "created_at": human_time(str(item.get("created_at") or "")),
                "actor": str(item.get("actor_display_name") or item.get("actor_username") or "Система"),
                "action": str(item.get("action") or ""),
                "details": str(item.get("details") or ""),
                "object": joined(item.get("object_type") or "", item.get("object_id") or "") or "—",
            } for item in self.deps.auth.list_audit_events(200)],
            "unified": [{
                "created_at": human_time(item["created_at"]),
                "actor": item["actor"],
                "action": item["action"][:220],
                "object": item["object"] or "—",
                "source": item["source"],
            } for item in self.deps.reporting.unified_audit_events(query, 400)],
            "monitor": [line for line in monitor_log_entries(100) if " ERROR " in f" {line} " or " WARN " in f" {line} "][:30],
        }

    def _analytics_filters(self, query: dict[str, list[str]], action: str) -> dict[str, object]:
        return {
            "action": action,
            "date_from": _first(query, "date_from"),
            "date_to": _first(query, "date_to"),
            "employee": _first(query, "employee"),
            "status": _first(query, "status"),
            "category": _first(query, "category"),
            "employees": sorted({str(item.get("assigned_to") or "") for item in self.deps.store.list_tickets("", "", "", "", 5000, 0) if str(item.get("assigned_to") or "")}, key=str.casefold),
            "statuses": _pairs(STATUSES),
            "categories": _pairs(self.deps.categories),
        }

    def manager_data(self, query: dict[str, list[str]]) -> dict[str, object]:
        data = self.deps.reporting.analytics_snapshot(query)
        employees = sorted(data["by_employee"].items(), key=lambda kv: (-kv[1]["total"], kv[0].casefold()))[:12]
        return {
            "title": "Dashboard руководителя", "active": "manager",
            "filters": self._analytics_filters(query, "/admin/manager"),
            "total": data["total"], "open": data["open"], "avg_first": data["avg_first"],
            "avg_resolution": data["avg_resolution"], "handoffs": data["handoffs"],
            "categories": [[name, count] for name, count in list(data["by_category"].items())[:8]],
            "employees": [{"name": name, "open": vals["open"], "done": vals["done"], "total": vals["total"]} for name, vals in employees],
        }

    def system_data(self) -> dict[str, object]:
        state = system_status_snapshot()
        resources = self.deps.runtime.process_resource_snapshot()
        backup_state = self.deps.runtime.scheduled_backup_snapshot()
        restore_check = backup_state.get('restore_check') or {}
        restore_label = ('Проверка восстановления: успешно · ' + str(restore_check.get('checked_at', '')) + ' · заявок: ' + str(restore_check.get('tickets', 0)) + ' · файлов: ' + str(restore_check.get('files', 0))) if restore_check.get('ok') else 'Проверка восстановления ещё не выполнялась'
        connector = state["connector"]
        database = state["database"]
        queue = state["queue"]
        counts = queue.get("counts", {}) if isinstance(queue, dict) else {}
        disk_total = int(state.get("disk_total", 0) or 0)
        disk_free = int(state.get("disk_free", 0) or 0)
        connector_ok = bool(connector.get("connected")) if isinstance(connector, dict) else False
        connector_status = str(connector.get("status", "offline")) if isinstance(connector, dict) else "offline"
        heartbeat_age = state.get("heartbeat_age")
        local_monitor = state.get("monitor_mode") == "local"
        heartbeat_text = (
            "не используется в локальном Windows-режиме"
            if local_monitor
            else ("нет данных" if heartbeat_age is None else ("только что" if int(heartbeat_age) < 60 else f"{int(heartbeat_age)//60} мин назад"))
        )
        status_labels = {"pending": "ожидает", "processing": "отправляется", "sent": "отправлено", "failed": "ошибка", "sending": "отправляется", "uncertain": "проверьте переписку"}
        return {
            "title": "Система", "active": "system",
            "cards": {
                "connector_ok_text": 'system-ok' if connector_ok else 'system-bad',
                "connector_ok_text_2": 'Подключён' if connector_ok else connector_status,
                "connector_updated_at": str(connector.get('updated_at', ''))[:19] if isinstance(connector, dict) else '',
                "ok_text": 'system-ok' if database.get('ok') else 'system-bad',
                "ok_text_2": 'OK' if database.get('ok') else 'Ошибка',
                "result": str(database.get('result', '')),
                "monitor_ok_text": 'system-ok' if state.get('monitor_ok') else 'system-warn',
                "monitor_label": "Локальный режим" if local_monitor else ("Активно" if state.get("monitor_ok") else "Нет свежего сигнала"),
                "local_monitor_text": 'На Windows watchdog отключён' if local_monitor else 'Проверка: ' + heartbeat_text,
            },
            "values": {
                "human_bytes": human_bytes(disk_free),
                "human_bytes_2": human_bytes(disk_total),
                "disk_percent": int(round((disk_free / disk_total) * 100)) if disk_total else 0,
                "human_bytes_3": human_bytes(int(state.get('db_size', 0) or 0)),
                "human_bytes_4": human_bytes(int(state.get('media_size', 0) or 0)),
                "human_bytes_5": human_bytes(int(resources.get('rss_bytes', 0) or 0)),
                "cpu_count": int(resources.get('cpu_count', 1) or 1),
                "load_text": 'Load: ' + str(resources.get('load')) if resources.get('load') else 'CPU time: ' + str(resources.get('cpu_time')) + ' с',
                "restore_label": restore_label,
                "last_file": str(backup_state.get('last_file') or 'ещё нет'),
                "count": int(backup_state.get('count', 0) or 0),
                "last_error_text": (' · ошибка: ' + str(backup_state.get('last_error'))) if backup_state.get('last_error') else '',
                "hours": int(backup_state.get('hours', 24) or 24),
                "keep": int(backup_state.get('keep', 14) or 14),
                "pending": int(counts.get('pending', 0) or 0),
                "sending_count": int(counts.get('processing', 0) or 0) + int(counts.get('sending', 0) or 0),
                "sent": int(counts.get('sent', 0) or 0),
                "uncertain": int(counts.get('uncertain', 0) or 0),
                "failed": int(counts.get('failed', 0) or 0),
            },
            "queue": [{
                "id": int(row.get('id', 0) or 0),
                "status": status_labels.get(str(row.get("status", "")), str(row.get("status", ""))),
                "attempts": f"{int(row.get('attempt_count', 0) or 0)}/{int(row.get('max_attempts', 4) or 4)}",
                "target": str(row.get("chat_id") or row.get("phone") or "")[:34],
                "error": str(row.get("last_error") or "")[:120] or "—",
                "updated_at": human_time(str(row.get('updated_at') or row.get('created_at') or '')),
            } for row in (queue.get("rows", []) if isinstance(queue, dict) else [])],
            "logs": list(state.get("logs", [])),
        }

    def admin_employee_count(self, query: dict[str, list[str]]) -> int:
        try:
            requested = int(query.get("count", [str(len(self.deps.employees))])[0])
        except ValueError:
            requested = len(self.deps.employees)
        return max(1, min(self.deps.max_employees, requested))

    def employees_data(self, query: dict[str, list[str]]) -> dict[str, object]:
        count = self.admin_employee_count(query)
        names = list(self.deps.employees[:count])
        while len(names) < count:
            names.append(f"Сотрудник {len(names) + 1}")
        return {"title": "Сотрудники", "active": "employees", "employees_count": len(self.deps.employees), "count": count,
                "max_employees": self.deps.max_employees, "names": names}

    def manual_data(self) -> dict[str, object]:
        names = assignment_names()
        return {"title": "Ручная заявка", "active": "manual",
                "categories": _pairs(self.deps.menu.active_ticket_category_items()),
                "priorities": _pairs(PRIORITIES),
                "employees": names, "employee": work_actor()}

    def _admin_live_flow_snapshot(self, language: str) -> dict[str, object]:
        previous_language = queue_user_locale.current_language()
        queue_user_locale.set_language(language)
        try:
            categories = []
            for row in sorted(self.deps.menu_config.rows(), key=lambda row: int(row["number"])):
                category = str(row["key"])
                categories.append([f"{row['number']}. {self.deps.menu.user_menu_label(category)}", self.deps.menu.configured_category_prompt(category)])
            sample_ticket = {
                "id": 12345,
                "category": "general",
                "status": "in_progress",
                "summary": tr("Пример активной заявки", "Белсенді өтінім мысалы"),
                "original_text": "",
            }
            close_reason = next(iter(queue_workflow.ALLOWED_CLOSE_REASONS.keys()), "")
            return {
                "profile": self.deps.contacts.profile_prompt_text(),
                "menu": self.deps.menu.main_menu_text(),
                "categories": categories,
                "active": self.deps.tickets.active_tickets_reply([sample_ticket]),
                "active_empty": self.deps.tickets.active_tickets_reply([]),
                "accepted": self.deps.tickets.ticket_accepted_reply(12345),
                "missing": v3_missing_fields_reply(["reference", "problem"]),
                "duplicate": v3_duplicate_reply(12345),
                "done": self.deps.tickets.status_reply_text(
                    "done", 12345,
                    close_comment=tr("Комментарий специалиста", "Маманның пікірі"),
                ),
                "invalid": self.deps.tickets.status_reply_text(
                    "invalid", 12345,
                    close_reason=close_reason,
                    close_comment=tr("Комментарий специалиста", "Маманның пікірі"),
                ),
            }
        finally:
            queue_user_locale.set_language(previous_language)

    def menu_data(self) -> dict[str, object]:
        return {
            "title": "Тексты WhatsApp", "active": "menu",
            "language_text": language_selection_text_v3(),
            "columns": [
                {"language": queue_user_locale.LANG_RU, "title": "Русский сценарий", **self._admin_live_flow_snapshot(queue_user_locale.LANG_RU)},
                {"language": queue_user_locale.LANG_KZ, "title": "Қазақша сценарий", **self._admin_live_flow_snapshot(queue_user_locale.LANG_KZ)},
            ],
        }

    def categories_data(self) -> dict[str, object]:
        profile = self.deps.menu_config.profile
        rows = profile["categories"]
        used = {str(row["number"]) for row in rows}
        free = [n for n in queue_generic_profile.MENU_NUMBERS if n not in used]
        return {
            "title": "Категории", "active": "categories",
            "greeting_ru": str(profile.get('greeting_ru', '')),
            "greeting_kz": str(profile.get('greeting_kz', '')),
            "editors": [_category_row(str(index), row, used) for index, row in enumerate(rows, start=1)],
            "new_row": _category_row("new", {"number": free[0], "label_ru": "", "label_kz": ""}, used) if free and len(rows) < 8 else None,
        }

    def discovered_contacts(self, search_raw: str = "", limit: int = 80) -> list[dict[str, object]]:
        with self.deps.chat_lock:
            discovered = list(self.deps.chat_state.get("discovered_contacts", [])) if isinstance(self.deps.chat_state.get("discovered_contacts", []), list) else []
        found: list[dict[str, object]] = []
        for item in discovered:
            if not isinstance(item, dict):
                continue
            name = normalize_message(str(item.get("name", "")))[:100]
            phone = normalize_phone(str(item.get("phone", "")))
            chat_id = valid_chat_id(str(item.get("chat_id", "")))
            if not phone or not chat_id or not flexible_contact_match(search_raw, name, phone, chat_id):
                continue
            default_name = name or display_phone(phone)
            found.append({
                "name": default_name,
                "initials": avatar_initials(default_name),
                "avatar_url": self.deps.avatars.url(chat_id),
                "phone": phone,
                "phone_display": display_phone(phone),
                "chat_id": chat_id,
                "saved_text": "Да" if item.get("saved") else "Нет",
                "duplicate": bool(self.deps.store.manual_whatsapp_contact(chat_id, phone)),
            })
            if len(found) >= max(1, min(limit, 200)):
                break
        return found

    def contacts_data(self, query: dict[str, list[str]]) -> dict[str, object]:
        search = _first(query, "q")
        return {
            "title": "Контакты WhatsApp", "active": "contacts", "search": search,
            "contacts": [{
                "name": str(contact["name"]),
                "initials": avatar_initials(str(contact["name"])),
                "avatar_url": self.deps.avatars.url(str(contact["chat_id"])),
                "chat_id": str(contact["chat_id"]),
                "phone_display": display_phone(str(contact["phone"])),
                "created_at": human_time(str(contact["created_at"])),
            } for contact in self.deps.store.list_manual_whatsapp_contacts()],
            "found": self.discovered_contacts(search, 80),
        }

    def render_admin_api_keys(self, query: dict[str, list[str]], *, new_token: str = "", notice: str = "") -> str:
        notice = notice or str(query.get("notice", [""])[0] or "")
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ''
        token_html = f'<section class="panel api-key-secret"><h2>Скопируйте ключ сейчас</h2><p>Полный секрет больше не показывается.</p><code>{e(new_token)}</code></section>' if new_token else ''
        rows = []
        for item in self.deps.auth.list_api_keys(200):
            enabled = bool(item.get("enabled"))
            revoke = f'<form method="post" action="/admin/api-keys"><input type="hidden" name="csrf_token" value="{e(self.deps.csrf_token())}"><input type="hidden" name="action" value="revoke"><input type="hidden" name="key_id" value="{int(item.get("id",0) or 0)}"><button class="button ghost" type="submit">Отключить</button></form>' if enabled else '<span class="muted">Отключён</span>'
            rows.append(f'<tr><td><strong>{e(str(item.get("name") or ""))}</strong><small>{e(str(item.get("token_prefix") or ""))}…</small></td><td>{e(str(item.get("scope") or "read"))}</td><td>{"Активен" if enabled else "Отключён"}</td><td>{e(human_time(str(item.get("last_used_at") or "")) or "—")}</td><td>{e(str(item.get("created_by_name") or "—"))}</td><td>{revoke}</td></tr>')
        table = ''.join(rows) or '<tr><td colspan="6" class="empty">API-ключей пока нет</td></tr>'
        content = f'''{notice_html}{token_html}<section class="panel manual-panel"><div><p class="eyebrow">Интеграции</p><h2>Создать API-ключ</h2><p>Только read-only доступ.</p></div><form method="post" action="/admin/api-keys" class="manual-form"><input type="hidden" name="csrf_token" value="{e(self.deps.csrf_token())}"><input type="hidden" name="action" value="create"><label class="wide">Название<input name="name" maxlength="80" required></label><button class="button primary" type="submit">Создать ключ</button></form></section><section class="panel table-panel"><div class="section-heading"><div><h2>Ключи интеграций</h2><p>Секрет хранится только как SHA-256 хэш.</p></div></div><div class="table-scroll"><table><thead><tr><th>Название</th><th>Scope</th><th>Статус</th><th>Использован</th><th>Создал</th><th></th></tr></thead><tbody>{table}</tbody></table></div></section><section class="panel history-panel"><h2>Endpoints</h2><ul><li><code>GET /api/integration/ping</code></li><li><code>GET /api/integration/tickets?limit=100&amp;status=in_progress</code></li></ul></section>'''
        return self.deps.admin_layout("API-ключи", content, "api_keys")
