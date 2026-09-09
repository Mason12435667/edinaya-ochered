from __future__ import annotations

import io
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TERMINAL = {"done", "invalid"}
DEFAULT_RETENTION = 20
ALLOWED_CLOSE_REASONS = {
    "resolved": "Решено",
    "duplicate": "Дубль",
    "invalid": "Недействительно",
    "user_error": "Ошибка пользователя",
    "transferred": "Передано",
    "other": "Другое",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: object, limit: int = 1000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def initialize(store) -> None:
    with store.connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_recent_chats (
                actor TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                PRIMARY KEY(actor, chat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_recent_chats_opened
                ON workflow_recent_chats(actor, opened_at DESC);

            CREATE TABLE IF NOT EXISTS workflow_chat_presence (
                chat_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'viewing',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, client_id)
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_presence_updated
                ON workflow_chat_presence(chat_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS workflow_internal_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_notes_ticket
                ON workflow_internal_notes(ticket_id, id DESC);

            CREATE TABLE IF NOT EXISTS workflow_ticket_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_ticket_id INTEGER NOT NULL,
                target_ticket_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workflow_rel_source
                ON workflow_ticket_relations(source_ticket_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_workflow_rel_target
                ON workflow_ticket_relations(target_ticket_id, id DESC);

            CREATE TABLE IF NOT EXISTS workflow_ticket_close_meta (
                ticket_id INTEGER PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                closed_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS workflow_close_comment_categories (
                category TEXT PRIMARY KEY,
                required INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        defaults = {
            "retention_messages_days": str(DEFAULT_RETENTION),
            "retention_media_days": str(DEFAULT_RETENTION),
            "retention_tickets_days": str(DEFAULT_RETENTION),
        }
        for key, value in defaults.items():
            db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", (key, value))


def _setting_int(store, key: str, default: int = DEFAULT_RETENTION, lo: int = 1, hi: int = 3650) -> int:
    try:
        value = int(store.get_setting(key, str(default)) or default)
    except (ValueError, TypeError):
        value = default
    return max(lo, min(hi, value))


def retention_settings(store) -> dict[str, int]:
    return {
        "messages": _setting_int(store, "retention_messages_days"),
        "media": _setting_int(store, "retention_media_days"),
        "tickets": _setting_int(store, "retention_tickets_days"),
    }


def save_retention_settings(store, messages: int, media: int, tickets: int) -> dict[str, int]:
    vals = {
        "messages": max(1, min(3650, int(messages))),
        "media": max(1, min(3650, int(media))),
        "tickets": max(1, min(3650, int(tickets))),
    }
    store.set_setting("retention_messages_days", str(vals["messages"]))
    store.set_setting("retention_media_days", str(vals["media"]))
    store.set_setting("retention_tickets_days", str(vals["tickets"]))
    return vals


def _kind(mime: str, name: str = "") -> str:
    mime = clean(mime, 200).lower()
    name = clean(name, 300).lower()
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "voice"
    if any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "photo"
    if any(name.endswith(ext) for ext in (".mp4", ".mov", ".avi", ".mkv", ".webm")):
        return "video"
    if any(name.endswith(ext) for ext in (".ogg", ".opus", ".mp3", ".wav", ".m4a")):
        return "voice"
    return "file"


def media_stats(store) -> dict[str, dict[str, int]]:
    stats = {k: {"count": 0, "bytes": 0} for k in ("photo", "video", "voice", "file")}
    with store.connection() as db:
        rows = db.execute("SELECT media_path,media_mime,media_name FROM whatsapp_chat_messages WHERE media_path<>''").fetchall()
    seen: set[str] = set()
    for row in rows:
        path = clean(row["media_path"], 1000)
        if not path or path in seen:
            continue
        seen.add(path)
        kind = _kind(row["media_mime"], row["media_name"])
        try:
            size = int(Path(path).stat().st_size)
        except OSError:
            size = 0
        stats[kind]["count"] += 1
        stats[kind]["bytes"] += max(0, size)
    return stats


def manual_media_cleanup(store, kind: str, min_bytes: int = 0, older_days: int = 0) -> dict[str, int]:
    kind = clean(kind, 20).lower()
    if kind not in {"photo", "video", "voice", "file", "all"}:
        raise ValueError("Неизвестный тип медиа")
    min_bytes = max(0, int(min_bytes or 0))
    older_days = max(0, min(3650, int(older_days or 0)))
    cutoff_epoch = int((datetime.now(timezone.utc) - timedelta(days=older_days)).timestamp()) if older_days else 0
    removed_files = 0
    released = 0
    cleared_rows = 0
    with store.connection() as db:
        rows = db.execute(
            """SELECT id,media_path,media_mime,media_name,message_timestamp,ticket_id
               FROM whatsapp_chat_messages WHERE media_path<>'' ORDER BY id"""
        ).fetchall()
        selected: list[tuple[int, str, int]] = []
        for row in rows:
            if int(row["ticket_id"] or 0):
                active = db.execute("SELECT 1 FROM tickets WHERE id=? AND status NOT IN ('done','invalid')", (int(row["ticket_id"]),)).fetchone()
                if active:
                    continue
            if kind != "all" and _kind(row["media_mime"], row["media_name"]) != kind:
                continue
            if cutoff_epoch and int(row["message_timestamp"] or 0) >= cutoff_epoch:
                continue
            path = clean(row["media_path"], 1000)
            # Never delete a physical file while another message that should be kept
            # still points to the same path (can happen after message reconciliation).
            protected = db.execute(
                """SELECT 1 FROM whatsapp_chat_messages m
                   WHERE m.media_path=? AND m.id<>? AND m.ticket_id IN
                     (SELECT id FROM tickets WHERE status NOT IN ('done','invalid')) LIMIT 1""",
                (path, int(row["id"])),
            ).fetchone()
            if protected:
                continue
            try:
                size = int(Path(path).stat().st_size)
            except OSError:
                size = 0
            if size < min_bytes:
                continue
            selected.append((int(row["id"]), path, size))
        if selected:
            db.executemany(
                "UPDATE whatsapp_chat_messages SET media_path='',media_mime='',media_name='' WHERE id=?",
                [(row_id,) for row_id, _, _ in selected],
            )
            cleared_rows = len(selected)
    for _, path, size in selected:
        try:
            p = Path(path)
            if p.is_file():
                p.unlink()
                removed_files += 1
                released += size
        except OSError:
            pass
    return {"rows": cleared_rows, "files": removed_files, "bytes": released}


def cleanup_retention(store) -> dict[str, Any]:
    settings = retention_settings(store)
    now = datetime.now(timezone.utc)
    message_cutoff = int((now - timedelta(days=settings["messages"])).timestamp())
    media_cutoff = int((now - timedelta(days=settings["media"])).timestamp())
    ticket_cutoff = (now - timedelta(days=settings["tickets"])).replace(microsecond=0).isoformat()
    media_paths: list[str] = []
    media_rows = messages = tickets = 0
    with store.connection() as db:
        raw_media_candidates = db.execute(
            """SELECT id,media_path FROM whatsapp_chat_messages
               WHERE media_path<>'' AND message_timestamp>0 AND message_timestamp<?
                 AND ticket_id NOT IN (SELECT id FROM tickets WHERE status NOT IN ('done','invalid'))""",
            (media_cutoff,),
        ).fetchall()
        media_candidates = []
        for x in raw_media_candidates:
            path = clean(x["media_path"], 1000)
            # A reconciled/local-echo message can share one file with a newer row.
            # Keep the file if any reference is newer or belongs to an active ticket.
            keep = db.execute(
                """SELECT 1 FROM whatsapp_chat_messages m WHERE m.media_path=? AND
                   (m.message_timestamp>=? OR m.ticket_id IN (SELECT id FROM tickets WHERE status NOT IN ('done','invalid'))) LIMIT 1""",
                (path, media_cutoff),
            ).fetchone()
            if not keep:
                media_candidates.append(x)
        media_paths = list(dict.fromkeys(clean(x["media_path"], 1000) for x in media_candidates if x["media_path"]))
        if media_candidates:
            db.executemany(
                "UPDATE whatsapp_chat_messages SET media_path='',media_mime='',media_name='' WHERE id=?",
                [(int(x["id"]),) for x in media_candidates],
            )
            media_rows = len(media_candidates)
        cur = db.execute(
            """DELETE FROM whatsapp_chat_messages
               WHERE message_timestamp>0 AND message_timestamp<?
                 AND ticket_id NOT IN (SELECT id FROM tickets WHERE status NOT IN ('done','invalid'))""",
            (message_cutoff,),
        )
        messages = max(0, int(cur.rowcount or 0))
        old = db.execute("SELECT id FROM tickets WHERE status IN ('done','invalid') AND updated_at<?", (ticket_cutoff,)).fetchall()
        ids = [int(x["id"]) for x in old]
        for ticket_id in ids:
            db.execute("DELETE FROM events WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM workflow_internal_notes WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM workflow_ticket_close_meta WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM workflow_ticket_relations WHERE source_ticket_id=? OR target_ticket_id=?", (ticket_id, ticket_id))
            db.execute("UPDATE whatsapp_chat_messages SET ticket_id=0 WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM outbound_messages WHERE ticket_id=?", (ticket_id,))
            db.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
        tickets = len(ids)
        # Auxiliary operational tables follow the longest text/ticket window.
        aux_cutoff = (now - timedelta(days=max(settings["messages"], settings["tickets"]))).replace(microsecond=0).isoformat()
        for table, col in (("inbound_messages", "received_at"), ("template_errors", "created_at"), ("whatsapp_actions", "created_at"), ("outbound_messages", "created_at")):
            try:
                db.execute(f"DELETE FROM {table} WHERE {col}<?", (aux_cutoff,))
            except sqlite3.OperationalError:
                pass
        # Rebuild chat previews from the newest visible message.
        for chat in db.execute("SELECT chat_id FROM whatsapp_chats").fetchall():
            cid = clean(chat["chat_id"], 120)
            latest = db.execute(
                """SELECT body,media_name,message_timestamp FROM whatsapp_chat_messages
                   WHERE chat_id=? AND deleted=0 ORDER BY message_timestamp DESC,id DESC LIMIT 1""",
                (cid,),
            ).fetchone()
            if latest:
                preview = clean(latest["body"] or latest["media_name"] or "[Вложение]", 160)
                db.execute("UPDATE whatsapp_chats SET last_message=?,last_timestamp=?,updated_at=? WHERE chat_id=?", (preview, int(latest["message_timestamp"] or 0), utc_now(), cid))
            else:
                db.execute("UPDATE whatsapp_chats SET last_message='',last_timestamp=0,unread_count=0,updated_at=? WHERE chat_id=?", (utc_now(), cid))
        db.execute("PRAGMA optimize")
    return {"settings": settings, "media_paths": media_paths, "media_rows": media_rows, "messages": messages, "tickets": tickets}


def record_recent(store, chat_id: str, actor: str) -> None:
    chat_id = clean(chat_id, 120)
    actor = clean(actor, 120)
    if not chat_id:
        return
    with store.connection() as db:
        db.execute(
            """INSERT INTO workflow_recent_chats(actor,chat_id,opened_at) VALUES(?,?,?)
               ON CONFLICT(actor,chat_id) DO UPDATE SET opened_at=excluded.opened_at""",
            (actor, chat_id, utc_now()),
        )
        # Keep only the newest 30 per employee.
        db.execute(
            """DELETE FROM workflow_recent_chats WHERE actor=? AND chat_id NOT IN (
                 SELECT chat_id FROM workflow_recent_chats WHERE actor=? ORDER BY opened_at DESC LIMIT 30
               )""",
            (actor, actor),
        )


def recent_chats(store, actor: str, limit: int = 20) -> list[dict[str, Any]]:
    actor = clean(actor, 120)
    with store.connection() as db:
        rows = db.execute(
            """SELECT r.chat_id,r.opened_at,
                      COALESCE(NULLIF(c.name,''),NULLIF(g.name,''),NULLIF(p.name,''),r.chat_id) AS name,
                      COALESCE(c.last_message,'') AS last_message,
                      COALESCE(c.unread_count,0) AS unread_count
               FROM workflow_recent_chats r
               LEFT JOIN whatsapp_chats c ON c.chat_id=r.chat_id
               LEFT JOIN whatsapp_groups g ON g.chat_id=r.chat_id
               LEFT JOIN whatsapp_contacts p ON p.chat_id=r.chat_id
               WHERE r.actor=? ORDER BY r.opened_at DESC LIMIT ?""",
            (actor, max(1, min(100, int(limit)))),
        ).fetchall()
    return [dict(x) for x in rows]


def unread_counts(store) -> dict[str, int]:
    """Unread badges for staff navigation.

    WhatsApp can discover many unrelated groups. Only groups explicitly enabled
    by an administrator are allowed to contribute to the group badge.
    """
    with store.connection() as db:
        personal = db.execute(
            "SELECT COALESCE(SUM(unread_count),0) n FROM whatsapp_chats WHERE chat_id NOT LIKE '%@g.us'"
        ).fetchone()["n"]
        groups = db.execute(
            """SELECT COALESCE(SUM(c.unread_count),0) n
               FROM whatsapp_chats AS c
               INNER JOIN whatsapp_groups AS g ON g.chat_id=c.chat_id
               WHERE g.added_by_admin=1"""
        ).fetchone()["n"]
    return {"personal": int(personal or 0), "groups": int(groups or 0)}


def next_unread_chat(store, group: bool | None = None) -> str:
    """Return next unread chat without surfacing an unconfigured group."""
    with store.connection() as db:
        if group is True:
            row = db.execute(
                """SELECT c.chat_id
                   FROM whatsapp_chats AS c
                   INNER JOIN whatsapp_groups AS g ON g.chat_id=c.chat_id
                   WHERE c.unread_count>0 AND g.added_by_admin=1
                   ORDER BY c.last_timestamp DESC LIMIT 1"""
            ).fetchone()
        elif group is False:
            row = db.execute(
                """SELECT chat_id FROM whatsapp_chats
                   WHERE unread_count>0 AND chat_id NOT LIKE '%@g.us'
                   ORDER BY last_timestamp DESC LIMIT 1"""
            ).fetchone()
        else:
            row = db.execute(
                """SELECT c.chat_id
                   FROM whatsapp_chats AS c
                   LEFT JOIN whatsapp_groups AS g ON g.chat_id=c.chat_id
                   WHERE c.unread_count>0
                     AND (c.chat_id NOT LIKE '%@g.us' OR g.added_by_admin=1)
                   ORDER BY c.last_timestamp DESC LIMIT 1"""
            ).fetchone()
    return clean(row["chat_id"], 120) if row else ""


def presence_update(store, chat_id: str, client_id: str, actor: str, state: str) -> None:
    chat_id = clean(chat_id, 120)
    client_id = clean(client_id, 120)
    actor = clean(actor, 120) or "Сотрудник"
    state = clean(state, 20).lower()
    if state not in {"viewing", "typing", "idle", "closed"}:
        state = "viewing"
    if not chat_id or not client_id:
        return
    now = utc_now()
    with store.connection() as db:
        if state == "closed":
            db.execute("DELETE FROM workflow_chat_presence WHERE chat_id=? AND client_id=?", (chat_id, client_id))
        else:
            db.execute(
                """INSERT INTO workflow_chat_presence(chat_id,client_id,actor,state,updated_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(chat_id,client_id) DO UPDATE SET actor=excluded.actor,state=excluded.state,updated_at=excluded.updated_at""",
                (chat_id, client_id, actor, state, now),
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=45)).replace(microsecond=0).isoformat()
        db.execute("DELETE FROM workflow_chat_presence WHERE updated_at<?", (cutoff,))


def presence_state(store, chat_id: str, client_id: str = "") -> list[dict[str, Any]]:
    chat_id = clean(chat_id, 120)
    client_id = clean(client_id, 120)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=45)).replace(microsecond=0).isoformat()
    with store.connection() as db:
        db.execute("DELETE FROM workflow_chat_presence WHERE updated_at<?", (cutoff,))
        rows = db.execute(
            "SELECT client_id,actor,state,updated_at FROM workflow_chat_presence WHERE chat_id=? AND client_id<>? ORDER BY state='typing' DESC,updated_at DESC",
            (chat_id, client_id),
        ).fetchall()
    return [dict(x) for x in rows]


def open_tickets_for_chat(store, chat_id: str, include_closed: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    chat_id = clean(chat_id, 120)
    if not chat_id:
        return []
    canonical = store.canonical_whatsapp_chat_id(chat_id)
    with store.connection() as db:
        condition = "" if include_closed else " AND status NOT IN ('done','invalid')"
        rows = db.execute(
            f"SELECT * FROM tickets WHERE chat_id=?{condition} ORDER BY updated_at DESC,id DESC LIMIT ?",
            (canonical, max(1, min(200, int(limit)))),
        ).fetchall()
    return [dict(x) for x in rows]


def auto_link_open_ticket(store, chat_id: str, messages: list[dict[str, Any]]) -> int:
    tickets = open_tickets_for_chat(store, chat_id, False, 3)
    if len(tickets) != 1:
        return 0
    ticket_id = int(tickets[0]["id"])
    linked = 0
    for message in messages:
        if bool(message.get("from_me")):
            continue
        key = clean(message.get("id"), 180)
        if not key:
            continue
        current = store.get_whatsapp_message(chat_id, key)
        if current and int(current.get("ticket_id", 0) or 0) == 0:
            if store.link_whatsapp_message_to_ticket(chat_id, key, ticket_id):
                linked += 1
    if linked:
        store.add_audit("ticket_link", "Система", f"Автопривязано сообщений: {linked}", "ticket", ticket_id, f"chat={chat_id}")
    return linked


def chat_history(store, chat_id: str, limit: int = 100) -> list[dict[str, Any]]:
    chat_id = store.canonical_whatsapp_chat_id(clean(chat_id, 120))
    with store.connection() as db:
        actions = db.execute(
            """SELECT created_at,actor,action,details,event_type FROM audit_log
               WHERE (object_type='chat' AND object_id=?) OR
                     (object_type='ticket' AND object_id IN (SELECT CAST(id AS TEXT) FROM tickets WHERE chat_id=?))
               ORDER BY id DESC LIMIT ?""",
            (chat_id, chat_id, max(1, min(300, int(limit)))),
        ).fetchall()
    return [dict(x) for x in actions]


def internal_notes(store, ticket_id: int) -> list[dict[str, Any]]:
    with store.connection() as db:
        rows = db.execute("SELECT * FROM workflow_internal_notes WHERE ticket_id=? ORDER BY id DESC LIMIT 200", (int(ticket_id),)).fetchall()
    return [dict(x) for x in rows]


def add_internal_note(store, ticket_id: int, actor: str, note: str) -> int:
    note = clean(note, 5000)
    if not note:
        raise ValueError("Заметка пустая")
    if not store.get_ticket(int(ticket_id)):
        raise ValueError("Заявка не найдена")
    now = utc_now()
    with store.connection() as db:
        cur = db.execute("INSERT INTO workflow_internal_notes(ticket_id,actor,note,created_at) VALUES(?,?,?,?)", (int(ticket_id), clean(actor, 120), note, now))
        note_id = int(cur.lastrowid)
        db.execute("INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)", (int(ticket_id), "Добавлена внутренняя заметка", clean(actor, 120), now))
    return note_id


def transfer_ticket(store, ticket_id: int, employee: str, reason: str, actor: str) -> bool:
    ticket = store.get_ticket(int(ticket_id))
    if not ticket:
        return False
    employee = clean(employee, 120)
    reason = clean(reason, 1200)
    if not employee:
        raise ValueError("Не выбран сотрудник")
    now = utc_now()
    with store.connection() as db:
        db.execute("UPDATE tickets SET assigned_to=?,updated_at=? WHERE id=?", (employee, now, int(ticket_id)))
        detail = f"Передано сотруднику {employee}" + (f". Причина: {reason}" if reason else "")
        db.execute("INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)", (int(ticket_id), detail, clean(actor, 120), now))
    store.add_audit("transfer", actor, f"Заявка передана: {employee}", "ticket", ticket_id, reason)
    return True


def merge_tickets(store, source_id: int, target_id: int, actor: str, note: str = "") -> bool:
    source_id, target_id = int(source_id), int(target_id)
    if source_id == target_id:
        raise ValueError("Нельзя объединить заявку саму с собой")
    source, target = store.get_ticket(source_id), store.get_ticket(target_id)
    if not source or not target:
        raise ValueError("Одна из заявок не найдена")
    now = utc_now()
    with store.connection() as db:
        db.execute("UPDATE whatsapp_chat_messages SET ticket_id=? WHERE ticket_id=?", (target_id, source_id))
        db.execute("UPDATE outbound_messages SET ticket_id=? WHERE ticket_id=?", (target_id, source_id))
        db.execute("UPDATE tickets SET status='invalid',updated_at=?,closed_at=? WHERE id=?", (now, now, source_id))
        db.execute("INSERT INTO workflow_ticket_relations(source_ticket_id,target_ticket_id,relation_type,actor,note,created_at) VALUES(?,?,?,?,?,?)", (source_id, target_id, "merge", clean(actor,120), clean(note,1200), now))
        db.execute("INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)", (source_id, f"Объединена с заявкой #{target_id}", clean(actor,120), now))
        db.execute("INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)", (target_id, f"Присоединена заявка #{source_id}", clean(actor,120), now))
    store.add_audit("merge", actor, f"Объединены заявки #{source_id} → #{target_id}", "ticket", target_id, note)
    return True


def split_ticket(store, source_id: int, actor: str, title: str, summary: str, category: str = "", priority: str = "") -> int:
    source = store.get_ticket(int(source_id))
    if not source:
        raise ValueError("Заявка не найдена")
    payload = {
        "source": source.get("source", "admin_manual"),
        "sender": source.get("sender", ""),
        "phone": source.get("phone", ""),
        "chat_id": source.get("chat_id", ""),
        "external_id": "",
        "category": clean(category, 80) or source.get("category", "general"),
        "priority": clean(priority, 40) or source.get("priority", "normal"),
        "title": clean(title, 160) or f"Часть заявки #{source_id}",
        "summary": clean(summary, 2000) or "Выделено из другой заявки",
        "original_text": clean(summary, 4000) or f"Выделено из заявки #{source_id}",
        "status": "new",
        "assigned_to": source.get("assigned_to", "") or clean(actor, 120),
    }
    new_id = int(store.create_ticket(payload))
    now = utc_now()
    with store.connection() as db:
        db.execute("INSERT INTO workflow_ticket_relations(source_ticket_id,target_ticket_id,relation_type,actor,note,created_at) VALUES(?,?,?,?,?,?)", (int(source_id), new_id, "split", clean(actor,120), clean(summary,1200), now))
        db.execute("INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)", (int(source_id), f"Из заявки выделена новая #{new_id}", clean(actor,120), now))
        db.execute("INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)", (new_id, f"Создана разделением заявки #{source_id}", clean(actor,120), now))
    return new_id


def related_tickets(store, ticket_id: int) -> list[dict[str, Any]]:
    ticket = store.get_ticket(int(ticket_id))
    if not ticket:
        return []
    chat_id = clean(ticket.get("chat_id"), 120)
    phone = clean(ticket.get("phone"), 60)
    with store.connection() as db:
        if chat_id:
            rows = db.execute("SELECT id,title,status,category,priority,created_at,updated_at FROM tickets WHERE chat_id=? AND id<>? ORDER BY id DESC LIMIT 50", (chat_id, int(ticket_id))).fetchall()
        elif phone:
            rows = db.execute("SELECT id,title,status,category,priority,created_at,updated_at FROM tickets WHERE phone=? AND id<>? ORDER BY id DESC LIMIT 50", (phone, int(ticket_id))).fetchall()
        else:
            rows = []
    return [dict(x) for x in rows]


def close_comment_required(store, category: str) -> bool:
    with store.connection() as db:
        row = db.execute("SELECT required FROM workflow_close_comment_categories WHERE category=?", (clean(category,80),)).fetchone()
    return bool(row and int(row["required"] or 0))


def set_close_comment_categories(store, categories: list[str]) -> None:
    cats = sorted({clean(x, 80) for x in categories if clean(x, 80)})
    now = utc_now()
    with store.connection() as db:
        db.execute("DELETE FROM workflow_close_comment_categories")
        db.executemany("INSERT INTO workflow_close_comment_categories(category,required,updated_at) VALUES(?,1,?)", [(x, now) for x in cats])


def required_close_categories(store) -> list[str]:
    with store.connection() as db:
        rows = db.execute("SELECT category FROM workflow_close_comment_categories WHERE required=1 ORDER BY category").fetchall()
    return [clean(x["category"], 80) for x in rows]


def validate_close(store, ticket_id: int, status: str, reason: str, comment: str) -> tuple[bool, str]:
    if status not in TERMINAL:
        return True, ""
    ticket = store.get_ticket(int(ticket_id))
    if not ticket:
        return False, "Заявка не найдена"
    reason = clean(reason, 40)
    if reason and reason not in ALLOWED_CLOSE_REASONS:
        return False, "Неизвестная причина закрытия"
    if close_comment_required(store, clean(ticket.get("category"),80)) and not clean(comment, 5000):
        return False, "Для этой категории комментарий при закрытии обязателен"
    return True, ""


def record_close_meta(store, ticket_id: int, status: str, reason: str, comment: str, actor: str) -> None:
    if status not in TERMINAL:
        return
    reason = clean(reason, 40) or ("resolved" if status == "done" else "invalid")
    if reason not in ALLOWED_CLOSE_REASONS:
        reason = "other"
    now = utc_now()
    with store.connection() as db:
        db.execute(
            """INSERT INTO workflow_ticket_close_meta(ticket_id,reason,comment,actor,closed_at) VALUES(?,?,?,?,?)
               ON CONFLICT(ticket_id) DO UPDATE SET reason=excluded.reason,comment=excluded.comment,actor=excluded.actor,closed_at=excluded.closed_at""",
            (int(ticket_id), reason, clean(comment,5000), clean(actor,120), now),
        )
        label = ALLOWED_CLOSE_REASONS.get(reason, reason)
        db.execute("INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)", (int(ticket_id), f"Причина закрытия: {label}" + (f". {clean(comment,500)}" if comment else ""), clean(actor,120), now))


def close_meta(store, ticket_id: int) -> dict[str, Any]:
    with store.connection() as db:
        row = db.execute("SELECT * FROM workflow_ticket_close_meta WHERE ticket_id=?", (int(ticket_id),)).fetchone()
    return dict(row) if row else {}


def quick_ticket_update(store, ticket_id: int, *, status: str = "", priority: str = "", actor: str = "", employee: str = "", reason: str = "", comment: str = "") -> dict[str, Any]:
    ticket = store.get_ticket(int(ticket_id))
    if not ticket:
        raise ValueError("Заявка не найдена")
    updated = False
    if priority:
        updated = bool(store.update_priority(int(ticket_id), clean(priority,40), clean(actor,120))) or updated
    if employee and clean(employee,120) != clean(ticket.get("assigned_to"),120):
        updated = transfer_ticket(store, int(ticket_id), employee, reason, actor) or updated
    if status and status != clean(ticket.get("status"),40):
        ok, message = validate_close(store, int(ticket_id), status, reason, comment)
        if not ok:
            raise ValueError(message)
        assignee = clean(employee,120) or clean(ticket.get("assigned_to"),120) or clean(actor,120)
        if not store.update_status(int(ticket_id), status, clean(actor,120), assignee):
            raise ValueError("Не удалось изменить статус")
        record_close_meta(store, int(ticket_id), status, reason, comment, actor)
        updated = True
    return {"updated": updated, "ticket": store.get_ticket(int(ticket_id))}


def bulk_update(store, ticket_ids: list[int], actor: str, *, status: str = "", priority: str = "", employee: str = "", reason: str = "", comment: str = "") -> dict[str, int]:
    ids = sorted({int(x) for x in ticket_ids if int(x) > 0})[:200]
    updated = failed = 0
    for ticket_id in ids:
        try:
            result = quick_ticket_update(store, ticket_id, status=status, priority=priority, employee=employee, actor=actor, reason=reason, comment=comment)
            updated += int(bool(result.get("updated")))
        except Exception:
            failed += 1
    return {"selected": len(ids), "updated": updated, "failed": failed}


def conversation_summary(store, chat_id: str) -> dict[str, Any]:
    chat_id = store.canonical_whatsapp_chat_id(clean(chat_id,120))
    with store.connection() as db:
        rows = db.execute(
            """SELECT from_me,body,transcript,media_name,message_timestamp,deleted FROM whatsapp_chat_messages
               WHERE chat_id=? ORDER BY message_timestamp DESC,id DESC LIMIT 100""",
            (chat_id,),
        ).fetchall()
    messages = [dict(x) for x in reversed(rows) if not int(x["deleted"] or 0)]
    def text_of(x: dict[str,Any]) -> str:
        return clean(x.get("body") or x.get("transcript") or x.get("media_name") or "", 500)
    incoming = [text_of(x) for x in messages if not int(x.get("from_me") or 0) and text_of(x)]
    outgoing = [text_of(x) for x in messages if int(x.get("from_me") or 0) and text_of(x)]
    latest = messages[-1] if messages else {}
    waiting = "Ответ пользователя" if latest and int(latest.get("from_me") or 0) else "Ответ сотрудника" if latest else "Нет сообщений"
    tickets = open_tickets_for_chat(store, chat_id, False, 10)
    return {
        "message_count": len(messages),
        "essence": (incoming[0] if incoming else (outgoing[0] if outgoing else "Нет текстовых сообщений"))[:360],
        "last_user": (incoming[-1] if incoming else "")[:360],
        "last_staff": (outgoing[-1] if outgoing else "")[:360],
        "waiting": waiting,
        "open_tickets": [{"id":int(t["id"]),"title":clean(t["title"],160),"status":clean(t["status"],40)} for t in tickets],
    }


def handover_summary(store, actor: str) -> dict[str, Any]:
    base = store.shift_summary(clean(actor,120))
    with store.connection() as db:
        rows = db.execute(
            """SELECT id,title,status,priority,assigned_to,updated_at FROM tickets
               WHERE status NOT IN ('done','invalid') ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,updated_at ASC LIMIT 80"""
        ).fetchall()
    return {"base": base, "tickets": [dict(x) for x in rows]}


def export_messages(store, chat_id: str) -> tuple[str, list[dict[str,Any]]]:
    chat_id = store.canonical_whatsapp_chat_id(clean(chat_id,120))
    with store.connection() as db:
        name_row = db.execute(
            """SELECT COALESCE(NULLIF(c.name,''),NULLIF(g.name,''),NULLIF(p.name,''),?) name
               FROM (SELECT 1) x LEFT JOIN whatsapp_chats c ON c.chat_id=? LEFT JOIN whatsapp_groups g ON g.chat_id=? LEFT JOIN whatsapp_contacts p ON p.chat_id=?""",
            (chat_id, chat_id, chat_id, chat_id),
        ).fetchone()
        rows = db.execute(
            """SELECT * FROM whatsapp_chat_messages WHERE chat_id=? ORDER BY message_timestamp,id""",
            (chat_id,),
        ).fetchall()
    return clean((name_row or {"name": chat_id})["name"],160) or chat_id, [dict(x) for x in rows]


def export_txt(store, chat_id: str) -> tuple[str, bytes]:
    name, rows = export_messages(store, chat_id)
    lines = [f"Единая очередь - история чата: {name}", f"Chat ID: {chat_id}", ""]
    for row in rows:
        ts = int(row.get("message_timestamp",0) or 0)
        when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M:%S") if ts else ""
        who = "Вы" if int(row.get("from_me",0) or 0) else clean(row.get("sender"),120) or "Пользователь"
        flags = []
        if int(row.get("forwarded",0) or 0): flags.append("переслано")
        if int(row.get("edited",0) or 0): flags.append("изменено")
        if int(row.get("deleted",0) or 0): flags.append("удалено")
        text = clean(row.get("body") or row.get("transcript") or row.get("media_name") or "[Вложение]", 32000)
        suffix = f" [{' / '.join(flags)}]" if flags else ""
        lines.append(f"[{when}] {who}{suffix}: {text}")
    return name, ("\n".join(lines) + "\n").encode("utf-8")


def export_zip(store, chat_id: str) -> tuple[str, bytes]:
    name, txt = export_txt(store, chat_id)
    _, rows = export_messages(store, chat_id)
    out = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("history.txt", txt)
        for idx, row in enumerate(rows, 1):
            path = clean(row.get("media_path"), 1000)
            if not path or not Path(path).is_file():
                continue
            raw_name = clean(row.get("media_name"), 200) or Path(path).name or f"media-{idx}"
            safe = re.sub(r"[^\w.()\- а-яА-ЯёЁ]+", "_", raw_name, flags=re.UNICODE).strip("._ ") or f"media-{idx}"
            candidate = safe
            n = 2
            while candidate in used:
                stem, suffix = os.path.splitext(safe)
                candidate = f"{stem}-{n}{suffix}"
                n += 1
            used.add(candidate)
            try:
                zf.write(path, f"attachments/{candidate}")
            except OSError:
                pass
    return name, out.getvalue()


def _pdf_unicode_font() -> Path | None:
    for candidate in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _ttf_cmap_for_chars(font_bytes: bytes, chars: str) -> dict[int, int]:
    """Read only the TTF cmap data needed for our PDF fallback."""
    import struct
    if len(font_bytes) < 12:
        return {}
    num_tables = struct.unpack(">H", font_bytes[4:6])[0]
    tables: dict[str, tuple[int, int]] = {}
    offset = 12
    for _ in range(num_tables):
        if offset + 16 > len(font_bytes):
            break
        tag = font_bytes[offset:offset + 4].decode("latin1", "ignore")
        _checksum, table_offset, length = struct.unpack(">III", font_bytes[offset + 4:offset + 16])
        tables[tag] = (table_offset, length)
        offset += 16
    if "cmap" not in tables:
        return {}
    cmap_offset, cmap_length = tables["cmap"]
    data = font_bytes[cmap_offset:cmap_offset + cmap_length]
    if len(data) < 4:
        return {}
    _version, count = struct.unpack(">HH", data[:4])
    records: list[tuple[int, int, int, int]] = []
    for index in range(count):
        pos = 4 + index * 8
        if pos + 8 > len(data):
            break
        platform, encoding, sub_offset = struct.unpack(">HHI", data[pos:pos + 8])
        if sub_offset + 2 <= len(data):
            fmt = struct.unpack(">H", data[sub_offset:sub_offset + 2])[0]
            if fmt in {4, 12}:
                records.append((fmt, platform, encoding, sub_offset))
    records.sort(
        key=lambda item: (
            0 if item[0] == 12 else 1,
            0 if item[1] == 3 else 1 if item[1] == 0 else 2,
            item[2],
        )
    )
    wanted = {ord(ch) for ch in chars}
    result: dict[int, int] = {}
    for fmt, _platform, _encoding, sub_offset in records:
        if fmt == 12 and sub_offset + 16 <= len(data):
            _fmt, _reserved, length, _language, groups_count = struct.unpack(
                ">HHIII", data[sub_offset:sub_offset + 16]
            )
            if sub_offset + length > len(data):
                continue
            groups: list[tuple[int, int, int]] = []
            pos = sub_offset + 16
            for _ in range(groups_count):
                if pos + 12 > sub_offset + length:
                    break
                start, finish, start_gid = struct.unpack(">III", data[pos:pos + 12])
                groups.append((start, finish, start_gid))
                pos += 12
            for codepoint in wanted - result.keys():
                lo, hi = 0, len(groups) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    start, finish, start_gid = groups[mid]
                    if codepoint < start:
                        hi = mid - 1
                    elif codepoint > finish:
                        lo = mid + 1
                    else:
                        result[codepoint] = start_gid + (codepoint - start)
                        break
        elif fmt == 4 and sub_offset + 14 <= len(data):
            _fmt, length, _language, seg_count_x2 = struct.unpack(
                ">HHHH", data[sub_offset:sub_offset + 8]
            )
            if sub_offset + length > len(data):
                continue
            seg_count = seg_count_x2 // 2
            end_offset = sub_offset + 14
            end_codes = struct.unpack(
                ">" + "H" * seg_count,
                data[end_offset:end_offset + 2 * seg_count],
            )
            start_offset = end_offset + 2 * seg_count + 2
            start_codes = struct.unpack(
                ">" + "H" * seg_count,
                data[start_offset:start_offset + 2 * seg_count],
            )
            delta_offset = start_offset + 2 * seg_count
            deltas = struct.unpack(
                ">" + "h" * seg_count,
                data[delta_offset:delta_offset + 2 * seg_count],
            )
            range_offset_pos = delta_offset + 2 * seg_count
            range_offsets = struct.unpack(
                ">" + "H" * seg_count,
                data[range_offset_pos:range_offset_pos + 2 * seg_count],
            )
            for codepoint in wanted - result.keys():
                if codepoint > 0xFFFF:
                    continue
                for idx, (start, finish) in enumerate(zip(start_codes, end_codes)):
                    if start <= codepoint <= finish:
                        range_offset = range_offsets[idx]
                        if range_offset == 0:
                            gid = (codepoint + deltas[idx]) & 0xFFFF
                        else:
                            glyph_pos = (
                                range_offset_pos
                                + 2 * idx
                                + range_offset
                                + 2 * (codepoint - start)
                            )
                            if glyph_pos + 2 > sub_offset + length:
                                gid = 0
                            else:
                                glyph = struct.unpack(
                                    ">H", data[glyph_pos:glyph_pos + 2]
                                )[0]
                                gid = ((glyph + deltas[idx]) & 0xFFFF) if glyph else 0
                        if gid:
                            result[codepoint] = gid
                        break
        if wanted.issubset(result.keys()):
            break
    return result


def _export_pdf_native(name: str, rows: list[dict[str, Any]]) -> bytes:
    """Self-contained Unicode PDF writer using an Ubuntu system TTF font."""
    import struct
    import textwrap

    font_path = _pdf_unicode_font()
    if not font_path:
        raise RuntimeError("PDF-экспорт: на сервере не найден Unicode-шрифт")
    font_bytes = font_path.read_bytes()

    raw_lines: list[str] = []
    for row in rows:
        ts = int(row.get("message_timestamp", 0) or 0)
        when = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone()
            .strftime("%d.%m.%Y %H:%M:%S")
            if ts
            else ""
        )
        who = (
            "Вы"
            if int(row.get("from_me", 0) or 0)
            else clean(row.get("sender"), 120) or "Пользователь"
        )
        flags: list[str] = []
        if int(row.get("forwarded", 0) or 0):
            flags.append("переслано")
        if int(row.get("edited", 0) or 0):
            flags.append("изменено")
        if int(row.get("deleted", 0) or 0):
            flags.append("удалено")
        text = clean(
            row.get("body")
            or row.get("transcript")
            or row.get("media_name")
            or "[Вложение]",
            32000,
        )
        prefix = " · ".join(
            part for part in (when, who, ", ".join(flags)) if part
        )
        logical = f"[{prefix}] {text}" if prefix else text
        for chunk in logical.splitlines() or [""]:
            raw_lines.extend(
                textwrap.wrap(
                    chunk,
                    width=92,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or [""]
            )
        raw_lines.append("")

    title = f"Единая очередь · история чата: {name}"
    cmap = _ttf_cmap_for_chars(font_bytes, title + "".join(raw_lines) + "?")
    fallback_gid = cmap.get(ord("?"), 0)
    used: dict[int, int] = {}

    def encode_line(value: str) -> str:
        payload = bytearray()
        for char in value:
            codepoint = ord(char)
            gid = cmap.get(codepoint, fallback_gid)
            if gid:
                used[gid] = codepoint
            payload.extend(struct.pack(">H", gid))
        return payload.hex().upper()

    title_hex = encode_line(title)
    per_page = 57
    pages = [
        raw_lines[index:index + per_page]
        for index in range(0, len(raw_lines), per_page)
    ] or [[]]
    encoded_pages = [[encode_line(line) for line in page] for page in pages]

    mappings: list[tuple[str, str]] = []
    for gid, codepoint in sorted(used.items()):
        src = f"<{gid:04X}>"
        if codepoint <= 0xFFFF:
            dst = f"<{codepoint:04X}>"
        else:
            value = codepoint - 0x10000
            high = 0xD800 + (value >> 10)
            low = 0xDC00 + (value & 0x3FF)
            dst = f"<{high:04X}{low:04X}>"
        mappings.append((src, dst))

    cmap_lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /QueueToUnicode def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for index in range(0, len(mappings), 100):
        chunk = mappings[index:index + 100]
        cmap_lines.append(f"{len(chunk)} beginbfchar")
        cmap_lines.extend(f"{src} {dst}" for src, dst in chunk)
        cmap_lines.append("endbfchar")
    cmap_lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
        ]
    )
    to_unicode = ("\n".join(cmap_lines) + "\n").encode("ascii")

    objects: list[bytes] = []

    def add_object(content: bytes) -> int:
        objects.append(content)
        return len(objects)

    catalog_id = add_object(b"")
    pages_id = add_object(b"")
    font_file_id = add_object(
        f"<< /Length {len(font_bytes)} /Length1 {len(font_bytes)} >>\nstream\n".encode()
        + font_bytes
        + b"\nendstream"
    )
    descriptor_id = add_object(
        (
            "<< /Type /FontDescriptor /FontName /QueueUnicode /Flags 32 "
            "/FontBBox [-1021 -463 1794 1232] /ItalicAngle 0 "
            "/Ascent 928 /Descent -236 /CapHeight 729 /StemV 80 "
            f"/FontFile2 {font_file_id} 0 R >>"
        ).encode()
    )
    cid_font_id = add_object(
        (
            "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /QueueUnicode "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/FontDescriptor {descriptor_id} 0 R /DW 600 /CIDToGIDMap /Identity >>"
        ).encode()
    )
    unicode_id = add_object(
        f"<< /Length {len(to_unicode)} >>\nstream\n".encode()
        + to_unicode
        + b"endstream"
    )
    type0_id = add_object(
        (
            "<< /Type /Font /Subtype /Type0 /BaseFont /QueueUnicode "
            "/Encoding /Identity-H "
            f"/DescendantFonts [{cid_font_id} 0 R] /ToUnicode {unicode_id} 0 R >>"
        ).encode()
    )

    page_ids: list[int] = []
    for lines in encoded_pages:
        commands = [
            "BT",
            "/F1 13 Tf",
            "38 806 Td",
            f"<{title_hex}> Tj",
            "0 -24 Td",
            "/F1 9 Tf",
            "12 TL",
        ]
        for line_hex in lines:
            commands.extend([f"<{line_hex}> Tj", "T*"])
        commands.append("ET")
        stream = ("\n".join(commands) + "\n").encode("ascii")
        content_id = add_object(
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"endstream"
        )
        page_id = add_object(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {type0_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        page_ids.append(page_id)

    objects[catalog_id - 1] = (
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    )
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] "
        f"/Count {len(page_ids)} >>"
    ).encode()

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def export_pdf(store, chat_id: str) -> tuple[str, bytes]:
    name, rows = export_messages(store, chat_id)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    except Exception:
        return name, _export_pdf_native(name, rows)

    font_name = "Helvetica"
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).is_file():
            try:
                pdfmetrics.registerFont(TTFont("QueueUnicode", path))
                font_name = "QueueUnicode"
                break
            except Exception:
                pass

    if font_name == "Helvetica" and any(ord(ch) > 255 for ch in name):
        return name, _export_pdf_native(name, rows)

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        rightMargin=34,
        leftMargin=34,
        topMargin=34,
        bottomMargin=34,
        title=f"История чата {name}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "QueueTitle",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=16,
        leading=20,
    )
    body_style = ParagraphStyle(
        "QueueBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        alignment=TA_LEFT,
    )
    story = [
        Paragraph(
            f"Единая очередь - история чата: {html_escape(name)}",
            title_style,
        ),
        Spacer(1, 12),
    ]
    for row in rows:
        ts = int(row.get("message_timestamp", 0) or 0)
        when = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone()
            .strftime("%d.%m.%Y %H:%M:%S")
            if ts
            else ""
        )
        who = (
            "Вы"
            if int(row.get("from_me", 0) or 0)
            else clean(row.get("sender"), 120) or "Пользователь"
        )
        text = clean(
            row.get("body")
            or row.get("transcript")
            or row.get("media_name")
            or "[Вложение]",
            32000,
        )
        story.append(
            Paragraph(
                f"<b>{html_escape(when)} · {html_escape(who)}</b><br/>"
                f"{html_escape(text).replace(chr(10), '<br/>')}",
                body_style,
            )
        )
        story.append(Spacer(1, 7))
    doc.build(story)
    return name, out.getvalue()


def html_escape(value: object) -> str:
    return clean(value, 50000).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def chat_conflict_state(store, chat_id: str) -> dict[str, Any]:
    chat_id = store.canonical_whatsapp_chat_id(clean(chat_id,120))
    with store.connection() as db:
        latest = db.execute(
            "SELECT message_key,message_timestamp,from_me FROM whatsapp_chat_messages WHERE chat_id=? ORDER BY message_timestamp DESC,id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        outbound = db.execute(
            "SELECT id,actor,created_at,sent_at,status FROM outbound_messages WHERE chat_id=? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return {
        "latest_message_key": clean(latest["message_key"],180) if latest else "",
        "latest_timestamp": int(latest["message_timestamp"] or 0) if latest else 0,
        "latest_from_me": bool(int(latest["from_me"] or 0)) if latest else False,
        "last_outbound": dict(outbound) if outbound else {},
    }
