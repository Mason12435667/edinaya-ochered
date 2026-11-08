"""Статусы, категории и приоритеты заявок, а также TicketStore — хранилище приложения в SQLite.

TicketStore отвечает за соединение, транзакции и заявки; каждая другая тема вынесена в
подмешиваемый класс в своём модуле queue_store_*, а схема и её обновления — в queue_store_schema.
"""
from __future__ import annotations

import difflib
import logging
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import queue_store_schema
from queue_core import normalize_message, utc_now
from queue_store_chat_aliases import ChatAliasStore
from queue_store_contacts import ContactStore
from queue_store_conversations import ConversationStore
from queue_store_messages import MessageStore
from queue_store_notifications import NotificationStore
from queue_store_outbound import OutboundStore
from queue_store_reports import ReportStore


STATUSES = {
    "new": "Не тронута",
    "in_progress": "В работе",
    "done": "Сделано",
    "invalid": "Недействительная",
}

CATEGORIES = {'database': 'Доступ к системе', 'mobile': 'Мобильное приложение', 'incident': 'Сообщить о проблеме', 'general': 'Общее обращение', 'support': 'Вопрос в поддержку'}

PRIORITIES = {
    "low": "Низкий",
    "normal": "Обычный",
    "high": "Высокий",
    "urgent": "Срочный",
}


