"""Маршруты заявок: список и карточка, смена статуса и приоритета, ответ пользователю, ручная заявка, внешний API."""
from __future__ import annotations

from http import HTTPStatus
from urllib.parse import quote

import queue_contextual_tickets
import queue_realtime
import queue_workflow
from queue_core import valid_conversation_id
from queue_work_context import assignment_names, work_actor


class TicketRoutes:
    def _get_dashboard(self, query) -> None:
        self.page_response("tickets")

    def _get_ticket(self, query) -> None:
        self.page_response("ticket")

    def _get_api_integration_ping(self, query) -> None:
        key = self.integration_api_key()
        if not key:
            return
        self.json_response({"ok":True,"service":"Единая очередь","version":self.deps.app_version,"key":{"name":str(key.get("name") or ""),"scope":str(key.get("scope") or "read")}})

    def _get_api_integration_tickets(self, query) -> None:
        key = self.integration_api_key()
        if not key:
            return
        try:
            limit = max(1, min(500, int(query.get("limit", ["100"])[0] or 100)))
        except (TypeError, ValueError):
            limit = 100
        rows = self.deps.final_ticket_filter(query, limit=5000)[:limit]
        public_rows = []
        for item in rows:
            public_rows.append({
                "id": int(item.get("id",0) or 0),
                "status": str(item.get("status") or ""),
                "category": str(item.get("category") or item.get("title") or ""),
                "priority": str(item.get("priority") or ""),
                "assigned_to": str(item.get("assigned_to") or ""),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "description": str(item.get("description") or item.get("problem") or ""),
            })
        self.json_response({"ok":True,"count":len(public_rows),"tickets":public_rows})

    def _get_api_dashboard_data(self, query) -> None:
        self.json_response(self.ticket_pages.dashboard(query))

    def _get_api_ticket_page(self, query) -> None:
        self.json_response(self.ticket_pages.ticket(self.query_int(query, "id"), self.is_admin()))

    def _post_context_history_apply(self, parsed, form) -> None:
        # POST /context-history/apply
        ticket_id = self.form_int(form, "ticket_id")
        history_id = self.form_int(form, "history_id")
        mode = form.get("mode", "field")
        field_name = form.get("field_name", "problem")
        updated, notice = queue_contextual_tickets.apply_history_item(
            self.deps.store, ticket_id, history_id, field_name, work_actor(), mode=mode
        )
        if not updated and not notice:
            notice = "Не удалось применить запись истории"
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_handoff(self, parsed, form) -> None:
        # POST /handoff
        ticket_id = self.form_int(form, "ticket_id")
        action = form.get("action", "transfer")
        updated = self.deps.store.set_ticket_handoff(
            ticket_id, action != "accept", work_actor()
        )
        notice = (
            "Заявка передана следующей смене"
            if updated and action != "accept"
            else "Заявка принята текущей сменой"
            if updated
            else "Не удалось изменить передачу смене"
        )
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_status(self, parsed, form) -> None:
        # POST /status
        ticket_id = self.form_int(form, "ticket_id")
        ticket = self.deps.store.get_ticket(ticket_id)
        assignee = form.get("employee", "")
        if assignee not in assignment_names():
            assignee = str((ticket or {}).get("assigned_to", "")) or work_actor()
        requested_status = form.get("status", "")
        close_reason = form.get("close_reason", "")
        close_comment = form.get("close_comment", "")
        valid_close, close_error = queue_workflow.validate_close(self.deps.store, ticket_id, requested_status, close_reason, close_comment)
        if not valid_close:
            self.redirect(f"/ticket?id={ticket_id}&notice={quote(close_error)}")
            return
        updated, _message_id = self.deps.update_ticket_status(
            ticket_id,
            requested_status,
            work_actor(),
            assignee,
            allow_reopen=self.is_admin(),
            close_reason=close_reason,
            close_comment=close_comment,
        )
        if updated:
            queue_workflow.record_close_meta(self.deps.store, ticket_id, requested_status, close_reason, close_comment, work_actor())
        notice = "" if updated else "Отработанную заявку может вернуть в работу только администратор"
        suffix = f"&notice={quote(notice)}" if notice else ""
        self.redirect(f"/ticket?id={ticket_id}{suffix}")

    def _post_quick_status(self, parsed, form) -> None:
        # POST /quick-status
        ticket_id = self.form_int(form, "ticket_id")
        ticket = self.deps.store.get_ticket(ticket_id)
        actor = work_actor()
        requested_status = form.get("status", "")
        assignee = actor if requested_status == "in_progress" else (str((ticket or {}).get("assigned_to", "")) or actor)
        close_reason = form.get("close_reason", "")
        close_comment = form.get("close_comment", "")
        valid_close, close_error = queue_workflow.validate_close(self.deps.store, ticket_id, requested_status, close_reason, close_comment)
        if not valid_close:
            self.json_response({"updated":False,"error":close_error}, HTTPStatus.BAD_REQUEST)
            return
        updated, message_id = self.deps.update_ticket_status(
            ticket_id,
            requested_status,
            actor,
            assignee,
            allow_reopen=self.is_admin(),
            close_reason=close_reason,
            close_comment=close_comment,
        )
        if updated:
            queue_workflow.record_close_meta(self.deps.store, ticket_id, requested_status, close_reason, close_comment, actor)
        self.json_response(
            {
                "updated": updated,
                "ticket_id": ticket_id,
                "notification_queued": bool(message_id),
            },
            HTTPStatus.OK if updated else (
                HTTPStatus.FORBIDDEN
                if ticket and str(ticket.get("status", "")) in {"done", "invalid"} and not self.is_admin()
                else HTTPStatus.BAD_REQUEST
            ),
        )

    def _post_quick_priority(self, parsed, form) -> None:
        # POST /quick-priority
        ticket_id = self.form_int(form, "ticket_id")
        updated = self.deps.store.update_priority(
            ticket_id,
            form.get("priority", ""),
            work_actor(),
        )
        self.json_response(
            {"updated": updated, "ticket_id": ticket_id},
            HTTPStatus.OK if updated else HTTPStatus.BAD_REQUEST,
        )

    def _post_classification(self, parsed, form) -> None:
        # POST /classification
        ticket_id = self.form_int(form, "ticket_id")
        updated = self.deps.store.update_priority(
            ticket_id,
            form.get("priority", ""),
            work_actor(),
        )
        notice = "Приоритет сохранён" if updated else "Не удалось изменить приоритет"
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_send_reply(self, parsed, form) -> None:
        # POST /send-reply
        ticket_id = self.form_int(form, "ticket_id")
        ticket = self.deps.store.get_ticket(ticket_id)
        linked_chat_id = valid_conversation_id(str((ticket or {}).get("chat_id", "") or ""))
        if linked_chat_id and not self.claim_conversation_write(linked_chat_id):
            return
        if form.get("send_custom") == "1":
            reply = form.get("custom_reply", "")
        else:
            reply = form.get("quick_reply", "")
        message_id = self.deps.store.queue_outbound_message(
            ticket_id,
            reply,
            work_actor(),
        )
        if message_id:
            queue_realtime.notify_outbound()
        notice = (
            "Ответ поставлен в очередь отправки через WhatsApp"
            if message_id
            else "Не удалось подготовить ответ: проверьте текст и WhatsApp-адрес"
        )
        self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")

    def _post_manual(self, parsed, form) -> None:
        # POST /manual
        title = form.get("title", "").strip() or "Ручная заявка"
        reference = form.get("reference", "").strip()
        comment = form.get("comment", "").strip()
        summary_parts = [title]
        if reference:
            summary_parts.append(f"№ {reference}")
        if comment:
            summary_parts.append(comment)
        employee = form.get("employee", "")
        if employee not in assignment_names():
            employee = work_actor()
        manual_payload = {
            "source": "manual",
            "sender": employee,
            "category": "general",
            "priority": form.get("priority", "normal"),
            "title": title,
            "summary": " · ".join(summary_parts),
            "original_text": comment,
            "status": form.get("status", "new"),
            "assigned_to": employee,
        }
        similar = self.deps.store.find_similar_open_ticket(manual_payload, hours=72)
        if similar:
            similar_id = int(similar.get("id", 0) or 0)
            self.redirect(f"/ticket?id={similar_id}&notice={quote(f'Похожая открытая заявка уже существует: №{similar_id}. Новая заявка не создана')}")
            return
        ticket_id = self.deps.store.create_ticket(manual_payload)
        self.redirect(f"/ticket?id={ticket_id}")
