from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

import queue_productivity as qp
from queue_core import log_suppressed


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


def _analytics_page(handler, app):
    if not handler.require_admin(): return
    data=qp.analytics(app.STORE)
    def rows(items): return ''.join(f'<tr><td>{app.e(name)}</td><td>{count}</td></tr>' for name,count in items) or '<tr><td colspan="2" class="empty">Нет данных</td></tr>'
    content=f"""
      <section class="page-heading"><div><p class="eyebrow">Отчёты</p><h1>Аналитика</h1><p>Статистика заявок без SLA-показателей</p></div><a class="button" href="/admin/analytics.csv">Экспорт CSV (открывается в Excel)</a></section>
      <section class="stats-grid productivity-stats"><article class="stat-card"><span>Всего</span><strong>{data['total']}</strong></article><article class="stat-card"><span>Открытые</span><strong>{data['open']}</strong></article><article class="stat-card"><span>Закрытые</span><strong>{data['closed']}</strong></article><article class="stat-card"><span>Первый ответ</span><strong>{data['avg_first_minutes']} мин</strong></article><article class="stat-card"><span>Решение</span><strong>{data['avg_solution_minutes']} мин</strong></article></section>
      <section class="analytics-grid analytics-grid-two"><div class="panel"><h2>По категориям</h2><div class="table-scroll"><table><thead><tr><th>Категория</th><th>Количество</th></tr></thead><tbody>{rows(data['by_category'])}</tbody></table></div></div>
      <div class="panel"><h2>По сотрудникам</h2><div class="table-scroll"><table><thead><tr><th>Сотрудник</th><th>Количество</th></tr></thead><tbody>{rows(data['by_employee'])}</tbody></table></div></div></section>
    """
    handler.html_response(app.admin_layout("Аналитика",content,"analytics"))


def _backups_page(handler, app, query):
    if not handler.require_admin(): return
    notice=_value(query,"notice")
    cards=[]
    for row in qp.list_backups(app.DATA_DIR):
        size=app.human_bytes(row['size']) if hasattr(app,'human_bytes') else f"{row['size']} Б"
        status='Исправна' if row['ok'] else 'Ошибка'
        cards.append(f"""<tr class="backup-row"><td><div class="backup-name"><span class="backup-icon">DB</span><strong>{app.e(row['name'])}</strong></div></td><td>{app.e(size)}</td><td><span class="backup-status {'ok' if row['ok'] else 'error'}"><i></i>{status}</span></td><td><span class="backup-detail">{app.e(row['detail'])}</span></td><td><form method="post" action="/admin/backups" data-backup-restore-form><input type="hidden" name="csrf_token" value="{app.e(app.csrf_token())}"><input type="hidden" name="action" value="restore"><input type="hidden" name="name" value="{app.e(row['name'])}"><button class="button compact backup-restore" {'disabled' if not row['ok'] else ''}>Восстановить</button></form></td></tr>""")
    content=f"""
      {f'<div class="notice">{app.e(notice)}</div>' if notice else ''}
      <section class="page-heading backup-heading"><div><p class="eyebrow">Надёжность</p><h1>Резервные копии базы</h1><p>Создание, проверка целостности и безопасное восстановление SQLite</p></div>
      <form method="post" action="/admin/backups" class="backup-create-form"><input type="hidden" name="csrf_token" value="{app.e(app.csrf_token())}"><input type="hidden" name="action" value="create"><button class="button primary backup-create"><span>＋</span> Создать копию сейчас</button></form></section>
      <section class="backup-summary"><article class="panel backup-summary-card"><span class="backup-summary-icon">◫</span><div><small>Хранилище</small><strong>SQLite</strong><span>Локальные резервные копии</span></div></article><article class="panel backup-summary-card"><span class="backup-summary-icon">✓</span><div><small>Проверка</small><strong>Автоматическая</strong><span>Целостность каждой копии</span></div></article><article class="panel backup-summary-card"><span class="backup-summary-icon">↶</span><div><small>Восстановление</small><strong>Со страховкой</strong><span>Перед откатом создаётся копия</span></div></article></section>
      <section class="panel table-panel backup-table-panel"><div class="section-heading backup-section-heading"><div><h2>Доступные копии</h2><p>Восстанавливать можно только копии, прошедшие проверку</p></div></div><div class="table-scroll"><table class="backup-table"><thead><tr><th>Копия</th><th>Размер</th><th>Проверка</th><th>Результат</th><th></th></tr></thead><tbody>{''.join(cards) or '<tr><td colspan="5" class="empty">Копий пока нет</td></tr>'}</tbody></table></div></section>
    """
    handler.html_response(app.admin_layout("Резервные копии",content,"backups"))


def _reminders_page(handler, app):
    rows=qp.list_reminders(app.STORE,False,200); cards=[]
    for r in rows:
        due=qp._parse_iso(r.get('remind_at',''))
        local=due.astimezone(qp.LOCAL_TZ).strftime('%d.%m.%Y %H:%M') if due else str(r.get('remind_at') or '')
        cards.append(f'<article class="panel reminder-card"><div><strong>Заявка #{r["ticket_id"]}</strong><time>{app.e(local)}</time></div><h3>{app.e(r.get("title") or "")}</h3><p>{app.e(r.get("note") or "Без заметки")}</p><div><a class="button compact" href="/ticket?id={r["ticket_id"]}">Открыть</a><button class="button compact ghost" data-reminder-done="{r["id"]}">Выполнено</button></div></article>')
    content=f'<section class="page-heading"><div><p class="eyebrow">Заявки</p><h1>Напоминания</h1><p>Запланированные напоминания сотрудников</p></div></section><section class="reminder-grid">{"".join(cards) or "<p class=muted>Активных напоминаний нет</p>"}</section>'
    handler.html_response(app.layout("Напоминания",content,"reminders",handler.is_admin()))


