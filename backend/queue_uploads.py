"""Bounded, resumable chunk uploads. No whole-file base64 on the HTTP path."""
import sqlite3
import re
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import queue_realtime
from queue_core import log_suppressed

MAX_FILE = 512 * 1024 * 1024
CHUNK = 512 * 1024


def _media_log(message: str) -> None:
    """Best-effort local diagnostics without logging message/file contents."""
    try:
        data_dir = Path(os.environ.get('QUEUE_DATA_DIR', '') or '.').resolve()
        log_dir = Path(os.environ.get('QUEUE_LOG_DIR', '') or (data_dir / 'logs'))
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with (log_dir / 'media-upload.log').open('a', encoding='utf-8') as handle:
            handle.write(f'[{stamp}] {message}\n')
    except (OSError, ValueError):
        log_suppressed("queue_uploads._media_log")


class Uploads:
    def __init__(self, root, store):
        self.root, self.store = Path(root), store
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.sessions = {}

    def start(self, payload, actor, owner=""):
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
                'mentions':payload.get('mentions',[]) if isinstance(payload.get('mentions',[]),list) else [],'actor':actor,'owner':owner}
        _media_log(f"start chat={chat} name={filename!r} mime={self.sessions[token]['mime']} size={size} upload={token[:8]}")
        return {'upload_id':token,'chunk_size':CHUNK}

    def authorized_chat(self, token, owner):
        with self.lock:
            state = self.sessions.get(token)
            if not state or not owner or state.get('owner') != owner:
                raise PermissionError('Загрузка принадлежит другому аккаунту или истекла')
            return state['chat']

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
            # Realtime fast path: chunk uploads must wake the WhatsApp connector
            # exactly like text sends do. Without this the connector can remain
            # inside /api/outbound/claim long-poll until its ~18 s timeout.
            queue_realtime.notify_outbound()
            queue_realtime.notify('outbound_media')
            _media_log(f"queued id={queued} chat={s['chat']} name={s['name']!r} mime={s['mime']} size={s['size']}")
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


class UploadRoutes:
    """/upload/* endpoints of the chunked upload, on behalf of the signed-in user."""

    def __init__(self, uploads: Uploads, work_actor: Callable[[], str]):
        self.uploads = uploads
        self.work_actor = work_actor

    def post(self, handler):
        parsed=urlparse(handler.path)
        if not parsed.path.startswith('/upload/'): return False
        user = handler.effective_user() or {}
        owner = str(user.get('id') or ('legacy:' + str(user.get('username') or '')))
        try:
            if parsed.path == '/upload/start':
                payload=handler.read_json_body(20000)
                if payload is None: return True
                if not handler.claim_conversation_write(str(payload.get('chat_id', ''))): return True
                result=self.uploads.start(payload,self.work_actor(),owner=owner)
            elif parsed.path == '/upload/chunk':
                q=parse_qs(parsed.query)
                self.uploads.authorized_chat(q.get('id',[''])[0],owner)
                length=int(handler.headers.get('Content-Length','0'))
                if length<=0 or length>CHUNK: raise ValueError('Недопустимый размер части')
                data=handler.read_body(CHUNK)
                if data is None or len(data)!=length: raise ValueError('Передача прервана')
                result=self.uploads.append(q.get('id',[''])[0],int(q.get('offset',['0'])[0]),data)
            elif parsed.path in {'/upload/finish','/upload/cancel'}:
                payload=handler.read_json_body(2000)
                if payload is None: return True
                token=str(payload.get('upload_id',''))
                chat = self.uploads.authorized_chat(token,owner)
                if parsed.path.endswith('/finish') and not handler.claim_conversation_write(chat): return True
                if parsed.path.endswith('/cancel'):
                    self.uploads.cancel(token); result={'cancelled':True}
                else: result=self.uploads.finish(token)
            else:
                handler.json_response({'error':'Неизвестная операция'},404); return True
            handler.json_response(result)
        except PermissionError as exc:
            handler.json_response({'error':str(exc)},403)
        except (ValueError,TypeError,OSError) as exc:
            _media_log(f"error route={parsed.path} reason={str(exc)[:180]!r}")
            handler.json_response({'error':str(exc)[:180]},400)
        return True


def recover_clipboard_attachment(store, attachments, chat_id, message_id, message_timestamp=0, body='', message_type=''):
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
        canonical = store.canonical_whatsapp_chat_id(safe_chat) or safe_chat
    except sqlite3.Error:
        log_suppressed("queue_uploads.recover_clipboard_attachment")
        canonical = safe_chat

    placeholders = {'[фото]', 'фото', '[image]', 'image', '[вложение]', 'вложение'}
    observed = str(body or '').strip()
    observed_cmp = '' if observed.casefold() in placeholders else observed

    try:
        with store.connection() as db:
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
    except sqlite3.Error:
        log_suppressed("queue_uploads.recover_clipboard_attachment")
        return {}

    from datetime import datetime, timezone
    candidates = []
    for row in rows:
        provider = str(row['provider_id'] or '').strip()
        if provider and provider != safe_message:
            # A different WhatsApp ID means this queue row was already paired.
            continue
        try:
            row_chat = store.canonical_whatsapp_chat_id(str(row['chat_id'] or '')) or str(row['chat_id'] or '')
        except sqlite3.Error:
            log_suppressed("queue_uploads.recover_clipboard_attachment")
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
        except (OSError, OverflowError, ValueError):
            log_suppressed("queue_uploads.recover_clipboard_attachment")
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

    preserved = attachments.preserve(queue_id)
    if not preserved:
        return {}

    # The from_me WhatsApp image with a stable provider id proves that this
    # uncertain queue item did reach WhatsApp.  Finalize it only after the bytes
    # were preserved, so complete_outbound_message may safely remove the temp.
    try:
        store.complete_outbound_message(queue_id, True, '', safe_message)
    except sqlite3.Error:
        # The media itself is already safe; history can still use it.
        log_suppressed("queue_uploads.recover_clipboard_attachment")

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
