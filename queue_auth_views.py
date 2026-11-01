from __future__ import annotations

from typing import Any, Callable


ASSET_VERSION = "1.00.6.84"


def _head(title: str) -> str:
    return f'''<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#112a25">
  <title>{title} · Единая очередь</title>
  <link rel="icon" type="image/png" href="/static/favicon.png">
  <script>document.documentElement.dataset.theme = localStorage.getItem("queue-theme") || "dark";</script>
  <link rel="stylesheet" href="/static/auth100615.css?v={ASSET_VERSION}">
  <link rel="stylesheet" href="/static/pink-theme.css?v=1.00.6.40">
  <link rel="stylesheet" href="/static/ui-polish-1.00.6.40.css?v=1.00.6.40">
</head>'''


def _script() -> str:
    return f'<script src="/static/auth100618.js?v={ASSET_VERSION}" defer></script>'


def render_login(
    query: dict[str, list[str]] | None = None,
    error: str = "",
    *,
    auth: Any,
    local_mode: bool,
    escape: Callable[[Any], str],
    session_hours: int,
) -> str:
    query = query or {}
    next_path = str(query.get("next", [""])[0] or "").strip()
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    no_users = not auth.has_users()
    setup_hint = ""
    if no_users:
        if local_mode:
            setup_hint = '<p class="auth-hint">Это чистая тестовая копия. Сначала <a href="/setup">создайте первого администратора</a>.</p>'
        else:
            setup_hint = '<p class="auth-hint">Учётные записи ещё не созданы. Администратору нужно войти через существующий серверный админ-доступ и открыть раздел «Доступ».</p>'
    error_html = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    return f'''<!doctype html>
<html lang="ru">
{_head("Вход")}
<body class="auth-page">
  <main class="auth-shell">
    <section class="auth-card">
      <div class="auth-brand"><span class="auth-brand-mark">Q</span><div><strong>Единая очередь</strong><small>{'WINDOWS · ТЕСТ' if local_mode else 'Авторизация'}</small></div><button class="auth-theme-toggle" type="button" data-auth-theme-toggle>Тема</button></div>
      <h1>Вход в систему</h1>
      <p>Используйте личную учётную запись сотрудника.</p>
      {error_html}
      {setup_hint}
      <form method="post" action="/login" class="auth-form" autocomplete="on">
        <input type="hidden" name="next" value="{escape(next_path)}">
        <label>Логин<input name="username" autocomplete="username" maxlength="32" required autofocus></label>
        <label>Пароль<input type="password" name="password" autocomplete="current-password" maxlength="256" required></label>
        <button class="auth-primary" type="submit">Войти</button>
      </form>
      <div class="auth-links"><a href="/register">Создать аккаунт</a><span>•</span><a href="/forgot-password">Восстановить пароль</a></div>
      <small class="auth-foot">Сеанс автоматически истекает через {int(session_hours)} ч.</small>
    </section>
  </main>
  {_script()}
</body>
</html>'''


def render_register(
    error: str = "",
    *,
    auth: Any,
    local_mode: bool,
    escape: Callable[[Any], str],
) -> str:
    error_html = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    if auth.enabled_admin_count() < 1:
        form_html = '<div class="auth-hint">Сначала должен быть создан администратор системы. После этого можно регистрировать аккаунты сотрудников.</div>'
    else:
        form_html = '''<form method="post" action="/register" class="auth-form" autocomplete="on">
          <label>Логин<input name="username" autocomplete="username" maxlength="32" pattern="[A-Za-z0-9_.-]{3,32}" placeholder="ivan.petrov" required autofocus></label>
          <label>Имя<input name="display_name" autocomplete="name" maxlength="80" placeholder="Иван Петров" required></label>
          <label>Пароль<input type="password" name="password" autocomplete="new-password" minlength="10" maxlength="256" required></label>
          <label>Повторите пароль<input type="password" name="password2" autocomplete="new-password" minlength="10" maxlength="256" required></label>
          <button class="auth-primary" type="submit">Создать аккаунт сотрудника</button>
        </form>
        <div class="auth-hint">Можно создавать несколько аккаунтов сотрудников. Каждый логин должен быть уникальным.</div>'''
    return f'''<!doctype html>
<html lang="ru">
{_head("Создать аккаунт")}
<body class="auth-page">
  <main class="auth-shell"><section class="auth-card">
    <div class="auth-brand"><span class="auth-brand-mark">Q</span><div><strong>Единая очередь</strong><small>{'WINDOWS · ТЕСТ' if local_mode else 'Регистрация'}</small></div><button class="auth-theme-toggle" type="button" data-auth-theme-toggle>Тема</button></div>
    <h1>Создать аккаунт</h1>
    <p>Самостоятельная регистрация создаёт только учётную запись с ролью «Сотрудник». Роль администратора через эту страницу получить нельзя.</p>
    {error_html}
    {form_html}
    <div class="auth-links"><a href="/login">Вернуться ко входу</a><span>•</span><a href="/forgot-password">Восстановить пароль</a></div>
  </section></main>
  {_script()}
</body></html>'''


