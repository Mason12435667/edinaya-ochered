"""Bounded, resumable chunk uploads. No whole-file base64 on the HTTP path."""
import re
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

MAX_FILE = 512 * 1024 * 1024
CHUNK = 512 * 1024


class Uploads:
    def __init__(self, root, store):
        self.root, self.store = Path(root), store
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.sessions = {}

    def start(self, payload, actor):
        size = int(payload.get('size',0))
        if size <= 0 or size > MAX_FILE:
            raise ValueError('Размер файла должен быть от 1 байта до 512 МБ')
        chat = str(payload.get('chat_id',''))
        if not re.fullmatch(r'[\w.:-]+@(c\.us|g\.us|lid)',chat):
            raise ValueError('Не выбран чат')
        filename = str(payload.get('filename','')).replace('\\','/').split('/')[-1]
        filename = re.sub(r'[\x00-\x1f\x7f]','',filename)[:180] or 'Вложение'
        with self.lock:
            for key, state in list(self.sessions.items()):
                if time.time()-state['at'] > 3600:
                    if not state.get('queued'):
                        state['path'].unlink(missing_ok=True)
                    del self.sessions[key]
            # Clean abandoned sessions after a server restart.
            for path in self.root.glob('*.part'):
                if time.time()-path.stat().st_mtime > 3600:
                    path.unlink(missing_ok=True)
            if sum(not s.get('queued') for s in self.sessions.values()) >= 5:
                raise ValueError('Сейчас загружается несколько файлов. Попробуйте позже')
            if shutil.disk_usage(self.root).free < size+64*1024*1024:
                raise ValueError('На сервере недостаточно свободного места')
            token=secrets.token_urlsafe(24)
            path=self.root/(token+'.part')
            path.touch(exist_ok=False)
            self.sessions[token]={'path':path,'size':size,'offset':0,'at':time.time(),
                'request_id':str(payload.get('request_id',''))[:120],
                'chat':chat,'name':filename,'mime':safe_mime(payload.get('mimetype','')),
                'body':str(payload.get('message',''))[:1000],'reply':str(payload.get('reply_to',''))[:160],
                'mentions':payload.get('mentions',[]) if isinstance(payload.get('mentions',[]),list) else [],'actor':actor}
        return {'upload_id':token,'chunk_size':CHUNK}

    def append(self, token, offset, data):
        with self.lock:
            s=self.sessions.get(token)
            if not s or s.get('queued'): raise ValueError('Загрузка завершена или истекла')
            if offset != s['offset']: raise ValueError('Порядок частей нарушен. Начните загрузку заново')
            if not data or len(data)>CHUNK or offset+len(data)>s['size']: raise ValueError('Некорректная часть файла')
            with s['path'].open('ab') as f: f.write(data)
            s['offset']+=len(data); s['at']=time.time()
            return {'offset':s['offset']}

    def finish(self, token):
        with self.lock:
            s=self.sessions.get(token)
            if not s: raise ValueError('Загрузка истекла')
            if s.get('queued'): return {'queued':True,'message_id':s['queued']}
            if s['offset'] != s['size']: raise ValueError('Файл загружен не полностью')
            if s['path'].suffix == '.part':
                destination=s['path'].with_suffix('.ready')
                s['path'].rename(destination)
                s['path']=destination
            queued = self.store.queue_direct_message(s['chat'],s['body'],s['actor'],s['mentions'],
                reply_to_key=s['reply'],media_path=str(s['path']),media_mime=s['mime'],media_name=s['name'],request_id=s['request_id'])
            if not queued: raise ValueError('Не удалось поставить файл в очередь')
            with self.store.connection() as db:
                row=db.execute('SELECT media_path FROM outbound_messages WHERE id=?',(queued,)).fetchone()
            if row and row['media_path'] != str(s['path']): s['path'].unlink(missing_ok=True)
            s['queued']=queued
            return {'queued':True,'message_id':queued}

    def cancel(self, token):
        with self.lock:
            s=self.sessions.get(token)
            if s and not s.get('queued'):
                s['path'].unlink(missing_ok=True)
                del self.sessions[token]


def safe_mime(value):
    value = str(value).split(';')[0].strip().lower()
    return value if re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+',value) else 'application/octet-stream'


