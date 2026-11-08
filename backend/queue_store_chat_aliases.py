"""Псевдонимы @lid личных чатов WhatsApp и объединение их строк в чат @c.us.

Подмешивается в ticketing.TicketStore, откуда берёт connection() и методы других хранилищ.
"""
from __future__ import annotations

import re
import sqlite3

import queue_message_identity
from queue_core import normalize_message, utc_now


class ChatAliasStore:
    def _canonical_whatsapp_chat_id(self, connection: sqlite3.Connection, chat_id: str) -> str:
        clean = normalize_message(chat_id)[:120]
        if not clean or clean.endswith("@g.us") or clean.endswith("@c.us"):
            return clean
        row = connection.execute(
            "SELECT canonical_id FROM whatsapp_chat_aliases WHERE alias_id=? LIMIT 1",
            (clean,),
        ).fetchone()
        canonical = normalize_message(str(row["canonical_id"] if row else ""))[:120]
        return canonical or clean

    def canonical_whatsapp_chat_id(self, chat_id: str) -> str:
        """Return the persistent canonical private-chat id used by the UI."""
        clean = normalize_message(chat_id)[:120]
        if not clean or clean.endswith("@g.us") or clean.endswith("@c.us"):
            return clean  # only @lid aliases need the database
        with self.connection() as connection:
            return self._canonical_whatsapp_chat_id(connection, chat_id)

    def remember_whatsapp_chat_alias(self, alias_id: str, canonical_id: str) -> bool:
        alias = normalize_message(alias_id)[:120]
        canonical = normalize_message(canonical_id)[:120]
        if not alias.endswith("@lid") or not canonical.endswith("@c.us") or alias == canonical:
            return False
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alias_id) DO UPDATE SET
                    canonical_id=excluded.canonical_id,
                    updated_at=excluded.updated_at
                """,
                (alias, canonical, utc_now()),
            )
            self._merge_whatsapp_lid_aliases(connection)
        return True

    def _merge_whatsapp_lid_aliases(self, connection: sqlite3.Connection) -> None:
        """Persist and merge private @lid aliases into phone based @c.us chats.

        The alias mapping must survive the first merge. Otherwise a later
        WhatsApp sync can recreate the same contact as a second chat after the
        ticket row has already been canonicalised.
        """
        now = utc_now()
        sources = connection.execute(
            """
            SELECT chat_id, phone FROM tickets
             WHERE source='whatsapp' AND chat_id LIKE '%@lid' AND phone<>''
            UNION ALL
            SELECT chat_id, phone FROM whatsapp_contacts
             WHERE chat_id LIKE '%@lid' AND phone<>''
            UNION ALL
            SELECT chat_id, sender_phone AS phone FROM whatsapp_chat_messages
             WHERE chat_id LIKE '%@lid' AND from_me=0 AND sender_phone<>''
            """
        ).fetchall()
        for row in sources:
            alias = normalize_message(str(row["chat_id"] or ""))[:120]
            digits = re.sub(r"\D", "", str(row["phone"] or ""))[:24]
            if alias.endswith("@lid") and digits:
                connection.execute(
                    """
                    INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(alias_id) DO UPDATE SET
                        canonical_id=excluded.canonical_id,
                        updated_at=excluded.updated_at
                    """,
                    (alias, f"{digits}@c.us", now),
                )

        # Дополнительное восстановление алиасов из уже сохранённой истории.
        # В новых версиях WhatsApp один личный чат может некоторое время
        # приходить как @lid, а затем как @c.us. Для входящего личного сообщения
        # sender_id часто уже содержит настоящий @c.us, даже когда chat_id ещё @lid.
        sender_rows = connection.execute(
            """
            SELECT chat_id, sender_id
              FROM whatsapp_chat_messages
             WHERE chat_id LIKE '%@lid'
               AND from_me=0
               AND sender_id LIKE '%@c.us'
             ORDER BY id DESC
             LIMIT 2000
            """
        ).fetchall()
        for row in sender_rows:
            alias = normalize_message(str(row["chat_id"] or ""))[:120]
            canonical = normalize_message(str(row["sender_id"] or ""))[:120]
            if alias.endswith("@lid") and canonical.endswith("@c.us") and alias != canonical:
                connection.execute(
                    """
                    INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(alias_id) DO UPDATE SET
                        canonical_id=excluded.canonical_id,
                        updated_at=excluded.updated_at
                    """,
                    (alias, canonical, now),
                )

        # Если sender_phone/sender_id в старой истории не заполнялись, одинаковый
        # provider message id остаётся безопасным признаком того, что @lid и @c.us
        # являются двумя представлениями одного чата. Message stanza у WhatsApp
        # глобально уникален, поэтому по имени/тексту сообщения здесь не гадаем.
        recent_rows = connection.execute(
            """
            SELECT chat_id, message_key
              FROM whatsapp_chat_messages
             WHERE chat_id LIKE '%@lid' OR chat_id LIKE '%@c.us'
             ORDER BY id DESC
             LIMIT 4000
            """
        ).fetchall()
        stanza_chats: dict[str, set[str]] = {}
        for row in recent_rows:
            chat = normalize_message(str(row["chat_id"] or ""))[:120]
            key = normalize_message(str(row["message_key"] or ""))[:160]
            if not chat or not key:
                continue
            _, stanza = queue_message_identity.parts(key)
            if len(stanza) < 8:
                continue
            stanza_chats.setdefault(stanza, set()).add(chat)
        alias_candidates: dict[str, set[str]] = {}
        for chats in stanza_chats.values():
            lids = {chat for chat in chats if chat.endswith("@lid")}
            phones = {chat for chat in chats if chat.endswith("@c.us")}
            if len(phones) != 1:
                continue
            canonical = next(iter(phones))
            for alias in lids:
                alias_candidates.setdefault(alias, set()).add(canonical)
        for alias, candidates in alias_candidates.items():
            # Не выбираем наугад, если один @lid каким-то образом пересёкся
            # сразу с несколькими телефонными чатами.
            if len(candidates) != 1:
                continue
            canonical = next(iter(candidates))
            connection.execute(
                """
                INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alias_id) DO UPDATE SET
                    canonical_id=excluded.canonical_id,
                    updated_at=excluded.updated_at
                """,
                (alias, canonical, now),
            )

        # Retain every mapping, but reconcile only aliases that still own data.
        # Re-running thousands of already completed merges held the write lock
        # on every message/chat sync, delaying unrelated replies and UI requests.
        aliases = connection.execute(
            """SELECT a.alias_id, a.canonical_id FROM whatsapp_chat_aliases AS a
               WHERE a.alias_id<>a.canonical_id AND (
                   EXISTS(SELECT 1 FROM whatsapp_chat_messages WHERE chat_id=a.alias_id)
                   OR EXISTS(SELECT 1 FROM whatsapp_chats WHERE chat_id=a.alias_id)
                   OR EXISTS(SELECT 1 FROM whatsapp_contacts WHERE chat_id=a.alias_id)
                   OR EXISTS(SELECT 1 FROM conversation_contexts WHERE contact_key=a.alias_id)
                   OR EXISTS(SELECT 1 FROM whatsapp_call_permissions WHERE contact_key=a.alias_id)
                   OR EXISTS(SELECT 1 FROM tickets WHERE chat_id=a.alias_id)
                   OR EXISTS(SELECT 1 FROM outbound_messages WHERE chat_id=a.alias_id)
               )"""
        ).fetchall()
        for row in aliases:
            alias = normalize_message(str(row["alias_id"] or ""))[:120]
            canonical = normalize_message(str(row["canonical_id"] or ""))[:120]
            if not alias.endswith("@lid") or not canonical.endswith("@c.us"):
                continue

            # Move history without rebuilding rows, so media, quotes, sender ids,
            # reactions and transcripts are never lost. If both aliases already
            # contain the same provider message, merge only the richer fields.
            message_rows = connection.execute(
                "SELECT * FROM whatsapp_chat_messages WHERE chat_id=? ORDER BY id",
                (alias,),
            ).fetchall()
            for message in message_rows:
                message_key = str(message["message_key"] or "")
                _, stanza = queue_message_identity.parts(message_key)
                candidates = connection.execute(
                    "SELECT * FROM whatsapp_chat_messages WHERE chat_id=? AND (message_key=? OR substr(message_key, -?)=?) ORDER BY id",
                    (canonical, message_key, max(1, len(stanza)), stanza),
                ).fetchall()
                matches = [candidate for candidate in candidates if queue_message_identity.same(candidate["message_key"], message_key)]
                existing = matches[0] if len(matches) == 1 else None
                if not existing:
                    connection.execute(
                        "UPDATE whatsapp_chat_messages SET chat_id=? WHERE id=?",
                        (canonical, message["id"]),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE whatsapp_chat_messages SET
                        ack=MAX(ack, ?),
                        sender=CASE WHEN sender='' THEN ? ELSE sender END,
                        sender_phone=CASE WHEN sender_phone='' THEN ? ELSE sender_phone END,
                        sender_id=CASE WHEN sender_id='' THEN ? ELSE sender_id END,
                        body=CASE WHEN body='' THEN ? ELSE body END,
                        message_type=CASE WHEN message_type IN ('','chat') AND ? NOT IN ('','chat') THEN ? ELSE message_type END,
                        message_timestamp=MAX(message_timestamp, ?),
                        media_path=CASE WHEN media_path='' THEN ? ELSE media_path END,
                        media_mime=CASE WHEN media_mime='' THEN ? ELSE media_mime END,
                        media_name=CASE WHEN media_name='' THEN ? ELSE media_name END,
                        transcript=CASE WHEN transcript='' THEN ? ELSE transcript END,
                        ticket_id=CASE WHEN ticket_id=0 THEN ? ELSE ticket_id END,
                        mentions_json=CASE WHEN mentions_json IN ('','[]') THEN ? ELSE mentions_json END,
                        quoted_message_key=CASE WHEN quoted_message_key='' THEN ? ELSE quoted_message_key END,
                        quoted_body=CASE WHEN quoted_body='' THEN ? ELSE quoted_body END,
                        quoted_sender=CASE WHEN quoted_sender='' THEN ? ELSE quoted_sender END,
                        forwarded=MAX(forwarded, ?), edited=MAX(edited, ?), deleted=MAX(deleted, ?),
                        reactions_json=CASE WHEN reactions_json IN ('','{}') THEN ? ELSE reactions_json END,
                        edit_timestamp=MAX(edit_timestamp, ?)
                    WHERE id=?
                    """,
                    (
                        int(message["ack"] or 0), str(message["sender"] or ""),
                        str(message["sender_phone"] or ""), str(message["sender_id"] or ""),
                        str(message["body"] or ""), str(message["message_type"] or ""),
                        str(message["message_type"] or ""), int(message["message_timestamp"] or 0),
                        str(message["media_path"] or ""), str(message["media_mime"] or ""),
                        str(message["media_name"] or ""), str(message["transcript"] or ""),
                        int(message["ticket_id"] or 0), str(message["mentions_json"] or "[]"),
                        str(message["quoted_message_key"] or ""), str(message["quoted_body"] or ""),
                        str(message["quoted_sender"] or ""), int(message["forwarded"] or 0),
                        int(message["edited"] or 0), int(message["deleted"] or 0),
                        str(message["reactions_json"] or "{}"), int(message["edit_timestamp"] or 0),
                        existing["id"],
                    ),
                )
                connection.execute("DELETE FROM whatsapp_chat_messages WHERE id=?", (message["id"],))

            alias_chat = connection.execute("SELECT * FROM whatsapp_chats WHERE chat_id=?", (alias,)).fetchone()
            canonical_chat = connection.execute("SELECT * FROM whatsapp_chats WHERE chat_id=?", (canonical,)).fetchone()
            if alias_chat and canonical_chat:
                alias_ts = int(alias_chat["last_timestamp"] or 0)
                canonical_ts = int(canonical_chat["last_timestamp"] or 0)
                best = alias_chat if alias_ts >= canonical_ts else canonical_chat
                alias_name = str(alias_chat["name"] or "").strip()
                canonical_name = str(canonical_chat["name"] or "").strip()
                canonical_digits = canonical.split("@", 1)[0]
                name = alias_name if alias_name and (not canonical_name or canonical_name == canonical_digits) else canonical_name
                connection.execute(
                    """
                    UPDATE whatsapp_chats SET name=?, last_message=?, last_timestamp=?, unread_count=?, updated_at=?
                    WHERE chat_id=?
                    """,
                    (name, str(best["last_message"] or ""), max(alias_ts, canonical_ts),
                     max(int(alias_chat["unread_count"] or 0), int(canonical_chat["unread_count"] or 0)),
                     str(best["updated_at"] or now), canonical),
                )
                connection.execute("DELETE FROM whatsapp_chats WHERE chat_id=?", (alias,))
            elif alias_chat:
                connection.execute("UPDATE whatsapp_chats SET chat_id=? WHERE chat_id=?", (canonical, alias))

            alias_contact = connection.execute("SELECT * FROM whatsapp_contacts WHERE chat_id=?", (alias,)).fetchone()
            canonical_contact = connection.execute("SELECT * FROM whatsapp_contacts WHERE chat_id=?", (canonical,)).fetchone()
            if alias_contact and not canonical_contact:
                connection.execute("UPDATE whatsapp_contacts SET chat_id=? WHERE chat_id=?", (canonical, alias))
            elif alias_contact:
                if not str(canonical_contact["name"] or "").strip() and str(alias_contact["name"] or "").strip():
                    connection.execute("UPDATE whatsapp_contacts SET name=? WHERE chat_id=?", (alias_contact["name"], canonical))
                connection.execute("DELETE FROM whatsapp_contacts WHERE chat_id=?", (alias,))

            alias_context = connection.execute("SELECT * FROM conversation_contexts WHERE contact_key=?", (alias,)).fetchone()
            canonical_context = connection.execute("SELECT * FROM conversation_contexts WHERE contact_key=?", (canonical,)).fetchone()
            if alias_context and not canonical_context:
                connection.execute("UPDATE conversation_contexts SET contact_key=? WHERE contact_key=?", (canonical, alias))
            elif alias_context:
                if str(alias_context["updated_at"] or "") > str(canonical_context["updated_at"] or ""):
                    connection.execute(
                        "UPDATE conversation_contexts SET pending_category=?, active_ticket_id=?, force_new=?, draft_json=?, updated_at=? WHERE contact_key=?",
                        (alias_context["pending_category"], alias_context["active_ticket_id"], alias_context["force_new"],
                         alias_context["draft_json"], alias_context["updated_at"], canonical),
                    )
                connection.execute("DELETE FROM conversation_contexts WHERE contact_key=?", (alias,))

            alias_permission = connection.execute("SELECT expires_at FROM whatsapp_call_permissions WHERE contact_key=?", (alias,)).fetchone()
            canonical_permission = connection.execute("SELECT expires_at FROM whatsapp_call_permissions WHERE contact_key=?", (canonical,)).fetchone()
            if alias_permission and not canonical_permission:
                connection.execute("UPDATE whatsapp_call_permissions SET contact_key=? WHERE contact_key=?", (canonical, alias))
            elif alias_permission:
                if str(alias_permission["expires_at"] or "") > str(canonical_permission["expires_at"] or ""):
                    connection.execute("UPDATE whatsapp_call_permissions SET expires_at=? WHERE contact_key=?", (alias_permission["expires_at"], canonical))
                connection.execute("DELETE FROM whatsapp_call_permissions WHERE contact_key=?", (alias,))

            connection.execute("UPDATE tickets SET chat_id=? WHERE chat_id=?", (canonical, alias))
            connection.execute("UPDATE outbound_messages SET chat_id=? WHERE chat_id=?", (canonical, alias))
