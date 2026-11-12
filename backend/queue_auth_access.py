"""Журнал действий администраторов, API-ключи и заявки на сброс пароля.

Подмешивается в queue_auth.AuthStore, откуда берёт _connection().
"""
from __future__ import annotations

import secrets
from typing import Any

from queue_auth_common import _iso, _normalize_username, _token_hash


class AccessStore:
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