def post(handler, app):
    parsed=urlparse(handler.path)
    if not parsed.path.startswith('/upload/'): return False
    if not secrets.compare_digest(handler.headers.get('X-CSRF-Token',''),app.ADMIN_FORM_TOKEN):
        handler.json_response({'error':'Обновите страницу перед загрузкой'},403)
        return True
    try:
        if parsed.path == '/upload/start':
            payload=handler.read_json_body(20000)
            if payload is None: return True
            result=app.UPLOADS.start(payload,app.active_employee())
        elif parsed.path == '/upload/chunk':
            q=parse_qs(parsed.query)
            length=int(handler.headers.get('Content-Length','0'))
            if length<=0 or length>CHUNK: raise ValueError('Недопустимый размер части')
            data=handler.rfile.read(length)
            if len(data)!=length: raise ValueError('Передача прервана')
            result=app.UPLOADS.append(q.get('id',[''])[0],int(q.get('offset',['0'])[0]),data)
        elif parsed.path in {'/upload/finish','/upload/cancel'}:
            payload=handler.read_json_body(2000)
            if payload is None: return True
            token=str(payload.get('upload_id',''))
            if parsed.path.endswith('/cancel'):
                app.UPLOADS.cancel(token); result={'cancelled':True}
            else: result=app.UPLOADS.finish(token)
        else:
            handler.json_response({'error':'Неизвестная операция'},404); return True
        handler.json_response(result)
    except (ValueError,TypeError,OSError) as exc:
        handler.json_response({'error':str(exc)[:180]},400)
    return True


def preserve_sent_attachment(app, message_id):
    """Keep a local copy of a successfully sent attachment for chat history.

    The WhatsApp queue deletes its temporary outbound file after a successful send.
    Without this preserved copy the remote WhatsApp message is correct, but the
    local bubble can later contain only the caption.  Preserve first, then let the
    queue clean the temporary file.
    """
    with app.STORE.connection() as db:
        row = db.execute(
            'SELECT media_path,media_mime,media_name FROM outbound_messages WHERE id=?',
            (message_id,),
        ).fetchone()
    if not row:
        return {}

    destination = app.MEDIA_DIR / ('outbound-' + str(message_id) + '.bin')
    destination.parent.mkdir(parents=True, exist_ok=True)
    mime = str(row['media_mime'] or 'application/octet-stream')
    name = str(row['media_name'] or 'Вложение')

    # Idempotent result callbacks must keep returning the already-preserved copy.
    if destination.is_file():
        return {
            'media_path': str(destination), 'media_mime': mime, 'media_name': name,
            'type': 'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'document',
        }

    if not row['media_path']: return {}
    source = Path(str(row['media_path'])).resolve()
    allowed_roots = [app.OUTBOUND_MEDIA_DIR.resolve()]
    try:
        allowed_roots.append(Path(app.UPLOADS.root).resolve())
    except Exception:
        pass
    if not any(root == source.parent or root in source.parents for root in allowed_roots):
        return {}
    if not source.is_file():
        return {}

    try:
        # Same data volume in the normal install: zero-copy and safe.
        os.link(source, destination)
    except OSError:
        # Cross-filesystem fallback.  Keep the source until the queue marks the
        # send complete, instead of moving it out from under the sender.
        shutil.copy2(source, destination)

    return {
        'media_path': str(destination), 'media_mime': mime, 'media_name': name,
        'type': 'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'document',
    }



