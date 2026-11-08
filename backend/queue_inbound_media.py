"""Chunked connector media: bounded HTTP bodies, atomic publication, no outgoing send."""
import base64
import hashlib
import os
import re
import shutil
import threading
import time
from pathlib import Path

MAX_SIZE = 512 * 1024 * 1024
CHUNK = 512 * 1024
_lock = threading.Lock()

def key(chat, message):
    return hashlib.sha256((chat+'|'+message).encode()).hexdigest()

def receive(root, payload):
    chat, message = str(payload.get('chat_id','')), str(payload.get('message_id',''))
    if not re.fullmatch(r'[\w.:-]+@(c\.us|g\.us|lid)',chat) or not message or len(message)>160:
        raise ValueError('Некорректный адрес вложения')
    total, offset = int(payload.get('total',0)), int(payload.get('offset',-1))
    if not 0 < total <= MAX_SIZE or not 0 <= offset < total: raise ValueError('Некорректный размер вложения')
    data=base64.b64decode(str(payload.get('data','')),validate=True)
    if not data or len(data)>CHUNK or offset+len(data)>total: raise ValueError('Некорректная часть вложения')
    root=Path(root); receipt=key(chat,message)
    destination=root/(receipt+'.inbound'); part=root/(receipt+'.incoming')
    with _lock:
        if destination.is_file():
            if destination.stat().st_size != total: raise ValueError('Размер сохранённого вложения отличается')
            return {'offset':total,'receipt':receipt}
        if not part.exists():
            if shutil.disk_usage(root).free < total+64*1024*1024: raise ValueError('Недостаточно места для вложения')
            part.touch(mode=0o640)
        size=part.stat().st_size
        if offset<size:
            with part.open('rb') as f:
                f.seek(offset)
                if f.read(len(data)) != data: raise ValueError('Повтор части содержит другие данные')
            return {'offset':size}
        if offset!=size: raise ValueError('Нарушен порядок частей')
        with part.open('ab') as f:
            f.write(data)
            if f.tell()==total: f.flush(); os.fsync(f.fileno())
        size=part.stat().st_size
        if size==total: part.replace(destination)
        return {'offset':size, 'receipt':receipt if size==total else ''}

def resolve(root, chat, message, receipt):
    if str(receipt)!=key(chat,message): return ''
    path=Path(root)/(str(receipt)+'.inbound')
    return str(path) if path.is_file() else ''
