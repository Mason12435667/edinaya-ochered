from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import queue_reliability as qr


def _value(query,key,default=''):
    return query.get(key,[default])[0]


def _csrf(handler, app, form) -> bool:
    if secrets.compare_digest(str(form.get('csrf_token','')),str(app.ADMIN_FORM_TOKEN)):
        return True
    handler.json_response({'error':'Обновите страницу и попробуйте снова'},403)
    return False


def _human(app,n):
    try:return app.human_bytes(int(n or 0))
    except Exception:return str(n or 0)


def _admin_page(handler,app,query):
    if not handler.require_admin(): return
    notice=_value(query,'notice')
    snap=qr.diagnostic_snapshot(app)
    db=snap['database']; disk=snap['disk']; conn=snap['connector']; probs=snap['queue_problems']; backups=snap['backups']
    problem_rows=''.join(
        f'''<tr><td>#{int(x['id'])}</td><td>{app.e(str(x['status']))}</td><td>{app.e(str(x.get('chat_id','')))}</td>
        <td>{app.e(str(x.get('media_name') or x.get('body') or '')[:90])}</td><td>{app.e(str(x.get('last_error') or '')[:180])}</td>
        <td><form method="post" action="/admin/reliability" class="inline-form"><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="retry"><input type="hidden" name="message_id" value="{int(x['id'])}"><button class="button compact" type="submit">Повторить</button></form>
        {f'<form method="post" action="/admin/reliability" class="inline-form"><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="retry_media"><input type="hidden" name="message_id" value="{int(x["id"])}"><button class="button compact ghost" type="submit">Только файл</button></form>' if x.get('media_path') else ''}</td></tr>'''
        for x in probs
    ) or '<tr><td colspan="6" class="muted">Ошибочных или зависших отправок нет</td></tr>'
    backup_rows=''.join(
        f'<tr><td>{app.e(Path(str(x["path"])).parent.name)}</td><td>{_human(app,x.get("size",0))}</td><td>{app.e(str(x.get("modified_at","")).replace("T"," ")[:19])}</td><td><span class="status {"status-done" if x.get("ok") else "status-invalid"}">{"OK" if x.get("ok") else app.e(str(x.get("detail","Ошибка")))}</span></td></tr>'
        for x in backups
    ) or '<tr><td colspan="4" class="muted">Резервные копии tickets.db не найдены в стандартных каталогах</td></tr>'
    secret=snap['secret_storage']
    maintenance=bool(snap['maintenance'])
    content=f'''
      {f'<div class="notice">{app.e(notice)}</div>' if notice else ''}
      <section class="page-heading"><div><p class="eyebrow">Надёжность и диагностика</p><h1>Контроль системы</h1><p>Проверки базы, очереди, бэкапов, ограничений файлов и режим обслуживания.</p></div></section>
      <section class="reliability-grid">
        <article class="panel reliability-card"><span>База данных</span><strong class="{'system-ok' if db.get('ok') else 'system-bad'}">{'OK' if db.get('ok') else 'Ошибка'}</strong><small>{app.e(str(db.get('detail','')))}</small></article>
        <article class="panel reliability-card"><span>WhatsApp</span><strong class="{'system-ok' if conn.get('connected') else 'system-bad'}">{'Подключён' if conn.get('connected') else 'Недоступен'}</strong><small>{app.e(str(conn.get('status','')))}</small></article>
        <article class="panel reliability-card"><span>Свободно на диске</span><strong>{_human(app,disk.get('free',0))}</strong><small>из {_human(app,disk.get('total',0))}</small></article>
        <article class="panel reliability-card"><span>Лимит вложения</span><strong>{_human(app,snap.get('max_media_bytes',0))}</strong><small>опасные исполняемые расширения блокируются до отправки</small></article>
      </section>
      <section class="panel reliability-maintenance"><div><h2>Режим обслуживания</h2><p>При включении сотрудники могут просматривать систему, но изменения и отправка сообщений блокируются. Коннектор продолжает синхронизацию.</p></div>
        <form method="post" action="/admin/reliability"><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="maintenance"><input type="hidden" name="enabled" value="{'0' if maintenance else '1'}"><button class="button {'danger' if not maintenance else 'primary'}" type="submit">{'Выключить обслуживание' if maintenance else 'Включить обслуживание'}</button></form></section>
      <section class="panel"><h2>Хранение секретов</h2><p>{'WEBHOOK_TOKEN берётся из окружения systemd.' if secret.get('env') else 'Сейчас используется совместимый локальный token-файл. Для полной изоляции задайте WEBHOOK_TOKEN через EnvironmentFile systemd; патч не переносит рабочий токен автоматически, чтобы не оборвать коннектор.'}</p></section>
      <section class="panel"><div class="section-heading"><div><h2>Неотправленные / зависшие</h2><p>Точная причина ошибки и ручной повтор. «Только файл» создаёт новую отправку без повторения текста.</p></div></div><div class="table-scroll"><table><thead><tr><th>ID</th><th>Статус</th><th>Чат</th><th>Сообщение</th><th>Причина</th><th>Действия</th></tr></thead><tbody>{problem_rows}</tbody></table></div></section>
      <section class="panel"><div class="section-heading"><div><h2>Проверка резервных копий</h2><p>SQLite quick_check выполняется только на чтение.</p></div></div><div class="table-scroll"><table><thead><tr><th>Копия</th><th>Размер</th><th>Дата</th><th>Проверка</th></tr></thead><tbody>{backup_rows}</tbody></table></div></section>
    '''
    handler.html_response(app.admin_layout('Надёжность',content,'reliability'))


