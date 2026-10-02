"""Recover a missing WhatsApp auto-reply after an unanswered inbound message.

The worker is deliberately conservative. It only acts when the latest visible
message in a direct chat is an inbound user message that has remained unanswered
for the configured grace period. Staff/manual chats, excluded contacts, groups,
messages already linked to a ticket, and chats with a pending specialist flow are
left untouched.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any

DEFAULT_MINUTES = 30
DEFAULT_SCAN_SECONDS = 60
DEFAULT_MAX_HOURS = 6


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def settings() -> tuple[int, int, int]:
    minutes = _int_env("QUEUE_STALE_MENU_MINUTES", DEFAULT_MINUTES, 5, 240)
    scan_seconds = _int_env("QUEUE_STALE_MENU_SCAN_SECONDS", DEFAULT_SCAN_SECONDS, 15, 600)
    max_hours = _int_env("QUEUE_STALE_MENU_MAX_HOURS", DEFAULT_MAX_HOURS, 1, 72)
    return minutes, scan_seconds, max_hours


def _iso_from_epoch(value: int) -> str:
    return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(microsecond=0).isoformat()


def _candidate_rows(store: Any, now_epoch: int, delay_seconds: int, max_age_seconds: int) -> list[dict[str, Any]]:
    newest_allowed = max(0, int(now_epoch) - int(delay_seconds))
    oldest_allowed = max(0, int(now_epoch) - int(max_age_seconds))
    with store.connection() as db:
        rows = db.execute(
            """
            SELECT m.id, m.message_key, m.chat_id, m.body, m.message_type,
                   m.message_timestamp, m.ticket_id, m.sender_phone
              FROM whatsapp_chat_messages AS m
             WHERE m.from_me = 0
               AND m.deleted = 0
               AND m.message_timestamp BETWEEN ? AND ?
               AND m.chat_id <> ''
               AND m.chat_id NOT LIKE '%@g.us'
               AND m.ticket_id = 0
               AND NOT EXISTS (
                    SELECT 1
                      FROM whatsapp_chat_messages AS newer
                     WHERE newer.chat_id = m.chat_id
                       AND newer.deleted = 0
                       AND (
                            newer.message_timestamp > m.message_timestamp
                            OR (newer.message_timestamp = m.message_timestamp AND newer.id > m.id)
                       )
               )
             ORDER BY m.message_timestamp ASC, m.id ASC
             LIMIT 200
            """,
            (oldest_allowed, newest_allowed),
        ).fetchall()
    return [dict(row) for row in rows]


def _has_queued_reply_after(store: Any, chat_id: str, message_timestamp: int) -> bool:
    since = _iso_from_epoch(message_timestamp)
    with store.connection() as db:
        row = db.execute(
            """
            SELECT 1
              FROM outbound_messages
             WHERE chat_id = ?
               AND created_at >= ?
               AND status NOT IN ('failed','cancelled')
             LIMIT 1
            """,
            (chat_id, since),
        ).fetchone()
    return bool(row)


def _request_id(chat_id: str, message_key: str) -> str:
    digest = hashlib.sha256(f"{chat_id}|{message_key}".encode("utf-8", "ignore")).hexdigest()[:32]
    return f"stale-menu:{digest}"



def _request_already_queued(store: Any, request_id: str) -> bool:
    with store.connection() as db:
        row = db.execute(
            "SELECT 1 FROM outbound_requests WHERE request_id = ? LIMIT 1",
            (request_id,),
        ).fetchone()
    return bool(row)

def _safe_context(app: Any, chat_id: str) -> tuple[bool, dict[str, Any] | None]:
    context = app.STORE.get_conversation_context(chat_id)
    if not context:
        return True, None
    mode = str(context.get("pending_category", "") or "")
    allowed = {"", str(app.MENU_CONTEXT), str(app.MENU_GATE_CONTEXT), str(app.USER_PROFILE_CONTEXT)}
    # Do not break a user who is already entering details for a selected category,
    # choosing BIN operations, following an active ticket, or being handed to staff.
    return mode in allowed, context


def recover_once(app: Any, now_epoch: int | None = None) -> int:
    if not bool(app.global_auto_reply_enabled()):
        return 0

    minutes, _scan_seconds, max_hours = settings()
    now_value = int(now_epoch if now_epoch is not None else time.time())
    candidates = _candidate_rows(
        app.STORE,
        now_value,
        delay_seconds=minutes * 60,
        max_age_seconds=max_hours * 3600,
    )
    sent = 0
    for row in candidates:
        chat_id = str(row.get("chat_id", "") or "").strip()
        phone = str(row.get("sender_phone", "") or "").strip()
        message_key = str(row.get("message_key", "") or "").strip()
        timestamp = int(row.get("message_timestamp", 0) or 0)
        if not chat_id or not message_key or timestamp <= 0:
            continue
        if app.STORE.manual_whatsapp_contact(chat_id, phone):
            continue
        if app.STORE.manual_chat_mode(chat_id):
            continue
        if app.queue_productivity.auto_reply_blocked(app.STORE, chat_id):
            continue
        if _has_queued_reply_after(app.STORE, chat_id, timestamp):
            continue
        safe, _context = _safe_context(app, chat_id)
        if not safe:
            continue

        language = app.get_contact_language(chat_id, phone)
        active = app.STORE.active_context_ticket(chat_id)
        active_id = int(active["id"]) if active else 0

        if not language:
            text = app.language_selection_text_v3()
            # Keep the context neutral: the normal inbound flow will persist the
            # language choice and then continue with profile/menu recovery.
        else:
            app.queue_user_locale.set_language(language)
            profile = app.get_contact_profile(chat_id, phone)
            if not profile:
                app.STORE.set_conversation_context(
                    chat_id,
                    app.USER_PROFILE_CONTEXT,
                    active_id,
                    bool(active),
                )
                text = app.profile_prompt_text()
            else:
                app.STORE.clear_conversation_draft(chat_id)
                app.queue_contextual_tickets.reset_context(app.STORE, chat_id, "")
                app.STORE.set_conversation_context(
                    chat_id,
                    app.MENU_CONTEXT,
                    active_id,
                    bool(active),
                )
                text = app.main_menu_text()

        request_id = _request_id(chat_id, message_key)
        if _request_already_queued(app.STORE, request_id):
            continue
        message_id = app.STORE.queue_direct_message(
            chat_id,
            text,
            "Система: восстановление меню",
            request_id=request_id,
        )
        if not message_id:
            continue
        try:
            app.queue_realtime.notify_outbound()
        except Exception:
            pass
        sent += 1
        print(
            f"[STALE MENU] восстановлен автоответ chat={chat_id} "
            f"message={message_key} age={max(0, now_value - timestamp)}s queue={message_id}",
            flush=True,
        )
    return sent


def worker_loop(app: Any) -> None:
    # Delay the first pass very slightly so DB initialization and the connector
    # can finish starting. Afterwards the worker is intentionally lightweight.
    time.sleep(5)
    while True:
        _minutes, scan_seconds, _max_hours = settings()
        try:
            recover_once(app)
        except Exception as error:
            print(f"[STALE MENU] ошибка фоновой проверки: {error}", flush=True)
        time.sleep(scan_seconds)
