"""Закрепление чата за сотрудником: кто ведёт чат, передача и снятие закрепления.

Подмешивается в queue_auth.AuthStore, откуда берёт _connection().
"""
from __future__ import annotations

import sqlite3
from typing import Any

from queue_auth_common import _iso, _normalize_username


class ConversationLockStore:
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
