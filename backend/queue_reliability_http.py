from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import queue_reliability as qr
from queue_avatars import AvatarCache
from queue_core import human_bytes, log_suppressed
from ticketing import TicketStore


def _value(query,key,default=''):
    return query.get(key,[default])[0]


def _human(n):
    try:return human_bytes(int(n or 0))
    except (TypeError, ValueError):
        log_suppressed("queue_reliability_http._human")
        return str(n or 0)


class ReliabilityRoutes:
    """System checks page, diagnostics, scheduled messages and profile refresh endpoints."""

    def __init__(self, store: TicketStore, monitor: qr.ReliabilityMonitor, *, max_media_bytes: int,
                 active_employee: Callable[[], str], chat_lock: Any, chat_state: dict, avatars: AvatarCache):
        self.store = store
        self.monitor = monitor
        self.max_media_bytes = max_media_bytes
        self.active_employee = active_employee
        self.chat_lock = chat_lock
        self.chat_state = chat_state
        self.avatars = avatars

    def page_data(self):
        snap = self.monitor.diagnostic_snapshot()
        db = snap["database"]; disk = snap["disk"]; conn = snap["connector"]
        maintenance = bool(snap["maintenance"])
        return {
            "title": "Надёжность", "active": "reliability",
            "values": {
                "ok_text": "system-ok" if db.get("ok") else "system-bad",
                "ok_text_2": "OK" if db.get("ok") else "Ошибка",
                "detail": str(db.get("detail", "")),
                "connected_text": "system-ok" if conn.get("connected") else "system-bad",
                "connected_text_2": "Подключён" if conn.get("connected") else "Недоступен",
                "status": str(conn.get("status", "")),
                "human": _human(disk.get("free", 0)),
                "human_2": _human(disk.get("total", 0)),
                "human_3": _human(snap.get("max_media_bytes", 0)),
                "maintenance_text": "0" if maintenance else "1",
                "value_text": "danger" if not maintenance else "primary",
                "maintenance_text_2": "Выключить обслуживание" if maintenance else "Включить обслуживание",
                "env_text": "WEBHOOK_TOKEN берётся из окружения systemd." if snap["secret_storage"].get("env") else "Сейчас используется совместимый локальный token-файл. Для полной изоляции задайте WEBHOOK_TOKEN через EnvironmentFile systemd; патч не переносит рабочий токен автоматически, чтобы не оборвать коннектор.",
            },
            "problems": [{
                "id": int(item["id"]), "status": str(item["status"]), "chat_id": str(item.get("chat_id", "")),
                "body": str(item.get("media_name") or item.get("body") or "")[:90],
                "error": str(item.get("last_error") or "")[:180],
                "has_media": bool(item.get("media_path")),
            } for item in snap["queue_problems"]],
            "backups": [{
                "name": Path(str(item["path"])).parent.name, "size": _human(item.get("size", 0)),
                "modified": str(item.get("modified_at", "")).replace("T", " ")[:19],
                "ok": bool(item.get("ok")), "detail": str(item.get("detail", "Ошибка")),
            } for item in snap["backups"]],
        }

    def get(self,handler):
        parsed=urlparse(handler.path); path=parsed.path; query=parse_qs(parsed.query)
        if path=='/admin/reliability':
            if handler.require_admin(): handler.page_response('admin')
        elif path=='/api/reliability-scheduled':
            chat_id=_value(query,'chat_id')[:120]
            handler.json_response({'items':qr.scheduled_messages(self.store,chat_id,100)})
        elif path=='/api/reliability-message-info':
            item=qr.message_info(self.store,_value(query,'chat_id')[:120],_value(query,'message_id')[:180])
            handler.json_response({'message':item} if item else {'error':'Сообщение не найдено'},200 if item else 404)
        elif path=='/api/reliability-diagnostics':
            if not handler.require_admin(): return True
            handler.json_response(self.monitor.diagnostic_snapshot())
        elif path=='/api/reliability-upload-policy':
            handler.json_response({'max_bytes':int(self.max_media_bytes),'blocked_extensions':sorted(qr.DANGEROUS_EXTENSIONS)})
        else:
            return False
        return True


    def post(self,handler):
        path=urlparse(handler.path).path
        known={'/api/reliability-schedule','/api/reliability-scheduled-cancel','/api/reliability-profile-refresh','/admin/reliability'}
        if path not in known:return False
        form=handler.read_form()
        try:
            if path=='/api/reliability-schedule':
                sid=qr.schedule_message(self.store,form.get('chat_id',''),form.get('body',''),self.active_employee(),form.get('due_at',''))
                handler.json_response({'scheduled':True,'id':sid})
            elif path=='/api/reliability-scheduled-cancel':
                ok=qr.cancel_scheduled(self.store,int(form.get('id','0') or 0))
                handler.json_response({'cancelled':ok},200 if ok else 404)
            elif path=='/api/reliability-profile-refresh':
                qr.request_profile_refresh(self.chat_lock,self.chat_state,self.avatars,form.get('chat_id',''))
                handler.json_response({'requested':True})
            elif path=='/admin/reliability':
                if not handler.require_admin():return True
                action=form.get('action',''); notice=''
                if action=='maintenance':
                    qr.set_maintenance(self.store,form.get('enabled','0') in {'1','true','on','yes'}); notice='Режим обслуживания обновлён'
                elif action in {'retry','retry_media'}:
                    new_id=qr.retry_outbound(self.store,int(form.get('message_id','0') or 0),action=='retry_media'); notice=f'Сообщение поставлено на повтор: #{new_id}'
                else: notice='Неизвестное действие'
                handler.redirect('/admin/reliability?notice='+quote(notice))
        except Exception as exc:
            if path=='/admin/reliability': handler.redirect('/admin/reliability?notice='+quote('Ошибка: '+str(exc)[:220]))
            else: handler.json_response({'error':str(exc)[:300]},400)
        return True
