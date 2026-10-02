from __future__ import annotations

from typing import Any
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import queue_auth
import queue_features
from queue_core import directory_size, display_phone, e, human_time, parse_iso_datetime
from ticketing import TicketStore, normalize_message

PAGE_SIZE = 25

@dataclass(frozen=True)
class WorkDependencies:
    store: TicketStore
    auth: queue_auth.AuthStore
    employees: list[str]
    max_employees: int
    categories: dict
    statuses: dict
    priorities: dict
    sla_minutes: dict
    data_dir: Path
    database_path: Path
    media_dir: Path
    outbound_media_dir: Path
    monitor_log_path: Path
    monitor_heartbeat_path: Path
    local_mode: bool
    snapshot_cache: dict
    snapshot_cache_lock: Any
    read_marked_at: dict
    read_mark_lock: Any
    category_items: Callable[[], list[tuple[str, str]]]
    connector_snapshot: Callable[[], dict[str, object]]


_deps: WorkDependencies


def configure(dependencies: WorkDependencies) -> None:
    """Configure the declared dependencies once during application startup."""
    global _deps
    _deps = dependencies



def load_employee_names(store, defaults, max_employees) -> list[str]:
    raw = store.get_setting("employees_json", "")
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list) and 1 <= len(values) <= max_employees:
                cleaned = [str(item).strip()[:60] for item in values]
                if all(cleaned) and len({item.casefold() for item in cleaned}) == len(cleaned):
                    return cleaned
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return list(defaults)


def cached_snapshot_value(key: str, ttl_seconds: float, loader):
    now = time.monotonic()
    with _deps.snapshot_cache_lock:
        cached = _deps.snapshot_cache.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
    value = loader()
    with _deps.snapshot_cache_lock:
        _deps.snapshot_cache[key] = (now, value)
    return value


def mark_whatsapp_read_throttled(chat_id: str, interval_seconds: float = 2.0) -> None:
    clean = str(chat_id or "").strip()
    if not clean:
        return
    now = time.monotonic()
    with _deps.read_mark_lock:
        previous = _deps.read_marked_at.get(clean, 0.0)
        if now - previous < interval_seconds:
            return
        _deps.read_marked_at[clean] = now
        if len(_deps.read_marked_at) > 500:
            cutoff = now - 300.0
            for key, marked_at in list(_deps.read_marked_at.items()):
                if marked_at < cutoff:
                    _deps.read_marked_at.pop(key, None)
    _deps.store.mark_whatsapp_chat_read(clean)


def active_employee() -> str:
    employee = str(_deps.store.get_setting("active_employee", "") or "").strip()
    if employee and employee in _deps.employees:
        return employee
    return _deps.employees[0] if _deps.employees else ""


def employee_identity(user: dict[str, object] | None, *, persist: bool = False) -> str:
    """Resolve the employee record bound to an authenticated employee account."""
    user = user or {}
    if str(user.get("role", "")).strip().lower() != "employee":
        return ""
    linked = str(user.get("employee_name", "") or "").strip()[:60]
    display = str(user.get("display_name") or user.get("username") or "Сотрудник").strip()[:60]
    name = linked or display
    if persist and name:
        if name not in _deps.employees and len(_deps.employees) < _deps.max_employees:
            _deps.employees.append(name)
            _deps.store.set_setting("employees_json", json.dumps(_deps.employees, ensure_ascii=False))
        if int(user.get("id", 0) or 0) > 0 and linked != name:
            _deps.auth.set_employee_link(int(user.get("id", 0) or 0), name)
            user["employee_name"] = name
    return name if name in _deps.employees else (display if display else active_employee())


def activate_employee_shift(user: dict[str, object] | None) -> dict[str, object]:
    """Employee login automatically becomes the active shift; admins never use shifts."""
    user = user or {}
    if str(user.get("role", "")).strip().lower() != "employee":
        return {"updated": False, "employee": "", "handed_off": 0}
    employee = employee_identity(user, persist=True)
    if not employee:
        return {"updated": False, "employee": "", "handed_off": 0}
    previous = str(_deps.store.get_setting("active_employee", "") or "").strip()
    handed_off = 0
    if previous and previous != employee:
        try:
            handed_off = int(_deps.store.switch_shift(previous, employee) or 0)
        except Exception:
            handed_off = 0
        try:
            queue_features.announce_shift(_deps.store, previous, employee)
        except Exception:
            pass
    _deps.store.set_setting("active_employee", employee)
    try:
        _deps.auth.record_audit(user, "shift_auto_login", object_type="shift", object_id=employee, details=f"Автоматически на смене после входа; передано заявок: {handed_off}")
    except Exception:
        pass
    return {"updated": previous != employee, "employee": employee, "handed_off": handed_off}


def work_actor() -> str:
    """Return the authenticated account identity for work history."""
    user = queue_auth.current_user() or {}
    role = str(user.get("role", "")).strip().lower()
    if role == "admin":
        return str(user.get("display_name") or user.get("username") or "Администратор").strip()[:100]
    if role == "employee":
        return employee_identity(user) or active_employee()
    return active_employee()


