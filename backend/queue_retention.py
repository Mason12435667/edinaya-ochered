"""Срок хранения сообщений, вложений и заявок: настройки, размер файлов по видам и очистка."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from queue_workflow import clean, DEFAULT_RETENTION, utc_now


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
