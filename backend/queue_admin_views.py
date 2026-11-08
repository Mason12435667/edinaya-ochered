from __future__ import annotations

from typing import Any, Callable, Iterable
from queue_templates import render_template


def admin_tabs(active: str, *, escape: Callable[[Any], str]) -> str:
    items = [
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
    return '<nav class="admin-tabs">' + "".join(
        f'<a class="admin-tab {"active" if key == active else ""}" href="{href}">{escape(label)}</a>'
        for key, href, label in items
    ) + "</nav>"


def admin_layout(
    title: str,
    content: str,
    active: str,
    *,
    layout_func: Callable[..., str],
    escape: Callable[[Any], str],
) -> str:
    heading = render_template("admin/layout.html")
    return layout_func(title, heading + admin_tabs(active, escape=escape) + content, "admin", True)


def render_admin_users(
    query: dict[str, list[str]],
    *,
    auth: Any,
    employees: Iterable[str],
    csrf_token: str,
    escape: Callable[[Any], str],
    human_time: Callable[[str], str],
    admin_layout_func: Callable[[str, str, str], str],
) -> str:
    employees = list(employees)
    notice = str(query.get("notice", [""])[0] or "")
    notice_kind = "error" if notice.casefold().startswith("ошибка:") else "success"
    notice_html = f'<div class="notice {notice_kind}">{escape(notice)}</div>' if notice else ""
    reset_rows: list[str] = []
    for request in auth.list_password_reset_requests():
        user_id = int(request["user_id"])
        requested_at = human_time(str(request.get("requested_at") or "")) if request.get("requested_at") else ""
        reset_rows.append(render_template(
            "admin/pending_user_row.html",
            display_name=escape(str(request.get('display_name') or request.get('username') or '')),
            username=escape(str(request.get('username') or '')),
            requested_at=escape(requested_at),
            csrf_token=escape(csrf_token),
            user_id=user_id,
        ))
    recovery_panel = ""
    if reset_rows:
        recovery_panel = f"""<section class="panel table-panel auth-recovery-panel"><div class="section-heading"><div><h2>Запросы на восстановление пароля</h2><p>При временном пароле пользователь обязан сменить его сразу после входа.</p></div></div><div class="table-scroll"><table><thead><tr><th>Пользователь</th><th>Запрошено</th><th>Действие</th></tr></thead><tbody>{''.join(reset_rows)}</tbody></table></div></section>"""
    rows: list[str] = []
    for user in auth.list_users():
        user_id = int(user["id"])
        role = str(user["role"])
        enabled = bool(user["enabled"])
        role_label = "Администратор" if role == "admin" else "Сотрудник"
        state = "Активен" if enabled else "Отключён"
        if user.get("must_change_password") and enabled:
            state += " · временный пароль"
        last_login = human_time(str(user.get("last_login_at") or "")) if user.get("last_login_at") else "Никогда"
        next_role = "employee" if role == "admin" else "admin"
        next_role_label = "Сделать сотрудником" if role == "admin" else "Сделать администратором"
        linked = str(user.get("employee_name") or "")
        user_theme = str(auth.get_user_preferences(user_id).get("theme") or "dark").strip().lower()
        if user_theme not in {"light", "dark", "pink"}:
            user_theme = "dark"
        theme_select = ''.join(
            f'<option value="{value}" {"selected" if value == user_theme else ""}>{label}</option>'
            for value, label in (("dark", "Тёмная"), ("light", "Светлая"), ("pink", "Pink / Pick Me 🌸"))
        )
        employee_select = '<option value="">Без привязки</option>' + ''.join(
            f'<option value="{escape(name)}" {"selected" if name == linked else ""}>{escape(name)}</option>'
            for name in employees
        )
        rows.append(render_template(
            "admin/user_row.html",
            display_name=escape(str(user['display_name'])),
            username=escape(str(user['username'])),
            linked_text=f'<small>Привязан: {escape(linked)}</small>' if linked else '',
            role=escape(role),
            role_label=escape(role_label),
            enabled_text='is-on' if enabled else 'is-off',
            state=escape(state),
            last_login=escape(last_login),
            csrf_token=escape(csrf_token),
            user_id=user_id,
            enabled_text_2='0' if enabled else '1',
            enabled_text_3='Отключить' if enabled else 'Включить',
            next_role=escape(next_role),
            next_role_label=escape(next_role_label),
            theme_select=theme_select,
            link_form=f'<details class="auth-reset-details"><summary>Привязка</summary><form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="employee_link"><input type="hidden" name="user_id" value="{user_id}"><select name="employee_name">{employee_select}</select><button class="button" type="submit">Сохранить</button></form></details>' if role == 'employee' else '',
        ))
    table_rows = "".join(rows) or '<tr><td colspan="5">Пользователей пока нет</td></tr>'

    session_rows: list[str] = []
    for session in auth.list_active_sessions(100):
        ua = str(session.get("user_agent") or "")
        session_rows.append(
            f"<tr><td><strong>{escape(str(session.get('display_name') or session.get('username') or ''))}</strong><small>@{escape(str(session.get('username') or ''))}</small></td><td>{escape(str(session.get('client_ip') or '—'))}</td><td>{escape(human_time(str(session.get('last_seen_at') or '')))}</td><td><small>{escape(ua[:90] or '—')}</small></td><td><form method='post' action='/admin/users'><input type='hidden' name='csrf_token' value='{escape(csrf_token)}'><input type='hidden' name='action' value='revoke_session'><input type='hidden' name='session_hash' value='{escape(str(session.get('token_hash') or ''))}'><button class='button ghost' type='submit'>Завершить</button></form></td></tr>"
        )
    session_table = "".join(session_rows) or '<tr><td colspan="5" class="empty">Активных сессий нет</td></tr>'

    login_rows: list[str] = []
    reasons = {
        "ok": "Успешный вход",
        "bad_password": "Неверный пароль",
        "lockout": "Блокировка после попыток",
        "locked": "Попытка во время блокировки",
        "disabled": "Отключённая учётная запись",
        "unknown_user": "Неизвестный логин",
    }
    for event in auth.list_login_events(120):
        ok = bool(event.get("success"))
        login_rows.append(
            f"<tr><td>{escape(human_time(str(event.get('created_at') or '')))}</td><td><strong>@{escape(str(event.get('username') or ''))}</strong></td><td><span class='auth-state {'is-on' if ok else 'is-off'}'>{'Успешно' if ok else 'Ошибка'}</span><small>{escape(reasons.get(str(event.get('reason') or ''), str(event.get('reason') or '')))}</small></td><td>{escape(str(event.get('client_ip') or '—'))}</td></tr>"
        )
    login_table = "".join(login_rows) or '<tr><td colspan="4" class="empty">История входов пуста</td></tr>'

    content = render_template(
        "admin/users.html",
        notice_html=notice_html,
        recovery_panel=recovery_panel,
        csrf_token=escape(csrf_token),
        table_rows=table_rows,
        session_table=session_table,
        login_table=login_table,
    )
    return admin_layout_func("Доступ", content, "users")
