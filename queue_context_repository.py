"""SQLite persistence for dialog context; uses the caller's transaction.

Schema and JSON codecs stay here. Parsing, required fields, and decisions about
creating or updating a ticket belong to queue_contextual_tickets.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, ContextManager, Protocol

from ticketing import normalize_message, utc_now


class ConnectionProvider(Protocol):
    def connection(self) -> ContextManager[sqlite3.Connection]: ...


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        return fallback
    return parsed


class ContextRepository:
    def __init__(self, store: ConnectionProvider):
        self.store = store

    def initialize(self) -> None:
        with self.store.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS contextual_conversation_state (
                    contact_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    field_meta_json TEXT NOT NULL DEFAULT '{}',
                    skipped_json TEXT NOT NULL DEFAULT '[]',
                    last_question_field TEXT NOT NULL DEFAULT '',
                    last_question_text TEXT NOT NULL DEFAULT '',
                    repeat_count INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'idle',
                    last_message_key TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS contextual_message_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL DEFAULT '',
                    contact_key TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    message_key TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL DEFAULT '',
                    attachment_name TEXT NOT NULL DEFAULT '',
                    parsed_json TEXT NOT NULL DEFAULT '{}',
                    confidence_json TEXT NOT NULL DEFAULT '{}',
                    unresolved_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_context_analysis_contact
                    ON contextual_message_analysis(contact_key, id DESC);
                CREATE TABLE IF NOT EXISTS ticket_context_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL DEFAULT '',
                    ticket_id INTEGER NOT NULL DEFAULT 0,
                    contact_key TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT '',
                    field_name TEXT NOT NULL DEFAULT '',
                    old_value TEXT NOT NULL DEFAULT '',
                    new_value TEXT NOT NULL DEFAULT '',
                    message_key TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    media_name TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ticket_context_history_ticket
                    ON ticket_context_history(ticket_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_ticket_context_history_session
                    ON ticket_context_history(session_id, id ASC);
                CREATE TABLE IF NOT EXISTS ticket_context_fields (
                    ticket_id INTEGER NOT NULL,
                    field_name TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    source_message_key TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(ticket_id, field_name)
                );
                """
            )

    def state(self, contact_key: str) -> dict[str, Any]:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM contextual_conversation_state WHERE contact_key = ?",
                (contact_key,),
            ).fetchone()
        if not row:
            return {}
        result = dict(row)
        result["field_meta"] = _load_json(result.get("field_meta_json"), {})
        result["skipped"] = _load_json(result.get("skipped_json"), [])
        return result

    def write_state(self, contact_key: str, state: dict[str, Any]) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO contextual_conversation_state
                    (contact_key, category, session_id, field_meta_json, skipped_json,
                     last_question_field, last_question_text, repeat_count, state,
                     last_message_key, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(contact_key) DO UPDATE SET
                    category=excluded.category, session_id=excluded.session_id,
                    field_meta_json=excluded.field_meta_json, skipped_json=excluded.skipped_json,
                    last_question_field=excluded.last_question_field,
                    last_question_text=excluded.last_question_text,
                    repeat_count=excluded.repeat_count, state=excluded.state,
                    last_message_key=excluded.last_message_key, updated_at=excluded.updated_at
                """,
                (
                    contact_key, state.get("category", ""), state.get("session_id", ""),
                    _json(state.get("field_meta", {})), _json(state.get("skipped", [])),
                    state.get("last_question_field", ""), state.get("last_question_text", ""),
                    int(state.get("repeat_count", 0) or 0), state.get("state", "idle"),
                    state.get("last_message_key", ""), utc_now(),
                ),
            )

    def record_history(
        self, *, session_id: str, ticket_id: int, contact_key: str,
        chat_id: str, event_type: str, field_name: str = "", old_value: str = "",
        new_value: str = "", message_key: str = "", text: str = "", media_name: str = "",
        confidence: float = 0.0, source: str = "WhatsApp",
    ) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO ticket_context_history
                    (session_id,ticket_id,contact_key,chat_id,event_type,field_name,old_value,new_value,
                     message_key,text,media_name,confidence,source,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id, int(ticket_id or 0), contact_key, chat_id, event_type, field_name,
                    normalize_message(old_value)[:1800], normalize_message(new_value)[:1800],
                    normalize_message(message_key)[:180], normalize_message(text)[:32000],
                    normalize_message(media_name)[:240], float(confidence or 0), source[:80], utc_now(),
                ),
            )

    def record_analysis(self, *, session_id: str, contact_key: str, chat_id: str,
                        message_key: str, category: str, text: str, attachment_name: str,
                        fields: dict, confidence: dict, unresolved: list) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """INSERT INTO contextual_message_analysis
                   (session_id,contact_key,chat_id,message_key,category,raw_text,attachment_name,
                    parsed_json,confidence_json,unresolved_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, contact_key, chat_id, message_key, category, text, attachment_name,
                 _json(fields), _json(confidence), _json(unresolved), utc_now()),
            )

    def link_session(self, session_id: str, ticket_id: int) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE ticket_context_history SET ticket_id = ? WHERE session_id = ? AND ticket_id = 0",
                (ticket_id, session_id),
            )

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT chat_id,message_key,text,media_name FROM ticket_context_history
                   WHERE session_id=? AND event_type='message_received' ORDER BY id""",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def session_message_keys(self, session_id: str) -> list[tuple[str, str]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT chat_id,message_key FROM ticket_context_history
                   WHERE session_id=? AND event_type='message_received'
                     AND message_key<>'' AND chat_id<>''""",
                (session_id,),
            ).fetchall()
        return [(row["chat_id"], row["message_key"]) for row in rows]

    def upsert_field(self, ticket_id: int, field_name: str, value: str, confidence: float,
                     source: str, message_key: str, *, updated_at: str | None = None) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """INSERT INTO ticket_context_fields
                   (ticket_id,field_name,value,confidence,source,source_message_key,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(ticket_id,field_name) DO UPDATE SET
                     value=excluded.value, confidence=excluded.confidence, source=excluded.source,
                     source_message_key=excluded.source_message_key, updated_at=excluded.updated_at""",
                (ticket_id, field_name, value, confidence, source, message_key,
                 updated_at or utc_now()),
            )

    def append_ticket_text(self, ticket_id: int, text: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE tickets SET original_text=COALESCE(original_text,'') || ?, updated_at=? WHERE id=?",
                (text, utc_now(), ticket_id),
            )

    def history_item(self, ticket_id: int, history_id: int) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM ticket_context_history WHERE id = ? AND ticket_id = ?",
                (int(history_id), int(ticket_id)),
            ).fetchone()
        return dict(row) if row else None

    def add_event(self, ticket_id: int, action: str, actor: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)",
                (ticket_id, action, actor, utc_now()),
            )

    def ticket_fields(self, ticket_id: int) -> list[dict[str, Any]]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ticket_context_fields WHERE ticket_id = ? ORDER BY field_name",
                (int(ticket_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def ticket_history(self, ticket_id: int, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 200), 500))
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ticket_context_history WHERE ticket_id = ? ORDER BY id DESC LIMIT ?",
                (int(ticket_id), safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]
