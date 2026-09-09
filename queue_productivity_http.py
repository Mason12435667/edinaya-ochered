from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlencode, urlparse

import queue_productivity as qp


def _value(query, key, default=""):
    return query.get(key, [default])[0]


def _bool(query, key):
    return _value(query, key) in {"1", "true", "on", "yes"}


def _csrf(handler, app, form) -> bool:
    if secrets.compare_digest(str(form.get("csrf_token", "")), str(app.ADMIN_FORM_TOKEN)):
        return True
    handler.json_response({"error": "Обновите страницу и попробуйте снова"}, 403)
    return False


def _send_bytes(handler, body: bytes, content_type: str, filename: str = ""):
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.end_headers()
    handler.wfile.write(body)


def _format_ts(value: object) -> str:
    try:
        ts = int(value or 0)
        if not ts:
            return ""
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(qp.ALMATY).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _search_page(handler, app, query):
    q = _value(query, "q")[:200]
    try: page = max(1, int(_value(query, "page", "1") or 1))
    except ValueError: page = 1
    filters = {
        "date_from": _value(query, "date_from")[:20],
        "date_to": _value(query, "date_to")[:20],
        "chat_type": _value(query, "chat_type")[:20],
        "sender": _value(query, "sender")[:160],
        "employee": _value(query, "employee")[:160],
        "attachment": _value(query, "attachment")[:20],
        "mentions_only": _bool(query, "mentions_only"),
        "voice_only": _bool(query, "voice_only"),
    }
    found = qp.advanced_search(app.STORE, q, page=page, **filters)
    cards=[]
    for row in found["results"]:
        chat_id=str(row.get("chat_id") or "")
        group=chat_id.endswith("@g.us")
        route="/groups" if group else "/whatsapp"
        href=route+"?"+urlencode({"chat_id":chat_id,"message_id":str(row.get("message_key") or "")})
        body=str(row.get("body") or row.get("media_name") or "[Сообщение]")
        transcript=str(row.get("transcript") or "")
        media=str(row.get("media_mime") or "")
        meta=["Группа" if group else "Личный чат",_format_ts(row.get("message_timestamp"))]
        if row.get("sender"): meta.append("Отправитель: "+str(row.get("sender")))
        if row.get("employee"): meta.append("Сотрудник: "+str(row.get("employee")))
        if media: meta.append(media)
        cards.append(
            f'<a class="search-card productivity-search-card" href="{app.e(href)}">'
            f'<div class="search-card-head"><strong>{app.e(row.get("name") or chat_id)}</strong><span class="search-card-meta">{app.e(" · ".join(x for x in meta if x))}</span></div>'
            f'<p>{app.e(body[:500])}</p>'
            f'{f"<p class=muted>Расшифровка: {app.e(transcript[:500])}</p>" if transcript else ""}'
            f'<span class="search-open">Перейти к сообщению ↗</span></a>'
        )
    params={"q":q,**{k:v for k,v in filters.items() if v}}
    params={k:("1" if v is True else str(v)) for k,v in params.items() if v not in {"",False,None}}
    previous=""; more=""
    if page>1:
        previous=f'<a class="button" href="/search?{app.e(urlencode({**params,"page":page-1}))}">← Назад</a>'
    if found.get("has_more"):
        more=f'<a class="button" href="/search?{app.e(urlencode({**params,"page":page+1}))}">Далее →</a>'
    content=f"""
      <section class="search-heading"><p class="eyebrow">Сообщения и контакты</p><h1>Расширенный поиск WhatsApp</h1>
      <p>Поиск по тексту, датам, типу чата, отправителю, сотруднику и вложениям</p></section>
      <form class="panel productivity-search-form" method="get" action="/search">
        <label class="wide">Поиск<input name="q" maxlength="200" value="{app.e(q)}" placeholder="Имя, телефон или текст"></label>
        <label>С даты<input type="date" name="date_from" value="{app.e(filters['date_from'])}"></label>
        <label>По дату<input type="date" name="date_to" value="{app.e(filters['date_to'])}"></label>
        <label>Чат<select name="chat_type"><option value="">Все</option><option value="personal" {'selected' if filters['chat_type']=='personal' else ''}>Личный</option><option value="group" {'selected' if filters['chat_type']=='group' else ''}>Группа</option></select></label>
        <label>Отправитель<input name="sender" value="{app.e(filters['sender'])}" placeholder="Имя или номер"></label>
        <label>Сотрудник<input name="employee" value="{app.e(filters['employee'])}" placeholder="Кто отправлял"></label>
        <label>Вложение<select name="attachment"><option value="">Любое</option><option value="image" {'selected' if filters['attachment']=='image' else ''}>Фото</option><option value="video" {'selected' if filters['attachment']=='video' else ''}>Видео</option><option value="audio" {'selected' if filters['attachment']=='audio' else ''}>Голосовое</option><option value="file" {'selected' if filters['attachment']=='file' else ''}>Файл</option></select></label>
        <label class="check"><input type="checkbox" name="mentions_only" value="1" {'checked' if filters['mentions_only'] else ''}> Только упоминания</label>
        <label class="check"><input type="checkbox" name="voice_only" value="1" {'checked' if filters['voice_only'] else ''}> Только голосовые</label>
        <div class="productivity-search-actions"><button class="button primary">Найти</button><a class="button ghost" href="/search">Сбросить</a><a class="button ghost" href="/bookmarks">Закладки</a></div>
      </form>
      <section class="search-results">{''.join(cards) or '<p class="search-empty">Ничего не найдено. Можно использовать фильтры даже без текста поиска.</p>'}</section>
      <nav class="search-pages">{previous}{more}</nav>
    """
    handler.html_response(app.layout("Поиск",content,"search",handler.is_admin()))


