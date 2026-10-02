from __future__ import annotations

from datetime import timezone

from queue_core import ALMATY_TIMEZONE, parse_iso_datetime


class ReportingService:
    """Ticket filtering, operational analytics and unified audit data."""

    def __init__(self, store, auth):
        self.store = store
        self.auth = auth

    @staticmethod
    def _one(query: dict[str, list[str]], key: str) -> str:
        values = query.get(key, [""])
        return str(values[0] if values else "" or "")

    def filter_tickets(self, query: dict[str, list[str]], *, limit: int = 5000) -> list[dict[str, object]]:
        status = self._one(query, "status")
        category = self._one(query, "category")
        priority = self._one(query, "priority")
        search = self._one(query, "q")
        employee = self._one(query, "employee").strip()
        date_from = self._one(query, "date_from").strip()
        date_to = self._one(query, "date_to").strip()
        rows = self.store.list_tickets(status, category, priority, search, max(1, min(5000, int(limit or 5000))), 0)

        def in_range(item: dict[str, object]) -> bool:
            if employee and str(item.get("assigned_to") or "") != employee:
                return False
            created = parse_iso_datetime(item.get("created_at"))
            if created:
                local_date = created.astimezone(ALMATY_TIMEZONE).date().isoformat()
                if date_from and local_date < date_from:
                    return False
                if date_to and local_date > date_to:
                    return False
            elif date_from or date_to:
                return False
            return True

        return [dict(item) for item in rows if in_range(item)]

    def ticket_resolution_minutes(self, ticket: dict[str, object]) -> int | None:
        created = parse_iso_datetime(ticket.get("created_at"))
        if not created:
            return None
        finished = None
        for value in (ticket.get("closed_at"), ticket.get("completed_at"), ticket.get("updated_at")):
            parsed = parse_iso_datetime(value)
            if parsed:
                finished = parsed
                break
        if not finished and str(ticket.get("status") or "") in {"done", "invalid"}:
            try:
                events = self.store.ticket_events(int(ticket.get("id", 0) or 0))
                if events:
                    finished = parse_iso_datetime(events[-1].get("created_at"))
            except Exception:
                finished = None
        return max(0, int((finished - created).total_seconds() // 60)) if finished else None

    def analytics_snapshot(self, query: dict[str, list[str]]) -> dict[str, object]:
        tickets = self.filter_tickets(query)
        by_employee: dict[str, dict[str, int]] = {}
        by_category: dict[str, int] = {}
        first_values: list[int] = []
        resolution_values: list[int] = []
        handoffs = 0
        for ticket in tickets:
            employee = str(ticket.get("assigned_to") or "Не назначено")
            bucket = by_employee.setdefault(employee, {"total": 0, "open": 0, "done": 0, "invalid": 0, "handoff": 0})
            bucket["total"] += 1
            status = str(ticket.get("status") or "")
            if status in {"new", "in_progress"}:
                bucket["open"] += 1
            if status == "done":
                bucket["done"] += 1
            if status == "invalid":
                bucket["invalid"] += 1
            if int(ticket.get("shift_handoff", 0) or 0):
                bucket["handoff"] += 1
                handoffs += 1
            category = str(ticket.get("category") or ticket.get("title") or "Без категории")
            by_category[category] = by_category.get(category, 0) + 1
            created = parse_iso_datetime(ticket.get("created_at"))
            first = parse_iso_datetime(ticket.get("first_response_at"))
            if created and first:
                first_values.append(max(0, int((first - created).total_seconds() // 60)))
            resolution = self.ticket_resolution_minutes(ticket)
            if resolution is not None and status in {"done", "invalid"}:
                resolution_values.append(resolution)
        return {
            "tickets": tickets,
            "total": len(tickets),
            "open": sum(1 for x in tickets if str(x.get("status") or "") in {"new", "in_progress"}),
            "done": sum(1 for x in tickets if str(x.get("status") or "") == "done"),
            "invalid": sum(1 for x in tickets if str(x.get("status") or "") == "invalid"),
            "handoffs": handoffs,
            "avg_first": int(sum(first_values) / len(first_values)) if first_values else 0,
            "avg_resolution": int(sum(resolution_values) / len(resolution_values)) if resolution_values else 0,
            "by_employee": by_employee,
            "by_category": dict(sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0].casefold()))),
        }

    def unified_audit_events(self, query: dict[str, list[str]], limit: int = 400) -> list[dict[str, str]]:
        actor_q = self._one(query, "actor").strip().casefold()
        action_q = self._one(query, "action").strip().casefold()
        events: list[dict[str, str]] = []
        try:
            for item in self.store.list_audit_entries(600, "", ""):
                events.append({
                    "created_at": str(item.get("created_at") or ""),
                    "actor": str(item.get("actor") or "Система"),
                    "action": str(item.get("action") or item.get("message") or ""),
                    "object": str(item.get("object") or item.get("target") or ""),
                    "source": "Система",
                })
        except Exception:
            pass
        for item in self.auth.list_audit_events(600):
            actor = str(item.get("actor_display_name") or item.get("actor_username") or "Система")
            action = str(item.get("action") or "")
            obj = " · ".join(x for x in [str(item.get("object_type") or ""), str(item.get("object_id") or "")] if x)
            details = str(item.get("details") or "")
            if details:
                action += " · " + details
            events.append({"created_at": str(item.get("created_at") or ""), "actor": actor, "action": action, "object": obj, "source": "Доступ"})
        events.sort(key=lambda x: x["created_at"], reverse=True)
        if actor_q:
            events = [x for x in events if actor_q in x["actor"].casefold()]
        if action_q:
            events = [x for x in events if action_q in x["action"].casefold() or action_q in x["object"].casefold()]
        return events[:max(1, min(int(limit or 400), 1000))]
