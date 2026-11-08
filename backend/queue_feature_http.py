"""HTTP endpoints separated from the legacy application entrypoint."""
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import queue_features
from queue_avatars import AvatarCache
from queue_core import valid_conversation_id
from ticketing import TicketStore


class FeatureRoutes:
    """Avatars, the quoted-message window, the shift announcement page."""

    def __init__(self, store: TicketStore, avatars: AvatarCache, root: Path, *,
                 decorate_chat_messages: Callable[[str, list], list]):
        self.store = store
        self.avatars = avatars
        self.root = root
        self.decorate_chat_messages = decorate_chat_messages

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
            handler.page_response('admin')
        else:
            return False
        return True

    def shift_data(self):
        return {
            'title': 'Объявление о смене', 'active': 'shift',
            'selected': self.store.get_setting('shift_announcement_group', ''),
            'text': self.store.get_setting('shift_announcement_text', 'На смене: {employee}. По рабочим вопросам обращайтесь в поддержку.'),
            'groups': [[str(g['chat_id']), str(g['name'])] for g in self.store.list_whatsapp_groups()],
        }

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