def get(handler, app):
    parsed=urlparse(handler.path); query=parse_qs(parsed.query); path=parsed.path
    # /search and /bookmarks are redirected to /whatsapp by QueueHTTPHandlerMixin.do_GET.
    if path=="/reminders": _reminders_page(handler,app)
    elif path=="/admin/analytics": _analytics_page(handler,app)
    elif path=="/admin/analytics.csv":
        if not handler.require_admin(): return True
        _send_bytes(handler,qp.analytics_csv(app.STORE),"text/csv; charset=utf-8","queue-analytics.csv")
    elif path=="/admin/backups": _backups_page(handler,app,query)
    elif path=="/api/productivity-state":
        chat_id=_value(query,"chat_id")[:120]
        handler.json_response({"contact":qp.get_contact_meta(app.STORE,chat_id),"mute":qp.mute_state(app.STORE,chat_id)})
    elif path=="/api/productivity-categories":
        try:
            categories=[[str(key),str(label)] for key,label in app.active_ticket_category_items()]
        except Exception:
            log_suppressed("queue_productivity_http.get")
            categories=[["general","Другая проблема"],["support","Поддержка"]]
        handler.json_response({"categories":categories})
    elif path=="/api/open-tickets": handler.json_response({"tickets":qp.open_tickets(app.STORE,150)})
    elif path=="/api/chat-gallery": handler.json_response({"items":qp.chat_gallery(app.STORE,_value(query,"chat_id"),300)})
    elif path=="/api/chat-find": handler.json_response({"results":qp.chat_find(app.STORE,_value(query,"chat_id"),_value(query,"q"),250)})
    elif path=="/api/ticket-reminders":
        try: tid=int(_value(query,"ticket_id","0") or 0)
        except ValueError: tid=0
        rows=[r for r in qp.list_reminders(app.STORE,False,300) if int(r.get('ticket_id') or 0)==tid] if tid else qp.list_reminders(app.STORE,False,100)
        handler.json_response({"reminders":rows})
    else: return False
    return True


def post(handler, app):
    path=urlparse(handler.path).path
    known={"/api/message-bookmark","/api/contact-meta","/api/chat-mark-unread","/api/chat-mute","/api/message-ticket-create","/api/message-ticket-link","/api/ticket-reminder","/api/reminder-done","/admin/backups"}
    if path not in known: return False
    if path=="/admin/backups" and not handler.require_admin(): return True
    form=handler.read_form()
    try:
        if path=="/api/message-bookmark":
            state=qp.toggle_bookmark(app.STORE,form.get('chat_id',''),form.get('message_id',''),app.active_employee())
            handler.json_response({"bookmarked":state})
        elif path=="/api/contact-meta":
            meta=qp.save_contact_meta(app.STORE,form.get('chat_id',''),form.get('organization',''),form.get('post',''),form.get('important')=='1',form.get('note',''),form.get('auto_reply_blocked')=='1')
            handler.json_response({"saved":True,"contact":meta})
        elif path=="/api/chat-mark-unread":
            handler.json_response({"updated":qp.mark_unread(app.STORE,form.get('chat_id',''))})
        elif path=="/api/chat-mute":
            handler.json_response({"mute":qp.set_chat_mute(app.STORE,form.get('chat_id',''),form.get('mode',''),app.active_employee())})
        elif path=="/api/message-ticket-create":
            tid=qp.create_ticket_from_message(app.STORE,form.get('chat_id',''),form.get('message_id',''),form.get('category','general'),app.active_employee())
            handler.json_response({"created":True,"ticket_id":tid,"href":f"/ticket?id={tid}"})
        elif path=="/api/message-ticket-link":
            ok=qp.link_message_to_ticket(app.STORE,form.get('chat_id',''),form.get('message_id',''),int(form.get('ticket_id','0') or 0))
            handler.json_response({"linked":ok},200 if ok else 400)
        elif path=="/api/ticket-reminder":
            rid=qp.add_reminder(app.STORE,int(form.get('ticket_id','0') or 0),form.get('remind_at',''),form.get('note',''),app.active_employee())
            handler.json_response({"created":True,"id":rid})
        elif path=="/api/reminder-done":
            handler.json_response({"updated":qp.complete_reminder(app.STORE,int(form.get('id','0') or 0))})
        elif path=="/admin/backups":
            action=form.get('action','')
            if action=='create':
                path_obj=qp.create_database_backup(app.DATABASE_PATH,app.DATA_DIR,'manual')
                handler.redirect('/admin/backups?notice='+quote('Создана копия '+path_obj.name))
            elif action=='restore':
                pre=qp.restore_database_backup(app.DATABASE_PATH,app.DATA_DIR,form.get('name',''))
                handler.redirect('/admin/backups?notice='+quote('База восстановлена. Страховочная копия: '+pre.name))
            else: handler.redirect('/admin/backups?notice='+quote('Неизвестное действие'))
    except Exception as exc:
        if path=="/admin/backups": handler.redirect('/admin/backups?notice='+quote('Ошибка: '+str(exc)[:180]))
        else: handler.json_response({"error":str(exc)[:220]},400)
    return True
