"""Resolve group quotes by exact stanza ID within one chat, never by body/name."""
import re

def parse(value):
    raw=str(value or '').strip()
    match=re.fullmatch(r'(true|false)_([^_]+)_([^_]+)(?:_(.+))?',raw)
    if match:return match.group(1),match.group(2),match.group(3),match.group(4) or ''
    if re.fullmatch(r'[A-Za-z0-9-]{8,}',raw):return '', '', raw, ''
    return '', '', '', ''

def resolve(store,chat,key):
    exact=store.get_whatsapp_message(chat,key)
    if exact:return exact
    direction,remote,stanza,participant=parse(key)
    if not stanza or (remote and remote!=chat and chat.endswith('@g.us')):return None
    with store.connection() as db:
        rows=db.execute('SELECT * FROM whatsapp_chat_messages WHERE chat_id=? AND instr(message_key,?)>0',(chat,stanza)).fetchall()
    matches=[]
    for row in rows:
        d,r,s,p=parse(row['message_key'])
        if s!=stanza or (direction and d and direction!=d):continue
        if direction and bool(row['from_me'])!=(direction=='true'):continue
        if participant and p and participant!=p:
            aliases={str(row['sender_id'] or '')}
            phone=re.sub(r'\D','',str(row['sender_phone'] or ''))
            if phone:aliases.add(phone+'@c.us')
            if not {participant,p}<=aliases:continue
        matches.append(dict(row))
    return matches[0] if len(matches)==1 else None
