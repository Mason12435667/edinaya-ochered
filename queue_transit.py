from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import socket
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import queue_google_sheets

STATUS_CHOICES = (
    "Снято штатно",
    "Срезано",
    "Снять через Bluetooth",
    "Нужно снять",
    "Отложено",
    "Повреждение троса",
    "Нету",
)

EDITABLE_FIELDS = (
    "employee",
    "attached_date",
    "removed_date",
    "vehicle_type",
    "vehicle_count",
    "plate",
    "seal_count",
    "seal",
    "reason",
    "status",
    "note",
    "transport_status",
    "location",
)

MAX_LENGTHS = {
    "employee": 120,
    "attached_date": 10,
    "removed_date": 10,
    "vehicle_type": 80,
    "vehicle_count": 20,
    "plate": 120,
    "seal_count": 20,
    "seal": 120,
    "reason": 600,
    "status": 80,
    "note": 1600,
    "transport_status": 300,
    "location": 500,
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean(value: object, limit: int = 500) -> str:
    text = str(value or "").replace("\x00", "")
    text = re.sub(r"[\t ]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines()).strip()
    return text[:limit]


def normalize_date(value: object) -> str:
    raw = _clean(value, 20)
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d,%m,%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw[:10]


def display_date(value: object) -> str:
    raw = _clean(value, 20)
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return raw


def input_date(value: object) -> str:
    raw = _clean(value, 20)
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else ""




def _source_url() -> str:
    return str(os.getenv("QUEUE_TRANSIT_SOURCE_URL", "") or "").strip()


def writeback_configured() -> bool:
    try:
        queue_google_sheets.spreadsheet_id(_source_url())
        return queue_google_sheets.configured()
    except Exception:
        return False


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _writeback_gid() -> int:
    try:
        return max(0, int(os.getenv("QUEUE_TRANSIT_SOURCE_GID", "0") or 0))
    except (TypeError, ValueError):
        return 0


def _google_values(row: dict[str, object]) -> dict[str, str]:
    return {
        "number": _clean(row.get("number", ""), 40),
        "employee": _clean(row.get("employee", ""), MAX_LENGTHS["employee"]),
        "attached_date": normalize_date(row.get("attached_date", "")),
        "removed_date": normalize_date(row.get("removed_date", "")),
        "vehicle_type": _clean(row.get("vehicle_type", ""), MAX_LENGTHS["vehicle_type"]),
        "vehicle_count": _clean(row.get("vehicle_count", ""), MAX_LENGTHS["vehicle_count"]),
        "plate": _clean(row.get("plate", ""), MAX_LENGTHS["plate"]),
        "seal_count": _clean(row.get("seal_count", ""), MAX_LENGTHS["seal_count"]),
        "seal": _clean(row.get("seal", ""), MAX_LENGTHS["seal"]),
        "reason": _clean(row.get("reason", ""), MAX_LENGTHS["reason"]),
        "status": _clean(row.get("status_raw", "") or row.get("status", ""), 160),
        "note": _clean(row.get("note", ""), MAX_LENGTHS["note"]),
        "transport_status": _clean(row.get("transport_status", ""), MAX_LENGTHS["transport_status"]),
        "location": _clean(row.get("location", ""), MAX_LENGTHS["location"]),
    }


def _post_writeback(payload: dict[str, object]) -> dict[str, object]:
    if not writeback_configured():
        raise RuntimeError("Google Sheets API ещё не настроен")
    return queue_google_sheets.write_record(
        source=_source_url(),
        gid=int(payload.get("gid", 0) or 0),
        action=str(payload.get("action", "append") or "append"),
        row=int(payload.get("row", 0) or 0),
        request_id=_clean(payload.get("request_id", ""), 80),
        values=dict(payload.get("values", {}) or {}),
    )


def _mark_writeback_pending(connection: Any, record_id: int) -> str:
    row = connection.execute(
        "SELECT google_request_id FROM transit_np_records WHERE id=?", (record_id,)
    ).fetchone()
    request_id = _clean(row[0] if row else "", 80) or _new_request_id()
    connection.execute(
        """UPDATE transit_np_records
           SET google_sync_state='pending', google_sync_error='', google_request_id=?
           WHERE id=?""",
        (request_id, record_id),
    )
    return request_id


def push_record_to_google(store: Any, record_id: int) -> tuple[bool, str]:
    ensure_schema(store)
    if record_id <= 0:
        return False, "Запись не найдена"
    with store.connection() as connection:
        row = connection.execute("SELECT * FROM transit_np_records WHERE id=?", (record_id,)).fetchone()
        if not row:
            return False, "Запись не найдена"
        item = dict(row)
        request_id = _clean(item.get("google_request_id", ""), 80)
        if not request_id:
            request_id = _mark_writeback_pending(connection, record_id)
            item["google_request_id"] = request_id
    if not writeback_configured():
        with store.connection() as connection:
            connection.execute(
                "UPDATE transit_np_records SET google_sync_state='pending', google_sync_error=? WHERE id=?",
                ("Google Sheets API ещё не настроен", record_id),
            )
        return False, "Google Sheets API ещё не настроен; запись сохранена локально и останется в очереди синхронизации"
    payload = {
        "action": "update" if int(item.get("source_row", 0) or 0) > 0 else "append",
        "gid": _writeback_gid(),
        "row": int(item.get("source_row", 0) or 0),
        "record_id": int(item.get("id", 0) or 0),
        "request_id": request_id,
        "values": _google_values(item),
    }
    try:
        result = _post_writeback(payload)
        google_row = int(result.get("row", 0) or 0)
        if google_row < 2:
            raise RuntimeError("Google Sheets не вернул номер сохранённой строки")
        now = _now()
        with store.connection() as connection:
            connection.execute(
                """UPDATE transit_np_records
                   SET source_row=?, source_active=1, google_sync_state='awaiting_source',
                       google_sync_error='', google_synced_at=?
                   WHERE id=?""",
                (google_row, now, record_id),
            )
        return True, f"Google Sheets: строка {google_row} обновлена"
    except Exception as error:
        message = _clean(str(error), 240) or "Не удалось записать в Google Sheets"
        with store.connection() as connection:
            connection.execute(
                """UPDATE transit_np_records
                   SET google_sync_state='error', google_sync_error=?
                   WHERE id=?""",
                (message, record_id),
            )
        return False, message


def flush_pending_writebacks(store: Any, *, limit: int = 25) -> dict[str, object]:
    ensure_schema(store)
    limit = max(1, min(100, int(limit or 25)))
    if not writeback_configured():
        with store.connection() as connection:
            pending = int(connection.execute(
                "SELECT COUNT(*) FROM transit_np_records WHERE google_sync_state IN ('pending','error')"
            ).fetchone()[0] or 0)
        return {"configured": False, "pending": pending, "synced": 0, "errors": 0}
    with store.connection() as connection:
        rows = connection.execute(
            """SELECT id FROM transit_np_records
               WHERE google_sync_state IN ('pending','error')
               ORDER BY updated_at ASC, id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    synced = 0
    errors = 0
    for row in rows:
        ok, _ = push_record_to_google(store, int(row[0] or 0))
        if ok:
            synced += 1
        else:
            errors += 1
    with store.connection() as connection:
        pending = int(connection.execute(
            "SELECT COUNT(*) FROM transit_np_records WHERE google_sync_state IN ('pending','error')"
        ).fetchone()[0] or 0)
    return {"configured": True, "pending": pending, "synced": synced, "errors": errors}


def ensure_schema(store: Any) -> None:
    with store.connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS transit_np_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_row INTEGER NOT NULL DEFAULT 0,
                number TEXT NOT NULL DEFAULT '',
                employee TEXT NOT NULL DEFAULT '',
                attached_date TEXT NOT NULL DEFAULT '',
                removed_date TEXT NOT NULL DEFAULT '',
                vehicle_type TEXT NOT NULL DEFAULT '',
                vehicle_count TEXT NOT NULL DEFAULT '',
                plate TEXT NOT NULL DEFAULT '',
                seal_count TEXT NOT NULL DEFAULT '',
                seal TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                status_raw TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                transport_status TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '',
                source_active INTEGER NOT NULL DEFAULT 1,
                source_hash TEXT NOT NULL DEFAULT '',
                source_synced_at TEXT NOT NULL DEFAULT '',
                google_sync_state TEXT NOT NULL DEFAULT '',
                google_sync_error TEXT NOT NULL DEFAULT '',
                google_synced_at TEXT NOT NULL DEFAULT '',
                google_request_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_transit_np_seal ON transit_np_records(seal);
            CREATE INDEX IF NOT EXISTS idx_transit_np_plate ON transit_np_records(plate);
            CREATE INDEX IF NOT EXISTS idx_transit_np_status ON transit_np_records(status);
            CREATE INDEX IF NOT EXISTS idx_transit_np_removed_date ON transit_np_records(removed_date);
            CREATE INDEX IF NOT EXISTS idx_transit_np_source_row ON transit_np_records(source_row);
            """
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(transit_np_records)").fetchall()}
        if "source_active" not in columns:
            connection.execute("ALTER TABLE transit_np_records ADD COLUMN source_active INTEGER NOT NULL DEFAULT 1")
        if "source_hash" not in columns:
            connection.execute("ALTER TABLE transit_np_records ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''")
        if "source_synced_at" not in columns:
            connection.execute("ALTER TABLE transit_np_records ADD COLUMN source_synced_at TEXT NOT NULL DEFAULT ''")
        if "google_sync_state" not in columns:
            connection.execute("ALTER TABLE transit_np_records ADD COLUMN google_sync_state TEXT NOT NULL DEFAULT ''")
        if "google_sync_error" not in columns:
            connection.execute("ALTER TABLE transit_np_records ADD COLUMN google_sync_error TEXT NOT NULL DEFAULT ''")
        if "google_synced_at" not in columns:
            connection.execute("ALTER TABLE transit_np_records ADD COLUMN google_synced_at TEXT NOT NULL DEFAULT ''")
        if "google_request_id" not in columns:
            connection.execute("ALTER TABLE transit_np_records ADD COLUMN google_request_id TEXT NOT NULL DEFAULT ''")
        # Existing records that were created/edited inside the system before 1.00.6.88
        # are queued once for Google write-back. Imported source rows are left untouched.
        connection.execute(
            """UPDATE transit_np_records
               SET google_sync_state='pending',
                   google_request_id=CASE WHEN google_request_id='' THEN lower(hex(randomblob(16))) ELSE google_request_id END
               WHERE google_sync_state=''
                 AND updated_by NOT IN ('', 'Импорт Excel', 'Синхронизация Google Sheets', 'Синхронизация источника')"""
        )


def ensure_seeded(store: Any, snapshot_path: str | Path) -> int:
    ensure_schema(store)
    path = Path(snapshot_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    records = payload.get("records", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        return 0
    with store.connection() as connection:
        existing = int(connection.execute("SELECT COUNT(*) FROM transit_np_records").fetchone()[0] or 0)
        if existing:
            return existing
        now = _now()
        for raw in records[:5000]:
            if not isinstance(raw, dict):
                continue
            values = _record_values(raw)
            connection.execute(
                """INSERT INTO transit_np_records
                   (source_row, number, employee, attached_date, removed_date, vehicle_type,
                    vehicle_count, plate, seal_count, seal, reason, status, status_raw, note,
                    transport_status, location, created_at, updated_at, created_by, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(raw.get("row", 0) or 0),
                    _clean(raw.get("number", ""), 40),
                    values["employee"], values["attached_date"], values["removed_date"],
                    values["vehicle_type"], values["vehicle_count"], values["plate"],
                    values["seal_count"], values["seal"], values["reason"], values["status"],
                    _clean(raw.get("status_raw", ""), 160), values["note"], values["transport_status"],
                    values["location"], now, now, "Импорт Excel", "Импорт Excel",
                ),
            )
        return int(connection.execute("SELECT COUNT(*) FROM transit_np_records").fetchone()[0] or 0)


def _source_digest(raw: dict[str, object], values: dict[str, str]) -> str:
    payload = {
        "number": _clean(raw.get("number", ""), 40),
        "status_raw": _clean(raw.get("status_raw", ""), 160),
        **values,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sync_source_records(store: Any, records: list[dict[str, object]], *, actor: str = "Синхронизация источника") -> dict[str, int]:
    """Merge source-managed rows while preserving locally created rows (source_row=0).

    Source rows are authoritative: when the external spreadsheet changes, matching
    source rows are updated. Rows removed from the source are hidden, not deleted.
    """
    ensure_schema(store)
    now = _now()
    actor = _clean(actor, 100) or "Синхронизация источника"
    inserted = 0
    updated = 0
    seen: set[int] = set()
    with store.connection() as connection:
        existing_rows = connection.execute(
            "SELECT * FROM transit_np_records WHERE source_row>0"
        ).fetchall()
        existing = {int(row["source_row"]): row for row in existing_rows if int(row["source_row"] or 0) > 0}
        for raw in records[:5000]:
            if not isinstance(raw, dict):
                continue
            try:
                source_row = int(raw.get("row", 0) or 0)
            except (TypeError, ValueError):
                source_row = 0
            if source_row <= 0:
                continue
            seen.add(source_row)
            values = _record_values(raw)
            raw_status = _clean(raw.get("status_raw", values.get("status", "")), 160)
            number = _clean(raw.get("number", ""), 40)
            digest = _source_digest(raw, values)
            current = existing.get(source_row)
            if current:
                sync_state = str(current["google_sync_state"] or "")
                if sync_state in {"pending", "error"}:
                    # A local user edit has not reached Google yet. Do not overwrite it
                    # with the older remote row; the background write-back will retry.
                    connection.execute(
                        "UPDATE transit_np_records SET source_active=1, source_synced_at=? WHERE source_row=?",
                        (now, source_row),
                    )
                    continue
                if sync_state == "awaiting_source":
                    current_item = dict(current)
                    local_values = _record_values(current_item)
                    local_digest = _source_digest(current_item, local_values)
                    if local_digest == digest:
                        connection.execute(
                            """UPDATE transit_np_records
                               SET source_active=1, source_hash=?, source_synced_at=?,
                                   google_sync_state='synced', google_sync_error=''
                               WHERE source_row=?""",
                            (digest, now, source_row),
                        )
                        continue
                    try:
                        synced_at = datetime.fromisoformat(str(current["google_synced_at"] or ""))
                        age = max(0.0, (datetime.now(timezone.utc) - synced_at).total_seconds())
                    except (TypeError, ValueError):
                        age = 999999.0
                    if age < 300:
                        # Google export can lag a little after Apps Script writes.
                        connection.execute(
                            "UPDATE transit_np_records SET source_active=1, source_synced_at=? WHERE source_row=?",
                            (now, source_row),
                        )
                        continue
                    connection.execute(
                        "UPDATE transit_np_records SET google_sync_state='synced', google_sync_error='' WHERE source_row=?",
                        (source_row,),
                    )
                changed = str(current["source_hash"] or "") != digest or int(current["source_active"] or 0) != 1
                if changed:
                    connection.execute(
                        """UPDATE transit_np_records SET
                           number=?, employee=?, attached_date=?, removed_date=?, vehicle_type=?, vehicle_count=?,
                           plate=?, seal_count=?, seal=?, reason=?, status=?, status_raw=?, note=?, transport_status=?,
                           location=?, updated_at=?, updated_by=?, source_active=1, source_hash=?, source_synced_at=?
                           WHERE source_row=?""",
                        (
                            number, values["employee"], values["attached_date"], values["removed_date"],
                            values["vehicle_type"], values["vehicle_count"], values["plate"], values["seal_count"],
                            values["seal"], values["reason"], values["status"], raw_status, values["note"],
                            values["transport_status"], values["location"], now, actor, digest, now, source_row,
                        ),
                    )
                    updated += 1
                else:
                    connection.execute(
                        "UPDATE transit_np_records SET source_active=1, source_synced_at=? WHERE source_row=?",
                        (now, source_row),
                    )
            else:
                connection.execute(
                    """INSERT INTO transit_np_records
                       (source_row, number, employee, attached_date, removed_date, vehicle_type, vehicle_count,
                        plate, seal_count, seal, reason, status, status_raw, note, transport_status, location,
                        created_at, updated_at, created_by, updated_by, source_active, source_hash, source_synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        source_row, number, values["employee"], values["attached_date"], values["removed_date"],
                        values["vehicle_type"], values["vehicle_count"], values["plate"], values["seal_count"],
                        values["seal"], values["reason"], values["status"], raw_status, values["note"],
                        values["transport_status"], values["location"], now, now, actor, actor, digest, now,
                    ),
                )
                inserted += 1
        if seen:
            placeholders = ",".join("?" for _ in seen)
            connection.execute(
                f"UPDATE transit_np_records SET source_active=0, source_synced_at=? WHERE source_row>0 AND google_sync_state NOT IN ('pending','error','awaiting_source') AND source_row NOT IN ({placeholders})",
                (now, *sorted(seen)),
            )
        hidden = int(connection.execute(
            "SELECT COUNT(*) FROM transit_np_records WHERE source_row>0 AND source_active=0"
        ).fetchone()[0] or 0)
    try:
        store.add_audit(
            "transit_np_sync", actor, "Синхронизация журнала НП", "transit_np", "source",
            f"Строк: {len(seen)}; добавлено: {inserted}; обновлено: {updated}; скрыто: {hidden}",
        )
    except Exception:
        pass
    return {"rows": len(seen), "inserted": inserted, "updated": updated, "hidden": hidden}


def _record_values(source: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in EDITABLE_FIELDS:
        limit = MAX_LENGTHS[field]
        value = _clean(source.get(field, ""), limit)
        if field in {"attached_date", "removed_date"}:
            value = normalize_date(value)
        result[field] = value
    return result


def _row_dict(row: Any) -> dict[str, object]:
    item = dict(row)
    item["attached_date_display"] = display_date(item.get("attached_date", ""))
    item["removed_date_display"] = display_date(item.get("removed_date", ""))
    return item


def list_records(store: Any) -> list[dict[str, object]]:
    ensure_schema(store)
    with store.connection() as connection:
        rows = connection.execute(
            """SELECT * FROM transit_np_records
               WHERE source_row=0 OR source_active=1
               ORDER BY CASE WHEN source_row=0 THEN 0 ELSE 1 END,
                        CASE WHEN source_row=0 THEN id END DESC,
                        source_row ASC, id ASC"""
        ).fetchall()
    return [_row_dict(row) for row in rows]


def get_record(store: Any, record_id: int) -> dict[str, object] | None:
    ensure_schema(store)
    if record_id <= 0:
        return None
    with store.connection() as connection:
        row = connection.execute("SELECT * FROM transit_np_records WHERE id=?", (record_id,)).fetchone()
    return _row_dict(row) if row else None


def _next_number(connection: Any) -> str:
    rows = connection.execute("SELECT number FROM transit_np_records").fetchall()
    values: list[int] = []
    for row in rows:
        raw = str(row[0] or "").strip()
        if raw.isdigit():
            values.append(int(raw))
    return str((max(values) if values else 0) + 1)


def create_record(store: Any, form: dict[str, str], actor: str) -> tuple[bool, str, int]:
    ensure_schema(store)
    values = _record_values(form)
    if not values["seal"] and not values["plate"]:
        return False, "Укажите номер НП или ГРНЗ / номер перевозки", 0
    now = _now()
    actor = _clean(actor, 100) or "Сотрудник"
    with store.connection() as connection:
        number = _next_number(connection)
        cursor = connection.execute(
            """INSERT INTO transit_np_records
               (source_row, number, employee, attached_date, removed_date, vehicle_type,
                vehicle_count, plate, seal_count, seal, reason, status, status_raw, note,
                transport_status, location, created_at, updated_at, created_by, updated_by)
               VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                number, values["employee"], values["attached_date"], values["removed_date"],
                values["vehicle_type"], values["vehicle_count"], values["plate"], values["seal_count"],
                values["seal"], values["reason"], values["status"], values["status"], values["note"],
                values["transport_status"], values["location"], now, now, actor, actor,
            ),
        )
        record_id = int(cursor.lastrowid or 0)
        _mark_writeback_pending(connection, record_id)
    try:
        store.add_audit(
            "transit_np", actor, "Добавлена запись НП", "transit_np", record_id,
            f"НП: {values['seal'] or '—'}; ГРНЗ/перевозка: {values['plate'] or '—'}",
        )
    except Exception:
        pass
    google_ok, google_message = push_record_to_google(store, record_id)
    if google_ok:
        return True, f"Запись №{number} добавлена и сохранена в Google Sheets", record_id
    return True, f"Запись №{number} добавлена локально. {google_message}", record_id


def update_record(store: Any, record_id: int, form: dict[str, str], actor: str) -> tuple[bool, str]:
    ensure_schema(store)
    if record_id <= 0:
        return False, "Запись не найдена"
    values = _record_values(form)
    if not values["seal"] and not values["plate"]:
        return False, "Укажите номер НП или ГРНЗ / номер перевозки"
    expected = _clean(form.get("expected_updated_at", ""), 80)
    now = _now()
    actor = _clean(actor, 100) or "Сотрудник"
    with store.connection() as connection:
        current = connection.execute("SELECT id, updated_at, number FROM transit_np_records WHERE id=?", (record_id,)).fetchone()
        if not current:
            return False, "Запись не найдена"
        if expected and str(current["updated_at"] or "") != expected:
            return False, "Запись уже изменил другой сотрудник. Обновите страницу и повторите редактирование"
        cursor = connection.execute(
            """UPDATE transit_np_records SET
               employee=?, attached_date=?, removed_date=?, vehicle_type=?, vehicle_count=?,
               plate=?, seal_count=?, seal=?, reason=?, status=?, status_raw=?, note=?,
               transport_status=?, location=?, updated_at=?, updated_by=?
               WHERE id=?""",
            (
                values["employee"], values["attached_date"], values["removed_date"], values["vehicle_type"],
                values["vehicle_count"], values["plate"], values["seal_count"], values["seal"],
                values["reason"], values["status"], values["status"], values["note"],
                values["transport_status"], values["location"], now, actor, record_id,
            ),
        )
        if not cursor.rowcount:
            return False, "Запись не найдена"
        _mark_writeback_pending(connection, record_id)
        number = str(current["number"] or record_id)
    try:
        store.add_audit(
            "transit_np", actor, "Изменена запись НП", "transit_np", record_id,
            f"Запись №{number}; НП: {values['seal'] or '—'}; ГРНЗ/перевозка: {values['plate'] or '—'}",
        )
    except Exception:
        pass
    google_ok, google_message = push_record_to_google(store, record_id)
    if google_ok:
        return True, f"Запись №{number} сохранена и обновлена в Google Sheets"
    return True, f"Запись №{number} сохранена локально. {google_message}"


def delete_record(store: Any, record_id: int, actor: str) -> tuple[bool, str]:
    """Delete a transport record from Google Sheets (when present) and SQLite.

    Google is cleared first so a source-managed row cannot reappear on the next
    synchronization. The sheet row itself is kept in place, only A:P are cleared,
    which preserves source row numbers for all other records.
    """
    ensure_schema(store)
    if record_id <= 0:
        return False, "Запись не найдена"

    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM transit_np_records WHERE id=?", (record_id,)
        ).fetchone()
        if not row:
            return False, "Запись не найдена"
        item = dict(row)

    number = _clean(item.get("number", ""), 40) or str(record_id)
    source_row = int(item.get("source_row", 0) or 0)
    request_id = _clean(item.get("google_request_id", ""), 80)
    has_google_reference = source_row >= 2 or bool(request_id)

    if has_google_reference:
        if not writeback_configured():
            return False, "Google Sheets API недоступен. Удаление отменено, чтобы запись не появилась снова"
        try:
            result = queue_google_sheets.clear_record(
                source=_source_url(),
                gid=_writeback_gid(),
                row=source_row,
                request_id=request_id,
                expected_number=number,
            )
            # Imported/source-managed rows must have a concrete Google row.
            if source_row >= 2 and not bool(result.get("deleted")):
                return False, "Строка в Google Sheets не найдена. Обновите журнал и повторите удаление"
        except Exception as error:
            return False, _clean(str(error), 240) or "Не удалось удалить запись из Google Sheets"

    actor = _clean(actor, 100) or "Администратор"
    seal = _clean(item.get("seal", ""), 120)
    plate = _clean(item.get("plate", ""), 120)
    with store.connection() as connection:
        cursor = connection.execute("DELETE FROM transit_np_records WHERE id=?", (record_id,))
        if not cursor.rowcount:
            return False, "Запись не найдена"

    try:
        store.add_audit(
            "transit_np", actor, "Удалена запись НП", "transit_np", record_id,
            f"Запись №{number}; НП: {seal or '—'}; ГРНЗ/перевозка: {plate or '—'}",
        )
    except Exception:
        pass
    suffix = " и Google Sheets" if has_google_reference else ""
    return True, f"Запись №{number} удалена из журнала{suffix}"


def summary(records: list[dict[str, object]]) -> dict[str, object]:
    dates = [str(row.get("removed_date") or "") for row in records if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("removed_date") or ""))]
    locations = {str(row.get("location") or "").strip() for row in records if str(row.get("location") or "").strip()}
    employees = {str(row.get("employee") or "").strip() for row in records if str(row.get("employee") or "").strip()}
    return {
        "record_count": len(records),
        "date_from": display_date(min(dates)) if dates else "",
        "date_to": display_date(max(dates)) if dates else "",
        "location_count": len(locations),
        "employee_count": len(employees),
    }


def export_csv_bytes(records: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";")
    writer.writerow([
        "№", "Сотрудник", "Дата установки", "Дата снятия", "Тип ТС", "Кол-во ТС",
        "ГРНЗ / № перевозки", "Кол-во НП", "НП", "Причина", "Статус НП",
        "Примечание", "Статус перевозки", "Место снятия НП", "Последнее изменение", "Изменил",
    ])
    for row in records:
        writer.writerow([
            str(row.get("number") or ""),
            str(row.get("employee") or ""),
            display_date(row.get("attached_date", "")),
            display_date(row.get("removed_date", "")),
            str(row.get("vehicle_type") or ""),
            str(row.get("vehicle_count") or ""),
            str(row.get("plate") or ""),
            str(row.get("seal_count") or ""),
            str(row.get("seal") or ""),
            str(row.get("reason") or ""),
            str(row.get("status") or ""),
            str(row.get("note") or ""),
            str(row.get("transport_status") or ""),
            str(row.get("location") or ""),
            str(row.get("updated_at") or ""),
            str(row.get("updated_by") or ""),
        ])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")
