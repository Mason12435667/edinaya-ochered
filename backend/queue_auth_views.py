"""HTML страниц, доступных до входа: вход, регистрация, восстановление пароля, первый запуск, смена временного пароля."""
from __future__ import annotations

from typing import Any, Callable

import queue_auth
from queue_templates import render_template


# Bump on every change to static/; appended as ?v= to bust browser caches.
ASSET_VERSION = "143.4"

AUTH_STYLESHEETS = ("auth.css", "pink-theme.css", "polish.css")


def stylesheet_tags(names) -> str:
    return "\n".join(f'  <link rel="stylesheet" href="/static/{name}?v={ASSET_VERSION}">' for name in names)


def script_tags(names) -> str:
    return "\n".join(f'  <script src="/static/{name}?v={ASSET_VERSION}" defer></script>' for name in names)


def _head(title: str) -> str:
    return render_template(
        "auth/head.html",
        title=title,
        stylesheet_tags=stylesheet_tags(AUTH_STYLESHEETS),
    )


def _script() -> str:
    return f'<script src="/static/account.js?v={ASSET_VERSION}" defer></script>'


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
    return render_template(
        "auth/login.html",
        head=_head("Вход"),
        local_mode_text='WINDOWS · ТЕСТ' if local_mode else 'Авторизация',
        error_html=error_html,
        setup_hint=setup_hint,
        next_path=escape(next_path),
        registration_enabled_text='<a href="/register">Создать аккаунт</a><span>•</span>' if queue_auth.self_registration_enabled() else "",
        session_hours=int(session_hours),
        script=_script(),
    )


def render_register(
    error: str = "",
    *,
    auth: Any,
    local_mode: bool,
    escape: Callable[[Any], str],
) -> str:
    error_html = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    if not queue_auth.self_registration_enabled():
        form_html = '<div class="auth-hint">Аккаунты сотрудников создаёт администратор в разделе «Пользователи».</div>'
    elif auth.enabled_admin_count() < 1:
        form_html = '<div class="auth-hint">Сначала должен быть создан администратор системы. После этого можно регистрировать аккаунты сотрудников.</div>'
    else:
        form_html = render_template("auth/register_form.html")
    return render_template(
        "auth/register.html",
        head=_head("Создать аккаунт"),
        local_mode_text='WINDOWS · ТЕСТ' if local_mode else 'Регистрация',
        error_html=error_html,
        form_html=form_html,
        script=_script(),
    )


def render_forgot_password(
    error: str = "",
    notice: str = "",
    *,
    local_mode: bool,
    escape: Callable[[Any], str],
) -> str:
    error_html = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    notice_html = f'<div class="auth-success">{escape(notice)}</div>' if notice else ""
    return render_template(
        "auth/forgot_password.html",
        head=_head("Восстановление пароля"),
        local_mode_text='WINDOWS · ТЕСТ' if local_mode else 'Восстановление доступа',
        error_html=error_html,
        notice_html=notice_html,
        registration_enabled_text='<span>•</span><a href="/register">Создать аккаунт</a>' if queue_auth.self_registration_enabled() else "",
        script=_script(),
    )


def render_local_setup(error: str = "", *, escape: Callable[[Any], str]) -> str:
    error_html = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    return render_template(
        "auth/local_setup.html",
        head=_head("Первый запуск"),
        error_html=error_html,
        script=_script(),
    )


def render_account(
    query: dict[str, list[str]],
    *,
    user: dict[str, Any] | None,
    escape: Callable[[Any], str],
    csrf_token: str = "",
) -> str:
    user = user or {}
    notice = str(query.get("notice", [""])[0] or "")
    notice_html = f'<div class="auth-error">{escape(notice)}</div>' if notice else ""
    if not user.get("must_change_password"):
        return ""
    return render_template(
        "auth/account.html",
        head=_head("Смена временного пароля"),
        id=int(user.get('id', 0) or 0),
        notice_html=notice_html,
        csrf_token=escape(csrf_token),
        script=_script(),
    )
