"""The administration pages."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from urllib.parse import quote, urlencode

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
from queue_work_context import category_options, employee_options, monitor_log_entries, priority_options, system_status_snapshot
from ticketing import STATUSES, TicketStore
from queue_templates import render_template

ERROR_REPORT_STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "resolved": "Исправлена",
    "rejected": "Отклонена",
}


def _admin_flow_message(title: str, text: str, note: str = "") -> str:
    note_html = f'<small class="admin-flow-note">{e(note)}</small>' if note else ""
    return (
        '<article class="panel admin-flow-card">'
        f'<div class="admin-flow-card-head"><h3>{e(title)}</h3>{note_html}</div>'
        f'<pre class="admin-flow-message">{e(text)}</pre>'
        '</article>'
    )


def _category_editor_row(suffix: str, row: dict[str, object], used_numbers: set[str]) -> str:
    number = str(row.get("number", ""))
    options = "".join(
        f'<option value="{n}" {"selected" if n == number else ""}>{n}</option>'
        for n in queue_generic_profile.MENU_NUMBERS if n == number or n not in used_numbers
    )
    required = set(row.get("required_fields") or ["problem"])
    field_labels = {"problem": "описание", "reference": "номер / идентификатор", "email": "email"}
    checks = "".join(
        f'<label class="checkbox-label"><input type="checkbox" name="req_{name}_{suffix}" value="1" {"checked" if name in required else ""}>{label}</label>'
        for name, label in field_labels.items()
    )
    key_field = f'<input type="hidden" name="key_{suffix}" value="{e(str(row["key"]))}">' if suffix != "new" else ""
    delete = (f'<label class="checkbox-label danger-choice"><input type="checkbox" name="delete_{suffix}" value="1">Удалить категорию</label>'
              if suffix != "new" else "")
    required_attr = "required" if suffix != "new" else ""
    return render_template(
        "admin/category_editor_row.html",
        key_field=key_field,
        suffix=suffix,
        options=options,
        label_ru=e(str(row.get('label_ru', ''))),
        required_attr=required_attr,
        label_kz=e(str(row.get('label_kz', ''))),
        prompt_ru=e(str(row.get('prompt_ru', ''))),
        prompt_kz=e(str(row.get('prompt_kz', ''))),
        checks=checks,
        delete=delete,
    )


def avatar_html(avatars: AvatarCache, chat_id: str, name: str) -> str:
    url = avatars.url(chat_id)
    letters = ''.join(word[0] for word in name.split()[:2]) or '?'
    pic = f'<img src="{e(url)}" alt="" loading="lazy" onerror="this.remove()">' if url else ''
    return f'<span class="chat-avatar table-avatar">{e(letters)}{pic}</span>'


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


class AdminPages:
    def __init__(self, deps: AdminPageDependencies):
        self.deps = deps

    def render_admin_home(self) -> str:
        contacts = len(self.deps.store.list_manual_whatsapp_contacts())
        groups = len(self.deps.store.list_whatsapp_groups())
        error_counts = self.deps.store.error_report_counts()
        open_errors = int(error_counts.get("new", 0)) + int(error_counts.get("in_progress", 0))
        cards = render_template(
            "admin/home.html",
            users_count=len(self.deps.auth.list_users()),
            employees_count=len(self.deps.employees),
            categories_count=len(self.deps.menu.active_ticket_category_items()),
            open_errors=open_errors,
            contacts=contacts,
            groups=groups,
            local_mode_text='Локальный тестовый режим' if self.deps.local_mode else 'Авторизация приложения',
            local_mode_text_2='Windows-копия использует отдельную тестовую базу и отдельные логины.' if self.deps.local_mode else 'Пользователи входят через /login. Существующий Nginx-администратор сохранён как аварийный путь для первичной настройки и восстановления доступа.',
        )
        return self.deps.admin_layout("Админка", cards, "home")

    def error_report_status_badge(self, status: str) -> str:
        label = ERROR_REPORT_STATUS_LABELS.get(status, status or "Новая")
        css = {
            "new": "status-new",
            "in_progress": "status-in_progress",
            "resolved": "status-done",
            "rejected": "status-invalid",
        }.get(status, "status-new")
        return f'<span class="status {css}">{e(label)}</span>'

    def render_admin_errors(self, query: dict[str, list[str]]) -> str:
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        selected_status = query.get("status", [""])[0]
        search = query.get("q", [""])[0]
        reports = self.deps.store.list_error_reports(selected_status, search, 300)
        rows: list[str] = []
        for report in reports:
            report_id = int(report.get("id", 0) or 0)
            description = normalize_message(str(report.get("description", "")))
            sender = str(report.get("sender", "") or "Неизвестный пользователь")
            phone = display_phone(str(report.get("phone", ""))) if report.get("phone") else "—"
            attachment = str(report.get("attachment_name", "") or "")
            rows.append(
                render_template(
                    "admin/error_row.html",
                    report_id=report_id,
                    error_report_status_badge=self.error_report_status_badge(str(report.get("status", "new"))),
                    sender=e(sender),
                    phone=e(phone),
                    description=e(description[:150] or "Без текста"),
                    attachment_text=e(("Вложение: " + attachment) if attachment else ""),
                    human_time=e(human_time(str(report.get("created_at", ""))) or "—"),
                )
            )
        table_rows = "".join(rows) or '<tr><td colspan="5" class="empty">Репортов по этому фильтру нет</td></tr>'
        options = '<option value="">Все статусы</option>' + "".join(
            f'<option value="{key}" {"selected" if selected_status == key else ""}>{e(label)}</option>'
            for key, label in ERROR_REPORT_STATUS_LABELS.items()
        )
        content = render_template(
            "admin/errors.html",
            notice_html=notice_html,
            search=e(search),
            options=options,
            table_rows=table_rows,
        )
        return self.deps.admin_layout("Ошибки", content, "errors")

    def render_admin_error(self, report_id: int, query: dict[str, list[str]]) -> str:
        report = self.deps.store.get_error_report(report_id)
        if not report:
            return self.deps.admin_layout("Ошибка не найдена", '<section class="panel empty"><h2>Репорт не найден</h2><a class="button" href="/admin/errors">К списку ошибок</a></section>', "errors")
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        chat_id = str(report.get("chat_id", "") or "")
        external_id = str(report.get("external_id", "") or "")
        message = self.deps.store.get_whatsapp_message(chat_id, external_id) if chat_id and external_id else None
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
        content = render_template(
            "admin/error.html",
            notice_html=notice_html,
            id=int(report.get("id", 0) or 0),
            human_time=e(human_time(str(report.get("created_at", "")))),
            chat_link=chat_link,
            error_report_status_badge=self.error_report_status_badge(str(report.get("status", "new"))),
            sender=e(str(report.get("sender", "") or "Неизвестный")),
            phone_text=e(display_phone(str(report.get("phone", ""))) if report.get("phone") else "—"),
            description=e(str(report.get("description", "") or "Без текста")),
            attachment_html=attachment_html,
            csrf_token=e(self.deps.csrf_token()),
            status_options=status_options,
            admin_note=e(str(report.get("admin_note", "") or "")),
        )
        return self.deps.admin_layout(f"Ошибка #{report_id}", content, "errors")

    def render_admin_audit(self, query: dict[str, list[str]]) -> str:
        level = query.get("level", [""])[0]
        search = query.get("q", [""])[0]
        entries = self.deps.store.list_audit_entries(300, level, search)
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
        for item in self.deps.auth.list_audit_events(200):
            actor=str(item.get("actor_display_name") or item.get("actor_username") or "Система")
            auth_rows.append(f'<tr><td>{e(human_time(str(item.get("created_at") or "")))}</td><td>{e(actor)}</td><td><strong>{e(str(item.get("action") or ""))}</strong><small>{e(str(item.get("details") or ""))}</small></td><td>{e(" · ".join(part for part in [str(item.get("object_type") or ""),str(item.get("object_id") or "")] if part) or "—")}</td></tr>')
        auth_audit_html=''.join(auth_rows) or '<tr><td colspan="4" class="empty">Событий доступа пока нет</td></tr>'
        levels = '<option value="">Все уровни</option>' + "".join(
            f'<option value="{key}" {"selected" if level == key else ""}>{label}</option>'
            for key, label in [("info","Инфо"),("warning","Внимание"),("error","Ошибка")]
        )
        content = render_template(
            "admin/audit.html",
            search=e(search),
            levels=levels,
            audit_rows=audit_rows,
            auth_audit_html=auth_audit_html,
            actor_filter=e(str(query.get('actor',[''])[0] or '')),
            action_filter=e(str(query.get('action',[''])[0] or '')),
            unified_audit_rows=self._unified_audit_rows(query),
            monitor_html=monitor_html,
        )
        return self.deps.admin_layout("Журнал", content, "audit")

    def _analytics_filters(self, query: dict[str, list[str]], action: str) -> str:
        employee=str(query.get("employee",[""])[0] or ""); date_from=str(query.get("date_from",[""])[0] or ""); date_to=str(query.get("date_to",[""])[0] or ""); status=str(query.get("status",[""])[0] or "")
        employees=sorted({str(item.get("assigned_to") or "") for item in self.deps.store.list_tickets("","","","",5000,0) if str(item.get("assigned_to") or "")},key=str.casefold)
        employee_html='<option value="">Все сотрудники</option>'+''.join(f'<option value="{e(name)}" {"selected" if name==employee else ""}>{e(name)}</option>' for name in employees)
        status_html='<option value="">Все статусы</option>'+''.join(f'<option value="{e(key)}" {"selected" if key==status else ""}>{e(label)}</option>' for key,label in STATUSES.items())
        category=str(query.get("category",[""])[0] or "")
        return f'''<form class="filters analytics-final-filters" method="get" action="{e(action)}"><input type="date" name="date_from" value="{e(date_from)}"><input type="date" name="date_to" value="{e(date_to)}"><select name="employee">{employee_html}</select><select name="status">{status_html}</select><select name="category">{category_options(category,True)}</select><button class="button" type="submit">Применить</button><a class="button ghost" href="{e(action)}">Сбросить</a></form>'''

    def render_admin_analytics_final(self, query: dict[str, list[str]]) -> str:
        data=self.deps.reporting.analytics_snapshot(query)
        employee_rows=''.join(f'<tr><td><strong>{e(name)}</strong></td><td>{vals["total"]}</td><td>{vals["open"]}</td><td>{vals["done"]}</td><td>{vals["invalid"]}</td><td>{vals["handoff"]}</td></tr>' for name,vals in sorted(data["by_employee"].items(),key=lambda kv:(-kv[1]["total"],kv[0].casefold()))) or '<tr><td colspan="6" class="empty">Нет данных</td></tr>'
        category_rows=''.join(f'<tr><td>{e(name)}</td><td>{count}</td><td>{round(count/max(1,int(data["total"]))*100,1)}%</td></tr>' for name,count in list(data["by_category"].items())[:30]) or '<tr><td colspan="3" class="empty">Нет данных</td></tr>'
        q=urlencode({k:v[0] for k,v in query.items() if v and v[0]}); export_href='/admin/tickets-export.csv'+(('?'+q) if q else '')
        content=f'''<section class="page-heading"><div><p class="eyebrow">Аналитика</p><h1>Статистика работы</h1><p>Сотрудники, смены, категории и скорость обработки заявок</p></div><a class="button primary" href="{e(export_href)}">Экспорт CSV</a></section>{self._analytics_filters(query,'/admin/analytics')}<section class="metrics analytics-final-metrics"><div class="metric"><span>Всего</span><strong>{data["total"]}</strong></div><div class="metric"><span>Открыты</span><strong>{data["open"]}</strong></div><div class="metric"><span>Выполнены</span><strong>{data["done"]}</strong></div><div class="metric"><span>Первый ответ</span><strong>{data["avg_first"]} мин</strong></div><div class="metric"><span>Решение</span><strong>{data["avg_resolution"]} мин</strong></div></section><section class="panel table-panel"><div class="section-heading"><div><h2>По сотрудникам и сменам</h2><p>Нагрузка и результат</p></div></div><div class="table-scroll"><table><thead><tr><th>Сотрудник</th><th>Всего</th><th>Открыты</th><th>Сделано</th><th>Недейств.</th><th>Передано</th></tr></thead><tbody>{employee_rows}</tbody></table></div></section><section class="panel table-panel"><div class="section-heading"><div><h2>По категориям</h2><p>Самые частые типы обращений</p></div></div><div class="table-scroll"><table><thead><tr><th>Категория</th><th>Заявки</th><th>Доля</th></tr></thead><tbody>{category_rows}</tbody></table></div></section>'''
        return self.deps.admin_layout("Аналитика",content,"analytics")

    def render_admin_manager_dashboard(self, query: dict[str, list[str]]) -> str:
        data=self.deps.reporting.analytics_snapshot(query)
        cats=''.join(f'<div class="manager-problem-row"><span>{e(name)}</span><strong>{count}</strong></div>' for name,count in list(data["by_category"].items())[:8]) or '<p class="empty">Нет данных</p>'
        emps=''.join(f'<div class="manager-load-row"><span><strong>{e(name)}</strong><small>открыто {vals["open"]} · сделано {vals["done"]}</small></span><b>{vals["total"]}</b></div>' for name,vals in sorted(data["by_employee"].items(),key=lambda kv:(-kv[1]["total"],kv[0].casefold()))[:12]) or '<p class="empty">Нет данных</p>'
        content=f'''<section class="page-heading"><div><p class="eyebrow">Руководитель</p><h1>Dashboard</h1><p>Сводная нагрузка и ключевые показатели</p></div></section>{self._analytics_filters(query,'/admin/manager')}<section class="manager-kpi-grid"><div class="panel manager-kpi"><span>Заявок</span><strong>{data["total"]}</strong></div><div class="panel manager-kpi"><span>Открыто</span><strong>{data["open"]}</strong></div><div class="panel manager-kpi"><span>Первый ответ</span><strong>{data["avg_first"]} мин</strong></div><div class="panel manager-kpi"><span>Решение</span><strong>{data["avg_resolution"]} мин</strong></div><div class="panel manager-kpi"><span>Передано сменой</span><strong>{data["handoffs"]}</strong></div></section><section class="manager-dashboard-grid"><div class="panel"><div class="section-heading"><div><h2>Нагрузка по сотрудникам</h2></div></div><div class="manager-list">{emps}</div></div><div class="panel"><div class="section-heading"><div><h2>Частые проблемы</h2></div></div><div class="manager-list">{cats}</div></div></section>'''
        return self.deps.admin_layout("Dashboard руководителя",content,"manager")

    def render_admin_api_keys(self, query: dict[str, list[str]], *, new_token: str="", notice: str="") -> str:
        notice=notice or str(query.get("notice",[""])[0] or ""); notice_html=f'<div class="notice">{e(notice)}</div>' if notice else ''
        token_html=f'<section class="panel api-key-secret"><h2>Скопируйте ключ сейчас</h2><p>Полный секрет больше не показывается.</p><code>{e(new_token)}</code></section>' if new_token else ''
        rows=[]
        for item in self.deps.auth.list_api_keys(200):
            enabled=bool(item.get("enabled")); revoke=f'<form method="post" action="/admin/api-keys"><input type="hidden" name="csrf_token" value="{e(self.deps.csrf_token())}"><input type="hidden" name="action" value="revoke"><input type="hidden" name="key_id" value="{int(item.get("id",0) or 0)}"><button class="button ghost" type="submit">Отключить</button></form>' if enabled else '<span class="muted">Отключён</span>'
            rows.append(f'<tr><td><strong>{e(str(item.get("name") or ""))}</strong><small>{e(str(item.get("token_prefix") or ""))}…</small></td><td>{e(str(item.get("scope") or "read"))}</td><td>{"Активен" if enabled else "Отключён"}</td><td>{e(human_time(str(item.get("last_used_at") or "")) or "—")}</td><td>{e(str(item.get("created_by_name") or "—"))}</td><td>{revoke}</td></tr>')
        table=''.join(rows) or '<tr><td colspan="6" class="empty">API-ключей пока нет</td></tr>'
        content=f'''{notice_html}{token_html}<section class="panel manual-panel"><div><p class="eyebrow">Интеграции</p><h2>Создать API-ключ</h2><p>Только read-only доступ.</p></div><form method="post" action="/admin/api-keys" class="manual-form"><input type="hidden" name="csrf_token" value="{e(self.deps.csrf_token())}"><input type="hidden" name="action" value="create"><label class="wide">Название<input name="name" maxlength="80" required></label><button class="button primary" type="submit">Создать ключ</button></form></section><section class="panel table-panel"><div class="section-heading"><div><h2>Ключи интеграций</h2><p>Секрет хранится только как SHA-256 хэш.</p></div></div><div class="table-scroll"><table><thead><tr><th>Название</th><th>Scope</th><th>Статус</th><th>Использован</th><th>Создал</th><th></th></tr></thead><tbody>{table}</tbody></table></div></section><section class="panel history-panel"><h2>Endpoints</h2><ul><li><code>GET /api/integration/ping</code></li><li><code>GET /api/integration/tickets?limit=100&amp;status=in_progress</code></li></ul></section>'''
        return self.deps.admin_layout("API-ключи",content,"api_keys")

    def _unified_audit_rows(self, query: dict[str, list[str]]) -> str:
        events = self.deps.reporting.unified_audit_events(query, 400)
        return ''.join(
            f'<tr><td>{e(human_time(item["created_at"]))}</td><td>{e(item["actor"])}</td><td>{e(item["action"][:220])}</td><td>{e(item["object"] or "—")}</td><td>{e(item["source"])}</td></tr>'
            for item in events
        ) or '<tr><td colspan="5" class="empty">Событий нет</td></tr>'

    def render_admin_system(self, query: dict[str, list[str]]) -> str:
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        state = system_status_snapshot()
        resources = self.deps.runtime.process_resource_snapshot()
        backup_state = self.deps.runtime.scheduled_backup_snapshot()
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
        cards = render_template(
            "admin/system_cards.html",
            connector_ok_text='system-ok' if connector_ok else 'system-bad',
            connector_ok_text_2=e('Подключён' if connector_ok else connector_status),
            connector_updated_at=e(str(connector.get('updated_at',''))[:19] if isinstance(connector,dict) else ''),
            ok_text='system-ok' if database.get('ok') else 'system-bad',
            ok_text_2=e('OK' if database.get('ok') else 'Ошибка'),
            result=e(str(database.get('result',''))),
            monitor_ok_text='system-ok' if state.get('monitor_ok') else 'system-warn',
            monitor_label=e(monitor_label),
            local_monitor_text=e('На Windows watchdog отключён' if local_monitor else 'Проверка: ' + heartbeat_text),
        )
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
        content = render_template(
            "admin/system.html",
            notice_html=notice_html,
            cards=cards,
            human_bytes=human_bytes(disk_free),
            human_bytes_2=human_bytes(disk_total),
            disk_percent=disk_percent,
            human_bytes_3=human_bytes(int(state.get('db_size',0) or 0)),
            human_bytes_4=human_bytes(int(state.get('media_size',0) or 0)),
            human_bytes_5=human_bytes(int(resources.get('rss_bytes',0) or 0)),
            cpu_count=int(resources.get('cpu_count',1) or 1),
            load_text='Load: ' + e(str(resources.get('load'))) if resources.get('load') else 'CPU time: ' + e(str(resources.get('cpu_time'))) + ' с',
            csrf_token=e(self.deps.csrf_token()),
            restore_label=e(restore_label),
            last_file=e(str(backup_state.get('last_file') or 'ещё нет')),
            count=int(backup_state.get('count',0) or 0),
            last_error_text=(' · ошибка: ' + e(str(backup_state.get('last_error')))) if backup_state.get('last_error') else '',
            hours=int(backup_state.get('hours',24) or 24),
            keep=int(backup_state.get('keep',14) or 14),
            pending=int(counts.get('pending',0) or 0),
            sending_count=int(counts.get('processing',0) or 0) + int(counts.get('sending',0) or 0),
            sent=int(counts.get('sent',0) or 0),
            uncertain=int(counts.get('uncertain',0) or 0),
            failed=int(counts.get('failed',0) or 0),
            queue_table=queue_table,
            logs=logs,
        )
        return self.deps.admin_layout("Система", content, "system")

    def admin_employee_count(self, query: dict[str, list[str]]) -> int:
        try:
            requested = int(query.get("count", [str(len(self.deps.employees))])[0])
        except ValueError:
            requested = len(self.deps.employees)
        return max(1, min(self.deps.max_employees, requested))

    def render_admin_employees(self, query: dict[str, list[str]]) -> str:
        count = self.admin_employee_count(query)
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""

        proposed = list(self.deps.employees[:count])
        while len(proposed) < count:
            proposed.append(f"Сотрудник {len(proposed) + 1}")

        fields = "".join(
            f'<label>Сотрудник {index}'
            f'<input name="employee_{index}" value="{e(name)}" maxlength="60" required>'
            f'</label>'
            for index, name in enumerate(proposed, start=1)
        )

        content = render_template(
            "admin/employees.html",
            notice_html=notice_html,
            employees_count=len(self.deps.employees),
            count=count,
            max_employees=self.deps.max_employees,
            csrf_token=e(self.deps.csrf_token()),
            fields=fields,
        )
        return self.deps.admin_layout("Сотрудники", content, "employees")

    def render_admin_manual(self, query: dict[str, list[str]]) -> str:
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        content = render_template(
            "admin/manual.html",
            notice_html=notice_html,
            csrf_token=e(self.deps.csrf_token()),
            category_options=category_options('general'),
            priority_options=priority_options('normal'),
            employee_options=employee_options(),
        )
        return self.deps.admin_layout("Ручная заявка", content, "manual")

    def _admin_live_flow_snapshot(self, language: str) -> dict[str, object]:
        previous_language = queue_user_locale.current_language()
        queue_user_locale.set_language(language)
        try:
            categories: list[tuple[str, str]] = []
            for row in sorted(self.deps.menu_config.rows(), key=lambda row: int(row["number"])):
                category = str(row["key"])
                categories.append((f"{row['number']}. {self.deps.menu.user_menu_label(category)}", self.deps.menu.configured_category_prompt(category)))
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

    def _admin_flow_language_column(self, language: str, title: str) -> str:
        snapshot = self._admin_live_flow_snapshot(language)
        category_cards = "".join(
            _admin_flow_message(label, prompt, "После выбора пункта меню")
            for label, prompt in snapshot["categories"]
        )
        return render_template(
            "admin/flow_language_column.html",
            language=e(language),
            upper=e(language.upper()),
            title=e(title),
            admin_flow_message=_admin_flow_message("Шаг 2 · Подразделение и должность", str(snapshot["profile"])),
            admin_flow_message_2=_admin_flow_message("Шаг 3 · Главное меню", str(snapshot["menu"])),
            category_cards=category_cards,
            admin_flow_message_3=_admin_flow_message("Есть активные заявки", str(snapshot["active"])),
            admin_flow_message_4=_admin_flow_message("Активных заявок нет", str(snapshot["active_empty"])),
            admin_flow_message_5=_admin_flow_message("Заявка принята", str(snapshot["accepted"])),
            admin_flow_message_6=_admin_flow_message("Не хватает данных", str(snapshot["missing"])),
            admin_flow_message_7=_admin_flow_message("Дублирующаяся заявка", str(snapshot["duplicate"])),
            admin_flow_message_8=_admin_flow_message("Заявка выполнена", str(snapshot["done"])),
            admin_flow_message_9=_admin_flow_message("Заявка закрыта / отклонена", str(snapshot["invalid"])),
        )

    def render_admin_menu(self, query: dict[str, list[str]]) -> str:
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        language_text = language_selection_text_v3()
        content = render_template(
            "admin/menu.html",
            notice_html=notice_html,
            admin_flow_message=_admin_flow_message("Шаг 1 · Выбор языка", language_text, "Общее сообщение RU/KZ"),
            admin_flow_language_column=self._admin_flow_language_column(queue_user_locale.LANG_RU, "Русский сценарий"),
            admin_flow_language_column_2=self._admin_flow_language_column(queue_user_locale.LANG_KZ, "Қазақша сценарий"),
        )
        return self.deps.admin_layout("Тексты WhatsApp", content, "menu")

    def render_admin_categories(self, query: dict[str, list[str]]) -> str:
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        profile = self.deps.menu_config.profile
        rows = profile["categories"]
        used = {str(row["number"]) for row in rows}
        editors = "".join(_category_editor_row(str(index), row, used) for index, row in enumerate(rows, start=1))
        free = [n for n in queue_generic_profile.MENU_NUMBERS if n not in used]
        add_row = (_category_editor_row("new", {"number": free[0], "label_ru": "", "label_kz": ""}, used)
                   if free and len(rows) < 8 else '<p class="muted">Все номера меню заняты.</p>')
        content = render_template(
            "admin/categories.html",
            notice_html=notice_html,
            csrf_token=e(self.deps.csrf_token()),
            greeting_ru=e(str(profile.get('greeting_ru', ''))),
            greeting_kz=e(str(profile.get('greeting_kz', ''))),
            editors=editors,
            add_row=add_row,
        )
        return self.deps.admin_layout("Категории", content, "categories")

    def discovered_contact_rows(self, search_raw: str = "", limit: int = 80) -> str:
        with self.deps.chat_lock:
            discovered = list(self.deps.chat_state.get("discovered_contacts", [])) if isinstance(self.deps.chat_state.get("discovered_contacts", []), list) else []
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
            duplicate = bool(self.deps.store.manual_whatsapp_contact(chat_id, phone))
            if duplicate:
                action = '<span class="duplicate-badge">Уже добавлен</span>'
            else:
                action = f"""<form method="post" action="/admin/contacts"><input type="hidden" name="csrf_token" value="{e(self.deps.csrf_token())}"><input type="hidden" name="action" value="save"><input type="hidden" name="name" value="{e(default_name)}"><input type="hidden" name="phone" value="{e(phone)}"><button class="button primary" type="submit">Добавить</button></form>"""
            found_rows.append(
                f"""<tr><td><span class="avatar-name">{avatar_html(self.deps.avatars,chat_id,default_name)}<strong>{e(default_name)}</strong></span></td><td>{e(display_phone(phone))}</td><td>{e(saved_text)}</td><td class="table-actions"><a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат</a>{action}</td></tr>"""
            )
            if len(found_rows) >= max(1, min(limit, 200)):
                break
        return "".join(found_rows) or '<tr><td colspan="4" class="empty">Ничего не найдено. Попробуйте имя, часть номера или обновите контакты.</td></tr>'

    def render_admin_contacts(self, query: dict[str, list[str]]) -> str:
        notice = query.get("notice", [""])[0]
        notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
        rows = []
        for contact in self.deps.store.list_manual_whatsapp_contacts():
            chat_id = str(contact["chat_id"])
            rows.append(f"""<tr><td><span class="avatar-name">{avatar_html(self.deps.avatars,chat_id,str(contact['name']))}<strong>{e(contact['name'])}</strong></span></td><td>{e(display_phone(str(contact['phone'])))}</td><td>{e(human_time(str(contact['created_at'])))}</td><td class="table-actions"><a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Профиль / чат</a><form method="post" action="/admin/contacts" onsubmit="return confirm('Удалить контакт из системы?')"><input type="hidden" name="csrf_token" value="{e(self.deps.csrf_token())}"><input type="hidden" name="action" value="delete"><input type="hidden" name="chat_id" value="{e(chat_id)}"><button class="button ghost" type="submit">Удалить</button></form></td></tr>""")
        table = "".join(rows) or '<tr><td colspan="4" class="empty">Контакты вручную ещё не добавлены</td></tr>'
        search_raw = query.get("q", [""])[0]
        found_table = self.discovered_contact_rows(search_raw, 80)
        content = render_template(
            "admin/contacts.html",
            notice_html=notice_html,
            csrf_token=e(self.deps.csrf_token()),
            table=table,
            search_raw=quote(search_raw),
            search_raw_2=e(search_raw),
            found_table=found_table,
        )
        return self.deps.admin_layout("Контакты WhatsApp", content, "contacts")
