"""Search, message navigation, voice jobs and shift announcements.

Independent of the HTTP server; all queries use the existing store migrations.
"""
import base64
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode


def search(store, query, page=1):
    query = str(query).strip()[:200]
    if not query:
        return {"results": [], "has_more": False}
    page = max(1, min(10000, int(page)))
    tokens = query.casefold().split()
    clauses, params = [], []
    for token in tokens:
        # instr treats %, _ and backslashes literally, supports Cyrillic casefold.
        clauses.append("(instr(FOLD(m.body || ' ' || m.transcript || ' ' || m.sender || ' ' || COALESCE(c.name,'') || ' ' || COALESCE(g.name,'') || ' ' || COALESCE(p.name,'') || ' ' || m.chat_id || ' ' || COALESCE(p.phone,'')), ?) > 0 OR (? <> '' AND instr(DIGITS(m.chat_id || COALESCE(p.phone,'')), ?) > 0))")
        digits = re.sub(r'\D', '', token) if re.fullmatch(r'[+\d().-]+', token) else ''
        params.extend([token, digits, digits])
    with store.connection() as db:
        rows = db.execute("""SELECT m.message_key, m.chat_id, m.body, m.transcript,
            m.message_timestamp, COALESCE(NULLIF(p.name,''),NULLIF(g.name,''),NULLIF(c.name,''),m.sender,m.chat_id) AS name
            FROM whatsapp_chat_messages m
            LEFT JOIN whatsapp_chats c ON c.chat_id=m.chat_id
            LEFT JOIN whatsapp_groups g ON g.chat_id=m.chat_id
            LEFT JOIN whatsapp_contacts p ON p.chat_id=m.chat_id
            WHERE m.deleted=0 AND """ + ' AND '.join(clauses) +
            ' ORDER BY m.message_timestamp DESC,m.id DESC LIMIT 51 OFFSET ?',
            [*params, (page-1)*50]).fetchall()
        # Contacts with no messages are also searchable.
        contacts = db.execute("""SELECT chat_id,name FROM whatsapp_contacts
            UNION SELECT chat_id,name FROM whatsapp_chats
            UNION SELECT chat_id,name FROM whatsapp_groups""").fetchall() if page == 1 else []
    results = []
    seen = set()
    for row in contacts:
        haystack = (row['name']+' '+row['chat_id']).casefold()
        if all(t in haystack or (re.fullmatch(r'[+\d().-]+',t) and re.sub(r'\D','',t) and re.sub(r'\D','',t) in re.sub(r'\D','',row['chat_id'])) for t in tokens) and row['chat_id'] not in seen:
            seen.add(row['chat_id'])
            results.append(dict(chat_id=row['chat_id'], name=row['name'], body='Открыть чат', message_key=''))
    results.extend(dict(row) for row in rows[:50])
    for row in results:
        route = '/groups' if row['chat_id'].endswith('@g.us') else '/whatsapp'
        row['url'] = route + '?' + urlencode({'chat_id':row['chat_id'], 'message_id':row['message_key']})
    return {'results':results, 'has_more':len(rows)>50}


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


class VoiceJobs:
    def __init__(self, store, media_dir, transcribe):
        self.store, self.media_dir, self.transcribe = store, Path(media_dir).resolve(), transcribe
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='voice')
        self.lock, self.states = threading.Lock(), {}

    def submit(self, chat_id, message_id):
        key = (chat_id, message_id)
        message = self.store.get_whatsapp_message(*key)
        if not message or message.get('deleted') or not str(message.get('media_mime','')).startswith('audio/'):
            return {'status':'error','reason':'Голосовое не найдено'}
        if message.get('transcript'):
            return {'status':'done','text':message['transcript']}
        path = Path(message.get('media_path') or '').resolve()
        if self.media_dir not in path.parents or not path.is_file() or path.stat().st_size > 10*1024*1024:
            return {'status':'error','reason':'Аудиофайл отсутствует или больше 10 МБ'}
        with self.lock:
            if self.states.get(key,{}).get('status') in {'queued','running'}:
                return dict(self.states[key])
            if sum(s['status'] in {'queued','running'} for s in self.states.values()) >= 20:
                return {'status':'error','reason':'Очередь расшифровки занята. Попробуйте позже'}
            if len(self.states)>200:
                self.states = {k:v for k,v in self.states.items() if v['status'] in {'queued','running'}}
            self.states[key] = {'status':'queued'}
        self.pool.submit(self.run, key, path, message['media_mime'])
        return {'status':'queued'}

    def run(self, key, path, mime):
        try:
            with self.lock:
                self.states[key] = {'status':'running'}
            result = self.transcribe(base64.b64encode(path.read_bytes()).decode(), mime)
            text = str(result.get('text',''))
            if text:
                with self.store.connection() as db:
                    db.execute('UPDATE whatsapp_chat_messages SET transcript=? WHERE chat_id=? AND message_key=? AND deleted=0',(text,*key))
            state = {'status':'done' if text else 'error','text':text,'reason':result.get('reason') or ('Речь не обнаружена' if not text else '')}
        except Exception as error:
            detail = str(error).replace('\n',' ').strip()[:220]
            low = detail.casefold()
            if isinstance(error, FileNotFoundError):
                reason = 'Аудиофайл исчез до начала распознавания. Возможно, вложение было удалено или очищено'
            elif isinstance(error, PermissionError) or 'permission denied' in low:
                reason = 'Нет прав на чтение аудиофайла или запись результата в базу'
            elif 'database is locked' in low:
                reason = 'База данных временно занята другой операцией. Повторите через несколько секунд'
            elif 'no space left on device' in low:
                reason = 'На сервере закончилось свободное место'
            else:
                reason = 'Не удалось обработать аудио на сервере'
            if detail:
                reason += '. Техническая причина: ' + detail
            state = {'status':'error','reason':reason}
        with self.lock:
            self.states[key] = state


def announce_shift(store, previous, employee):
    group = store.get_setting('shift_announcement_group','')
    if previous == employee or not group or group not in {g['chat_id'] for g in store.list_whatsapp_groups()}:
        return 0
    template = store.get_setting('shift_announcement_text','На смене: {employee}. По рабочим вопросам обращайтесь в поддержку.')
    return store.queue_direct_message(group, template.replace('{employee}',employee), employee)
