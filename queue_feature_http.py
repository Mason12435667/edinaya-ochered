"""HTTP endpoints separated from the legacy application entrypoint."""
from urllib.parse import urlparse, parse_qs
import queue_features
import queue_avatars
from queue_core import e, valid_conversation_id


def get(handler, deps):
    parsed = urlparse(handler.path)
    query = parse_qs(parsed.query)
    value = lambda key: query.get(key,[''])[0]
    if parsed.path == '/api/avatar':
        queue_avatars.serve(handler,deps,value('chat_id'))
    elif parsed.path in {'/static/ui.js','/static/ui.css'}:
        handler.file_response(deps.ROOT / parsed.path.lstrip('/'), 'text/javascript; charset=utf-8' if parsed.path.endswith('.js') else 'text/css; charset=utf-8')
    elif parsed.path == '/api/message-window':
        chat_id = valid_conversation_id(value('chat_id'))
        window = queue_features.message_window(deps.STORE,chat_id,value('message_id'))
        if window is None:
            handler.json_response({'error':'Сообщение удалено или не найдено'},404)
        else:
            window['messages'] = deps.decorate_chat_messages(chat_id,window['messages'])
            handler.json_response(window)
    elif parsed.path == '/admin/shift':
        if not handler.require_admin(): return True
        selected = deps.STORE.get_setting('shift_announcement_group','')
        options = '<option value="">Не отправлять</option>' + ''.join(f'<option value="{e(g["chat_id"])}" {"selected" if selected==g["chat_id"] else ""}>{e(g["name"])}</option>' for g in deps.STORE.list_whatsapp_groups())
        text = deps.STORE.get_setting('shift_announcement_text','На смене: {employee}. По рабочим вопросам обращайтесь в поддержку.')
        content = f'<h2>Объявление о смене</h2><form method="post" class="panel manual-form"><input type="hidden" name="csrf_token" value="{e(deps.csrf_token())}"><label>Группа<select name="group">{options}</select></label><label>Текст<textarea name="text" maxlength="800" required>{e(text)}</textarea></label><p>Используйте {{employee}} для имени сотрудника. Отправка только при изменении сотрудника на смене, не при сохранении настроек.</p><button class="button primary">Сохранить</button></form>'
        handler.html_response(deps.admin_layout('Объявление о смене',content,'shift'))
    else:
        return False
    return True


def post(handler, deps):
    # CSRF is checked once for every signed-in POST in QueueHTTPHandlerMixin.do_POST.
    if urlparse(handler.path).path != '/admin/shift':
        return False
    if not handler.require_admin(): return True
    form = handler.read_form()
    group = form.get('group','')
    if group and group not in {g['chat_id'] for g in deps.STORE.list_whatsapp_groups()}:
        handler.json_response({'error':'Группа не найдена'},400)
        return True
    deps.STORE.set_setting('shift_announcement_group',group)
    deps.STORE.set_setting('shift_announcement_text',form.get('text','')[:800])
    handler.redirect('/admin/shift')
    return True
