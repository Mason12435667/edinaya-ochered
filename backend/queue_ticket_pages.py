"""Данные статических страниц заявок из frontend/pages; страницы рисуют их сами."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import quote

import queue_contextual_tickets
import queue_operations
from queue_core import LOCAL_TIMEZONE, display_phone, human_time, normalize_phone, parse_iso_datetime
from queue_work_context import (assignment_names, pagination_data, query_page, sla_view, ticket_row_data,
                                ticket_sla_state, work_actor)
from ticketing import PRIORITIES, STATUSES, TicketStore


def _pairs(mapping: Mapping[str, str]) -> list[list[str]]:
    return [[str(key), str(label)] for key, label in mapping.items()]


def _employees(selected: str = "") -> dict[str, object]:
    names = assignment_names()
    return {"names": names, "selected": selected if selected in names else work_actor()}


class TicketPages:
    def __init__(self, store: TicketStore, *, categories: Mapping[str, str], page_size: int):
        self.store = store
        self.categories = categories
        self.page_size = page_size

    def _dashboard_tickets(self, query, filters, employee):
        args = (filters["status"], filters["category"], filters["priority"], filters["q"])
        minutes = filters["no_answer_minutes"]
        if minutes:
            found = [
                item for item in self.store.list_tickets(*args, 5000, 0, filters["view"], employee)
                if ticket_sla_state(item).get("active")
                and int(ticket_sla_state(item).get("elapsed_seconds", 0) or 0) >= minutes * 60
            ]
            total = len(found)
            pages = max(1, (total + self.page_size - 1) // self.page_size)
            page = min(query_page(query), pages)
            return found[(page - 1) * self.page_size:page * self.page_size], total, page
        total = self.store.count_tickets(*args, filters["view"], employee)
        pages = max(1, (total + self.page_size - 1) // self.page_size)
        page = min(query_page(query), pages)
        return self.store.list_tickets(*args, self.page_size, (page - 1) * self.page_size, filters["view"], employee), total, page

    def dashboard(self, query: dict[str, list[str]]) -> dict[str, object]:
        view = query.get("view", [""])[0]
        try:
            minutes = max(0, min(10080, int(query.get("no_answer_minutes", ["0"])[0] or 0)))
        except ValueError:
            minutes = 0
        filters = {
            "view": view if view in {"", "mine", "handoff", "open", "urgent"} else "",
            "q": query.get("q", [""])[0],
            "status": query.get("status", [""])[0],
            "category": query.get("category", [""])[0],
            "priority": query.get("priority", [""])[0],
            "no_answer_minutes": minutes,
        }
        employee = work_actor()
        tickets, total, page = self._dashboard_tickets(query, filters, employee)
        shift = self.store.shift_summary(employee)
        handoff = [item for item in self.store.list_tickets("", "", "", "", 500, 0) if int(item.get("shift_handoff", 0) or 0)][:20]
        return {
            "version": self.store.dashboard_version(),
            "counts": self.store.counts(),
            "rows": [ticket_row_data(ticket) for ticket in tickets],
            "pagination": pagination_data(query, total, page),
            "filters": filters,
            "options": {
                "statuses": _pairs(STATUSES),
                "categories": _pairs(self.categories),
                "priorities": _pairs(PRIORITIES),
                "employees": _employees(),
            },
            "shift": {
                "employee": employee,
                "previous_employee": str(shift.get("previous_employee", "") or ""),
                "views": [
                    {"key": key, "label": label, "count": int(shift.get(key, 0) or 0)}
                    for key, label in (("mine", "Мои заявки"), ("handoff", "От предыдущей смены"),
                                       ("open", "Незакрытые"), ("urgent", "Срочные"))
                ],
            },
            "handoff": [{
                "id": int(item["id"]),
                "title": str(item.get("title", "Заявка")),
                "sender": str(item.get("sender", "")),
                "handoff_from": str(item.get("handoff_from", "")),
                "handoff_at": human_time(str(item.get("handoff_at", ""))),
            } for item in handoff],
        }

    @staticmethod
    def _history(item: dict[str, object]) -> dict[str, object]:
        field_name = str(item.get("field_name", ""))
        new_value = str(item.get("new_value", ""))
        text = str(item.get("text", ""))
        media = str(item.get("media_name", ""))
        return {
            "id": int(item.get("id", 0) or 0),
            "event": str(item.get("event_type", "")).replace("_", " "),
            "created_at": human_time(str(item.get("created_at", ""))),
            "source": str(item.get("source", "")),
            "field_label": queue_contextual_tickets.FIELD_LABELS.get(field_name, field_name) if field_name else "",
            "old_value": str(item.get("old_value", "")),
            "new_value": new_value,
            "text": text,
            "media_name": media,
            "can_apply_field": bool(new_value or text or media),
            "can_attach": bool(str(item.get("message_key", "")) and str(item.get("chat_id", ""))),
        }

    def _media(self, ticket: dict[str, object], ticket_id: int, chat_id: str) -> list[dict[str, object]]:
        items = []
        for media in self.store.list_ticket_whatsapp_media(ticket_id, chat_id, str(ticket.get("external_id", "") or ""), 24):
            key = str(media.get("message_key", "") or "")
            if not key:
                continue
            mime = str(media.get("media_mime", "") or "")
            sender = str(media.get("sender", "") or ("Рабочий WhatsApp" if media.get("from_me") else ticket.get("sender", "Пользователь")))
            try:
                when = datetime.fromtimestamp(int(media.get("message_timestamp", 0) or 0), tz=timezone.utc).astimezone(LOCAL_TIMEZONE).strftime("%d.%m %H:%M")
            except (ValueError, TypeError, OSError):
                when = ""
            items.append({
                "url": f"/api/chat-media?chat_id={quote(str(media.get('chat_id', chat_id) or chat_id))}&message_id={quote(key)}",
                "kind": mime.split("/", 1)[0] if mime.split("/", 1)[0] in {"image", "audio", "video"} else "file",
                "name": str(media.get("media_name", "") or "Вложение"),
                "meta": " · ".join(part for part in (sender, when) if part),
                "transcript": str(media.get("transcript", "") or "") if mime.startswith("audio/") else "",
            })
        return items

    def _sla(self, ticket: dict[str, object]) -> dict[str, object] | None:
        state = ticket_sla_state(ticket)
        if state.get("active"):
            priority = str(ticket.get("priority", "normal"))
            return {"badge": sla_view(ticket), "overdue": bool(state.get("overdue")),
                    "priority_label": PRIORITIES.get(priority, priority),
                    "limit_minutes": int(state.get("limit_minutes", 0) or 0)}
        first_response = parse_iso_datetime(ticket.get("first_response_at"))
        created = parse_iso_datetime(ticket.get("created_at"))
        if first_response and created:
            return {"reacted_minutes": max(0, int((first_response - created).total_seconds() // 60))}
        return None

    def ticket(self, ticket_id: int, show_admin: bool) -> dict[str, object]:
        ticket = self.store.get_ticket(ticket_id)
        if not ticket:
            return {"found": False}
        chat_id = str(ticket.get("chat_id", "") or "").strip()
        if not chat_id:
            digits = re.sub(r"\D", "", normalize_phone(str(ticket.get("phone", ""))))
            chat_id = f"{digits}@c.us" if digits else ""
        terminal = str(ticket["status"]) in {"done", "invalid"}
        handoff = bool(int(ticket.get("shift_handoff", 0) or 0))
        media = self._media(ticket, ticket_id, chat_id)
        return {
            "found": True,
            "id": int(ticket["id"]),
            "revision": int(ticket.get("revision", 0)),
            "title": str(ticket["title"]),
            "summary": str(ticket["summary"]),
            "status": str(ticket["status"]),
            "status_label": STATUSES.get(str(ticket["status"]), str(ticket["status"])),
            "priority": str(ticket["priority"]),
            "priority_label": PRIORITIES.get(str(ticket["priority"]), str(ticket["priority"])),
            "source": str(ticket["source"]),
            "sender": str(ticket["sender"]),
            "phone": display_phone(str(ticket["phone"])),
            "category_label": str(self.categories.get(ticket["category"], ticket["category"])),
            "created_at": human_time(str(ticket["created_at"])),
            "original_text": str(ticket["original_text"] or ticket["summary"]),
            "chat_id": chat_id,
            "media": media,
            "attachment_name": "" if media else str(ticket.get("attachment_name") or ""),
            "context_fields": [{
                "label": queue_contextual_tickets.FIELD_LABELS.get(str(item.get("field_name", "")), str(item.get("field_name", ""))),
                "value": str(item.get("value", "")),
                "confidence": int(float(item.get("confidence", 0) or 0) * 100),
                "source": str(item.get("source", "")),
            } for item in queue_contextual_tickets.ticket_fields(self.store, ticket_id) if str(item.get("value", "")).strip()],
            "field_options": _pairs(queue_contextual_tickets.FIELD_LABELS),
            "history": [self._history(item) for item in queue_contextual_tickets.ticket_history(self.store, ticket_id, 120)],
            "status_locked": terminal and not show_admin,
            "statuses": _pairs(STATUSES),
            "priorities": _pairs(PRIORITIES),
            "employees": _employees(str(ticket["assigned_to"])),
            "sla": self._sla(ticket),
            "handoff": None if terminal else {
                "pending": handoff,
                "from": str(ticket.get("handoff_from", "")),
                "at": human_time(str(ticket.get("handoff_at", ""))),
                "history": queue_operations.handoff_history(self.store, ticket_id),
            },
            "handoff_badge": handoff,
            "notification": queue_operations.notification_status(self.store, ticket_id),
            "events": [{"action": str(event["action"]), "actor": str(event["actor"]), "created_at": human_time(event["created_at"])}
                       for event in self.store.ticket_events(ticket_id)],
        }
