"""Вход по паролю, сессии и журнал входов.

Подмешивается в queue_auth.AuthStore, откуда берёт _connect(), _connection() и _public_user().
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from queue_auth_common import (
    _iso, LOCKOUT_ATTEMPTS, LOCKOUT_MINUTES, _normalize_username, _parse_iso, SESSION_HOURS,
    _token_hash, _utcnow, verify_password,
)


class SessionStore:
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
                return {"ok": False, "error": "Учётная запись отключена или ещё не одобрена администратором"}
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
