from __future__ import annotations

from typing import Any

import queue_transit
import queue_operations
import queue_source_sync
import queue_transit_process

def bind(context: dict[str, Any]) -> None:
    protected = {"bind", "Any"}
    for name, value in context.items():
        if name.startswith("__") or name in protected:
            continue
        globals()[name] = value

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
        ("instruction", INSTRUCTION_URL, "Инструкция", ""),
        ("transit_process", "/transit-process", "Процесс Транзит", ""),
    ]
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
    user_prefs = AUTH.get_user_preferences(user_id) if user_id > 0 else {}
    preferred_theme = str(user_prefs.get("theme") or "").strip()
    if preferred_theme not in {"light", "dark", "pink"}:
        preferred_theme = ""
    shift_note = "Работает от имени аккаунта" if str(auth_user.get("role")) == "admin" else f"На смене автоматически: {e(employee_identity(auth_user) or current_employee)}"
    legacy_note = bool(auth_user.get("legacy"))
    password_form = "" if legacy_note else f'''<form class="auth-profile-password" method="post" action="/account/password">
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
  <meta name="queue-csrf" content="{e(ADMIN_FORM_TOKEN)}">
  <title>{e(title)} · Единая очередь</title>
  <meta name="theme-color" content="#112a25">
  <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon.png?v=3.3.79">
  <link rel="apple-touch-icon" href="/static/favicon.png?v=3.3.79">
  <script>document.documentElement.dataset.theme = {json.dumps(preferred_theme)} || localStorage.getItem("queue-theme") || "light";</script>
  <link rel="stylesheet" href="/static/style.css?v=1.00.3">
  <link rel="stylesheet" href="/static/ui.css?v=3.3.79">
  <link rel="stylesheet" href="/static/instruction.css?v=1.00.6.96">
  <link rel="stylesheet" href="/static/transit-process.css?v=1.00.6.76">
  <script src="/static/transit-process.js?v=1.00.6.76" defer></script>
  <link rel="stylesheet" href="/static/global-theme.css?v=1.00.5">
  <link rel="stylesheet" href="/static/productivity.css?v=3.3.90">
  <link rel="stylesheet" href="/static/workflow.css?v=3.3.97">
  <link rel="stylesheet" href="/static/reliability.css?v=3.3.99">
  <link rel="stylesheet" href="/static/notification-hotfix.css?v=3.3.104">
  <link rel="stylesheet" href="/static/interface105.css?v=3.3.111">
  <link rel="stylesheet" href="/static/performance107.css?v=3.3.107">
  <link rel="stylesheet" href="/static/hotfix10069.css?v=1.00.6.10">
  <link rel="stylesheet" href="/static/auth100615.css?v=1.00.6.40">
  <link rel="stylesheet" href="/static/pink-theme.css?v=1.00.6.40">
  <link rel="stylesheet" href="/static/ui-polish-1.00.6.40.css?v=1.00.6.40">
  <link rel="stylesheet" href="/static/user-ui-1.00.6.97.css?v=1.00.6.97">
</head>
<body data-admin="{'1' if show_admin else '0'}" data-auth-user-id="{user_id}">
  <header class="topbar">
    <a class="brand" href="/"><span class="brand-mark">Q</span><span>Единая очередь</span></a>
    {'<span class="local-mode-badge">WINDOWS · ТЕСТ</span>' if LOCAL_MODE else ''}
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
  {'<div class="maintenance-banner">Режим обслуживания включён: просмотр доступен, изменения сотрудников временно заблокированы.</div>' if queue_reliability.maintenance_active(STORE) else ''}
  <main class="page">{content}</main>
  <footer>Единая очередь · рабочая система обработки заявок</footer>
  <script src="/static/ui.js?v=3.3.79" defer></script>
  <script src="/static/app.js?v=1.00.6.85" defer></script>
  <script src="/static/productivity.js?v=1.00.6.36" defer></script>
  <script src="/static/workflow.js?v=1.00.6.85" defer></script>
  <script src="/static/reliability.js?v=3.3.99" defer></script>
  <script src="/static/interface105.js?v=1.00.6.36" defer></script>
  <script src="/static/performance107.js?v=3.3.107" defer></script>
  <script src="/static/hotfix10068.js?v=1.00.6.10" defer></script>
  <script src="/static/auth100618.js?v=1.00.6.40" defer></script>
</body>
</html>"""


def instruction_text_html(value: object) -> str:
    """Escape instruction text and turn explicit web URLs into safe links."""
    raw = str(value or "")
    parts: list[str] = []
    cursor = 0
    for match in INSTRUCTION_LINK_RE.finditer(raw):
        start, end = match.span(1)
        parts.append(e(raw[cursor:start]))
        token = match.group(1)
        trailing = ""
        while token and token[-1] in ".,;:!?)]:":
            trailing = token[-1] + trailing
            token = token[:-1]
        href = token if token.lower().startswith(("http://", "https://")) else f"https://{token}"
        parts.append(
            f'<a class="instruction-link" href="{e(href)}" target="_blank" rel="noopener noreferrer">{e(token)}</a>'
        )
        parts.append(e(trailing))
        cursor = end
    parts.append(e(raw[cursor:]))
    return "".join(parts).replace("\n", "<br>")


def _instruction_extract_media(value: object) -> tuple[str, list[dict[str, str]]]:
    raw = str(value or "")
    images: list[dict[str, str]] = []
    lines: list[str] = []
    for line in raw.splitlines():
        item = str(line or "").strip()
        if not item:
            lines.append("")
            continue
        match = re.match(r'^!\[(?P<alt>[^\]]*)\]\((?P<src>(?:https?://|/)[^)]+)\)$', item, re.I)
        if not match:
            match = re.match(r'^\[(?:img|image)\s*:\s*(?P<src>(?:https?://|/)[^|\]]+)(?:\|(?P<alt>.*?))?\]$', item, re.I)
        if match:
            src = str(match.group('src') or '').strip()
            alt = str(match.groupdict().get('alt') or '').strip()
            if src and (src.startswith('/') or src.lower().startswith(('http://', 'https://'))):
                images.append({'src': src, 'alt': alt or 'Скриншот'})
                continue
        lines.append(line)
    while lines and not str(lines[-1]).strip():
        lines.pop()
    while lines and not str(lines[0]).strip():
        lines.pop(0)
    return "\n".join(lines), images


def instruction_item_body_html(value: object) -> str:
    text, images = _instruction_extract_media(value)
    body = instruction_text_html(text) if text else ''
    media_html = ''
    if images:
        cards = []
        for index, image in enumerate(images, start=1):
            src = str(image.get('src') or '').strip()
            alt = str(image.get('alt') or '').strip() or f'Изображение {index}'
            cards.append(
                f'<button class="instruction-media-card" type="button" data-instruction-media data-image-src="{e(src)}" data-image-caption="{e(alt)}" aria-label="Открыть изображение: {e(alt)}">'
                f'<span class="instruction-media-thumb"><img src="{e(src)}" alt="{e(alt)}" loading="lazy" decoding="async"></span>'
                f'<span class="instruction-media-meta"><strong>Фото</strong></span>'
                f'</button>'
            )
        media_html = f'<div class="instruction-media-grid">{"".join(cards)}</div>'
    if body and media_html:
        return body + media_html
    return body or media_html


def load_instruction_data(force: bool = False) -> dict[str, object]:
    queue_source_sync.schedule_instruction_sync(INSTRUCTION_SOURCE_URL, INSTRUCTION_DATA_PATH, force=force)
    payload = queue_source_sync.instruction_snapshot(INSTRUCTION_SOURCE_URL, INSTRUCTION_DATA_PATH)
    if not isinstance(payload, dict):
        return {"title": "Инструкция", "snapshot_date": "", "sections": [], "_sync": {"state": "local"}}
    sections = payload.get("sections", [])
    if not isinstance(sections, list):
        sections = []
    cleaned_sections: list[dict[str, object]] = []
    for index, section in enumerate(sections[:50], start=1):
        if not isinstance(section, dict):
            continue
        title = str(section.get("title", "") or "").strip()[:160]
        section_id = re.sub(r"[^a-z0-9а-яё_-]+", "-", str(section.get("id", "") or "").casefold()).strip("-")
        if not section_id:
            section_id = f"section-{index}"
        items = section.get("items", [])
        if not isinstance(items, list):
            items = []
        cleaned_items = [str(item or "").strip()[:14000] for item in items if str(item or "").strip()]
        if title and cleaned_items:
            cleaned_sections.append({"id": section_id, "title": title, "items": cleaned_items[:150]})
    sync = payload.get("_sync", {}) if isinstance(payload.get("_sync"), dict) else {}
    return {
        "title": str(payload.get("title", "Инструкция") or "Инструкция")[:160],
        "source_name": str(payload.get("source_name", "") or "")[:200],
        "source_url": str(payload.get("source_url", INSTRUCTION_SOURCE_URL) or INSTRUCTION_SOURCE_URL)[:1000],
        "snapshot_date": str(payload.get("snapshot_date", "") or "")[:40],
        "sections": cleaned_sections,
        "_sync": sync,
    }



def load_transit_instruction_data() -> dict[str, object]:
    path = ROOT / "transit_instruction_data.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"title": "Перевозки ИС Транзит", "records": [], "point_changes": []}
    if not isinstance(payload, dict):
        return {"title": "Перевозки ИС Транзит", "records": [], "point_changes": []}
    point_changes = payload.get("point_changes", [])
    if not isinstance(point_changes, list):
        point_changes = []
    payload["point_changes"] = point_changes[:20]
    return payload


def _transit_record_search(record: dict[str, object]) -> str:
    fields = [
        "number", "employee", "attached_date", "attached_date_display", "removed_date", "removed_date_display",
        "vehicle_type", "plate", "seal", "reason", "status", "status_raw", "note", "transport_status", "location",
        "created_by", "updated_by",
    ]
    return normalize_message(" ".join(str(record.get(field, "") or "") for field in fields)).casefold()


def _transit_status_options(current: str) -> str:
    current = str(current or "").strip()
    choices = list(queue_transit.STATUS_CHOICES)
    if current and current not in choices:
        choices.append(current)
    values = [""] + choices
    return "".join(
        f'<option value="{e(value)}" {"selected" if value == current else ""}>{e(value or "Без статуса")}</option>'
        for value in values
    )


def _transit_form(record: dict[str, object] | None, *, mode: str, actor_default: str = "") -> str:
    item = record or {}
    is_edit = mode == "update"
    record_id = int(item.get("id", 0) or 0)
    employee = str(item.get("employee", "") or "").strip() or actor_default
    heading = f'Редактирование записи №{e(item.get("number") or record_id)}' if is_edit else 'Добавить новую запись НП'
    submit = 'Сохранить изменения' if is_edit else 'Добавить запись'
    source_managed = is_edit and int(item.get("source_row", 0) or 0) > 0
    source_warning = (
        '<div class="transit-source-warning"><strong>Запись связана с исходной таблицей.</strong>'
        '<span>Локальные правки сохранятся сразу, но при следующей синхронизации исходная строка снова станет приоритетной.</span></div>'
        if source_managed else ''
    )
    hidden = (
        f'<input type="hidden" name="record_id" value="{record_id}">'
        f'<input type="hidden" name="expected_updated_at" value="{e(item.get("updated_at", ""))}">'
        if is_edit else ""
    )
    return f'''
      <form class="transit-edit-form" method="post" action="/transit/record" autocomplete="off">
        <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
        <input type="hidden" name="action" value="{e(mode)}">
        {hidden}
        <div class="transit-editor-title"><div><span>{'Правка' if is_edit else 'Новая запись'}</span><h3>{heading}</h3></div>{f'<a class="button ghost compact" href="/instruction#transit">Отмена</a>' if is_edit else ''}</div>
        {source_warning}
        <div class="transit-form-grid">
          <label>Сотрудник<input name="employee" maxlength="120" list="transit-employees" value="{e(employee)}" placeholder="Кто работал с НП"></label>
          <label>Дата установки<input type="date" name="attached_date" value="{e(queue_transit.input_date(item.get('attached_date', '')))}"></label>
          <label>Дата снятия<input type="date" name="removed_date" value="{e(queue_transit.input_date(item.get('removed_date', '')))}"></label>
          <label>Тип ТС<input name="vehicle_type" maxlength="80" value="{e(item.get('vehicle_type', ''))}" placeholder="Авто / тягач / прицеп"></label>
          <label>Количество ТС<input name="vehicle_count" maxlength="20" value="{e(item.get('vehicle_count', ''))}" inputmode="numeric"></label>
          <label class="wide">ГРНЗ / № перевозки<input name="plate" maxlength="120" value="{e(item.get('plate', ''))}" placeholder="Например Т418КВ39 / 3ТА7650"></label>
          <label>Количество НП<input name="seal_count" maxlength="20" value="{e(item.get('seal_count', ''))}" inputmode="numeric"></label>
          <label>Номер НП<input class="transit-mono" name="seal" maxlength="120" value="{e(item.get('seal', ''))}" placeholder="Номер навигационной пломбы"></label>
          <label class="wide">Причина<textarea name="reason" maxlength="600" rows="2" placeholder="Причина снятия / среза">{e(item.get('reason', ''))}</textarea></label>
          <label>Статус НП<select name="status">{_transit_status_options(str(item.get('status', '') or ''))}</select></label>
          <label>Статус перевозки<input name="transport_status" maxlength="300" value="{e(item.get('transport_status', ''))}" placeholder="Если применимо"></label>
          <label class="wide">Место снятия НП<input name="location" maxlength="500" value="{e(item.get('location', ''))}" placeholder="Пост / пункт / адрес"></label>
          <label class="wide">Примечание<textarea name="note" maxlength="1600" rows="3" placeholder="Согласование, результат, важные детали">{e(item.get('note', ''))}</textarea></label>
        </div>
        <p class="transit-form-hint">Для сохранения достаточно указать номер НП или ГРНЗ / номер перевозки. Изменения сохраняются в основной SQLite-базе вместе с остальными данными системы.</p>
        <button class="button primary transit-save" type="submit">{submit}</button>
      </form>
    '''


def render_transit_instruction_panel(
    data: dict[str, object],
    show_admin: bool = False,
    query: dict[str, list[str]] | None = None,
) -> str:
    query = query or {}
    sync_state: dict[str, object] = {}
    # Рабочий журнал всегда открывается с последних записей.
    # Номер записи является общей последовательностью для исходных и локальных строк.
    def _transit_newest_key(row: object) -> tuple[int, int, int]:
        item = row if isinstance(row, dict) else {}
        raw_number = str(item.get("number", "") or "").strip()
        number = int(raw_number) if raw_number.isdigit() else 0
        return (number, int(item.get("id", 0) or 0), int(item.get("source_row", 0) or 0))
    try:
        queue_transit.ensure_seeded(STORE, ROOT / "transit_instruction_data.json")
        queue_source_sync.schedule_transit_sync(STORE)
        sync_state = queue_source_sync.transit_sync_state()
        records = queue_transit.list_records(STORE)
    except Exception as error:
        sync_state = {"state": "local", "message": "Используется локальный журнал НП", "error": str(error)[:180]}
        raw_records = data.get("records", []) if isinstance(data, dict) else []
        records = raw_records if isinstance(raw_records, list) else []
    records = sorted(records, key=_transit_newest_key, reverse=True)
    fallback_points = data.get("point_changes", []) if isinstance(data, dict) else []
    if not isinstance(fallback_points, list):
        fallback_points = []

    notice = str(query.get("notice", [""])[0] or "").strip()
    notice_html = f'<div class="transit-notice">{e(notice)}</div>' if notice else ""
    current_user = queue_auth.current_user() or {}
    actor_default = str(employee_identity(current_user) or current_user.get("display_name") or current_user.get("username") or active_employee() or "").strip()

    edit_record = None
    try:
        edit_id = int(query.get("transit_edit", ["0"])[0] or 0)
    except (TypeError, ValueError):
        edit_id = 0
    if edit_id > 0:
        try:
            edit_record = queue_transit.get_record(STORE, edit_id)
        except Exception:
            edit_record = None

    status_counts: dict[str, int] = {}
    record_html: list[str] = []
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status", "") or "").strip() or "Без статуса"
        status_counts[status] = status_counts.get(status, 0) + 1
        search = _transit_record_search(raw)
        employee = str(raw.get("employee", "") or "").strip() or "Исполнитель не указан"
        removed_date = str(raw.get("removed_date_display", "") or queue_transit.display_date(raw.get("removed_date", ""))).strip() or "Дата не указана"
        attached_date = str(raw.get("attached_date_display", "") or queue_transit.display_date(raw.get("attached_date", ""))).strip()
        plate = str(raw.get("plate", "") or "").strip()
        seal = str(raw.get("seal", "") or "").strip()
        vehicle_type = str(raw.get("vehicle_type", "") or "").strip()
        vehicle_count = str(raw.get("vehicle_count", "") or "").strip()
        seal_count = str(raw.get("seal_count", "") or "").strip()
        location = str(raw.get("location", "") or "").strip()
        note = str(raw.get("note", "") or "").strip()
        reason = str(raw.get("reason", "") or "").strip()
        transport_status = str(raw.get("transport_status", "") or "").strip()
        number = str(raw.get("number", "") or "").strip() or str(index)
        record_id = int(raw.get("id", 0) or 0)
        source_managed = int(raw.get("source_row", 0) or 0) > 0
        source_badge = ""
        if show_admin:
            source_badge = '<span class="transit-origin source">Источник</span>' if source_managed else '<span class="transit-origin local">Локально</span>'
        raw_filter_date = str(raw.get("removed_date", "") or raw.get("attached_date", "") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_filter_date):
            fallback_stamp = str(raw.get("created_at", "") or raw.get("updated_at", "") or "").strip()
            raw_filter_date = fallback_stamp[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", fallback_stamp) else ""
        order_number = int(number) if number.isdigit() else max(record_id, int(raw.get("source_row", 0) or 0), index)
        status_key = re.sub(r"[^a-z0-9а-яё_-]+", "-", status.casefold()).strip("-") or "other"
        details = []
        if vehicle_type:
            details.append(f'<div><span>ТС</span><strong>{e(vehicle_type)}</strong></div>')
        if vehicle_count:
            details.append(f'<div><span>Кол-во ТС</span><strong>{e(vehicle_count)}</strong></div>')
        if plate:
            details.append(f'<div><span>ГРНЗ / № перевозки</span><strong>{e(plate)}</strong></div>')
        if seal:
            details.append(f'<div><span>НП</span><strong class="transit-mono">{e(seal)}</strong></div>')
        if seal_count:
            details.append(f'<div><span>Кол-во НП</span><strong>{e(seal_count)}</strong></div>')
        if attached_date:
            details.append(f'<div><span>Установка</span><strong>{e(attached_date)}</strong></div>')
        if reason:
            details.append(f'<div><span>Причина</span><strong>{e(reason)}</strong></div>')
        if transport_status:
            details.append(f'<div><span>Статус перевозки</span><strong>{e(transport_status)}</strong></div>')
        meta = ""
        if show_admin and str(raw.get("updated_by", "") or "").strip() and str(raw.get("updated_by", "")) != "Импорт Excel":
            meta = f'<div class="transit-card-meta">Изменил: {e(raw.get("updated_by"))}</div>'
        edit_link = f'<a class="button ghost compact transit-edit-link" href="/instruction?transit_edit={record_id}#transit">Редактировать</a>' if record_id else ""
        delete_form = ""
        if show_admin and record_id:
            delete_form = (
                f'<form class="transit-delete-form" method="post" action="/transit/record" '
                f'data-transit-delete-form data-record-number="{e(number)}">'
                f'<input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">'
                f'<input type="hidden" name="action" value="delete">'
                f'<input type="hidden" name="record_id" value="{record_id}">'
                f'<input type="hidden" name="confirm_delete" value="1">'
                f'<button class="button compact transit-delete-link" type="submit" title="Удалить запись">Удалить</button>'
                f'</form>'
            )
        action_buttons = f'<div class="transit-card-buttons">{edit_link}{delete_form}</div>' if (edit_link or delete_form) else ""
        record_html.append(
            f'<article class="transit-card" data-transit-record data-transit-status="{e(status_key)}" '
            f'data-search="{e(search)}" data-transit-index="{index}" data-transit-date="{e(raw_filter_date)}" data-transit-order="{order_number}">'
            f'<div class="transit-card-head"><div><small><span class="transit-record-number">№{e(number)}</span>{source_badge}</small><strong>{e(employee)}</strong>'
            f'<span class="transit-card-date">{e(removed_date)}</span></div><div class="transit-card-actions"><span class="transit-status" data-status="{e(status_key)}">{e(status)}</span>{action_buttons}</div></div>'
            f'<div class="transit-card-grid">{"".join(details)}</div>'
            f'{f"<div class=\"transit-location\"><span>Место снятия НП</span><p>{e(location)}</p></div>" if location else ""}'
            f'{f"<div class=\"transit-note\"><span>Примечание</span><p>{e(note)}</p></div>" if note else ""}'
            f'{meta}'
            f'</article>'
        )

    chips = ['<button class="transit-filter is-active" type="button" data-transit-filter="all">Все</button>']
    for status, count in sorted(status_counts.items(), key=lambda item: (-item[1], item[0])):
        key = re.sub(r"[^a-z0-9а-яё_-]+", "-", status.casefold()).strip("-") or "other"
        chips.append(f'<button class="transit-filter" type="button" data-transit-filter="{e(key)}">{e(status)} <b>{count}</b></button>')

    stats = queue_transit.summary(records) if records else {"record_count": 0, "date_from": "", "date_to": "", "location_count": 0}
    no_status_count = int(status_counts.get("Без статуса", 0) or 0)
    employee_options = "".join(f'<option value="{e(name)}"></option>' for name in EMPLOYEES)
    edit_html = f'<section class="transit-editor transit-editor-active">{_transit_form(edit_record, mode="update", actor_default=actor_default)}</section>' if edit_record else ""
    sync_mode = str(sync_state.get("state", "local") or "local")
    sync_label = {"live": "Источник актуален", "refreshing": "Обновляется", "cached": "Работа из кэша", "local": "Локальные данные"}.get(sync_mode, "Источник")
    sync_last = str(sync_state.get("last_sync", "") or "")
    sync_message = str(sync_state.get("message", "") or "")
    sync_error = str(sync_state.get("error", "") or "").strip()[:320]
    sync_detail_text = sync_message
    if sync_error and sync_mode not in {"live", "refreshing"}:
        sync_detail_text = f"{sync_message} · Ошибка: {sync_error}" if sync_message else f"Ошибка: {sync_error}"
    transit_source_url = queue_source_sync.transit_source_url()
    if show_admin:
        sync_detail = f'{e(sync_detail_text)}{f" · {e(sync_last)}" if sync_last else ""}'
        source_action = f'<a class="button ghost compact" href="{e(transit_source_url)}" target="_blank" rel="noopener noreferrer">Открыть источник ↗</a>'
    else:
        sync_detail = f'Последнее обновление: {e(sync_last)}' if sync_last else 'Данные обновляются автоматически'
        source_action = ""

    return f"""
      <section class="instruction-tab-panel transit-panel" data-instruction-tab-panel="transit" hidden>
        {notice_html}
        <datalist id="transit-employees">{employee_options}</datalist>
        <div class="transit-hero transit-workspace-hero">
          <div><span class="transit-eyebrow">Журнал НП и перевозок</span><h2>Перевозки</h2>
          <p>Добавляйте новые записи, быстро находите нужную перевозку и обновляйте данные после снятия НП.</p></div>
          <div class="transit-stats"><div><small>Всего записей</small><strong>{int(stats.get('record_count', 0) or 0)}</strong></div><div><small>Последняя дата</small><strong>{e(stats.get('date_to', '') or '—')}</strong></div><div><small>Без статуса</small><strong>{no_status_count}</strong></div></div>
        </div>

        <div class="instruction-livebar transit-livebar is-{e(sync_mode)}">
          <div class="instruction-live-status"><span class="instruction-live-dot"></span><div><strong>{'Обновляется' if sync_mode == 'refreshing' else 'Данные актуальны' if sync_mode == 'live' else e(sync_label)}</strong><small>{sync_detail}</small></div></div>
          <div class="instruction-live-actions">
            <form method="post" action="/instruction/sync"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="kind" value="transit"><button class="button ghost compact" type="submit">↻ Обновить</button></form>
            {source_action}
          </div>
        </div>

        <div class="transit-actions-bar transit-workspace-actions">
          <details class="transit-editor transit-new-editor">
            <summary class="button primary">＋ Добавить НП</summary>
            {_transit_form(None, mode="create", actor_default=actor_default)}
          </details>
          <a class="button ghost" href="/transit/export.csv">Скачать Excel</a>
          {('<span class="transit-admin-mode">Администратор · доступно удаление</span>' if show_admin else '')}
          <span class="transit-actions-note">Новые записи сверху</span>
        </div>
        {edit_html}

        <section class="transit-browser transit-workspace-browser">
          <div class="transit-browser-head"><div><span>Рабочий журнал</span><h3>Последние записи</h3><p class="transit-browser-subtitle">По умолчанию новые записи находятся сверху</p></div><strong data-transit-visible-count>{len(record_html)}</strong></div>
          <div class="transit-browser-controls">
          <div class="transit-search"><span>⌕</span><input type="search" placeholder="НП, ГРНЗ, сотрудник, место или статус" data-transit-search autocomplete="off"><button type="button" data-transit-clear hidden aria-label="Очистить поиск">×</button></div>
          <div class="transit-filter-panel">
            <div class="transit-filter-panel-head"><div><strong>Фильтры</strong><span>Можно сочетать период, статус и поиск</span></div><button class="button ghost compact" type="button" data-transit-reset-all hidden>Сбросить всё</button></div>
            <div class="transit-filter-row">
              <span class="transit-filter-label">Период</span>
              <div class="transit-time-filters">
                <button class="transit-filter is-active" type="button" data-transit-period="all">Все время</button>
                <button class="transit-filter" type="button" data-transit-period="today">Сегодня</button>
                <button class="transit-filter" type="button" data-transit-period="7">7 дней</button>
                <button class="transit-filter" type="button" data-transit-period="30">30 дней</button>
                <button class="transit-filter" type="button" data-transit-period="90">90 дней</button>
              </div>
              <div class="transit-date-range">
                <label><span>С</span><input type="date" data-transit-from></label>
                <label><span>По</span><input type="date" data-transit-to></label>
                <button class="button ghost compact" type="button" data-transit-date-reset hidden>Сбросить даты</button>
              </div>
            </div>
            <div class="transit-filter-row">
              <span class="transit-filter-label">Статус</span>
              <div class="transit-filters">{''.join(chips)}</div>
            </div>
          </div>
          </div>
          <div class="transit-records" data-transit-records>{''.join(record_html)}</div>
          <div class="transit-no-results" data-transit-empty hidden>По этому запросу записей нет</div>
          <div class="transit-pagination"><button class="button ghost" type="button" data-transit-prev>← Назад</button><span data-transit-page></span><button class="button ghost" type="button" data-transit-next>Вперёд →</button></div>
        </section>
      </section>
    """

def render_instruction(show_admin: bool = False, query: dict[str, list[str]] | None = None) -> str:
    query = query or {}
    data = load_instruction_data()
    transit_data = load_transit_instruction_data()
    transit_panel = render_transit_instruction_panel(transit_data, show_admin, query)
    sections = data.get("sections", []) if isinstance(data, dict) else []
    section_html: list[str] = []
    doc_svg = """
      <svg viewBox="0 0 320 180" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Подготовка документов и ТД">
        <defs>
          <linearGradient id="tdg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#dbeafe"/><stop offset="100%" stop-color="#ecfeff"/></linearGradient>
        </defs>
        <rect x="8" y="8" width="304" height="164" rx="22" fill="url(#tdg)"/>
        <rect x="30" y="34" width="92" height="112" rx="14" fill="#ffffff" stroke="#93c5fd" stroke-width="2"/>
        <line x1="48" y1="62" x2="104" y2="62" stroke="#94a3b8" stroke-width="6" stroke-linecap="round"/>
        <line x1="48" y1="84" x2="104" y2="84" stroke="#cbd5e1" stroke-width="6" stroke-linecap="round"/>
        <line x1="48" y1="106" x2="90" y2="106" stroke="#cbd5e1" stroke-width="6" stroke-linecap="round"/>
        <rect x="154" y="44" width="136" height="90" rx="16" fill="#0f172a"/>
        <rect x="168" y="58" width="108" height="14" rx="7" fill="#38bdf8"/>
        <rect x="168" y="82" width="82" height="12" rx="6" fill="#ffffff" fill-opacity="0.88"/>
        <rect x="168" y="104" width="56" height="12" rx="6" fill="#ffffff" fill-opacity="0.65"/>
        <path d="M122 92H152" stroke="#1d4ed8" stroke-width="7" stroke-linecap="round"/>
        <path d="M145 83L154 92L145 101" fill="none" stroke="#1d4ed8" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="32" y="28" fill="#1e3a8a" font-size="14" font-family="Arial" font-weight="700">Документы</text>
        <text x="156" y="30" fill="#0f172a" font-size="14" font-family="Arial" font-weight="700">ТД в Кеден</text>
      </svg>
    """
    search_svg = """
      <svg viewBox="0 0 320 180" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Поиск перевозки по ГРНЗ">
        <defs>
          <linearGradient id="trg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#e0f2fe"/><stop offset="100%" stop-color="#f0fdf4"/></linearGradient>
        </defs>
        <rect x="8" y="8" width="304" height="164" rx="22" fill="url(#trg)"/>
        <rect x="28" y="30" width="264" height="122" rx="18" fill="#ffffff" stroke="#bae6fd" stroke-width="2"/>
        <rect x="46" y="48" width="164" height="26" rx="13" fill="#eff6ff" stroke="#93c5fd"/>
        <text x="58" y="66" fill="#1e40af" font-size="13" font-family="Arial" font-weight="700">ГРНЗ: 123 ABC 02</text>
        <circle cx="246" cy="61" r="16" fill="#0ea5e9"/>
        <circle cx="242" cy="57" r="7" fill="none" stroke="#ffffff" stroke-width="3"/>
        <path d="M247 62l7 7" stroke="#ffffff" stroke-width="3" stroke-linecap="round"/>
        <rect x="46" y="90" width="222" height="16" rx="8" fill="#dbeafe"/>
        <rect x="46" y="116" width="168" height="14" rx="7" fill="#e2e8f0"/>
        <rect x="226" y="112" width="42" height="22" rx="11" fill="#22c55e"/>
        <text x="20" y="24" fill="#0369a1" font-size="14" font-family="Arial" font-weight="700">Поиск нужной перевозки</text>
      </svg>
    """
    seal_svg = """
      <svg viewBox="0 0 320 180" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Добавление НП к перевозке">
        <defs>
          <linearGradient id="npg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#ede9fe"/><stop offset="100%" stop-color="#fee2e2"/></linearGradient>
        </defs>
        <rect x="8" y="8" width="304" height="164" rx="22" fill="url(#npg)"/>
        <rect x="30" y="38" width="170" height="100" rx="18" fill="#ffffff" stroke="#c4b5fd" stroke-width="2"/>
        <rect x="48" y="56" width="134" height="18" rx="9" fill="#ede9fe"/>
        <rect x="48" y="84" width="102" height="14" rx="7" fill="#e2e8f0"/>
        <rect x="48" y="108" width="92" height="14" rx="7" fill="#e2e8f0"/>
        <rect x="216" y="52" width="62" height="84" rx="16" fill="#1f2937"/>
        <rect x="230" y="68" width="34" height="26" rx="8" fill="#22c55e"/>
        <circle cx="247" cy="110" r="9" fill="#fbbf24"/>
        <path d="M200 92H216" stroke="#7c3aed" stroke-width="6" stroke-linecap="round"/>
        <path d="M209 84L217 92L209 100" fill="none" stroke="#7c3aed" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="28" y="28" fill="#7c3aed" font-size="14" font-family="Arial" font-weight="700">Привязка навигационной пломбы</text>
      </svg>
    """
    finish_svg = """
      <svg viewBox="0 0 320 180" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Мониторинг и завершение">
        <defs>
          <linearGradient id="fing" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#dcfce7"/><stop offset="100%" stop-color="#dbeafe"/></linearGradient>
        </defs>
        <rect x="8" y="8" width="304" height="164" rx="22" fill="url(#fing)"/>
        <rect x="34" y="112" width="252" height="18" rx="9" fill="#94a3b8" opacity="0.5"/>
        <rect x="52" y="78" width="106" height="36" rx="14" fill="#0f172a"/>
        <circle cx="80" cy="118" r="12" fill="#0f172a"/>
        <circle cx="132" cy="118" r="12" fill="#0f172a"/>
        <path d="M186 50l18 18 34-34" fill="none" stroke="#16a34a" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>
        <rect x="212" y="84" width="58" height="30" rx="15" fill="#22c55e"/>
        <text x="30" y="30" fill="#166534" font-size="14" font-family="Arial" font-weight="700">Мониторинг и завершение перевозки</text>
        <text x="220" y="103" fill="#ffffff" font-size="13" font-family="Arial" font-weight="700">OK</text>
      </svg>
    """
    illustration_cards = [
        {"title": "Подготовка документов и ТД", "text": "Сначала проверяются документы и только потом создаётся и регистрируется ТД в Кеден.", "svg": doc_svg},
        {"title": "Поиск нужной перевозки", "text": "Менеджер в ИС Транзит работает через поиск по ГРНЗ и выбирает правильную перевозку.", "svg": search_svg},
        {"title": "Добавление НП", "text": "После выбора перевозки в карточке указывается номер НП и выполняется привязка пломбы к перевозке.", "svg": seal_svg},
        {"title": "Мониторинг и завершение", "text": "После активации начинается контроль движения, а в конце выполняется деактивация и завершение перевозки.", "svg": finish_svg},
    ]
    illustrations_html = "".join(
        f'<article class="tp-illustration-card"><div class="tp-illustration-art">{card["svg"]}</div><div class="tp-illustration-body"><h3>{e(card["title"])}</h3><p>{e(card["text"])}</p></div></article>'
        for card in illustration_cards
    )

    nav_html: list[str] = []
    total_items = 0
    total_sections = 0

    for section_index, section in enumerate(sections if isinstance(sections, list) else [], start=1):
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id", "") or "")
        title = str(section.get("title", "") or "")
        items = section.get("items", [])
        if not isinstance(items, list):
            items = []

        item_html: list[str] = []
        for item_index, item in enumerate(items, start=1):
            text = str(item or "").strip()
            if not text:
                continue
            total_items += 1
            search_text = normalize_message(text).casefold()
            item_html.append(
                f'<article class="instruction-item" data-instruction-item data-search="{e(search_text)}" '
                f'>'
                f'<div class="instruction-item-index">{item_index:02d}</div>'
                f'<div class="instruction-item-text">{instruction_item_body_html(text)}</div>'
                f'<button class="instruction-copy" type="button" data-copy-instruction aria-label="Скопировать пункт" title="Скопировать">'
                f'<span aria-hidden="true">⧉</span></button>'
                f'</article>'
            )
        if not item_html:
            continue

        total_sections += 1
        nav_html.append(
            f'<a class="instruction-chip" data-instruction-chip="{e(section_id)}" '
            f'href="#instruction-{e(section_id)}"><span>{section_index:02d}</span>{e(title)}</a>'
        )
        section_search = normalize_message(f"{title} {' '.join(str(item or '') for item in items)}").casefold()
        section_html.append(
            f'<section class="instruction-section" id="instruction-{e(section_id)}" '
            f'data-instruction-section data-section-id="{e(section_id)}" data-search="{e(section_search)}" '
            f'>'
            f'<button class="instruction-section-head" type="button" data-instruction-toggle aria-expanded="true">'
            f'<span class="instruction-section-number">{section_index:02d}</span>'
            f'<span class="instruction-section-copy"><span class="instruction-kicker">Раздел {section_index}</span><strong>{e(title)}</strong></span>'
            f'<span class="instruction-section-count">{len(item_html)} пунктов</span>'
            f'<span class="instruction-chevron" aria-hidden="true">⌄</span>'
            f'</button>'
            f'<div class="instruction-section-body"><div class="instruction-section-body-inner">'
            f'<div class="instruction-items">{"".join(item_html)}</div>'
            f'</div></div></section>'
        )

    snapshot = str(data.get("snapshot_date", "") or "") if isinstance(data, dict) else ""
    source_url = str(data.get("source_url", INSTRUCTION_SOURCE_URL) or INSTRUCTION_SOURCE_URL) if isinstance(data, dict) else INSTRUCTION_SOURCE_URL
    if not source_url.startswith(("http://", "https://")):
        source_url = INSTRUCTION_SOURCE_URL

    sync = data.get("_sync", {}) if isinstance(data, dict) and isinstance(data.get("_sync"), dict) else {}
    sync_mode = str(sync.get("state", "local") or "local")
    sync_label = {"live": "Актуально", "refreshing": "Обновляется", "cached": "Последний кэш", "local": "Локальная копия"}.get(sync_mode, "Источник")
    sync_last = str(sync.get("last_sync", "") or snapshot or "")
    sync_message = str(sync.get("message", "") or "")
    sync_error = str(sync.get("error", "") or "").strip()[:320]
    sync_detail_text = sync_message
    if sync_error and sync_mode not in {"live", "refreshing"}:
        sync_detail_text = f"{sync_message} · Ошибка: {sync_error}" if sync_message else f"Ошибка: {sync_error}"
    snapshot_card = f'<div class="instruction-stat"><span>{e(sync_label)}</span><strong>{e(sync_last or "—")}</strong></div>'
    empty = '' if section_html else '<section class="panel empty"><h2>Инструкция пока не загружена</h2><p>Проверьте доступ к исходной таблице или локальный кэш.</p></section>'
    general_notice = str(query.get("notice", [""])[0] or "").strip()
    general_notice_html = f'<div class="transit-notice instruction-general-notice">{e(general_notice)}</div>' if general_notice else ""
    content = f"""
      <section class="instruction-hero">
        <div class="instruction-orb instruction-orb-a" aria-hidden="true"></div>
        <div class="instruction-orb instruction-orb-b" aria-hidden="true"></div>
        <div class="instruction-hero-grid" aria-hidden="true"></div>
        <div class="instruction-hero-content">
          <div class="instruction-badge"><span class="instruction-badge-dot"></span>Внутренняя база знаний</div>
          <h1>Инструкция</h1>
          <p>Актуальная рабочая база знаний из исходной таблицы. Изменения в источнике автоматически появляются здесь после синхронизации.</p>
          <div class="instruction-hero-actions">
            <button class="button instruction-focus-search" type="button" data-focus-instruction-search>Найти инструкцию</button>
            <a class="button ghost" href="{e(source_url)}" target="_blank" rel="noopener noreferrer">Исходная таблица ↗</a>
          </div>
        </div>
        <div class="instruction-stats" aria-label="Статистика инструкции">
          <div class="instruction-stat"><span>Разделов</span><strong>{total_sections}</strong></div>
          <div class="instruction-stat"><span>Пунктов</span><strong data-instruction-count>{total_items}</strong></div>
          {snapshot_card}
        </div>
      </section>

      <div class="instruction-subtabs" role="tablist" aria-label="Разделы базы знаний">
        <button class="instruction-subtab is-active" type="button" role="tab" aria-selected="true" data-instruction-tab="general"><span>01</span> Общая инструкция</button>
        <button class="instruction-subtab" type="button" role="tab" aria-selected="false" data-instruction-tab="transit"><span>02</span> Перевозки ИС Транзит</button>
      </div>

      <section class="instruction-tab-panel" data-instruction-tab-panel="general">
      {general_notice_html}
      <div class="instruction-livebar is-{e(sync_mode)}">
        <div class="instruction-live-status"><span class="instruction-live-dot"></span><div><strong>{e(sync_label)}</strong><small>{e(sync_detail_text)}{f" · {e(sync_last)}" if sync_last else ""}</small></div></div>
        <div class="instruction-live-actions">
          <form method="post" action="/instruction/sync"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="kind" value="general"><button class="button ghost compact" type="submit">↻ Обновить сейчас</button></form>
          <a class="button ghost compact" href="{e(source_url)}" target="_blank" rel="noopener noreferrer">Открыть исходник ↗</a>
        </div>
      </div>
      <div class="instruction-media-hint"><strong>Скриншоты и фото:</strong> изображения, вставленные в исходную Google-таблицу через «Вставка → Изображение», теперь подхватываются автоматически при синхронизации. Ссылочный формат <code>![Подпись](https://...)</code> тоже поддерживается.</div>
      <section class="instruction-toolbar" data-instruction-toolbar>
        <div class="instruction-search-wrap">
          <span class="instruction-search-icon" aria-hidden="true">⌕</span>
          <input id="instruction-search" type="search" placeholder="Поиск по инструкции..." autocomplete="off" data-instruction-search aria-label="Поиск по инструкции">
          <kbd class="instruction-search-key">/</kbd>
          <button class="instruction-search-clear" type="button" data-instruction-clear hidden aria-label="Очистить поиск">×</button>
        </div>
        <div class="instruction-toolbar-actions">
          <button class="instruction-mini-button" type="button" data-expand-all>Развернуть всё</button>
          <button class="instruction-mini-button" type="button" data-collapse-all>Свернуть всё</button>
        </div>
      </section>

      <nav class="instruction-chips" aria-label="Разделы инструкции">{"".join(nav_html)}</nav>
      <div class="instruction-search-status" data-instruction-search-status aria-live="polite"></div>
      <div class="instruction-list" data-instruction-list>{"".join(section_html)}{empty}</div>
      <div class="instruction-no-results" data-instruction-empty hidden>
        <div class="instruction-no-results-icon">⌕</div>
        <h2>Ничего не найдено</h2>
        <p>Попробуйте изменить запрос или очистить поиск.</p>
        <button class="button ghost" type="button" data-instruction-empty-clear>Очистить поиск</button>
      </div>
      <div class="instruction-toast" data-instruction-toast role="status" aria-live="polite">Пункт скопирован</div>
      <div class="instruction-lightbox" data-instruction-lightbox hidden>
        <button class="instruction-lightbox-close" type="button" data-lightbox-close aria-label="Закрыть">×</button>
        <div class="instruction-lightbox-dialog" role="dialog" aria-modal="true" aria-label="Просмотр изображения">
          <div class="instruction-lightbox-stage"><img src="" alt="" data-lightbox-image></div>
          <div class="instruction-lightbox-footer">
            <div class="instruction-lightbox-caption" data-lightbox-caption>Изображение</div>
            <div class="instruction-lightbox-actions">
              <button class="instruction-lightbox-action" type="button" data-lightbox-zoom-out aria-label="Уменьшить">−</button>
              <button class="instruction-lightbox-action" type="button" data-lightbox-zoom-reset>100%</button>
              <button class="instruction-lightbox-action" type="button" data-lightbox-zoom-in aria-label="Увеличить">+</button>
            </div>
          </div>
        </div>
      </div>
      </section>
      {transit_panel}

      <script>
        (() => {{
          const input = document.querySelector('[data-instruction-search]');
          const sections = [...document.querySelectorAll('[data-instruction-section]')];
          const chips = [...document.querySelectorAll('[data-instruction-chip]')];
          const counter = document.querySelector('[data-instruction-count]');
          const empty = document.querySelector('[data-instruction-empty]');
          const clearButton = document.querySelector('[data-instruction-clear]');
          const emptyClear = document.querySelector('[data-instruction-empty-clear]');
          const searchWrap = input?.closest('.instruction-search-wrap');
          const status = document.querySelector('[data-instruction-search-status]');
          const toast = document.querySelector('[data-instruction-toast]');
          if (!input) return;

          const normalize = (value) => String(value || '').toLocaleLowerCase('ru-RU').replace(/ё/g,'е').trim();
          let toastTimer = null;

          const setCollapsed = (section, collapsed) => {{
            section.classList.toggle('is-collapsed', collapsed);
            const button = section.querySelector('[data-instruction-toggle]');
            if (button) button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
          }};

          document.querySelectorAll('[data-instruction-toggle]').forEach((button) => {{
            button.addEventListener('click', () => {{
              const section = button.closest('[data-instruction-section]');
              if (section) setCollapsed(section, !section.classList.contains('is-collapsed'));
            }});
          }});

          document.querySelector('[data-expand-all]')?.addEventListener('click', () => sections.forEach((section) => setCollapsed(section, false)));
          document.querySelector('[data-collapse-all]')?.addEventListener('click', () => sections.forEach((section) => setCollapsed(section, true)));
          document.querySelector('[data-focus-instruction-search]')?.addEventListener('click', () => {{
            input.focus({{preventScroll:true}});
            document.querySelector('[data-instruction-toolbar]')?.scrollIntoView({{behavior:'smooth',block:'start'}});
          }});

          const clearSearch = () => {{
            input.value = '';
            input.dispatchEvent(new Event('input'));
            input.focus();
          }};
          clearButton?.addEventListener('click', clearSearch);
          emptyClear?.addEventListener('click', clearSearch);

          document.addEventListener('keydown', (event) => {{
            const target = event.target;
            const typing = target && (target.matches?.('input,textarea,select') || target.isContentEditable);
            if (event.key === '/' && !typing) {{
              event.preventDefault();
              input.focus();
            }}
            if (event.key === 'Escape' && !lightbox?.hidden) {{
              event.preventDefault();
              closeLightbox();
              return;
            }}
            if (event.key === 'Escape' && document.activeElement === input && input.value) clearSearch();
            if (!lightbox?.hidden && (event.key === '+' || event.key === '=')) {{ event.preventDefault(); setLightboxZoom(zoomIndex + 1); }}
            if (!lightbox?.hidden && event.key === '-') {{ event.preventDefault(); setLightboxZoom(zoomIndex - 1); }}
          }});

          // QUEUE_1_00_5_REPLY_LIGHT_INSTRUCTION: cache search text once and
          // debounce DOM writes. The previous version re-read item.textContent and
          // mutated every instruction row on every keystroke, which caused visible
          // stalls on large knowledge bases.
          const searchIndex = sections.map((section) => ({{
            section,
            items: [...section.querySelectorAll('[data-instruction-item]')].map((item) => ({{
              item, haystack: normalize(item.dataset.search || '')
            }})),
          }}));
          let searchTimer = 0;
          let searchGeneration = 0;
          const apply = () => {{
            const query = normalize(input.value);
            const terms = query.split(/\\s+/).filter(Boolean);
            let visibleItems = 0;
            let visibleSections = 0;
            document.documentElement.classList.add('instruction-filtering');
            for (const group of searchIndex) {{
              let sectionCount = 0;
              for (const entry of group.items) {{
                const show = !terms.length || terms.every((term) => entry.haystack.includes(term));
                if (entry.item.hidden === show) entry.item.hidden = !show;
                const matched = show && terms.length > 0;
                if (entry.item.classList.contains('is-search-match') !== matched) entry.item.classList.toggle('is-search-match', matched);
                if (show) sectionCount += 1;
              }}
              const hideSection = sectionCount === 0;
              if (group.section.hidden !== hideSection) group.section.hidden = hideSection;
              if (sectionCount) {{
                visibleSections += 1;
                if (terms.length && group.section.classList.contains('is-collapsed')) setCollapsed(group.section, false);
              }}
              visibleItems += sectionCount;
            }}
            if (counter && counter.textContent !== String(visibleItems)) counter.textContent = String(visibleItems);
            if (empty && empty.hidden !== (visibleSections !== 0)) empty.hidden = visibleSections !== 0;
            if (clearButton && clearButton.hidden !== !query) clearButton.hidden = !query;
            searchWrap?.classList.toggle('has-value', Boolean(query));
            if (status) {{
              const next = query ? `Найдено: ${{visibleItems}} пунктов в ${{visibleSections}} разделах` : '';
              if (status.textContent !== next) status.textContent = next;
            }}
            requestAnimationFrame(() => document.documentElement.classList.remove('instruction-filtering'));
          }};
          const scheduleApply = () => {{
            const generation = ++searchGeneration;
            window.clearTimeout(searchTimer);
            const delay = normalize(input.value) ? 120 : 0;
            searchTimer = window.setTimeout(() => {{
              if (generation !== searchGeneration) return;
              requestAnimationFrame(apply);
            }}, delay);
          }};
          input.addEventListener('input', scheduleApply, {{passive:true}});

          chips.forEach((chip) => {{
            chip.addEventListener('click', () => {{
              const id = chip.dataset.instructionChip;
              const section = sections.find((node) => node.dataset.sectionId === id);
              if (!section) return;
              setCollapsed(section, false);
              section.classList.add('is-target');
              window.setTimeout(() => section.classList.remove('is-target'), 1100);
            }});
          }});

          if ('IntersectionObserver' in window) {{
            const observer = new IntersectionObserver((entries) => {{
              const visible = entries.filter((entry) => entry.isIntersecting).sort((a,b) => b.intersectionRatio - a.intersectionRatio)[0];
              if (!visible) return;
              const id = visible.target.dataset.sectionId;
              chips.forEach((chip) => chip.classList.toggle('is-active', chip.dataset.instructionChip === id));
            }}, {{rootMargin:'-22% 0px -58% 0px',threshold:[0,.15,.35]}});
            sections.forEach((section) => observer.observe(section));
          }}

          document.querySelectorAll('[data-copy-instruction]').forEach((button) => {{
            button.addEventListener('click', async () => {{
              const item = button.closest('[data-instruction-item]');
              const text = item?.querySelector('.instruction-item-text')?.innerText?.trim() || '';
              if (!text) return;
              try {{
                await navigator.clipboard.writeText(text);
                if (toast) {{
                  toast.classList.add('is-visible');
                  window.clearTimeout(toastTimer);
                  toastTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 1400);
                }}
              }} catch (_) {{}}
            }});
          }});

          const lightbox = document.querySelector('[data-instruction-lightbox]');
          const lightboxImage = lightbox?.querySelector('[data-lightbox-image]');
          const lightboxCaption = lightbox?.querySelector('[data-lightbox-caption]');
          const lightboxReset = lightbox?.querySelector('[data-lightbox-zoom-reset]');
          const zoomLevels = [50, 75, 100, 125, 150, 175, 200];
          let zoomIndex = 2;
          const applyLightboxZoom = () => {{
            if (!lightbox) return;
            zoomLevels.forEach((level) => lightbox.classList.remove(`zoom-${{level}}`));
            const level = zoomLevels[zoomIndex] || 100;
            lightbox.classList.add(`zoom-${{level}}`);
            if (lightboxReset) lightboxReset.textContent = `${{level}}%`;
          }};
          const setLightboxZoom = (nextIndex) => {{
            zoomIndex = Math.max(0, Math.min(zoomLevels.length - 1, Number(nextIndex) || 0));
            applyLightboxZoom();
          }};
          const closeLightbox = () => {{
            if (!lightbox) return;
            lightbox.hidden = true;
            document.documentElement.classList.remove('instruction-lightbox-open');
            zoomLevels.forEach((level) => lightbox.classList.remove(`zoom-${{level}}`));
            if (lightboxImage) {{
              lightboxImage.removeAttribute('src');
              lightboxImage.removeAttribute('alt');
            }}
            zoomIndex = 2;
          }};
          document.querySelectorAll('[data-instruction-media]').forEach((button) => {{
            button.addEventListener('click', () => {{
              if (!lightbox || !lightboxImage) return;
              const src = button.dataset.imageSrc || '';
              const caption = button.dataset.imageCaption || 'Фото';
              if (!src) return;
              lightbox.hidden = false;
              document.documentElement.classList.add('instruction-lightbox-open');
              lightboxImage.src = src;
              lightboxImage.alt = caption;
              if (lightboxCaption) lightboxCaption.textContent = 'Фото';
              zoomIndex = 2;
              applyLightboxZoom();
            }});
          }});
          lightbox?.addEventListener('click', (event) => {{
            if (event.target === lightbox) closeLightbox();
          }});
          lightbox?.querySelector('[data-lightbox-close]')?.addEventListener('click', closeLightbox);
          lightbox?.querySelector('[data-lightbox-zoom-in]')?.addEventListener('click', () => setLightboxZoom(zoomIndex + 1));
          lightbox?.querySelector('[data-lightbox-zoom-out]')?.addEventListener('click', () => setLightboxZoom(zoomIndex - 1));
          lightboxReset?.addEventListener('click', () => setLightboxZoom(2));

          const instructionTabs = [...document.querySelectorAll('[data-instruction-tab]')];
          const instructionPanels = [...document.querySelectorAll('[data-instruction-tab-panel]')];
          const selectInstructionTab = (name) => {{
            instructionTabs.forEach((tab) => {{
              const active = tab.dataset.instructionTab === name;
              tab.classList.toggle('is-active', active);
              tab.setAttribute('aria-selected', active ? 'true' : 'false');
            }});
            instructionPanels.forEach((panel) => {{ panel.hidden = panel.dataset.instructionTabPanel !== name; }});
            try {{ history.replaceState(null, '', name === 'transit' ? '#transit' : '#instruction'); }} catch (_) {{}}
          }};
          instructionTabs.forEach((tab) => tab.addEventListener('click', () => selectInstructionTab(tab.dataset.instructionTab || 'general')));
          if (location.hash === '#transit') selectInstructionTab('transit');

          const transitSearch = document.querySelector('[data-transit-search]');
          const transitClear = document.querySelector('[data-transit-clear]');
          const transitRecords = document.querySelector('[data-transit-records]');
          const transitCards = [...document.querySelectorAll('[data-transit-record]')];
          const transitFilters = [...document.querySelectorAll('[data-transit-filter]')];
          const transitPeriods = [...document.querySelectorAll('[data-transit-period]')];
          const transitFrom = document.querySelector('[data-transit-from]');
          const transitTo = document.querySelector('[data-transit-to]');
          const transitDateReset = document.querySelector('[data-transit-date-reset]');
          const transitResetAll = document.querySelector('[data-transit-reset-all]');
          const transitCount = document.querySelector('[data-transit-visible-count]');
          const transitEmpty = document.querySelector('[data-transit-empty]');
          const transitPrev = document.querySelector('[data-transit-prev]');
          const transitNext = document.querySelector('[data-transit-next]');
          const transitPage = document.querySelector('[data-transit-page]');
          let transitFilter = 'all';
          let transitPeriod = 'all';
          let transitPageNo = 1;
          const transitPerPage = 24;

          transitCards.sort((left, right) => Number(right.dataset.transitOrder || 0) - Number(left.dataset.transitOrder || 0));
          if (transitRecords) transitCards.forEach((card) => transitRecords.appendChild(card));

          const dateValue = (value) => {{
            if (!/^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(value || '')) return null;
            const result = new Date(`${{value}}T00:00:00`);
            return Number.isNaN(result.getTime()) ? null : result;
          }};
          const startOfToday = () => {{ const now = new Date(); now.setHours(0, 0, 0, 0); return now; }};
          const periodOk = (card) => {{
            const cardDate = dateValue(card.dataset.transitDate || '');
            const fromDate = dateValue(transitFrom?.value || '');
            const toDate = dateValue(transitTo?.value || '');
            if (fromDate || toDate) {{
              if (!cardDate) return false;
              if (fromDate && cardDate < fromDate) return false;
              if (toDate) {{ const inclusiveTo = new Date(toDate); inclusiveTo.setHours(23, 59, 59, 999); if (cardDate > inclusiveTo) return false; }}
              return true;
            }}
            if (transitPeriod === 'all') return true;
            if (!cardDate) return false;
            const today = startOfToday();
            if (transitPeriod === 'today') return cardDate.getTime() === today.getTime();
            const days = Number(transitPeriod || 0);
            if (!days) return true;
            const threshold = new Date(today);
            threshold.setDate(threshold.getDate() - (days - 1));
            return cardDate >= threshold && cardDate <= today;
          }};
          const updateTransit = () => {{
            if (!transitSearch) return;
            const terms = normalize(transitSearch.value).split(/\\s+/).filter(Boolean);
            const matches = transitCards.filter((card) => {{
              const statusOk = transitFilter === 'all' || card.dataset.transitStatus === transitFilter;
              const haystack = normalize(card.dataset.search || '');
              return statusOk && periodOk(card) && (!terms.length || terms.every((term) => haystack.includes(term)));
            }});
            const pages = Math.max(1, Math.ceil(matches.length / transitPerPage));
            transitPageNo = Math.min(Math.max(1, transitPageNo), pages);
            const start = (transitPageNo - 1) * transitPerPage;
            const visible = new Set(matches.slice(start, start + transitPerPage));
            transitCards.forEach((card) => {{ card.hidden = !visible.has(card); }});
            if (transitCount) transitCount.textContent = String(matches.length);
            if (transitEmpty) transitEmpty.hidden = matches.length !== 0;
            if (transitPage) transitPage.textContent = matches.length ? `Страница ${{transitPageNo}} из ${{pages}} · записей ${{matches.length}}` : 'Ничего не найдено';
            if (transitPrev) transitPrev.disabled = transitPageNo <= 1;
            if (transitNext) transitNext.disabled = transitPageNo >= pages;
            if (transitClear) transitClear.hidden = !transitSearch.value;
            if (transitDateReset) transitDateReset.hidden = !(transitFrom?.value || transitTo?.value);
            if (transitResetAll) transitResetAll.hidden = !(
              transitSearch.value || transitFilter !== 'all' || transitPeriod !== 'all' || transitFrom?.value || transitTo?.value
            );
          }};
          transitSearch?.addEventListener('input', () => {{ transitPageNo = 1; updateTransit(); }});
          transitClear?.addEventListener('click', () => {{ if (transitSearch) transitSearch.value = ''; transitPageNo = 1; updateTransit(); transitSearch?.focus(); }});
          transitFilters.forEach((button) => button.addEventListener('click', () => {{
            transitFilter = button.dataset.transitFilter || 'all';
            transitFilters.forEach((node) => node.classList.toggle('is-active', node === button));
            transitPageNo = 1; updateTransit();
          }}));
          transitPeriods.forEach((button) => button.addEventListener('click', () => {{
            transitPeriod = button.dataset.transitPeriod || 'all';
            transitPeriods.forEach((node) => node.classList.toggle('is-active', node === button));
            if (transitFrom) transitFrom.value = '';
            if (transitTo) transitTo.value = '';
            transitPageNo = 1; updateTransit();
          }}));
          const customDateChanged = () => {{
            if (transitFrom?.value || transitTo?.value) transitPeriods.forEach((node) => node.classList.remove('is-active'));
            else {{ transitPeriod = 'all'; transitPeriods.forEach((node) => node.classList.toggle('is-active', node.dataset.transitPeriod === 'all')); }}
            transitPageNo = 1; updateTransit();
          }};
          transitFrom?.addEventListener('change', customDateChanged);
          transitTo?.addEventListener('change', customDateChanged);
          transitDateReset?.addEventListener('click', () => {{
            if (transitFrom) transitFrom.value = '';
            if (transitTo) transitTo.value = '';
            transitPeriod = 'all';
            transitPeriods.forEach((node) => node.classList.toggle('is-active', node.dataset.transitPeriod === 'all'));
            transitPageNo = 1; updateTransit();
          }});
          transitResetAll?.addEventListener('click', () => {{
            if (transitSearch) transitSearch.value = '';
            if (transitFrom) transitFrom.value = '';
            if (transitTo) transitTo.value = '';
            transitFilter = 'all';
            transitPeriod = 'all';
            transitFilters.forEach((node) => node.classList.toggle('is-active', node.dataset.transitFilter === 'all'));
            transitPeriods.forEach((node) => node.classList.toggle('is-active', node.dataset.transitPeriod === 'all'));
            transitPageNo = 1; updateTransit();
          }});
          document.querySelectorAll('[data-transit-delete-form]').forEach((form) => {{
            form.addEventListener('submit', (event) => {{
              const number = form.dataset.recordNumber || '';
              const ok = window.confirm(`Удалить запись №${{number}}? Она будет удалена из журнала и Google Sheets. Это действие нельзя отменить.`);
              if (!ok) event.preventDefault();
            }});
          }});
          transitPrev?.addEventListener('click', () => {{ if (transitPageNo > 1) {{ transitPageNo -= 1; updateTransit(); document.querySelector('.transit-browser')?.scrollIntoView({{behavior:'smooth',block:'start'}}); }} }});
          transitNext?.addEventListener('click', () => {{ transitPageNo += 1; updateTransit(); document.querySelector('.transit-browser')?.scrollIntoView({{behavior:'smooth',block:'start'}}); }});
          updateTransit();

          apply();
        }})();
      </script>
    """
    return layout("Инструкция", content, "instruction", show_admin)



def render_transit_process(show_admin: bool = False, query: dict[str, list[str]] | None = None) -> str:
    # Standalone visual guide for the TD -> Transit -> NP process.
    data = queue_transit_process

    flow_html = "".join(
        f'<div class="tp-flow-step"><span>{e(number)}</span><div><strong>{e(title)}</strong><small>{e(text)}</small></div></div>'
        for number, title, text in data.FLOW
    )
    role_html = "".join(
        f'<article class="tp-role-card"><div class="tp-role-icon">{index:02d}</div><h3>{e(role["title"])}</h3><p>{e(role["text"])}</p></article>'
        for index, role in enumerate(data.ROLES, start=1)
    )
    diff_html = "".join(
        f'<article class="tp-diff-card"><div class="tp-diff-code">{e(item["code"])}</div><div><h3>{e(item["title"])}</h3><p>{e(item["text"])}</p></div></article>'
        for item in data.KEY_DIFFERENCE
    )

    glance_cards = [
        ("Сначала подготовка", "Проверьте компанию, водителя и транспорт. Если нужных записей нет, создайте их до оформления перевозки"),
        ("Далее предварительная перевозка", "Заполните тип перевозки, документы, товар, маршрут, транспорт, водителя и количество НП"),
        ("Нац оператор только снимает НП", "Операции по снятию навигационных пломб в точке завершения выполняет национальный оператор"),
    ]
    glance_html = "".join(
        f'<article class="tp-glance-card"><strong>{e(title)}</strong><p>{e(text)}</p></article>'
        for title, text in glance_cards
    )

    lane_columns = [
        ("Перевозчик / сотрудник", ["Готовит компанию, водителя и транспорт", "Создаёт предварительную перевозку", "Заполняет документы, маршрут и сведения по НП"]),
        ("Менеджер системы", ["Проверяет сведения по перевозке", "Выполняет окончательное оформление", "Подтверждает готовность к дальнейшей работе"]),
        ("Национальный оператор", ["Участвует на этапе завершения", "Отвечает только за снятие НП", "Не выполняет наложение НП в этой инструкции"]),
    ]
    lane_html = "".join(
        '<article class="tp-lane-card">'
        f'<h3>{e(title)}</h3>'
        + ''.join(f'<div class="tp-lane-chip">{idx:02d} · {e(item)}</div>' for idx, item in enumerate(items, start=1))
        + '</article>'
        for title, items in lane_columns
    )

    business_process_cards = [
        ("01", "Старт", "Вход в процесс", "Есть задача на перевозку и исходные сведения: активная компания, водитель, транспорт, документы, товар и маршрут. Если чего-то не хватает, процесс возвращается на подготовку."),
        ("02", "Создание", "Формирование перевозки", "Создаётся предварительная перевозка. В неё связываются документы, товар, транспорт, водитель, маршрут и требуемое количество НП. На этом этапе отслеживание ещё не считается начатым."),
        ("03", "Контроль", "Контроль и окончательное оформление", "Перед переходом к работе с рейсом проверяется, что сведения относятся к одной фактической перевозке и не противоречат друг другу. Ошибки лучше исправить до окончательного оформления."),
        ("04", "В пути", "Выполнение и мониторинг", "После готовности перевозки транспорт следует по маршруту, а система используется для контроля состояния перевозки и связанных с ней навигационных пломб."),
        ("05", "Финиш", "Завершение", "В точке завершения организуется снятие НП. В этой инструкции национальный оператор отвечает именно за снятие пломб. После операции проверяется, что завершение корректно отражено в системе."),
    ]
    business_process_html = "".join(
        f'<article class="tp-business-card">'
        f'<div class="tp-business-marker"><span>{e(num)}</span></div>'
        f'<div class="tp-business-body"><div class="tp-business-meta">{e(label)}</div><h3>{e(title)}</h3><p>{e(desc)}</p></div>'
        f'</article>'
        for num, label, title, desc in business_process_cards
    )

    business_handoffs = [
        ("01", "Вернуться к подготовке", "Данных не хватает", "Не переходить дальше. Создать или исправить компанию, водителя, транспорт либо недостающие сведения и только потом вернуться к перевозке."),
        ("02", "Исправить до продолжения", "Есть расхождение", "Если документы, товар, транспорт, маршрут или сведения по НП не совпадают с фактической перевозкой, сначала исправить данные, затем продолжить оформление."),
        ("03", "Дождаться подтверждения", "Нужно подтверждение операции", "Если для конкретного сценария требуется предварительная заявка, дождаться подтверждения до следующего шага."),
        ("04", "Контроль результата", "Перевозка завершена", "После снятия НП проверить не только фактическое завершение операции, но и итоговое состояние перевозки в системе."),
    ]
    business_handoffs_html = "".join(
        f'<article class="tp-handoff-card"><div class="tp-handoff-top"><span class="tp-handoff-no">{e(num)}</span><span class="tp-handoff-state">{e(state)}</span></div><strong>{e(title)}</strong><p>{e(desc)}</p></article>'
        for num, state, title, desc in business_handoffs
    )

    object_chain = [
        "Компания / водитель / ТС",
        "Предварительная перевозка",
        "Документы / товар / маршрут / НП",
        "Окончательное оформление",
        "Отслеживание",
        "Снятие НП",
        "Проверка завершения",
    ]
    object_chain_html = "".join(
        f'<div class="tp-business-chain-step"><span>{idx:02d}</span><strong>{e(label)}</strong></div>'
        for idx, label in enumerate(object_chain, start=1)
    )

    stage_checks = [
        ("01", "Документы", "Сверьте документы и исходные сведения до сохранения или окончательного оформления."),
        ("02", "Система", "Убедитесь, что действие выполняется в нужной карточке перевозки и для активной компании."),
        ("03", "Проверка", "Перед следующим этапом ещё раз сравните транспорт, маршрут, товар и данные НП."),
        ("04", "Результат", "После каждого шага проверьте ожидаемый результат и только после этого переходите дальше."),
    ]
    stage_checks_html = "".join(
        f'<article class="tp-checkguide-item">'
        f'<span class="tp-checkguide-num">{e(num)}</span>'
        f'<div><strong>{e(title)}</strong><p>{e(desc)}</p></div></article>'
        for num, title, desc in stage_checks
    )
    glossary_html = "".join(
        f'<article class="tp-term-card"><strong>{e(item["term"])}</strong><p>{e(item["definition"])}</p></article>'
        for item in data.TERMS
    )
    examples_html = "".join(
        f'<a class="tp-example-card" href="#tp-{e(str(item.get("target", "")))}" data-tp-example>'
        f'<span>Пример {idx:02d}</span><h3>{e(item["title"])}</h3><p>{e(item["text"])}</p>'
        f'<strong class="tp-example-link">Перейти к шагу →</strong></a>'
        for idx, item in enumerate(data.EXAMPLES, start=1)
    )

    illustration_cards = [
        {"title": "Подготовка", "text": "Компания, водитель, транспорт и документы должны быть готовы до создания перевозки.", "src": "/static/transit-guide-01.webp?v=1.00.6.57"},
        {"title": "Создание перевозки", "text": "Заполняются данные перевозки, маршрут, товар, транспорт и необходимое количество НП.", "src": "/static/transit-guide-02.webp?v=1.00.6.57"},
        {"title": "Подготовка НП", "text": "Проверьте количество и номера НП и их связь с нужной перевозкой.", "src": "/static/transit-guide-03.webp?v=1.00.6.57"},
        {"title": "Отслеживание и завершение", "text": "Перевозка отслеживается до точки завершения, после чего национальный оператор выполняет снятие НП.", "src": "/static/transit-guide-04.webp?v=1.00.6.57"},
    ]
    illustrations_html = "".join(
        f'<article class="tp-illustration-card"><div class="tp-illustration-art"><img src="{e(card["src"])}" alt="{e(card["title"])}" loading="eager"></div><div class="tp-illustration-body"><h3>{e(card["title"])}</h3><p>{e(card["text"])}</p></div></article>'
        for card in illustration_cards
    )
    step_images = {
        "prepare": illustration_cards[0],
        "create": illustration_cards[1],
        "seal-prepare": illustration_cards[2],
        "tracking": illustration_cards[3],
    }

    # Build every quick-navigation row through one code path.
    nav_items: list[tuple[str, str, str]] = [
        (str(section.get("id", "")), str(section.get("number", "")), str(section.get("title", "")))
        for section in data.SECTIONS
    ]
    nav_items.extend([
        ("full-chain", "08", "Полная цепочка"),
        ("td-note", "09", "Документы и ТД"),
    ])
    # Each navigation item is wrapped in its own DIV row. This is deliberate:
    # even if old global link/span styles win the CSS cascade, DIV remains a
    # separate block and items can never concatenate into one paragraph.
    nav_html = "".join(
        f'<a class="tp-qnav-row" href="#tp-{e(sid)}">'
        f'<span class="tp-qnav-number">{e(number)}</span>'
        f'<span class="tp-qnav-label">{e(title)}</span>'
        f'</a>'
        for sid, number, title in nav_items
    )

    section_html: list[str] = []
    for section in data.SECTIONS:
        sid = str(section.get("id", ""))
        number = str(section.get("number", ""))
        title = str(section.get("title", ""))
        lead = str(section.get("lead", ""))

        body: list[str] = []
        if lead:
            body.append(f'<p class="tp-section-lead">{e(lead)}</p>')

        items = section.get("items") or []
        if items:
            body.append('<div class="tp-check-grid">' + ''.join(
                f'<div class="tp-check"><span>✓</span><p>{e(item)}</p></div>' for item in items
            ) + '</div>')

        steps = section.get("steps") or []
        if steps:
            body.append('<div class="tp-step-list">' + ''.join(
                f'<div class="tp-step-row"><div class="tp-step-num">{idx:02d}</div><div><strong>{e(step_title)}</strong><p>{e(step_text)}</p></div></div>'
                for idx, (step_title, step_text) in enumerate(steps, start=1)
            ) + '</div>')

        table_rows = section.get("table") or []
        if table_rows:
            headers = section.get("table_headers") or ("Данные", "Что указать / что делать")
            body.append(
                f'<div class="tp-table-wrap"><table class="tp-data-table"><thead><tr><th>{e(str(headers[0]))}</th><th>{e(str(headers[1]))}</th></tr></thead><tbody>'
                + ''.join(f'<tr><td><strong>{e(left)}</strong></td><td>{e(right)}</td></tr>' for left, right in table_rows)
                + '</tbody></table></div>'
            )

        checklist = section.get("checklist") or []
        if checklist:
            body.append('<div class="tp-checklist-block"><strong class="tp-subtitle">Перед оформлением проверьте</strong><div class="tp-check-grid">' + ''.join(
                f'<div class="tp-check"><span>✓</span><p>{e(item)}</p></div>' for item in checklist
            ) + '</div></div>')

        fields = section.get("fields") or []
        if fields:
            body.append('<div class="tp-field-row">' + ''.join(f'<span>{e(field)}</span>' for field in fields) + '</div>')

        chain = section.get("chain") or []
        if chain:
            body.append('<div class="tp-mini-flow">' + ''.join(
                f'<div><span>{idx:02d}</span><strong>{e(value)}</strong></div>' for idx, value in enumerate(chain, start=1)
            ) + '</div>')

        alerts = section.get("alerts") or []
        if alerts:
            body.append('<div class="tp-alert-grid">' + ''.join(
                f'<div class="tp-alert-item"><span>!</span><p>{e(item)}</p></div>' for item in alerts
            ) + '</div>')

        details = section.get("details") or []
        if details:
            body.append('<div class="tp-detail-panel"><strong>Дополнительно</strong>' + ''.join(
                f'<p>{e(item)}</p>' for item in details
            ) + '</div>')

        note = str(section.get("note", "") or "")
        if note:
            body.append(f'<div class="tp-note"><strong>Важно</strong><p>{e(note)}</p></div>')
        warning = str(section.get("warning", "") or "")
        if warning:
            body.append(f'<div class="tp-warning"><strong>Проверьте до следующего шага</strong><p>{e(warning)}</p></div>')
        result = str(section.get("result", "") or "")
        if result:
            body.append(f'<div class="tp-result"><strong>Результат шага</strong><p>{e(result)}</p></div>')

        step_image = step_images.get(sid)
        if step_image:
            body.append(
                f'<figure class="tp-step-image">'
                f'<button class="tp-step-image-link media-zoom-button" type="button" data-media-zoom="{e(step_image["src"])}" data-media-name="{e(step_image["title"])}" aria-label="Увеличить изображение шага">'
                f'<img src="{e(step_image["src"])}" alt="{e(step_image["title"])}" width="900" height="675" loading="lazy" decoding="async" fetchpriority="low">'
                f'</button>'
                f'</figure>'
            )

        search_blob = normalize_message(" ".join([title, lead] + [str(x) for x in items] + [str(x) for x in fields] + [str(x) for x in alerts])).casefold()
        section_html.append(
            f'<section class="tp-section" id="tp-{e(sid)}" data-tp-section data-search="{e(search_blob)}">'
            f'<div class="tp-section-number">{e(number)}</div>'
            f'<div class="tp-section-content"><div class="tp-section-heading"><span>Этап {e(number)}</span><h2>{e(title)}</h2></div>{"".join(body)}</div>'
            '</section>'
        )

    chain_html = "".join(
        f'<div class="tp-chain-row"><span>{index:02d}</span><p>{e(item)}</p></div>'
        for index, item in enumerate(data.FULL_CHAIN, start=1)
    )
    td_notes_html = "".join(
        f'<div class="tp-diagnostic-row"><span>{index:02d}</span><p>{e(item)}</p></div>'
        for index, item in enumerate(data.TD_NOTES, start=1)
    )

    problem_cards = [
        ("Не хватает данных до создания перевозки", ["Проверьте компанию", "Проверьте водителя и транспорт", "Создайте отсутствующие записи до продолжения"]),
        ("Ошибка в предварительной перевозке", ["Проверьте документы и товар", "Сверьте маршрут", "Проверьте количество НП перед сохранением"]),
        ("Заявка на НП ещё не подтверждена", ["Проверьте, требуется ли предварительная заявка", "Для ряда сценариев заявка подаётся минимум за 24 часа", "Дождитесь подтверждения перед дальнейшими действиями"]),
        ("Перевозка завершена не полностью", ["Проверьте снятие НП", "Проверьте итоговый статус перевозки", "При расхождении действуйте по рабочему регламенту"]),
    ]
    problems_html = "".join(
        '<article class="tp-problem-card">'
        f'<h3>{e(title)}</h3>'
        + ''.join(f'<div class="tp-problem-line">• {e(item)}</div>' for item in items)
        + '</article>'
        for title, items in problem_cards
    )

    style_block = ""


    content = f"""
      <div class=\"tp-page\">
      <section class=\"tp-hero\">
        <div class=\"tp-hero-copy\">
          <div class=\"tp-eyebrow\">ИС Транзит · порядок работы</div>
          <h1>Подготовка → перевозка → НП → завершение</h1>
          <p>{e(data.SUBTITLE)}</p>
          <div class=\"tp-hero-actions\"><a class=\"button primary\" href=\"#tp-flow\">Порядок действий</a><a class=\"button ghost\" href=\"#tp-prepare\">Начать с подготовки</a></div>
        </div>
        <div class=\"tp-hero-visual\" aria-label=\"Визуальная схема процесса\">
          <div class=\"tp-visual-card tp-visual-td\"><span>Шаг 1</span><strong>Подготовка</strong><small>Компания, водитель и транспорт готовы к оформлению</small></div>
          <div class=\"tp-visual-arrow\">→</div>
          <div class=\"tp-visual-card tp-visual-tr\"><span>Шаг 2</span><strong>Предварительная перевозка</strong><small>Заполняются документы, товар, маршрут и количество НП</small></div>
          <div class=\"tp-visual-arrow\">→</div>
          <div class=\"tp-visual-card tp-visual-np\"><span>Шаг 3</span><strong>НП и завершение</strong><small>Заявка, установка пломб, отслеживание, снятие и проверка завершения</small></div>
        </div>
      </section>


      <section>
        <div class=\"tp-block-heading\"><span>Перед началом</span><h2>Ключевые правила процесса</h2></div>
        <div class=\"tp-glance-grid\">{glance_html}</div>
      </section>

      <section class=\"tp-quick\" id=\"tp-flow\">
        <div class=\"tp-block-heading\"><span>Карта процесса</span><h2>Последовательность действий</h2></div>
        <div class=\"tp-flow\">{flow_html}</div>
      </section>

      <section>
        <div class=\"tp-block-heading\"><span>Работа по ролям</span><h2>Кто за что отвечает</h2></div>
        <div class=\"tp-lanes\">{lane_html}</div>
      </section>

      <section class=\"tp-difference\">
        <div class=\"tp-block-heading\"><span>Ключевое различие</span><h2>Предварительная перевозка и работа с НП</h2></div>
        <div class=\"tp-diff-grid\">{diff_html}</div>
      </section>

      <section class="tp-business-process" id="tp-business-process">
        <div class="tp-block-heading"><span>Бизнес-процесс</span><h2>Как проходит работа от входных данных до завершения</h2><p>Этот блок показывает не отдельные кнопки системы, а логику передачи процесса между этапами и контрольные точки.</p></div>
        <div class="tp-business-grid">{business_process_html}</div>
        <div class="tp-business-chain-wrap"><div class="tp-business-chain-title"><span>Связь объектов</span><strong>Что передаётся дальше по процессу</strong></div><div class="tp-business-chain">{object_chain_html}</div></div>
      </section>

      <section class="tp-business-control">
        <div class="tp-block-heading"><span>Переходы и возвраты</span><h2>Когда процесс нельзя просто продолжить дальше</h2><p>Если контрольная точка не пройдена, процесс возвращается на предыдущий этап для исправления данных.</p></div>
        <div class="tp-handoff-grid">{business_handoffs_html}</div>
      </section>

      <section class=\"tp-roles\">
        <div class=\"tp-block-heading\"><span>Участники</span><h2>Кто за какой этап отвечает</h2></div>
        <div class=\"tp-role-grid\">{role_html}</div>
      </section>

      <section class=\"tp-terms-section\" id=\"tp-terms\">
        <div class=\"tp-block-heading\"><span>Термины</span><h2>Обозначения и понятия</h2><p>Короткие определения терминов, которые встречаются в инструкции.</p></div>
        <div class=\"tp-terms-grid\">{glossary_html}</div>
      </section>

      <section>
        <div class=\"tp-block-heading\"><span>Примеры</span><h2>Типовые рабочие ситуации</h2></div>
        <div class=\"tp-example-grid\">{examples_html}</div>
      </section>

      <section class=\"tp-checkguide-section\">
        <div class=\"tp-block-heading\"><span>Контроль</span><h2>Что проверять на каждом этапе</h2><p>Четыре простых ориентира, которые помогают не пропустить важные данные.</p></div>
        <div class=\"tp-checkguide\">{stage_checks_html}</div>
      </section>

      <section>
        <div class=\"tp-block-heading\"><span>Проверки</span><h2>Типовые проблемные ситуации</h2></div>
        <div class=\"tp-problem-grid\">{problems_html}</div>
      </section>

      <section class=\"tp-workspace\">
        <aside class=\"tp-side-nav\">
          <strong>Быстрый переход</strong>
          <div class=\"tp-search\"><span>⌕</span><input type=\"search\" data-tp-search placeholder=\"Поиск по памятке...\" autocomplete=\"off\"></div>
          <div class="tp-qnav-list">{nav_html}</div>
        </aside>
        <div class=\"tp-content-list\">
          {''.join(section_html)}
          <section class=\"tp-section tp-chain-section\" id=\"tp-full-chain\" data-tp-section data-search=\"полная цепочка подготовка перевозка заявка пломба отслеживание снятие завершение\">
            <div class=\"tp-section-number\">08</div>
            <div class=\"tp-section-content\"><div class=\"tp-section-heading\"><span>Кратко</span><h2>Полная последовательность</h2></div><div class=\"tp-chain\">{chain_html}</div></div>
          </section>
          <section class=\"tp-section tp-diagnostic\" id=\"tp-td-note\" data-tp-section data-search=\"документы транзитная декларация тд карточка перевозки\">
            <div class=\"tp-section-number\">09</div>
            <div class=\"tp-section-content\"><div class=\"tp-section-heading\"><span>Важно</span><h2>Документы и транзитная декларация</h2></div>
              <div class=\"tp-diagnostic-list\">{td_notes_html}</div>
              <div class=\"tp-note\"><strong>Разделяйте операции</strong><p>{e(data.TD_NOTE)}</p></div>
            </div>
          </section>
          <div class=\"tp-no-results\" data-tp-empty hidden><strong>Ничего не найдено</strong><p>Измените поисковый запрос.</p></div>
        </div>
      </section>
      </div>

      <script>
      (() => {{
        const input = document.querySelector('[data-tp-search]');
        const sections = [...document.querySelectorAll('[data-tp-section]')];
        const empty = document.querySelector('[data-tp-empty]');
        const qnavLinks = [...document.querySelectorAll('.tp-qnav-row[href^="#"]')];
        if (!input) return;
        const norm = (v) => String(v || '').toLocaleLowerCase('ru-RU').replace(/ё/g,'е').trim();
        const visibleSections = () => sections.filter((section) => !section.hidden);
        const setActiveLink = (id) => {{
          qnavLinks.forEach((link) => {{
            const active = (link.getAttribute('href') || '') === `#${{id}}`;
            link.classList.toggle('is-active', active);
            if (active) link.setAttribute('aria-current', 'location');
            else link.removeAttribute('aria-current');
          }});
        }};
        const pickActiveSection = () => {{
          const items = visibleSections();
          if (!items.length) return '';
          const trigger = Math.max(120, Math.min(Math.round(window.innerHeight * 0.26), 220));
          let current = null;
          for (const section of items) {{
            const rect = section.getBoundingClientRect();
            if (rect.top <= trigger && rect.bottom > trigger) {{
              current = section;
              break;
            }}
          }}
          if (!current) current = items.find((section) => section.getBoundingClientRect().top > 0) || items[items.length - 1];
          return current ? current.id : '';
        }};
        let raf = 0;
        const refreshActive = () => {{
          if (raf) return;
          raf = requestAnimationFrame(() => {{
            raf = 0;
            const id = pickActiveSection();
            if (id) setActiveLink(id);
          }});
        }};
        const bindAnchorScroll = (link) => {{
          link.addEventListener('click', (event) => {{
            const href = link.getAttribute('href') || '';
            if (!href.startsWith('#')) return;
            const target = document.querySelector(href);
            if (!target) return;
            event.preventDefault();
            setActiveLink(target.id);
            target.scrollIntoView({{behavior:'smooth', block:'start'}});
            try {{ history.replaceState(null, '', href); }} catch (_) {{}}
          }});
        }};
        document.querySelectorAll('[data-tp-example]').forEach(bindAnchorScroll);
        qnavLinks.forEach(bindAnchorScroll);
        input.addEventListener('input', () => {{
          const q = norm(input.value);
          let visible = 0;
          sections.forEach((section) => {{
            const match = !q || norm(section.dataset.search).includes(q) || norm(section.textContent).includes(q);
            section.hidden = !match;
            if (match) visible += 1;
          }});
          if (empty) empty.hidden = visible > 0;
          refreshActive();
        }});
        window.addEventListener('scroll', refreshActive, {{passive:true}});
        window.addEventListener('resize', refreshActive);
        refreshActive();
      }})();
      </script>
    """
    return layout(data.TITLE, content, active="transit_process", show_admin=show_admin)


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
        all_filtered = STORE.list_tickets(
            selected_status, selected_category, selected_priority, search, 5000, 0, selected_view, current_employee,
        )
        all_filtered = [item for item in all_filtered if ticket_sla_state(item).get("active") and int(ticket_sla_state(item).get("elapsed_seconds",0) or 0) >= no_answer_minutes * 60]
        total = len(all_filtered)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = all_filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    else:
        total = STORE.count_tickets(
            selected_status, selected_category, selected_priority, search, selected_view, current_employee,
        )
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = STORE.list_tickets(
            selected_status,
            selected_category,
            selected_priority,
            search,
            PAGE_SIZE,
            (page - 1) * PAGE_SIZE,
            selected_view,
            current_employee,
        )
    counts = STORE.counts()
    shift_summary = STORE.shift_summary(current_employee)
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
    handoff_tickets = [item for item in STORE.list_tickets("", "", "", "", 500, 0) if int(item.get("shift_handoff", 0) or 0)][:20]
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
        <div><p class="eyebrow">Service Desk</p><h1>Заявки</h1><p>WhatsApp-запросы и ручные заявки из Telegram в одной очереди</p></div>
        <a class="button primary" href="/whatsapp">Открыть WhatsApp</a>
      </section>
      {notice_html}
      <section class="metrics">{cards}</section>
      <section class="smart-shift-panel"><div><strong>Текущая смена: {e(current_employee)}</strong>{shift_note}</div><div class="shift-metrics">{shift_cards}</div></section>
      {handoff_board}
      <section class="panel filters-panel">
        <form class="filters" method="get" action="/" data-dashboard-filter-form>
          <input type="hidden" name="view" value="{e(selected_view)}">
          <input type="search" name="q" value="{e(search)}" placeholder="№ заявки, ФИО, телефон, пост, БИН, ТД, текст..." data-live-ticket-search autocomplete="off">
          <select name="status">{status_options}</select>
          <select name="category">{category_options(selected_category, True)}</select>
          <select name="priority">{priority_options(selected_priority, True)}</select>
          <input type="number" name="no_answer_minutes" value="{no_answer_minutes or ''}" min="0" max="10080" step="5" placeholder="Без ответа > N мин" title="Показать заявки без первого ответа дольше N минут">
          <button class="button" type="submit">Применить</button>
          <a class="button ghost" href="/">Сбросить</a>
        </form>
      </section>
      <section class="panel table-panel" data-dashboard data-dashboard-version="{e(STORE.dashboard_version())}">
        <div class="table-hint"><span>Приоритет меняется в левом столбце. Правой кнопкой мыши по заявке можно быстро изменить статус.</span><span id="auto-refresh-state">Автообновление включено</span></div>
        <div class="table-scroll"><table>
          <thead><tr><th>Приоритет</th><th>№</th><th>Источник</th><th>Отправитель</th><th>Запрос</th><th>Статус</th><th>Время Алматы</th></tr></thead>
          <tbody id="ticket-table-body">{table_body}</tbody>
        </table></div>
        <div class="pagination" id="ticket-pagination">{render_pagination(query, total, page)}</div>
      </section>
      <section class="panel manual-panel">
        <div><p class="eyebrow">Telegram вручную</p><h2>Быстро добавить запрос</h2><p>Для заявок, которые сотрудник переносит из Telegram вручную</p></div>
        <form class="manual-form" method="post" action="/manual">
          <label>Тип запроса<input name="title" value="Открытие НП" required></label>
          <label>Номер пломбы / перевозки<input name="reference" placeholder="Например, 784512"></label>
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
    ticket = STORE.get_ticket(ticket_id)
    if not ticket:
        return layout(
            "Не найдено",
            '<section class="panel empty">Заявка не найдена</section>',
            show_admin=show_admin,
        )
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    events = STORE.ticket_events(ticket_id)
    event_rows = "".join(
        f'<li><span>{e(event["action"])}</span><small>{e(event["actor"])} · {e(human_time(event["created_at"]))}</small></li>'
        for event in events
    )
    context_fields = queue_contextual_tickets.ticket_fields(STORE, ticket_id)
    context_history = queue_contextual_tickets.ticket_history(STORE, ticket_id, 120)
    context_field_rows = "".join(
        f'<div class="context-field-row"><span>{e(queue_contextual_tickets.FIELD_LABELS.get(str(item.get("field_name","")), str(item.get("field_name",""))))}</span>'
        f'<strong>{e(str(item.get("value", "")))}</strong>'
        f'<small>{int(float(item.get("confidence",0) or 0)*100)}% · {e(str(item.get("source", "")))}</small></div>'
        for item in context_fields if str(item.get("value", "")).strip()
    )
    context_fields_panel = (
        '<section class="panel context-fields-panel"><h2>Контекстные поля</h2>'
        '<p class="muted">Данные, накопленные из нескольких сообщений. ФИО и пост могут оставаться необязательными.</p>'
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
    bin_details = ""
    if ticket["category"] == "bin":
        bin_details = f"""
          <div class="detail-grid bin-grid">
            <div><span>Старый БИН</span><strong>{e(ticket['bin_old'])}</strong></div>
            <div><span>Новый БИН</span><strong>{e(ticket['bin_new'])}</strong></div>
            <div><span>Номер АТС</span><strong>{e(ticket['ats_number'])}</strong></div>
            <div><span>Компания</span><strong>{e(ticket['company'])}</strong></div>
            <div><span>Страна</span><strong>{e(ticket['country'])}</strong></div>
          </div>"""

    chat_id = str(ticket.get("chat_id", "") or "").strip()
    if not chat_id:
        phone_digits = re.sub(r"\D", "", normalize_phone(str(ticket.get("phone", ""))))
        chat_id = f"{phone_digits}@c.us" if phone_digits else ""

    attachment = ""
    external_id = str(ticket.get("external_id", "") or "")
    media_items = STORE.list_ticket_whatsapp_media(ticket_id, chat_id, external_id, 24)
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
                when = datetime.fromtimestamp(int(media_item.get("message_timestamp", 0) or 0), tz=timezone.utc).astimezone(ALMATY_TIMEZONE).strftime("%d.%m %H:%M")
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
        sla_panel = f'''
          <section class="panel sla-panel {'sla-panel-overdue' if sla_state.get('overdue') else ''}">
            <h2>SLA реакции</h2>
            <div>{sla_badge(ticket)}</div>
            <p class="muted">Лимит для приоритета «{e(PRIORITIES.get(str(ticket.get('priority','normal')), str(ticket.get('priority','normal'))))}»: {int(sla_state.get('limit_minutes',0) or 0)} мин.</p>
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
              <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
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
            <div><span>Категория</span><strong>{e(CATEGORIES.get(ticket['category'], ticket['category']))}</strong></div>
            <div><span>Приоритет</span><strong>{e(PRIORITIES.get(ticket['priority'], ticket['priority']))}</strong></div>
            <div><span>Создана</span><strong>{e(human_time(ticket['created_at']))}</strong></div>
          </div>
          {bin_details}
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
    revision_field = f'<input type="hidden" name="revision" value="{int(ticket.get("revision",0))}"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">'
    content = content.replace(f'<input type="hidden" name="ticket_id" value="{ticket_id}">', f'<input type="hidden" name="ticket_id" value="{ticket_id}">'+revision_field)
    content = content.replace('<button class="button" name="action" value="transfer"', '<label>Что сделано и что осталось<textarea name="handoff_note" maxlength="2000" required></textarea></label><button class="button" name="action" value="transfer"')
    content = content.replace('<h2>Передача смене</h2>', '<h2>Передача смене</h2>'+queue_operations.handoff_history(STORE,ticket_id))
    content = content.replace('<aside>', '<aside>'+queue_operations.notification_panel(STORE,ticket_id),1)
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
        mine = STORE.list_tickets("", "", "", "", 400, 0, "mine", actor)
    except Exception:
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
    for event in AUTH.list_user_login_events(user_id, 12):
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
    for event in AUTH.list_user_audit_events(user_id, 16):
        object_label = " · ".join(
            part for part in [str(event.get("object_type") or ""), str(event.get("object_id") or "")] if part
        ) or "—"
        activity_rows.append(
            f'<tr><td>{e(human_time(str(event.get("created_at") or "")))}</td>'
            f'<td><strong>{e(str(event.get("action") or ""))}</strong><small>{e(str(event.get("details") or ""))}</small></td>'
            f'<td>{e(object_label)}</td></tr>'
        )
    activity_table = "".join(activity_rows) or '<tr><td colspan="3" class="empty">Действий аккаунта пока нет</td></tr>'
    session_count = AUTH.active_session_count_for_user(user_id)
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
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
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
