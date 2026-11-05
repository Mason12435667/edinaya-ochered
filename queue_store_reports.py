"""Audit log, user error reports and template error records.

Mixed into ticketing.TicketStore, which provides connection() and the other stores' helpers.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from queue_core import normalize_message, utc_now


class ReportStore:
    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        event_type: str,
        actor: str,
        action: str,
        object_type: str = "",
        object_id: str | int = "",
        details: str = "",
        level: str = "info",
        created_at: str = "",
    ) -> None:
        safe_level = level if level in {"info", "warning", "error"} else "info"
        connection.execute(
            """INSERT INTO audit_log
               (event_type, actor, action, object_type, object_id, details, level, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalize_message(event_type)[:60] or "action",
                normalize_message(actor)[:100],
                normalize_message(action)[:300],
                normalize_message(object_type)[:60],
                normalize_message(str(object_id))[:120],
                normalize_message(details)[:1600],
                safe_level,
                created_at or utc_now(),
            ),
        )

    def add_audit(
        self,
        event_type: str,
        actor: str,
        action: str,
        object_type: str = "",
        object_id: str | int = "",
        details: str = "",
        level: str = "info",
    ) -> None:
        with self.connection() as connection:
            self._audit(connection, event_type, actor, action, object_type, object_id, details, level)

    def list_audit_entries(self, limit: int = 200, level: str = "", query: str = "") -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if level in {"info", "warning", "error"}:
            conditions.append("level = ?")
            params.append(level)
        clean_query = normalize_message(query).casefold()
        if clean_query:
            conditions.append("(FOLD(actor) LIKE ? OR FOLD(action) LIKE ? OR FOLD(details) LIKE ? OR FOLD(object_id) LIKE ?)")
            term = f"%{clean_query}%"
            params.extend([term, term, term, term])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(max(1, min(int(limit), 1000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_error_report(
        self, *, sender: str, phone: str = "", chat_id: str = "", external_id: str = "",
        description: str, attachment_name: str = ""
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        sender = normalize_message(sender)[:160] or "Неизвестный отправитель"
        phone = normalize_message(phone)[:40]
        chat_id = normalize_message(chat_id)[:120]
        external_id = normalize_message(external_id)[:160]
        description = normalize_message(description)[:32000]
        attachment_name = normalize_message(attachment_name)[:240]
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO error_reports
                    (sender, phone, chat_id, external_id, description, attachment_name, status, admin_note, created_at, updated_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, 'new', '', ?, ?, '')
                """,
                (sender, phone, chat_id, external_id, description, attachment_name, now, now),
            )
            report_id = int(cursor.lastrowid)
            self._audit(
                connection, "error_report", sender, "Репорт об ошибке создан",
                "error_report", report_id, description[:300], created_at=now,
            )
        return report_id

    def get_error_report(self, report_id: int) -> dict[str, Any] | None:
        try:
            report_id = int(report_id)
        except (TypeError, ValueError):
            return None
        if report_id <= 0:
            return None
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM error_reports WHERE id = ?", (report_id,)).fetchone()
        return dict(row) if row else None

    def list_error_reports(self, status: str = "", query: str = "", limit: int = 300) -> list[dict[str, Any]]:
        valid_statuses = {"new", "in_progress", "resolved", "rejected"}
        clauses: list[str] = []
        params: list[Any] = []
        if status in valid_statuses:
            clauses.append("status = ?")
            params.append(status)
        search = normalize_message(query).strip()
        if search:
            folded = f"%{search.casefold()}%"
            digits = re.sub(r"\D", "", search)
            search_clauses = [
                "FOLD(sender) LIKE ?", "FOLD(description) LIKE ?",
                "FOLD(attachment_name) LIKE ?", "CAST(id AS TEXT) LIKE ?",
            ]
            params.extend([folded, folded, folded, f"%{search}%"])
            if digits:
                search_clauses.append("DIGITS(phone) LIKE ?")
                params.append(f"%{digits}%")
            clauses.append("(" + " OR ".join(search_clauses) + ")")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(1000, int(limit or 300))))
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM error_reports{where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def error_report_counts(self) -> dict[str, int]:
        result = {"new": 0, "in_progress": 0, "resolved": 0, "rejected": 0}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM error_reports GROUP BY status"
            ).fetchall()
        for row in rows:
            result[str(row["status"])] = int(row["count"] or 0)
        return result

    def update_error_report(self, report_id: int, status: str, admin_note: str, actor: str = "") -> bool:
        valid_statuses = {"new", "in_progress", "resolved", "rejected"}
        if status not in valid_statuses:
            status = "new"
        try:
            report_id = int(report_id)
        except (TypeError, ValueError):
            return False
        if report_id <= 0:
            return False
        now = datetime.now(timezone.utc).isoformat()
        resolved_at = now if status in {"resolved", "rejected"} else ""
        note = normalize_message(admin_note)[:3000]
        actor = normalize_message(actor)[:160] or "Администратор"
        with self.connection() as connection:
            current = connection.execute("SELECT id FROM error_reports WHERE id = ?", (report_id,)).fetchone()
            if not current:
                return False
            connection.execute(
                "UPDATE error_reports SET status = ?, admin_note = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
                (status, note, now, resolved_at, report_id),
            )
            self._audit(
                connection, "error_report", actor, f"Репорт: {status}",
                "error_report", report_id, note[:300], created_at=now,
            )
        return True

    def add_template_error(
        self,
        sender: str,
        phone: str,
        missing_fields: list[str],
        reply_body: str,
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO template_errors
                    (sender, phone, missing_fields, reply_body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sender, phone, ", ".join(missing_fields), reply_body, utc_now()),
            )
            return int(cursor.lastrowid)

    def update_template_error_delivery(
        self,
        error_id: int,
        delivery_status: str,
        provider_id: str = "",
        error: str = "",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE template_errors
                SET delivery_status = ?, provider_id = ?, error = ?
                WHERE id = ?
                """,
                (delivery_status, provider_id, error, error_id),
            )
