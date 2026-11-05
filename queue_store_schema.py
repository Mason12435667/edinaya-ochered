"""Database schema: tables and indexes of a new database, then upgrades of an existing one.

Both steps are idempotent and run on every start, inside TicketStore.initialize().
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_requests (
    request_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, actor TEXT NOT NULL,
    message_id INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    sender TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    original_text TEXT NOT NULL DEFAULT '',
    attachment_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    assigned_to TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT '',
    first_response_at TEXT NOT NULL DEFAULT '',
    shift_handoff INTEGER NOT NULL DEFAULT 0,
    handoff_from TEXT NOT NULL DEFAULT '',
    handoff_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS error_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    attachment_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    admin_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL DEFAULT 'action',
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    object_type TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_messages (
    external_id TEXT PRIMARY KEY,
    sender TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    missing_fields TEXT NOT NULL,
    reply_body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    provider_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT '',
    mentions_json TEXT NOT NULL DEFAULT '[]',
    reply_to_key TEXT NOT NULL DEFAULT '',
    media_path TEXT NOT NULL DEFAULT '',
    media_mime TEXT NOT NULL DEFAULT '',
    media_name TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 4,
    next_attempt_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    provider_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT '',
    failed_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE IF NOT EXISTS conversation_contexts (
    contact_key TEXT PRIMARY KEY,
    pending_category TEXT NOT NULL DEFAULT '',
    active_ticket_id INTEGER NOT NULL DEFAULT 0,
    force_new INTEGER NOT NULL DEFAULT 0,
    draft_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whatsapp_chats (
    chat_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    last_timestamp INTEGER NOT NULL DEFAULT 0,
    unread_count INTEGER NOT NULL DEFAULT 0,
    mention_unread_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whatsapp_chat_aliases (
    alias_id TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_chat_aliases_canonical
    ON whatsapp_chat_aliases(canonical_id);

CREATE TABLE IF NOT EXISTS whatsapp_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    from_me INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL DEFAULT 'chat',
    message_timestamp INTEGER NOT NULL DEFAULT 0,
    ack INTEGER NOT NULL DEFAULT 0,
    media_path TEXT NOT NULL DEFAULT '',
    media_mime TEXT NOT NULL DEFAULT '',
    media_name TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    ticket_id INTEGER NOT NULL DEFAULT 0,
    mentions_json TEXT NOT NULL DEFAULT '[]',
    notify INTEGER NOT NULL DEFAULT 0,
    quoted_message_key TEXT NOT NULL DEFAULT '',
    quoted_body TEXT NOT NULL DEFAULT '',
    quoted_sender TEXT NOT NULL DEFAULT '',
    forwarded INTEGER NOT NULL DEFAULT 0,
    edited INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    reactions_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, message_key)
);

CREATE TABLE IF NOT EXISTS whatsapp_contacts (
    chat_id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whatsapp_groups (
    chat_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    participant_count INTEGER NOT NULL DEFAULT 0,
    added_by_admin INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whatsapp_call_permissions (
    contact_key TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whatsapp_manual_chat_modes (
    chat_id TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_reply_cooldowns (
    contact_key TEXT NOT NULL,
    reply_kind TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,
    PRIMARY KEY(contact_key, reply_kind)
);

CREATE TABLE IF NOT EXISTS whatsapp_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_key TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_chat_messages_chat_time
ON whatsapp_chat_messages(chat_id, message_timestamp, id);
CREATE INDEX IF NOT EXISTS idx_tickets_status_updated
ON tickets(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_error_reports_status_created
ON error_reports(status, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_error_reports_chat_created
ON error_reports(chat_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_category_priority
ON tickets(category, priority, updated_at);
CREATE INDEX IF NOT EXISTS idx_outbound_status_created
ON outbound_messages(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_whatsapp_actions_status_created
ON whatsapp_actions(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_whatsapp_chats_last_timestamp
ON whatsapp_chats(last_timestamp DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_whatsapp_groups_enabled_name
ON whatsapp_groups(added_by_admin, name);
CREATE INDEX IF NOT EXISTS idx_audit_log_created
ON audit_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_level
ON audit_log(level, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee_status
ON tickets(assigned_to, status, shift_handoff, priority, id DESC);
"""