def assignment_names() -> list[str]:
    names = list(_deps.employees)
    user = queue_auth.current_user() or {}
    if str(user.get("role", "")).strip().lower() == "admin":
        admin_name = work_actor()
        if admin_name and admin_name not in names:
            names.append(admin_name)
    return names


def audit_admin_form_submission(path: str, form: dict[str, str], actor: dict[str, object] | None) -> None:
    """Keep a password/token-safe history of admin setting submissions."""
    path = str(path or "")[:160]
    if not path.startswith("/admin/") or path in {"/admin/users"}:
        return
    secret_words = ("password", "token", "secret", "csrf", "key", "cookie")
    safe_parts: list[str] = []
    for key, value in dict(form or {}).items():
        clean_key = str(key or "")[:80]
        if not clean_key or any(word in clean_key.casefold() for word in secret_words):
            continue
        clean_value = normalize_message(str(value or ""))[:180]
        safe_parts.append(f"{clean_key}={clean_value}")
        if len(safe_parts) >= 24:
            break
    details = "; ".join(safe_parts) or "Изменение без открытых параметров"
    try:
        _deps.auth.record_audit(actor or {}, "admin_settings_submit", object_type="admin_settings", object_id=path, details=details)
    except Exception:
        pass


def monitor_log_entries(limit: int = 40) -> list[str]:
    try:
        lines = _deps.monitor_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [normalize_message(line)[:600] for line in lines[-max(1, min(limit, 200)):]][::-1]


def system_status_snapshot() -> dict[str, object]:
    connector = _deps.connector_snapshot()
    database = _deps.store.database_health()
    queue = _deps.store.outbound_queue_snapshot(30)
    try:
        disk = shutil.disk_usage(_deps.data_dir)
        disk_total, disk_used, disk_free = int(disk.total), int(disk.used), int(disk.free)
    except OSError:
        disk_total = disk_used = disk_free = 0
    db_size = _deps.database_path.stat().st_size if _deps.database_path.exists() else 0
    media_size = directory_size(_deps.media_dir)
    outbound_media_size = directory_size(_deps.outbound_media_dir)
    heartbeat_age = None
    if _deps.monitor_heartbeat_path.exists():
        try:
            heartbeat_age = max(0, int(datetime.now(timezone.utc).timestamp() - _deps.monitor_heartbeat_path.stat().st_mtime))
        except OSError:
            heartbeat_age = None
    return {
        "site_ok": True,
        "connector": connector,
        "database": database,
        "queue": queue,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "db_size": db_size,
        "media_size": media_size,
        "outbound_media_size": outbound_media_size,
        "heartbeat_age": heartbeat_age,
        "monitor_ok": True if _deps.local_mode else (heartbeat_age is not None and heartbeat_age < 300),
        "monitor_mode": "local" if _deps.local_mode else "server",
        "logs": monitor_log_entries(40),
    }


def ticket_sla_state(ticket: dict[str, object]) -> dict[str, object]:
    priority = str(ticket.get("priority", "normal"))
    limit_minutes = int(_deps.sla_minutes.get(priority, _deps.sla_minutes["normal"]))
    created = parse_iso_datetime(ticket.get("created_at"))
    first_response = parse_iso_datetime(ticket.get("first_response_at"))
    now = datetime.now(timezone.utc)
    if not created:
        return {"active": False, "overdue": False, "warning": False, "limit_minutes": limit_minutes}
    due = created + timedelta(minutes=limit_minutes)
    status = str(ticket.get("status", ""))
    active = status == "new" and not first_response
    reference = now if active else (first_response or now)
    elapsed = max(0, int((reference - created).total_seconds()))
    overdue = active and now > due
    warning = active and not overdue and elapsed >= int(limit_minutes * 60 * 0.75)
    remaining = int((due - now).total_seconds()) if active else 0
    return {
        "active": active, "overdue": overdue, "warning": warning,
        "limit_minutes": limit_minutes, "due_epoch": int(due.timestamp()),
        "elapsed_seconds": elapsed, "remaining_seconds": remaining,
    }


def sla_badge(ticket: dict[str, object]) -> str:
    state = ticket_sla_state(ticket)
    if not state.get("active"):
        return ""
    remaining = int(state.get("remaining_seconds", 0) or 0)
    overdue = bool(state.get("overdue"))
    warning = bool(state.get("warning"))
    seconds = abs(remaining)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    short = f"{hours}ч {minutes}м" if hours else f"{minutes}м"
    label = f"SLA просрочен {short}" if overdue else f"SLA {short}"
    cls = "sla-overdue" if overdue else "sla-warning" if warning else "sla-ok"
    return (
        f'<span class="sla-badge {cls}" data-sla-deadline="{int(state.get("due_epoch",0) or 0)}" '
        f'data-sla-active="1">{e(label)}</span>'
    )


def status_badge(status: str) -> str:
    return f'<span class="status status-{e(status)}">{e(_deps.statuses.get(status, status))}</span>'


