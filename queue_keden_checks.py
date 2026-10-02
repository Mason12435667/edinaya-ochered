from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

CATEGORY = "check_td"
SUPPORT_EMAIL = "keden_support@kgd.minfin.gov.kz"

DOC_RE = re.compile(r"\b[A-Z]{2}/[A-Z]{3}\d{3,9}/\d{5,15}\b", re.IGNORECASE)
TRANSPORT_RE = re.compile(r"\bKZ\d{5,14}\b", re.IGNORECASE)
LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{6,16}(?!\d)")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean(value: object, limit: int = 32000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def extract_reference(text: object) -> str:
    value = _clean(text, 4000)
    if not value:
        return ""
    for regex in (DOC_RE, TRANSPORT_RE):
        match = regex.search(value)
        if match:
            return match.group(0).strip()[:120]
    # Accept a standalone numeric TD/reference, or a labelled one.
    labelled = re.search(r"(?i)(?:\bтд\b|декларац\w*|номер)\s*[:№#=\-]?\s*(\d{6,16})", value)
    if labelled:
        return labelled.group(1)
    numbers = LONG_DIGITS_RE.findall(value)
    if len(numbers) == 1:
        return numbers[0]
    return ""


def initialize(store: Any) -> None:
    with store.connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS keden_check_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT NOT NULL UNIQUE,
                chat_id TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                external_id TEXT NOT NULL DEFAULT '',
                reference TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'ru',
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                worker TEXT NOT NULL DEFAULT '',
                claimed_at TEXT NOT NULL DEFAULT '',
                result_kind TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                package_rows_json TEXT NOT NULL DEFAULT '[]',
                issues_json TEXT NOT NULL DEFAULT '[]',
                ticket_id INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_keden_check_jobs_status
                ON keden_check_jobs(status, id);
            """
        )


def _job_key(chat_id: str, external_id: str, reference: str) -> str:
    raw = f"{chat_id}|{external_id}|{reference}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()


def enqueue(
    store: Any,
    *,
    chat_id: str,
    phone: str,
    sender: str,
    external_id: str,
    reference: str,
    source_text: str,
    language: str = "ru",
) -> dict[str, object]:
    initialize(store)
    chat_id = _clean(chat_id, 160)
    phone = _clean(phone, 80)
    sender = _clean(sender, 180) or "Неизвестный отправитель"
    external_id = _clean(external_id, 180)
    reference = extract_reference(reference) or _clean(reference, 120)
    source_text = _clean(source_text, 32000)
    language = "kz" if str(language or "").casefold() == "kz" else "ru"
    if not chat_id or not reference:
        raise ValueError("chat_id and reference are required")
    key = _job_key(chat_id, external_id, reference)
    now = _now()
    with store.connection() as db:
        row = db.execute("SELECT * FROM keden_check_jobs WHERE job_key=?", (key,)).fetchone()
        if row:
            return dict(row)
        cur = db.execute(
            """
            INSERT INTO keden_check_jobs
                (job_key,chat_id,phone,sender,external_id,reference,source_text,language,
                 status,attempts,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?, 'queued',0,?,?)
            """,
            (key, chat_id, phone, sender, external_id, reference, source_text, language, now, now),
        )
        job_id = int(cur.lastrowid)
        row = db.execute("SELECT * FROM keden_check_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row)


def claim(store: Any, *, worker: str = "") -> dict[str, object] | None:
    initialize(store)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.replace(microsecond=0).isoformat()
    stale = (now_dt - timedelta(minutes=5)).replace(microsecond=0).isoformat()
    worker = _clean(worker, 120) or "keden-worker"
    with store.connection() as db:
        # Requeue only abandoned processing jobs. Result submission itself is idempotent.
        db.execute(
            """UPDATE keden_check_jobs SET status='queued',worker='',claimed_at='',updated_at=?
               WHERE status='processing' AND claimed_at<>'' AND claimed_at<?""",
            (now, stale),
        )
        row = db.execute(
            """SELECT * FROM keden_check_jobs
               WHERE status='queued' ORDER BY id ASC LIMIT 1"""
        ).fetchone()
        if not row:
            return None
        job_id = int(row["id"])
        updated = db.execute(
            """UPDATE keden_check_jobs
               SET status='processing',attempts=attempts+1,worker=?,claimed_at=?,updated_at=?
               WHERE id=? AND status='queued'""",
            (worker, now, now, job_id),
        ).rowcount
        if not updated:
            return None
        row = db.execute("SELECT * FROM keden_check_jobs WHERE id=?", (job_id,)).fetchone()
    item = dict(row)
    return {
        "id": int(item["id"]),
        "reference": item["reference"],
        "chat_id": item["chat_id"],
        "language": item["language"],
        "attempts": int(item["attempts"] or 0),
    }


def _queue_reply(app: Any, row: dict[str, object], body: str, suffix: str) -> int:
    body = _clean(body, 32000)
    if not body:
        return 0
    request_id = f"keden-check:{int(row['id'])}:{suffix}"
    message_id = int(app.STORE.queue_direct_message(
        str(row.get("chat_id", "")), body, "Система · Проверка ТД", request_id=request_id
    ) or 0)
    if message_id:
        app.queue_realtime.notify_outbound()
    return message_id


def _localized(row: dict[str, object], ru: str, kz: str) -> str:
    return kz if str(row.get("language", "ru")) == "kz" else ru


def _create_issue_ticket(app: Any, row: dict[str, object], issues: list[str], summary: str, *, technical=False) -> int:
    reference = str(row.get("reference", ""))
    details = [f"ТД / данные проверки: {reference}"]
    if summary:
        details.append(summary)
    if issues:
        details.append("Обнаружено:\n- " + "\n- ".join(issues[:30]))
    if technical:
        details.append("Автоматическая проверка не завершилась; требуется ручная проверка специалистом.")
    payload = {
        "source": "whatsapp",
        "sender": str(row.get("sender", "")) or "Неизвестный отправитель",
        "phone": str(row.get("phone", "")),
        "chat_id": str(row.get("chat_id", "")),
        "external_id": str(row.get("external_id", "")),
        "category": CATEGORY,
        "priority": "normal",
        "title": f"Проверка ТД / пакетов Кеден: {reference}",
        "summary": "\n".join(details),
        "original_text": str(row.get("source_text", "")),
        "attachment_name": "",
        "assigned_to": app.active_employee(),
    }
    ticket_id = int(app.STORE.create_ticket(payload))
    try:
        if row.get("chat_id") and row.get("external_id"):
            app.STORE.link_whatsapp_message_to_ticket(str(row["chat_id"]), str(row["external_id"]), ticket_id)
    except Exception:
        pass
    try:
        app.queue_realtime.notify("ticket")
    except Exception:
        pass
    return ticket_id


def complete(app: Any, payload: dict[str, object]) -> dict[str, object]:
    store = app.STORE
    initialize(store)
    try:
        job_id = int(payload.get("job_id", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid job_id")
    if job_id <= 0:
        raise ValueError("invalid job_id")
    kind = _clean(payload.get("kind", ""), 40).casefold()
    summary = _clean(payload.get("summary", ""), 2000)
    error = _clean(payload.get("error", ""), 2000)
    raw_issues = payload.get("issues", [])
    raw_rows = payload.get("rows", [])
    issues = [_clean(x, 600) for x in raw_issues] if isinstance(raw_issues, list) else []
    rows = raw_rows if isinstance(raw_rows, list) else []
    rows = rows[:250]

    with store.connection() as db:
        dbrow = db.execute("SELECT * FROM keden_check_jobs WHERE id=?", (job_id,)).fetchone()
    if not dbrow:
        raise ValueError("job not found")
    row = dict(dbrow)
    if str(row.get("status", "")) in {"done", "failed"}:
        return {"ok": True, "duplicate": True, "job_id": job_id, "ticket_id": int(row.get("ticket_id", 0) or 0)}

    ticket_id = 0
    status = "done"
    if kind == "no_packets":
        body = _localized(
            row,
            f"По ТД {row['reference']} пакеты Кеден не поступили. Напишите, пожалуйста, в поддержку Кеден: {SUPPORT_EMAIL} и укажите номер ТД.",
            f"{row['reference']} ТД бойынша КЕДЕН пакеттері түспеген. КЕДЕН қолдау қызметіне жазыңыз: {SUPPORT_EMAIL} және ТД нөмірін көрсетіңіз.",
        )
        _queue_reply(app, row, body, "no-packages")
    elif kind == "ok":
        body = _localized(
            row,
            f"По ТД {row['reference']} пакеты Кеден поступили. Ошибок не обнаружено.",
            f"{row['reference']} ТД бойынша КЕДЕН пакеттері түсті. Қате анықталған жоқ.",
        )
        _queue_reply(app, row, body, "ok")
    elif kind == "issue":
        ticket_id = _create_issue_ticket(app, row, issues, summary)
        body = _localized(
            row,
            f"По ТД {row['reference']} пакеты Кеден поступили, но обнаружена проблема. Заявка №{ticket_id} создана, специалист решает этот вопрос.",
            f"{row['reference']} ТД бойынша КЕДЕН пакеттері түсті, бірақ мәселе анықталды. №{ticket_id} өтінім құрылды, маман мәселені шешіп жатыр.",
        )
        _queue_reply(app, row, body, "issue")
    else:
        # A browser/login/worker failure must not lose the user's request.
        issues_for_ticket = issues or ([error] if error else ["Автоматическая проверка Кеден не завершилась"])
        ticket_id = _create_issue_ticket(app, row, issues_for_ticket, summary or error, technical=True)
        kind = "error"
        status = "failed"
        body = _localized(
            row,
            f"Автоматически проверить ТД {row['reference']} не удалось. Заявка №{ticket_id} создана для ручной проверки специалистом.",
            f"{row['reference']} ТД автоматты түрде тексерілмеді. Қолмен тексеру үшін №{ticket_id} өтінім құрылды.",
        )
        _queue_reply(app, row, body, "error")

    now = _now()
    with store.connection() as db:
        db.execute(
            """UPDATE keden_check_jobs SET status=?,result_kind=?,result_summary=?,package_rows_json=?,
               issues_json=?,ticket_id=?,last_error=?,updated_at=? WHERE id=?""",
            (
                status, kind, summary, json.dumps(rows, ensure_ascii=False)[:500000],
                json.dumps(issues, ensure_ascii=False)[:100000], ticket_id, error, now, job_id,
            ),
        )
    return {"ok": True, "job_id": job_id, "kind": kind, "ticket_id": ticket_id}


def snapshot(store: Any, limit: int = 50) -> list[dict[str, object]]:
    initialize(store)
    with store.connection() as db:
        rows = db.execute(
            "SELECT * FROM keden_check_jobs ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),)
        ).fetchall()
    return [dict(row) for row in rows]