def create(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(SCHEMA)


def _add_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> set[str]:
    """Add the columns an older database lacks, in the given order; return the names added."""
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    added = set()
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            added.add(name)
    return added


def migrate(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "tickets", {
        "chat_id": "TEXT NOT NULL DEFAULT ''",
        "external_id": "TEXT NOT NULL DEFAULT ''",
        "priority": "TEXT NOT NULL DEFAULT 'normal'",
        "shift_handoff": "INTEGER NOT NULL DEFAULT 0",
        "handoff_from": "TEXT NOT NULL DEFAULT ''",
        "handoff_at": "TEXT NOT NULL DEFAULT ''",
        "first_response_at": "TEXT NOT NULL DEFAULT ''",
    })
    connection.execute(
        "UPDATE tickets SET first_response_at = updated_at WHERE first_response_at = '' AND status <> 'new'"
    )
    _add_columns(connection, "conversation_contexts", {
        "force_new": "INTEGER NOT NULL DEFAULT 0",
        "draft_json": "TEXT NOT NULL DEFAULT '{}'",
    })
    _add_columns(connection, "whatsapp_chat_messages", {
        "ack": "INTEGER NOT NULL DEFAULT 0",
        "sender": "TEXT NOT NULL DEFAULT ''",
        "sender_phone": "TEXT NOT NULL DEFAULT ''",
        "sender_id": "TEXT NOT NULL DEFAULT ''",
        "media_path": "TEXT NOT NULL DEFAULT ''",
        "media_mime": "TEXT NOT NULL DEFAULT ''",
        "media_name": "TEXT NOT NULL DEFAULT ''",
        "transcript": "TEXT NOT NULL DEFAULT ''",
        "ticket_id": "INTEGER NOT NULL DEFAULT 0",
        "mentions_json": "TEXT NOT NULL DEFAULT '[]'",
        "notify": "INTEGER NOT NULL DEFAULT 0",
        "quoted_message_key": "TEXT NOT NULL DEFAULT ''",
        "quoted_body": "TEXT NOT NULL DEFAULT ''",
        "quoted_sender": "TEXT NOT NULL DEFAULT ''",
        "forwarded": "INTEGER NOT NULL DEFAULT 0",
        "edited": "INTEGER NOT NULL DEFAULT 0",
        "edit_timestamp": "INTEGER NOT NULL DEFAULT 0",
        "deleted": "INTEGER NOT NULL DEFAULT 0",
        "reactions_json": "TEXT NOT NULL DEFAULT '{}'",
    })
    _add_columns(connection, "whatsapp_groups", {
        "added_by_admin": "INTEGER NOT NULL DEFAULT 0",
        "muted": "INTEGER NOT NULL DEFAULT 0",
    })
    _add_columns(connection, "whatsapp_chats", {"mention_unread_count": "INTEGER NOT NULL DEFAULT 0"})
    _add_columns(connection, "outbound_messages", {
        "mentions_json": "TEXT NOT NULL DEFAULT '[]'",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "INTEGER NOT NULL DEFAULT 4",
        "next_attempt_at": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "provider_id": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "sent_at": "TEXT NOT NULL DEFAULT ''",
        "failed_at": "TEXT NOT NULL DEFAULT ''",
    })
    connection.execute(
        "UPDATE outbound_messages SET updated_at = created_at WHERE updated_at = ''"
    )
    _add_columns(connection, "whatsapp_actions", {"actor": "TEXT NOT NULL DEFAULT ''"})
    if _add_columns(connection, "outbound_messages", {"dispatch_started_at": "TEXT NOT NULL DEFAULT ''"}):
        connection.execute(
            "UPDATE outbound_messages SET status='uncertain', claimed_at='', next_attempt_at='', "
            "last_error='Отправка старой версии могла состояться. Проверьте переписку.' WHERE status='processing'"
        )
    # Legacy builds may have sent these already. Do not replay on boot.
    connection.execute(
        "UPDATE outbound_messages SET status='uncertain', claimed_at='', next_attempt_at='', "
        "last_error='Отправка могла состояться. Автоповтор остановлен, проверьте переписку.' "
        "WHERE status IN ('pending','failed') AND last_error LIKE '%WhatsApp не подтвердил отправку%'"
    )
    _add_columns(connection, "outbound_messages", {
        "reply_to_key": "TEXT NOT NULL DEFAULT ''",
        "media_path": "TEXT NOT NULL DEFAULT ''",
        "media_mime": "TEXT NOT NULL DEFAULT ''",
        "media_name": "TEXT NOT NULL DEFAULT ''",
    })
    # В базу исходящие сообщения попадают только после успешного
    # client.sendMessage, поэтому старое значение 0 означает
    # «отправлено», а не «ещё отправляется».
    connection.execute(
        "UPDATE whatsapp_chat_messages SET ack = 1 "
        "WHERE from_me = 1 AND ack < 1"
    )
    # Alias reconciliation updates these columns once per alias.
    # Without indexes every alias scans all tickets and outbound rows.
    connection.execute("CREATE INDEX IF NOT EXISTS idx_db141_tickets_chat ON tickets(chat_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_db141_outbound_chat ON outbound_messages(chat_id)")
