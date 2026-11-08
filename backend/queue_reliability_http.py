from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import queue_reliability as qr
from queue_avatars import AvatarCache
from queue_core import e, human_bytes, log_suppressed
from ticketing import TicketStore
from queue_templates import render_template


def _value(query,key,default=''):
    return query.get(key,[default])[0]


def _human(n):
    try:return human_bytes(int(n or 0))
    except (TypeError, ValueError):
        log_suppressed("queue_reliability_http._human")
        return str(n or 0)


class ReliabilityRoutes:
    """System checks page, diagnostics, scheduled messages and profile refresh endpoints."""

    def __init__(self, store: TicketStore, monitor: qr.ReliabilityMonitor, *, csrf_token: Callable[[], str],
                 admin_layout: Callable[[str, str, str], str], max_media_bytes: int,
                 active_employee: Callable[[], str], chat_lock: Any, chat_state: dict, avatars: AvatarCache):
        self.store = store
        self.monitor = monitor
        self.csrf_token = csrf_token
        self.admin_layout = admin_layout
        self.max_media_bytes = max_media_bytes
        self.active_employee = active_employee
        self.chat_lock = chat_lock
        self.chat_state = chat_state
        self.avatars = avatars

    def _admin_page(self,handler,query):
        if not handler.require_admin(): return
        notice=_value(query,'notice')
        snap=self.monitor.diagnostic_snapshot()
        db=snap['database']; disk=snap['disk']; conn=snap['connector']; probs=snap['queue_problems']; backups=snap['backups']
        problem_rows=''.join(
            f'''<tr><td>#{int(x['id'])}</td><td>{e(str(x['status']))}</td><td>{e(str(x.get('chat_id','')))}</td>
        <td>{e(str(x.get('media_name') or x.get('body') or '')[:90])}</td><td>{e(str(x.get('last_error') or '')[:180])}</td>
        <td><form method="post" action="/admin/reliability" class="inline-form"><input type="hidden" name="csrf_token" value="{e(self.csrf_token())}"><input type="hidden" name="action" value="retry"><input type="hidden" name="message_id" value="{int(x['id'])}"><button class="button compact" type="submit">Повторить</button></form>
        {f'<form method="post" action="/admin/reliability" class="inline-form"><input type="hidden" name="csrf_token" value="{e(self.csrf_token())}"><input type="hidden" name="action" value="retry_media"><input type="hidden" name="message_id" value="{int(x["id"])}"><button class="button compact ghost" type="submit">Только файл</button></form>' if x.get('media_path') else ''}</td></tr>'''
            for x in probs
        ) or '<tr><td colspan="6" class="muted">Ошибочных или зависших отправок нет</td></tr>'
        backup_rows=''.join(
            f'<tr><td>{e(Path(str(x["path"])).parent.name)}</td><td>{_human(x.get("size",0))}</td><td>{e(str(x.get("modified_at","")).replace("T"," ")[:19])}</td><td><span class="status {"status-done" if x.get("ok") else "status-invalid"}">{"OK" if x.get("ok") else e(str(x.get("detail","Ошибка")))}</span></td></tr>'
            for x in backups
        ) or '<tr><td colspan="4" class="muted">Резервные копии tickets.db не найдены в стандартных каталогах</td></tr>'
        secret=snap['secret_storage']
        maintenance=bool(snap['maintenance'])
        content=render_template(
            "reliability/page.html",
            notice_text=f'<div class="notice">{e(notice)}</div>' if notice else '',
            ok_text='system-ok' if db.get('ok') else 'system-bad',
            ok_text_2='OK' if db.get('ok') else 'Ошибка',
            detail=e(str(db.get('detail',''))),
            connected_text='system-ok' if conn.get('connected') else 'system-bad',
            connected_text_2='Подключён' if conn.get('connected') else 'Недоступен',
            status=e(str(conn.get('status',''))),
            human=_human(disk.get('free',0)),
            human_2=_human(disk.get('total',0)),
            human_3=_human(snap.get('max_media_bytes',0)),
            csrf_token=e(self.csrf_token()),
            maintenance_text='0' if maintenance else '1',
            value_text='danger' if not maintenance else 'primary',
            maintenance_text_2='Выключить обслуживание' if maintenance else 'Включить обслуживание',
            env_text='WEBHOOK_TOKEN берётся из окружения systemd.' if secret.get('env') else 'Сейчас используется совместимый локальный token-файл. Для полной изоляции задайте WEBHOOK_TOKEN через EnvironmentFile systemd; патч не переносит рабочий токен автоматически, чтобы не оборвать коннектор.',
            problem_rows=problem_rows,
            backup_rows=backup_rows,
        )
        handler.html_response(self.admin_layout('Надёжность',content,'reliability'))


    def get(self,handler):
        parsed=urlparse(handler.path); path=parsed.path; query=parse_qs(parsed.query)
        if path=='/admin/reliability':
            self._admin_page(handler,query)
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
