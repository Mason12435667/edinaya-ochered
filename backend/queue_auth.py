"""Учётные записи: таблицы, пользователи, роли, пароли и личные настройки.

Сессии и вход, журнал с API-ключами и закрепление чатов подмешаны в AuthStore из
queue_auth_sessions, queue_auth_access и queue_auth_locks.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from queue_auth_common import (
    PBKDF2_ALGORITHM, PBKDF2_ITERATIONS, SESSION_HOURS, LOCKOUT_ATTEMPTS, LOCKOUT_MINUTES,
    USERNAME_RE, ROLES, self_registration_enabled, _iso, _normalize_username, password_error,
    hash_password, verify_password, set_current_user, current_user, clear_current_user,
)
from queue_auth_sessions import SessionStore
from queue_auth_access import AccessStore
from queue_auth_locks import ConversationLockStore


class AuthStore(SessionStore, AccessStore, ConversationLockStore):
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'employee',
                    employee_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_app_users_role_enabled
                    ON app_users(role, enabled);

                CREATE TABLE IF NOT EXISTS app_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    client_ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES app_users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_app_sessions_user
                    ON app_sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_app_sessions_expires
                    ON app_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS password_reset_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolved_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES app_users(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_password_reset_pending_user
                    ON password_reset_requests(user_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_password_reset_status_time
                    ON password_reset_requests(status, requested_at);

                CREATE TABLE IF NOT EXISTS conversation_locks (
                    conversation_id TEXT PRIMARY KEY,
                    conversation_kind TEXT NOT NULL DEFAULT 'chat',
                    owner_user_id INTEGER NOT NULL DEFAULT 0,
                    owner_username TEXT NOT NULL DEFAULT '',
                    owner_display_name TEXT NOT NULL DEFAULT '',
                    owner_role TEXT NOT NULL DEFAULT '',
                    acquired_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_locks_owner
                    ON conversation_locks(owner_user_id, owner_username);

                CREATE TABLE IF NOT EXISTS auth_login_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    username TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    client_ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auth_login_events_time
                    ON auth_login_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_auth_login_events_user
                    ON auth_login_events(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS auth_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER NOT NULL DEFAULT 0,
                    actor_username TEXT NOT NULL DEFAULT '',
                    actor_display_name TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL DEFAULT '',
                    object_id TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auth_audit_events_time
                    ON auth_audit_events(created_at DESC);

                CREATE TABLE IF NOT EXISTS auth_user_preferences (
                    user_id INTEGER NOT NULL,
                    pref_key TEXT NOT NULL,
                    pref_value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, pref_key),
                    FOREIGN KEY(user_id) REFERENCES app_users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_auth_user_preferences_user
                    ON auth_user_preferences(user_id);


                CREATE TABLE IF NOT EXISTS auth_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL DEFAULT 'read',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_by_user_id INTEGER NOT NULL DEFAULT 0,
                    created_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_auth_api_keys_enabled
                    ON auth_api_keys(enabled, created_at DESC);
                """
            )
            # Safe in-place migration for older databases.
            user_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(app_users)").fetchall()}
            if "employee_name" not in user_columns:
                connection.execute("ALTER TABLE app_users ADD COLUMN employee_name TEXT NOT NULL DEFAULT ''")
            connection.commit()
            self.cleanup_sessions(connection=connection)

    @staticmethod
    def _public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"]),
            "role": str(row["role"]),
            "employee_name": str(row["employee_name"] or "") if "employee_name" in row.keys() else "",
            "enabled": bool(row["enabled"]),
            "must_change_password": bool(row["must_change_password"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "last_login_at": str(row["last_login_at"] or ""),
        }

    def has_users(self) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT 1 FROM app_users LIMIT 1").fetchone()
            return bool(row)

    def enabled_admin_count(self, *, excluding_user_id: int = 0) -> int:
        with self._connection() as connection:
            if excluding_user_id:
                row = connection.execute(
                    "SELECT COUNT(*) AS amount FROM app_users WHERE role='admin' AND enabled=1 AND id<>?",
                    (int(excluding_user_id),),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS amount FROM app_users WHERE role='admin' AND enabled=1"
                ).fetchone()
            return int(row["amount"] if row else 0)

    def list_users(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, username, display_name, role, employee_name, enabled, must_change_password,
                       created_at, updated_at, last_login_at
                  FROM app_users
                 ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END,
                          lower(display_name), lower(username)
                """
            ).fetchall()
            return [self._public_user(row) for row in rows]

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, role, employee_name, enabled, must_change_password,
                       created_at, updated_at, last_login_at
                  FROM app_users WHERE id=?
                """,
                (int(user_id),),
            ).fetchone()
            return self._public_user(row) if row else None

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        *,
        role: str = "employee",
        enabled: bool = True,
        must_change_password: bool = False,
        employee_name: str = "",
    ) -> tuple[bool, str, int]:
        username = _normalize_username(username)
        display_name = str(display_name or "").strip()
        role = str(role or "employee").strip().lower()
        employee_name = str(employee_name or "").strip()[:80]
        if not USERNAME_RE.fullmatch(username):
            return False, "Логин: 3-32 символа, латиница, цифры, точка, дефис или подчёркивание", 0
        if not display_name or len(display_name) > 80:
            return False, "Укажите отображаемое имя до 80 символов", 0
        if role not in ROLES:
            return False, "Некорректная роль", 0
        problem = password_error(password)
        if problem:
            return False, problem, 0
        encoded = hash_password(password)
        now = _iso()
        with self._lock, self._connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO app_users(
                        username, display_name, role, employee_name, password_hash, enabled,
                        must_change_password, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        username,
                        display_name,
                        role,
                        employee_name,
                        encoded,
                        1 if enabled else 0,
                        1 if must_change_password else 0,
                        now,
                        now,
                    ),
                )
                connection.commit()
                return True, "Пользователь создан", int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                return False, "Такой логин уже существует", 0

    def employee_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS amount FROM app_users WHERE role='employee'").fetchone()
            return int(row["amount"] if row else 0)

    def self_register_employee(
        self,
        username: str,
        display_name: str,
        password: str,
    ) -> tuple[bool, str, int]:
        if not self_registration_enabled():
            return False, "Самостоятельная регистрация отключена. Обратитесь к администратору", 0
        username = _normalize_username(username)
        display_name = str(display_name or "").strip()
        if not USERNAME_RE.fullmatch(username):
            return False, "Логин: 3-32 символа, латиница, цифры, точка, дефис или подчёркивание", 0
        if not display_name or len(display_name) > 80:
            return False, "Укажите отображаемое имя до 80 символов", 0
        problem = password_error(password)
        if problem:
            return False, problem, 0
        encoded = hash_password(password)
        now = _iso()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            admin_row = connection.execute(
                "SELECT COUNT(*) AS amount FROM app_users WHERE role='admin' AND enabled=1"
            ).fetchone()
            if int(admin_row["amount"] if admin_row else 0) < 1:
                connection.rollback()
                return False, "Сначала должен быть создан администратор", 0
            # Самостоятельно зарегистрированный аккаунт создаётся выключенным:
            # доступ к переписке и заявкам появляется только после того, как
            # администратор включит его в разделе «Пользователи».
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO app_users(
                        username, display_name, role, employee_name, password_hash, enabled,
                        must_change_password, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (username, display_name, "employee", display_name[:80], encoded, 0, 0, now, now),
                )
                connection.commit()
                return True, "Заявка на доступ отправлена. Войти можно после одобрения администратором", int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                connection.rollback()
                return False, "Такой логин уже существует", 0

    def get_user_preferences(self, user_id: int) -> dict[str, str]:
        user_id = int(user_id or 0)
        if user_id <= 0:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT pref_key,pref_value FROM auth_user_preferences WHERE user_id=?",
                (user_id,),
            ).fetchall()
            return {str(row["pref_key"]): str(row["pref_value"] or "") for row in rows}

    def set_user_preference(self, user_id: int, key: str, value: str) -> tuple[bool, str]:
        user_id = int(user_id or 0)
        key = str(key or "").strip().lower()[:64]
        value = str(value or "")[:500]
        allowed = {"theme", "chat_scale", "compact_chats", "sidebar_width"}
        if user_id <= 0 or key not in allowed:
            return False, "Некорректная настройка интерфейса"
        if not self.get_user(user_id):
            return False, "Пользователь не найден"
        if key == "theme" and value not in {"light", "dark", "pink"}:
            return False, "Некорректная тема"
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO auth_user_preferences(user_id,pref_key,pref_value,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id,pref_key) DO UPDATE SET
                    pref_value=excluded.pref_value, updated_at=excluded.updated_at
                """,
                (user_id, key, value, _iso()),
            )
            connection.commit()
        return True, "Настройка интерфейса сохранена"

    def set_user_preferences(self, user_id: int, values: dict[str, str]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for key, value in dict(values or {}).items():
            ok, _ = self.set_user_preference(user_id, str(key), str(value))
            result[str(key)] = ok
        return result

    def set_employee_link(self, user_id: int, employee_name: str) -> tuple[bool, str]:
        employee_name = str(employee_name or "").strip()[:80]
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE app_users SET employee_name=?, updated_at=? WHERE id=?",
                (employee_name, _iso(), int(user_id)),
            )
            connection.commit()
            return (True, "Привязка сотрудника сохранена") if cursor.rowcount else (False, "Пользователь не найден")

    def set_password(self, user_id: int, password: str, *, must_change_password: bool = False) -> tuple[bool, str]:
        problem = password_error(password)
        if problem:
            return False, problem
        encoded = hash_password(password)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE app_users
                   SET password_hash=?, must_change_password=?, failed_attempts=0,
                       locked_until='', updated_at=?
                 WHERE id=?
                """,
                (encoded, 1 if must_change_password else 0, _iso(), int(user_id)),
            )
            if not cursor.rowcount:
                return False, "Пользователь не найден"
            connection.execute("DELETE FROM app_sessions WHERE user_id=?", (int(user_id),))
            connection.execute("DELETE FROM conversation_locks WHERE owner_user_id=?", (int(user_id),))
            connection.execute(
                """
                UPDATE password_reset_requests
                   SET status='resolved', resolved_at=?
                 WHERE user_id=? AND status='pending'
                """,
                (_iso(), int(user_id)),
            )
            connection.commit()
            return True, "Пароль изменён. Старые сеансы завершены"

    def change_own_password(self, user_id: int, current_password: str, new_password: str) -> tuple[bool, str]:
        with self._connection() as connection:
            row = connection.execute("SELECT password_hash FROM app_users WHERE id=? AND enabled=1", (int(user_id),)).fetchone()
            if not row or not verify_password(current_password, str(row["password_hash"])):
                return False, "Текущий пароль указан неверно"
        return self.set_password(int(user_id), new_password, must_change_password=False)

    def update_profile(self, user_id: int, display_name: str) -> tuple[bool, str]:
        display_name = str(display_name or "").strip()
        if not display_name or len(display_name) > 80:
            return False, "Укажите отображаемое имя до 80 символов"
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE app_users SET display_name=?, updated_at=? WHERE id=?",
                (display_name, _iso(), int(user_id)),
            )
            connection.commit()
            return (True, "Имя обновлено") if cursor.rowcount else (False, "Пользователь не найден")

    def set_enabled(self, user_id: int, enabled: bool) -> tuple[bool, str]:
        user = self.get_user(user_id)
        if not user:
            return False, "Пользователь не найден"
        if not enabled and user["role"] == "admin" and user["enabled"]:
            if self.enabled_admin_count(excluding_user_id=int(user_id)) < 1:
                return False, "Нельзя отключить последнего активного администратора"
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE app_users SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, _iso(), int(user_id)),
            )
            if not enabled:
                connection.execute("DELETE FROM app_sessions WHERE user_id=?", (int(user_id),))
                connection.execute("DELETE FROM conversation_locks WHERE owner_user_id=?", (int(user_id),))
            connection.commit()
            return True, "Учётная запись включена" if enabled else "Учётная запись отключена"

    def set_role(self, user_id: int, role: str) -> tuple[bool, str]:
        role = str(role or "").strip().lower()
        if role not in ROLES:
            return False, "Некорректная роль"
        user = self.get_user(user_id)
        if not user:
            return False, "Пользователь не найден"
        if user["role"] == "admin" and role != "admin" and user["enabled"]:
            if self.enabled_admin_count(excluding_user_id=int(user_id)) < 1:
                return False, "Нельзя понизить последнего активного администратора"
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE app_users SET role=?, updated_at=? WHERE id=?",
                (role, _iso(), int(user_id)),
            )
            connection.execute("DELETE FROM conversation_locks WHERE owner_user_id=?", (int(user_id),))
            connection.commit()
            return True, "Роль изменена"

    def delete_user(self, user_id: int) -> tuple[bool, str]:
        user = self.get_user(user_id)
        if not user:
            return False, "Пользователь не найден"
        if user["role"] == "admin" and user["enabled"]:
            if self.enabled_admin_count(excluding_user_id=int(user_id)) < 1:
                return False, "Нельзя удалить последнего активного администратора"
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM conversation_locks WHERE owner_user_id=?", (int(user_id),))
            connection.execute("DELETE FROM app_users WHERE id=?", (int(user_id),))
            connection.commit()
            return True, "Пользователь удалён"
