from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = max(200_000, int(os.getenv("QUEUE_AUTH_PBKDF2_ITERATIONS", "390000")))
SESSION_HOURS = max(1, min(168, int(os.getenv("QUEUE_AUTH_SESSION_HOURS", "12"))))
LOCKOUT_ATTEMPTS = max(3, min(20, int(os.getenv("QUEUE_AUTH_LOCKOUT_ATTEMPTS", "5"))))
LOCKOUT_MINUTES = max(1, min(120, int(os.getenv("QUEUE_AUTH_LOCKOUT_MINUTES", "10"))))
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ROLES = {"admin", "employee"}
_THREAD_CONTEXT = threading.local()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).replace(microsecond=0).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_username(value: str) -> str:
    return str(value or "").strip().lower()


def password_error(password: str) -> str:
    value = str(password or "")
    if len(value) < 10:
        return "Пароль должен содержать минимум 10 символов"
    if len(value) > 256:
        return "Пароль слишком длинный"
    return ""


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    problem = password_error(password)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=32,
    )
    return f"pbkdf2_{PBKDF2_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = str(encoded).split("$", 3)
        if scheme != f"pbkdf2_{PBKDF2_ALGORITHM}":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 5_000_000:
            return False
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
        actual = hashlib.pbkdf2_hmac(
            PBKDF2_ALGORITHM,
            str(password or "").encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def set_current_user(user: dict[str, Any] | None) -> None:
    _THREAD_CONTEXT.user = dict(user) if user else None


def current_user() -> dict[str, Any] | None:
    user = getattr(_THREAD_CONTEXT, "user", None)
    return dict(user) if isinstance(user, dict) else None


def clear_current_user() -> None:
    if hasattr(_THREAD_CONTEXT, "user"):
        delattr(_THREAD_CONTEXT, "user")


class AuthStore:
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
            # Safe in-place migration for databases created by 1.00.6.15-1.00.6.17.
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
            # 1.00.6.84: самостоятельная регистрация сотрудников больше не
            # ограничена одним аккаунтом на всю систему. Ограничение остаётся
            # только на уникальность логина и наличие хотя бы одного администратора.
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO app_users(
                        username, display_name, role, employee_name, password_hash, enabled,
                        must_change_password, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (username, display_name, "employee", display_name[:80], encoded, 1, 0, now, now),
                )
                connection.commit()
                return True, "Аккаунт сотрудника создан", int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                connection.rollback()
                return False, "Такой логин уже существует", 0

    def request_password_reset(self, username: str) -> str:
        username = _normalize_username(username)
        generic = "Если такой аккаунт существует, запрос на восстановление передан администратору"
        if not username:
            return generic
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT id, username FROM app_users WHERE username=? AND enabled=1",
                (username,),
            ).fetchone()
            if not row:
                return generic
            user_id = int(row["id"])
            now = _iso()
            existing = connection.execute(
                "SELECT id FROM password_reset_requests WHERE user_id=? AND status='pending'",
                (user_id,),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE password_reset_requests SET requested_at=? WHERE id=?",
                    (now, int(existing["id"])),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO password_reset_requests(user_id, username, requested_at, status, resolved_at)
                    VALUES(?,?,?,'pending','')
                    """,
                    (user_id, str(row["username"]), now),
                )
            connection.commit()
        return generic

    def list_password_reset_requests(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.id, r.user_id, r.username, r.requested_at, r.status, r.resolved_at,
                       u.display_name, u.enabled
                  FROM password_reset_requests r
                  JOIN app_users u ON u.id=r.user_id
                 WHERE r.status='pending'
                 ORDER BY r.requested_at DESC, r.id DESC
                """
            ).fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"]),
                    "display_name": str(row["display_name"] or row["username"]),
                    "requested_at": str(row["requested_at"] or ""),
                    "enabled": bool(row["enabled"]),
                }
                for row in rows
            ]

    def resolve_password_reset_requests(self, user_id: int) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE password_reset_requests
                   SET status='resolved', resolved_at=?
                 WHERE user_id=? AND status='pending'
                """,
                (_iso(), int(user_id)),
            )
            connection.commit()

    def _record_login_event_conn(
        self, connection: sqlite3.Connection, *, user_id: int = 0, username: str = "",
        success: bool = False, reason: str = "", client_ip: str = "", user_agent: str = ""
    ) -> None:
        connection.execute(
            """INSERT INTO auth_login_events(user_id,username,success,reason,client_ip,user_agent,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (int(user_id or 0), _normalize_username(username), 1 if success else 0,
             str(reason or "")[:180], str(client_ip or "")[:80], str(user_agent or "")[:300], _iso()),
        )

    def list_login_events(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit or 200)))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM auth_login_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def list_active_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit or 200)))
        self.cleanup_sessions()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.token_hash, s.user_id, s.created_at, s.expires_at, s.last_seen_at,
                       s.client_ip, s.user_agent, u.username, u.display_name, u.role
                  FROM app_sessions s JOIN app_users u ON u.id=s.user_id
                 WHERE u.enabled=1
                 ORDER BY s.last_seen_at DESC, s.created_at DESC LIMIT ?
                """, (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def revoke_session_hash(self, token_hash: str) -> bool:
        value = str(token_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            return False
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT user_id FROM app_sessions WHERE token_hash=?", (value,)).fetchone()
            cursor = connection.execute("DELETE FROM app_sessions WHERE token_hash=?", (value,))
            if row:
                remaining = connection.execute("SELECT 1 FROM app_sessions WHERE user_id=? LIMIT 1", (int(row["user_id"]),)).fetchone()
                if not remaining:
                    connection.execute("DELETE FROM conversation_locks WHERE owner_user_id=?", (int(row["user_id"]),))
            connection.commit()
            return bool(cursor.rowcount)

    def record_audit(
        self, actor: dict[str, Any] | None, action: str, *, object_type: str = "",
        object_id: str = "", details: str = ""
    ) -> None:
        actor = actor or {}
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO auth_audit_events(
                    actor_user_id,actor_username,actor_display_name,action,object_type,object_id,details,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (int(actor.get("id",0) or 0), str(actor.get("username","") or "")[:80],
                 str(actor.get("display_name","") or "")[:100], str(action or "")[:120],
                 str(object_type or "")[:80], str(object_id or "")[:120], str(details or "")[:1000], _iso()),
            )
            connection.commit()

    def list_audit_events(self, limit: int = 300) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit or 300)))
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM auth_audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]



    def create_api_key(self, actor: dict[str, Any] | None, name: str, scope: str = "read") -> tuple[bool, str, str]:
        name = str(name or "").strip()[:80]
        scope = str(scope or "read").strip().lower()
        if not name:
            return False, "Укажите название API-ключа", ""
        if scope not in {"read"}:
            return False, "Поддерживается только read-доступ", ""
        token = "eoq_" + secrets.token_urlsafe(32)
        digest = _token_hash(token)
        prefix = token[:12]
        actor = actor or {}
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO auth_api_keys(
                    name,token_hash,token_prefix,scope,enabled,created_by_user_id,created_by_name,created_at,last_used_at
                ) VALUES(?,?,?,?,1,?,?,?, '')""",
                (name, digest, prefix, scope, int(actor.get("id",0) or 0),
                 str(actor.get("display_name") or actor.get("username") or "")[:100], _iso()),
            )
            connection.commit()
        self.record_audit(actor, "api_key_created", object_type="api_key", object_id=prefix, details=name)
        return True, "API-ключ создан", token

    def list_api_keys(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit or 200)))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id,name,token_prefix,scope,enabled,created_by_user_id,created_by_name,created_at,last_used_at FROM auth_api_keys ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def revoke_api_key(self, actor: dict[str, Any] | None, key_id: int) -> tuple[bool, str]:
        key_id = int(key_id or 0)
        if key_id <= 0:
            return False, "Некорректный API-ключ"
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT id,name,token_prefix FROM auth_api_keys WHERE id=?", (key_id,)).fetchone()
            if not row:
                return False, "API-ключ не найден"
            connection.execute("UPDATE auth_api_keys SET enabled=0 WHERE id=?", (key_id,))
            connection.commit()
        self.record_audit(actor or {}, "api_key_revoked", object_type="api_key", object_id=str(row["token_prefix"]), details=str(row["name"]))
        return True, "API-ключ отключён"

    def authenticate_api_key(self, token: str, required_scope: str = "read") -> dict[str, Any] | None:
        raw = str(token or "").strip()
        if not raw.startswith("eoq_") or len(raw) < 24:
            return None
        digest = _token_hash(raw)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_api_keys WHERE token_hash=? AND enabled=1 LIMIT 1", (digest,)
            ).fetchone()
            if not row:
                return None
            scope = str(row["scope"] or "read")
            if required_scope == "read" and scope != "read":
                return None
            connection.execute("UPDATE auth_api_keys SET last_used_at=? WHERE id=?", (_iso(), int(row["id"])))
            connection.commit()
            return {"id":int(row["id"]),"name":str(row["name"]),"prefix":str(row["token_prefix"]),"scope":scope,"created_by_name":str(row["created_by_name"] or "")}

    def list_user_login_events(self, user_id: int, limit: int = 60) -> list[dict[str, Any]]:
        user_id = int(user_id or 0)
        limit = max(1, min(300, int(limit or 60)))
        if user_id <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM auth_login_events WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_user_audit_events(self, user_id: int, limit: int = 80) -> list[dict[str, Any]]:
        user_id = int(user_id or 0)
        limit = max(1, min(300, int(limit or 80)))
        if user_id <= 0:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM auth_audit_events WHERE actor_user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def active_session_count_for_user(self, user_id: int) -> int:
        user_id = int(user_id or 0)
        if user_id <= 0:
            return 0
        self.cleanup_sessions()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM app_sessions WHERE user_id=?", (user_id,)
            ).fetchone()
            return int(row["c"] if row else 0)

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

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        client_ip: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        username = _normalize_username(username)
        generic_error = "Неверный логин или пароль"
        if not username or not password:
            return {"ok": False, "error": generic_error}
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM app_users WHERE username=?", (username,)).fetchone()
            if not row:
                # Do a fake expensive hash so unknown users are not trivially distinguishable.
                hashlib.pbkdf2_hmac("sha256", str(password).encode(), b"queue-auth-missing", 120_000, dklen=32)
                self._record_login_event_conn(connection, username=username, success=False, reason="unknown_user", client_ip=client_ip, user_agent=user_agent)
                connection.commit()
                return {"ok": False, "error": generic_error}
            if not bool(row["enabled"]):
                self._record_login_event_conn(connection, user_id=int(row["id"]), username=username, success=False, reason="disabled", client_ip=client_ip, user_agent=user_agent)
                connection.commit()
                return {"ok": False, "error": "Учётная запись отключена администратором"}
            locked_until = _parse_iso(row["locked_until"])
            now = _utcnow()
            if locked_until and locked_until > now:
                minutes = max(1, int((locked_until - now).total_seconds() // 60) + 1)
                self._record_login_event_conn(connection, user_id=int(row["id"]), username=username, success=False, reason="locked", client_ip=client_ip, user_agent=user_agent)
                connection.commit()
                return {"ok": False, "error": f"Слишком много попыток. Повторите через {minutes} мин"}
            if not verify_password(password, str(row["password_hash"])):
                attempts = int(row["failed_attempts"] or 0) + 1
                lock_value = ""
                if attempts >= LOCKOUT_ATTEMPTS:
                    lock_value = _iso(now + timedelta(minutes=LOCKOUT_MINUTES))
                    attempts = 0
                connection.execute(
                    "UPDATE app_users SET failed_attempts=?, locked_until=?, updated_at=? WHERE id=?",
                    (attempts, lock_value, _iso(now), int(row["id"])),
                )
                self._record_login_event_conn(connection, user_id=int(row["id"]), username=username, success=False, reason="lockout" if lock_value else "bad_password", client_ip=client_ip, user_agent=user_agent)
                connection.commit()
                if lock_value:
                    return {"ok": False, "error": f"Слишком много попыток. Вход заблокирован на {LOCKOUT_MINUTES} мин"}
                return {"ok": False, "error": generic_error}

            user_id = int(row["id"])
            connection.execute(
                """
                UPDATE app_users
                   SET failed_attempts=0, locked_until='', last_login_at=?, updated_at=?
                 WHERE id=?
                """,
                (_iso(now), _iso(now), user_id),
            )
            token = secrets.token_urlsafe(48)
            token_hash = _token_hash(token)
            expires = now + timedelta(hours=SESSION_HOURS)
            connection.execute(
                """
                INSERT INTO app_sessions(token_hash, user_id, created_at, expires_at, last_seen_at, client_ip, user_agent)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    token_hash,
                    user_id,
                    _iso(now),
                    _iso(expires),
                    _iso(now),
                    str(client_ip or "")[:80],
                    str(user_agent or "")[:300],
                ),
            )
            self._record_login_event_conn(connection, user_id=user_id, username=username, success=True, reason="ok", client_ip=client_ip, user_agent=user_agent)
            connection.commit()
            fresh = connection.execute("SELECT * FROM app_users WHERE id=?", (user_id,)).fetchone()
            return {"ok": True, "token": token, "user": self._public_user(fresh)}

    def session_user(self, token: str, *, touch: bool = True) -> dict[str, Any] | None:
        raw = str(token or "").strip()
        if len(raw) < 32:
            return None
        token_hash = _token_hash(raw)
        now = _utcnow()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT u.*, s.expires_at, s.last_seen_at
                  FROM app_sessions s
                  JOIN app_users u ON u.id=s.user_id
                 WHERE s.token_hash=?
                """,
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            expires = _parse_iso(row["expires_at"])
            if not expires or expires <= now or not bool(row["enabled"]):
                connection.execute("DELETE FROM app_sessions WHERE token_hash=?", (token_hash,))
                connection.commit()
                return None
            if touch:
                last_seen = _parse_iso(row["last_seen_at"])
                if not last_seen or now - last_seen >= timedelta(minutes=5):
                    connection.execute(
                        "UPDATE app_sessions SET last_seen_at=? WHERE token_hash=?",
                        (_iso(now), token_hash),
                    )
                    connection.commit()
            return self._public_user(row)

    def revoke_session(self, token: str) -> None:
        raw = str(token or "").strip()
        if not raw:
            return
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM app_sessions WHERE token_hash=?", (_token_hash(raw),))
            connection.commit()

    def revoke_user_sessions(self, user_id: int) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM app_sessions WHERE user_id=?", (int(user_id),))
            connection.commit()

    def cleanup_sessions(self, *, connection: sqlite3.Connection | None = None) -> None:
        owns_connection = connection is None
        db = connection or self._connect()
        try:
            db.execute("DELETE FROM app_sessions WHERE expires_at<=?", (_iso(),))
            db.commit()
        finally:
            if owns_connection:
                db.close()

    @staticmethod
    def _public_conversation_lock(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "conversation_id": str(row["conversation_id"]),
            "conversation_kind": str(row["conversation_kind"] or "chat"),
            "owner_user_id": int(row["owner_user_id"] or 0),
            "owner_username": str(row["owner_username"] or ""),
            "owner_display_name": str(row["owner_display_name"] or ""),
            "owner_role": str(row["owner_role"] or ""),
            "acquired_at": str(row["acquired_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    @staticmethod
    def _same_lock_owner(row: sqlite3.Row | dict[str, Any], user: dict[str, Any]) -> bool:
        user_id = int(user.get("id", 0) or 0)
        owner_user_id = int(row["owner_user_id"] or 0)
        if user_id > 0 and owner_user_id > 0:
            return user_id == owner_user_id
        username = _normalize_username(str(user.get("username", "")))
        owner_username = _normalize_username(str(row["owner_username"] or ""))
        return bool(username and owner_username and username == owner_username)

    def conversation_lock(self, conversation_id: str) -> dict[str, Any] | None:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_locks WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            owner_user_id = int(row["owner_user_id"] or 0)
            if owner_user_id > 0:
                owner = connection.execute(
                    "SELECT enabled FROM app_users WHERE id=?",
                    (owner_user_id,),
                ).fetchone()
                if not owner or not bool(owner["enabled"]):
                    connection.execute(
                        "DELETE FROM conversation_locks WHERE conversation_id=?",
                        (conversation_id,),
                    )
                    connection.commit()
                    return None
            return self._public_conversation_lock(row)

    def claim_conversation(
        self,
        conversation_id: str,
        conversation_kind: str,
        user: dict[str, Any],
        *,
        display_name: str = "",
    ) -> tuple[bool, dict[str, Any] | None]:
        conversation_id = str(conversation_id or "").strip()
        conversation_kind = "group" if str(conversation_kind).strip().lower() == "group" else "chat"
        if not conversation_id or not isinstance(user, dict):
            return False, None
        user_id = int(user.get("id", 0) or 0)
        username = _normalize_username(str(user.get("username", "")))
        role = str(user.get("role", "employee") or "employee").strip().lower()
        visible_name = str(display_name or user.get("display_name") or username or "Пользователь").strip()[:100]
        if user_id <= 0 and not username:
            return False, None
        now = _iso()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM conversation_locks WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if row and not self._same_lock_owner(row, user):
                connection.rollback()
                return False, self._public_conversation_lock(row)
            if row:
                connection.execute(
                    """
                    UPDATE conversation_locks
                       SET conversation_kind=?, owner_user_id=?, owner_username=?,
                           owner_display_name=?, owner_role=?, updated_at=?
                     WHERE conversation_id=?
                    """,
                    (conversation_kind, user_id, username, visible_name, role, now, conversation_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO conversation_locks(
                        conversation_id, conversation_kind, owner_user_id, owner_username,
                        owner_display_name, owner_role, acquired_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (conversation_id, conversation_kind, user_id, username, visible_name, role, now, now),
                )
            connection.commit()
            fresh = connection.execute(
                "SELECT * FROM conversation_locks WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            return True, self._public_conversation_lock(fresh)

    def release_conversation(
        self,
        conversation_id: str,
        user: dict[str, Any],
        *,
        force: bool = False,
    ) -> tuple[bool, str]:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return False, "Диалог не выбран"
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_locks WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return True, "Диалог уже свободен"
            is_owner = self._same_lock_owner(row, user)
            can_force = force and str(user.get("role", "")) == "admin"
            if not is_owner and not can_force:
                return False, "Диалог занят другим пользователем"
            connection.execute(
                "DELETE FROM conversation_locks WHERE conversation_id=?",
                (conversation_id,),
            )
            connection.commit()
            return True, "Диалог освобождён"

    def transfer_conversation(
        self,
        conversation_id: str,
        actor_user: dict[str, Any],
        target_user_id: int,
        *,
        target_display_name: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return False, "Диалог не выбран", None
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM conversation_locks WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not row:
                connection.rollback()
                return False, "Сначала возьмите диалог в работу", None
            is_owner = self._same_lock_owner(row, actor_user)
            if not is_owner and str(actor_user.get("role", "")) != "admin":
                connection.rollback()
                return False, "Передать диалог может текущий владелец или администратор", self._public_conversation_lock(row)
            target = connection.execute(
                "SELECT * FROM app_users WHERE id=? AND enabled=1",
                (int(target_user_id),),
            ).fetchone()
            if not target:
                connection.rollback()
                return False, "Пользователь для передачи не найден или отключён", self._public_conversation_lock(row)
            now = _iso()
            visible_name = str(target_display_name or target["display_name"] or target["username"]).strip()[:100]
            connection.execute(
                """
                UPDATE conversation_locks
                   SET owner_user_id=?, owner_username=?, owner_display_name=?, owner_role=?, updated_at=?
                 WHERE conversation_id=?
                """,
                (int(target["id"]), str(target["username"]), visible_name, str(target["role"]), now, conversation_id),
            )
            connection.commit()
            fresh = connection.execute(
                "SELECT * FROM conversation_locks WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            return True, "Диалог передан", self._public_conversation_lock(fresh)

    def release_user_locks(self, user_id: int = 0, username: str = "") -> int:
        user_id = int(user_id or 0)
        username = _normalize_username(username)
        if user_id <= 0 and not username:
            return 0
        with self._lock, self._connection() as connection:
            if user_id > 0:
                cursor = connection.execute(
                    "DELETE FROM conversation_locks WHERE owner_user_id=?",
                    (user_id,),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM conversation_locks WHERE lower(owner_username)=?",
                    (username,),
                )
            connection.commit()
            return int(cursor.rowcount or 0)

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
