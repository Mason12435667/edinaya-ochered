"""HTTP endpoints separated from the legacy application entrypoint."""
import secrets
import re
from urllib.parse import urlparse, parse_qs
import queue_features


def excerpt(text, query, limit=300):
    text = str(text or '')
    positions = [text.casefold().find(t) for t in query.casefold().split()]
    hit = min((p for p in positions if p >= 0), default=0)
    start = max(0, hit-75)
    return ('…' if start else '') + text[start:start+limit] + ('…' if start+limit<len(text) else '')


def marked(app, text, query):
    tokens = sorted(set(query.split()),key=len,reverse=True)
    if not tokens: return app.e(text)
    pattern = re.compile('|'.join(re.escape(t) for t in tokens), re.I)
    parts=[]; start=0
    for match in pattern.finditer(str(text)):
        parts.extend([app.e(str(text)[start:match.start()]), '<mark>'+app.e(match.group())+'</mark>'])
        start=match.end()
    parts.append(app.e(str(text)[start:]))
    return ''.join(parts)


def get(handler, app):
    parsed = urlparse(handler.path)
    query = parse_qs(parsed.query)
    value = lambda key: query.get(key,[''])[0]
    if parsed.path == '/api/avatar':
        app.queue_avatars.serve(handler,app,value('chat_id'))
    elif parsed.path in {'/static/ui.js','/static/ui.css'}:
        handler.file_response(app.ROOT / parsed.path.lstrip('/'), 'text/javascript; charset=utf-8' if parsed.path.endswith('.js') else 'text/css; charset=utf-8')
    elif parsed.path == '/search':
        q = value('q')[:200]
        try: page = max(1,int(value('page') or 1))
        except ValueError: page = 1
        found = queue_features.search(app.STORE,q,page)
        cards = []
        for row in found['results']:
            is_message = bool(row.get('message_key'))
            group = row['chat_id'].endswith('@g.us')
            label = 'Группа' if group else 'Личный чат'
            timestamp = int(row.get('message_timestamp',0) or 0)
            if timestamp:
                from datetime import datetime, timezone
                label += ' · ' + app.human_time(datetime.fromtimestamp(timestamp,timezone.utc).isoformat())
            body = marked(app,excerpt(row.get('body',''),q),q)
            transcript = row.get('transcript','')
            transcript_html = '<p>Расшифровка: '+marked(app,excerpt(transcript,q),q)+'</p>' if transcript else ''
            cards.append(f'<a class="search-card" href="{app.e(row["url"])}"><div class="search-card-head"><strong>{marked(app,row["name"],q)}</strong><span class="search-card-meta">{app.e(label)}</span></div><p>{body}</p>{transcript_html}<span class="search-open">{"Перейти к сообщению" if is_message else "Открыть чат"} ↗</span></a>')
        from urllib.parse import urlencode
        more = '<a class="button" href="/search?'+app.e(urlencode({'q':q,'page':page+1}))+'">Далее →</a>' if found['has_more'] else ''
        previous = '<a class="button" href="/search?'+app.e(urlencode({'q':q,'page':page-1}))+'">← Назад</a>' if page>1 else ''
        empty = 'Введите имя, номер телефона или фразу из переписки' if not q else 'Ничего не найдено. Попробуйте часть имени, номера или слова.'
        content = f'<section class="search-heading"><p class="eyebrow">Сообщения и контакты</p><h1>Поиск по WhatsApp</h1><p>Нажмите на результат, чтобы открыть сообщение в переписке</p></section><form class="search-form"><input name="q" maxlength="200" value="{app.e(q)}" aria-label="Поисковый запрос" placeholder="Имя, телефон или текст сообщения"><button class="button primary">Найти</button></form><section class="search-results">{"".join(cards) or "<p class=search-empty>"+empty+"</p>"}</section><nav class="search-pages">{previous}{more}</nav>'
        handler.html_response(app.layout('Поиск',content,'search',handler.is_admin()))
    elif parsed.path == '/api/message-window':
        chat_id = app.valid_conversation_id(value('chat_id'))
        window = queue_features.message_window(app.STORE,chat_id,value('message_id'))
        if window is None:
            handler.json_response({'error':'Сообщение удалено или не найдено'},404)
        else:
            window['messages'] = app.decorate_chat_messages(chat_id,window['messages'])
            handler.json_response(window)
    elif parsed.path == '/api/voice-job':
        key = (app.valid_conversation_id(value('chat_id')),value('message_id'))
        with app.VOICE_JOBS.lock:
            state = dict(app.VOICE_JOBS.states.get(key,{'status':'idle'}))
        handler.json_response(state)
    elif parsed.path == '/admin/shift':
        if not handler.require_admin(): return True
        selected = app.STORE.get_setting('shift_announcement_group','')
        options = '<option value="">Не отправлять</option>' + ''.join(f'<option value="{app.e(g["chat_id"])}" {"selected" if selected==g["chat_id"] else ""}>{app.e(g["name"])}</option>' for g in app.STORE.list_whatsapp_groups())
        text = app.STORE.get_setting('shift_announcement_text','На смене: {employee}. По рабочим вопросам обращайтесь в поддержку.')
        content = f'<h2>Объявление о смене</h2><form method="post" class="panel manual-form"><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><label>Группа<select name="group">{options}</select></label><label>Текст<textarea name="text" maxlength="800" required>{app.e(text)}</textarea></label><p>Используйте {{employee}} для имени сотрудника. Отправка только при изменении сотрудника на смене, не при сохранении настроек.</p><button class="button primary">Сохранить</button></form>'
        handler.html_response(app.admin_layout('Объявление о смене',content,'shift'))
    else:
        return False
    return True


def post(handler, app):
    path = urlparse(handler.path).path
    if path not in {'/voice-transcribe','/admin/shift'}:
        return False
    if path == '/admin/shift' and not handler.require_admin(): return True
    form = handler.read_form()
    if not secrets.compare_digest(form.get('csrf_token',''),app.ADMIN_FORM_TOKEN):
        handler.json_response({'error':'Обновите страницу и попробуйте снова'},403)
        return True
    if path == '/voice-transcribe':
        handler.json_response(app.VOICE_JOBS.submit(app.valid_conversation_id(form.get('chat_id','')),form.get('message_id','')))
    else:
        group = form.get('group','')
        if group and group not in {g['chat_id'] for g in app.STORE.list_whatsapp_groups()}:
            handler.json_response({'error':'Группа не найдена'},400)
            return True
        app.STORE.set_setting('shift_announcement_group',group)
        app.STORE.set_setting('shift_announcement_text',form.get('text','')[:800])
        handler.redirect('/admin/shift')
    return True
