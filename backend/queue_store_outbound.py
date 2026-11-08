"""Очередь исходящих сообщений и действия WhatsApp для коннектора.

Подмешивается в ticketing.TicketStore, откуда берёт connection() и методы других хранилищ.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from queue_core import normalize_message, utc_now


class OutboundStore:
    def queue_outbound_message(self, ticket_id: int, body: str, actor: str) -> int:
        body = normalize_message(body)[:32000]
        if not body:
            return 0
        now = utc_now()
        with self.connection() as connection:
            ticket = connection.execute(
                "SELECT source, chat_id, phone FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if not ticket or ticket["source"] != "whatsapp":
                return 0
            if not ticket["chat_id"] and not ticket["phone"]:
                return 0
            cursor = connection.execute(
                """
                INSERT INTO outbound_messages
                    (ticket_id, chat_id, phone, body, actor, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    ticket["chat_id"],
                    ticket["phone"],
                    body,
                    actor,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, "Ответ поставлен в очередь отправки", actor, now),
            )
            message_id = int(cursor.lastrowid)
            self._audit(connection, "message", actor, "Сообщение поставлено в очередь", "ticket", ticket_id, f"Очередь #{message_id}", created_at=now)
            return message_id

    def queue_direct_message(
        self, chat_id: str, body: str, actor: str, mentions: list[str] | None = None,
        reply_to_key: str = "", media_path: str = "", media_mime: str = "", media_name: str = "",
        request_id: str = "",
    ) -> int:
        body = normalize_message(body)[:32000]
        chat_id = normalize_message(chat_id)[:120]
        reply_to_key = normalize_message(reply_to_key)[:160]
        media_path = str(media_path or "")[:500]
        media_mime = normalize_message(media_mime)[:120]
        media_name = normalize_message(media_name)[:180]
        if not chat_id or (not body and not media_path):
            return 0
        safe_mentions = []
        for value in mentions or []:
            mention = normalize_message(str(value))[:120]
            if mention and mention not in safe_mentions:
                safe_mentions.append(mention)
        request_id = str(request_id or "")[:120]
        now = utc_now()
        with self.connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            if request_id:
                existing = connection.execute("SELECT * FROM outbound_requests WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    return int(existing["message_id"]) if existing["chat_id"] == chat_id and existing["actor"] == actor else 0
            cursor = connection.execute(
                """
                INSERT INTO outbound_messages
                    (ticket_id, chat_id, phone, body, actor, created_at, updated_at, mentions_json,
                     reply_to_key, media_path, media_mime, media_name)
                VALUES (0, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, body, actor, now, now, json.dumps(safe_mentions, ensure_ascii=False),
                 reply_to_key, media_path, media_mime, media_name),
            )
            message_id = int(cursor.lastrowid)
            if request_id:
                connection.execute("INSERT INTO outbound_requests VALUES (?, ?, ?, ?, ?)", (request_id, chat_id, actor, message_id, now))
            detail = (body[:180] if body else media_name or "Вложение")
            self._audit(connection, "message", actor, "Сообщение поставлено в очередь", "chat", chat_id, f"#{message_id}: {detail}", created_at=now)
            return message_id

    def queue_whatsapp_action(
        self, action_type: str, chat_id: str, message_key: str, body: str = "", actor: str = ""
    ) -> int:
        if action_type not in {"edit", "delete", "forward", "react"}:
            return 0
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        body = normalize_message(body)[:32000]
        actor = normalize_message(actor)[:100] or "Сотрудник"
        if not chat_id or not message_key or (action_type in {"edit", "forward"} and not body):
            return 0
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO whatsapp_actions(action_type, chat_id, message_key, body, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (action_type, chat_id, message_key, body, actor, now),
            )
            action_id = int(cursor.lastrowid)
            label = {"edit": "Редактирование сообщения поставлено в очередь", "delete": "Удаление сообщения поставлено в очередь", "forward": "Пересылка сообщения поставлена в очередь", "react": "Реакция на сообщение поставлена в очередь"}[action_type]
            self._audit(connection, "message_action", actor, label, "chat", chat_id, f"message={message_key}; action=#{action_id}", created_at=now)
            return action_id

    def claim_whatsapp_action(self) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        stale_cutoff = (now_dt - timedelta(seconds=60)).isoformat()
        cleanup_cutoff = (now_dt - timedelta(days=1)).isoformat()
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM whatsapp_actions WHERE status IN ('sent','failed') AND created_at < ?",
                (cleanup_cutoff,),
            )
            connection.execute(
                "UPDATE whatsapp_actions SET status='failed', claimed_at='', error='Нет подтверждения действия. Проверьте переписку перед повтором.' WHERE status='processing' AND claimed_at<>'' AND claimed_at < ?",
                (stale_cutoff,),
            )
            row = connection.execute(
                "SELECT * FROM whatsapp_actions WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                "UPDATE whatsapp_actions SET status = 'processing', claimed_at = ? WHERE id = ? AND status = 'pending'",
                (now, row["id"]),
            )
            if not cursor.rowcount:
                return None
        return dict(row)

    def whatsapp_action_status(self, action_id: int) -> dict[str, Any] | None:
        if action_id <= 0:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, action_type, chat_id, message_key, status, error, created_at, claimed_at FROM whatsapp_actions WHERE id = ?",
                (action_id,),
            ).fetchone()
        return dict(row) if row else None

    def complete_whatsapp_action(self, action_id: int, sent: bool, error: str = "") -> dict[str, Any] | None:
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM whatsapp_actions WHERE id = ?", (action_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            if data["status"] == "sent":
                return data
            safe_error = normalize_message(error)[:500]
            connection.execute(
                "UPDATE whatsapp_actions SET status=?, error=?, claimed_at='' WHERE id=?",
                ("sent" if sent else "failed", "" if sent else safe_error, action_id),
            )
            action_type = str(data.get("action_type", ""))
            actor = str(data.get("actor", "") or "Сотрудник")
            if sent:
                if action_type == "edit":
                    connection.execute(
                        "UPDATE whatsapp_chat_messages SET body = ?, edited = 1, edit_timestamp=? WHERE chat_id = ? AND message_key = ?",
                        (data["body"], int(datetime.now(timezone.utc).timestamp()*1000), data["chat_id"], data["message_key"]),
                    )
                elif action_type == "delete":
                    target = connection.execute(
                        """SELECT body, media_name, message_type, message_timestamp
                             FROM whatsapp_chat_messages
                            WHERE chat_id = ? AND message_key = ?""",
                        (data["chat_id"], data["message_key"]),
                    ).fetchone()
                    connection.execute(
                        "UPDATE whatsapp_chat_messages SET body = '', media_path = '', media_mime = '', media_name = '', deleted = 1 WHERE chat_id = ? AND message_key = ?",
                        (data["chat_id"], data["message_key"]),
                    )
                elif action_type == "react":
                    reaction_row = connection.execute(
                        "SELECT reactions_json FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                        (data["chat_id"], data["message_key"]),
                    ).fetchone()
                    if reaction_row:
                        try:
                            reaction_values = json.loads(str(reaction_row["reactions_json"] or "{}"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            reaction_values = {}
                        if not isinstance(reaction_values, dict):
                            reaction_values = {}
                        reaction_emoji = str(data.get("body", "") or "")[:32]
                        if reaction_emoji:
                            reaction_values["__me__"] = reaction_emoji
                        else:
                            reaction_values.pop("__me__", None)
                        connection.execute(
                            "UPDATE whatsapp_chat_messages SET reactions_json = ? WHERE chat_id = ? AND message_key = ?",
                            (json.dumps(reaction_values, ensure_ascii=False, separators=(",", ":")), data["chat_id"], data["message_key"]),
                        )
                latest = connection.execute(
                    """SELECT body, media_name, message_timestamp
                       FROM whatsapp_chat_messages
                       WHERE chat_id = ? AND deleted = 0
                       ORDER BY message_timestamp DESC, id DESC LIMIT 1""",
                    (data["chat_id"],),
                ).fetchone()
                if latest:
                    preview = latest["body"] or latest["media_name"] or "[Вложение]"
                    preview_timestamp = int(latest["message_timestamp"] or 0)
                else:
                    preview, preview_timestamp = "", 0
                connection.execute(
                    "UPDATE whatsapp_chats SET last_message = ?, last_timestamp = ?, updated_at = ? WHERE chat_id = ?",
                    (str(preview)[:160], preview_timestamp, now, data["chat_id"]),
                )
                label = {"edit": "Сообщение изменено в WhatsApp", "delete": "Сообщение удалено в WhatsApp", "forward": "Сообщение переслано в WhatsApp", "react": "Реакция отправлена в WhatsApp"}.get(action_type, "Действие с сообщением выполнено")
                self._audit(connection, "message_action", actor, label, "chat", data["chat_id"], f"message={data['message_key']}", created_at=now)
            else:
                safe_error = safe_error or "WhatsApp не выполнил действие"
                connection.execute("UPDATE whatsapp_actions SET error=? WHERE id=?", (safe_error, action_id))
                label = {"edit": "Не удалось изменить сообщение", "delete": "Не удалось удалить сообщение", "forward": "Не удалось переслать сообщение", "react": "Не удалось отправить реакцию"}.get(action_type, "Ошибка действия с сообщением")
                self._audit(connection, "message_action", actor, label, "chat", data["chat_id"], safe_error, level="error", created_at=now)
                data["error"] = safe_error
            data["status"] = "sent" if sent else "failed"
            if sent:
                data["error"] = ""
            return data

    def claim_outbound_message(self, created_after: str = "") -> dict[str, Any] | None:
        now = utc_now()
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).replace(microsecond=0).isoformat()
        with self.connection() as connection:
            connection.execute(
                "UPDATE outbound_messages SET status='uncertain', updated_at=?, "
                "last_error='Связь прервалась во время отправки. Проверьте переписку; автоповтор остановлен.' "
                "WHERE status='sending' AND claimed_at<>'' AND claimed_at < ?",
                (now, stale_cutoff),
            )
            # Если коннектор взял сообщение и умер до результата, запись не теряется:
            # она возвращается в очередь, а при исчерпанных попытках становится
            # failed, чтобы попасть в список проблемных и в ручной повтор.
            connection.execute(
                """
                UPDATE outbound_messages
                SET status=CASE WHEN attempt_count >= max_attempts THEN 'failed' ELSE 'pending' END,
                    failed_at=CASE WHEN attempt_count >= max_attempts THEN ? ELSE failed_at END,
                    claimed_at='', next_attempt_at='',
                    last_error=CASE
                        WHEN attempt_count >= max_attempts THEN 'Попытки отправки исчерпаны: коннектор не вернул результат'
                        WHEN last_error='' THEN 'Повтор после зависшей отправки'
                        ELSE last_error END,
                    updated_at=?
                WHERE status='processing' AND claimed_at <> '' AND claimed_at < ?
                """,
                (now, now, stale_cutoff),
            )
            # Записи, которые раньше застряли в pending без оставшихся попыток.
            connection.execute(
                """
                UPDATE outbound_messages
                SET status='failed', failed_at=?, updated_at=?,
                    last_error='Попытки отправки исчерпаны: коннектор не вернул результат'
                WHERE status='pending' AND attempt_count >= max_attempts
                """,
                (now, now),
            )
            # Очередь постоянная: после перезапуска коннектора старые pending
            # сообщения не теряются и продолжают отправляться.
            row = connection.execute(
                """
                SELECT * FROM outbound_messages
                WHERE status='pending'
                  AND attempt_count < max_attempts
                  AND (next_attempt_at='' OR next_attempt_at <= ?)
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                """
                UPDATE outbound_messages
                SET status='processing', claimed_at=?, attempt_count=attempt_count+1,
                    updated_at=?
                WHERE id=? AND status='pending'
                """,
                (now, now, row["id"]),
            )
            if not cursor.rowcount:
                return None
            row = connection.execute(
                "SELECT * FROM outbound_messages WHERE id=?", (row["id"],)
            ).fetchone()
        return dict(row) if row else None

    def touch_outbound_message(self, message_id: int) -> bool:
        with self.connection() as connection:
            return bool(connection.execute(
                "UPDATE outbound_messages SET claimed_at=? WHERE id=? AND status IN ('processing','sending')",
                (utc_now(), message_id),
            ).rowcount)

    def begin_outbound_send(self, message_id: int) -> bool:
        with self.connection() as connection:
            return bool(connection.execute(
                "UPDATE outbound_messages SET status='sending', claimed_at=?, updated_at=?, dispatch_started_at=? "
                "WHERE id=? AND status='processing'",
                (utc_now(), utc_now(), utc_now(), message_id),
            ).rowcount)

    def mark_outbound_uncertain(self, message_id: int, error: str = '') -> dict[str, Any] | None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE outbound_messages SET status='uncertain', last_error=?, updated_at=?, "
                "claimed_at='', next_attempt_at='' WHERE id=? AND status IN ('processing','sending','uncertain')",
                (normalize_message(error)[:500] or 'Нет подтверждения WhatsApp. Проверьте переписку.', utc_now(), message_id),
            )
            row = connection.execute('SELECT * FROM outbound_messages WHERE id=?', (message_id,)).fetchone()
            return dict(row) if row else None

    def complete_outbound_message(
        self,
        message_id: int,
        sent: bool,
        error: str = "",
        provider_id: str = "",
    ) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM outbound_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "sent":
                return dict(row)
            if not sent and row["status"] in {"sending", "uncertain"}:
                connection.execute(
                    "UPDATE outbound_messages SET status='uncertain', claimed_at='', next_attempt_at='', "
                    "last_error=?, updated_at=? WHERE id=?",
                    ('Отправка могла состояться. Автоповтор остановлен. ' + normalize_message(error)[:350], now, message_id),
                )
                return dict(connection.execute('SELECT * FROM outbound_messages WHERE id=?', (message_id,)).fetchone())
            media_path = str(row["media_path"] or "")
            attempts = int(row["attempt_count"] or 0)
            max_attempts = max(1, int(row["max_attempts"] or 4))
            if sent:
                connection.execute(
                    """
                    UPDATE outbound_messages
                    SET status='sent', claimed_at='', next_attempt_at='', last_error='',
                        provider_id=?, sent_at=?, failed_at='', updated_at=?, media_path=''
                    WHERE id=?
                    """,
                    (normalize_message(provider_id)[:180], now, now, message_id),
                )
                # SLA считается до первой реальной реакции сотрудника. Успешно
                # доставленный ответ по заявке останавливает таймер даже если
                # сотрудник ещё не успел вручную изменить статус заявки.
                if int(row["ticket_id"] or 0) > 0:
                    connection.execute(
                        """
                        UPDATE tickets
                           SET first_response_at = CASE WHEN first_response_at='' THEN ? ELSE first_response_at END,
                               updated_at = ?
                         WHERE id = ?
                        """,
                        (now, now, int(row["ticket_id"])),
                    )
                if media_path:
                    try:
                        Path(media_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                action = "Ответ отправлен пользователю"
            else:
                safe_error = normalize_message(error)[:500]
                if attempts < max_attempts:
                    delays = (5, 15, 45, 120)
                    delay = delays[min(max(0, attempts - 1), len(delays) - 1)]
                    next_at = (now_dt + timedelta(seconds=delay)).isoformat()
                    connection.execute(
                        """
                        UPDATE outbound_messages
                        SET status='pending', claimed_at='', next_attempt_at=?,
                            last_error=?, updated_at=?
                        WHERE id=?
                        """,
                        (next_at, safe_error, now, message_id),
                    )
                    action = f"Ошибка отправки, повтор {attempts + 1}/{max_attempts} через {delay} сек."
                    if safe_error:
                        action += f" {safe_error[:160]}"
                else:
                    connection.execute(
                        """
                        UPDATE outbound_messages
                        SET status='failed', claimed_at='', next_attempt_at='',
                            last_error=?, failed_at=?, updated_at=?, media_path=''
                        WHERE id=?
                        """,
                        (safe_error, now, now, message_id),
                    )
                    if media_path:
                        try:
                            Path(media_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                    action = "Не удалось отправить ответ после повторных попыток"
                    if safe_error:
                        action += f": {safe_error[:160]}"
            level = "info" if sent else ("warning" if attempts < max_attempts else "error")
            self._audit(
                connection, "message", str(row["actor"] or "Система"), action,
                "ticket" if row["ticket_id"] else "chat", row["ticket_id"] or row["chat_id"],
                normalize_message(error)[:500], level=level, created_at=now,
            )
            if row["ticket_id"]:
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (row["ticket_id"], action, row["actor"], now),
                )
            current = connection.execute(
                "SELECT * FROM outbound_messages WHERE id=?", (message_id,)
            ).fetchone()
            return dict(current) if current else None

    def outbound_queue_snapshot(self, limit: int = 30) -> dict[str, Any]:
        with self.connection() as connection:
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM outbound_messages GROUP BY status"
                ).fetchall()
            }
            rows = connection.execute(
                """
                SELECT id, ticket_id, chat_id, phone, actor, status, created_at, updated_at,
                       attempt_count, max_attempts, next_attempt_at, last_error, provider_id,
                       sent_at, failed_at, CASE WHEN media_path<>'' THEN 1 ELSE 0 END AS has_media
                FROM outbound_messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return {"counts": counts, "rows": [dict(row) for row in rows]}

    def retry_failed_outbound(self) -> int:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbound_messages
                SET status='pending', attempt_count=0, claimed_at='', next_attempt_at='',
                    failed_at='', updated_at=?,
                    last_error=CASE WHEN last_error='' THEN 'Повтор запрошен администратором' ELSE last_error END
                WHERE status='failed'
                """,
                (now,),
            )
            return int(cursor.rowcount or 0)