def priority_badge(priority: str) -> str:
    return f'<span class="priority priority-{e(priority)}">{e(_deps.priorities.get(priority, priority))}</span>'


def employee_options(selected: str = "") -> str:
    names = assignment_names()
    selected = selected if selected in names else work_actor()
    return "".join(
        f'<option value="{e(name)}" {"selected" if name == selected else ""}>{e(name)}</option>'
        for name in names
    )


def category_options(selected: str = "", include_all: bool = False) -> str:
    prefix = '<option value="">Все категории</option>' if include_all else ""
    items = list(_deps.categories.items()) if include_all else _deps.category_items()
    return prefix + "".join(
        f'<option value="{e(key)}" {"selected" if key == selected else ""}>{e(label)}</option>'
        for key, label in items
    )


def priority_options(selected: str = "", include_all: bool = False) -> str:
    prefix = '<option value="">Все приоритеты</option>' if include_all else ""
    return prefix + "".join(
        f'<option value="{e(key)}" {"selected" if key == selected else ""}>{e(label)}</option>'
        for key, label in _deps.priorities.items()
    )


def inline_priority_select(ticket_id: int, selected: str) -> str:
    return (
        f'<select class="priority-select priority-select-{e(selected)}" '
        f'data-quick-priority data-ticket-id="{ticket_id}" '
        f'aria-label="Приоритет заявки #{ticket_id}">'
        f'{priority_options(selected)}</select>'
    )


def render_ticket_rows(tickets: list[dict[str, object]]) -> str:
    rows: list[str] = []
    for ticket in tickets:
        source_label = {
            "whatsapp": "WhatsApp",
            "telegram_manual": "Telegram вручную",
            "admin_manual": "Вручную",
        }.get(str(ticket["source"]), "Вручную")
        sla = ticket_sla_state(ticket)
        row_sla_class = " sla-row-overdue" if sla.get("overdue") else " sla-row-warning" if sla.get("warning") else ""
        rows.append(
            f"""<tr class="{row_sla_class.strip()}" data-ticket-row data-revision="{int(ticket.get('revision',0))}" data-ticket-id="{ticket['id']}" data-ticket-title="{e(ticket['title'])}" data-assigned-to="{e(ticket['assigned_to'])}" data-ticket-status="{e(ticket['status'])}">
              <td class="priority-cell">{inline_priority_select(int(ticket['id']), str(ticket['priority']))}</td>
              <td class="ticket-number">#{ticket['id']}</td>
              <td><span class="source source-{e(ticket['source'])}">{e(source_label)}</span></td>
              <td><strong>{e(ticket['sender'])}</strong><small>{e(display_phone(str(ticket['phone'])))}</small></td>
              <td><a class="ticket-title-link" href="/ticket?id={ticket['id']}"><strong>{e(ticket['title'])}</strong></a><small class="summary">{e(ticket['summary'])}</small></td>
              <td>{status_badge(str(ticket['status']))}{'<span class="handoff-badge">Следующая смена</span>' if int(ticket.get('shift_handoff', 0) or 0) else ''}{sla_badge(ticket)}<small>{e(ticket['assigned_to'])}</small></td>
              <td><time>{e(human_time(str(ticket['created_at'])))}</time></td>
            </tr>"""
        )
    return "".join(rows) or '<tr><td colspan="7" class="empty">Заявок пока нет. Запусти WhatsApp QR-коннектор или добавь заявку вручную.</td></tr>'


def query_page(query: dict[str, list[str]]) -> int:
    try:
        return max(1, int(query.get("page", ["1"])[0]))
    except ValueError:
        return 1


def render_pagination(
    query: dict[str, list[str]],
    total: int,
    page: int,
    page_size: int = PAGE_SIZE,
) -> str:
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    if pages <= 1:
        return f'<span class="pagination-total">Всего заявок: {total}</span>'

    def page_url(number: int) -> str:
        values = {
            key: items[0]
            for key, items in query.items()
            if items and items[0] and key != "page"
        }
        if number > 1:
            values["page"] = str(number)
        encoded = urlencode(values)
        return f"/?{encoded}" if encoded else "/"

    visible = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    links: list[str] = []
    previous = 0
    for number in visible:
        if previous and number - previous > 1:
            links.append('<span class="pagination-gap">…</span>')
        current = " current" if number == page else ""
        links.append(
            f'<a class="pagination-link{current}" href="{e(page_url(number))}">{number}</a>'
        )
        previous = number
    prev_link = (
        f'<a class="pagination-link" href="{e(page_url(page - 1))}">Назад</a>'
        if page > 1
        else '<span class="pagination-link disabled">Назад</span>'
    )
    next_link = (
        f'<a class="pagination-link" href="{e(page_url(page + 1))}">Вперёд</a>'
        if page < pages
        else '<span class="pagination-link disabled">Вперёд</span>'
    )
    return (
        f'<span class="pagination-total">Страница {page} из {pages} · заявок {total}</span>'
        f'<div class="pagination-links">{prev_link}{"".join(links)}{next_link}</div>'
    )


