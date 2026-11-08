"""Сохранённые чаты и сообщения WhatsApp: синхронизация, статусы доставки, реакции, связь с заявками.

Подмешивается в ticketing.TicketStore, откуда берёт connection() и методы других хранилищ.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import queue_message_identity
import queue_quote_lookup
from queue_core import normalize_message, utc_now


class MessageStore:
    def upsert_whatsapp_chats(self, chats: list[dict[str, Any]]) -> None:
        if not chats:
            return
        now = utc_now()
        with self.connection() as connection:
            for chat in chats:
                chat_id = normalize_message(str(chat.get("id", "")))[:120]
                chat_id = self._canonical_whatsapp_chat_id(connection, chat_id)
                if not chat_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO whatsapp_chats
                        (chat_id, name, last_message, last_timestamp, unread_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE whatsapp_chats.name END,
                        last_message = CASE
                            WHEN excluded.last_timestamp >= whatsapp_chats.last_timestamp
                            THEN excluded.last_message ELSE whatsapp_chats.last_message END,
                        last_timestamp = MAX(whatsapp_chats.last_timestamp, excluded.last_timestamp),
                        unread_count = whatsapp_chats.unread_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_id,
                        normalize_message(str(chat.get("name", "")))[:100],
                        normalize_message(str(chat.get("last_message", "")))[:160],
                        max(0, int(chat.get("timestamp", 0) or 0)),
                        0,
                        now,
                    ),
                )
            self._merge_whatsapp_lid_aliases(connection)

    def save_whatsapp_chat_messages(
        self,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id or not messages:
            return
        now = utc_now()
        with self.connection() as connection:
            chat_id = self._canonical_whatsapp_chat_id(connection, chat_id)
            for item in messages:
                body = normalize_message(str(item.get("body", "")))[:32000]
                timestamp = max(0, int(item.get("timestamp", 0) or 0))
                from_me = bool(item.get("from_me"))
                ack = max(0, min(4, int(item.get("ack", 0) or 0)))
                message_key = normalize_message(str(item.get("id", "")))[:160]
                if not message_key:
                    continue  # No stable provider ID: wait for the next valid observation.
                _, stanza = queue_message_identity.parts(message_key)
                if len(stanza) >= 8:
                    variants = connection.execute("SELECT message_key FROM whatsapp_chat_messages WHERE chat_id=? AND (message_key=? OR substr(message_key, -?)=?)", (chat_id, stanza, len(stanza), stanza)).fetchall()
                    variants = [row["message_key"] for row in variants if queue_message_identity.same(row["message_key"], message_key)]
                    if len(variants) == 1: message_key = variants[0]
                sender = normalize_message(str(item.get("sender", "")))[:100]
                sender_phone = normalize_message(str(item.get("sender_phone", "")))[:50]
                sender_id = normalize_message(str(item.get("sender_id", "")))[:120]
                contact_key = sender_id if chat_id.endswith("@g.us") else chat_id
                manual = connection.execute("SELECT name FROM whatsapp_contacts WHERE chat_id=?", (contact_key,)).fetchone()
                if manual and not from_me and manual["name"]:
                    sender = manual["name"]
                # Provider IDs, not matching text, identify a message. Repeated
                # identical messages can be intentional and must stay separate.
                media_path = str(item.get("media_path", ""))[:500]
                media_mime = normalize_message(str(item.get("media_mime", "")))[:120]
                media_name = normalize_message(str(item.get("media_name", "")))[:180]
                transcript = normalize_message(str(item.get("transcript", "")))[:8000]
                try:
                    ticket_id = max(0, int(item.get("ticket_id", 0) or 0))
                except (ValueError, TypeError):
                    ticket_id = 0
                raw_mentions = item.get("mentions", [])
                if not isinstance(raw_mentions, list):
                    raw_mentions = []
                mentions_json = json.dumps(raw_mentions[:100], ensure_ascii=False)
                # WhatsApp can render a group-wide @all mention without returning
                # it through the ordinary mention metadata. Treat a standalone
                # inbound @all token as a real notification mention as well.
                all_mention = bool(
                    chat_id.endswith("@g.us")
                    and not from_me
                    and re.search(r"(?<!\w)@all(?!\w)", body, re.IGNORECASE)
                )
                notify = 1 if item.get("notify") or all_mention else 0
                quoted_message_key = normalize_message(str(item.get("quoted_message_key", "")))[:160]
                quoted_body = normalize_message(str(item.get("quoted_body", "")))[:1200]
                quoted_sender = normalize_message(str(item.get("quoted_sender", "")))[:100]
                forwarded = 1 if item.get("forwarded") else 0
                edited = 1 if item.get("edited") else 0
                edit_timestamp = max(0, int(item.get("edit_timestamp", 0) or 0))
                deleted = 1 if item.get("deleted") else 0
                raw_reactions = item.get("reactions", {})
                reaction_map: dict[str, str] = {}
                if isinstance(raw_reactions, dict):
                    for reactor, emoji in list(raw_reactions.items())[:200]:
                        reactor_key = normalize_message(str(reactor))[:160]
                        reaction_emoji = str(emoji or "")[:32]
                        if reactor_key and reaction_emoji:
                            reaction_map[reactor_key] = reaction_emoji
                elif isinstance(raw_reactions, list):
                    for reaction in raw_reactions[:200]:
                        if not isinstance(reaction, dict):
                            continue
                        reactor_key = normalize_message(str(reaction.get("sender_id", "")))[:160]
                        if reaction.get("from_me"):
                            reactor_key = "__me__"
                        reaction_emoji = str(reaction.get("emoji", "") or "")[:32]
                        if reactor_key and reaction_emoji:
                            reaction_map[reactor_key] = reaction_emoji
                reactions_json = json.dumps(reaction_map, ensure_ascii=False, separators=(",", ":"))
                exists = connection.execute(
                    "SELECT 1 FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                    (chat_id, message_key),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO whatsapp_chat_messages
                        (message_key, chat_id, from_me, sender, sender_phone, sender_id, body, message_type,
                         message_timestamp, ack, media_path, media_mime, media_name,
                         transcript, ticket_id, mentions_json, notify, quoted_message_key, quoted_body,
                         quoted_sender, forwarded, edited, deleted, reactions_json, created_at, edit_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_key) DO UPDATE SET
                        ack = MAX(whatsapp_chat_messages.ack, excluded.ack),
                        sender = CASE WHEN whatsapp_chat_messages.from_me=1 AND whatsapp_chat_messages.sender='Система' THEN whatsapp_chat_messages.sender WHEN excluded.sender <> '' THEN excluded.sender ELSE whatsapp_chat_messages.sender END,
                        sender_phone = CASE WHEN excluded.sender_phone <> '' THEN excluded.sender_phone ELSE whatsapp_chat_messages.sender_phone END,
                        sender_id = CASE WHEN excluded.sender_id <> '' THEN excluded.sender_id ELSE whatsapp_chat_messages.sender_id END,
                        body = CASE WHEN whatsapp_chat_messages.deleted=1 THEN '' WHEN whatsapp_chat_messages.edited=1 AND (excluded.edited=0 OR excluded.edit_timestamp <= whatsapp_chat_messages.edit_timestamp) AND excluded.deleted=0 THEN whatsapp_chat_messages.body WHEN excluded.body <> '' OR excluded.deleted=1 OR excluded.edited=1 THEN excluded.body ELSE whatsapp_chat_messages.body END,
                        message_type = CASE WHEN excluded.message_type <> '' THEN excluded.message_type ELSE whatsapp_chat_messages.message_type END,
                        media_path = CASE WHEN excluded.media_path <> '' THEN excluded.media_path ELSE whatsapp_chat_messages.media_path END,
                        media_mime = CASE WHEN excluded.media_mime <> '' THEN excluded.media_mime ELSE whatsapp_chat_messages.media_mime END,
                        media_name = CASE WHEN excluded.media_name <> '' THEN excluded.media_name ELSE whatsapp_chat_messages.media_name END,
                        transcript = CASE WHEN excluded.transcript <> '' THEN excluded.transcript ELSE whatsapp_chat_messages.transcript END,
                        ticket_id = CASE WHEN excluded.ticket_id > 0 THEN excluded.ticket_id ELSE whatsapp_chat_messages.ticket_id END,
                        mentions_json = CASE WHEN excluded.mentions_json <> '[]' THEN excluded.mentions_json ELSE whatsapp_chat_messages.mentions_json END,
                        notify = MAX(whatsapp_chat_messages.notify, excluded.notify),
                        quoted_message_key = CASE WHEN excluded.quoted_message_key <> '' THEN excluded.quoted_message_key ELSE whatsapp_chat_messages.quoted_message_key END,
                        quoted_body = CASE WHEN excluded.quoted_body <> '' THEN excluded.quoted_body ELSE whatsapp_chat_messages.quoted_body END,
                        quoted_sender = CASE WHEN excluded.quoted_sender <> '' THEN excluded.quoted_sender ELSE whatsapp_chat_messages.quoted_sender END,
                        forwarded = MAX(whatsapp_chat_messages.forwarded, excluded.forwarded),
                        edited = MAX(whatsapp_chat_messages.edited, excluded.edited),
                        edit_timestamp = MAX(whatsapp_chat_messages.edit_timestamp, excluded.edit_timestamp),
                        deleted = MAX(whatsapp_chat_messages.deleted, excluded.deleted),
                        reactions_json = CASE WHEN excluded.reactions_json <> '{}' THEN excluded.reactions_json ELSE whatsapp_chat_messages.reactions_json END
                    """,
                    (
                        message_key, chat_id, int(from_me), sender, sender_phone, sender_id, body,
                        normalize_message(str(item.get("type", "chat")))[:40] or "chat",
                        timestamp, ack, media_path, media_mime, media_name, transcript, ticket_id,
                        mentions_json, notify, quoted_message_key, quoted_body, quoted_sender, forwarded,
                        edited, deleted, reactions_json, now, edit_timestamp,
                    ),
                )
                latest_visible = connection.execute(
                    """SELECT body, media_name, message_timestamp
                         FROM whatsapp_chat_messages
                        WHERE chat_id = ? AND deleted = 0
                        ORDER BY message_timestamp DESC, id DESC LIMIT 1""",
                    (chat_id,),
                ).fetchone()
                if latest_visible:
                    preview = latest_visible["body"] or latest_visible["media_name"] or "[Вложение]"
                    preview_timestamp = int(latest_visible["message_timestamp"] or 0)
                else:
                    preview, preview_timestamp = "", 0
                connection.execute(
                    """
                    INSERT INTO whatsapp_chats
                        (chat_id, name, last_message, last_timestamp, unread_count, updated_at)
                    VALUES (?, '', ?, ?, 0, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        last_message = excluded.last_message,
                        last_timestamp = excluded.last_timestamp,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, str(preview)[:160], preview_timestamp, now),
                )
                should_increment = False
                mention_increment = False
                if chat_id.endswith("@g.us"):
                    # Число справа у группы считает все входящие, как в WhatsApp.
                    # Отдельный счётчик упоминаний нужен только для колокольчика/
                    # системных уведомлений и растёт лишь при реальном @упоминании нас.
                    should_increment = (not from_me and not exists)
                    mention_increment = bool(should_increment and notify)
                elif not from_me and not exists:
                    # Every real personal WhatsApp message is unread until
                    # an employee opens the chat. Earlier builds counted only
                    # contacts manually added in Admin, so ordinary chats could
                    # arrive silently even though the message was saved.
                    should_increment = True
                if should_increment:
                    connection.execute(
                        "UPDATE whatsapp_chats SET unread_count = unread_count + 1 WHERE chat_id = ?",
                        (chat_id,),
                    )
                if mention_increment:
                    connection.execute(
                        "UPDATE whatsapp_chats SET mention_unread_count = mention_unread_count + 1 WHERE chat_id = ?",
                        (chat_id,),
                    )
            self._merge_whatsapp_lid_aliases(connection)

    def update_whatsapp_message_ack(
        self,
        chat_id: str,
        message_key: str,
        ack: int,
    ) -> bool:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        safe_ack = max(0, min(4, int(ack)))
        if not chat_id or not message_key:
            return False
        original = self.resolve_whatsapp_message(chat_id, message_key)
        if original: message_key = original["message_key"]
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE whatsapp_chat_messages
                SET ack = MAX(ack, ?)
                WHERE chat_id = ? AND message_key = ?
                """,
                (safe_ack, chat_id, message_key),
            )
            if cursor.rowcount == 0:
                # WhatsApp может прислать подтверждение под @c.us, а входящий
                # чат ранее сохранить под @lid. ID самого сообщения совпадает.
                cursor = connection.execute(
                    """
                    UPDATE whatsapp_chat_messages
                    SET ack = MAX(ack, ?)
                    WHERE message_key = ?
                    """,
                    (safe_ack, message_key),
                )
            return cursor.rowcount > 0

    def update_whatsapp_group_message_identities(
        self,
        chat_id: str,
        identities: list[dict[str, Any]],
    ) -> int:
        """Update sender metadata only for group messages that already exist.

        This is intentionally update-only: the connector may inspect older loaded
        WhatsApp models to recover an author name/number, but must never import
        historical messages that were not already saved by the system.
        """
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id.endswith("@g.us") or not identities:
            return 0
        generic = {"", "участник", "участник группы", "пользователь whatsapp"}
        updated = 0
        with self.connection() as connection:
            for item in identities[:100]:
                message_key = normalize_message(str(item.get("id", "")))[:160]
                if not message_key:
                    continue
                row = connection.execute(
                    "SELECT sender, sender_phone, sender_id FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                    (chat_id, message_key),
                ).fetchone()
                if not row:
                    continue
                old_sender = normalize_message(str(row["sender"] or ""))[:100]
                old_phone = normalize_message(str(row["sender_phone"] or ""))[:50]
                old_sender_id = normalize_message(str(row["sender_id"] or ""))[:120]
                new_sender = normalize_message(str(item.get("sender", "")))[:100]
                new_phone = normalize_message(str(item.get("sender_phone", "")))[:50]
                new_sender_id = normalize_message(str(item.get("sender_id", "")))[:120]

                # Never downgrade an already useful display name to a generic
                # placeholder or a raw number. Upgrade generic/number-only names
                # as soon as WhatsApp exposes a real pushname/contact name.
                old_folded = old_sender.casefold()
                new_folded = new_sender.casefold()
                old_is_generic = old_folded in generic or bool(re.fullmatch(r"[+\d\s().-]+", old_sender or ""))
                new_is_useful = bool(new_sender) and new_folded not in generic and not bool(re.fullmatch(r"[+\d\s().-]+", new_sender))
                sender_value = new_sender if (new_is_useful and old_is_generic) else old_sender
                phone_value = new_phone or old_phone
                sender_id_value = new_sender_id or old_sender_id
                if sender_value == old_sender and phone_value == old_phone and sender_id_value == old_sender_id:
                    continue
                connection.execute(
                    """
                    UPDATE whatsapp_chat_messages
                    SET sender = ?, sender_phone = ?, sender_id = ?
                    WHERE chat_id = ? AND message_key = ?
                    """,
                    (sender_value, phone_value, sender_id_value, chat_id, message_key),
                )
                updated += 1
        return updated

    def mark_whatsapp_chat_read(self, chat_id: str) -> None:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id:
            return
        with self.connection() as connection:
            connection.execute(
                "UPDATE whatsapp_chats SET unread_count = 0, mention_unread_count = 0 "
                "WHERE chat_id = ? AND (unread_count <> 0 OR mention_unread_count <> 0)",
                (chat_id,),
            )

    def list_saved_whatsapp_chats(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.chat_id, c.name, c.last_message, c.last_timestamp, c.unread_count,
                       COALESCE((
                           SELECT m.from_me
                           FROM whatsapp_chat_messages AS m
                           WHERE m.chat_id = c.chat_id AND m.deleted = 0
                           ORDER BY m.message_timestamp DESC, m.id DESC
                           LIMIT 1
                       ), 0) AS last_from_me
                FROM whatsapp_chats AS c
                WHERE c.chat_id NOT LIKE '%@g.us'
                ORDER BY last_timestamp DESC, updated_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [
            {
                "id": row["chat_id"],
                "name": row["name"] or str(row["chat_id"]).split("@")[0],
                "last_message": row["last_message"],
                "timestamp": int(row["last_timestamp"] or 0),
                "unread_count": int(row["unread_count"] or 0),
                "last_from_me": bool(row["last_from_me"]),
            }
            for row in rows
        ]

    @staticmethod
    def _reaction_summary(raw: object) -> list[dict[str, Any]]:
        try:
            values = json.loads(str(raw or "{}")) if not isinstance(raw, dict) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        counts: dict[str, int] = {}
        my_reaction = str(values.get("__me__", "") or "")
        for emoji in values.values():
            value = str(emoji or "")[:32]
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        return [
            {"emoji": emoji, "count": count, "me": emoji == my_reaction}
            for emoji, count in counts.items()
        ]

    def update_whatsapp_message_reaction(
        self, chat_id: str, message_key: str, sender_id: str, emoji: str, *, from_me: bool = False
    ) -> bool:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        sender_key = "__me__" if from_me else normalize_message(sender_id)[:160]
        reaction = str(emoji or "")[:32]
        if not chat_id or not message_key or not sender_key:
            return False
        with self.connection() as connection:
            row = connection.execute(
                "SELECT reactions_json FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                (chat_id, message_key),
            ).fetchone()
            if not row:
                return False
            try:
                values = json.loads(str(row["reactions_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                values = {}
            if not isinstance(values, dict):
                values = {}
            if reaction:
                values[sender_key] = reaction
            else:
                values.pop(sender_key, None)
            cursor = connection.execute(
                "UPDATE whatsapp_chat_messages SET reactions_json = ? WHERE chat_id = ? AND message_key = ?",
                (json.dumps(values, ensure_ascii=False, separators=(",", ":")), chat_id, message_key),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _chat_message_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            mentions = json.loads(str(row["mentions_json"] or "[]"))
            if not isinstance(mentions, list):
                mentions = []
        except (TypeError, ValueError, json.JSONDecodeError):
            mentions = []
        return {
            "id": row["message_key"],
            "history_row_id": int(row["id"] or 0),
            "from_me": bool(row["from_me"]),
            "sender": row["sender"],
            "sender_phone": row["sender_phone"],
            "sender_id": row["sender_id"],
            "body": row["body"],
            "type": row["message_type"],
            "timestamp": int(row["message_timestamp"] or 0),
            "ack": int(row["ack"] or 0),
            "media_path": row["media_path"],
            "media_mime": row["media_mime"],
            "media_name": row["media_name"],
            "transcript": row["transcript"],
            "ticket_id": int(row["ticket_id"] or 0),
            "mentions": mentions,
            "quoted_message_key": row["quoted_message_key"],
            "quoted_body": row["quoted_body"],
            "quoted_sender": row["quoted_sender"],
            "forwarded": bool(row["forwarded"]),
            "edited": bool(row["edited"]),
            "edit_timestamp": int(row["edit_timestamp"] or 0),
            "deleted": bool(row["deleted"]),
            "reactions": MessageStore._reaction_summary(row["reactions_json"] if "reactions_json" in row.keys() else "{}"),
        }

    def list_saved_whatsapp_messages_page(
        self, chat_id: str, limit: int = 50, cursor: str = ""
    ) -> dict[str, Any]:
        chat_id = normalize_message(chat_id)[:120]
        safe_limit = max(1, min(int(limit), 100))
        conditions = ["chat_id = ?"]
        params: list[Any] = [chat_id]
        if cursor:
            try:
                ts_text, row_text = cursor.split(":", 1)
                before_ts, before_row = int(ts_text), int(row_text)
            except (ValueError, TypeError):
                before_ts = before_row = 0
            if before_row > 0:
                conditions.append("(message_timestamp < ? OR (message_timestamp = ? AND id < ?))")
                params.extend([before_ts, before_ts, before_row])
        params.append(safe_limit + 1)
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT id, message_key, from_me, sender, sender_phone, sender_id, body, message_type, message_timestamp, ack,
                           media_path, media_mime, media_name, transcript, ticket_id, mentions_json,
                           quoted_message_key, quoted_body, quoted_sender, forwarded, edited, deleted, edit_timestamp, reactions_json
                    FROM whatsapp_chat_messages
                    WHERE {' AND '.join(conditions)}
                    ORDER BY message_timestamp DESC, id DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        has_more = len(rows) > safe_limit
        rows = rows[:safe_limit]
        next_cursor = ""
        if rows:
            oldest = rows[-1]
            next_cursor = f"{int(oldest['message_timestamp'] or 0)}:{int(oldest['id'])}"
        messages = [self._chat_message_row(row) for row in reversed(rows)]
        return {"messages": messages, "has_more": has_more, "cursor": next_cursor}

    def list_saved_whatsapp_messages(
        self,
        chat_id: str,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        return list(self.list_saved_whatsapp_messages_page(chat_id, limit).get("messages", []))

    def link_whatsapp_message_to_ticket(self, chat_id: str, message_key: str, ticket_id: int) -> bool:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        try:
            safe_ticket_id = max(0, int(ticket_id))
        except (TypeError, ValueError):
            safe_ticket_id = 0
        if not chat_id or not message_key or not safe_ticket_id:
            return False
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE whatsapp_chat_messages SET ticket_id = ? WHERE chat_id = ? AND message_key = ?",
                (safe_ticket_id, chat_id, message_key),
            )
            return cursor.rowcount > 0

    def link_recent_whatsapp_images_to_ticket(
        self,
        chat_id: str,
        ticket_id: int,
        before_timestamp: int = 0,
        lookback_seconds: int = 600,
        limit: int = 12,
    ) -> list[str]:
        """Attach only pre-ticket images that belong to this ticket context.

        Earlier builds attached every recent unassigned image from the private chat.
        That could pull an unrelated screenshot from a previous conversation into a
        newly created ticket. Now the WhatsApp message must be present
        in ticket_context_history for the ticket that has just been committed.
        Therefore an image is auto-attached only when it was received while the
        user was already filling the current request/category.
        """
        clean_chat_id = normalize_message(chat_id)[:120]
        try:
            safe_ticket_id = max(0, int(ticket_id))
        except (TypeError, ValueError):
            safe_ticket_id = 0
        if not clean_chat_id or not safe_ticket_id or clean_chat_id.endswith("@g.us"):
            return []
        try:
            safe_before = max(0, int(before_timestamp or 0))
        except (TypeError, ValueError):
            safe_before = 0
        try:
            safe_lookback = max(60, min(3600, int(lookback_seconds or 600)))
        except (TypeError, ValueError):
            safe_lookback = 600
        try:
            safe_limit = max(1, min(24, int(limit or 12)))
        except (TypeError, ValueError):
            safe_limit = 12

        with self.connection() as connection:
            canonical = self._canonical_whatsapp_chat_id(connection, clean_chat_id)
            # Contextual history is created by queue_contextual_tickets while the
            # user is filling the chosen category. commit_ticket() assigns the
            # current ticket_id to that session before this method is called.
            # Joining through that history prevents unrelated recent screenshots
            # from being donated to a new ticket merely because they are recent.
            params: list[Any] = [canonical, safe_ticket_id]
            time_clause = ""
            if safe_before > 0:
                cutoff = max(0, safe_before - safe_lookback)
                time_clause = "AND w.message_timestamp BETWEEN ? AND ?"
                params.extend([cutoff, safe_before])
            else:
                cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=safe_lookback)).isoformat()
                time_clause = "AND w.created_at >= ?"
                params.append(cutoff_iso)
            params.append(safe_limit)
            try:
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT w.id, w.message_key, w.message_timestamp
                    FROM whatsapp_chat_messages AS w
                    INNER JOIN ticket_context_history AS h
                      ON h.message_key = w.message_key
                     AND h.ticket_id = ?
                     AND h.event_type = 'message_received'
                    WHERE w.chat_id = ?
                      AND w.from_me = 0
                      AND w.ticket_id = 0
                      AND w.deleted = 0
                      AND w.media_path <> ''
                      AND (w.media_mime LIKE 'image/%' OR w.message_type = 'image')
                      {time_clause}
                    ORDER BY w.message_timestamp DESC, w.id DESC
                    LIMIT ?
                    """,
                    [safe_ticket_id, canonical, *params[2:]],
                ).fetchall()
            except sqlite3.OperationalError as exc:
                # On an incomplete/legacy database it is safer to attach nothing
                # than to fall back to the old broad recent-image behaviour.
                if "ticket_context_history" in str(exc).casefold():
                    return []
                raise
            if not rows:
                return []
            row_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in row_ids)
            connection.execute(
                f"UPDATE whatsapp_chat_messages SET ticket_id = ? "
                f"WHERE ticket_id = 0 AND id IN ({placeholders})",
                [safe_ticket_id, *row_ids],
            )
            return [str(row["message_key"]) for row in reversed(rows)]

    def list_ticket_whatsapp_media(
        self, ticket_id: int, chat_id: str = "", external_id: str = "", limit: int = 24
    ) -> list[dict[str, Any]]:
        try:
            safe_ticket_id = max(0, int(ticket_id))
        except (TypeError, ValueError):
            safe_ticket_id = 0
        chat_id = normalize_message(chat_id)[:120]
        external_id = normalize_message(external_id)[:160]
        if not safe_ticket_id and not external_id:
            return []
        clauses = []
        params: list[Any] = []
        if safe_ticket_id:
            clauses.append("ticket_id = ?")
            params.append(safe_ticket_id)
        if chat_id and external_id:
            clauses.append("(chat_id = ? AND message_key = ?)")
            params.extend([chat_id, external_id])
        where = " OR ".join(clauses) or "0"
        params.append(max(1, min(100, int(limit))))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT message_key, chat_id, from_me, sender, body, message_type, message_timestamp,
                       media_path, media_mime, media_name, transcript, ticket_id, deleted
                FROM whatsapp_chat_messages
                WHERE ({where}) AND media_path <> '' AND deleted = 0
                ORDER BY message_timestamp ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_whatsapp_message(self, chat_id: str, message_key: str):
        return queue_quote_lookup.resolve(self, chat_id, message_key)

    # Reply-queue calculation removed; only real chat activity remains.
    def chat_activity(self):
        """One indexed lookup per chat, including groups. No per-message SQL."""
        with self.connection() as connection:
            rows = connection.execute("""
                SELECT m.*
                FROM whatsapp_chats c JOIN whatsapp_chat_messages m ON m.id=(SELECT id FROM whatsapp_chat_messages
                         WHERE chat_id=c.chat_id AND deleted=0 ORDER BY message_timestamp DESC, id DESC LIMIT 1)
            """).fetchall()
        return {row["chat_id"]: {
            "last_sender": row["sender"], "last_sender_id": row["sender_id"],
            "last_from_me": bool(row["from_me"]), "last_message": row["body"] or queue_message_identity.media_label(dict(row)),
            "last_ack": int(row["ack"] or 0),
            "timestamp": row["message_timestamp"],
        } for row in rows}

    def get_whatsapp_message(self, chat_id: str, message_key: str) -> dict[str, Any] | None:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        if not chat_id or not message_key:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                (chat_id, message_key),
            ).fetchone()
        return dict(row) if row else None