def get(handler,app):
    parsed=urlparse(handler.path); path=parsed.path; query=parse_qs(parsed.query)
    if path=='/admin/reliability':
        _admin_page(handler,app,query)
    elif path=='/api/reliability-scheduled':
        chat_id=_value(query,'chat_id')[:120]
        handler.json_response({'items':qr.scheduled_messages(app.STORE,chat_id,100)})
    elif path=='/api/reliability-message-info':
        item=qr.message_info(app.STORE,_value(query,'chat_id')[:120],_value(query,'message_id')[:180])
        handler.json_response({'message':item} if item else {'error':'Сообщение не найдено'},200 if item else 404)
    elif path=='/api/reliability-diagnostics':
        if not handler.require_admin(): return True
        handler.json_response(qr.diagnostic_snapshot(app))
    elif path=='/api/reliability-upload-policy':
        handler.json_response({'max_bytes':int(app.MAX_MEDIA_BYTES),'blocked_extensions':sorted(qr.DANGEROUS_EXTENSIONS)})
    else:
        return False
    return True


def post(handler,app):
    path=urlparse(handler.path).path
    known={'/api/reliability-schedule','/api/reliability-scheduled-cancel','/api/reliability-profile-refresh','/admin/reliability'}
    if path not in known:return False
    form=handler.read_form()
    if not _csrf(handler,app,form):return True
    try:
        if path=='/api/reliability-schedule':
            sid=qr.schedule_message(app.STORE,form.get('chat_id',''),form.get('body',''),app.active_employee(),form.get('due_at',''))
            handler.json_response({'scheduled':True,'id':sid})
        elif path=='/api/reliability-scheduled-cancel':
            ok=qr.cancel_scheduled(app.STORE,int(form.get('id','0') or 0))
            handler.json_response({'cancelled':ok},200 if ok else 404)
        elif path=='/api/reliability-profile-refresh':
            qr.request_profile_refresh(app,form.get('chat_id',''))
            handler.json_response({'requested':True})
        elif path=='/admin/reliability':
            if not handler.require_admin():return True
            action=form.get('action',''); notice=''
            if action=='maintenance':
                qr.set_maintenance(app.STORE,form.get('enabled','0') in {'1','true','on','yes'}); notice='Режим обслуживания обновлён'
            elif action in {'retry','retry_media'}:
                new_id=qr.retry_outbound(app.STORE,int(form.get('message_id','0') or 0),action=='retry_media'); notice=f'Сообщение поставлено на повтор: #{new_id}'
            else: notice='Неизвестное действие'
            handler.redirect('/admin/reliability?notice='+quote(notice))
    except Exception as exc:
        if path=='/admin/reliability': handler.redirect('/admin/reliability?notice='+quote('Ошибка: '+str(exc)[:220]))
        else: handler.json_response({'error':str(exc)[:300]},400)
    return True
