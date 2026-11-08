from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import queue_productivity as qp
from queue_core import human_bytes
from ticketing import TicketStore


def _value(query, key, default=""):
    return query.get(key, [default])[0]


def _send_bytes(handler, body: bytes, content_type: str, filename: str = ""):
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.end_headers()
    handler.wfile.write(body)


class ProductivityRoutes:
    """Reminders, analytics, backups and chat productivity endpoints."""

    def __init__(self, store: TicketStore, data_dir: Path, database_path: Path, *,
                 category_items: Callable[[], Iterable[tuple[str, str]]],
                 active_employee: Callable[[], str]):
        self.store = store
        self.data_dir = data_dir
        self.database_path = database_path
        self.category_items = category_items
        self.active_employee = active_employee

    def analytics_data(self):
        data = qp.analytics(self.store)
        return {
            "title": "Аналитика", "active": "analytics",
            "total": data["total"], "open": data["open"], "closed": data["closed"],
            "avg_first_minutes": str(data["avg_first_minutes"]), "avg_solution_minutes": str(data["avg_solution_minutes"]),  # JSON would turn 0.0 into 0
            "by_category": [list(item) for item in data["by_category"]],
            "by_employee": [list(item) for item in data["by_employee"]],
        }

    def backups_data(self):
        return {"title": "Резервные копии", "active": "backups", "rows": [
            {"name": row["name"], "size": human_bytes(row["size"]), "ok": bool(row["ok"]), "detail": row["detail"]}
            for row in qp.list_backups(self.data_dir)
        ]}

    def reminders_data(self):
        rows = []
        for reminder in qp.list_reminders(self.store, False, 200):
            due = qp._parse_iso(reminder.get("remind_at", ""))
            rows.append({
                "id": reminder["id"], "ticket_id": reminder["ticket_id"],
                "due": due.astimezone(qp.LOCAL_TZ).strftime("%d.%m.%Y %H:%M") if due else str(reminder.get("remind_at") or ""),
                "title": reminder.get("title") or "", "note": reminder.get("note") or "Без заметки",
            })
        return {"title": "Напоминания", "rows": rows}

    def get(self, handler):
        parsed=urlparse(handler.path); query=parse_qs(parsed.query); path=parsed.path
        # /search and /bookmarks are redirected to /whatsapp by QueueHTTPHandlerMixin.do_GET.
        if path=="/reminders": handler.page_response("app")
        elif path=="/admin/analytics":
            if handler.require_admin(): handler.page_response("admin")
        elif path=="/admin/analytics.csv":
            if not handler.require_admin(): return True
            _send_bytes(handler,qp.analytics_csv(self.store),"text/csv; charset=utf-8","queue-analytics.csv")
        elif path=="/admin/backups":
            if handler.require_admin(): handler.page_response("admin")
        elif path=="/api/productivity-state":
            chat_id=_value(query,"chat_id")[:120]
            handler.json_response({"contact":qp.get_contact_meta(self.store,chat_id),"mute":qp.mute_state(self.store,chat_id)})
        elif path=="/api/productivity-categories":
            handler.json_response({"categories":[[str(key),str(label)] for key,label in self.category_items()]})
        elif path=="/api/open-tickets": handler.json_response({"tickets":qp.open_tickets(self.store,150)})
        elif path=="/api/chat-gallery": handler.json_response({"items":qp.chat_gallery(self.store,_value(query,"chat_id"),300)})
        elif path=="/api/chat-find": handler.json_response({"results":qp.chat_find(self.store,_value(query,"chat_id"),_value(query,"q"),250)})
        elif path=="/api/ticket-reminders":
            try: tid=int(_value(query,"ticket_id","0") or 0)
            except ValueError: tid=0
            rows=[r for r in qp.list_reminders(self.store,False,300) if int(r.get('ticket_id') or 0)==tid] if tid else qp.list_reminders(self.store,False,100)
            handler.json_response({"reminders":rows})
        else: return False
        return True

    def post(self, handler):
        path=urlparse(handler.path).path
        known={"/api/message-bookmark","/api/contact-meta","/api/chat-mark-unread","/api/chat-mute","/api/message-ticket-create","/api/message-ticket-link","/api/ticket-reminder","/api/reminder-done","/admin/backups"}
        if path not in known: return False
        if path=="/admin/backups" and not handler.require_admin(): return True
        form=handler.read_form()
        try:
            if path=="/api/message-bookmark":
                state=qp.toggle_bookmark(self.store,form.get('chat_id',''),form.get('message_id',''),self.active_employee())
                handler.json_response({"bookmarked":state})
            elif path=="/api/contact-meta":
                meta=qp.save_contact_meta(self.store,form.get('chat_id',''),form.get('organization',''),form.get('post',''),form.get('important')=='1',form.get('note',''),form.get('auto_reply_blocked')=='1')
                handler.json_response({"saved":True,"contact":meta})
            elif path=="/api/chat-mark-unread":
                handler.json_response({"updated":qp.mark_unread(self.store,form.get('chat_id',''))})
            elif path=="/api/chat-mute":
                handler.json_response({"mute":qp.set_chat_mute(self.store,form.get('chat_id',''),form.get('mode',''),self.active_employee())})
            elif path=="/api/message-ticket-create":
                tid=qp.create_ticket_from_message(self.store,form.get('chat_id',''),form.get('message_id',''),form.get('category','general'),self.active_employee())
                handler.json_response({"created":True,"ticket_id":tid,"href":f"/ticket?id={tid}"})
            elif path=="/api/message-ticket-link":
                ok=qp.link_message_to_ticket(self.store,form.get('chat_id',''),form.get('message_id',''),int(form.get('ticket_id','0') or 0))
                handler.json_response({"linked":ok},200 if ok else 400)
            elif path=="/api/ticket-reminder":
                rid=qp.add_reminder(self.store,int(form.get('ticket_id','0') or 0),form.get('remind_at',''),form.get('note',''),self.active_employee())
                handler.json_response({"created":True,"id":rid})
            elif path=="/api/reminder-done":
                handler.json_response({"updated":qp.complete_reminder(self.store,int(form.get('id','0') or 0))})
            elif path=="/admin/backups":
                action=form.get('action','')
                if action=='create':
                    path_obj=qp.create_database_backup(self.database_path,self.data_dir,'manual')
                    handler.redirect('/admin/backups?notice='+quote('Создана копия '+path_obj.name))
                elif action=='restore':
                    pre=qp.restore_database_backup(self.database_path,self.data_dir,form.get('name',''))
                    handler.redirect('/admin/backups?notice='+quote('База восстановлена. Страховочная копия: '+pre.name))
                else: handler.redirect('/admin/backups?notice='+quote('Неизвестное действие'))
        except Exception as exc:
            if path=="/admin/backups": handler.redirect('/admin/backups?notice='+quote('Ошибка: '+str(exc)[:180]))
            else: handler.json_response({"error":str(exc)[:220]},400)
        return True
