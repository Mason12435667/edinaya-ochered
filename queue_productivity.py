from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ALMATY = timezone(timedelta(hours=5))
_ALERT_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean(value: object, limit: int = 1000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def initialize(store) -> None:
    with store.connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS message_bookmarks (
                chat_id TEXT NOT NULL,
                message_key TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, message_key)
            );
            CREATE INDEX IF NOT EXISTS idx_message_bookmarks_created
                ON message_bookmarks(created_at DESC);

            CREATE TABLE IF NOT EXISTS contact_meta (
                chat_id TEXT PRIMARY KEY,
                organization TEXT NOT NULL DEFAULT '',
                post TEXT NOT NULL DEFAULT '',
                important INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                auto_reply_blocked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS chat_notification_mutes (
                chat_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT '',
                muted_until TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_chat_notification_mutes_until
                ON chat_notification_mutes(muted_until);

            CREATE TABLE IF NOT EXISTS ticket_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                remind_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ticket_reminders_due
                ON ticket_reminders(done, remind_at, id);
            """
        )


def toggle_bookmark(store, chat_id: str, message_key: str, actor: str = "", note: str = "") -> bool:
    chat_id = _clean(chat_id, 120)
    message_key = _clean(message_key, 180)
    if not chat_id or not message_key:
        return False
    with store.connection() as db:
        row = db.execute(
            "SELECT 1 FROM message_bookmarks WHERE chat_id=? AND message_key=?",
            (chat_id, message_key),
        ).fetchone()
        if row:
            db.execute("DELETE FROM message_bookmarks WHERE chat_id=? AND message_key=?", (chat_id, message_key))
            return False
        db.execute(
            "INSERT INTO message_bookmarks(chat_id,message_key,actor,note,created_at) VALUES(?,?,?,?,?)",
            (chat_id, message_key, _clean(actor, 120), _clean(note, 1000), utc_now()),
        )
        return True


def is_bookmarked(store, chat_id: str, message_key: str) -> bool:
    with store.connection() as db:
        return db.execute(
            "SELECT 1 FROM message_bookmarks WHERE chat_id=? AND message_key=?",
            (_clean(chat_id, 120), _clean(message_key, 180)),
        ).fetchone() is not None


def list_bookmarks(store, limit: int = 200) -> list[dict[str, Any]]:
    with store.connection() as db:
        rows = db.execute(
            """
            SELECT b.chat_id,b.message_key,b.actor,b.note,b.created_at,
                   m.body,m.media_name,m.media_mime,m.message_timestamp,m.sender,m.from_me,m.deleted,
                   COALESCE(NULLIF(c.name,''),NULLIF(g.name,''),NULLIF(p.name,''),m.chat_id) AS chat_name
            FROM message_bookmarks b
            LEFT JOIN whatsapp_chat_messages m ON m.chat_id=b.chat_id AND m.message_key=b.message_key
            LEFT JOIN whatsapp_chats c ON c.chat_id=b.chat_id
            LEFT JOIN whatsapp_groups g ON g.chat_id=b.chat_id
            LEFT JOIN whatsapp_contacts p ON p.chat_id=b.chat_id
            ORDER BY b.created_at DESC LIMIT ?
            """,
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_contact_meta(store, chat_id: str) -> dict[str, Any]:
    chat_id = _clean(chat_id, 120)
    with store.connection() as db:
        row = db.execute("SELECT * FROM contact_meta WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else {
        "chat_id": chat_id, "organization": "", "post": "", "important": 0,
        "note": "", "auto_reply_blocked": 0, "updated_at": "",
    }


def save_contact_meta(store, chat_id: str, organization: str = "", post: str = "", important: bool = False,
                      note: str = "", auto_reply_blocked: bool = False) -> dict[str, Any]:
    chat_id = _clean(chat_id, 120)
    if not chat_id or chat_id.endswith("@g.us"):
        raise ValueError("Нужно выбрать личный чат")
    now = utc_now()
    with store.connection() as db:
        db.execute(
            """INSERT INTO contact_meta(chat_id,organization,post,important,note,auto_reply_blocked,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET organization=excluded.organization,post=excluded.post,
                 important=excluded.important,note=excluded.note,auto_reply_blocked=excluded.auto_reply_blocked,
                 updated_at=excluded.updated_at""",
            (chat_id, _clean(organization, 160), _clean(post, 160), int(bool(important)),
             _clean(note, 3000), int(bool(auto_reply_blocked)), now),
        )
    return get_contact_meta(store, chat_id)


def auto_reply_blocked(store, chat_id: str) -> bool:
    with store.connection() as db:
        row = db.execute("SELECT auto_reply_blocked FROM contact_meta WHERE chat_id=?", (_clean(chat_id,120),)).fetchone()
    return bool(row and row["auto_reply_blocked"])


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _end_of_shift_utc(now: datetime | None = None) -> datetime:
    now_local = (now or datetime.now(timezone.utc)).astimezone(ALMATY)
    today = now_local.date()
    boundary_hour = 20 if 8 <= now_local.hour < 20 else 8
    boundary = datetime(today.year, today.month, today.day, boundary_hour, 0, tzinfo=ALMATY)
    if boundary <= now_local:
        boundary += timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def set_chat_mute(store, chat_id: str, mode: str, actor: str = "") -> dict[str, Any]:
    chat_id = _clean(chat_id, 120)
    if not chat_id:
        raise ValueError("Чат не выбран")
    mode = _clean(mode, 32).lower()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if mode in {"off", "none", "unmute", ""}:
        with store.connection() as db:
            db.execute("DELETE FROM chat_notification_mutes WHERE chat_id=?", (chat_id,))
        return {"chat_id": chat_id, "muted": False, "mode": "off", "muted_until": ""}
    if mode == "hour":
        until = now + timedelta(hours=1)
    elif mode == "shift":
        until = _end_of_shift_utc(now)
    elif mode == "permanent":
        until = datetime(9999, 12, 31, 23, 59, tzinfo=timezone.utc)
    else:
        raise ValueError("Неизвестный режим уведомлений")
    with store.connection() as db:
        db.execute(
            """INSERT INTO chat_notification_mutes(chat_id,mode,muted_until,actor,updated_at) VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode,muted_until=excluded.muted_until,
                 actor=excluded.actor,updated_at=excluded.updated_at""",
            (chat_id, mode, until.isoformat(), _clean(actor,120), now.isoformat()),
        )
    return mute_state(store, chat_id)


def mute_state(store, chat_id: str) -> dict[str, Any]:
    chat_id = _clean(chat_id, 120)
    with store.connection() as db:
        row = db.execute("SELECT * FROM chat_notification_mutes WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            return {"chat_id": chat_id, "muted": False, "mode": "off", "muted_until": ""}
        data = dict(row)
        until = _parse_iso(data.get("muted_until", ""))
        if until is None or until <= datetime.now(timezone.utc):
            db.execute("DELETE FROM chat_notification_mutes WHERE chat_id=?", (chat_id,))
            return {"chat_id": chat_id, "muted": False, "mode": "off", "muted_until": ""}
    return {"chat_id": chat_id, "muted": True, "mode": data.get("mode", ""), "muted_until": data.get("muted_until", "")}


def active_muted_chat_ids(store) -> set[str]:
    now = datetime.now(timezone.utc)
    result: set[str] = set()
    expired: list[str] = []
    with store.connection() as db:
        rows = db.execute("SELECT chat_id,muted_until FROM chat_notification_mutes").fetchall()
        for row in rows:
            until = _parse_iso(row["muted_until"])
            if until and until > now:
                result.add(str(row["chat_id"]))
            else:
                expired.append(str(row["chat_id"]))
        if expired:
            db.executemany("DELETE FROM chat_notification_mutes WHERE chat_id=?", [(x,) for x in expired])
    return result


def notification_counts(store, base: dict[str, int]) -> dict[str, int]:
    counts = dict(base or {})
    muted = active_muted_chat_ids(store)
    with store.connection() as db:
        contacts = db.execute(
            """SELECT c.chat_id,c.unread_count FROM whatsapp_chats c
               INNER JOIN whatsapp_contacts p ON p.chat_id=c.chat_id
               WHERE c.chat_id NOT LIKE '%@g.us' AND c.chat_id NOT LIKE '%@broadcast' AND c.chat_id NOT LIKE '%@newsletter'"""
        ).fetchall()
        groups = db.execute(
            """SELECT c.chat_id,c.mention_unread_count,COALESCE(g.muted,0) AS legacy_muted
               FROM whatsapp_chats c INNER JOIN whatsapp_groups g ON g.chat_id=c.chat_id
               WHERE g.added_by_admin=1"""
        ).fetchall()
    counts["contacts"] = sum(int(r["unread_count"] or 0) for r in contacts if str(r["chat_id"]) not in muted)
    counts["groups"] = sum(int(r["mention_unread_count"] or 0) for r in groups if not r["legacy_muted"] and str(r["chat_id"]) not in muted)
    return counts


def filter_notification_events(store, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    muted = active_muted_chat_ids(store)
    return [e for e in events if str(e.get("chat_id", "") or "") not in muted]


def first_unmuted_chat(store, group: bool) -> str:
    muted = active_muted_chat_ids(store)
    with store.connection() as db:
        if group:
            rows = db.execute(
                """SELECT c.chat_id FROM whatsapp_chats c INNER JOIN whatsapp_groups g ON g.chat_id=c.chat_id
                   WHERE g.added_by_admin=1 AND COALESCE(g.muted,0)=0 AND c.mention_unread_count>0
                   ORDER BY c.last_timestamp DESC,c.updated_at DESC LIMIT 50"""
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT c.chat_id FROM whatsapp_chats c INNER JOIN whatsapp_contacts p ON p.chat_id=c.chat_id
                   WHERE c.unread_count>0 AND c.chat_id NOT LIKE '%@g.us' AND c.chat_id NOT LIKE '%@broadcast' AND c.chat_id NOT LIKE '%@newsletter'
                   ORDER BY c.last_timestamp DESC,c.updated_at DESC LIMIT 50"""
            ).fetchall()
    for row in rows:
        cid = str(row["chat_id"])
        if cid not in muted:
            return cid
    return ""


def mark_unread(store, chat_id: str) -> bool:
    chat_id = _clean(chat_id, 120)
    if not chat_id:
        return False
    with store.connection() as db:
        cursor = db.execute(
            "UPDATE whatsapp_chats SET unread_count=CASE WHEN unread_count<1 THEN 1 ELSE unread_count END, updated_at=? WHERE chat_id=?",
            (utc_now(), chat_id),
        )
    return cursor.rowcount > 0


def open_tickets(store, limit: int = 120) -> list[dict[str, Any]]:
    with store.connection() as db:
        rows = db.execute(
            """SELECT id,title,sender,category,status,assigned_to,updated_at FROM tickets
               WHERE status NOT IN ('done','invalid') ORDER BY updated_at DESC,id DESC LIMIT ?""",
            (max(1,min(int(limit),500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def create_ticket_from_message(store, chat_id: str, message_key: str, category: str, actor: str) -> int:
    chat_id = _clean(chat_id,120)
    message_key = _clean(message_key,180)
    message = store.get_whatsapp_message(chat_id, message_key) if chat_id and message_key else None
    if not message or bool(message.get("deleted")):
        raise ValueError("Сообщение не найдено")
    body = _clean(message.get("body") or message.get("media_name") or "Сообщение WhatsApp", 32000)
    sender = _clean(message.get("sender") or "Пользователь WhatsApp", 180)
    phone = _clean(message.get("sender_phone") or "", 60)
    title = body.replace("\n", " ")[:100] or "Сообщение WhatsApp"
    ticket_id = store.create_ticket({
        "source": "whatsapp-manual",
        "sender": sender,
        "phone": phone,
        "chat_id": chat_id,
        "external_id": message_key,
        "category": _clean(category,60) or "general",
        "priority": "normal",
        "title": title,
        "summary": body[:2000],
        "original_text": body,
        "attachment_name": _clean(message.get("media_name") or "", 240),
        "assigned_to": _clean(actor,120),
    })
    store.link_whatsapp_message_to_ticket(chat_id, message_key, ticket_id)
    return ticket_id


def link_message_to_ticket(store, chat_id: str, message_key: str, ticket_id: int) -> bool:
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        return False
    if ticket_id <= 0:
        return False
    with store.connection() as db:
        valid = db.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    return bool(valid) and store.link_whatsapp_message_to_ticket(_clean(chat_id,120), _clean(message_key,180), ticket_id)


def add_reminder(store, ticket_id: int, remind_at: str, note: str, actor: str) -> int:
    try:
        ticket_id = int(ticket_id)
    except (TypeError, ValueError):
        raise ValueError("Некорректная заявка")
    due = _parse_iso(remind_at)
    if due is None:
        raise ValueError("Некорректное время напоминания")
    if due <= datetime.now(timezone.utc) - timedelta(minutes=1):
        raise ValueError("Время напоминания уже прошло")
    now = utc_now()
    with store.connection() as db:
        ticket = db.execute("SELECT id FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not ticket:
            raise ValueError("Заявка не найдена")
        cur = db.execute(
            "INSERT INTO ticket_reminders(ticket_id,remind_at,note,actor,done,created_at,updated_at) VALUES(?,?,?,?,0,?,?)",
            (ticket_id, due.isoformat(), _clean(note,1000), _clean(actor,120), now, now),
        )
        return int(cur.lastrowid)


def complete_reminder(store, reminder_id: int) -> bool:
    with store.connection() as db:
        cur = db.execute("UPDATE ticket_reminders SET done=1,updated_at=? WHERE id=?", (utc_now(), int(reminder_id)))
    return cur.rowcount > 0


def list_reminders(store, due_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    now = utc_now()
    where = "r.done=0 AND r.remind_at<=?" if due_only else "r.done=0"
    params: list[Any] = [now] if due_only else []
    params.append(max(1,min(int(limit),500)))
    with store.connection() as db:
        rows = db.execute(
            f"""SELECT r.*,t.title,t.sender,t.status FROM ticket_reminders r
                LEFT JOIN tickets t ON t.id=r.ticket_id WHERE {where}
                ORDER BY r.remind_at ASC,r.id ASC LIMIT ?""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def reminder_events(store) -> tuple[int, list[dict[str, Any]]]:
    rows = list_reminders(store, due_only=True, limit=20)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    events = []
    for row in rows:
        events.append({
            "id": f"reminder:{row['id']}",
            "kind": "system",
            "title": f"Напоминание по заявке #{row['ticket_id']}",
            "detail": _clean(row.get("note") or row.get("title") or "Открыть заявку", 240),
            "source": "Напоминание",
            "href": f"/ticket?id={row['ticket_id']}",
            "timestamp": now_ts,
        })
    return len(rows), events


def chat_gallery(store, chat_id: str, limit: int = 300) -> list[dict[str, Any]]:
    chat_id = _clean(chat_id,120)
    with store.connection() as db:
        rows = db.execute(
            """SELECT message_key,body,message_timestamp,media_mime,media_name,from_me,sender
               FROM whatsapp_chat_messages WHERE chat_id=? AND deleted=0 AND media_path<>''
               ORDER BY message_timestamp DESC,id DESC LIMIT ?""",
            (chat_id,max(1,min(int(limit),1000))),
        ).fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        mime=str(item.get("media_mime") or "")
        item["kind"] = "photo" if mime.startswith("image/") else "video" if mime.startswith("video/") else "voice" if mime.startswith("audio/") else "file"
        item["media_url"] = f"/api/chat-media?chat_id={chat_id}&message_id={item['message_key']}"
        result.append(item)
    return result


def chat_find(store, chat_id: str, query: str, limit: int = 200) -> list[dict[str, Any]]:
    chat_id = _clean(chat_id,120)
    query = _clean(query,200).casefold()
    if not chat_id or not query:
        return []
    tokens=[x for x in query.split() if x]
    clauses=[]; params: list[Any]=[chat_id]
    for token in tokens:
        clauses.append("instr(FOLD(body || ' ' || transcript || ' ' || sender || ' ' || media_name),?)>0")
        params.append(token)
    params.append(max(1,min(int(limit),500)))
    with store.connection() as db:
        rows=db.execute(
            "SELECT message_key,body,transcript,sender,media_name,message_timestamp FROM whatsapp_chat_messages WHERE chat_id=? AND deleted=0 AND " + " AND ".join(clauses) + " ORDER BY message_timestamp ASC,id ASC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def advanced_search(store, query: str = "", *, date_from: str = "", date_to: str = "", chat_type: str = "",
                    sender: str = "", employee: str = "", attachment: str = "", mentions_only: bool = False,
                    voice_only: bool = False, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    q=_clean(query,200).casefold(); sender=_clean(sender,160).casefold(); employee=_clean(employee,160).casefold()
    page=max(1,min(int(page or 1),10000)); page_size=max(10,min(int(page_size),100))
    if not any([q,date_from,date_to,chat_type,sender,employee,attachment,mentions_only,voice_only]):
        return {"results":[],"has_more":False,"page":page}
    conditions=["m.deleted=0"]; params: list[Any]=[]
    for token in q.split():
        conditions.append("instr(FOLD(m.body || ' ' || m.transcript || ' ' || m.sender || ' ' || COALESCE(c.name,'') || ' ' || COALESCE(g.name,'') || ' ' || COALESCE(p.name,'') || ' ' || m.chat_id || ' ' || COALESCE(p.phone,'')),?)>0")
        params.append(token)
    if date_from:
        try:
            start=int(datetime.fromisoformat(date_from).replace(tzinfo=ALMATY).astimezone(timezone.utc).timestamp())
            conditions.append("m.message_timestamp>=?"); params.append(start)
        except ValueError: pass
    if date_to:
        try:
            end_dt=datetime.fromisoformat(date_to).replace(tzinfo=ALMATY)+timedelta(days=1)
            conditions.append("m.message_timestamp<?"); params.append(int(end_dt.astimezone(timezone.utc).timestamp()))
        except ValueError: pass
    if chat_type=="personal": conditions.append("m.chat_id NOT LIKE '%@g.us'")
    elif chat_type=="group": conditions.append("m.chat_id LIKE '%@g.us'")
    if sender:
        conditions.append("instr(FOLD(m.sender || ' ' || m.sender_phone),?)>0"); params.append(sender)
    if employee:
        conditions.append("instr(FOLD(COALESCE(o.actor,'')),?)>0"); params.append(employee)
    if attachment and attachment!="any":
        if attachment=="image": conditions.append("m.media_mime LIKE 'image/%'")
        elif attachment=="video": conditions.append("m.media_mime LIKE 'video/%'")
        elif attachment=="audio": conditions.append("m.media_mime LIKE 'audio/%'")
        elif attachment=="file": conditions.append("m.media_path<>'' AND m.media_mime NOT LIKE 'image/%' AND m.media_mime NOT LIKE 'video/%' AND m.media_mime NOT LIKE 'audio/%'")
    if mentions_only: conditions.append("(m.notify=1 OR m.mentions_json<>'[]')")
    if voice_only: conditions.append("m.media_mime LIKE 'audio/%'")
    sql=f"""SELECT m.message_key,m.chat_id,m.body,m.transcript,m.sender,m.sender_phone,m.media_name,m.media_mime,m.message_timestamp,m.from_me,
                    COALESCE(NULLIF(p.name,''),NULLIF(g.name,''),NULLIF(c.name,''),m.sender,m.chat_id) AS name,
                    COALESCE(o.actor,'') AS employee
             FROM whatsapp_chat_messages m
             LEFT JOIN whatsapp_chats c ON c.chat_id=m.chat_id
             LEFT JOIN whatsapp_groups g ON g.chat_id=m.chat_id
             LEFT JOIN whatsapp_contacts p ON p.chat_id=m.chat_id
             LEFT JOIN outbound_messages o ON o.provider_id=m.message_key
             WHERE {' AND '.join(conditions)} ORDER BY m.message_timestamp DESC,m.id DESC LIMIT ? OFFSET ?"""
    params.extend([page_size+1,(page-1)*page_size])
    with store.connection() as db:
        rows=db.execute(sql,params).fetchall()
    result=[dict(r) for r in rows[:page_size]]
    return {"results":result,"has_more":len(rows)>page_size,"page":page}


def _seconds_between(start: str, end: str) -> float | None:
    a=_parse_iso(start); b=_parse_iso(end)
    if not a or not b or b<a: return None
    return (b-a).total_seconds()


def _extract_post(text: str) -> str:
    value=str(text or "")
    m=re.search(r"(?im)^\s*пост\s*[:\-]\s*([^\n]{2,120})",value)
    if not m: m=re.search(r"(?i)\bпост\s+([A-Za-zА-Яа-яЁё0-9 ._\-/]{2,80})",value)
    return _clean(m.group(1),80) if m else "Не указан"


def analytics(store) -> dict[str, Any]:
    with store.connection() as db:
        rows=[dict(r) for r in db.execute("SELECT id,category,status,assigned_to,created_at,first_response_at,closed_at,original_text,summary FROM tickets ORDER BY id DESC").fetchall()]
    by_category: dict[str,int]={}; by_employee: dict[str,int]={}; by_post: dict[str,int]={}
    first=[]; solution=[]
    for r in rows:
        by_category[str(r.get('category') or 'Без категории')]=by_category.get(str(r.get('category') or 'Без категории'),0)+1
        emp=str(r.get('assigned_to') or 'Не назначен'); by_employee[emp]=by_employee.get(emp,0)+1
        post=_extract_post(str(r.get('original_text') or r.get('summary') or '')); by_post[post]=by_post.get(post,0)+1
        x=_seconds_between(str(r.get('created_at') or ''),str(r.get('first_response_at') or ''))
        if x is not None: first.append(x)
        x=_seconds_between(str(r.get('created_at') or ''),str(r.get('closed_at') or ''))
        if x is not None: solution.append(x)
    return {
        "total":len(rows),
        "open":sum(1 for r in rows if r.get('status') not in {'done','invalid'}),
        "closed":sum(1 for r in rows if r.get('status') in {'done','invalid'}),
        "avg_first_minutes":round(sum(first)/len(first)/60,1) if first else 0,
        "avg_solution_minutes":round(sum(solution)/len(solution)/60,1) if solution else 0,
        "by_category":sorted(by_category.items(),key=lambda x:(-x[1],x[0]))[:50],
        "by_employee":sorted(by_employee.items(),key=lambda x:(-x[1],x[0]))[:50],
        "by_post":sorted(by_post.items(),key=lambda x:(-x[1],x[0]))[:50],
    }


def analytics_csv(store) -> bytes:
    data=analytics(store); stream=io.StringIO(); writer=csv.writer(stream,delimiter=';')
    writer.writerow(["Показатель","Значение"])
    writer.writerow(["Всего заявок",data['total']]); writer.writerow(["Открытые",data['open']]); writer.writerow(["Закрытые",data['closed']])
    writer.writerow(["Среднее время первого ответа, мин",data['avg_first_minutes']]); writer.writerow(["Среднее время решения, мин",data['avg_solution_minutes']])
    for title,key in [("По категориям","by_category"),("По сотрудникам","by_employee")]:
        writer.writerow([]); writer.writerow([title,"Количество"]); writer.writerows(data[key])
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


def backup_dir(data_dir: Path) -> Path:
    path=Path(os.getenv('QUEUE_MANUAL_BACKUP_DIR','/var/backups/edinaya-ochered-db')).expanduser()
    path.mkdir(parents=True,exist_ok=True)
    return path


def create_database_backup(database_path: Path, data_dir: Path, label: str = "manual") -> Path:
    target_dir=backup_dir(data_dir)
    stamp=datetime.now(ALMATY).strftime('%Y%m%d-%H%M%S')
    safe=re.sub(r'[^A-Za-zА-Яа-я0-9_-]+','-',_clean(label,50)).strip('-') or 'manual'
    target=target_dir/f"tickets-{stamp}-{safe}.db"
    source=sqlite3.connect(str(database_path),timeout=20)
    dest=sqlite3.connect(str(target),timeout=20)
    try:
        source.execute('PRAGMA wal_checkpoint(PASSIVE)')
        source.backup(dest)
        check=dest.execute('PRAGMA integrity_check').fetchone()
        if not check or str(check[0]).lower()!='ok':
            raise RuntimeError('Проверка резервной копии не пройдена')
    finally:
        dest.close(); source.close()
    return target


def backup_integrity(path: Path) -> tuple[bool,str]:
    try:
        db=sqlite3.connect(str(path),timeout=10)
        try: result=db.execute('PRAGMA integrity_check').fetchone()
        finally: db.close()
        ok=bool(result and str(result[0]).lower()=='ok')
        return ok, str(result[0]) if result else 'Нет результата проверки'
    except Exception as exc:
        return False, str(exc)[:200]


def list_backups(data_dir: Path) -> list[dict[str,Any]]:
    result=[]
    for path in sorted(backup_dir(data_dir).glob('tickets-*.db'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]:
        ok,detail=backup_integrity(path)
        stat=path.stat()
        result.append({"name":path.name,"size":stat.st_size,"mtime":stat.st_mtime,"ok":ok,"detail":detail})
    return result


def restore_database_backup(database_path: Path, data_dir: Path, name: str) -> Path:
    name=Path(_clean(name,240)).name
    source=backup_dir(data_dir)/name
    if not source.is_file(): raise ValueError('Резервная копия не найдена')
    ok,detail=backup_integrity(source)
    if not ok: raise ValueError('Копия повреждена: '+detail)
    pre=create_database_backup(database_path,data_dir,'before-restore')
    src=sqlite3.connect(str(source),timeout=20); dst=sqlite3.connect(str(database_path),timeout=20)
    try:
        src.backup(dst)
        check=dst.execute('PRAGMA integrity_check').fetchone()
        if not check or str(check[0]).lower()!='ok': raise RuntimeError('Восстановленная база не прошла проверку')
    finally:
        dst.close(); src.close()
    return pre


def system_alerts(store, data_dir: Path, connector: dict[str,Any] | None = None) -> list[dict[str,Any]]:
    global _ALERT_CACHE
    import time
    now_mono=time.monotonic()
    # Notifications are polled frequently by the browser. Heavy checks are cached.
    if now_mono-_ALERT_CACHE[0] < 20:
        cached=[dict(x) for x in _ALERT_CACHE[1]]
        # Connector state is cheap and may change inside the cache window.
        cached=[x for x in cached if x.get('id')!='system:connector-critical']
        if not bool((connector or {}).get('connected')):
            cached.insert(0,{"id":"system:connector-critical","kind":"system","title":"WhatsApp-коннектор не подключён","detail":"Перезапустите коннектор или проверьте QR-сессию","source":"Система","href":"/admin/system","timestamp":int(datetime.now(timezone.utc).timestamp())})
        return cached
    now=int(datetime.now(timezone.utc).timestamp()); events=[]
    connector=connector or {}
    if not bool(connector.get('connected')):
        events.append({"id":"system:connector-critical","kind":"system","title":"WhatsApp-коннектор не подключён","detail":"Перезапустите коннектор или проверьте QR-сессию","source":"Система","href":"/admin/system","timestamp":now})
    try:
        usage=shutil.disk_usage(Path(data_dir))
        free_ratio=usage.free/max(1,usage.total)
        if usage.free < 1_000_000_000 or free_ratio < 0.05:
            events.append({"id":"system:disk-critical","kind":"system","title":"Мало свободного места","detail":f"Свободно {usage.free/1024/1024/1024:.1f} ГБ ({free_ratio*100:.1f}%)","source":"Система","href":"/admin/system","timestamp":now})
    except OSError: pass
    try:
        with store.connection() as db:
            check=db.execute('PRAGMA quick_check').fetchone()
        if not check or str(check[0]).lower()!='ok':
            events.append({"id":"system:database-critical","kind":"system","title":"Ошибка проверки базы данных","detail":_clean(check[0] if check else 'Нет ответа от SQLite',200),"source":"Система","href":"/admin/system","timestamp":now})
    except Exception as exc:
        events.append({"id":"system:database-error","kind":"system","title":"База данных недоступна","detail":_clean(exc,200),"source":"Система","href":"/admin/system","timestamp":now})
    _ALERT_CACHE=(now_mono,[dict(x) for x in events])
    return events