def render_forgot_password(
    error: str = "",
    notice: str = "",
    *,
    local_mode: bool,
    escape: Callable[[Any], str],
) -> str:
    error_html = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    notice_html = f'<div class="auth-success">{escape(notice)}</div>' if notice else ""
    return f'''<!doctype html>
<html lang="ru">
{_head("Восстановление пароля")}
<body class="auth-page">
  <main class="auth-shell"><section class="auth-card">
    <div class="auth-brand"><span class="auth-brand-mark">Q</span><div><strong>Единая очередь</strong><small>{'WINDOWS · ТЕСТ' if local_mode else 'Восстановление доступа'}</small></div><button class="auth-theme-toggle" type="button" data-auth-theme-toggle>Тема</button></div>
    <h1>Восстановить пароль</h1>
    <p>Укажите логин. Запрос появится у администратора, после чего он сможет назначить новый пароль.</p>
    {error_html}{notice_html}
    <form method="post" action="/forgot-password" class="auth-form" autocomplete="on">
      <label>Логин<input name="username" autocomplete="username" maxlength="32" required autofocus></label>
      <button class="auth-primary" type="submit">Отправить запрос</button>
    </form>
    <div class="auth-links"><a href="/login">Вернуться ко входу</a><span>•</span><a href="/register">Создать аккаунт</a></div>
  </section></main>
  {_script()}
</body></html>'''


def render_local_setup(error: str = "", *, escape: Callable[[Any], str]) -> str:
    error_html = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    return f'''<!doctype html>
<html lang="ru">
{_head("Первый запуск")}
<body class="auth-page">
  <main class="auth-shell"><section class="auth-card">
    <div class="auth-brand"><span class="auth-brand-mark">Q</span><div><strong>Единая очередь</strong><small>WINDOWS · ТЕСТ</small></div><button class="auth-theme-toggle" type="button" data-auth-theme-toggle>Тема</button></div>
    <h1>Создание администратора</h1>
    <p>Эта страница доступна только локально и только пока в тестовой базе нет пользователей.</p>
    {error_html}
    <form method="post" action="/setup" class="auth-form">
      <label>Логин<input name="username" value="admin" maxlength="32" required></label>
      <label>Имя<input name="display_name" value="Администратор" maxlength="80" required></label>
      <label>Пароль<input type="password" name="password" minlength="10" maxlength="256" required></label>
      <label>Повторите пароль<input type="password" name="password2" minlength="10" maxlength="256" required></label>
      <button class="auth-primary" type="submit">Создать и войти</button>
    </form>
  </section></main>
  {_script()}
</body></html>'''


def render_account(
    query: dict[str, list[str]],
    *,
    user: dict[str, Any] | None,
    escape: Callable[[Any], str],
) -> str:
    user = user or {}
    notice = str(query.get("notice", [""])[0] or "")
    notice_html = f'<div class="auth-error">{escape(notice)}</div>' if notice else ""
    if not user.get("must_change_password"):
        return ""
    return f'''<!doctype html>
<html lang="ru">
{_head("Смена временного пароля")}
<body class="auth-page" data-auth-user-id="{int(user.get('id', 0) or 0)}">
  <main class="auth-shell"><section class="auth-card">
    <div class="auth-brand"><span class="auth-brand-mark">Q</span><div><strong>Единая очередь</strong><small>Безопасность аккаунта</small></div></div>
    <h1>Смените временный пароль</h1>
    <p>После смены временного пароля откроется доступ к рабочей системе.</p>
    {notice_html}
    <form class="auth-form" method="post" action="/account/password">
      <input type="hidden" name="next" value="/">
      <label>Текущий пароль<input type="password" name="current_password" autocomplete="current-password" required autofocus></label>
      <label>Новый пароль<input type="password" name="new_password" minlength="10" autocomplete="new-password" required></label>
      <label>Повторите новый пароль<input type="password" name="new_password2" minlength="10" autocomplete="new-password" required></label>
      <button class="auth-primary" type="submit">Сменить пароль</button>
    </form>
    <div class="auth-links"><a href="/logout">Выйти</a></div>
  </section></main>
  {_script()}
</body></html>'''
