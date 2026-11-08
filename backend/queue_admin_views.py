from __future__ import annotations

from typing import Any, Callable, Iterable


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
    heading = """
      <section class="page-heading admin-heading">
        <div><p class="eyebrow">Управление системой</p><h1>Админка</h1><p>Сотрудники, контакты, группы и настройки обращений</p></div>
      </section>
    """
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
        reset_rows.append(f"""
          <tr><td><strong>{escape(str(request.get('display_name') or request.get('username') or ''))}</strong><small>@{escape(str(request.get('username') or ''))}</small></td>
          <td>{escape(requested_at)}</td>
          <td><details class="auth-reset-details"><summary>Назначить новый пароль</summary><form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="password"><input type="hidden" name="user_id" value="{user_id}"><input type="password" name="password" minlength="10" placeholder="Новый пароль" required><label class="auth-check"><input type="checkbox" name="temporary_password" value="1" checked> временный</label><button class="button" type="submit">Сбросить</button></form></details></td></tr>
        """)
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
        rows.append(f"""
          <tr>
            <td><strong>{escape(str(user['display_name']))}</strong><small>@{escape(str(user['username']))}</small>{f'<small>Привязан: {escape(linked)}</small>' if linked else ''}</td>
            <td><span class="auth-role auth-role-{escape(role)}">{escape(role_label)}</span></td>
            <td><span class="auth-state {'is-on' if enabled else 'is-off'}">{escape(state)}</span></td>
            <td>{escape(last_login)}</td>
            <td class="table-actions auth-user-actions">
              <form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="toggle"><input type="hidden" name="user_id" value="{user_id}"><input type="hidden" name="enabled" value="{'0' if enabled else '1'}"><button class="button ghost" type="submit">{'Отключить' if enabled else 'Включить'}</button></form>
              <form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="role"><input type="hidden" name="user_id" value="{user_id}"><input type="hidden" name="role" value="{escape(next_role)}"><button class="button ghost" type="submit">{escape(next_role_label)}</button></form>
              <details class="auth-reset-details"><summary>Пароль</summary><form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="password"><input type="hidden" name="user_id" value="{user_id}"><input type="password" name="password" minlength="10" placeholder="Новый пароль" required><label class="auth-check"><input type="checkbox" name="temporary_password" value="1"> временный</label><button class="button" type="submit">Сбросить</button></form></details>
              <details class="auth-reset-details"><summary>Тема</summary><form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="theme"><input type="hidden" name="user_id" value="{user_id}"><select name="theme">{theme_select}</select><button class="button" type="submit">Сохранить</button></form></details>
              {f'<details class="auth-reset-details"><summary>Привязка</summary><form method="post" action="/admin/users"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="employee_link"><input type="hidden" name="user_id" value="{user_id}"><select name="employee_name">{employee_select}</select><button class="button" type="submit">Сохранить</button></form></details>' if role == 'employee' else ''}
              <form class="auth-delete-user-form" method="post" action="/admin/users" onsubmit="return confirm('Удалить пользователя? Аккаунт, его активные сессии и персональные настройки будут удалены. Действие необратимо.')"><input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="delete"><input type="hidden" name="user_id" value="{user_id}"><button class="button danger auth-delete-user" type="submit" title="Удалить учётную запись">Удалить</button></form>
            </td>
          </tr>
        """)
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

    content = f"""
      {notice_html}{recovery_panel}
      <section class="panel manual-panel"><div><h2>Добавить пользователя</h2><p>Для временного пароля включите соответствующий флажок: после первого входа остальные разделы будут заблокированы до смены пароля.</p></div>
        <form class="manual-form auth-create-user" method="post" action="/admin/users">
          <input type="hidden" name="csrf_token" value="{escape(csrf_token)}"><input type="hidden" name="action" value="create">
          <label>Логин<input name="username" maxlength="32" pattern="[A-Za-z0-9_.-]{{3,32}}" placeholder="ivan.petrov" required></label>
          <label>Имя<input name="display_name" maxlength="80" placeholder="Иван Петров" required></label>
          <label>Роль<select name="role"><option value="employee">Сотрудник</option><option value="admin">Администратор</option></select></label>
          <label>Пароль<input type="password" name="password" minlength="10" maxlength="256" required></label>
          <label class="auth-check"><input type="checkbox" name="temporary_password" value="1" checked> Временный пароль, потребовать смену при первом входе</label>
          <button class="button primary" type="submit">Создать пользователя</button>
        </form>
      </section>
      <section class="panel table-panel auth-users-panel"><div class="section-heading"><div><h2>Учётные записи</h2><p>Здесь можно управлять доступом, ролью, темой и привязкой сотрудника. Удаление завершает активные сессии пользователя; удалить собственный аккаунт и последнего активного администратора нельзя.</p></div></div><div class="table-scroll"><table><thead><tr><th>Пользователь</th><th>Роль</th><th>Статус</th><th>Последний вход</th><th>Действия</th></tr></thead><tbody>{table_rows}</tbody></table></div></section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>Активные сессии</h2><p>Администратор может завершить отдельный сеанс пользователя.</p></div></div><div class="table-scroll"><table><thead><tr><th>Пользователь</th><th>IP</th><th>Последняя активность</th><th>Устройство</th><th></th></tr></thead><tbody>{session_table}</tbody></table></div></section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>История авторизаций</h2><p>Успешные и неуспешные попытки входа.</p></div></div><div class="table-scroll"><table><thead><tr><th>Время</th><th>Логин</th><th>Результат</th><th>IP</th></tr></thead><tbody>{login_table}</tbody></table></div></section>
    """
    return admin_layout_func("Доступ", content, "users")
