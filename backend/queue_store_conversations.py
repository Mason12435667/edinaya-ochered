"""Per-contact bot conversation state: pending menu/category and the ticket draft.

Mixed into ticketing.TicketStore, which provides connection() and the other stores' helpers.
"""
from __future__ import annotations

import json
from typing import Any

from queue_core import normalize_message, utc_now


# pending_category values that mean "the bot is showing its menu", not a category
MENU_CONTEXT = "__menu__"
MENU_GATE_CONTEXT = "__menu_gate__"


class ConversationStore:
    def get_conversation_context(self, contact_key: str) -> dict[str, Any] | None:
        if not contact_key:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_contexts WHERE contact_key = ?",
                (contact_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_conversation_draft(self, contact_key: str) -> dict[str, str]:
        context = self.get_conversation_context(contact_key)
        if not context:
            return {}
        try:
            raw = json.loads(str(context.get("draft_json", "{}") or "{}"))
        except (ValueError, TypeError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): normalize_message(str(value)) for key, value in raw.items() if str(value).strip()}

    def set_conversation_draft(self, contact_key: str, draft: dict[str, str]) -> None:
        if not contact_key:
            return
        clean = {str(key): normalize_message(str(value))[:1800] for key, value in draft.items() if str(value).strip()}
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_contexts WHERE contact_key = ?", (contact_key,)
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE conversation_contexts SET draft_json = ?, updated_at = ? WHERE contact_key = ?",
                    (json.dumps(clean, ensure_ascii=False), now, contact_key),
                )
            else:
                connection.execute(
                    "INSERT INTO conversation_contexts(contact_key, pending_category, active_ticket_id, force_new, draft_json, updated_at) VALUES (?, '', 0, 0, ?, ?)",
                    (contact_key, json.dumps(clean, ensure_ascii=False), now),
                )

    def clear_conversation_draft(self, contact_key: str) -> None:
        if not contact_key:
            return
        with self.connection() as connection:
            connection.execute(
                "UPDATE conversation_contexts SET draft_json = '{}', updated_at = ? WHERE contact_key = ?",
                (utc_now(), contact_key),
            )

    def set_conversation_context(
        self,
        contact_key: str,
        pending_category: str = "",
        active_ticket_id: int = 0,
        force_new: bool = False,
    ) -> None:
        if not contact_key:
            return
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO conversation_contexts
                    (contact_key, pending_category, active_ticket_id, force_new, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(contact_key) DO UPDATE SET
                    pending_category = excluded.pending_category,
                    active_ticket_id = excluded.active_ticket_id,
                    force_new = excluded.force_new,
                    updated_at = excluded.updated_at
                """,
                (
                    contact_key,
                    pending_category,
                    active_ticket_id,
                    int(force_new),
                    utc_now(),
                ),
            )
