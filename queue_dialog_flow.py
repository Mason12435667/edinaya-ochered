"""Durable pre-menu context and bounded, explainable clarification helpers."""
import json
import re
import time
from queue_user_locale import tr

PREFIX = 'dialog138:'
TTL = 24 * 3600

def load(store, key):
    try:
        value = json.loads(store.get_setting(PREFIX + key, '{}'))
        return value if isinstance(value, dict) and time.time() - value.get('updated', 0) <= TTL else {}
    except (ValueError, TypeError):
        return {}

def save(store, key, value):
    value['updated'] = time.time()
    store.set_setting(PREFIX + key, json.dumps(value, ensure_ascii=False))

def remember(store, key, payload):
    text = str(payload.get('text') or '').strip()
    if not text and not payload.get('attachment_name'): return
    if re.fullmatch(r'\d{1,2}', text) or text.casefold() in {'ок','спасибо','рахмет','привет','здравствуйте','добрый день'}: return
    state = load(store, key)
    items = state.setdefault('buffer', [])
    mid = payload.get('external_id')
    if mid and any(x.get('external_id') == mid for x in items): return
    items.append({k: payload.get(k, '') for k in ('text','external_id','attachment_name','media_mime','message_timestamp')})
    # Bound storage; chat history remains the authoritative complete record.
    state['buffer'] = items[-20:]
    save(store, key, state)

def pop_buffer(store, key):
    state = load(store, key);items = state.pop('buffer', [])
    save(store,key,state)
    return items

def review(store, key, reason):
    state=load(store,key);state['review']={'reason':reason,'since':time.time()};save(store,key,state)
    store.enable_manual_chat_mode(key, 'Бот: нужен разбор', minutes=30)

def review_state(store,key):
    state=load(store,key)
    manual=store.manual_chat_mode(key)
    return state.get('review',{}) if manual and manual.get('actor') == 'Бот: нужен разбор' else {}

def other_topic(text, category):
    patterns = {
        'database': r'блокиров|выдать\s+рол|снять\s+блок|разблокир',
        'seal': r'\b(?:нп|пломб[аы]?)[\s:№]*\d|пломб\w*\s+не\s+откр',
        'bin_company_name': r'переименовать\s+компан|изменить\s+название\s+компан',
    }
    # Only strong signals; never switch categories automatically.
    return next((key for key,pattern in patterns.items() if key != category and re.search(pattern,text,re.I)), '')

def topic_prompt():
    return tr('Это дополнение к текущей заявке или новая проблема?\nНапишите «Дополнение» либо «Новая». Для новой заявки потребуется выбрать категорию.',
              'Бұл ағымдағы өтінімге қосымша ма, әлде жаңа мәселе ме?\n«Қосымша» немесе «Жаңа» деп жазыңыз. Жаңа өтінім үшін санатты таңдау қажет.')
