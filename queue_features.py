"""Message navigation, mention matching and shift announcements.

Independent of the HTTP server; all queries use the existing store migrations.
"""
import re


def message_window(store, chat_id, message_id):
    with store.connection() as db:
        target = db.execute('SELECT id,message_timestamp FROM whatsapp_chat_messages WHERE chat_id=? AND message_key=? AND deleted=0', (chat_id,message_id)).fetchone()
    if not target:
        return None
    # Existing cursor API includes the target and 49 preceding messages.
    return store.list_saved_whatsapp_messages_page(chat_id,50,f"{target['message_timestamp']}:{target['id']+1}")


def mention_match(message, employees, own_ids=()):
    if message.get('deleted') or message.get('from_me'):
        return False
    mentions = set(str(x) for x in message.get('mentions', []))
    if mentions.intersection(own_ids):
        return True
    names = [*employees, 'поддержка', 'support', 'Рабочий WhatsApp', *[x.split('@')[0] for x in own_ids]]
    body = str(message.get('body',''))
    return any(re.search(r'(?<!\w)@'+re.escape(name)+r'(?!\w)',body,re.I) for name in names if name)


def announce_shift(store, previous, employee):
    group = store.get_setting('shift_announcement_group','')
    if previous == employee or not group or group not in {g['chat_id'] for g in store.list_whatsapp_groups()}:
        return 0
    template = store.get_setting('shift_announcement_text','На смене: {employee}. По рабочим вопросам обращайтесь в поддержку.')
    return store.queue_direct_message(group, template.replace('{employee}',employee), employee)
