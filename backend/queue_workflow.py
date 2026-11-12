"""Логика рабочих процессов: недавние чаты, присутствие, служебные заметки, закрытие, передача и объединение заявок.

Сроки хранения и очистка медиа — в queue_retention, выгрузка чата — в queue_chat_export.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, target_id))
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
        db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, int(source_id)))
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