class TicketStore(ChatAliasStore, ContactStore, MessageStore, NotificationStore,
                  OutboundStore, ConversationStore, ReportStore):
    """SQLite storage of the whole application; each topic lives in its own mixin module."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._transaction_state = threading.local()
        self.initialize()

    @contextmanager
    def atomic_inbound(self):
        """Commit the dedup marker and all inbound DB effects together."""
        if getattr(self._transaction_state, "connection", None) is not None:
            yield self._transaction_state.connection
            return
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._transaction_state.connection = connection
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._transaction_state.connection = None

    # One transaction for any group of writes that must commit (or roll back) together.
    atomic = atomic_inbound

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        shared = getattr(self._transaction_state, "connection", None)
        if shared is not None:
            yield shared
            return
        started = time.monotonic()
        caller = sys._getframe(2).f_code.co_name
        connection = None
        failed = False
        try:
            connection = sqlite3.connect(self.database_path, timeout=15)
            connection.row_factory = sqlite3.Row
            connection.create_function("FOLD", 1, lambda value: normalize_message(str(value or "")).casefold())
            connection.create_function("DIGITS", 1, lambda value: re.sub(r"\D", "", str(value or "")))
            connection.execute("PRAGMA busy_timeout = 15000")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA cache_size = -32768")
            connection.execute("PRAGMA mmap_size = 134217728")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
            yield connection
            connection.commit()
        except BaseException as error:
            failed = True
            if connection is not None:
                connection.rollback()
            logging.getLogger(__name__).warning(
                "[db141] operation=%s duration=%.3fs error=%s thread=%s",
                caller, time.monotonic() - started, type(error).__name__,
                threading.current_thread().name,
            )
            raise
        finally:
            if connection is not None:
                connection.close()
            elapsed = time.monotonic() - started
            if elapsed >= 2.0 and not failed:
                logging.getLogger(__name__).warning(
                    "[db141] slow operation=%s duration=%.3fs thread=%s",
                    caller, elapsed, threading.current_thread().name,
                )

    def initialize(self) -> None:
        with self.connection() as connection:
            queue_store_schema.create(connection)
            queue_store_schema.migrate(connection)
            queue_store_schema.create_indexes(connection)
            self._merge_whatsapp_lid_aliases(connection)
            # Let SQLite refresh planner statistics for the newly created indexes.
            connection.execute("PRAGMA optimize")

    def create_ticket(self, payload: dict[str, Any]) -> int:
        now = utc_now()
        fields = {
            "source": payload.get("source", "whatsapp"),
            "sender": payload.get("sender", "Неизвестный отправитель"),
            "phone": payload.get("phone", ""),
            "chat_id": payload.get("chat_id", ""),
            "external_id": payload.get("external_id", ""),
            "category": payload.get("category", "general"),
            "priority": payload.get("priority", "normal")
            if payload.get("priority", "normal") in PRIORITIES
            else "normal",
            "title": payload.get("title", "Новый запрос"),
            "summary": payload.get("summary", ""),
            "original_text": payload.get("original_text", ""),
            "attachment_name": payload.get("attachment_name", ""),
            "status": payload.get("status", "new"),
            "assigned_to": payload.get("assigned_to", ""),
            "created_at": now,
            "updated_at": now,
            "closed_at": now if payload.get("status") in {"done", "invalid"} else "",
            "first_response_at": now if payload.get("status") not in {None, "", "new"} else "",
            "shift_handoff": 1 if payload.get("shift_handoff") else 0,
            "handoff_from": payload.get("handoff_from", ""),
            "handoff_at": payload.get("handoff_at", ""),
        }
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with self.connection() as connection:
            cursor = connection.execute(
                f"INSERT INTO tickets ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
            ticket_id = int(cursor.lastrowid)
            actor = str(payload.get("assigned_to", "") or "Система")
            connection.execute(
                "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, "Заявка создана", actor, now),
            )
            self._audit(
                connection, "ticket", actor, "Заявка создана", "ticket", ticket_id,
                f"{fields['title']} · {fields['sender']} · {fields['source']}", created_at=now,
            )
        return ticket_id

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def list_tickets(
        self,
        status: str = "",
        category: str = "",
        priority: str = "",
        query: str = "",
        limit: int = 0,
        offset: int = 0,
        view: str = "",
        employee: str = "",
    ) -> list[dict[str, Any]]:
        where, params = self._ticket_filter(status, category, priority, query, view, employee)
        pagination = ""
        if limit > 0:
            pagination = " LIMIT ? OFFSET ?"
            params.extend([str(limit), str(max(0, offset))])
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM tickets{where} ORDER BY shift_handoff DESC, id DESC{pagination}",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_tickets(
        self,
        status: str = "",
        category: str = "",
        priority: str = "",
        query: str = "",
        view: str = "",
        employee: str = "",
    ) -> int:
        where, params = self._ticket_filter(status, category, priority, query, view, employee)
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS amount FROM tickets{where}",
                params,
            ).fetchone()
        return int(row["amount"])

    @staticmethod
    def _ticket_filter(
        status: str,
        category: str,
        priority: str,
        query: str,
        view: str = "",
        employee: str = "",
    ) -> tuple[str, list[str]]:
        conditions: list[str] = []
        params: list[str] = []
        if status in STATUSES:
            conditions.append("status = ?")
            params.append(status)
        if category in CATEGORIES:
            conditions.append("category = ?")
            params.append(category)
        if priority in PRIORITIES:
            conditions.append("priority = ?")
            params.append(priority)
        if view == "mine" and employee:
            conditions.extend(["assigned_to = ?", "status IN ('new','in_progress')"] )
            params.append(employee)
        elif view == "handoff":
            conditions.extend(["shift_handoff = 1", "status IN ('new','in_progress')"] )
        elif view == "open":
            conditions.append("status IN ('new','in_progress')")
        elif view == "urgent":
            conditions.extend(["status IN ('new','in_progress')", "priority = 'urgent'"] )
        clean_query = normalize_message(query).casefold()
        query_digits = re.sub(r"\D", "", clean_query)
        phone_only = bool(len(query_digits) >= 7 and re.fullmatch(r"[+\d\s().-]+", clean_query))
        if phone_only:
            canonical_digits = ("7" + query_digits[1:]) if len(query_digits) == 11 and query_digits.startswith("8") else query_digits
            # Длинная цифровая строка может быть не только телефоном, но и номером
            # из текста заявки. Поэтому форматированный телефон ищем по phone/chat_id,
            # а те же цифры одновременно проверяем в тексте заявки.
            conditions.append(
                "(DIGITS(phone) LIKE ? OR DIGITS(chat_id) LIKE ? OR FOLD(original_text) LIKE ? "
                "OR FOLD(summary) LIKE ?)"
            )
            params.extend([
                f"%{canonical_digits}%", f"%{canonical_digits}%", f"%{query_digits}%", f"%{query_digits}%",
            ])
            tokens: list[str] = []
        else:
            tokens = [token for token in re.split(r"\s+", clean_query) if token][:8]
        category_aliases = {
            key: normalize_message(label).casefold() for key, label in CATEGORIES.items()
        }
        searchable = [
            "CAST(id AS TEXT)", "source", "sender", "phone", "chat_id", "category",
            "title", "summary", "original_text",
        ]
        for token in tokens:
            token_conditions = [f"FOLD({field}) LIKE ?" for field in searchable]
            token_params = [f"%{token}%"] * len(searchable)
            digits = re.sub(r"\D", "", token)
            if digits:
                canonical_digits = ("7" + digits[1:]) if len(digits) == 11 and digits.startswith("8") else digits
                token_conditions.extend(["DIGITS(phone) LIKE ?", "DIGITS(chat_id) LIKE ?"])
                token_params.extend([f"%{canonical_digits}%", f"%{canonical_digits}%"])
            matching_categories = [
                key for key, label in category_aliases.items()
                if token in label or label in token
            ]
            if matching_categories:
                token_conditions.append("category IN (" + ",".join("?" for _ in matching_categories) + ")")
                token_params.extend(matching_categories)
            conditions.append("(" + " OR ".join(token_conditions) + ")")
            params.extend(token_params)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

    def counts(self) -> dict[str, int]:
        result = {status: 0 for status in STATUSES}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS amount FROM tickets GROUP BY status"
            ).fetchall()
        for row in rows:
            result[row["status"]] = row["amount"]
        result["all"] = sum(result.values())
        return result

    def delete_ticket(self, ticket_id: int, actor: str = "Администратор") -> bool:
        if ticket_id <= 0:
            return False
        with self.connection() as connection:
            row = connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not row:
                return False
            # Это только внутреннее удаление. Никакое исходящее сообщение не
            # создаётся. Неотправленные ответы этой заявки удаляем из очереди.
            connection.execute("DELETE FROM outbound_messages WHERE ticket_id = ?", (ticket_id,))
            connection.execute("DELETE FROM events WHERE ticket_id = ?", (ticket_id,))
            connection.execute("UPDATE whatsapp_chat_messages SET ticket_id = 0 WHERE ticket_id = ?", (ticket_id,))
            connection.execute(
                "UPDATE conversation_contexts SET active_ticket_id = 0, force_new = 0, draft_json = '{}' WHERE active_ticket_id = ?",
                (ticket_id,),
            )
            connection.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            self._audit(connection, "ticket", normalize_message(actor)[:100] or "Администратор", "Заявка удалена", "ticket", ticket_id, level="warning")
        return True

    def dashboard_version(self) -> str:
        with self.connection() as connection:
            tickets = connection.execute(
                "SELECT COUNT(*) AS amount, COALESCE(MAX(id), 0) AS last_id FROM tickets"
            ).fetchone()
            events = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS last_id FROM events"
            ).fetchone()
        return f"{tickets['amount']}:{tickets['last_id']}:{events['last_id']}"

    def shift_summary(self, employee: str) -> dict[str, Any]:
        employee = normalize_message(employee)[:100]
        with self.connection() as connection:
            row = connection.execute(
                """SELECT
                    SUM(CASE WHEN assigned_to=? AND status IN ('new','in_progress') THEN 1 ELSE 0 END) AS mine,
                    SUM(CASE WHEN shift_handoff=1 AND status IN ('new','in_progress') THEN 1 ELSE 0 END) AS handoff,
                    SUM(CASE WHEN status IN ('new','in_progress') THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN priority='urgent' AND status IN ('new','in_progress') THEN 1 ELSE 0 END) AS urgent
                    FROM tickets""",
                (employee,),
            ).fetchone()
        return {
            "mine": int(row["mine"] or 0),
            "handoff": int(row["handoff"] or 0),
            "open": int(row["open_count"] or 0),
            "urgent": int(row["urgent"] or 0),
            "previous_employee": self.get_setting("previous_shift_employee", ""),
        }

    def switch_shift(self, previous_employee: str, new_employee: str) -> int:
        previous_employee = normalize_message(previous_employee)[:100]
        new_employee = normalize_message(new_employee)[:100]
        if not previous_employee or not new_employee or previous_employee == new_employee:
            return 0
        now = utc_now()
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM tickets WHERE assigned_to=? AND status IN ('new','in_progress') AND shift_handoff=0",
                (previous_employee,),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE tickets SET shift_handoff=1, handoff_from=?, handoff_at=?, updated_at=? WHERE id IN ({placeholders})",
                    [previous_employee, now, now, *ids],
                )
                connection.executemany(
                    "INSERT INTO events(ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    [(ticket_id, "Автоматически передано следующей смене", previous_employee, now) for ticket_id in ids],
                )
            self._audit(
                connection, "shift", new_employee, "Смена сотрудника", "shift", new_employee,
                f"Предыдущая смена: {previous_employee}; автоматически передано заявок: {len(ids)}", created_at=now,
            )
        self.set_setting("previous_shift_employee", previous_employee)
        return len(ids)

    def set_ticket_handoff(self, ticket_id: int, enabled: bool, actor: str) -> bool:
        now = utc_now()
        with self.connection() as connection:
            ticket = connection.execute("SELECT id, status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not ticket or str(ticket["status"]) in {"done", "invalid"}:
                return False
            if enabled:
                cursor = connection.execute(
                    "UPDATE tickets SET shift_handoff = 1, handoff_from = ?, handoff_at = ?, updated_at = ? WHERE id = ?",
                    (actor, now, now, ticket_id),
                )
                action = "Передано следующей смене"
            else:
                cursor = connection.execute(
                    "UPDATE tickets SET shift_handoff = 0, handoff_from = '', handoff_at = '', assigned_to = ?, updated_at = ? WHERE id = ?",
                    (actor, now, ticket_id),
                )
                action = "Принято со следующей смены"
            if cursor.rowcount:
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (ticket_id, action, actor, now),
                )
                self._audit(connection, "shift", actor, action, "ticket", ticket_id, created_at=now)
                return True
        return False

    def find_similar_open_ticket(self, payload: dict[str, Any], hours: int = 72) -> dict[str, Any] | None:
        """Find a duplicate ticket from the same requester.

        Two protections are used:
        1. an exact repeat of the same data is blocked for 30 minutes even if the
           previous ticket was already closed, which stops button/message spam;
        2. an open ticket with the same or very similar data is treated as the
           same request for up to seven days.
        Different identifiers/data are not blocked merely because the same user
        created several tickets.
        """
        chat_id = normalize_message(str(payload.get("chat_id", "")))[:120]
        phone = re.sub(r"\D", "", str(payload.get("phone", "")))
        category = str(payload.get("category", ""))
        has_identity = bool(chat_id or phone)
        now_dt = datetime.now(timezone.utc)
        active_cutoff = (now_dt - timedelta(hours=max(168, max(1, hours)))).replace(microsecond=0).isoformat()
        exact_cutoff = (now_dt - timedelta(minutes=30)).replace(microsecond=0).isoformat()

        identity_sql: list[str] = []
        identity_params: list[Any] = []
        if chat_id:
            identity_sql.append("chat_id = ?")
            identity_params.append(chat_id)
        if phone:
            identity_sql.append("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?")
            identity_params.append(f"%{phone[-10:]}")

        def fingerprint(value: str) -> str:
            value = normalize_message(value).casefold()
            value = re.sub(r"[^a-zа-яё0-9]+", " ", value)
            stop = {"заявка", "запрос", "проблема", "пожалуйста", "нужно", "надо", "номер"}
            tokens = [token for token in value.split() if token not in stop]
            return " ".join(tokens)

        def ticket_text(row: dict[str, Any]) -> str:
            return fingerprint(" ".join([
                str(row.get("summary", "")), str(row.get("original_text", "")),
            ]))

        new_text = ticket_text(payload)
        new_numbers = set(re.findall(r"\d{4,}", new_text))

        # Exact recent repeats: include closed tickets too. This is deliberately a
        # short window so a genuinely recurring problem can be submitted later.
        recent_conditions = ["created_at >= ?"]
        recent_params: list[Any] = [exact_cutoff]
        if identity_sql:
            recent_conditions.append("(" + " OR ".join(identity_sql) + ")")
            recent_params.extend(identity_params)
        elif category:
            recent_conditions.append("category = ?")
            recent_params.append(category)
        with self.connection() as connection:
            recent_rows = connection.execute(
                f"SELECT * FROM tickets WHERE {' AND '.join(recent_conditions)} ORDER BY id DESC LIMIT 50",
                recent_params,
            ).fetchall()
        for row in recent_rows:
            existing = dict(row)
            if str(existing.get("status", "")) == "invalid":
                continue
            old_text = ticket_text(existing)
            # Same normalized text.
            if new_text and old_text and new_text == old_text:
                return existing
            # Same set of meaningful identifiers is an exact-data repeat even when
            # the user changes a few words around it.
            old_numbers = set(re.findall(r"\d{4,}", old_text))
            if new_numbers and old_numbers and new_numbers == old_numbers and len(new_numbers) >= 2:
                return existing

        # Open duplicate protection: same requester + similar data for up to 7 days.
        conditions = ["status IN ('new','in_progress')", "created_at >= ?"]
        params: list[Any] = [active_cutoff]
        if identity_sql:
            conditions.append("(" + " OR ".join(identity_sql) + ")")
            params.extend(identity_params)
        elif category:
            conditions.append("category = ?")
            params.append(category)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM tickets WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT 60",
                params,
            ).fetchall()

        new_tokens = set(new_text.split())
        for row in rows:
            existing = dict(row)
            old_text = ticket_text(existing)
            if not new_text or not old_text:
                continue
            if new_text == old_text:
                return existing
            ratio = difflib.SequenceMatcher(None, new_text, old_text).ratio()
            old_tokens = set(old_text.split())
            union = new_tokens | old_tokens
            jaccard = len(new_tokens & old_tokens) / len(union) if union else 0.0
            old_numbers = set(re.findall(r"\d{4,}", old_text))
            common_numbers = new_numbers & old_numbers
            identifier_match = bool(common_numbers)
            # Two or more identical long identifiers from the same requester are a
            # strong duplicate signal even if the wording around them is different.
            strong_identifier_match = len(common_numbers) >= 2
            if has_identity:
                matched = (
                    ratio >= 0.78 or jaccard >= 0.64 or
                    (identifier_match and jaccard >= 0.38) or strong_identifier_match
                )
            else:
                matched = (
                    (identifier_match and jaccard >= 0.55) or
                    (ratio >= 0.92 and jaccard >= 0.78)
                )
            if matched:
                return existing
        return None

    def update_status(
        self,
        ticket_id: int,
        status: str,
        actor: str,
        assigned_to: str | None = None,
    ) -> bool:
        if status not in STATUSES:
            return False
        now = utc_now()
        closed_at = now if status in {"done", "invalid"} else ""
        assignee = assigned_to if assigned_to is not None else actor
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT status, assigned_to, created_at, first_response_at FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE tickets
                SET status = ?, assigned_to = ?, updated_at = ?, closed_at = ?,
                    first_response_at = CASE WHEN first_response_at='' AND ? <> 'new' THEN ? ELSE first_response_at END,
                    shift_handoff = CASE WHEN ? IN ('in_progress','done','invalid') THEN 0 ELSE shift_handoff END,
                    handoff_from = CASE WHEN ? IN ('in_progress','done','invalid') THEN '' ELSE handoff_from END,
                    handoff_at = CASE WHEN ? IN ('in_progress','done','invalid') THEN '' ELSE handoff_at END
                WHERE id = ?
                """,
                (status, assignee, now, closed_at, status, now, status, status, status, ticket_id),
            )
            if cursor.rowcount:
                action = f"Статус изменён: {STATUSES[status]}"
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (ticket_id, action, actor, now),
                )
                before = str(previous["status"] if previous else "")
                details = f"{STATUSES.get(before, before) or '—'} → {STATUSES[status]}; исполнитель: {assignee}"
                self._audit(connection, "status", actor, action, "ticket", ticket_id, details, created_at=now)
                return True
        return False

    def update_priority(self, ticket_id: int, priority: str, actor: str) -> bool:
        if priority not in PRIORITIES:
            return False
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE tickets SET priority = ?, updated_at = ? WHERE id = ?",
                (priority, now, ticket_id),
            )
            if cursor.rowcount:
                action = f"Приоритет изменён: {PRIORITIES[priority]}"
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (ticket_id, action, actor, now),
                )
                self._audit(connection, "priority", actor, action, "ticket", ticket_id, created_at=now)
                return True
        return False

    def ticket_events(self, ticket_id: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY id DESC",
                (ticket_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def list_active_user_tickets(
        self, chat_id: str = "", phone: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return only this WhatsApp user's currently active tickets.

        Matching prefers the canonical chat id and also accepts the same normalized
        phone number, so an @lid/@c.us alias does not hide or duplicate a ticket.
        """
        safe_limit = min(max(1, int(limit or 10)), 30)
        phone_digits = re.sub(r"\D", "", normalize_message(phone))[:24]
        if len(phone_digits) == 11 and phone_digits.startswith("8"):
            phone_digits = "7" + phone_digits[1:]
        elif len(phone_digits) == 10:
            phone_digits = "7" + phone_digits
        with self.connection() as connection:
            canonical = self._canonical_whatsapp_chat_id(connection, chat_id) if chat_id else ""
            rows = connection.execute(
                """
                SELECT * FROM tickets
                 WHERE source='whatsapp' AND status IN ('new','in_progress')
                 ORDER BY updated_at DESC, id DESC
                 LIMIT 200
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            seen: set[int] = set()
            for row in rows:
                data = dict(row)
                ticket_id = int(data.get("id", 0) or 0)
                if not ticket_id or ticket_id in seen:
                    continue
                row_chat = normalize_message(str(data.get("chat_id", "")))[:120]
                row_canonical = self._canonical_whatsapp_chat_id(connection, row_chat) if row_chat else ""
                row_digits = re.sub(r"\D", "", normalize_message(str(data.get("phone", ""))))[:24]
                if len(row_digits) == 11 and row_digits.startswith("8"):
                    row_digits = "7" + row_digits[1:]
                elif len(row_digits) == 10:
                    row_digits = "7" + row_digits
                same_chat = bool(canonical and row_canonical and canonical == row_canonical)
                same_phone = bool(phone_digits and row_digits and phone_digits == row_digits)
                if not (same_chat or same_phone):
                    continue
                seen.add(ticket_id)
                result.append(data)
                if len(result) >= safe_limit:
                    break
        return result

    def active_context_ticket(self, contact_key: str) -> dict[str, Any] | None:
        context = self.get_conversation_context(contact_key)
        if not context or not context["active_ticket_id"]:
            return None
        ticket = self.get_ticket(int(context["active_ticket_id"]))
        if not ticket or ticket["status"] not in {"new", "in_progress"}:
            return None
        return ticket

    def note_context_followup(
        self, ticket_id: int, body: str = "", attachment_name: str = ""
    ) -> bool:
        now = utc_now()
        clean_body = normalize_message(body)[:700]
        clean_attachment = normalize_message(attachment_name)[:240]
        details: list[str] = []
        if clean_body:
            details.append(clean_body)
        if clean_attachment:
            details.append(f"Вложение: {clean_attachment}")
        action = "Получено уточнение от пользователя"
        if details:
            action += ": " + " | ".join(details)
        action = action[:1000]
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE tickets SET updated_at = ? WHERE id = ? AND status IN ('new','in_progress')",
                (now, ticket_id),
            )
            if not cursor.rowcount:
                return False
            connection.execute(
                "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, action, "WhatsApp", now),
            )
            return True

    def database_health(self) -> dict[str, Any]:
        try:
            with self.connection() as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
                result = str(row[0] if row else "unknown")
                tables = int(connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0])
            return {"ok": result.lower() == "ok", "result": result, "tables": tables}
        except Exception as exc:
            return {"ok": False, "result": normalize_message(str(exc))[:300], "tables": 0}

    def claim_inbound_message(self, external_id: str, sender: str) -> bool:
        if not external_id:
            return True
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO inbound_messages (external_id, sender, received_at) VALUES (?, ?, ?)",
                (external_id, sender, utc_now()),
            )
            return bool(cursor.rowcount)