def recover_clipboard_attachment(app, chat_id, message_id, message_timestamp=0, body='', message_type=''):
    """Recover a Win+Shift+S attachment from the outbound queue by chat and time.

    WhatsApp can successfully deliver a clipboard screenshot while whatsapp-web.js
    exposes only an outgoing image stub that cannot be downloaded back.  The
    original upload is still present in outbound_messages while the row is
    ``uncertain``.  Match only our generated ``clipboard-*`` files, preserve the
    local bytes, and treat the WhatsApp echo as authoritative proof of delivery.
    """
    safe_chat = str(chat_id or '').strip()
    safe_message = str(message_id or '').strip()[:180]
    if not safe_chat or not safe_message:
        return {}
    try:
        ts = int(message_timestamp or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return {}

    mtype = str(message_type or '').strip().lower()
    if mtype not in {'image', 'photo', 'sticker'}:
        return {}

    try:
        canonical = app.STORE.canonical_whatsapp_chat_id(safe_chat) or safe_chat
    except Exception:
        canonical = safe_chat

    placeholders = {'[фото]', 'фото', '[image]', 'image', '[вложение]', 'вложение'}
    observed = str(body or '').strip()
    observed_cmp = '' if observed.casefold() in placeholders else observed

    try:
        with app.STORE.connection() as db:
            rows = db.execute(
                """
                SELECT id, chat_id, body, status, provider_id, media_path,
                       media_mime, media_name, created_at
                  FROM outbound_messages
                 WHERE media_name LIKE 'clipboard-%'
                   AND status IN ('pending','processing','sending','uncertain','sent')
                 ORDER BY id DESC
                 LIMIT 40
                """
            ).fetchall()
    except Exception:
        return {}

    from datetime import datetime, timezone
    candidates = []
    for row in rows:
        provider = str(row['provider_id'] or '').strip()
        if provider and provider != safe_message:
            # A different WhatsApp ID means this queue row was already paired.
            continue
        try:
            row_chat = app.STORE.canonical_whatsapp_chat_id(str(row['chat_id'] or '')) or str(row['chat_id'] or '')
        except Exception:
            row_chat = str(row['chat_id'] or '')
        if row_chat != canonical:
            continue
        mime = str(row['media_mime'] or '').strip().lower()
        if mime and not mime.startswith('image/'):
            continue
        created_raw = str(row['created_at'] or '').strip()
        try:
            created = datetime.fromisoformat(created_raw.replace('Z', '+00:00'))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            created_ts = int(created.timestamp())
        except Exception:
            continue
        delta = abs(ts - created_ts)
        # Real observations in the diagnosed server were 5-11 seconds after
        # the queue row.  90 seconds leaves room for a slow WhatsApp send while
        # still preventing an unrelated old screenshot from being attached.
        if delta > 90:
            continue
        queued_body = str(row['body'] or '').strip()
        if queued_body and observed_cmp and queued_body != observed_cmp:
            continue
        # Prefer a body match, then the closest timestamp, then newest queue id.
        body_penalty = 0 if queued_body == observed_cmp else 20
        candidates.append((body_penalty + delta, -int(row['id']), row))

    if not candidates:
        return {}
    candidates.sort(key=lambda value: (value[0], value[1]))
    row = candidates[0][2]
    queue_id = int(row['id'])

    preserved = preserve_sent_attachment(app, queue_id)
    if not preserved:
        return {}

    # The from_me WhatsApp image with a stable provider id proves that this
    # uncertain queue item did reach WhatsApp.  Finalize it only after the bytes
    # were preserved, so complete_outbound_message may safely remove the temp.
    try:
        app.STORE.complete_outbound_message(queue_id, True, '', safe_message)
    except Exception:
        # The media itself is already safe; history can still use it.
        pass

    return {
        **preserved,
        'outbound_message_id': queue_id,
        'provider_id': safe_message,
        'local_clipboard_recovered': True,
    }

def serve_range(handler, path, mime, disposition):
    size=path.stat().st_size
    start,end=0,size-1
    raw=handler.headers.get('Range','')
    if raw:
        match=re.fullmatch(r'bytes=(\d*)-(\d*)',raw)
        try:
            if not match or not any(match.groups()): raise ValueError
            left,right=match.groups()
            if left: start=int(left); end=min(int(right),size-1) if right else size-1
            else: start=max(0,size-int(right))
            if start<0 or start>=size or end<start: raise ValueError
        except ValueError:
            handler.send_response(416)
            handler.send_header('Content-Range',f'bytes */{size}')
            handler.send_header('Content-Length','0'); handler.end_headers()
            return
    handler.send_response(206 if raw else 200)
    handler.send_header('Content-Type',safe_mime(mime))
    handler.send_header('X-Content-Type-Options','nosniff')
    handler.send_header('Accept-Ranges','bytes')
    handler.send_header('Content-Length',str(max(0,end-start+1)))
    handler.send_header('Content-Disposition',disposition)
    if raw: handler.send_header('Content-Range',f'bytes {start}-{end}/{size}')
    handler.end_headers()
    try:
        with path.open('rb') as f:
            f.seek(start); remaining=end-start+1
            while remaining>0:
                chunk=f.read(min(256*1024,remaining))
                if not chunk: break
                handler.wfile.write(chunk); remaining-=len(chunk)
    except (BrokenPipeError,ConnectionResetError):
        pass
