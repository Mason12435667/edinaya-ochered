"""Small local cache of profile images supplied by the authenticated connector."""
import base64
import hashlib
import re
from pathlib import Path
import os
import tempfile
from urllib.parse import urlencode


def cache_path(app, chat_id):
    return app.DATA_DIR / 'avatars' / (hashlib.sha256(chat_id.encode()).hexdigest()+'.img')


def image_mime(data):
    if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'image/webp'
    return ''


def url(app, chat_id):
    path = cache_path(app,chat_id)
    try: version = path.stat().st_mtime_ns
    except OSError: return ''
    return '/api/avatar?' + urlencode({'chat_id':chat_id,'v':version})


def save(app, payload):
    chat = app.valid_conversation_id(str(payload.get('chat_id','')))
    if not chat: raise ValueError('Неизвестный чат')
    path = cache_path(app,chat)
    if payload.get('missing') is True:
        # A transient missing photo is not a deletion request.
        return
    raw = str(payload.get('image_base64',''))
    if len(raw)>1400000: raise ValueError('Слишком большой аватар')
    data = base64.b64decode(raw,validate=True)
    if len(data)>1024*1024 or not image_mime(data): raise ValueError('Неподдерживаемое изображение')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return
    fd, name = tempfile.mkstemp(prefix=path.stem+'-', suffix='.tmp', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def serve(handler,app,chat):
    chat=app.valid_conversation_id(chat)
    path=cache_path(app,chat)
    if not chat or not path.is_file():
        handler.send_error(404); return
    data=path.read_bytes()
    mime=image_mime(data)
    if not mime: handler.send_error(404); return
    handler.send_response(200)
    handler.send_header('Content-Type',mime)
    handler.send_header('Content-Length',str(len(data)))
    handler.send_header('Cache-Control','private, max-age=600')
    handler.end_headers()
    handler.wfile.write(data)
