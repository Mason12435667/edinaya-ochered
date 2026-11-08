"""Сохранённые контакты и группы WhatsApp, разрешения на звонки и ручной режим чата.

Подмешивается в ticketing.TicketStore, откуда берёт connection() и методы других хранилищ.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from queue_core import normalize_message, utc_now


class ContactStore:
    def list_whatsapp_contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            manual_rows = connection.execute(
                """
                SELECT chat_id, phone, name, created_at
                FROM whatsapp_contacts
                ORDER BY name COLLATE NOCASE, created_at DESC
                """
            ).fetchall()
            rows = connection.execute(
                """
                SELECT chat_id, phone, sender, summary, created_at
                FROM tickets
                WHERE source = 'whatsapp' AND (chat_id <> '' OR phone <> '')
                ORDER BY id DESC
                LIMIT 500
                """
            ).fetchall()
        contacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in manual_rows:
            chat_id = str(row["chat_id"] or "")
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            contacts.append(
                {
                    "id": chat_id,
                    "name": str(row["name"] or row["phone"] or "Пользователь WhatsApp"),
                    "phone": str(row["phone"] or ""),
                    "last_message": "Добавлен администратором",
                    "created_at": str(row["created_at"] or ""),
                    "manual": True,
                }
            )
            if len(contacts) >= max(1, limit):
                return contacts
        for row in rows:
            chat_id = str(row["chat_id"] or "")
            if not chat_id:
                digits = re.sub(r"\D", "", str(row["phone"] or ""))
                chat_id = f"{digits}@c.us" if digits else ""
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            contacts.append(
                {
                    "id": chat_id,
                    "name": str(row["sender"] or "Пользователь WhatsApp"),
                    "phone": str(row["phone"] or ""),
                    "last_message": str(row["summary"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "manual": False,
                }
            )
            if len(contacts) >= max(1, limit):
                break
        return contacts

    def save_whatsapp_contact(self, name: str, phone: str, chat_id: str) -> bool:
        clean_name = normalize_message(name)[:100]
        clean_phone = normalize_message(phone)[:30]
        clean_chat_id = normalize_message(chat_id)[:120]
        if not clean_name or not clean_phone or not clean_chat_id.endswith("@c.us"):
            return False
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO whatsapp_contacts (chat_id, phone, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    phone = excluded.phone,
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (clean_chat_id, clean_phone, clean_name, now, now),
            )
        return True

    def delete_whatsapp_contact(self, chat_id: str) -> bool:
        clean_chat_id = normalize_message(chat_id)[:120]
        if not clean_chat_id:
            return False
        with self.connection() as connection:
            row = connection.execute(
                "SELECT phone FROM whatsapp_contacts WHERE chat_id = ? LIMIT 1",
                (clean_chat_id,),
            ).fetchone()
            phone_digits = re.sub(r"\D", "", str(row["phone"] if row else ""))[:24]
            if phone_digits:
                # Удаляем и возможный старый дубль того же номера. Это важно
                # после объединения @lid/@c.us: один оставшийся alias не должен
                # продолжать считать пользователя ручным контактом.
                cursor = connection.execute(
                    """
                    DELETE FROM whatsapp_contacts
                    WHERE chat_id = ?
                       OR chat_id = ?
                       OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') = ?
                    """,
                    (clean_chat_id, f"{phone_digits}@c.us", phone_digits),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM whatsapp_contacts WHERE chat_id = ?",
                    (clean_chat_id,),
                )

            # Контакт после удаления должен сразу стать обычным пользователем.
            # Чистим старые одноразовые разрешения на звонки и контекст для
            # точного chat_id и канонического номерного @c.us alias.
            cleanup_keys = {clean_chat_id}
            if phone_digits:
                cleanup_keys.add(f"{phone_digits}@c.us")
            for cleanup_key in cleanup_keys:
                if not cleanup_key:
                    continue
                connection.execute(
                    "DELETE FROM whatsapp_call_permissions WHERE contact_key = ?",
                    (cleanup_key,),
                )
                connection.execute(
                    "DELETE FROM conversation_contexts WHERE contact_key = ?",
                    (cleanup_key,),
                )
        return cursor.rowcount > 0

    def list_manual_whatsapp_contacts(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, phone, name, created_at, updated_at
                FROM whatsapp_contacts
                ORDER BY name COLLATE NOCASE, created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def manual_whatsapp_contact(
        self,
        chat_id: str = "",
        phone: str = "",
    ) -> dict[str, Any] | None:
        clean_chat_id = normalize_message(chat_id)[:120]
        phone_digits = re.sub(r"\D", "", normalize_message(phone))[:24]
        chat_digits = clean_chat_id.split("@", 1)[0] if clean_chat_id.endswith("@c.us") else ""
        candidates = [digits for digits in (phone_digits, chat_digits) if digits]
        with self.connection() as connection:
            if clean_chat_id:
                row = connection.execute(
                    """
                    SELECT chat_id, phone, name, created_at, updated_at
                    FROM whatsapp_contacts
                    WHERE chat_id = ?
                    LIMIT 1
                    """,
                    (clean_chat_id,),
                ).fetchone()
                if row:
                    return dict(row)
            for digits in candidates:
                row = connection.execute(
                    """
                    SELECT chat_id, phone, name, created_at, updated_at
                    FROM whatsapp_contacts
                    WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') = ?
                       OR chat_id = ?
                    LIMIT 1
                    """,
                    (digits, f"{digits}@c.us"),
                ).fetchone()
                if row:
                    return dict(row)
        return None

    def is_manual_whatsapp_contact(self, chat_id: str = "", phone: str = "") -> bool:
        return self.manual_whatsapp_contact(chat_id, phone) is not None

    def update_whatsapp_contact_display_name(
        self, chat_id: str, name: str, phone: str = ""
    ) -> int:
        clean_chat_id = normalize_message(chat_id)[:120]
        clean_name = normalize_message(name)[:100].strip()
        if not clean_name or clean_name.casefold() in {"система", "system", "рабочий whatsapp"}:
            return 0
        digits_name = re.sub(r"\D", "", clean_name)
        if digits_name and len(digits_name) >= 8 and not re.search(r"[A-Za-zА-Яа-яЁё]", clean_name):
            return 0
        phone_digits = re.sub(r"\D", "", normalize_message(phone))[:24]
        candidates = {clean_chat_id} if clean_chat_id else set()
        if phone_digits:
            if len(phone_digits) == 11 and phone_digits.startswith("8"):
                phone_digits = "7" + phone_digits[1:]
            candidates.add(f"{phone_digits}@c.us")
        candidates.discard("")
        if not candidates:
            return 0
        now = utc_now()
        changed = 0
        with self.connection() as connection:
            for candidate in candidates:
                manual = connection.execute("SELECT name FROM whatsapp_contacts WHERE chat_id=?", (candidate,)).fetchone()
                if manual and manual["name"]: clean_name = manual["name"]
                cursor = connection.execute(
                    "UPDATE whatsapp_chats SET name=?, updated_at=? WHERE chat_id=?",
                    (clean_name, now, candidate),
                )
                changed += int(cursor.rowcount or 0)
                connection.execute(
                    "UPDATE tickets SET sender=?, updated_at=? WHERE source='whatsapp' AND chat_id=?",
                    (clean_name, now, candidate),
                )
        return changed

    def replace_whatsapp_groups(self, groups: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("DELETE FROM whatsapp_groups WHERE added_by_admin IN (0, 2)")
            for group in groups:
                chat_id = normalize_message(str(group.get("id", "")))[:120]
                if not chat_id.endswith("@g.us"):
                    continue
                try:
                    participants = max(0, int(group.get("participant_count", 0) or 0))
                except (TypeError, ValueError):
                    participants = 0
                connection.execute(
                    """
                    INSERT INTO whatsapp_groups
                        (chat_id, name, participant_count, added_by_admin, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        name = excluded.name,
                        participant_count = excluded.participant_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_id,
                        normalize_message(str(group.get("name", "")))[:100]
                        or "Группа WhatsApp",
                        participants,
                        now,
                    ),
                )

    def save_whatsapp_group(self, name: str, chat_id: str) -> dict[str, Any] | None:
        clean_name = normalize_message(name)[:100]
        clean_chat_id = normalize_message(chat_id)[:120]
        if not clean_name or not re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", clean_chat_id):
            return None
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO whatsapp_groups
                    (chat_id, name, participant_count, added_by_admin, updated_at)
                VALUES (?, ?, 0, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    name = excluded.name,
                    added_by_admin = 1,
                    updated_at = excluded.updated_at
                """,
                (clean_chat_id, clean_name, now),
            )
        return {"chat_id": clean_chat_id, "name": clean_name}

    def set_whatsapp_group_muted(self, chat_id: str, muted: bool) -> bool:
        clean_chat_id = normalize_message(chat_id)[:120]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", clean_chat_id):
            return False
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE whatsapp_groups SET muted = ?, updated_at = ? WHERE chat_id = ? AND added_by_admin = 1",
                (1 if muted else 0, utc_now(), clean_chat_id),
            )
            if muted:
                # Старое упоминание не должно всплыть после последующего размьюта.
                connection.execute(
                    "UPDATE whatsapp_chats SET mention_unread_count = 0 WHERE chat_id = ?",
                    (clean_chat_id,),
                )
        return cursor.rowcount > 0

    def delete_whatsapp_group(self, chat_id: str) -> bool:
        clean_chat_id = normalize_message(chat_id)[:120]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", clean_chat_id):
            return False
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE whatsapp_groups SET added_by_admin = 2, updated_at = ? WHERE chat_id = ?",
                (utc_now(), clean_chat_id),
            )
        return cursor.rowcount > 0

    def list_whatsapp_groups(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT g.chat_id, g.name, g.participant_count, g.added_by_admin, g.muted, g.updated_at,
                       COALESCE(c.last_message, '') AS last_message,
                       COALESCE(c.last_timestamp, 0) AS last_timestamp,
                       COALESCE(c.unread_count, 0) AS unread_count,
                       COALESCE(c.mention_unread_count, 0) AS mention_unread_count,
                       COALESCE((
                           SELECT m.from_me
                           FROM whatsapp_chat_messages AS m
                           WHERE m.chat_id = g.chat_id AND m.deleted = 0
                           ORDER BY m.message_timestamp DESC, m.id DESC
                           LIMIT 1
                       ), 0) AS last_from_me
                FROM whatsapp_groups AS g
                LEFT JOIN whatsapp_chats AS c ON c.chat_id = g.chat_id
                WHERE g.added_by_admin = 1
                ORDER BY c.last_timestamp DESC, g.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_discovered_whatsapp_groups(self) -> list[dict[str, Any]]:
        """Groups visible to WhatsApp but not currently enabled in the system."""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, name, participant_count, added_by_admin, updated_at
                FROM whatsapp_groups
                WHERE added_by_admin = 0
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def consume_whatsapp_call_permission(self, contact_key: str) -> bool:
        clean_key = normalize_message(contact_key)[:120]
        if not clean_key:
            return False
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT expires_at FROM whatsapp_call_permissions WHERE contact_key = ?",
                (clean_key,),
            ).fetchone()
            connection.execute(
                "DELETE FROM whatsapp_call_permissions WHERE contact_key = ?",
                (clean_key,),
            )
        return bool(row and str(row["expires_at"]) >= now)

    def enable_manual_chat_mode(self, chat_id: str, actor: str, minutes: int = 30) -> bool:
        """Employee opt-outs persist; automatic review pauses expire."""
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id or chat_id.endswith("@g.us"):
            return False
        now = datetime.now(timezone.utc).replace(microsecond=0)
        # Существующая таблица требует expires_at. Используем далёкую дату как
        # постоянное состояние, чтобы не менять схему БД при обновлении.
        automatic = actor == "Бот: нужен разбор"
        expires = ((now + timedelta(minutes=max(1, min(120, int(minutes))))).isoformat()
                   if automatic else "9999-12-31T23:59:59+00:00")
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO whatsapp_manual_chat_modes(chat_id, expires_at, actor, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET expires_at = excluded.expires_at, actor = excluded.actor, updated_at = excluded.updated_at
                   WHERE excluded.actor <> 'Бот: нужен разбор'
                      OR whatsapp_manual_chat_modes.actor = 'Бот: нужен разбор'""",
                (chat_id, expires, actor, now.isoformat()),
            )
            if not cursor.rowcount:
                return False  # Automatic review must never replace an employee opt-out.
            self._audit(connection, "auto_reply", actor,
                        "Автоответчик приостановлен для разбора" if automatic else "Автоответчик выключен сотрудником",
                        "chat", chat_id, created_at=now.isoformat())
        return True

    def disable_manual_chat_mode(self, chat_id: str, actor: str = "") -> bool:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id:
            return False
        with self.connection() as connection:
            cursor = connection.execute("DELETE FROM whatsapp_manual_chat_modes WHERE chat_id = ?", (chat_id,))
            if cursor.rowcount:
                self._audit(connection, "auto_reply", actor or "Сотрудник", "Автоответчик включен сотрудником", "chat", chat_id)
            return cursor.rowcount > 0

    def manual_chat_mode(self, chat_id: str) -> dict[str, Any] | None:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT chat_id, expires_at, actor, updated_at FROM whatsapp_manual_chat_modes WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if not row:
            return None
        result = {key: row[key] for key in ("chat_id", "expires_at", "actor")}
        if row["actor"] == "Бот: нужен разбор":
            try:
                # Legacy automatic pauses used the employee's infinite sentinel.
                legacy = str(row["expires_at"]).startswith("9999-")
                expires = datetime.fromisoformat(str(row["updated_at"] if legacy else row["expires_at"]))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if legacy:
                    expires += timedelta(minutes=30)
                if expires <= datetime.now(timezone.utc):
                    return None
                result["expires_at"] = expires.isoformat()
            except (ValueError, TypeError, OverflowError):
                # Keep a malformed record blocked for explicit operator review.
                pass
        return result