def _bookmarks_page(handler, app):
    rows=qp.list_bookmarks(app.STORE,300); cards=[]
    for row in rows:
        chat_id=str(row.get("chat_id") or ""); mid=str(row.get("message_key") or "")
        href=("/groups" if chat_id.endswith("@g.us") else "/whatsapp")+"?"+urlencode({"chat_id":chat_id,"message_id":mid})
        body=str(row.get("body") or row.get("media_name") or ("Сообщение удалено" if row.get("deleted") else "Сообщение"))
        cards.append(
            f'<article class="panel bookmark-card"><div><strong>{app.e(row.get("chat_name") or chat_id)}</strong><small>{app.e(_format_ts(row.get("message_timestamp")))}</small></div>'
            f'<p>{app.e(body[:700])}</p><div class="bookmark-actions"><a class="button compact" href="{app.e(href)}">Открыть сообщение</a>'
            f'<button class="button compact ghost" data-bookmark-link data-chat-id="{app.e(chat_id)}" data-message-id="{app.e(mid)}">Привязать к заявке</button>'
            f'<button class="button compact ghost" data-bookmark-remove data-chat-id="{app.e(chat_id)}" data-message-id="{app.e(mid)}">Убрать</button></div></article>'
        )
    content=f'<section class="page-heading"><div><p class="eyebrow">WhatsApp</p><h1>Закладки сообщений</h1><p>Важные сообщения, которые можно быстро открыть позже</p></div></section><section class="bookmark-grid">{"".join(cards) or "<p class=muted>Закладок пока нет</p>"}</section>'
    handler.html_response(app.layout("Закладки",content,"bookmarks",handler.is_admin()))


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
        cards.append(f"""<tr class="backup-row"><td><div class="backup-name"><span class="backup-icon">DB</span><strong>{app.e(row['name'])}</strong></div></td><td>{app.e(size)}</td><td><span class="backup-status {'ok' if row['ok'] else 'error'}"><i></i>{status}</span></td><td><span class="backup-detail">{app.e(row['detail'])}</span></td><td><form method="post" action="/admin/backups" data-backup-restore-form><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="restore"><input type="hidden" name="name" value="{app.e(row['name'])}"><button class="button compact backup-restore" {'disabled' if not row['ok'] else ''}>Восстановить</button></form></td></tr>""")
    content=f"""
      {f'<div class="notice">{app.e(notice)}</div>' if notice else ''}
      <section class="page-heading backup-heading"><div><p class="eyebrow">Надёжность</p><h1>Резервные копии базы</h1><p>Создание, проверка целостности и безопасное восстановление SQLite</p></div>
      <form method="post" action="/admin/backups" class="backup-create-form"><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="create"><button class="button primary backup-create"><span>＋</span> Создать копию сейчас</button></form></section>
      <section class="backup-summary"><article class="panel backup-summary-card"><span class="backup-summary-icon">◫</span><div><small>Хранилище</small><strong>SQLite</strong><span>Локальные резервные копии</span></div></article><article class="panel backup-summary-card"><span class="backup-summary-icon">✓</span><div><small>Проверка</small><strong>Автоматическая</strong><span>Целостность каждой копии</span></div></article><article class="panel backup-summary-card"><span class="backup-summary-icon">↶</span><div><small>Восстановление</small><strong>Со страховкой</strong><span>Перед откатом создаётся копия</span></div></article></section>
      <section class="panel table-panel backup-table-panel"><div class="section-heading backup-section-heading"><div><h2>Доступные копии</h2><p>Восстанавливать можно только копии, прошедшие проверку</p></div></div><div class="table-scroll"><table class="backup-table"><thead><tr><th>Копия</th><th>Размер</th><th>Проверка</th><th>Результат</th><th></th></tr></thead><tbody>{''.join(cards) or '<tr><td colspan="5" class="empty">Копий пока нет</td></tr>'}</tbody></table></div></section>
    """
    handler.html_response(app.admin_layout("Резервные копии",content,"backups"))


def _reminders_page(handler, app):
    rows=qp.list_reminders(app.STORE,False,200); cards=[]
    for r in rows:
        due=qp._parse_iso(r.get('remind_at',''))
        local=due.astimezone(qp.ALMATY).strftime('%d.%m.%Y %H:%M') if due else str(r.get('remind_at') or '')
        cards.append(f'<article class="panel reminder-card"><div><strong>Заявка #{r["ticket_id"]}</strong><time>{app.e(local)}</time></div><h3>{app.e(r.get("title") or "")}</h3><p>{app.e(r.get("note") or "Без заметки")}</p><div><a class="button compact" href="/ticket?id={r["ticket_id"]}">Открыть</a><button class="button compact ghost" data-reminder-done="{r["id"]}">Выполнено</button></div></article>')
    content=f'<section class="page-heading"><div><p class="eyebrow">Заявки</p><h1>Напоминания</h1><p>Запланированные напоминания сотрудников</p></div></section><section class="reminder-grid">{"".join(cards) or "<p class=muted>Активных напоминаний нет</p>"}</section>'
    handler.html_response(app.layout("Напоминания",content,"reminders",handler.is_admin()))


def get(handler, app):
    parsed=urlparse(handler.path); query=parse_qs(parsed.query); path=parsed.path
    if path=="/search": _search_page(handler,app,query)
    elif path=="/bookmarks": _bookmarks_page(handler,app)
    elif path=="/reminders": _reminders_page(handler,app)
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
    if not _csrf(handler,app,form): return True
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
