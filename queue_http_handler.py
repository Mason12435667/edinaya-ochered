from __future__ import annotations

from typing import Any
from http import HTTPStatus
from pathlib import Path
import json
import secrets
import threading

# The bookmarks live in existing app_settings; no new SQLite schema or media changes.
_BOOKMARKS_LOCK = threading.RLock()
_BOOKMARKS_MAX = 300


def _bookmark_setting(user_id: int) -> str:
    return f"whatsapp_message_bookmarks_v1:{int(user_id)}"


def _load_message_bookmarks(user_id: int) -> list[dict[str, object]]:
    if user_id <= 0:
        return []
    raw = STORE.get_setting(_bookmark_setting(user_id), "[]")
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


import queue_transit
import queue_source_sync
import queue_ticket_views
import queue_dialog_flow

APP_MODULE: Any = None

def bind(context: dict[str, Any], app_module: Any) -> None:
    global APP_MODULE
    APP_MODULE = app_module
    protected = {"bind", "APP_MODULE", "Any", "QueueHTTPHandlerMixin"}
    for name, value in context.items():
        if name.startswith("__") or name in protected:
            continue
        globals()[name] = value


class QueueHTTPHandlerMixin:
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
        # В 1.00.6.15 локальный Windows-режим тоже использует настоящую
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

    @staticmethod
    def auth_slot_cookie_name(slot: int) -> str:
        return f"{AUTH_SESSION_COOKIE}_{int(slot)}"

    def active_auth_slot(self, cookies=None) -> int:
        cookies = cookies or self.auth_cookie_jar()
        morsel = cookies.get(f"{AUTH_SESSION_COOKIE}_active")
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
            user = AUTH.session_user(token, touch=touch)
            if user:
                result.append({"slot": slot, "token": token, "user": user, "active": slot == active_slot})
        legacy = cookies.get(AUTH_SESSION_COOKIE)
        legacy_token = str(legacy.value).strip() if legacy else ""
        if legacy_token and legacy_token not in seen:
            user = AUTH.session_user(legacy_token, touch=touch)
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
        legacy = cookies.get(AUTH_SESSION_COOKIE)
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
        legacy = cookies.get(AUTH_SESSION_COOKIE)
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
        user = AUTH.session_user(token) if token else None
        if not user:
            for item in self.saved_account_sessions(touch=False):
                candidate = str(item.get("token") or "")
                if not candidate or candidate == token:
                    continue
                user = AUTH.session_user(candidate)
                if user:
                    break
        self._queue_auth_user = user if user else False
        return user

    def legacy_admin_user(self) -> dict[str, object] | None:
        username = self.authenticated_user().casefold()
        if (bool(username) and username == ADMIN_USER) or self.has_admin_session():
            return {
                "id": 0,
                "username": ADMIN_USER,
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
        elif self.path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or self.path.startswith("/chat"):
            self.json_response({"error": "authentication_required"}, HTTPStatus.UNAUTHORIZED)
        else:
            self.redirect("/login")
        return False

    def local_setup_allowed(self) -> bool:
        return bool(
            LOCAL_MODE
            and not AUTH.has_users()
            and self.client_address
            and self.client_address[0] in {"127.0.0.1", "::1"}
        )

    @staticmethod
    def service_post_path(path: str) -> bool:
        return path in {
            "/api/media-stage",
            "/api/whatsapp",
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
            "/api/keden/claim",
            "/api/keden/result",
        }

    def _session_cookie_header(self, name: str, value: str, *, max_age: int | None = None) -> str:
        secure = "" if LOCAL_MODE else "Secure; "
        age = AUTH_SESSION_MAX_AGE if max_age is None else max(0, int(max_age))
        return f"{name}={value}; Path=/; Max-Age={age}; HttpOnly; {secure}SameSite=Lax"

    def start_app_session(self, token: str, location: str = "/") -> None:
        token = str(token or "").strip()
        new_user = AUTH.session_user(token, touch=False) if token else None
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
            existing_user = AUTH.session_user(existing_token, touch=False)
            if existing_user:
                slots[slot] = (existing_token, existing_user)
            else:
                invalid_slots.append(slot)

        migrated: list[tuple[int, str]] = []
        legacy = cookies.get(AUTH_SESSION_COOKIE)
        legacy_token = str(legacy.value).strip() if legacy else ""
        legacy_user = AUTH.session_user(legacy_token, touch=False) if legacy_token else None
        new_user_id = int(new_user.get("id", 0) or 0)
        slot = 0
        for existing_slot, (existing_token, existing_user) in slots.items():
            if int(existing_user.get("id", 0) or 0) == new_user_id:
                slot = existing_slot
                if existing_token != token:
                    AUTH.revoke_session(existing_token)
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
                    AUTH.revoke_session(old_token)

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        for migrated_slot, migrated_token in migrated:
            self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(migrated_slot), migrated_token))
        migrated_slots = {migrated_slot for migrated_slot, _migrated_token in migrated}
        for invalid_slot in invalid_slots:
            if invalid_slot != slot and invalid_slot not in migrated_slots:
                self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(invalid_slot), "", max_age=0))
        self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(slot), token))
        self.send_header("Set-Cookie", self._session_cookie_header(f"{AUTH_SESSION_COOKIE}_active", str(slot)))
        self.send_header("Set-Cookie", self._session_cookie_header(AUTH_SESSION_COOKIE, "", max_age=0))
        self.end_headers()

    def switch_app_session(self, slot: int, location: str = "/") -> bool:
        cookies = self.auth_cookie_jar()
        if slot == 0:
            morsel = cookies.get(AUTH_SESSION_COOKIE)
        elif 1 <= slot <= self.MULTI_ACCOUNT_SLOTS:
            morsel = cookies.get(self.auth_slot_cookie_name(slot))
        else:
            morsel = None
        token = str(morsel.value).strip() if morsel else ""
        user = AUTH.session_user(token, touch=False) if token else None
        if not user:
            return False
        if str(user.get("role", "")) == "employee":
            activate_employee_shift(user)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if slot > 0:
            self.send_header("Set-Cookie", self._session_cookie_header(f"{AUTH_SESSION_COOKIE}_active", str(slot)))
        else:
            self.send_header("Set-Cookie", self._session_cookie_header(f"{AUTH_SESSION_COOKIE}_active", "", max_age=0))
        self.end_headers()
        return True

    def end_app_session(self) -> None:
        token = self.auth_cookie_token()
        current_slot = self.current_auth_slot()
        sessions = self.saved_account_sessions(touch=False)
        if token:
            user = AUTH.session_user(token, touch=False)
            if user:
                AUTH.release_user_locks(int(user.get("id", 0) or 0), str(user.get("username", "") or ""))
            AUTH.revoke_session(token)
        remaining = [item for item in sessions if str(item.get("token") or "") != token]
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/accounts" if remaining else "/login")
        secure = "" if LOCAL_MODE else "Secure; "
        if current_slot > 0:
            self.send_header("Set-Cookie", self._session_cookie_header(self.auth_slot_cookie_name(current_slot), "", max_age=0))
        else:
            self.send_header("Set-Cookie", self._session_cookie_header(AUTH_SESSION_COOKIE, "", max_age=0))
        if remaining:
            next_user = remaining[0].get("user") if isinstance(remaining[0].get("user"), dict) else {}
            if str(next_user.get("role", "")) == "employee":
                activate_employee_shift(next_user)
            next_slot = int(remaining[0].get("slot", 0) or 0)
            if next_slot > 0:
                self.send_header("Set-Cookie", self._session_cookie_header(f"{AUTH_SESSION_COOKIE}_active", str(next_slot)))
            else:
                self.send_header("Set-Cookie", self._session_cookie_header(f"{AUTH_SESSION_COOKIE}_active", "", max_age=0))
        else:
            self.send_header("Set-Cookie", self._session_cookie_header(f"{AUTH_SESSION_COOKIE}_active", "", max_age=0))
            self.send_header("Set-Cookie", f"{ADMIN_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; {secure}SameSite=Lax")
        self.end_headers()

    def has_admin_session(self) -> bool:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
            morsel = cookies.get(ADMIN_SESSION_COOKIE)
            return bool(morsel) and secrets.compare_digest(morsel.value, ADMIN_SESSION_TOKEN)
        except (CookieError, ValueError):
            return False

    def is_admin(self) -> bool:
        user = self.current_app_user()
        if user and str(user.get("role")) == "admin":
            return True
        username = self.authenticated_user().casefold()
        return (bool(username) and username == ADMIN_USER) or self.has_admin_session()

    def claim_conversation_write(self, chat_id: str) -> bool:
        chat_id = valid_conversation_id(chat_id) or valid_group_id(chat_id)
        if not chat_id:
            self.json_response({"queued": False, "error": "Диалог не выбран"}, HTTPStatus.BAD_REQUEST)
            return False
        user = self.effective_user() or {}
        kind = "group" if chat_id.endswith("@g.us") else "chat"
        ok, lock = AUTH.claim_conversation(chat_id, kind, user, display_name=work_actor())
        if ok:
            return True
        owner = lock or {}
        owner_name = str(owner.get("owner_display_name", "") or owner.get("owner_username", "") or "другой пользователь")
        self.json_response(
            {
                "queued": False,
                "error": "conversation_locked",
                "message": f"Диалог уже ведёт {owner_name}. Доступен только просмотр.",
                "conversation_lock": conversation_lock_snapshot(chat_id),
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
            f"{ADMIN_SESSION_COOKIE}={ADMIN_SESSION_TOKEN}; Path=/; Max-Age={ADMIN_SESSION_MAX_AGE}; HttpOnly; {'Secure; ' if not LOCAL_MODE else ''}SameSite=Lax",
        )
        self.end_headers()

    def end_admin_session(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{ADMIN_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
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
        key = AUTH.authenticate_api_key(raw, "read") if raw else None
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
        if parsed.path == "/search":
            self.redirect("/whatsapp")
            return
        if parsed.path == "/bookmarks":
            self.redirect("/whatsapp?bookmarks=1")
            return
        if parsed.path in {"/api/message-bookmarks", "/api/bookmarks"}:
            user_id = int((self.effective_user() or {}).get("id", 0) or 0)
            if user_id <= 0:
                self.json_response({"error": "Требуется вход"}, HTTPStatus.UNAUTHORIZED)
                return
            rows = _load_message_bookmarks(user_id)
            self.json_response({"bookmarks": rows, "items": rows})
            return
        if parsed.path == "/api/global-search":
            self.json_response({"error": "Глобальный поиск пока недоступен"}, HTTPStatus.GONE)
            return
        if parsed.path == "/api/account-preferences":
            current = self.effective_user() or {}
            user_id = int(current.get("id", 0) or 0)
            self.json_response({"preferences": AUTH.get_user_preferences(user_id) if user_id > 0 else {}})
            return
        if parsed.path == "/api/realtime-wait":
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
            return
        if queue_performance_http.get(self, APP_MODULE):
            return
        if queue_reliability_http.get(self, APP_MODULE):
            return
        if queue_workflow_http.get(self, APP_MODULE):
            return
        if queue_productivity_http.get(self, APP_MODULE):
            return
        if queue_feature_http.get(self, APP_MODULE):
            return
        show_admin = self.is_admin()
        if parsed.path == "/login":
            add_account = str(query.get("add", [""])[0] or "").strip().lower() in {"1", "true", "yes"}
            if self.effective_user() and not add_account:
                self.redirect("/")
            else:
                if add_account and not query.get("next"):
                    query["next"] = ["/accounts"]
                self.html_response(render_login(query))
        elif parsed.path == "/accounts":
            sessions = self.saved_account_sessions(touch=False)
            self.html_response(queue_ticket_views.render_account_switcher(query, show_admin, sessions, self.current_auth_slot()))
        elif parsed.path == "/register":
            # 1.00.6.84: регистрация доступна с любого устройства и также
            # из уже авторизованного браузера. Новый вход будет добавлен в
            # мультиаккаунтный сеанс через start_app_session().
            self.html_response(render_register())
        elif parsed.path == "/forgot-password":
            if self.effective_user():
                self.redirect("/account")
            else:
                self.html_response(render_forgot_password())
        elif parsed.path == "/setup":
            if not self.local_setup_allowed():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.html_response(render_local_setup())
        elif parsed.path == "/logout":
            self.end_app_session()
        elif parsed.path == "/account":
            current = self.effective_user() or {}
            if current.get("must_change_password"):
                self.html_response(render_account(query))
            else:
                self.redirect("/")
        elif parsed.path == "/me":
            self.html_response(render_my_page(query, show_admin))
        elif parsed.path == "/":
            self.html_response(render_dashboard(query, show_admin))
        elif parsed.path == "/api/instruction-sync-status":
            instruction = queue_source_sync.instruction_snapshot(INSTRUCTION_SOURCE_URL, INSTRUCTION_DATA_PATH)
            instruction_sync = instruction.get("_sync", {}) if isinstance(instruction, dict) else {}
            self.json_response({"instruction": instruction_sync, "transit": queue_source_sync.transit_sync_state()})
        elif parsed.path == "/transit-process":
            self.html_response(queue_ticket_views.render_transit_process(show_admin, query))
        elif parsed.path.startswith("/instruction-media/"):
            name = parsed.path[len("/instruction-media/"):]
            media_path = queue_source_sync.instruction_media_path(name)
            if media_path is None:
                self.send_error(HTTPStatus.NOT_FOUND, "Изображение не найдено")
                return
            content_types = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }
            content_type = content_types.get(media_path.suffix.casefold())
            if not content_type:
                self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            self.cached_file_response(media_path, content_type)
        elif parsed.path == "/instruction":
            self.html_response(render_instruction(show_admin, query))
        elif parsed.path == "/transit/export.csv":
            try:
                queue_transit.ensure_seeded(STORE, ROOT / "transit_instruction_data.json")
                queue_source_sync.sync_transit_source(STORE)
                records = queue_transit.list_records(STORE)
                self.csv_bytes_response(queue_transit.export_csv_bytes(records), "transit-np.csv")
            except Exception as error:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Не удалось выгрузить журнал НП: {str(error)[:120]}")
        elif parsed.path == "/admin/login":
            if self.authenticated_user().casefold() != ADMIN_USER:
                self.send_error(HTTPStatus.FORBIDDEN, "Administrator login required")
                return
            self.start_admin_session()
        elif parsed.path == "/admin/logout":
            self.end_admin_session()
        elif parsed.path == "/ticket":
            self.html_response(render_ticket(self.query_int(query, "id"), query, show_admin))
        elif parsed.path in {"/admin", "/admin/"}:
            if not self.require_admin():
                return
            self.html_response(render_admin_home())
        elif parsed.path == "/admin/users":
            if not self.require_admin():
                return
            self.html_response(render_admin_users(query))
        elif parsed.path == "/admin/employees":
            if not self.require_admin():
                return
            self.html_response(render_admin_employees(query))
        elif parsed.path == "/admin/manual":
            if not self.require_admin():
                return
            self.html_response(render_admin_manual(query))
        elif parsed.path == "/admin/menu":
            if not self.require_admin():
                return
            self.html_response(render_admin_menu(query))
        elif parsed.path == "/admin/categories":
            if not self.require_admin():
                return
            self.html_response(render_admin_categories(query))
        elif parsed.path == "/admin/errors":
            if not self.require_admin():
                return
            self.html_response(render_admin_errors(query))
        elif parsed.path == "/admin/error":
            if not self.require_admin():
                return
            self.html_response(render_admin_error(self.query_int(query, "id"), query))
        elif parsed.path == "/admin/contacts":
            if not self.require_admin():
                return
            self.html_response(render_admin_contacts(query))
        elif parsed.path == "/admin/audit":
            if not self.require_admin():
                return
            self.html_response(render_admin_audit(query))
        elif parsed.path == "/admin/analytics":
            if not self.require_admin():
                return
            self.html_response(render_admin_analytics_final(query))
        elif parsed.path == "/admin/manager":
            if not self.require_admin():
                return
            self.html_response(render_admin_manager_dashboard(query))
        elif parsed.path == "/admin/api-keys":
            if not self.require_admin():
                return
            self.html_response(render_admin_api_keys(query))
        elif parsed.path == "/admin/tickets-export.csv":
            if not self.require_admin():
                return
            rows = _final_ticket_filter(query)
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
            self.csv_bytes_response(("\ufeff" + stream.getvalue()).encode("utf-8"), "tickets.csv")
        elif parsed.path == "/admin/system":
            if not self.require_admin():
                return
            self.html_response(render_admin_system(query))
        elif parsed.path == "/api/admin/contact-search":
            if not self.require_admin():
                return
            search_raw = query.get("q", [""])[0]
            self.json_response({"rows_html": discovered_contact_rows(search_raw, 80)})
        elif parsed.path == "/admin/groups":
            if not self.require_admin():
                return
            self.html_response(render_groups(query, True))
        elif parsed.path == "/admin/whatsapp":
            if not self.require_admin():
                return
            self.redirect("/admin/")
        elif parsed.path == "/api/admin/connector-state":
            if not self.require_admin():
                return
            self.json_response(connector_state_snapshot())
        elif parsed.path == "/whatsapp":
            self.html_response(render_whatsapp(query, show_admin))
        elif parsed.path == "/groups":
            self.html_response(render_groups(query, show_admin))
        elif parsed.path == "/qr":
            if not self.require_admin():
                return
            self.redirect("/admin/")
        elif parsed.path == "/api/integration/ping":
            key = self.integration_api_key()
            if not key:
                return
            self.json_response({"ok":True,"service":"Единая очередь","version":APP_VERSION,"key":{"name":str(key.get("name") or ""),"scope":str(key.get("scope") or "read")}})
        elif parsed.path == "/api/integration/tickets":
            key = self.integration_api_key()
            if not key:
                return
            try:
                limit = max(1, min(500, int(query.get("limit", ["100"])[0] or 100)))
            except (TypeError, ValueError):
                limit = 100
            rows = _final_ticket_filter(query, limit=5000)[:limit]
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
        elif parsed.path == "/api/dashboard-data":
            self.handle_dashboard_data(query)
        elif parsed.path == "/api/chat-state":
            # EO_CHAT_STATE_LASTMILE_DEDUPE_V27_20261001
            # Last-mile protection for old live duplicates. Even if CHAT_STATE
            # still contains both @lid and @c.us rows, the browser receives one
            # canonical row. This runs on every state snapshot, so already
            # visible duplicates disappear without waiting for another alias event.
            state = chat_state_snapshot(
                query.get("chat_id", [""])[0],
                query.get("read", ["0"])[0] == "1",
            )
            if isinstance(state, dict):
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
                        canonical_id = STORE.canonical_whatsapp_chat_id(raw_id) or raw_id
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
                            raw_id = valid_chat_id(str(target.get("id", "")))
                            if not raw_id:
                                continue
                            canonical_id = STORE.canonical_whatsapp_chat_id(raw_id) or raw_id
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
                    state["selected_chat_id"] = STORE.canonical_whatsapp_chat_id(selected) or selected

            self.json_response(state)
        elif parsed.path == "/api/group-state":
            self.json_response(group_state_snapshot(query.get("chat_id", [""])[0], query.get("read", ["0"])[0] == "1"))
        elif parsed.path == "/api/chat-history":
            self.handle_chat_history(query)
        elif parsed.path == "/api/message-window":
            self.handle_message_window(query)
        elif parsed.path == "/api/chat-action-status":
            action_id = self.query_int(query, "id")
            action = STORE.whatsapp_action_status(action_id)
            self.json_response({"found": bool(action), "action": action or {}})
        elif parsed.path == "/api/notifications":
            self.json_response(notifications_snapshot())
        elif parsed.path == "/api/conversation-users":
            users=[]
            for item in AUTH.list_users():
                if not item.get("enabled"): continue
                users.append({"id":int(item.get("id",0) or 0),"username":str(item.get("username","") or ""),"display_name":str(item.get("display_name","") or ""),"role":str(item.get("role","") or ""),"employee_name":str(item.get("employee_name","") or "")})
            current=self.effective_user() or {}
            self.json_response({
                "users": users,
                "current_user_id": int(current.get("id", 0) or 0),
                "is_admin": self.is_admin(),
                "active_shift_employee": str(STORE.get_setting("active_employee", active_employee()) or active_employee()),
            })
        elif parsed.path == "/api/chat-media":
            self.handle_chat_media(query)
        elif parsed.path == "/static/style.css":
            self.file_response(ROOT / "static" / "style.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/instruction.css":
            self.file_response(ROOT / "static" / "instruction.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/transit-process.css":
            self.file_response(ROOT / "static" / "transit-process.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/transit-process.js":
            self.file_response(ROOT / "static" / "transit-process.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/transit-guide-01.png":
            self.file_response(ROOT / "static" / "transit-guide-01.png", "image/png")
        elif parsed.path == "/static/transit-guide-02.png":
            self.file_response(ROOT / "static" / "transit-guide-02.png", "image/png")
        elif parsed.path == "/static/transit-guide-03.png":
            self.file_response(ROOT / "static" / "transit-guide-03.png", "image/png")
        elif parsed.path == "/static/transit-guide-04.png":
            self.file_response(ROOT / "static" / "transit-guide-04.png", "image/png")
        elif parsed.path == "/static/transit-guide-01.webp":
            self.cached_file_response(ROOT / "static" / "transit-guide-01.webp", "image/webp")
        elif parsed.path == "/static/transit-guide-02.webp":
            self.cached_file_response(ROOT / "static" / "transit-guide-02.webp", "image/webp")
        elif parsed.path == "/static/transit-guide-03.webp":
            self.cached_file_response(ROOT / "static" / "transit-guide-03.webp", "image/webp")
        elif parsed.path == "/static/transit-guide-04.webp":
            self.cached_file_response(ROOT / "static" / "transit-guide-04.webp", "image/webp")
        elif parsed.path == "/static/global-theme.css":
            self.file_response(ROOT / "static" / "global-theme.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/pink-theme.css":
            self.file_response(ROOT / "static" / "pink-theme.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/ui-polish-1.00.6.40.css":
            self.file_response(ROOT / "static" / "ui-polish-1.00.6.40.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/user-ui-1.00.6.97.css":
            self.file_response(ROOT / "static" / "user-ui-1.00.6.97.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/productivity.css":
            self.file_response(ROOT / "static" / "productivity.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/productivity.js":
            self.file_response(ROOT / "static" / "productivity.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/workflow.css":
            self.file_response(ROOT / "static" / "workflow.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/workflow.js":
            self.file_response(ROOT / "static" / "workflow.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/reliability.css":
            self.file_response(ROOT / "static" / "reliability.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/reliability.js":
            self.file_response(ROOT / "static" / "reliability.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/notification-hotfix.css":
            self.file_response(ROOT / "static" / "notification-hotfix.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/interface105.css":
            self.file_response(ROOT / "static" / "interface105.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/interface105.js":
            self.file_response(ROOT / "static" / "interface105.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/performance107.css":
            self.file_response(ROOT / "static" / "performance107.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/performance107.js":
            self.file_response(ROOT / "static" / "performance107.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/hotfix10067.js":
            self.file_response(ROOT / "static" / "hotfix10067.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/hotfix10069.css":
            self.file_response(ROOT / "static" / "hotfix10069.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/auth100615.css":
            self.file_response(ROOT / "static" / "auth100615.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/auth100618.js":
            self.file_response(ROOT / "static" / "auth100618.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/hotfix10068.js":
            self.file_response(ROOT / "static" / "hotfix10068.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/app.js":
            self.file_response(ROOT / "static" / "app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/favicon.png" or parsed.path == "/favicon.ico":
            self.file_response(ROOT / "static" / "favicon.png", "image/png")
        elif parsed.path == "/health/connector":
            self.json_response(connector_state_snapshot())
        elif parsed.path == "/health":
            self.json_response({"status": "ok"})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        queue_auth.clear_current_user()
        if not self.request_host_allowed():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            form = self.read_form()
            result = AUTH.authenticate(
                form.get("username", ""),
                form.get("password", ""),
                client_ip=self.client_address[0] if self.client_address else "",
                user_agent=self.headers.get("User-Agent", ""),
            )
            if not result.get("ok"):
                next_path = form.get("next", "/")
                self.html_response(render_login({"next": [next_path]}, str(result.get("error") or "Ошибка входа")), HTTPStatus.UNAUTHORIZED)
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
            return
        if parsed.path == "/register":
            form = self.read_form()
            password = form.get("password", "")
            if password != form.get("password2", ""):
                self.html_response(render_register("Пароли не совпадают"), HTTPStatus.BAD_REQUEST)
                return
            ok, message, _ = AUTH.self_register_employee(
                form.get("username", ""),
                form.get("display_name", ""),
                password,
            )
            if not ok:
                self.html_response(render_register(message), HTTPStatus.BAD_REQUEST)
                return
            result = AUTH.authenticate(
                form.get("username", ""), password,
                client_ip=self.client_address[0] if self.client_address else "",
                user_agent=self.headers.get("User-Agent", ""),
            )
            if not result.get("ok"):
                self.html_response(render_login({}, "Аккаунт создан. Войдите с новым логином и паролем"), HTTPStatus.OK)
                return
            register_user = result.get("user") or {}
            if str(register_user.get("role", "")) == "employee":
                activate_employee_shift(register_user)
            self.start_app_session(str(result["token"]), "/")
            return
        if parsed.path == "/forgot-password":
            form = self.read_form()
            notice = AUTH.request_password_reset(form.get("username", ""))
            self.html_response(render_forgot_password(notice=notice), HTTPStatus.OK)
            return
        if parsed.path == "/setup":
            if not self.local_setup_allowed():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = self.read_form()
            if form.get("password", "") != form.get("password2", ""):
                self.html_response(render_local_setup("Пароли не совпадают"), HTTPStatus.BAD_REQUEST)
                return
            ok, message, _ = AUTH.create_user(
                form.get("username", ""), form.get("display_name", ""), form.get("password", ""), role="admin"
            )
            if not ok:
                self.html_response(render_local_setup(message), HTTPStatus.BAD_REQUEST)
                return
            result = AUTH.authenticate(
                form.get("username", ""), form.get("password", ""),
                client_ip=self.client_address[0] if self.client_address else "",
                user_agent=self.headers.get("User-Agent", ""),
            )
            if not result.get("ok"):
                self.html_response(render_login({}, "Администратор создан, но автоматический вход не выполнен"), HTTPStatus.UNAUTHORIZED)
                return
            self.start_app_session(str(result["token"]), "/admin/users")
            return
        if not self.service_post_path(parsed.path) and not self.require_user():
            return
        user = self.effective_user()
        if user:
            queue_auth.set_current_user(user)
        if parsed.path.startswith("/admin/") and self.is_admin():
            try:
                AUTH.record_audit(user or {}, "admin_post", object_type="admin_settings", object_id=parsed.path, details="Отправлено изменение в административном разделе")
            except Exception:
                pass
        if parsed.path == "/api/global-search":
            self.json_response({"updated": False, "error": "Глобальный поиск пока недоступен"}, HTTPStatus.GONE)
            return
        if parsed.path in {"/api/message-bookmarks", "/api/bookmarks"}:
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
                chat_id = STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
            is_favorite = str(form.get("favorite", "1")).strip().lower() in {"1", "true", "yes", "on"}
            with _BOOKMARKS_LOCK:
                items = _load_message_bookmarks(user_id)
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
                STORE.set_setting(_bookmark_setting(user_id), json.dumps(items, ensure_ascii=False))
            self.json_response({"updated": True, "favorite": is_favorite, "bookmarks": items})
            return
        if parsed.path == "/accounts/switch":
            form = self.read_form()
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.redirect("/accounts?notice=" + quote("Страница устарела. Обновите её и повторите переключение"))
                return
            try:
                slot = int(form.get("slot", "0") or 0)
            except (TypeError, ValueError):
                slot = -1
            if not self.switch_app_session(slot, "/"):
                self.redirect("/accounts?notice=" + quote("Сеанс этого аккаунта уже завершён. Войдите в него снова"))
            return
        if parsed.path == "/admin/api-keys":
            if not self.require_admin(): return
            form=self.read_form()
            if form is None: return
            supplied_token=form.get("csrf_token","")
            if not supplied_token or not secrets.compare_digest(supplied_token,ADMIN_FORM_TOKEN): self.html_response(render_admin_api_keys({},notice="Сессия формы устарела"),HTTPStatus.FORBIDDEN); return
            action=str(form.get("action") or "").strip().lower(); actor=self.effective_user() or {}
            if action == "create":
                ok,message,token=AUTH.create_api_key(actor,form.get("name",""),"read"); self.html_response(render_admin_api_keys({},new_token=token if ok else "",notice=message),HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST); return
            if action == "revoke":
                try: key_id=int(form.get("key_id",0) or 0)
                except ValueError: key_id=0
                ok,message=AUTH.revoke_api_key(actor,key_id); self.redirect("/admin/api-keys?notice="+quote(message)); return
            self.html_response(render_admin_api_keys({},notice="Неизвестное действие"),HTTPStatus.BAD_REQUEST); return
        if queue_performance_http.post(self, APP_MODULE):
            return
        if queue_reliability_http.post(self, APP_MODULE):
            return
        if queue_reliability.maintenance_active(STORE) and queue_reliability.should_block_mutation(parsed.path, self.is_admin()):
            if parsed.path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or parsed.path.startswith("/chat"):
                self.json_response({"error":"Режим обслуживания: изменения временно заблокированы администратором"}, HTTPStatus.LOCKED)
            else:
                self.redirect("/?notice=" + quote("Режим обслуживания: изменения временно заблокированы"))
            return
        if queue_workflow_http.post(self, APP_MODULE):
            return
        if queue_productivity_http.post(self, APP_MODULE):
            return
        if queue_feature_http.post(self, APP_MODULE):
            return
        if queue_uploads.post(self, APP_MODULE):
            return
        if parsed.path == "/api/system-warning-action":
            payload = self.read_json_body(120_000)
            if payload is None:
                return
            action = str(payload.get("action") or "").strip().lower()
            event_id = str(payload.get("event_id") or "").strip()
            try:
                if action == "dismiss":
                    if not queue_reliability.dismiss_notification(STORE, event_id):
                        self.json_response({"error":"invalid_warning_id"}, HTTPStatus.BAD_REQUEST)
                        return
                    self.json_response({"ok":True,"dismissed":1})
                    return
                if action == "dismiss_all":
                    snapshot = notifications_snapshot()
                    ids = [str(item.get("id") or item.get("event_id") or "") for item in snapshot.get("events",[]) if isinstance(item,dict)]
                    count = queue_reliability.dismiss_notifications(STORE, ids)
                    self.json_response({"ok":True,"dismissed":count})
                    return
                if action in {"check","check_all"}:
                    queue_reliability.refresh_active_alerts(APP_MODULE)
                    self.json_response({"ok":True,"checked":True})
                    return
            except Exception as error:
                self.json_response({"error":str(error)[:180]}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.json_response({"error":"invalid_action"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/media-stage":
            payload = self.read_authorized_json(800_000)
            if payload is not None:
                try: self.json_response(queue_inbound_media.receive(MEDIA_DIR, payload))
                except (ValueError, OSError) as error: self.json_response({"error": str(error)[:180]}, 400)
            return
        if parsed.path == "/api/keden/claim":
            payload = self.read_authorized_json(120_000)
            if payload is not None:
                try:
                    job = queue_keden_checks.claim(STORE, worker=str(payload.get("worker", "")))
                    self.json_response({"ok": True, "job": job})
                except Exception as error:
                    self.log_error("KEDEN claim failed: %s", type(error).__name__)
                    self.json_response({"error": "keden_claim_failed", "message": str(error)[:240]}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/keden/result":
            payload = self.read_authorized_json(800_000)
            if payload is not None:
                try:
                    result = queue_keden_checks.complete(APP_MODULE, payload)
                    self.json_response(result)
                except ValueError as error:
                    self.json_response({"error": "invalid_keden_result", "message": str(error)[:240]}, HTTPStatus.BAD_REQUEST)
                except Exception as error:
                    self.log_error("KEDEN result failed: %s", type(error).__name__)
                    self.json_response({"error": "keden_result_failed", "message": str(error)[:240]}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/whatsapp":
            self.handle_api_message()
            queue_realtime.notify("ticket")
            return
        if parsed.path == "/api/template-error-delivery":
            self.handle_template_error_delivery()
            return
        if parsed.path == "/api/outbound/claim":
            self.handle_outbound_claim()
            return
        if parsed.path in {"/api/outbound/heartbeat", "/api/outbound/begin"}:
            payload = self.read_authorized_json()
            if payload is not None:
                try:
                    operation = STORE.begin_outbound_send if parsed.path.endswith('/begin') else STORE.touch_outbound_message
                    alive = operation(int(payload.get("message_id", 0)))
                    self.json_response({"updated": alive})
                except (ValueError, TypeError):
                    self.json_response({"error": "invalid_id"}, 400)
            return
        if parsed.path == "/api/outbound/result":
            self.handle_outbound_result()
            return
        if parsed.path == "/api/chat-list-sync":
            self.handle_chat_list_sync()
            return
        if parsed.path == "/api/group-list-sync":
            self.handle_group_list_sync()
            return
        if parsed.path == "/api/presence-sync":
            self.handle_presence_sync()
            return
        elif parsed.path == "/api/group-participants-sync":
            self.handle_group_participants_sync()
            return
        if parsed.path == "/api/group-message-identities-sync":
            self.handle_group_message_identities_sync()
            return
        if parsed.path == "/api/contact-list-sync":
            self.handle_contact_list_sync()
            return
        if parsed.path == "/api/avatar-sync":
            payload = self.read_authorized_json()
            if payload is not None:
                try:
                    queue_avatars.save(APP_MODULE,payload)
                    self.json_response({"updated":True})
                except (ValueError,TypeError,OSError):
                    self.json_response({"error":"Некорректный аватар"},400)
            return
        if parsed.path == "/api/contact-profile-sync":
            self.handle_contact_profile_sync()
            return
        if parsed.path == "/api/group-refresh-check":
            self.handle_group_refresh_check()
            return
        if parsed.path == "/api/connector-state":
            self.handle_connector_state()
            return
        if parsed.path == "/api/contact-policy":
            self.handle_contact_policy()
            return
        if parsed.path == "/api/call-permission":
            self.handle_call_permission()
            return
        if parsed.path == "/api/chat-messages-sync":
            self.handle_chat_messages_sync()
            return
        if parsed.path == "/api/chat-message-ack":
            self.handle_chat_message_ack()
            return
        if parsed.path == "/api/chat-reaction-sync":
            self.handle_chat_reaction_sync()
            return
        if parsed.path == "/chat-media-send":
            self.handle_chat_media_send()
            return
        if parsed.path == "/chat-media-send-raw":
            self.handle_chat_media_send_raw(parse_qs(parsed.query))
            return
        if parsed.path == "/chat-forward":
            self.handle_chat_forward()
            return
        if parsed.path == "/api/whatsapp-action/claim":
            self.handle_whatsapp_action_claim()
            return
        if parsed.path == "/api/whatsapp-action/result":
            self.handle_whatsapp_action_result()
            return
        if parsed.path == "/api/chat-control":
            self.handle_chat_control()
            return
        form = self.read_form()
        if parsed.path == "/instruction/sync":
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.redirect("/instruction?notice=" + quote("Страница была открыта до перезапуска сервера. Обновите страницу и повторите синхронизацию."))
                return
            kind = str(form.get("kind", "general") or "general").strip().lower()
            if kind == "transit":
                try:
                    queue_transit.ensure_seeded(STORE, ROOT / "transit_instruction_data.json")
                    started = queue_source_sync.schedule_transit_sync(STORE, force=True)
                    notice = "Обновление журнала НП запущено в фоне" if started else "Обновление журнала НП уже выполняется"
                except Exception as error:
                    notice = f"Не удалось запустить обновление НП: {str(error)[:160]}"
                self.redirect(f"/instruction?notice={quote(notice)}#transit")
                return
            try:
                started = queue_source_sync.schedule_instruction_sync(INSTRUCTION_SOURCE_URL, INSTRUCTION_DATA_PATH, force=True)
                notice = "Обновление инструкции запущено в фоне" if started else "Обновление инструкции уже выполняется"
            except Exception as error:
                notice = f"Не удалось запустить обновление инструкции: {str(error)[:160]}"
            self.redirect(f"/instruction?notice={quote(notice)}#instruction")
            return
        if parsed.path == "/transit/record":
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid form token")
                return
            try:
                queue_transit.ensure_seeded(STORE, ROOT / "transit_instruction_data.json")
                action = str(form.get("action", "create") or "create").strip().lower()
                actor = work_actor()
                if action == "create":
                    ok, notice, record_id = queue_transit.create_record(STORE, form, actor)
                    target = f"/instruction?notice={quote(notice)}#transit"
                elif action == "update":
                    record_id = self.form_int(form, "record_id")
                    ok, notice = queue_transit.update_record(STORE, record_id, form, actor)
                    target = f"/instruction?notice={quote(notice)}" + ("#transit" if ok else f"&transit_edit={record_id}#transit")
                elif action == "delete":
                    record_id = self.form_int(form, "record_id")
                    if not self.is_admin():
                        ok, notice = False, "Удаление записей доступно только администратору"
                    elif str(form.get("confirm_delete", "") or "") != "1":
                        ok, notice = False, "Удаление не подтверждено"
                    else:
                        ok, notice = queue_transit.delete_record(STORE, record_id, actor)
                    target = f"/instruction?notice={quote(notice)}#transit"
                else:
                    ok, notice = False, "Неизвестное действие"
                    target = f"/instruction?notice={quote(notice)}#transit"
            except Exception as error:
                ok, notice = False, f"Не удалось выполнить действие: {str(error)[:160]}"
                target = f"/instruction?notice={quote(notice)}#transit"
            self.redirect(target)
            return
        if parsed.path == "/api/account-preferences":
            current = self.effective_user() or {}
            user_id = int(current.get("id", 0) or 0)
            if user_id <= 0:
                self.json_response({"updated": False, "error": "Требуется вход"}, HTTPStatus.UNAUTHORIZED)
                return
            updates: dict[str, str] = {}
            for key in ("theme", "chat_scale", "compact_chats", "sidebar_width"):
                if key in form:
                    updates[key] = str(form.get(key, ""))
            result = AUTH.set_user_preferences(user_id, updates)
            AUTH.record_audit(current, "interface_preferences", object_type="user", object_id=str(user_id), details=", ".join(k for k,v in result.items() if v))
            self.json_response({"updated": all(result.values()) if result else False, "preferences": AUTH.get_user_preferences(user_id), "result": result})
            return
        audit_admin_form_submission(parsed.path, form, self.effective_user())
        if parsed.path == "/conversation-lock":
            chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
            action = str(form.get("action", "claim") or "claim").strip().lower()
            user = self.effective_user() or {}
            if not chat_id:
                self.json_response({"updated": False, "error": "Диалог не выбран"}, HTTPStatus.BAD_REQUEST)
                return
            if action == "claim":
                ok, _lock = AUTH.claim_conversation(
                    chat_id, "group" if chat_id.endswith("@g.us") else "chat", user, display_name=work_actor()
                )
                state = conversation_lock_snapshot(chat_id)
                if ok:
                    AUTH.record_audit(user, "conversation_claim", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=work_actor())
                    queue_realtime.notify_outbound()
                self.json_response({"updated": ok, "conversation_lock": state}, HTTPStatus.OK if ok else HTTPStatus.LOCKED)
                return
            if action == "release":
                ok, message = AUTH.release_conversation(
                    chat_id, user, force=self.is_admin() and form.get("force", "") == "1"
                )
                if ok:
                    AUTH.record_audit(user, "conversation_release", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=message)
                    queue_realtime.notify_outbound()
                self.json_response({"updated": ok, "message": message, "conversation_lock": conversation_lock_snapshot(chat_id)}, HTTPStatus.OK if ok else HTTPStatus.LOCKED)
                return
            if action == "transfer":
                try:
                    target_user_id = int(form.get("target_user_id", "0") or 0)
                except ValueError:
                    target_user_id = 0
                target = AUTH.get_user(target_user_id) if target_user_id > 0 else None
                target_name = ""
                if target:
                    target_name = (
                        str(target.get("employee_name", "") or "").strip()
                        or str(target.get("display_name", "") or target.get("username", "")).strip()
                    )
                ok, message, _lock = AUTH.transfer_conversation(
                    chat_id, user, target_user_id, target_display_name=target_name
                )
                if ok:
                    AUTH.record_audit(user, "conversation_transfer", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=f"Передано user_id={target_user_id}")
                    queue_realtime.notify_outbound()
                self.json_response({"updated": ok, "message": message, "conversation_lock": conversation_lock_snapshot(chat_id)}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return
            self.json_response({"updated": False, "error": "Неизвестное действие блокировки"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/account/password":
            user = self.current_app_user()
            if not user:
                self.redirect("/login")
                return
            new_password = form.get("new_password", "")
            target = "/account" if user.get("must_change_password") else "/"
            if new_password != form.get("new_password2", ""):
                self.redirect(f"{target}?notice={quote('Новые пароли не совпадают')}")
                return
            ok, notice = AUTH.change_own_password(int(user["id"]), form.get("current_password", ""), new_password)
            if ok:
                AUTH.record_audit(user, "password_changed", object_type="user", object_id=str(user.get("id", "")), details="Пользователь сменил пароль")
                # Смена пароля завершает старые сеансы, поэтому нужно войти снова.
                self.end_app_session()
            else:
                self.redirect(f"{target}?notice={quote(notice)}")
            return
        if parsed.path == "/admin/users":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "create")
            actor_user = self.effective_user() or {}
            object_id = str(form.get("user_id", "") or "")
            if action == "create":
                temporary = form.get("temporary_password", "") == "1"
                ok, notice, created_id = AUTH.create_user(
                    form.get("username", ""), form.get("display_name", ""), form.get("password", ""),
                    role=form.get("role", "employee"), must_change_password=temporary,
                )
                object_id = str(created_id or "")
            elif action == "toggle":
                ok, notice = AUTH.set_enabled(self.form_int(form, "user_id"), form.get("enabled", "0") == "1")
            elif action == "role":
                ok, notice = AUTH.set_role(self.form_int(form, "user_id"), form.get("role", "employee"))
            elif action == "password":
                ok, notice = AUTH.set_password(self.form_int(form, "user_id"), form.get("password", ""), must_change_password=form.get("temporary_password", "") == "1")
            elif action == "theme":
                theme = str(form.get("theme", "") or "").strip().lower()
                ok, notice = AUTH.set_user_preference(self.form_int(form, "user_id"), "theme", theme)
                if ok:
                    notice = "Тема пользователя сохранена"
            elif action == "employee_link":
                employee_name = form.get("employee_name", "")
                if employee_name and employee_name not in EMPLOYEES:
                    ok, notice = False, "Сотрудник не найден в списке"
                else:
                    ok, notice = AUTH.set_employee_link(self.form_int(form, "user_id"), employee_name)
            elif action == "delete":
                target_user_id = self.form_int(form, "user_id")
                target_user = AUTH.get_user(target_user_id) if target_user_id > 0 else None
                actor_user_id = int(actor_user.get("id", 0) or 0)
                if actor_user_id > 0 and target_user_id == actor_user_id:
                    ok, notice = False, "Нельзя удалить собственную учётную запись"
                elif not target_user:
                    ok, notice = False, "Пользователь не найден"
                else:
                    target_name = str(target_user.get("username") or target_user.get("display_name") or target_user_id)
                    ok, delete_notice = AUTH.delete_user(target_user_id)
                    notice = (
                        f"Пользователь @{target_name} удалён. Его активные сессии завершены"
                        if ok else delete_notice
                    )
            elif action == "revoke_session":
                ok = AUTH.revoke_session_hash(form.get("session_hash", ""))
                notice = "Сессия завершена" if ok else "Сессия уже завершена или не найдена"
                object_id = str(form.get("session_hash", ""))[:12]
            else:
                ok, notice = False, "Неизвестное действие"
            if ok:
                AUTH.record_audit(actor_user, f"user_{action}", object_type="auth", object_id=object_id, details=notice)
            prefix = "" if ok else "Ошибка: "
            self.redirect(f"/admin/users?notice={quote(prefix + notice)}")
            return
        if parsed.path == "/context-history/apply":
            ticket_id = self.form_int(form, "ticket_id")
            history_id = self.form_int(form, "history_id")
            mode = form.get("mode", "field")
            field_name = form.get("field_name", "problem")
            updated, notice = queue_contextual_tickets.apply_history_item(
                STORE, ticket_id, history_id, field_name, work_actor(), mode=mode
            )
            if not updated and not notice:
                notice = "Не удалось применить запись истории"
            self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")
            return
        if parsed.path == "/chat-favorite":
            chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
            if not chat_id:
                self.json_response({"updated": False, "error": "Чат не найден"}, HTTPStatus.BAD_REQUEST)
                return
            favorite = str(form.get("favorite", "0")).lower() in {"1", "true", "yes", "on"}
            updated, canonical = set_chat_favorite(chat_id, favorite)
            self.json_response({"updated": updated, "chat_id": canonical, "favorite": favorite},
                               HTTPStatus.OK if updated else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/group-mute":
            chat_id = valid_group_id(form.get("chat_id", ""))
            muted = form.get("muted", "0") in {"1", "true", "yes", "on"}
            updated = STORE.set_whatsapp_group_muted(chat_id, muted)
            if not updated:
                self.json_response({"updated": False, "error": "group_not_found"}, HTTPStatus.NOT_FOUND)
            else:
                self.json_response({"updated": True, "chat_id": chat_id, "muted": muted})
            return
        if parsed.path == "/admin/employees":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            try:
                count = int(form.get("employee_count", "0"))
            except ValueError:
                count = 0
            if count < 1 or count > MAX_EMPLOYEES:
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
            old_names = list(EMPLOYEES)
            old_active = active_employee()
            STORE.set_setting("employees_json", json.dumps(names, ensure_ascii=False))
            EMPLOYEES[:] = names
            if old_active in old_names:
                old_index = old_names.index(old_active)
                if old_index < len(names):
                    STORE.set_setting("active_employee", names[old_index])
                else:
                    STORE.set_setting("active_employee", names[0])
            elif old_active not in names:
                STORE.set_setting("active_employee", names[0])
            self.redirect(f"/admin/employees?notice={quote('Список сотрудников сохранён')}")
            return
        if parsed.path == "/admin/ticket-delete":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            ticket_id = self.form_int(form, "ticket_id")
            deleted = STORE.delete_ticket(ticket_id, f"Администратор · {work_actor()}")
            notice = (
                f"Заявка №{ticket_id} удалена только из системы. Сообщение пользователю не отправлялось"
                if deleted else "Заявка не найдена"
            )
            self.redirect(f"/?notice={quote(notice)}")
            return
        if parsed.path == "/admin/contacts":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "delete":
                contact_chat_id = valid_chat_id(form.get("chat_id", ""))
                existing_contact = STORE.manual_whatsapp_contact(contact_chat_id, "")
                deleted = STORE.delete_whatsapp_contact(contact_chat_id)
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
                            STORE.set_conversation_context(reset_key, MENU_CONTEXT, 0, False)
                notice = "Контакт удалён. Автообработка для него снова включена" if deleted else "Контакт не найден"
                self.redirect(f"/admin/contacts?notice={quote(notice)}")
                return
            name = normalize_message(form.get("name", ""))[:100]
            phone = normalize_phone(form.get("phone", ""))
            chat_id = f"{phone.lstrip('+')}@c.us" if phone else ""
            existing = STORE.manual_whatsapp_contact(chat_id, phone) if phone else None
            if existing:
                notice = f"Номер {display_phone(phone)} уже добавлен как {existing.get('name') or 'контакт'}"
                self.redirect(f"/admin/contacts?notice={quote(notice)}")
                return
            saved = STORE.save_whatsapp_contact(name, phone, chat_id)
            notice = "Контакт добавлен" if saved else "Проверьте имя и мобильный номер"
            self.redirect(f"/admin/contacts?notice={quote(notice)}")
            return
        if parsed.path == "/admin/manual":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            employee = form.get("employee", "")
            if employee not in assignment_names():
                employee = work_actor()
            category = form.get("category", "general")
            if category not in CATEGORIES:
                category = "general"
            status = form.get("status", "new")
            if status not in STATUSES:
                status = "new"
            ticket_id = STORE.create_ticket(
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
            return
        if parsed.path == "/admin/system":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "")
            if action == "cleanup":
                result = queue_workflow.cleanup_retention(STORE)
                notice = f"Очистка завершена: сообщений {result.get('messages',0)}, медиа {result.get('media_rows',0)}, заявок {result.get('tickets',0)}"
            elif action == "retry_failed":
                count = STORE.retry_failed_outbound()
                notice = f"На повторную отправку возвращено: {count}"
            elif action == "backup_now":
                ok, info = create_scheduled_backup("manual_admin")
                notice = f"Резервная копия создана: {info}" if ok else f"Ошибка резервной копии: {info}"
            elif action == "backup_settings":
                try:
                    hours=max(1,min(168,int(form.get("backup_hours","24"))))
                    keep=max(2,min(90,int(form.get("backup_keep","14"))))
                    STORE.set_setting("scheduled_backup_hours",str(hours)); STORE.set_setting("scheduled_backup_keep",str(keep))
                    notice=f"Расписание сохранено: каждые {hours} ч., хранить {keep} копий"
                except ValueError:
                    notice="Некорректные параметры расписания"
            else:
                notice = "Неизвестное действие"
            AUTH.record_audit(self.effective_user() or {}, f"system_{action}", object_type="system", details=notice)
            try:
                with MONITOR_LOG_PATH.open("a", encoding="utf-8") as log:
                    log.write(f"{datetime.now(timezone.utc).isoformat()} ADMIN {notice}\n")
            except OSError:
                pass
            self.redirect(f"/admin/system?notice={quote(notice)}")
            return
        if parsed.path == "/admin/groups":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "refresh":
                STORE.set_setting("group_refresh_request", secrets.token_urlsafe(18))
                notice = "Запрос отправлен QR-коннектору. Список обновится примерно через 5–7 секунд"
                self.redirect(f"/groups?notice={quote(notice)}&refresh=1")
                return
            if action == "delete":
                deleted = STORE.delete_whatsapp_group(valid_group_id(form.get("chat_id", "")))
                notice = "Группа удалена" if deleted else "Группа не найдена"
            elif action == "add_discovered":
                chat_id = valid_group_id(form.get("chat_id", ""))
                discovered = next(
                    (item for item in STORE.list_discovered_whatsapp_groups() if str(item.get("chat_id", "")) == chat_id),
                    None,
                )
                saved = STORE.save_whatsapp_group(str(discovered.get("name", "")), chat_id) if discovered else None
                notice = "Группа добавлена" if saved else "Группа не найдена в списке WhatsApp"
            else:
                saved = STORE.save_whatsapp_group(
                    form.get("name", ""),
                    valid_group_id(form.get("chat_id", "")),
                )
                notice = "Группа добавлена" if saved else "Укажите название и корректный WhatsApp ID группы"
            self.redirect(f"/groups?notice={quote(notice)}")
            return
        if parsed.path == "/admin/errors":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            report_id = self.form_int(form, "report_id")
            status = form.get("status", "new")
            admin_note = normalize_message(form.get("admin_note", ""))[:3000]
            updated = STORE.update_error_report(report_id, status, admin_note, f"Администратор · {work_actor()}")
            notice = "Репорт обновлён" if updated else "Репорт не найден"
            self.redirect(f"/admin/error?id={report_id}&notice={quote(notice)}")
            return
        if parsed.path == "/admin/categories":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "add":
                if len(MENU_OPTIONS) >= MAX_MENU_OPTIONS:
                    self.redirect(
                        f"/admin/categories?notice={quote(f'Можно добавить не больше {MAX_MENU_OPTIONS} пунктов меню и категорий суммарно')}"
                    )
                    return
                label = normalize_message(form.get("new_label", ""))[:80]
                if not label:
                    self.redirect(f"/admin/categories?notice={quote('Укажите название категории')}")
                    return
                key = f"custom_{secrets.token_hex(5)}"
                CATEGORIES[key] = label
                MENU_OPTIONS.append(
                    {
                        "key": key,
                        "label": label,
                        "prompt": category_prompt(key),
                        "enabled": True,
                        "action": MENU_ACTION_TICKET,
                    }
                )
                notice = "Категория добавлена"
            else:
                try:
                    count = int(form.get("row_count", "0"))
                except ValueError:
                    count = 0
                if count != len(MENU_OPTIONS):
                    self.redirect(f"/admin/categories?notice={quote('Категории изменились, обновите страницу и повторите')}")
                    return
                updated_options: list[dict[str, object]] = []
                enabled_ticket_count = 0
                for index, current in enumerate(MENU_OPTIONS, start=1):
                    if str(current.get("action", MENU_ACTION_TICKET)) != MENU_ACTION_TICKET:
                        updated_options.append(current)
                        continue
                    key = form.get(f"key_{index}", "")
                    if key != str(current.get("key", "")):
                        self.send_error(HTTPStatus.BAD_REQUEST, "Invalid category row")
                        return
                    label = normalize_message(form.get(f"label_{index}", ""))[:80]
                    if not label:
                        self.redirect(f"/admin/categories?notice={quote('У каждой категории должно быть название')}")
                        return
                    if form.get(f"delete_{index}") == "1":
                        # Keep the label for historical tickets, but remove the category
                        # from all new-ticket selectors and from the WhatsApp request menu.
                        CATEGORY_ARCHIVE[key] = label
                        CATEGORIES[key] = label
                        continue
                    enabled = form.get(f"enabled_{index}") == "1"
                    old_label = str(current.get("label", ""))
                    old_prompt = str(current.get("prompt", ""))
                    updated = dict(current)
                    updated["label"] = label
                    updated["enabled"] = enabled
                    CATEGORIES[key] = label
                    # If the prompt is still the automatically generated one, keep
                    # its heading in sync with a renamed category. Hand-edited prompts
                    # from the menu editor are left untouched.
                    if old_prompt.startswith(f"Вы выбрали: {old_label}."):
                        updated["prompt"] = category_prompt(key)
                    updated_options.append(updated)
                    if enabled:
                        enabled_ticket_count += 1
                if enabled_ticket_count < 1:
                    self.redirect(f"/admin/categories?notice={quote('Оставьте хотя бы одну активную категорию для заявок')}")
                    return
                MENU_OPTIONS[:] = updated_options
                notice = "Категории сохранены. Нумерация меню для пользователей обновлена автоматически"
            apply_menu_labels()
            STORE.set_setting("request_menu_json", json.dumps(MENU_OPTIONS, ensure_ascii=False))
            STORE.set_setting("request_category_archive_json", json.dumps(CATEGORY_ARCHIVE, ensure_ascii=False))
            self.redirect(f"/admin/categories?notice={quote(notice)}")
            return
        if parsed.path == "/admin/menu":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "add":
                if len(MENU_OPTIONS) >= MAX_MENU_OPTIONS:
                    self.redirect(
                        f"/admin/menu?notice={quote(f'Можно добавить не больше {MAX_MENU_OPTIONS} вариантов')}"
                    )
                    return
                label = normalize_message(form.get("new_label", ""))[:80]
                prompt = normalize_message(form.get("new_prompt", ""))[:1200]
                menu_action = form.get("new_action", MENU_ACTION_TICKET)
                if menu_action not in MENU_ACTIONS:
                    menu_action = MENU_ACTION_TICKET
                if not label or not prompt:
                    self.redirect(f"/admin/menu?notice={quote('Заполните название и подсказку')}")
                    return
                MENU_OPTIONS.append(
                    {
                        "key": f"custom_{secrets.token_hex(5)}",
                        "label": label,
                        "prompt": prompt,
                        "enabled": True,
                        "action": menu_action,
                    }
                )
            else:
                try:
                    count = int(form.get("row_count", "0"))
                except ValueError:
                    count = 0
                if count != len(MENU_OPTIONS):
                    self.redirect(f"/admin/menu?notice={quote('Меню изменилось, обновите страницу и повторите')}")
                    return
                updated_options: list[dict[str, object]] = []
                for index, current in enumerate(MENU_OPTIONS, start=1):
                    key = form.get(f"key_{index}", "")
                    if key != current["key"]:
                        self.send_error(HTTPStatus.BAD_REQUEST, "Invalid menu row")
                        return
                    label = normalize_message(form.get(f"label_{index}", ""))[:80]
                    prompt = normalize_message(form.get(f"prompt_{index}", ""))[:1200]
                    menu_action = form.get(f"action_{index}", MENU_ACTION_TICKET)
                    if menu_action not in MENU_ACTIONS:
                        menu_action = MENU_ACTION_TICKET
                    if not label or not prompt:
                        self.redirect(f"/admin/menu?notice={quote('У каждого варианта должны быть название и подсказка')}")
                        return
                    updated_options.append(
                        {
                            "key": key,
                            "label": label,
                            "prompt": prompt,
                            "enabled": form.get(f"enabled_{index}") == "1",
                            "action": menu_action,
                        }
                    )
                if not any(bool(item["enabled"]) for item in updated_options):
                    self.redirect(f"/admin/menu?notice={quote('Оставьте включённым хотя бы один вариант')}")
                    return
                MENU_OPTIONS[:] = updated_options
            apply_menu_labels()
            STORE.set_setting("request_menu_json", json.dumps(MENU_OPTIONS, ensure_ascii=False))
            self.redirect(f"/admin/menu?notice={quote('Меню обращений сохранено. Нумерация видимых пунктов обновлена автоматически')}")
            return
        if parsed.path == "/active-employee":
            self.json_response({"updated": False, "error": "Ручной выбор смены отключён. Сотрудник становится на смену автоматически после входа."}, HTTPStatus.GONE)
        elif parsed.path == "/handoff":
            ticket_id = self.form_int(form, "ticket_id")
            action = form.get("action", "transfer")
            updated = STORE.set_ticket_handoff(
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
        elif parsed.path == "/status":
            ticket_id = self.form_int(form, "ticket_id")
            ticket = STORE.get_ticket(ticket_id)
            assignee = form.get("employee", "")
            if assignee not in assignment_names():
                assignee = str((ticket or {}).get("assigned_to", "")) or work_actor()
            requested_status = form.get("status", "")
            close_reason = form.get("close_reason", "")
            close_comment = form.get("close_comment", "")
            valid_close, close_error = queue_workflow.validate_close(STORE, ticket_id, requested_status, close_reason, close_comment)
            if not valid_close:
                self.redirect(f"/ticket?id={ticket_id}&notice={quote(close_error)}")
                return
            updated, _message_id = update_ticket_status(
                ticket_id,
                requested_status,
                work_actor(),
                assignee,
                allow_reopen=self.is_admin(),
                close_reason=close_reason,
                close_comment=close_comment,
            )
            if updated:
                queue_workflow.record_close_meta(STORE, ticket_id, requested_status, close_reason, close_comment, work_actor())
            notice = "" if updated else "Отработанную заявку может вернуть в работу только администратор"
            suffix = f"&notice={quote(notice)}" if notice else ""
            self.redirect(f"/ticket?id={ticket_id}{suffix}")
        elif parsed.path == "/quick-status":
            ticket_id = self.form_int(form, "ticket_id")
            ticket = STORE.get_ticket(ticket_id)
            actor = work_actor()
            requested_status = form.get("status", "")
            assignee = actor if requested_status == "in_progress" else (str((ticket or {}).get("assigned_to", "")) or actor)
            close_reason = form.get("close_reason", "")
            close_comment = form.get("close_comment", "")
            valid_close, close_error = queue_workflow.validate_close(STORE, ticket_id, requested_status, close_reason, close_comment)
            if not valid_close:
                self.json_response({"updated":False,"error":close_error}, HTTPStatus.BAD_REQUEST)
                return
            updated, message_id = update_ticket_status(
                ticket_id,
                requested_status,
                actor,
                assignee,
                allow_reopen=self.is_admin(),
                close_reason=close_reason,
                close_comment=close_comment,
            )
            if updated:
                queue_workflow.record_close_meta(STORE, ticket_id, requested_status, close_reason, close_comment, actor)
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
        elif parsed.path == "/quick-priority":
            ticket_id = self.form_int(form, "ticket_id")
            updated = STORE.update_priority(
                ticket_id,
                form.get("priority", ""),
                work_actor(),
            )
            self.json_response(
                {"updated": updated, "ticket_id": ticket_id},
                HTTPStatus.OK if updated else HTTPStatus.BAD_REQUEST,
            )
        elif parsed.path == "/classification":
            ticket_id = self.form_int(form, "ticket_id")
            updated = STORE.update_priority(
                ticket_id,
                form.get("priority", ""),
                work_actor(),
            )
            notice = "Приоритет сохранён" if updated else "Не удалось изменить приоритет"
            self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")
        elif parsed.path == "/send-reply":
            ticket_id = self.form_int(form, "ticket_id")
            ticket = STORE.get_ticket(ticket_id)
            linked_chat_id = valid_conversation_id(str((ticket or {}).get("chat_id", "") or ""))
            if linked_chat_id and not self.claim_conversation_write(linked_chat_id):
                return
            if form.get("send_custom") == "1":
                reply = form.get("custom_reply", "")
            else:
                reply = form.get("quick_reply", "")
            message_id = STORE.queue_outbound_message(
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
        elif parsed.path == "/chat-send":
            chat_id = valid_chat_id(form.get("chat_id", ""))
            if not self.claim_conversation_write(chat_id):
                return
            message_id = STORE.queue_direct_message(
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
        elif parsed.path == "/chat-mode":
            # EO_AUTOREPLY_TOGGLE_V15_20261001
            # Per-chat employee control:
            #   disable -> pause automatic replies without losing dialog state
            #   enable  -> resume automatic replies without resetting the dialog
            #   reset   -> keep the existing hard reset behaviour
            raw_chat_id = valid_chat_id(form.get("chat_id", ""))
            if not self.claim_conversation_write(raw_chat_id):
                return
            if not raw_chat_id:
                self.json_response({"updated": False, "error": "Выберите личный чат"}, HTTPStatus.BAD_REQUEST)
                return
            chat_id = STORE.canonical_whatsapp_chat_id(raw_chat_id) or raw_chat_id
            if STORE.manual_whatsapp_contact(chat_id, ""):
                self.json_response({
                    "updated": False,
                    "manual_contact": True,
                    "error": "Контакт добавлен в админке — автоответчик для него отключён постоянно",
                }, HTTPStatus.CONFLICT)
                return

            action = normalize_message(form.get("action", "reset")).casefold()

            if action in {"disable", "pause", "off"}:
                actor = work_actor()
                STORE.enable_manual_chat_mode(chat_id, actor)
                if raw_chat_id != chat_id:
                    STORE.enable_manual_chat_mode(raw_chat_id, actor)
                manual_mode = STORE.manual_chat_mode(chat_id) or STORE.manual_chat_mode(raw_chat_id) or {}
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
                STORE.disable_manual_chat_mode(chat_id, actor)
                if raw_chat_id != chat_id:
                    STORE.disable_manual_chat_mode(raw_chat_id, actor)
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
            STORE.disable_manual_chat_mode(chat_id, work_actor())
            if raw_chat_id != chat_id:
                STORE.disable_manual_chat_mode(raw_chat_id, work_actor())
            try:
                with STORE.connection() as db:
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
                STORE.clear_conversation_draft(reset_key)
                try:
                    queue_dialog_flow.save(STORE, reset_key, {})
                except Exception as exc:
                    print(f"Bot hard-reset dialog-flow warning for {reset_key}: {exc}", flush=True)
                try:
                    queue_contextual_tickets.reset_context(STORE, reset_key, "")
                except Exception as exc:
                    print(f"Bot hard-reset contextual-flow warning for {reset_key}: {exc}", flush=True)
                STORE.set_conversation_context(reset_key, MENU_CONTEXT, 0, False)

            reset_token = secrets.token_urlsafe(18)
            STORE.set_setting(f"bot_hard_reset_v1:{chat_id}", reset_token)
            if raw_chat_id != chat_id:
                STORE.set_setting(f"bot_hard_reset_v1:{raw_chat_id}", reset_token)

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
        elif parsed.path == "/group-send":
            chat_id = valid_group_id(form.get("chat_id", ""))
            if not self.claim_conversation_write(chat_id):
                return
            mention_ids = [
                item for item in form.get("mentions", "").split(",")
                if valid_conversation_id(item.strip()) or valid_group_id(item.strip())
            ]
            message_id = STORE.queue_direct_message(
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
        elif parsed.path == "/chat-action":
            chat_id = valid_conversation_id(form.get("chat_id", ""))
            if not self.claim_conversation_write(chat_id):
                return
            message_key = normalize_message(form.get("message_id", ""))[:160]
            action = form.get("action", "")
            message = STORE.get_whatsapp_message(chat_id, message_key) if chat_id and message_key else None
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
                if action_body and action_body not in REACTION_EMOJIS:
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
            action_id = STORE.queue_whatsapp_action(action, chat_id, message_key, action_body, work_actor())
            if action_id:
                queue_realtime.notify_outbound()
            self.json_response(
                {"queued": bool(action_id), "action_id": action_id},
                HTTPStatus.OK if action_id else HTTPStatus.BAD_REQUEST,
            )
        elif parsed.path == "/manual":
            title = form.get("title", "Открытие НП").strip()
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
                "source": "telegram_manual",
                "sender": employee,
                "category": "telegram_manual",
                "priority": form.get("priority", "normal"),
                "title": title,
                "summary": " · ".join(summary_parts),
                "original_text": comment,
                "status": form.get("status", "new"),
                "assigned_to": employee,
            }
            similar = STORE.find_similar_open_ticket(manual_payload, hours=72)
            if similar:
                similar_id = int(similar.get("id", 0) or 0)
                self.redirect(f"/ticket?id={similar_id}&notice={quote(f'Похожая открытая заявка уже существует: №{similar_id}. Новая заявка не создана')}")
                return
            ticket_id = STORE.create_ticket(manual_payload)
            self.redirect(f"/ticket?id={ticket_id}")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def handle_dashboard_data(self, query: dict[str, list[str]]) -> None:
        status = query.get("status", [""])[0]
        category = query.get("category", [""])[0]
        priority = query.get("priority", [""])[0]
        search = query.get("q", [""])[0]
        view = query.get("view", [""])[0]
        if view not in {"", "mine", "handoff", "open", "urgent"}:
            view = ""
        employee = work_actor()
        total = STORE.count_tickets(status, category, priority, search, view, employee)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = STORE.list_tickets(
            status, category, priority, search, PAGE_SIZE, (page - 1) * PAGE_SIZE, view, employee
        )
        self.json_response(
            {
                "version": STORE.dashboard_version(),
                "rows_html": render_ticket_rows(tickets),
                "counts": STORE.counts(),
                "pagination_html": render_pagination(query, total, page),
            }
        )


    def read_json_body(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > max_bytes:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_authorized_json(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        if self.headers.get("X-Webhook-Token") != WEBHOOK_TOKEN:
            self.json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > max_bytes:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(body).items()}

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
        # 1.00.6.14: global kill-switch for bot replies. The inbound webhook is
        # still processed normally, so chats, contexts and tickets keep working;
        # only the automatic response returned to the WhatsApp connector is muted.
        if urlparse(self.path).path == "/api/whatsapp" and not global_auto_reply_enabled():
            payload = suppress_inbound_auto_reply(payload)
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
