"""Unread counters and the notification feed.

Mixed into ticketing.TicketStore, which provides connection() and the other stores' helpers.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import queue_message_identity
from queue_core import normalize_message


def epoch_from_iso(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, TypeError, OSError):
        return 0


class NotificationStore:
    def first_unread_whatsapp_contact(self) -> str:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT c.chat_id
                FROM whatsapp_chats AS c
                INNER JOIN whatsapp_contacts AS m ON m.chat_id = c.chat_id
                WHERE c.unread_count > 0
                  AND c.chat_id NOT LIKE '%@g.us'
                  AND c.chat_id NOT LIKE '%@broadcast'
                  AND c.chat_id NOT LIKE '%@newsletter'
                ORDER BY c.last_timestamp DESC, c.updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["chat_id"]) if row else ""

    def notification_counts(self) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'new' AND category <> 'support' THEN 1 ELSE 0 END) AS tickets,
                    SUM(CASE WHEN status = 'new' AND category = 'support' THEN 1 ELSE 0 END) AS support
                FROM tickets
                """
            ).fetchone()
            groups = connection.execute(
                """
                SELECT COALESCE(SUM(c.unread_count), 0) AS amount
                FROM whatsapp_chats AS c
                INNER JOIN whatsapp_groups AS g ON g.chat_id = c.chat_id
                WHERE g.added_by_admin = 1 AND COALESCE(g.muted, 0) = 0
                """
            ).fetchone()
            contacts = connection.execute(
                """
                SELECT COALESCE(SUM(c.unread_count), 0) AS amount
                FROM whatsapp_chats AS c
                WHERE c.chat_id NOT LIKE '%@g.us'
                  AND c.chat_id NOT LIKE '%@broadcast'
                  AND c.chat_id NOT LIKE '%@newsletter'
                """
            ).fetchone()
        return {
            "tickets": int(rows["tickets"] or 0),
            "support": int(rows["support"] or 0),
            "groups": int(groups["amount"] or 0),
            "contacts": int(contacts["amount"] or 0),
        }

    def notification_events(self, limit: int = 12) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        # QUEUE_1_00_5_REPLY_LIGHT_INSTRUCTION: recognise a real WhatsApp Reply
        # to one of our outgoing messages. Exact provider ID is preferred; the
        # stanza fallback covers the same message seen through @lid/@c.us aliases.
        def quoted_outgoing(connection: sqlite3.Connection, chat_id: str, quoted_key: str):
            key = normalize_message(str(quoted_key or ""))[:160]
            if not key:
                return None
            row = connection.execute(
                """SELECT message_key, body, media_name, sender, message_timestamp
                   FROM whatsapp_chat_messages
                   WHERE chat_id=? AND from_me=1 AND deleted=0 AND message_key=?
                   ORDER BY id DESC LIMIT 1""",
                (chat_id, key),
            ).fetchone()
            if row is not None:
                return row
            _, stanza = queue_message_identity.parts(key)
            if len(stanza) < 8:
                return None
            candidates = connection.execute(
                """SELECT message_key, body, media_name, sender, message_timestamp
                   FROM whatsapp_chat_messages
                   WHERE chat_id=? AND from_me=1 AND deleted=0 AND substr(message_key, -?)=?
                   ORDER BY id DESC LIMIT 12""",
                (chat_id, len(stanza), stanza),
            ).fetchall()
            for candidate in candidates:
                if queue_message_identity.same(str(candidate["message_key"] or ""), key):
                    return candidate
            return None

        with self.connection() as connection:
            tickets = connection.execute(
                """SELECT id, sender, phone, category, title, created_at
                   FROM tickets WHERE status = 'new' ORDER BY id DESC LIMIT ?""",
                (max(1, min(limit, 20)),),
            ).fetchall()
            for row in tickets:
                category = str(row["category"] or "")
                events.append({
                    "id": f"ticket:{int(row['id'])}",
                    "kind": "support" if category == "support" else "ticket",
                    "title": "Новый вопрос в поддержку" if category == "support" else f"Новая заявка №{int(row['id'])}",
                    "detail": str(row["title"] or "Заявка"),
                    "source": " · ".join(part for part in [str(row["sender"] or ""), str(row["phone"] or "")] if part),
                    "href": f"/ticket?id={int(row['id'])}",
                    "timestamp": epoch_from_iso(str(row["created_at"] or "")),
                })
            contacts = connection.execute(
                """SELECT c.chat_id, COALESCE(NULLIF(m.name,''), c.name, '') AS name,
                          w.body AS last_message, w.message_timestamp AS last_timestamp,
                          COALESCE(m.phone,'') AS phone, w.message_key,
                          w.quoted_message_key, w.quoted_body, w.quoted_sender,
                          w.media_name AS last_media_name
                   FROM whatsapp_chats AS c
                   LEFT JOIN whatsapp_contacts AS m ON m.chat_id = c.chat_id
                   JOIN whatsapp_chat_messages w ON w.id=(SELECT id FROM whatsapp_chat_messages WHERE chat_id=c.chat_id AND from_me=0 AND deleted=0 ORDER BY message_timestamp DESC,id DESC LIMIT 1)
                   WHERE c.unread_count > 0
                     AND c.chat_id NOT LIKE '%@g.us'
                     AND c.chat_id NOT LIKE '%@broadcast'
                     AND c.chat_id NOT LIKE '%@newsletter'
                   ORDER BY c.last_timestamp DESC LIMIT 6"""
            ).fetchall()
            for row in contacts:
                chat_id = str(row["chat_id"] or "")
                fallback_phone = chat_id.split("@")[0] if "@" in chat_id else chat_id
                source_name = str(row["name"] or "").strip()
                if source_name.casefold() in {"система", "system", "рабочий whatsapp"}:
                    source_name = ""
                source_phone = str(row["phone"] or "").strip() or (f"+{fallback_phone}" if fallback_phone.isdigit() else "")
                quoted = quoted_outgoing(connection, chat_id, str(row["quoted_message_key"] or ""))
                is_reply = quoted is not None
                reply_text = str(row["last_message"] or row["last_media_name"] or "Новое сообщение")
                quoted_context = ""
                if quoted is not None:
                    quoted_context = str(quoted["body"] or quoted["media_name"] or row["quoted_body"] or "Сообщение")[:500]
                event_prefix = "reply" if is_reply else "contact"
                events.append({
                    "id": f"{event_prefix}:{chat_id}:{row['message_key']}",
                    "chat_id": chat_id, "message_key": row["message_key"],
                    "kind": "contact",
                    "reply_to_our_message": is_reply,
                    "title": "Ответ на ваше сообщение" if is_reply else "Новое сообщение WhatsApp",
                    "detail": reply_text,
                    "context": quoted_context,
                    "source": " · ".join(part for part in [source_name, source_phone] if part) or "WhatsApp",
                    "href": f"/whatsapp?chat_id={chat_id}&message_id={row['message_key']}",
                    "timestamp": int(row["last_timestamp"] or 0),
                })
            groups = connection.execute(
                """SELECT c.chat_id, g.name, w.body AS last_message, w.message_timestamp AS last_timestamp,
                          w.message_key, w.notify, w.quoted_message_key, w.quoted_body, w.quoted_sender,
                          w.media_name AS last_media_name
                   FROM whatsapp_chats AS c
                   INNER JOIN whatsapp_groups AS g ON g.chat_id = c.chat_id
                   JOIN whatsapp_chat_messages w ON w.id=(
                       SELECT id FROM whatsapp_chat_messages
                       WHERE chat_id=c.chat_id AND from_me=0 AND deleted=0
                       ORDER BY message_timestamp DESC,id DESC LIMIT 1
                   )
                   WHERE g.added_by_admin = 1
                     AND COALESCE(g.muted, 0) = 0
                     AND c.unread_count > 0
                   ORDER BY w.message_timestamp DESC LIMIT 6"""
            ).fetchall()
            for row in groups:
                mentioned = bool(row["notify"])
                group_chat_id = str(row["chat_id"] or "")
                quoted = quoted_outgoing(connection, group_chat_id, str(row["quoted_message_key"] or ""))
                is_reply = quoted is not None
                quoted_context = str(quoted["body"] or quoted["media_name"] or row["quoted_body"] or "Сообщение")[:500] if quoted is not None else ""
                title = "Ответ на ваше сообщение в группе" if is_reply else ("Упоминание в группе" if mentioned else "Новое сообщение в группе")
                detail = str(row["last_message"] or row["last_media_name"] or ("Вас упомянули" if mentioned else "Новое сообщение"))
                event_prefix = "group-reply" if is_reply else "group"
                events.append({
                    "id": f"{event_prefix}:{group_chat_id}:{row['message_key']}",
                    "chat_id": group_chat_id, "message_key": row["message_key"],
                    "kind": "group",
                    "mentioned": mentioned,
                    "reply_to_our_message": is_reply,
                    "title": title,
                    "detail": detail,
                    "context": quoted_context,
                    "source": str(row["name"] or "Группа WhatsApp"),
                    "href": f"/groups?chat_id={group_chat_id}&message_id={row['message_key']}",
                    "timestamp": int(row["last_timestamp"] or 0),
                })
        events.sort(key=lambda item: int(item.get("timestamp", 0) or 0), reverse=True)
        return events[:max(1, min(limit, 20))]
