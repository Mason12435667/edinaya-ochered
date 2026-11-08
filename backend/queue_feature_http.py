"""HTTP endpoints separated from the legacy application entrypoint."""
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import queue_features
from queue_avatars import AvatarCache
from queue_core import e, valid_conversation_id
from ticketing import TicketStore


class FeatureRoutes:
    """Avatars, the quoted-message window, the shift announcement page."""

    def __init__(self, store: TicketStore, avatars: AvatarCache, root: Path, *,
                 decorate_chat_messages: Callable[[str, list], list], csrf_token: Callable[[], str],
                 admin_layout: Callable[[str, str, str], str]):
        self.store = store
        self.avatars = avatars
        self.root = root
        self.decorate_chat_messages = decorate_chat_messages
        self.csrf_token = csrf_token
        self.admin_layout = admin_layout

    def get(self, handler):
        parsed = urlparse(handler.path)
        query = parse_qs(parsed.query)
        value = lambda key: query.get(key,[''])[0]
        if parsed.path == '/api/avatar':
            self.avatars.serve(handler,value('chat_id'))
        elif parsed.path in {'/static/ui.js','/static/ui.css'}:
            handler.file_response(self.root / 'frontend' / parsed.path.lstrip('/'), 'text/javascript; charset=utf-8' if parsed.path.endswith('.js') else 'text/css; charset=utf-8')
        elif parsed.path == '/api/message-window':
            chat_id = valid_conversation_id(value('chat_id'))
            window = queue_features.message_window(self.store,chat_id,value('message_id'))
            if window is None:
                handler.json_response({'error':'Сообщение удалено или не найдено'},404)
            else:
                window['messages'] = self.decorate_chat_messages(chat_id,window['messages'])
                handler.json_response(window)
        elif parsed.path == '/admin/shift':
            if not handler.require_admin(): return True
            selected = self.store.get_setting('shift_announcement_group','')
            options = '<option value="">Не отправлять</option>' + ''.join(f'<option value="{e(g["chat_id"])}" {"selected" if selected==g["chat_id"] else ""}>{e(g["name"])}</option>' for g in self.store.list_whatsapp_groups())
            text = self.store.get_setting('shift_announcement_text','На смене: {employee}. По рабочим вопросам обращайтесь в поддержку.')
            content = f'<h2>Объявление о смене</h2><form method="post" class="panel manual-form"><input type="hidden" name="csrf_token" value="{e(self.csrf_token())}"><label>Группа<select name="group">{options}</select></label><label>Текст<textarea name="text" maxlength="800" required>{e(text)}</textarea></label><p>Используйте {{employee}} для имени сотрудника. Отправка только при изменении сотрудника на смене, не при сохранении настроек.</p><button class="button primary">Сохранить</button></form>'
            handler.html_response(self.admin_layout('Объявление о смене',content,'shift'))
        else:
            return False
        return True

    def post(self, handler):
        # CSRF is checked once for every signed-in POST in QueueHTTPHandlerMixin.do_POST.
        if urlparse(handler.path).path != '/admin/shift':
            return False
        if not handler.require_admin(): return True
        form = handler.read_form()
        group = form.get('group','')
        if group and group not in {g['chat_id'] for g in self.store.list_whatsapp_groups()}:
            handler.json_response({'error':'Группа не найдена'},400)
            return True
        self.store.set_setting('shift_announcement_group',group)
        self.store.set_setting('shift_announcement_text',form.get('text','')[:800])
        handler.redirect('/admin/shift')
        return True
