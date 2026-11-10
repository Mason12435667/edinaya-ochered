"""Обработчик HTTP-запросов: проверка входа и CSRF, таблицы маршрутов GET и POST, отдача статики и страниц, ответы JSON."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import queue_auth
import queue_contextual_tickets
import queue_dialog_flow
import queue_inbound_media
import queue_performance_http
import queue_realtime
import queue_reliability
import queue_admin_views
import queue_ticket_views
import queue_workflow
from queue_avatars import AvatarCache
from queue_chat_views import groups_page_data
from queue_core import content_disposition_header, display_phone, log_suppressed, normalize_message, normalize_phone, valid_chat_id, valid_conversation_id, valid_group_id
from queue_menu_config import MenuConfig
from queue_store_conversations import MENU_CONTEXT
from queue_work_context import activate_employee_shift, active_employee, assignment_names, audit_admin_form_submission, work_actor
from ticketing import STATUSES, TicketStore

@dataclass(frozen=True)
class HttpDependencies:
    store: TicketStore
    auth: queue_auth.AuthStore
    root: Path
    media_dir: Path
    monitor_log_path: Path
    avatars: AvatarCache
    menu_config: MenuConfig
    reliability: queue_reliability.ReliabilityMonitor
    categories: Mapping[str, str]
    employees: list[str]  # app.py edits this list in place
    max_employees: int
    page_size: int
    local_mode: bool
    app_version: str
    webhook_token: str
    reaction_emojis: set[str]
    admin_user: str
    admin_session_token: str
    admin_session_cookie: str
    admin_session_max_age: int
    auth_session_cookie: str
    auth_session_max_age: int
    csrf_token: Callable[..., str]
    csrf_token_valid: Callable[..., bool]
    render_login: Callable[..., str]
    render_register: Callable[..., str]
    render_forgot_password: Callable[..., str]
    render_local_setup: Callable[..., str]
    render_account: Callable[..., str]
    admin_users_data: Callable[[], dict[str, object]]
    final_ticket_filter: Callable[..., list]
    update_ticket_status: Callable[..., tuple]
    set_chat_favorite: Callable[[str, bool], tuple[bool, str]]
    create_scheduled_backup: Callable[..., object]


# Files under static/ that may be served (every page asset is part of the main layout).
STATIC_FILES = {
    "favicon.png": "image/png",
    **{name: "text/css; charset=utf-8" for name in queue_ticket_views.MAIN_STYLESHEETS},
    **{name: "text/javascript; charset=utf-8" for name in (*queue_ticket_views.MAIN_SCRIPTS, *queue_ticket_views.PAGE_SCRIPTS)},
    **{f"templates/{path.name}": "text/html; charset=utf-8"
       for path in (Path(__file__).resolve().parents[1] / "frontend" / "static" / "templates").glob("*.html")},
}


# Largest request body read just to refuse it; anything bigger is left unread.
MAX_UNREAD_BODY = 64 * 1024
UNREAD_BODY_SECONDS = 2


class _CountedBody:
    """The request body stream, counting what the routes have read from it."""

    def __init__(self, raw):
        self.raw = raw
        self.count = 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.count += len(data)
        return data

    def readline(self, size=-1):
        data = self.raw.readline(size)
        self.count += len(data)
        return data

    def readinto(self, buffer):
        size = self.raw.readinto(buffer) or 0
        self.count += size
        return size

    def __getattr__(self, name):
        return getattr(self.raw, name)

# The bookmarks live in existing app_settings; no new SQLite schema or media changes.
_BOOKMARKS_LOCK = threading.RLock()
_BOOKMARKS_MAX = 300


def _bookmark_setting(user_id: int) -> str:
    return f"whatsapp_message_bookmarks_v1:{int(user_id)}"


def _load_message_bookmarks(store: TicketStore, user_id: int) -> list[dict[str, object]]:
    if user_id <= 0:
        return []
    raw = store.get_setting(_bookmark_setting(user_id), "[]")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        values = []
    if not isinstance(values, list):
        return []
    result = []
    for entry in values[:_BOOKMARKS_MAX]:
        if isinstance(entry, dict) and entry.get("chat_id") and entry.get("message_id"):
            result.append(entry)
    return result


def merge_chat_alias_rows(store: TicketStore, state: dict[str, object]) -> None:
    """Collapse @lid/@c.us rows of one private chat in a chat-state snapshot, in place.

    Last-mile protection for old live duplicates: even if CHAT_STATE still holds both
    rows, the browser receives one canonical row, so visible duplicates disappear
    without waiting for another alias event.
    """
    raw_chats = state.get("chats", [])
    if isinstance(raw_chats, list):
        merged_chats: dict[str, dict[str, object]] = {}

        def _useful_chat_name_v27(value: object) -> bool:
            text = str(value or "").strip()
            if not text:
                return False
            if text.casefold() in {
                "пользователь whatsapp",
                "неизвестный отправитель",
                "direct",
                "рабочий whatsapp",
            }:
                return False
            compact = re.sub(r"[\s+().-]+", "", text)
            return not compact.isdigit()

        for raw_row in raw_chats:
            if not isinstance(raw_row, dict):
                continue
            raw_id = valid_chat_id(str(raw_row.get("id", "")))
            if not raw_id:
                continue
            canonical_id = store.canonical_whatsapp_chat_id(raw_id) or raw_id
            row = dict(raw_row)
            row["id"] = canonical_id

            previous = merged_chats.get(canonical_id)
            if previous is None:
                merged_chats[canonical_id] = row
                continue

            try:
                previous_ts = int(previous.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                previous_ts = 0
            try:
                row_ts = int(row.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                row_ts = 0

            newer, older = (
                (row, previous)
                if row_ts >= previous_ts
                else (previous, row)
            )
            combined = dict(older)
            combined.update(newer)
            combined["id"] = canonical_id

            newer_name = str(newer.get("name", "") or "").strip()
            older_name = str(older.get("name", "") or "").strip()
            if not _useful_chat_name_v27(newer_name) and _useful_chat_name_v27(older_name):
                combined["name"] = older_name

            for counter in ("unread_count", "mention_unread_count"):
                try:
                    combined[counter] = max(
                        int(previous.get(counter, 0) or 0),
                        int(row.get(counter, 0) or 0),
                    )
                except (TypeError, ValueError):
                    pass

            for flag in ("favorite", "muted", "needs_reply"):
                if flag in previous or flag in row:
                    combined[flag] = bool(previous.get(flag)) or bool(row.get(flag))

            for field in (
                "avatar_url",
                "profile_pic_url",
                "phone",
                "last_sender",
                "last_sender_avatar_url",
            ):
                if not combined.get(field):
                    combined[field] = previous.get(field) or row.get(field) or ""

            merged_chats[canonical_id] = combined

        deduped = list(merged_chats.values())
        deduped.sort(
            key=lambda item: (
                int(bool(item.get("favorite"))),
                int(item.get("timestamp", 0) or 0),
            ),
            reverse=True,
        )
        state["chats"] = deduped

        # Forward target menu must use the same identity list.
        raw_targets = state.get("forward_targets")
        if isinstance(raw_targets, list):
            target_map: dict[str, dict[str, object]] = {}
            for target in raw_targets:
                if not isinstance(target, dict):
                    continue
                raw_id = valid_conversation_id(str(target.get("id", "")))
                if not raw_id:
                    continue
                canonical_id = store.canonical_whatsapp_chat_id(raw_id) or raw_id
                row = dict(target)
                row["id"] = canonical_id
                previous = target_map.get(canonical_id)
                if previous is None:
                    target_map[canonical_id] = row
                    continue
                # Prefer the row with a useful name.
                old_name = str(previous.get("name", "") or "").strip()
                new_name = str(row.get("name", "") or "").strip()
                if _useful_chat_name_v27(new_name) or not _useful_chat_name_v27(old_name):
                    target_map[canonical_id] = {**previous, **row, "id": canonical_id}
            state["forward_targets"] = list(target_map.values())

    selected = valid_chat_id(str(state.get("selected_chat_id", "") or ""))
    if selected:
        state["selected_chat_id"] = store.canonical_whatsapp_chat_id(selected) or selected


class QueueHTTPHandlerMixin:
    def handle_one_request(self) -> None:
        # A browser leaving the page closes its pending long poll: not a server error.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if not any(line.lower().startswith(b"cache-control:") for line in getattr(self,"_headers_buffer",[])):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def request_host_allowed(self) -> bool:
        host = self.headers.get("Host", "").lower().strip()
        hostname = host.split(":", 1)[0].strip("[]")
        configured_hosts = {
            item.strip().casefold()
            for item in os.getenv("QUEUE_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if (
            os.getenv("QUEUE_ALLOW_LAN") == "1"
            or hostname in {"127.0.0.1", "localhost", "::1"}
            or hostname in configured_hosts
        ):
            return True
        self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "Invalid site name")
        return False

    def authenticated_user(self) -> str:
        # Локальный Windows-режим тоже использует настоящую
        # авторизацию приложения. Автоматического входа администратором больше нет.
        # На боевом сервере доверяем имени пользователя только от локального Nginx.
        # Клиентский Authorization здесь намеренно не разбирается.
        remote_user = self.headers.get("X-Remote-User", "").strip()
        if self.client_address and self.client_address[0] in {"127.0.0.1", "::1"}:
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", remote_user):
                return remote_user
        return ""

    MULTI_ACCOUNT_SLOTS = 8

    def auth_cookie_jar(self):
        cookies = SimpleCookie()
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return cookies
        try:
            cookies.load(raw_cookie)
        except (CookieError, ValueError):
            return SimpleCookie()
        return cookies

    def auth_slot_cookie_name(self, slot: int) -> str:
        return f"{self.deps.auth_session_cookie}_{int(slot)}"

    def active_auth_slot(self, cookies=None) -> int:
        cookies = cookies or self.auth_cookie_jar()
        morsel = cookies.get(f"{self.deps.auth_session_cookie}_active")
        try:
            slot = int(str(morsel.value)) if morsel else 0
        except (TypeError, ValueError):
            return 0
        return slot if 1 <= slot <= self.MULTI_ACCOUNT_SLOTS else 0

    def saved_account_sessions(self, *, touch: bool = False) -> list[dict[str, object]]:
        cookies = self.auth_cookie_jar()
        active_slot = self.active_auth_slot(cookies)
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for slot in range(1, self.MULTI_ACCOUNT_SLOTS + 1):
            morsel = cookies.get(self.auth_slot_cookie_name(slot))
            token = str(morsel.value).strip() if morsel else ""
            if not token or token in seen:
                continue
            seen.add(token)
            user = self.deps.auth.session_user(token, touch=touch)
            if user:
                result.append({"slot": slot, "token": token, "user": user, "active": slot == active_slot})
        legacy = cookies.get(self.deps.auth_session_cookie)
        legacy_token = str(legacy.value).strip() if legacy else ""
        if legacy_token and legacy_token not in seen:
            user = self.deps.auth.session_user(legacy_token, touch=touch)
            if user:
                result.append({"slot": 0, "token": legacy_token, "user": user, "active": active_slot == 0})
        if result and not any(bool(item.get("active")) for item in result):
            result[0]["active"] = True
        return result

    def auth_cookie_token(self) -> str:
        cookies = self.auth_cookie_jar()
        active_slot = self.active_auth_slot(cookies)
        if active_slot:
            morsel = cookies.get(self.auth_slot_cookie_name(active_slot))
            if morsel:
                return str(morsel.value).strip()
        legacy = cookies.get(self.deps.auth_session_cookie)
        if legacy:
            return str(legacy.value).strip()
        for slot in range(1, self.MULTI_ACCOUNT_SLOTS + 1):
            morsel = cookies.get(self.auth_slot_cookie_name(slot))
            if morsel:
                return str(morsel.value).strip()
        return ""

    def current_auth_slot(self) -> int:
        token = self.auth_cookie_token()
        if not token:
            return 0
        cookies = self.auth_cookie_jar()
        for slot in range(1, self.MULTI_ACCOUNT_SLOTS + 1):
            morsel = cookies.get(self.auth_slot_cookie_name(slot))
            if morsel and str(morsel.value).strip() == token:
                return slot
        legacy = cookies.get(self.deps.auth_session_cookie)
        if legacy and str(legacy.value).strip() == token:
            return 0
        return 0

    def current_app_user(self) -> dict[str, object] | None:
        cached = getattr(self, "_queue_auth_user", None)
        if cached is False:
            return None
        if isinstance(cached, dict):
            return cached
        token = self.auth_cookie_token()
        user = self.deps.auth.session_user(token) if token else None
        if not user:
            for item in self.saved_account_sessions(touch=False):
                candidate = str(item.get("token") or "")
                if not candidate or candidate == token:
                    continue
                user = self.deps.auth.session_user(candidate)
                if user:
                    break
        self._queue_auth_user = user if user else False
        return user

    def legacy_admin_user(self) -> dict[str, object] | None:
        username = self.authenticated_user().casefold()
        if (bool(username) and username == self.deps.admin_user) or self.has_admin_session():
            return {
                "id": 0,
                "username": self.deps.admin_user,
                "display_name": "Администратор",
                "role": "admin",
                "enabled": True,
                "legacy": True,
            }
        return None

    def effective_user(self) -> dict[str, object] | None:
        return self.current_app_user() or self.legacy_admin_user()

    def require_user(self) -> bool:
        user = self.effective_user()
        if user:
            queue_auth.set_current_user(user)
            path = urlparse(self.path).path
            if user.get("must_change_password") and not user.get("legacy") and path not in {"/account", "/account/password", "/logout"}:
                if self.command == "GET":
                    self.redirect("/account?required=1")
                elif path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or path.startswith("/chat"):
                    self.json_response({"error": "password_change_required", "message": "Сначала смените временный пароль"}, HTTPStatus.LOCKED)
                else:
                    self.redirect("/account?required=1")
                return False
            return True
        if self.command == "GET":
            target = quote(self.path if self.path.startswith("/") else "/")
            self.redirect(f"/login?next={target}")
            return False
        if self.path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or self.path.startswith("/chat"):
            self.json_response({"error": "authentication_required"}, HTTPStatus.UNAUTHORIZED)
        else:
            self.redirect("/login")
        return False

    def local_setup_allowed(self) -> bool:
        return bool(
            self.deps.local_mode
            and not self.deps.auth.has_users()
            and self.client_address
            and self.client_address[0] in {"127.0.0.1", "::1"}
        )

    @staticmethod
    def service_post_path(path: str) -> bool:
        return path in {
            "/api/media-stage",
            "/api/whatsapp",
            "/api/inbound-recovery-reply",
            "/api/template-error-delivery",
            "/api/outbound/claim",
            "/api/outbound/heartbeat",
            "/api/outbound/begin",
            "/api/outbound/result",
            "/api/chat-list-sync",
            "/api/group-list-sync",
            "/api/presence-sync",
            "/api/group-participants-sync",
            "/api/group-message-identities-sync",
            "/api/contact-list-sync",
            "/api/avatar-sync",
            "/api/contact-profile-sync",
            "/api/group-refresh-check",
            "/api/connector-state",
            "/api/contact-policy",
            "/api/call-permission",
            "/api/chat-messages-sync",
            "/api/chat-message-ack",
            "/api/chat-reaction-sync",
            "/api/whatsapp-action/claim",
            "/api/whatsapp-action/result",
            "/api/chat-control",
        }

    def _session_cookie_header(self, name: str, value: str, *, max_age: int | None = None) -> str:
        secure = "" if self.deps.local_mode else "Secure; "
        age = self.deps.auth_session_max_age if max_age is None else max(0, int(max_age))
        return f"{name}={value}; Path=/; Max-Age={age}; HttpOnly; {secure}SameSite=Lax"

    def start_app_session(self, token: str, location: str = "/") -> None:
        token = str(token or "").strip()
        new_user = self.deps.auth.session_user(token, touch=False) if token else None
        if not new_user:
            self.redirect("/login")
            return
        cookies = self.auth_cookie_jar()
        slots: dict[int, tuple[str, dict[str, object]]] = {}
        invalid_slots: list[int] = []
        for slot in range(1, self.MULTI_ACCOUNT_SLOTS + 1):
            morsel = cookies.get(self.auth_slot_cookie_name(slot))
            existing_token = str(morsel.value).strip() if morsel else ""
            if not existing_token:
                continue
            existing_user = self.deps.auth.session_user(existing_token, touch=False)
            if existing_user:
                slots[slot] = (existing_token, existing_user)
            else:
                invalid_slots.append(slot)

        migrated: list[tuple[int, str]] = []
        legacy = cookies.get(self.deps.auth_session_cookie)
        legacy_token = str(legacy.value).strip() if legacy else ""
        legacy_user = self.deps.auth.session_user(legacy_token, touch=False) if legacy_token else None
        new_user_id = int(new_user.get("id", 0) or 0)
        slot = 0
        for existing_slot, (existing_token, existing_user) in slots.items():
            if int(existing_user.get("id", 0) or 0) == new_user_id:
                slot = existing_slot
                if existing_token != token:
                    self.deps.auth.revoke_session(existing_token)
                break

        if legacy_user and int(legacy_user.get("id", 0) or 0) == new_user_id:
            legacy_user = None

        if legacy_user and legacy_token not in {item[0] for item in slots.values()}:
            free_for_legacy = next((n for n in range(1, self.MULTI_ACCOUNT_SLOTS + 1) if n not in slots and n != slot), 0)
            if free_for_legacy:
                slots[free_for_legacy] = (legacy_token, legacy_user)
                migrated.append((free_for_legacy, legacy_token))

        if not slot:
            free_slots = [n for n in range(1, self.MULTI_ACCOUNT_SLOTS + 1) if n not in slots]
            slot = free_slots[0] if free_slots else (self.active_auth_slot(cookies) or 1)
            if slot in slots:
                old_token, _old_user = slots[slot]
                if old_token != token:
                    self.deps.auth.revoke_session(old_token)

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        for migrated_slot, migrated_token in migrated:
            self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(migrated_slot), migrated_token))
        migrated_slots = {migrated_slot for migrated_slot, _migrated_token in migrated}
        for invalid_slot in invalid_slots:
            if invalid_slot != slot and invalid_slot not in migrated_slots:
                self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(invalid_slot), "", max_age=0))
        self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(slot), token))
        self.send_header("Set-Cookie", self._session_cookie_header(f"{self.deps.auth_session_cookie}_active", str(slot)))
        self.send_header("Set-Cookie", self._session_cookie_header(self.deps.auth_session_cookie, "", max_age=0))
        self.end_headers()

    def switch_app_session(self, slot: int, location: str = "/") -> bool:
        cookies = self.auth_cookie_jar()
        if slot == 0:
            morsel = cookies.get(self.deps.auth_session_cookie)
        elif 1 <= slot <= self.MULTI_ACCOUNT_SLOTS:
            morsel = cookies.get(self.auth_slot_cookie_name(slot))
        else:
            morsel = None
        token = str(morsel.value).strip() if morsel else ""
        user = self.deps.auth.session_user(token, touch=False) if token else None
        if not user:
            return False
        if str(user.get("role", "")) == "employee":
            activate_employee_shift(user)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if slot > 0:
            self.send_header("Set-Cookie", self._session_cookie_header(f"{self.deps.auth_session_cookie}_active", str(slot)))
        else:
            self.send_header("Set-Cookie", self._session_cookie_header(f"{self.deps.auth_session_cookie}_active", "", max_age=0))
        self.end_headers()
        return True

    def end_app_session(self) -> None:
        token = self.auth_cookie_token()
        current_slot = self.current_auth_slot()
        sessions = self.saved_account_sessions(touch=False)
        if token:
            user = self.deps.auth.session_user(token, touch=False)
            if user:
                self.deps.auth.release_user_locks(int(user.get("id", 0) or 0), str(user.get("username", "") or ""))
            self.deps.auth.revoke_session(token)
        remaining = [item for item in sessions if str(item.get("token") or "") != token]
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/accounts" if remaining else "/login")
        secure = "" if self.deps.local_mode else "Secure; "
        if current_slot > 0:
            self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(current_slot), "", max_age=0))
        else:
            self.send_header("Set-Cookie", self._session_cookie_header(self.deps.auth_session_cookie, "", max_age=0))
        if remaining:
            next_user = remaining[0].get("user") if isinstance(remaining[0].get("user"), dict) else {}
            if str(next_user.get("role", "")) == "employee":
                activate_employee_shift(next_user)
            next_slot = int(remaining[0].get("slot", 0) or 0)
            if next_slot > 0:
                self.send_header("Set-Cookie", self._session_cookie_header(f"{self.deps.auth_session_cookie}_active", str(next_slot)))
            else:
                self.send_header("Set-Cookie", self._session_cookie_header(f"{self.deps.auth_session_cookie}_active", "", max_age=0))
        else:
            self.send_header("Set-Cookie", self._session_cookie_header(f"{self.deps.auth_session_cookie}_active", "", max_age=0))
            self.send_header("Set-Cookie", f"{self.deps.admin_session_cookie}=; Path=/; Max-Age=0; HttpOnly; {secure}SameSite=Lax")
        self.end_headers()

    def has_admin_session(self) -> bool:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
            morsel = cookies.get(self.deps.admin_session_cookie)
            return bool(morsel) and secrets.compare_digest(morsel.value, self.deps.admin_session_token)
        except (CookieError, ValueError):
            return False

    def is_admin(self) -> bool:
        user = self.current_app_user()
        if user and str(user.get("role")) == "admin":
            return True
        username = self.authenticated_user().casefold()
        return (bool(username) and username == self.deps.admin_user) or self.has_admin_session()

    def claim_conversation_write(self, chat_id: str) -> bool:
        chat_id = valid_conversation_id(chat_id) or valid_group_id(chat_id)
        if not chat_id:
            self.json_response({"queued": False, "error": "Диалог не выбран"}, HTTPStatus.BAD_REQUEST)
            return False
        user = self.effective_user() or {}
        kind = "group" if chat_id.endswith("@g.us") else "chat"
        ok, lock = self.deps.auth.claim_conversation(chat_id, kind, user, display_name=work_actor())
        if ok:
            return True
        owner = lock or {}
        owner_name = str(owner.get("owner_display_name", "") or owner.get("owner_username", "") or "другой пользователь")
        self.json_response(
            {
                "queued": False,
                "error": "conversation_locked",
                "message": f"Диалог уже ведёт {owner_name}. Доступен только просмотр.",
                "conversation_lock": self.chat_views.conversation_lock_snapshot(chat_id),
            },
            HTTPStatus.LOCKED,
        )
        return False

    def require_admin(self) -> bool:
        if self.is_admin():
            return True
        if self.command == "GET":
            self.redirect("/admin/login")
        else:
            self.send_error(HTTPStatus.FORBIDDEN, "Administrator access required")
        return False

    def start_admin_session(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/admin/")
        self.send_header(
            "Set-Cookie",
            f"{self.deps.admin_session_cookie}={self.deps.admin_session_token}; Path=/; Max-Age={self.deps.admin_session_max_age}; HttpOnly; {'Secure; ' if not self.deps.local_mode else ''}SameSite=Lax",
        )
        self.end_headers()

    def end_admin_session(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{self.deps.admin_session_cookie}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
        )
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def integration_api_key(self) -> dict[str, object] | None:
        raw = str(self.headers.get("X-API-Key", "") or "").strip()
        if not raw:
            auth_header = str(self.headers.get("Authorization", "") or "").strip()
            if auth_header.lower().startswith("bearer "):
                raw = auth_header[7:].strip()
        key = self.deps.auth.authenticate_api_key(raw, "read") if raw else None
        if key:
            return key
        self.json_response({"error":"unauthorized","message":"Требуется действующий API-ключ"}, HTTPStatus.UNAUTHORIZED)
        return None

    def csv_bytes_response(self, body: bytes, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", content_disposition_header("attachment", filename))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        queue_auth.clear_current_user()
        if not self.request_host_allowed():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        public_get = (
            parsed.path in {"/login", "/register", "/forgot-password", "/setup", "/health", "/health/connector", "/admin/login"}
            or parsed.path.startswith("/static/")
            or parsed.path.startswith("/api/integration/")
            or parsed.path == "/favicon.ico"
        )
        if not public_get and not self.require_user():
            return
        user = self.effective_user()
        if user:
            queue_auth.set_current_user(user)
        route = self.EARLY_GET_ROUTES.get(parsed.path)
        if route:
            route(self, query)
            return
        if queue_performance_http.get(self):
            return
        if self.reliability_routes.get(self):
            return
        if self.workflow_routes.get(self):
            return
        if self.productivity_routes.get(self):
            return
        if self.feature_routes.get(self):
            return
        route = self.ADMIN_GET_ROUTES.get(parsed.path)
        if route:
            if self.require_admin():
                route(self, query)
            return
        route = self.GET_ROUTES.get(parsed.path)
        if route:
            route(self, query)
            return
        name = parsed.path[len("/static/"):]
        if parsed.path.startswith("/static/") and name in STATIC_FILES:
            self.file_response(self.deps.root / "frontend" / "static" / name, STATIC_FILES[name])
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _get_search(self, query) -> None:
        self.redirect("/whatsapp")

    def _get_bookmarks(self, query) -> None:
        self.redirect("/whatsapp?bookmarks=1")

    def _get_api_message_bookmarks(self, query) -> None:
        user_id = int((self.effective_user() or {}).get("id", 0) or 0)
        if user_id <= 0:
            self.json_response({"error": "Требуется вход"}, HTTPStatus.UNAUTHORIZED)
            return
        rows = _load_message_bookmarks(self.deps.store, user_id)
        self.json_response({"bookmarks": rows, "items": rows})

    def _get_api_global_search(self, query) -> None:
        self.json_response({"error": "Глобальный поиск пока недоступен"}, HTTPStatus.GONE)

    def _get_api_account_preferences(self, query) -> None:
        current = self.effective_user() or {}
        user_id = int(current.get("id", 0) or 0)
        self.json_response({"preferences": self.deps.auth.get_user_preferences(user_id) if user_id > 0 else {}})

    def _get_api_realtime_wait(self, query) -> None:
        try:
            since = max(0, int(query.get("since", ["0"])[0] or 0))
        except (TypeError, ValueError):
            since = 0
        try:
            timeout_ms = max(250, min(25000, int(query.get("timeout", ["25000"])[0] or 25000)))
        except (TypeError, ValueError):
            timeout_ms = 25000
        state = queue_realtime.wait_for_change(since, timeout_ms / 1000.0)
        self.json_response(state)

    def _get_login(self, query) -> None:
        add_account = str(query.get("add", [""])[0] or "").strip().lower() in {"1", "true", "yes"}
        if self.effective_user() and not add_account:
            self.redirect("/")
        else:
            if add_account and not query.get("next"):
                query["next"] = ["/accounts"]
            self.html_response(self.deps.render_login(query))

    def _get_accounts(self, query) -> None:
        self.page_response("app")

    def _get_register(self, query) -> None:
        status = HTTPStatus.OK if queue_auth.self_registration_enabled() else HTTPStatus.FORBIDDEN
        self.html_response(self.deps.render_register(), status)

    def _get_forgot_password(self, query) -> None:
        if self.effective_user():
            self.redirect("/account")
        else:
            self.html_response(self.deps.render_forgot_password())

    def _get_setup(self, query) -> None:
        if not self.local_setup_allowed():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.html_response(self.deps.render_local_setup())

    def _get_logout(self, query) -> None:
        self.end_app_session()

    def _get_account(self, query) -> None:
        current = self.effective_user() or {}
        if current.get("must_change_password"):
            self.html_response(self.deps.render_account(query))
        else:
            self.redirect("/")

    def _get_me(self, query) -> None:
        self.page_response("app")

    def _get_dashboard(self, query) -> None:
        self.page_response("tickets")

    def _get_admin_login(self, query) -> None:
        if self.authenticated_user().casefold() != self.deps.admin_user:
            self.send_error(HTTPStatus.FORBIDDEN, "Administrator login required")
            return
        self.start_admin_session()

    def _get_admin_logout(self, query) -> None:
        self.end_admin_session()

    def _get_ticket(self, query) -> None:
        self.page_response("ticket")

    def page_response(self, name: str) -> None:
        self.file_response(self.deps.root / "frontend" / "pages" / f"{name}.html", "text/html; charset=utf-8")

    def _get_whatsapp(self, query) -> None:
        self.page_response("whatsapp")

    def _get_api_app_page(self, query) -> None:
        page = query.get("page", [""])[0]
        providers = {
            "me": self.ticket_views.my_page_data,
            "accounts": lambda: self.ticket_views.accounts_data(self.saved_account_sessions(touch=False), self.current_auth_slot()),
            "reminders": self.productivity_routes.reminders_data,
            "recent": self.workflow_routes.recent_data,
        }
        if page not in providers:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.json_response(providers[page]())

    def _get_api_page_context(self, query) -> None:
        self.json_response(self.ticket_views.page_context(self.is_admin()))

    def _get_api_groups_page(self, query) -> None:
        self.json_response(groups_page_data(self.is_admin(), store=self.deps.store, avatars=self.deps.avatars))

    def _get_groups(self, query) -> None:
        self.page_response("groups")

    def _get_api_integration_ping(self, query) -> None:
        key = self.integration_api_key()
        if not key:
            return
        self.json_response({"ok":True,"service":"Единая очередь","version":self.deps.app_version,"key":{"name":str(key.get("name") or ""),"scope":str(key.get("scope") or "read")}})

    def _get_api_integration_tickets(self, query) -> None:
        key = self.integration_api_key()
        if not key:
            return
        try:
            limit = max(1, min(500, int(query.get("limit", ["100"])[0] or 100)))
        except (TypeError, ValueError):
            limit = 100
        rows = self.deps.final_ticket_filter(query, limit=5000)[:limit]
        public_rows = []
        for item in rows:
            public_rows.append({
                "id": int(item.get("id",0) or 0),
                "status": str(item.get("status") or ""),
                "category": str(item.get("category") or item.get("title") or ""),
                "priority": str(item.get("priority") or ""),
                "assigned_to": str(item.get("assigned_to") or ""),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "description": str(item.get("description") or item.get("problem") or ""),
            })
        self.json_response({"ok":True,"count":len(public_rows),"tickets":public_rows})

    def _get_api_dashboard_data(self, query) -> None:
        self.json_response(self.ticket_pages.dashboard(query))

    def _get_api_ticket_page(self, query) -> None:
        self.json_response(self.ticket_pages.ticket(self.query_int(query, "id"), self.is_admin()))

    def _get_api_chat_state(self, query) -> None:
        state = self.chat_views.chat_state_snapshot(
            query.get("chat_id", [""])[0],
            query.get("read", ["0"])[0] == "1",
        )
        if isinstance(state, dict):
            merge_chat_alias_rows(self.deps.store, state)
        self.json_response(state)

    def _get_api_group_state(self, query) -> None:
        self.json_response(self.chat_views.group_state_snapshot(query.get("chat_id", [""])[0], query.get("read", ["0"])[0] == "1"))

    def _get_api_chat_history(self, query) -> None:
        self.handle_chat_history(query)

    def _get_api_chat_action_status(self, query) -> None:
        action_id = self.query_int(query, "id")
        action = self.deps.store.whatsapp_action_status(action_id)
        self.json_response({"found": bool(action), "action": action or {}})

    def _get_api_notifications(self, query) -> None:
        self.json_response(self.chat_views.notifications_snapshot())

    def _get_api_conversation_users(self, query) -> None:
        users=[]
        for item in self.deps.auth.list_users():
            if not item.get("enabled"): continue
            users.append({"id":int(item.get("id",0) or 0),"username":str(item.get("username","") or ""),"display_name":str(item.get("display_name","") or ""),"role":str(item.get("role","") or ""),"employee_name":str(item.get("employee_name","") or "")})
        current=self.effective_user() or {}
        self.json_response({
            "users": users,
            "current_user_id": int(current.get("id", 0) or 0),
            "is_admin": self.is_admin(),
            "active_shift_employee": str(self.deps.store.get_setting("active_employee", active_employee()) or active_employee()),
        })

    def _get_api_chat_media(self, query) -> None:
        self.handle_chat_media(query)

    def _get_favicon(self, query) -> None:
        self.file_response(self.deps.root / "frontend" / "static" / "favicon.png", "image/png")

    def _get_connector_state(self, query) -> None:
        self.json_response(self.chat_views.connector.snapshot())

    def _get_health_connector(self, query) -> None:
        # Open without signing in: whoever scans the QR code links a phone to the work WhatsApp.
        self.json_response({**self.chat_views.connector.snapshot(), "qr_data_url": ""})

    def _get_health(self, query) -> None:
        self.json_response({"status": "ok"})

    def _get_admin_home(self, query) -> None:
        self.page_response("admin")

    def _get_admin_users(self, query) -> None:
        self.page_response("admin")

    def _get_admin_employees(self, query) -> None:
        self.page_response("admin")

    def _get_admin_manual(self, query) -> None:
        self.page_response("admin")

    def _get_admin_menu(self, query) -> None:
        self.page_response("admin")

    def _get_admin_categories(self, query) -> None:
        self.page_response("admin")

    def _get_admin_errors(self, query) -> None:
        self.page_response("admin")

    def _get_admin_error(self, query) -> None:
        self.page_response("admin")

    def _get_admin_contacts(self, query) -> None:
        self.page_response("admin")

    def _get_admin_audit(self, query) -> None:
        self.page_response("admin")

    def _get_admin_manager(self, query) -> None:
        self.page_response("admin")

    def _get_admin_api_keys(self, query) -> None:
        self.page_response("admin")

    def _get_admin_tickets_export(self, query) -> None:
        rows = self.deps.final_ticket_filter(query)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["ID","Статус","Категория","Приоритет","Сотрудник","Создано","Обновлено","Описание"])
        for item in rows:
            writer.writerow([
                int(item.get("id",0) or 0),
                str(item.get("status") or ""),
                str(item.get("category") or item.get("title") or ""),
                str(item.get("priority") or ""),
                str(item.get("assigned_to") or ""),
                str(item.get("created_at") or ""),
                str(item.get("updated_at") or ""),
                str(item.get("description") or item.get("problem") or ""),
            ])
        self.csv_bytes_response(("﻿" + stream.getvalue()).encode("utf-8"), "tickets.csv")

    def _get_admin_system(self, query) -> None:
        self.page_response("admin")

    def _get_api_admin_contact_search(self, query) -> None:
        search_raw = query.get("q", [""])[0]
        self.json_response({"rows": self.admin_pages.discovered_contacts(search_raw, 80)})

    def _get_api_admin_page(self, query) -> None:
        page = query.get("page", [""])[0]
        providers = {
            "users": self.deps.admin_users_data,
            "analytics": self.productivity_routes.analytics_data,
            "backups": self.productivity_routes.backups_data,
            "reliability": self.reliability_routes.page_data,
            "workflow": self.workflow_routes.admin_data,
            "shift": self.feature_routes.shift_data,
            "api-keys": lambda: self.admin_pages.api_keys_data(int((self.effective_user() or {}).get("id", 0) or 0)),
        }
        data = providers[page]() if page in providers else self.admin_pages.page_data(page, query)
        if data is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.json_response({**data, "tabs": [{"key": key, "href": href, "label": label} for key, href, label in queue_admin_views.ADMIN_TABS]})

    def _get_admin_groups(self, query) -> None:
        self.page_response("groups")

    def _get_to_admin_home(self, query) -> None:
        self.redirect("/admin/")

    def do_POST(self) -> None:
        body = _CountedBody(self.rfile)
        self.rfile = body
        try:
            self._route_post()
        finally:
            self.rfile = body.raw
            self.discard_unread_body(body.count)

    def discard_unread_body(self, consumed: int = 0) -> None:
        """Read what a refusing route left of the request body. Closing with it unread
        resets the connection before the client reads the answer."""
        try:
            left = int(self.headers.get("Content-Length", "0")) - consumed
            if 0 < left <= MAX_UNREAD_BODY:
                # the answer is already sent: never wait long for bytes that may not come
                waited = self.connection.gettimeout()
                self.connection.settimeout(UNREAD_BODY_SECONDS)
                try:
                    self.rfile.read(left)
                finally:
                    self.connection.settimeout(waited)
        except (ValueError, OSError):
            pass

    def _route_post(self) -> None:
        queue_auth.clear_current_user()
        self._body_cache = None
        if not self.request_host_allowed():
            return
        parsed = urlparse(self.path)
        route = self.PUBLIC_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed)
            return
        if not self.service_post_path(parsed.path) and not self.require_user():
            return
        user = self.effective_user()
        if user:
            queue_auth.set_current_user(user)
        # Connector endpoints authenticate with X-Webhook-Token instead.
        if not self.service_post_path(parsed.path) and not self.csrf_check_passed(parsed.path):
            return
        if parsed.path.startswith("/admin/") and self.is_admin():
            try:
                self.deps.auth.record_audit(user or {}, "admin_post", object_type="admin_settings", object_id=parsed.path, details="Отправлено изменение в административном разделе")
            except sqlite3.Error:
                log_suppressed("queue_http_handler.do_POST")
        route = self.EARLY_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed)
            return
        if queue_performance_http.post(self):
            return
        if self.reliability_routes.post(self):
            return
        if queue_reliability.maintenance_active(self.deps.store) and queue_reliability.should_block_mutation(parsed.path, self.is_admin()):
            if parsed.path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or parsed.path.startswith("/chat"):
                self.json_response({"error":"Режим обслуживания: изменения временно заблокированы администратором"}, HTTPStatus.LOCKED)
            else:
                self.redirect("/?notice=" + quote("Режим обслуживания: изменения временно заблокированы"))
            return
        if self.workflow_routes.post(self):
            return
        if self.productivity_routes.post(self):
            return
        if self.feature_routes.post(self):
            return
        if self.upload_routes.post(self):
            return
        route = self.SERVICE_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed)
            return
        form = self.read_form()
        route = self.FORM_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed, form)
            return
        audit_admin_form_submission(parsed.path, form, self.effective_user())
        route = self.AUDITED_FORM_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed, form)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _post_login(self, parsed) -> None:
        # POST /login
        form = self.read_form()
        result = self.deps.auth.authenticate(
            form.get("username", ""),
            form.get("password", ""),
            client_ip=self.client_address[0] if self.client_address else "",
            user_agent=self.headers.get("User-Agent", ""),
        )
        if not result.get("ok"):
            next_path = form.get("next", "/")
            self.html_response(self.deps.render_login({"next": [next_path]}, str(result.get("error") or "Ошибка входа")), HTTPStatus.UNAUTHORIZED)
            return
        next_path = str(form.get("next", "/") or "/")
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        login_user = result.get("user") or {}
        if str(login_user.get("role", "")) == "employee":
            activate_employee_shift(login_user)
        if bool(login_user.get("must_change_password")):
            next_path = "/account?required=1"
        self.start_app_session(str(result["token"]), next_path)

    def _post_register(self, parsed) -> None:
        # POST /register (only with QUEUE_ALLOW_SELF_REGISTRATION=1; the account then waits for approval)
        if not queue_auth.self_registration_enabled():
            self.html_response(self.deps.render_register(), HTTPStatus.FORBIDDEN)
            return
        form = self.read_form()
        password = form.get("password", "")
        if password != form.get("password2", ""):
            self.html_response(self.deps.render_register("Пароли не совпадают"), HTTPStatus.BAD_REQUEST)
            return
        ok, message, _ = self.deps.auth.self_register_employee(
            form.get("username", ""),
            form.get("display_name", ""),
            password,
        )
        if not ok:
            self.html_response(self.deps.render_register(message), HTTPStatus.BAD_REQUEST)
            return
        # The new account is disabled until an administrator approves it: no session yet.
        self.html_response(self.deps.render_login({}, message), HTTPStatus.OK)

    def _post_forgot_password(self, parsed) -> None:
        # POST /forgot-password
        form = self.read_form()
        notice = self.deps.auth.request_password_reset(form.get("username", ""))
        self.html_response(self.deps.render_forgot_password(notice=notice), HTTPStatus.OK)

    def _post_setup(self, parsed) -> None:
        # POST /setup
        if not self.local_setup_allowed():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        form = self.read_form()
        if form.get("password", "") != form.get("password2", ""):
            self.html_response(self.deps.render_local_setup("Пароли не совпадают"), HTTPStatus.BAD_REQUEST)
            return
        ok, message, _ = self.deps.auth.create_user(
            form.get("username", ""), form.get("display_name", ""), form.get("password", ""), role="admin"
        )
        if not ok:
            self.html_response(self.deps.render_local_setup(message), HTTPStatus.BAD_REQUEST)
            return
        result = self.deps.auth.authenticate(
            form.get("username", ""), form.get("password", ""),
            client_ip=self.client_address[0] if self.client_address else "",
            user_agent=self.headers.get("User-Agent", ""),
        )
        if not result.get("ok"):
            self.html_response(self.deps.render_login({}, "Администратор создан, но автоматический вход не выполнен"), HTTPStatus.UNAUTHORIZED)
            return
        self.start_app_session(str(result["token"]), "/admin/users")

    def _post_api_global_search(self, parsed) -> None:
        # POST /api/global-search
        self.json_response({"updated": False, "error": "Глобальный поиск пока недоступен"}, HTTPStatus.GONE)

    def _post_api_message_bookmarks(self, parsed) -> None:
        # POST /api/message-bookmarks, /api/bookmarks
        form = self.read_form()
        user_id = int((self.effective_user() or {}).get("id", 0) or 0)
        chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
        message_id = str(form.get("message_id", "") or "").strip()
        if user_id <= 0:
            self.json_response({"updated": False, "error": "Требуется вход"}, HTTPStatus.UNAUTHORIZED)
            return
        if not chat_id or not 1 <= len(message_id) <= 240 or any(ord(c) < 32 for c in message_id):
            self.json_response({"updated": False, "error": "Некорректное сообщение"}, HTTPStatus.BAD_REQUEST)
            return
        if not chat_id.endswith("@g.us"):
            chat_id = self.deps.store.canonical_whatsapp_chat_id(chat_id) or chat_id
        is_favorite = str(form.get("favorite", "1")).strip().lower() in {"1", "true", "yes", "on"}
        with _BOOKMARKS_LOCK:
            items = _load_message_bookmarks(self.deps.store, user_id)
            items = [item for item in items if not (str(item.get("chat_id")) == chat_id and str(item.get("message_id")) == message_id)]
            if is_favorite:
                preview = str(form.get("preview", "") or "").replace("\x00", "").strip()[:280]
                name = str(form.get("chat_name", "") or "").replace("\x00", "").strip()[:100]
                sender = str(form.get("sender", "") or "").replace("\x00", "").strip()[:100]
                try:
                    timestamp = max(0, int(form.get("timestamp", "0") or 0))
                except ValueError:
                    timestamp = 0
                items.insert(0, {"chat_id": chat_id, "message_id": message_id, "chat_name": name,
                                 "preview": preview, "sender": sender, "timestamp": timestamp})
            items = items[:_BOOKMARKS_MAX]
            self.deps.store.set_setting(_bookmark_setting(user_id), json.dumps(items, ensure_ascii=False))
        self.json_response({"updated": True, "favorite": is_favorite, "bookmarks": items})

    def _post_accounts_switch(self, parsed) -> None:
        # POST /accounts/switch
        form = self.read_form()
        try:
            slot = int(form.get("slot", "0") or 0)
        except (TypeError, ValueError):
            slot = -1
        if not self.switch_app_session(slot, "/"):
            self.redirect("/accounts?notice=" + quote("Сеанс этого аккаунта уже завершён. Войдите в него снова"))

    def _post_admin_api_keys(self, parsed) -> None:
        # POST /admin/api-keys
        if not self.require_admin(): return
        form=self.read_form()
        if form is None: return
        action=str(form.get("action") or "").strip().lower(); actor=self.effective_user() or {}
        if action == "create":
            ok,message,token=self.deps.auth.create_api_key(actor,form.get("name",""),"read")
            if ok: self.admin_pages.remember_new_api_key(int(actor.get("id",0) or 0),token)
            self.redirect("/admin/api-keys?notice="+quote(message)); return
        if action == "revoke":
            try: key_id=int(form.get("key_id",0) or 0)
            except ValueError: key_id=0
            ok,message=self.deps.auth.revoke_api_key(actor,key_id); self.redirect("/admin/api-keys?notice="+quote(message)); return
        self.redirect("/admin/api-keys?notice="+quote("Неизвестное действие"))

    def _post_api_system_warning_action(self, parsed) -> None:
        # POST /api/system-warning-action
        payload = self.read_json_body(120_000)
        if payload is None:
            return
        action = str(payload.get("action") or "").strip().lower()
        event_id = str(payload.get("event_id") or "").strip()
        try:
            if action == "dismiss":
                if not queue_reliability.dismiss_notification(self.deps.store, event_id):
                    self.json_response({"error":"invalid_warning_id"}, HTTPStatus.BAD_REQUEST)
                    return
                self.json_response({"ok":True,"dismissed":1})
                return
            if action == "dismiss_all":
                snapshot = self.chat_views.notifications_snapshot()
                ids = [str(item.get("id") or item.get("event_id") or "") for item in snapshot.get("events",[]) if isinstance(item,dict)]
                count = queue_reliability.dismiss_notifications(self.deps.store, ids)
                self.json_response({"ok":True,"dismissed":count})
                return
            if action in {"check","check_all"}:
                self.deps.reliability.refresh_active_alerts()
                self.json_response({"ok":True,"checked":True})
                return
        except Exception as error:
            self.json_response({"error":str(error)[:180]}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.json_response({"error":"invalid_action"}, HTTPStatus.BAD_REQUEST)

    def _post_api_media_stage(self, parsed) -> None:
        # POST /api/media-stage
        payload = self.read_authorized_json(800_000)
        if payload is not None:
            try: self.json_response(queue_inbound_media.receive(self.deps.media_dir, payload))
            except (ValueError, OSError) as error: self.json_response({"error": str(error)[:180]}, 400)

    def _post_api_whatsapp(self, parsed) -> None:
        # POST /api/whatsapp
        self.handle_api_message()
        queue_realtime.notify("ticket")

    def _post_api_template_error_delivery(self, parsed) -> None:
        # POST /api/template-error-delivery
        self.handle_template_error_delivery()

    def _post_api_outbound_claim(self, parsed) -> None:
        # POST /api/outbound/claim
        self.handle_outbound_claim()

    def _post_inbound_recovery_reply(self, parsed) -> None:
        payload = self.read_authorized_json(120_000)
        if payload is not None:
            result = self.outbound_service.recover_reply(payload)
            self.json_response(result.payload, result.status)

    def _post_api_outbound_heartbeat(self, parsed) -> None:
        payload = self.read_authorized_json()
        if payload is not None:
            result = self.outbound_service.heartbeat(payload, begin=parsed.path.endswith('/begin'))
            self.json_response(result.payload, result.status)

    def _post_api_outbound_result(self, parsed) -> None:
        # POST /api/outbound/result
        self.handle_outbound_result()

    def _post_api_chat_list_sync(self, parsed) -> None:
        # POST /api/chat-list-sync
        self.handle_chat_list_sync()

    def _post_api_group_list_sync(self, parsed) -> None:
        # POST /api/group-list-sync
        self.handle_group_list_sync()

    def _post_api_presence_sync(self, parsed) -> None:
        # POST /api/presence-sync
        self.handle_presence_sync()

    def _post_api_group_participants_sync(self, parsed) -> None:
        # POST /api/group-participants-sync
        self.handle_group_participants_sync()

    def _post_api_group_message_identities_sync(self, parsed) -> None:
        # POST /api/group-message-identities-sync
        self.handle_group_message_identities_sync()

    def _post_api_contact_list_sync(self, parsed) -> None:
        # POST /api/contact-list-sync
        self.handle_contact_list_sync()

    def _post_api_avatar_sync(self, parsed) -> None:
        # POST /api/avatar-sync
        payload = self.read_authorized_json()
        if payload is not None:
            try:
                self.deps.avatars.save(payload)
                self.json_response({"updated":True})
            except (ValueError,TypeError,OSError):
                self.json_response({"error":"Некорректный аватар"},400)

    def _post_api_contact_profile_sync(self, parsed) -> None:
        # POST /api/contact-profile-sync
        self.handle_contact_profile_sync()

    def _post_api_group_refresh_check(self, parsed) -> None:
        # POST /api/group-refresh-check
        self.handle_group_refresh_check()

    def _post_api_connector_state(self, parsed) -> None:
        # POST /api/connector-state
        self.handle_connector_state()

    def _post_api_contact_policy(self, parsed) -> None:
        # POST /api/contact-policy
        self.handle_contact_policy()

    def _post_api_call_permission(self, parsed) -> None:
        # POST /api/call-permission
        self.handle_call_permission()

    def _post_api_chat_messages_sync(self, parsed) -> None:
        # POST /api/chat-messages-sync
        self.handle_chat_messages_sync()

    def _post_api_chat_message_ack(self, parsed) -> None:
        # POST /api/chat-message-ack
        self.handle_chat_message_ack()

    def _post_api_chat_reaction_sync(self, parsed) -> None:
        # POST /api/chat-reaction-sync
        self.handle_chat_reaction_sync()

    def _post_chat_media_send(self, parsed) -> None:
        # POST /chat-media-send
        self.handle_chat_media_send()

    def _post_chat_media_send_raw(self, parsed) -> None:
        # POST /chat-media-send-raw
        self.handle_chat_media_send_raw(parse_qs(parsed.query))

    def _post_chat_forward(self, parsed) -> None:
        # POST /chat-forward
        self.handle_chat_forward()

    def _post_api_whatsapp_action_claim(self, parsed) -> None:
        # POST /api/whatsapp-action/claim
        self.handle_whatsapp_action_claim()

    def _post_api_whatsapp_action_result(self, parsed) -> None:
        # POST /api/whatsapp-action/result
        self.handle_whatsapp_action_result()

    def _post_api_chat_control(self, parsed) -> None:
        # POST /api/chat-control
        self.handle_chat_control()


    def _post_api_account_preferences(self, parsed, form) -> None:
        # POST /api/account-preferences
        current = self.effective_user() or {}
        user_id = int(current.get("id", 0) or 0)
        if user_id <= 0:
            self.json_response({"updated": False, "error": "Требуется вход"}, HTTPStatus.UNAUTHORIZED)
            return
        updates: dict[str, str] = {}
        for key in ("theme", "chat_scale", "compact_chats", "sidebar_width"):
            if key in form:
                updates[key] = str(form.get(key, ""))
        result = self.deps.auth.set_user_preferences(user_id, updates)
        self.deps.auth.record_audit(current, "interface_preferences", object_type="user", object_id=str(user_id), details=", ".join(k for k,v in result.items() if v))
        self.json_response({"updated": all(result.values()) if result else False, "preferences": self.deps.auth.get_user_preferences(user_id), "result": result})

    def _post_conversation_lock(self, parsed, form) -> None:
        # POST /conversation-lock
        chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
        action = str(form.get("action", "claim") or "claim").strip().lower()
        user = self.effective_user() or {}
        if not chat_id:
            self.json_response({"updated": False, "error": "Диалог не выбран"}, HTTPStatus.BAD_REQUEST)
            return
        if action == "claim":
            ok, _lock = self.deps.auth.claim_conversation(
                chat_id, "group" if chat_id.endswith("@g.us") else "chat", user, display_name=work_actor()
            )
            state = self.chat_views.conversation_lock_snapshot(chat_id)
            if ok:
                self.deps.auth.record_audit(user, "conversation_claim", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=work_actor())
                queue_realtime.notify_outbound()
            self.json_response({"updated": ok, "conversation_lock": state}, HTTPStatus.OK if ok else HTTPStatus.LOCKED)
            return
        if action == "release":
            ok, message = self.deps.auth.release_conversation(
                chat_id, user, force=self.is_admin() and form.get("force", "") == "1"
            )
            if ok:
                self.deps.auth.record_audit(user, "conversation_release", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=message)
                queue_realtime.notify_outbound()
            self.json_response({"updated": ok, "message": message, "conversation_lock": self.chat_views.conversation_lock_snapshot(chat_id)}, HTTPStatus.OK if ok else HTTPStatus.LOCKED)
            return
        if action == "transfer":
            try:
                target_user_id = int(form.get("target_user_id", "0") or 0)
            except ValueError:
                target_user_id = 0
            target = self.deps.auth.get_user(target_user_id) if target_user_id > 0 else None
            target_name = ""
            if target:
                target_name = (
                    str(target.get("employee_name", "") or "").strip()
                    or str(target.get("display_name", "") or target.get("username", "")).strip()
                )
            ok, message, _lock = self.deps.auth.transfer_conversation(
                chat_id, user, target_user_id, target_display_name=target_name
            )
            if ok:
                self.deps.auth.record_audit(user, "conversation_transfer", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=f"Передано user_id={target_user_id}")
                queue_realtime.notify_outbound()
            self.json_response({"updated": ok, "message": message, "conversation_lock": self.chat_views.conversation_lock_snapshot(chat_id)}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
            return
        self.json_response({"updated": False, "error": "Неизвестное действие блокировки"}, HTTPStatus.BAD_REQUEST)

    def _post_account_password(self, parsed, form) -> None:
        # POST /account/password
        user = self.current_app_user()
        if not user:
            self.redirect("/login")
            return
        new_password = form.get("new_password", "")
        target = "/account" if user.get("must_change_password") else "/"
        if new_password != form.get("new_password2", ""):
            self.redirect(f"{target}?notice={quote('Новые пароли не совпадают')}")
            return
        ok, notice = self.deps.auth.change_own_password(int(user["id"]), form.get("current_password", ""), new_password)
        if ok:
            self.deps.auth.record_audit(user, "password_changed", object_type="user", object_id=str(user.get("id", "")), details="Пользователь сменил пароль")
            # Смена пароля завершает старые сеансы, поэтому нужно войти снова.
            self.end_app_session()
        else:
            self.redirect(f"{target}?notice={quote(notice)}")

    def _post_admin_users(self, parsed, form) -> None:
        # POST /admin/users
        if not self.require_admin():
            return
        action = form.get("action", "create")
        actor_user = self.effective_user() or {}
        object_id = str(form.get("user_id", "") or "")
        if action == "create":
            temporary = form.get("temporary_password", "") == "1"
            ok, notice, created_id = self.deps.auth.create_user(
                form.get("username", ""), form.get("display_name", ""), form.get("password", ""),
                role=form.get("role", "employee"), must_change_password=temporary,
            )
            object_id = str(created_id or "")
        elif action == "toggle":
            ok, notice = self.deps.auth.set_enabled(self.form_int(form, "user_id"), form.get("enabled", "0") == "1")
        elif action == "role":
            ok, notice = self.deps.auth.set_role(self.form_int(form, "user_id"), form.get("role", "employee"))
        elif action == "password":
            ok, notice = self.deps.auth.set_password(self.form_int(form, "user_id"), form.get("password", ""), must_change_password=form.get("temporary_password", "") == "1")
        elif action == "theme":
            theme = str(form.get("theme", "") or "").strip().lower()
            ok, notice = self.deps.auth.set_user_preference(self.form_int(form, "user_id"), "theme", theme)
            if ok:
                notice = "Тема пользователя сохранена"
        elif action == "employee_link":
            employee_name = form.get("employee_name", "")
            if employee_name and employee_name not in self.deps.employees:
                ok, notice = False, "Сотрудник не найден в списке"
            else:
                ok, notice = self.deps.auth.set_employee_link(self.form_int(form, "user_id"), employee_name)
        elif action == "delete":
            target_user_id = self.form_int(form, "user_id")
            target_user = self.deps.auth.get_user(target_user_id) if target_user_id > 0 else None
            actor_user_id = int(actor_user.get("id", 0) or 0)
            if actor_user_id > 0 and target_user_id == actor_user_id:
                ok, notice = False, "Нельзя удалить собственную учётную запись"
            elif not target_user:
                ok, notice = False, "Пользователь не найден"
            else:
                target_name = str(target_user.get("username") or target_user.get("display_name") or target_user_id)
                ok, delete_notice = self.deps.auth.delete_user(target_user_id)
                notice = (
                    f"Пользователь @{target_name} удалён. Его активные сессии завершены"
                    if ok else delete_notice
                )
        elif action == "revoke_session":
            ok = self.deps.auth.revoke_session_hash(form.get("session_hash", ""))
            notice = "Сессия завершена" if ok else "Сессия уже завершена или не найдена"
            object_id = str(form.get("session_hash", ""))[:12]
        else:
            ok, notice = False, "Неизвестное действие"
        if ok:
            self.deps.auth.record_audit(actor_user, f"user_{action}", object_type="auth", object_id=object_id, details=notice)
        prefix = "" if ok else "Ошибка: "
        self.redirect(f"/admin/users?notice={quote(prefix + notice)}")

    def _post_context_history_apply(self, parsed, form) -> None:
        # POST /context-history/apply
        ticket_id = self.form_int(form, "ticket_id")
        history_id = self.form_int(form, "history_id")
        mode = form.get("mode", "field")
        field_name = form.get("field_name", "problem")
        updated, notice = queue_contextual_tickets.apply_history_item(
            self.deps.store, ticket_id, history_id, field_name, work_actor(), mode=mode
        )
        if not updated and not notice:
            notice = "Не удалось применить запись истории"
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_chat_favorite(self, parsed, form) -> None:
        # POST /chat-favorite
        chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
        if not chat_id:
            self.json_response({"updated": False, "error": "Чат не найден"}, HTTPStatus.BAD_REQUEST)
            return
        favorite = str(form.get("favorite", "0")).lower() in {"1", "true", "yes", "on"}
        updated, canonical = self.deps.set_chat_favorite(chat_id, favorite)
        self.json_response({"updated": updated, "chat_id": canonical, "favorite": favorite},
                           HTTPStatus.OK if updated else HTTPStatus.BAD_REQUEST)

    def _post_group_mute(self, parsed, form) -> None:
        # POST /group-mute
        chat_id = valid_group_id(form.get("chat_id", ""))
        muted = form.get("muted", "0") in {"1", "true", "yes", "on"}
        updated = self.deps.store.set_whatsapp_group_muted(chat_id, muted)
        if not updated:
            self.json_response({"updated": False, "error": "group_not_found"}, HTTPStatus.NOT_FOUND)
        else:
            self.json_response({"updated": True, "chat_id": chat_id, "muted": muted})

    def _post_admin_employees(self, parsed, form) -> None:
        # POST /admin/employees
        if not self.require_admin():
            return
        try:
            count = int(form.get("employee_count", "0"))
        except ValueError:
            count = 0
        if count < 1 or count > self.deps.max_employees:
            self.redirect(f"/admin/employees?notice={quote('Некорректное количество сотрудников')}")
            return
        names = [
            normalize_message(form.get(f"employee_{index}", ""))[:60].strip()
            for index in range(1, count + 1)
        ]
        if any(not name for name in names):
            self.redirect(f"/admin/employees?count={count}&notice={quote('Все имена должны быть заполнены')}")
            return
        if len({name.casefold() for name in names}) != len(names):
            self.redirect(f"/admin/employees?count={count}&notice={quote('Имена сотрудников должны отличаться')}")
            return
        old_names = list(self.deps.employees)
        old_active = active_employee()
        self.deps.store.set_setting("employees_json", json.dumps(names, ensure_ascii=False))
        self.deps.employees[:] = names
        if old_active in old_names:
            old_index = old_names.index(old_active)
            if old_index < len(names):
                self.deps.store.set_setting("active_employee", names[old_index])
            else:
                self.deps.store.set_setting("active_employee", names[0])
        elif old_active not in names:
            self.deps.store.set_setting("active_employee", names[0])
        self.redirect(f"/admin/employees?notice={quote('Список сотрудников сохранён')}")

    def _post_admin_ticket_delete(self, parsed, form) -> None:
        # POST /admin/ticket-delete
        if not self.require_admin():
            return
        ticket_id = self.form_int(form, "ticket_id")
        deleted = self.deps.store.delete_ticket(ticket_id, f"Администратор · {work_actor()}")
        notice = (
            f"Заявка №{ticket_id} удалена только из системы. Сообщение пользователю не отправлялось"
            if deleted else "Заявка не найдена"
        )
        self.redirect(f"/?notice={quote(notice)}")

    def _post_admin_contacts(self, parsed, form) -> None:
        # POST /admin/contacts
        if not self.require_admin():
            return
        action = form.get("action", "save")
        if action == "delete":
            contact_chat_id = valid_chat_id(form.get("chat_id", ""))
            existing_contact = self.deps.store.manual_whatsapp_contact(contact_chat_id, "")
            deleted = self.deps.store.delete_whatsapp_contact(contact_chat_id)
            if deleted:
                # После удаления из исключений следующий текст снова должен
                # запускать обычную автоматику, а не продолжать старый контекст
                # заявки, существовавший до добавления пользователя в контакты.
                reset_keys = {contact_chat_id}
                if existing_contact:
                    contact_phone = normalize_phone(str(existing_contact.get("phone", "")))
                    if contact_phone:
                        reset_keys.add(f"{contact_phone.lstrip('+')}@c.us")
                for reset_key in reset_keys:
                    if reset_key:
                        self.deps.store.set_conversation_context(reset_key, MENU_CONTEXT, 0, False)
            notice = "Контакт удалён. Автообработка для него снова включена" if deleted else "Контакт не найден"
            self.redirect(f"/admin/contacts?notice={quote(notice)}")
            return
        name = normalize_message(form.get("name", ""))[:100]
        phone = normalize_phone(form.get("phone", ""))
        chat_id = f"{phone.lstrip('+')}@c.us" if phone else ""
        existing = self.deps.store.manual_whatsapp_contact(chat_id, phone) if phone else None
        if existing:
            notice = f"Номер {display_phone(phone)} уже добавлен как {existing.get('name') or 'контакт'}"
            self.redirect(f"/admin/contacts?notice={quote(notice)}")
            return
        saved = self.deps.store.save_whatsapp_contact(name, phone, chat_id)
        notice = "Контакт добавлен" if saved else "Проверьте имя и мобильный номер"
        self.redirect(f"/admin/contacts?notice={quote(notice)}")

    def _post_admin_manual(self, parsed, form) -> None:
        # POST /admin/manual
        if not self.require_admin():
            return
        employee = form.get("employee", "")
        if employee not in assignment_names():
            employee = work_actor()
        category = form.get("category", "general")
        if category not in self.deps.categories:
            category = "general"
        status = form.get("status", "new")
        if status not in STATUSES:
            status = "new"
        ticket_id = self.deps.store.create_ticket(
            {
                "source": "admin_manual",
                "sender": employee,
                "category": category,
                "priority": form.get("priority", "normal"),
                "title": normalize_message(form.get("title", ""))[:160] or "Ручная заявка",
                "summary": normalize_message(form.get("summary", ""))[:2000],
                "original_text": normalize_message(form.get("summary", ""))[:2000],
                "status": status,
                "assigned_to": employee,
            }
        )
        self.redirect(f"/ticket?id={ticket_id}")

    def _post_admin_system(self, parsed, form) -> None:
        # POST /admin/system
        if not self.require_admin():
            return
        action = form.get("action", "")
        if action == "cleanup":
            result = queue_workflow.cleanup_retention(self.deps.store)
            notice = f"Очистка завершена: сообщений {result.get('messages',0)}, медиа {result.get('media_rows',0)}, заявок {result.get('tickets',0)}"
        elif action == "retry_failed":
            count = self.deps.store.retry_failed_outbound()
            notice = f"На повторную отправку возвращено: {count}"
        elif action == "backup_now":
            ok, info = self.deps.create_scheduled_backup("manual_admin")
            notice = f"Резервная копия создана: {info}" if ok else f"Ошибка резервной копии: {info}"
        elif action == "backup_settings":
            try:
                hours=max(1,min(168,int(form.get("backup_hours","24"))))
                keep=max(2,min(90,int(form.get("backup_keep","14"))))
                self.deps.store.set_setting("scheduled_backup_hours",str(hours)); self.deps.store.set_setting("scheduled_backup_keep",str(keep))
                notice=f"Расписание сохранено: каждые {hours} ч., хранить {keep} копий"
            except ValueError:
                notice="Некорректные параметры расписания"
        else:
            notice = "Неизвестное действие"
        self.deps.auth.record_audit(self.effective_user() or {}, f"system_{action}", object_type="system", details=notice)
        try:
            with self.deps.monitor_log_path.open("a", encoding="utf-8") as log:
                log.write(f"{datetime.now(timezone.utc).isoformat()} ADMIN {notice}\n")
        except OSError:
            pass
        self.redirect(f"/admin/system?notice={quote(notice)}")

    def _post_admin_groups(self, parsed, form) -> None:
        # POST /admin/groups
        if not self.require_admin():
            return
        action = form.get("action", "save")
        if action == "refresh":
            self.deps.store.set_setting("group_refresh_request", secrets.token_urlsafe(18))
            notice = "Запрос отправлен QR-коннектору. Список обновится примерно через 5–7 секунд"
            self.redirect(f"/groups?notice={quote(notice)}&refresh=1")
            return
        if action == "delete":
            deleted = self.deps.store.delete_whatsapp_group(valid_group_id(form.get("chat_id", "")))
            notice = "Группа удалена" if deleted else "Группа не найдена"
        elif action == "add_discovered":
            chat_id = valid_group_id(form.get("chat_id", ""))
            discovered = next(
                (item for item in self.deps.store.list_discovered_whatsapp_groups() if str(item.get("chat_id", "")) == chat_id),
                None,
            )
            saved = self.deps.store.save_whatsapp_group(str(discovered.get("name", "")), chat_id) if discovered else None
            notice = "Группа добавлена" if saved else "Группа не найдена в списке WhatsApp"
        else:
            saved = self.deps.store.save_whatsapp_group(
                form.get("name", ""),
                valid_group_id(form.get("chat_id", "")),
            )
            notice = "Группа добавлена" if saved else "Укажите название и корректный WhatsApp ID группы"
        self.redirect(f"/groups?notice={quote(notice)}")

    def _post_admin_errors(self, parsed, form) -> None:
        # POST /admin/errors
        if not self.require_admin():
            return
        report_id = self.form_int(form, "report_id")
        status = form.get("status", "new")
        admin_note = normalize_message(form.get("admin_note", ""))[:3000]
        updated = self.deps.store.update_error_report(report_id, status, admin_note, f"Администратор · {work_actor()}")
        notice = "Репорт обновлён" if updated else "Репорт не найден"
        self.redirect(f"/admin/error?id={report_id}&notice={quote(notice)}")

    def _post_admin_categories(self, parsed, form) -> None:
        if not self.require_admin():
            return
        try:
            self.deps.menu_config.update_from_form(form)
        except ValueError as error:
            self.redirect(f"/admin/categories?notice={quote(str(error))}")
            return
        self.redirect(f"/admin/categories?notice={quote('Сохранено. Меню бота обновлено')}")

    def _post_active_employee(self, parsed, form) -> None:
        # POST /active-employee
        self.json_response({"updated": False, "error": "Ручной выбор смены отключён. Сотрудник становится на смену автоматически после входа."}, HTTPStatus.GONE)

    def _post_handoff(self, parsed, form) -> None:
        # POST /handoff
        ticket_id = self.form_int(form, "ticket_id")
        action = form.get("action", "transfer")
        updated = self.deps.store.set_ticket_handoff(
            ticket_id, action != "accept", work_actor()
        )
        notice = (
            "Заявка передана следующей смене"
            if updated and action != "accept"
            else "Заявка принята текущей сменой"
            if updated
            else "Не удалось изменить передачу смене"
        )
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_status(self, parsed, form) -> None:
        # POST /status
        ticket_id = self.form_int(form, "ticket_id")
        ticket = self.deps.store.get_ticket(ticket_id)
        assignee = form.get("employee", "")
        if assignee not in assignment_names():
            assignee = str((ticket or {}).get("assigned_to", "")) or work_actor()
        requested_status = form.get("status", "")
        close_reason = form.get("close_reason", "")
        close_comment = form.get("close_comment", "")
        valid_close, close_error = queue_workflow.validate_close(self.deps.store, ticket_id, requested_status, close_reason, close_comment)
        if not valid_close:
            self.redirect(f"/ticket?id={ticket_id}&notice={quote(close_error)}")
            return
        updated, _message_id = self.deps.update_ticket_status(
            ticket_id,
            requested_status,
            work_actor(),
            assignee,
            allow_reopen=self.is_admin(),
            close_reason=close_reason,
            close_comment=close_comment,
        )
        if updated:
            queue_workflow.record_close_meta(self.deps.store, ticket_id, requested_status, close_reason, close_comment, work_actor())
        notice = "" if updated else "Отработанную заявку может вернуть в работу только администратор"
        suffix = f"&notice={quote(notice)}" if notice else ""
        self.redirect(f"/ticket?id={ticket_id}{suffix}")

    def _post_quick_status(self, parsed, form) -> None:
        # POST /quick-status
        ticket_id = self.form_int(form, "ticket_id")
        ticket = self.deps.store.get_ticket(ticket_id)
        actor = work_actor()
        requested_status = form.get("status", "")
        assignee = actor if requested_status == "in_progress" else (str((ticket or {}).get("assigned_to", "")) or actor)
        close_reason = form.get("close_reason", "")
        close_comment = form.get("close_comment", "")
        valid_close, close_error = queue_workflow.validate_close(self.deps.store, ticket_id, requested_status, close_reason, close_comment)
        if not valid_close:
            self.json_response({"updated":False,"error":close_error}, HTTPStatus.BAD_REQUEST)
            return
        updated, message_id = self.deps.update_ticket_status(
            ticket_id,
            requested_status,
            actor,
            assignee,
            allow_reopen=self.is_admin(),
            close_reason=close_reason,
            close_comment=close_comment,
        )
        if updated:
            queue_workflow.record_close_meta(self.deps.store, ticket_id, requested_status, close_reason, close_comment, actor)
        self.json_response(
            {
                "updated": updated,
                "ticket_id": ticket_id,
                "notification_queued": bool(message_id),
            },
            HTTPStatus.OK if updated else (
                HTTPStatus.FORBIDDEN
                if ticket and str(ticket.get("status", "")) in {"done", "invalid"} and not self.is_admin()
                else HTTPStatus.BAD_REQUEST
            ),
        )

    def _post_quick_priority(self, parsed, form) -> None:
        # POST /quick-priority
        ticket_id = self.form_int(form, "ticket_id")
        updated = self.deps.store.update_priority(
            ticket_id,
            form.get("priority", ""),
            work_actor(),
        )
        self.json_response(
            {"updated": updated, "ticket_id": ticket_id},
            HTTPStatus.OK if updated else HTTPStatus.BAD_REQUEST,
        )

    def _post_classification(self, parsed, form) -> None:
        # POST /classification
        ticket_id = self.form_int(form, "ticket_id")
        updated = self.deps.store.update_priority(
            ticket_id,
            form.get("priority", ""),
            work_actor(),
        )
        notice = "Приоритет сохранён" if updated else "Не удалось изменить приоритет"
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_send_reply(self, parsed, form) -> None:
        # POST /send-reply
        ticket_id = self.form_int(form, "ticket_id")
        ticket = self.deps.store.get_ticket(ticket_id)
        linked_chat_id = valid_conversation_id(str((ticket or {}).get("chat_id", "") or ""))
        if linked_chat_id and not self.claim_conversation_write(linked_chat_id):
            return
        if form.get("send_custom") == "1":
            reply = form.get("custom_reply", "")
        else:
            reply = form.get("quick_reply", "")
        message_id = self.deps.store.queue_outbound_message(
            ticket_id,
            reply,
            work_actor(),
        )
        if message_id:
            queue_realtime.notify_outbound()
        notice = (
            "Ответ поставлен в очередь отправки через WhatsApp"
            if message_id
            else "Не удалось подготовить ответ: проверьте текст и WhatsApp-адрес"
        )
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_chat_send(self, parsed, form) -> None:
        # POST /chat-send
        chat_id = valid_chat_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(chat_id):
            return
        message_id = self.deps.store.queue_direct_message(
            chat_id,
            form.get("message", ""),
            work_actor(),
            reply_to_key=normalize_message(form.get("reply_to", ""))[:160],
            request_id=form.get("request_id", ""),
        )
        if message_id:
            queue_realtime.notify_outbound()
        # Отправка сообщения сама по себе больше не меняет состояние
        # автоответчика. Его включает/выключает только сотрудник кнопкой
        # в карточке диалога.
        self.json_response(
            {"queued": bool(message_id), "message_id": message_id},
            HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
        )

    def _post_chat_mode(self, parsed, form) -> None:
        # POST /chat-mode
        raw_chat_id = valid_chat_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(raw_chat_id):
            return
        if not raw_chat_id:
            self.json_response({"updated": False, "error": "Выберите личный чат"}, HTTPStatus.BAD_REQUEST)
            return
        chat_id = self.deps.store.canonical_whatsapp_chat_id(raw_chat_id) or raw_chat_id
        if self.deps.store.manual_whatsapp_contact(chat_id, ""):
            self.json_response({
                "updated": False,
                "manual_contact": True,
                "error": "Контакт добавлен в админке — автоответчик для него отключён постоянно",
            }, HTTPStatus.CONFLICT)
            return

        action = normalize_message(form.get("action", "reset")).casefold()

        if action in {"disable", "pause", "off"}:
            actor = work_actor()
            self.deps.store.enable_manual_chat_mode(chat_id, actor)
            if raw_chat_id != chat_id:
                self.deps.store.enable_manual_chat_mode(raw_chat_id, actor)
            manual_mode = self.deps.store.manual_chat_mode(chat_id) or self.deps.store.manual_chat_mode(raw_chat_id) or {}
            queue_realtime.notify("chat")
            self.json_response({
                "updated": True,
                "auto_reply_enabled": False,
                "manual_mode": manual_mode,
                "message": "Автоответы для этого пользователя отключены. Входящие сообщения продолжат отображаться в системе.",
            })
            return

        if action in {"enable", "resume", "on"}:
            actor = work_actor()
            self.deps.store.disable_manual_chat_mode(chat_id, actor)
            if raw_chat_id != chat_id:
                self.deps.store.disable_manual_chat_mode(raw_chat_id, actor)
            queue_realtime.notify("chat")
            self.json_response({
                "updated": True,
                "auto_reply_enabled": True,
                "manual_mode": {},
                "message": "Автоответы для этого пользователя включены.",
            })
            return

        if action != "reset":
            self.json_response({"updated": False, "error": "Неизвестное действие"}, HTTPStatus.BAD_REQUEST)
            return

        # Existing per-user hard reset. It also guarantees that auto-replies
        # are enabled again after the reset.
        self.deps.store.disable_manual_chat_mode(chat_id, work_actor())
        if raw_chat_id != chat_id:
            self.deps.store.disable_manual_chat_mode(raw_chat_id, work_actor())
        try:
            with self.deps.store.connection() as db:
                db.execute(
                    "UPDATE contact_meta SET auto_reply_blocked = 0 WHERE chat_id IN (?, ?)",
                    (chat_id, raw_chat_id),
                )
                db.execute(
                    "DELETE FROM auto_reply_cooldowns WHERE contact_key IN (?, ?)",
                    (chat_id, raw_chat_id),
                )
        except Exception as exc:
            print(f"Bot hard-reset metadata warning for {chat_id}: {exc}", flush=True)

        for reset_key in dict.fromkeys((chat_id, raw_chat_id)):
            if not reset_key:
                continue
            self.deps.store.clear_conversation_draft(reset_key)
            try:
                queue_dialog_flow.save(self.deps.store, reset_key, {})
            except Exception as exc:
                print(f"Bot hard-reset dialog-flow warning for {reset_key}: {exc}", flush=True)
            try:
                queue_contextual_tickets.reset_context(self.deps.store, reset_key, "")
            except Exception as exc:
                print(f"Bot hard-reset contextual-flow warning for {reset_key}: {exc}", flush=True)
            self.deps.store.set_conversation_context(reset_key, MENU_CONTEXT, 0, False)

        reset_token = secrets.token_urlsafe(18)
        self.deps.store.set_setting(f"bot_hard_reset_v1:{chat_id}", reset_token)
        if raw_chat_id != chat_id:
            self.deps.store.set_setting(f"bot_hard_reset_v1:{raw_chat_id}", reset_token)

        queue_realtime.notify("chat")
        print(f"Bot hard reset completed for {chat_id}", flush=True)
        self.json_response({
            "updated": True,
            "bot_restarted": True,
            "hard_reset": True,
            "reset_token": reset_token,
            "auto_reply_enabled": True,
            "manual_mode": {},
            "message": "Бот полностью перезагружен для этого пользователя. Автоответы включены.",
        })

    def _post_group_send(self, parsed, form) -> None:
        # POST /group-send
        chat_id = valid_group_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(chat_id):
            return
        mention_ids = [
            item for item in form.get("mentions", "").split(",")
            if valid_conversation_id(item.strip()) or valid_group_id(item.strip())
        ]
        message_id = self.deps.store.queue_direct_message(
            chat_id,
            form.get("message", ""),
            work_actor(),
            mention_ids,
            reply_to_key=normalize_message(form.get("reply_to", ""))[:160],
            request_id=form.get("request_id", ""),
        )
        if message_id:
            queue_realtime.notify_outbound()
        self.json_response(
            {"queued": bool(message_id), "message_id": message_id},
            HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
        )

    def _post_chat_action(self, parsed, form) -> None:
        # POST /chat-action
        chat_id = valid_conversation_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(chat_id):
            return
        message_key = normalize_message(form.get("message_id", ""))[:160]
        action = form.get("action", "")
        message = self.deps.store.get_whatsapp_message(chat_id, message_key) if chat_id and message_key else None
        if not message or bool(message.get("deleted")):
            self.json_response({"queued": False, "error": "Недоступное сообщение"}, HTTPStatus.BAD_REQUEST)
            return
        if action in {"edit", "delete"} and not bool(message.get("from_me")):
            self.json_response({"queued": False, "error": "Изменять и удалять можно только наши сообщения"}, HTTPStatus.BAD_REQUEST)
            return
        if action not in {"edit", "delete", "react"}:
            self.json_response({"queued": False, "error": "Неизвестное действие"}, HTTPStatus.BAD_REQUEST)
            return
        action_body = form.get("message", "")
        if action == "react":
            action_body = str(action_body or "")[:32]
            if action_body and action_body not in self.deps.reaction_emojis:
                self.json_response({"queued": False, "error": "Эта реакция пока не поддерживается"}, HTTPStatus.BAD_REQUEST)
                return
        if action == "delete":
            action_body = json.dumps(
                {
                    "body": str(message.get("body", "") or ""),
                    "timestamp": int(message.get("message_timestamp", 0) or 0),
                    "media_name": str(message.get("media_name", "") or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        action_id = self.deps.store.queue_whatsapp_action(action, chat_id, message_key, action_body, work_actor())
        if action_id:
            queue_realtime.notify_outbound()
        self.json_response(
            {"queued": bool(action_id), "action_id": action_id},
            HTTPStatus.OK if action_id else HTTPStatus.BAD_REQUEST,
        )

    def _post_manual(self, parsed, form) -> None:
        # POST /manual
        title = form.get("title", "").strip() or "Ручная заявка"
        reference = form.get("reference", "").strip()
        comment = form.get("comment", "").strip()
        summary_parts = [title]
        if reference:
            summary_parts.append(f"№ {reference}")
        if comment:
            summary_parts.append(comment)
        employee = form.get("employee", "")
        if employee not in assignment_names():
            employee = work_actor()
        manual_payload = {
            "source": "manual",
            "sender": employee,
            "category": "general",
            "priority": form.get("priority", "normal"),
            "title": title,
            "summary": " · ".join(summary_parts),
            "original_text": comment,
            "status": form.get("status", "new"),
            "assigned_to": employee,
        }
        similar = self.deps.store.find_similar_open_ticket(manual_payload, hours=72)
        if similar:
            similar_id = int(similar.get("id", 0) or 0)
            self.redirect(f"/ticket?id={similar_id}&notice={quote(f'Похожая открытая заявка уже существует: №{similar_id}. Новая заявка не создана')}")
            return
        ticket_id = self.deps.store.create_ticket(manual_payload)
        self.redirect(f"/ticket?id={ticket_id}")

    # signed-in, before feature modules
    EARLY_GET_ROUTES = {
        "/search": _get_search,
        "/bookmarks": _get_bookmarks,
        "/api/message-bookmarks": _get_api_message_bookmarks,
        "/api/bookmarks": _get_api_message_bookmarks,
        "/api/global-search": _get_api_global_search,
        "/api/account-preferences": _get_api_account_preferences,
        "/api/realtime-wait": _get_api_realtime_wait,
    }

    # after feature modules; administrators only (others are sent to /admin/login)
    ADMIN_GET_ROUTES = {
        "/admin": _get_admin_home,
        "/admin/": _get_admin_home,
        "/admin/users": _get_admin_users,
        "/admin/employees": _get_admin_employees,
        "/admin/manual": _get_admin_manual,
        "/admin/menu": _get_admin_menu,
        "/admin/categories": _get_admin_categories,
        "/admin/errors": _get_admin_errors,
        "/admin/error": _get_admin_error,
        "/admin/contacts": _get_admin_contacts,
        "/admin/audit": _get_admin_audit,
        "/admin/manager": _get_admin_manager,
        "/admin/api-keys": _get_admin_api_keys,
        "/admin/tickets-export.csv": _get_admin_tickets_export,
        "/admin/system": _get_admin_system,
        "/api/admin/contact-search": _get_api_admin_contact_search,
        "/api/admin-page": _get_api_admin_page,
        "/admin/groups": _get_admin_groups,
        "/admin/whatsapp": _get_to_admin_home,
        "/api/admin/connector-state": _get_connector_state,
        "/qr": _get_to_admin_home,
    }

    # after feature modules; public paths are listed in do_GET
    GET_ROUTES = {
        "/login": _get_login,
        "/accounts": _get_accounts,
        "/register": _get_register,
        "/forgot-password": _get_forgot_password,
        "/setup": _get_setup,
        "/logout": _get_logout,
        "/account": _get_account,
        "/me": _get_me,
        "/": _get_dashboard,
        "/admin/login": _get_admin_login,
        "/admin/logout": _get_admin_logout,
        "/ticket": _get_ticket,
        "/whatsapp": _get_whatsapp,
        "/groups": _get_groups,
        "/api/integration/ping": _get_api_integration_ping,
        "/api/integration/tickets": _get_api_integration_tickets,
        "/api/dashboard-data": _get_api_dashboard_data,
        "/api/ticket-page": _get_api_ticket_page,
        "/api/page-context": _get_api_page_context,
        "/api/app-page": _get_api_app_page,
        "/api/groups-page": _get_api_groups_page,
        "/api/chat-state": _get_api_chat_state,
        "/api/group-state": _get_api_group_state,
        "/api/chat-history": _get_api_chat_history,
        "/api/chat-action-status": _get_api_chat_action_status,
        "/api/notifications": _get_api_notifications,
        "/api/conversation-users": _get_api_conversation_users,
        "/api/chat-media": _get_api_chat_media,
        "/favicon.ico": _get_favicon,
        "/health/connector": _get_health_connector,
        "/health": _get_health,
    }

    # before sign-in
    PUBLIC_POST_ROUTES = {
        "/login": _post_login,
        "/register": _post_register,
        "/forgot-password": _post_forgot_password,
        "/setup": _post_setup,
    }

    # signed-in, before feature modules and the maintenance gate
    EARLY_POST_ROUTES = {
        "/api/global-search": _post_api_global_search,
        "/api/message-bookmarks": _post_api_message_bookmarks,
        "/api/bookmarks": _post_api_message_bookmarks,
        "/accounts/switch": _post_accounts_switch,
        "/admin/api-keys": _post_admin_api_keys,
    }

    # connector/JSON endpoints that read their own request body
    SERVICE_POST_ROUTES = {
        "/api/system-warning-action": _post_api_system_warning_action,
        "/api/media-stage": _post_api_media_stage,
        "/api/whatsapp": _post_api_whatsapp,
        "/api/inbound-recovery-reply": _post_inbound_recovery_reply,
        "/api/template-error-delivery": _post_api_template_error_delivery,
        "/api/outbound/claim": _post_api_outbound_claim,
        "/api/outbound/heartbeat": _post_api_outbound_heartbeat,
        "/api/outbound/begin": _post_api_outbound_heartbeat,
        "/api/outbound/result": _post_api_outbound_result,
        "/api/chat-list-sync": _post_api_chat_list_sync,
        "/api/group-list-sync": _post_api_group_list_sync,
        "/api/presence-sync": _post_api_presence_sync,
        "/api/group-participants-sync": _post_api_group_participants_sync,
        "/api/group-message-identities-sync": _post_api_group_message_identities_sync,
        "/api/contact-list-sync": _post_api_contact_list_sync,
        "/api/avatar-sync": _post_api_avatar_sync,
        "/api/contact-profile-sync": _post_api_contact_profile_sync,
        "/api/group-refresh-check": _post_api_group_refresh_check,
        "/api/connector-state": _post_api_connector_state,
        "/api/contact-policy": _post_api_contact_policy,
        "/api/call-permission": _post_api_call_permission,
        "/api/chat-messages-sync": _post_api_chat_messages_sync,
        "/api/chat-message-ack": _post_api_chat_message_ack,
        "/api/chat-reaction-sync": _post_api_chat_reaction_sync,
        "/chat-media-send": _post_chat_media_send,
        "/chat-media-send-raw": _post_chat_media_send_raw,
        "/chat-forward": _post_chat_forward,
        "/api/whatsapp-action/claim": _post_api_whatsapp_action_claim,
        "/api/whatsapp-action/result": _post_api_whatsapp_action_result,
        "/api/chat-control": _post_api_chat_control,
    }

    # form posts that are not written to the admin form audit
    FORM_POST_ROUTES = {
        "/api/account-preferences": _post_api_account_preferences,
    }

    # form posts recorded by audit_admin_form_submission
    AUDITED_FORM_POST_ROUTES = {
        "/conversation-lock": _post_conversation_lock,
        "/account/password": _post_account_password,
        "/admin/users": _post_admin_users,
        "/context-history/apply": _post_context_history_apply,
        "/chat-favorite": _post_chat_favorite,
        "/group-mute": _post_group_mute,
        "/admin/employees": _post_admin_employees,
        "/admin/ticket-delete": _post_admin_ticket_delete,
        "/admin/contacts": _post_admin_contacts,
        "/admin/manual": _post_admin_manual,
        "/admin/system": _post_admin_system,
        "/admin/groups": _post_admin_groups,
        "/admin/errors": _post_admin_errors,
        "/admin/categories": _post_admin_categories,
        "/active-employee": _post_active_employee,
        "/handoff": _post_handoff,
        "/status": _post_status,
        "/quick-status": _post_quick_status,
        "/quick-priority": _post_quick_priority,
        "/classification": _post_classification,
        "/send-reply": _post_send_reply,
        "/chat-send": _post_chat_send,
        "/chat-mode": _post_chat_mode,
        "/group-send": _post_group_send,
        "/chat-action": _post_chat_action,
        "/manual": _post_manual,
    }

    def read_body(self, max_bytes: int) -> bytes | None:
        """The whole request body, read from the socket at most once (the CSRF check
        may need it before the route). None if missing, malformed or over max_bytes."""
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached if len(cached) <= max_bytes else None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > max_bytes:
            return None
        self._body_cache = self.rfile.read(length)
        return self._body_cache

    def body_already_read(self) -> bool:
        return getattr(self, "_body_cache", None) is not None

    def read_json_body(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        try:
            raw = self.read_body(max_bytes)
            if raw is None:
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_authorized_json(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        supplied = self.headers.get("X-Webhook-Token", "").encode("utf-8", "surrogateescape")
        if not secrets.compare_digest(supplied, self.deps.webhook_token.encode("utf-8")):
            self.json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return None
        try:
            raw = self.read_body(max_bytes)
            if raw is None:
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_form(self) -> dict[str, str]:
        raw = self.read_body(1_000_000)
        if raw is None:
            return {}
        body = raw.decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(body).items()}

    def csrf_check_passed(self, path: str) -> bool:
        """Signed-in POSTs must carry the user's token: X-CSRF-Token header or csrf_token field."""
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied and self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            supplied = self.read_form().get("csrf_token", "")
        if self.deps.csrf_token_valid(supplied):
            return True
        message = "Страница устарела. Обновите её и повторите действие"
        if path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or "X-CSRF-Token" in self.headers:
            self.json_response({"error": message, "csrf": False}, HTTPStatus.FORBIDDEN)
        else:
            self.redirect("/?notice=" + quote(message))
        return False

    @staticmethod
    def form_int(form: dict[str, str], key: str) -> int:
        try:
            return int(form.get(key, "0"))
        except ValueError:
            return 0

    @staticmethod
    def query_int(query: dict[str, list[str]], key: str) -> int:
        try:
            return int(query.get(key, ["0"])[0])
        except ValueError:
            return 0

    def file_response(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def cached_file_response(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(body)

    def html_response(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def json_response(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()
