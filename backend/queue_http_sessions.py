"""Вход и сессии: cookie и несколько аккаунтов, права пользователя и администратора, страницы входа, регистрации и аккаунта."""
from __future__ import annotations

import re
import secrets
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote, urlparse

import queue_auth
from queue_work_context import activate_employee_shift


class SessionRoutes:
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

    def _get_api_account_preferences(self, query) -> None:
        current = self.effective_user() or {}
        user_id = int(current.get("id", 0) or 0)
        self.json_response({"preferences": self.deps.auth.get_user_preferences(user_id) if user_id > 0 else {}})

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

    def _get_admin_login(self, query) -> None:
        if self.authenticated_user().casefold() != self.deps.admin_user:
            self.send_error(HTTPStatus.FORBIDDEN, "Administrator login required")
            return
        self.start_admin_session()

    def _get_admin_logout(self, query) -> None:
        self.end_admin_session()

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

    def _post_accounts_switch(self, parsed) -> None:
        # POST /accounts/switch
        form = self.read_form()
        try:
            slot = int(form.get("slot", "0") or 0)
        except (TypeError, ValueError):
            slot = -1
        if not self.switch_app_session(slot, "/"):
            self.redirect("/accounts?notice=" + quote("Сеанс этого аккаунта уже завершён. Войдите в него снова"))

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
