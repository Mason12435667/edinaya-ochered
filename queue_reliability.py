from __future__ import annotations

import os
import re
import sqlite3
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

DANGEROUS_EXTENSIONS = {
    '.exe','.com','.scr','.pif','.msi','.msp','.bat','.cmd','.ps1','.psm1','.vbs','.vbe','.js','.jse',
    '.wsf','.wsh','.hta','.cpl','.reg','.lnk','.scf','.jar','.apk','.appx','.appxbundle','.iso','.img'
}

CONNECTOR_PATHS = {
    '/api/media-stage','/api/whatsapp','/api/template-error-delivery','/api/outbound/claim',
    '/api/outbound/heartbeat','/api/outbound/begin','/api/outbound/result','/api/chat-list-sync',
    '/api/group-list-sync','/api/presence-sync','/api/group-participants-sync','/api/group-message-identities-sync',
    '/api/contact-list-sync','/api/avatar-sync','/api/contact-profile-sync','/api/group-refresh-check',
    '/api/connector-state','/api/contact-policy','/api/call-permission','/api/chat-messages-sync',
    '/api/chat-message-ack','/api/chat-reaction-sync','/api/whatsapp-action/claim','/api/whatsapp-action/result',
    '/api/chat-control'
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_dt(value: object) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def initialize(store) -> None:
    with store.connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS reliability_scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            body TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled',
            outbound_id INTEGER NOT NULL DEFAULT 0,
            cancelled_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_reliability_schedule_due
          ON reliability_scheduled_messages(status,due_at,id);
        CREATE TABLE IF NOT EXISTS reliability_alerts (
            alert_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            level TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reliability_notification_dismissals (
            event_id TEXT PRIMARY KEY,
            dismissed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reliability_checks (
            check_key TEXT PRIMARY KEY,
            ok INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL
        );
        ''')
        db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('maintenance_mode','0')")
        db.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('sla_alerts_enabled','1')")


def maintenance_active(store) -> bool:
    return str(store.get_setting('maintenance_mode','0')).strip().lower() in {'1','true','yes','on'}


def set_maintenance(store, enabled: bool) -> None:
    store.set_setting('maintenance_mode', '1' if enabled else '0')


def should_block_mutation(path: str, is_admin: bool) -> bool:
    if is_admin:
        return False
    path = str(path or '')
    if path in CONNECTOR_PATHS or path in {'/admin/login','/admin/logout','/health','/health/connector'}:
        return False
    if path.startswith('/api/reliability-'):
        return False
    # Only POST requests use this helper. Everything else is an employee mutation.
    return True


def upload_validation(filename: str, mimetype: str, encoded_length: int, max_bytes: int) -> tuple[bool,str]:
    name = str(filename or '').strip()
    suffix = Path(name).suffix.lower()
    if suffix in DANGEROUS_EXTENSIONS:
        return False, f'Файл {suffix or name} заблокирован: потенциально опасное расширение'
    # base64 overhead ~4/3. This rejects oversized payloads before decoding to RAM.
    estimated = max(0, int(encoded_length or 0)) * 3 // 4
    if estimated > int(max_bytes):
        mb = max(1, int(max_bytes) // (1024*1024))
        return False, f'Файл превышает лимит {mb} МБ'
    mime = str(mimetype or '').lower().strip()
    if mime in {'application/x-msdownload','application/x-msdos-program','application/x-sh','application/x-powershell'}:
        return False, 'Исполняемые и командные файлы запрещены для отправки'
    return True, ''


def schedule_message(store, chat_id: str, body: str, actor: str, due_at: str) -> int:
    chat_id = str(chat_id or '').strip()[:120]
    body = str(body or '').replace('\x00','').strip()[:32000]
    actor = str(actor or '').strip()[:80]
    due = _parse_dt(due_at)
    now = datetime.now(timezone.utc)
    if not chat_id or not body:
        raise ValueError('Нужно выбрать чат и указать текст сообщения')
    if due is None:
        raise ValueError('Некорректное время отправки')
    if due < now + timedelta(seconds=3):
        raise ValueError('Время отправки должно быть хотя бы через 3 секунды')
    if due > now + timedelta(days=30):
        raise ValueError('Отложить сообщение можно максимум на 30 дней')
    with store.connection() as db:
        cur = db.execute('''INSERT INTO reliability_scheduled_messages(chat_id,body,actor,due_at,created_at,status)
                            VALUES(?,?,?,?,?,'scheduled')''', (chat_id,body,actor,due.isoformat(),utc_now()))
        return int(cur.lastrowid)


def scheduled_messages(store, chat_id: str = '', limit: int = 100) -> list[dict[str,Any]]:
    with store.connection() as db:
        if chat_id:
            rows = db.execute('''SELECT * FROM reliability_scheduled_messages
                                 WHERE chat_id=? AND status='scheduled' ORDER BY due_at,id LIMIT ?''', (chat_id,int(limit))).fetchall()
        else:
            rows = db.execute('''SELECT * FROM reliability_scheduled_messages
                                 WHERE status='scheduled' ORDER BY due_at,id LIMIT ?''', (int(limit),)).fetchall()
    return [dict(x) for x in rows]


def cancel_scheduled(store, schedule_id: int) -> bool:
    with store.connection() as db:
        cur = db.execute("UPDATE reliability_scheduled_messages SET status='cancelled',cancelled_at=? WHERE id=? AND status='scheduled'",
                         (utc_now(),int(schedule_id)))
        return bool(cur.rowcount)


def dispatch_due(store, limit: int = 25) -> int:
    now = utc_now(); moved = 0
    with store.connection() as db:
        rows = db.execute("SELECT * FROM reliability_scheduled_messages WHERE status='scheduled' AND due_at<=? ORDER BY due_at,id LIMIT ?",
                          (now,int(limit))).fetchall()
    for raw in rows:
        row = dict(raw)
        try:
            outbound_id = int(store.queue_direct_message(row['chat_id'], row['body'], row['actor']) or 0)
            if outbound_id <= 0:
                raise RuntimeError('Не удалось поставить сообщение во внутреннюю очередь WhatsApp')
            with store.connection() as db:
                db.execute("UPDATE reliability_scheduled_messages SET status='queued',outbound_id=?,last_error='' WHERE id=? AND status='scheduled'",
                           (outbound_id,int(row['id'])))
            moved += 1
        except Exception as exc:
            with store.connection() as db:
                db.execute("UPDATE reliability_scheduled_messages SET last_error=? WHERE id=? AND status='scheduled'",
                           (str(exc)[:300],int(row['id'])))
    return moved


def queue_problem_rows(store, limit: int = 100) -> list[dict[str,Any]]:
    stale = (datetime.now(timezone.utc)-timedelta(seconds=90)).replace(microsecond=0).isoformat()
    with store.connection() as db:
        rows = db.execute('''SELECT id,ticket_id,chat_id,body,actor,status,attempt_count,max_attempts,last_error,
                                    media_path,media_mime,media_name,created_at,updated_at,provider_id
                             FROM outbound_messages
                             WHERE status IN ('failed','uncertain')
                                OR (status IN ('sending','processing') AND updated_at<>'' AND updated_at<?)
                             ORDER BY id DESC LIMIT ?''', (stale,int(limit))).fetchall()
    return [dict(x) for x in rows]


def retry_outbound(store, message_id: int, media_only: bool = False) -> int:
    with store.connection() as db:
        row = db.execute('SELECT * FROM outbound_messages WHERE id=?',(int(message_id),)).fetchone()
        if not row:
            raise ValueError('Сообщение очереди не найдено')
        if str(row['status']) not in {'failed','uncertain'}:
            raise ValueError('Повтор доступен только для ошибочной или неопределённой отправки')
        if media_only:
            media_path = str(row['media_path'] or '')
            if not media_path or not Path(media_path).is_file():
                raise ValueError('Исходный файл вложения уже недоступен')
            now = utc_now()
            cur = db.execute('''INSERT INTO outbound_messages
                (ticket_id,chat_id,phone,body,actor,status,created_at,claimed_at,mentions_json,reply_to_key,
                 media_path,media_mime,media_name,attempt_count,max_attempts,next_attempt_at,last_error,provider_id,updated_at,sent_at,failed_at)
                VALUES(?,?,?,?,?,'pending',?,'',?,'',?,?,?,0,4,'','Повтор только вложения','',?,'','')''',
                (int(row['ticket_id'] or 0),str(row['chat_id'] or ''),str(row['phone'] or ''),'',str(row['actor'] or ''),now,
                 str(row['mentions_json'] or '[]'),media_path,str(row['media_mime'] or ''),str(row['media_name'] or ''),now))
            return int(cur.lastrowid)
        now = utc_now()
        cur = db.execute("""UPDATE outbound_messages SET status='pending',attempt_count=0,claimed_at='',next_attempt_at='',
                          failed_at='',updated_at=?,last_error='Повтор запрошен вручную' WHERE id=? AND status IN ('failed','uncertain')""",
                         (now,int(message_id)))
        if not cur.rowcount:
            raise ValueError('Не удалось вернуть сообщение в очередь')
        return int(message_id)


def cancel_pending_outbound(store, message_id: int) -> bool:
    with store.connection() as db:
        cur = db.execute("UPDATE outbound_messages SET status='cancelled',updated_at=?,last_error='Отменено сотрудником до отправки' WHERE id=? AND status='pending' AND attempt_count=0",
                         (utc_now(),int(message_id)))
        return bool(cur.rowcount)


def message_info(store, chat_id: str, message_key: str) -> dict[str,Any] | None:
    with store.connection() as db:
        row = db.execute('''SELECT id,chat_id,message_key,from_me,sender,sender_id,sender_phone,body,message_type,
                                   media_mime,media_name,media_path,message_timestamp,created_at,ack,forwarded,deleted,ticket_id
                            FROM whatsapp_chat_messages WHERE chat_id=? AND message_key=? ORDER BY id DESC LIMIT 1''',
                         (str(chat_id),str(message_key))).fetchone()
    if not row:
        return None
    item = dict(row)
    ts = int(item.get('message_timestamp') or 0)
    item['received_at'] = datetime.fromtimestamp(ts,tz=timezone.utc).isoformat() if ts else str(item.get('created_at') or '')
    item['source'] = 'Единая очередь' if int(item.get('from_me') or 0) else 'WhatsApp / телефон'
    return item


def request_profile_refresh(app, chat_id: str) -> None:
    chat_id = str(chat_id or '').strip()[:120]
    if not chat_id:
        raise ValueError('Чат не выбран')
    try:
        with app.CHAT_LOCK:
            app.CHAT_STATE['requested_chat_id'] = chat_id
            profiles = app.CHAT_STATE.get('contact_profiles')
            if isinstance(profiles,dict): profiles.pop(chat_id,None)
            presence = app.CHAT_STATE.get('presence')
            if isinstance(presence,dict): presence.pop(chat_id,None)
    except Exception:
        pass
    try:
        path = app.queue_avatars.cache_path(app,chat_id)
        if path.is_file(): path.unlink()
    except Exception:
        pass


def database_check(database_path: Path) -> dict[str,Any]:
    try:
        uri = f'file:{database_path}?mode=ro'
        db = sqlite3.connect(uri,uri=True,timeout=2)
        try:
            result = str(db.execute('PRAGMA quick_check').fetchone()[0])
            tables = int(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
        finally: db.close()
        return {'ok':result.lower()=='ok','detail':result,'tables':tables}
    except Exception as exc:
        return {'ok':False,'detail':str(exc)[:300],'tables':0}


def _backup_candidates(data_dir: Path) -> list[Path]:
    values: list[Path] = []
    roots = [data_dir/'backups', Path('/var/backups')]
    for root in roots:
        if not root.exists(): continue
        try:
            for p in root.rglob('tickets.db'):
                if p.is_file(): values.append(p)
        except OSError: pass
    values.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return values[:12]


def verify_backups(data_dir: Path) -> list[dict[str,Any]]:
    out=[]
    for p in _backup_candidates(data_dir):
        check=database_check(p)
        try: size=int(p.stat().st_size); mtime=datetime.fromtimestamp(p.stat().st_mtime,tz=timezone.utc).isoformat()
        except OSError: size=0; mtime=''
        out.append({'path':str(p),'size':size,'modified_at':mtime,**check})
    return out


def diagnostic_snapshot(app) -> dict[str,Any]:
    db = database_check(app.DATABASE_PATH)
    try:
        usage=shutil.disk_usage(app.DATA_DIR); disk={'total':usage.total,'used':usage.used,'free':usage.free}
    except OSError: disk={'total':0,'used':0,'free':0}
    try: connector=app.connector_state_snapshot()
    except Exception: connector={}
    problems=queue_problem_rows(app.STORE,100)
    backups=verify_backups(app.DATA_DIR)
    legacy_secret=app.ROOT/'.connector_token'
    secret_env=bool(os.getenv('WEBHOOK_TOKEN','').strip())
    return {
        'database':db,'disk':disk,'connector':connector,'queue_problems':problems,
        'backups':backups,'maintenance':maintenance_active(app.STORE),
        'secret_storage':{'env':secret_env,'legacy_file':legacy_secret.is_file(),'legacy_path':str(legacy_secret)},
        'max_media_bytes':int(getattr(app,'MAX_MEDIA_BYTES',12*1024*1024)),
    }



def _normalise_warning_event_id(event_id: object) -> str:
    value = str(event_id or '').strip()[:240]
    if not value.startswith(('reliability:','system:','sla:')):
        return ''
    return value


def dismissed_notification_ids(store, event_ids: list[str] | tuple[str,...] | set[str]) -> set[str]:
    ids = sorted({_normalise_warning_event_id(x) for x in event_ids if _normalise_warning_event_id(x)})
    if not ids:
        return set()
    placeholders = ','.join('?' for _ in ids)
    try:
        with store.connection() as db:
            rows = db.execute(
                f"SELECT event_id FROM reliability_notification_dismissals WHERE event_id IN ({placeholders})",
                ids,
            ).fetchall()
        return {str(row['event_id'] if hasattr(row,'keys') else row[0]) for row in rows}
    except Exception:
        return set()


def reconcile_notification_dismissals(store, current_event_ids: list[object], prefixes: tuple[str,...]) -> None:
    current={_normalise_warning_event_id(x) for x in current_event_ids if _normalise_warning_event_id(x)}
    try:
        with store.connection() as db:
            rows=db.execute("SELECT event_id FROM reliability_notification_dismissals").fetchall()
            stale=[]
            for row in rows:
                event_id=str(row['event_id'] if hasattr(row,'keys') else row[0])
                if event_id.startswith(prefixes) and event_id not in current:
                    stale.append((event_id,))
            if stale:
                db.executemany("DELETE FROM reliability_notification_dismissals WHERE event_id=?",stale)
    except Exception:
        pass


def filter_dismissed_notifications(store, events: list[dict[str,Any]]) -> list[dict[str,Any]]:
    event_ids = [str(e.get('id') or e.get('event_id') or '') for e in events if isinstance(e,dict)]
    hidden = dismissed_notification_ids(store, event_ids)
    if not hidden:
        return events
    return [e for e in events if str(e.get('id') or e.get('event_id') or '') not in hidden]


def dismiss_notification(store, event_id: object) -> bool:
    event_id = _normalise_warning_event_id(event_id)
    if not event_id:
        return False
    with store.connection() as db:
        db.execute(
            "INSERT INTO reliability_notification_dismissals(event_id,dismissed_at) VALUES(?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET dismissed_at=excluded.dismissed_at",
            (event_id, utc_now()),
        )
    return True


def dismiss_notifications(store, event_ids: list[object]) -> int:
    clean = sorted({_normalise_warning_event_id(x) for x in event_ids if _normalise_warning_event_id(x)})
    if not clean:
        return 0
    now = utc_now()
    with store.connection() as db:
        db.executemany(
            "INSERT INTO reliability_notification_dismissals(event_id,dismissed_at) VALUES(?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET dismissed_at=excluded.dismissed_at",
            [(event_id,now) for event_id in clean],
        )
    return len(clean)


def clear_notification_dismissal(store, event_id: object) -> None:
    event_id = _normalise_warning_event_id(event_id)
    if not event_id:
        return
    with store.connection() as db:
        db.execute("DELETE FROM reliability_notification_dismissals WHERE event_id=?", (event_id,))


def resolve_persisted_alert(store, alert_key: str) -> bool:
    alert_key = str(alert_key or '').strip()[:120]
    if not alert_key:
        return False
    event_id = 'reliability:' + alert_key
    with store.connection() as db:
        row = db.execute("SELECT active FROM reliability_alerts WHERE alert_key=?", (alert_key,)).fetchone()
        if not row:
            return False
        active = bool(row['active'] if hasattr(row,'keys') else row[0])
        if not active:
            return False
        db.execute("UPDATE reliability_alerts SET active=0,updated_at=? WHERE alert_key=? AND active<>0", (utc_now(),alert_key))
        db.execute("DELETE FROM reliability_notification_dismissals WHERE event_id=?", (event_id,))
    return True


def refresh_active_alerts(app) -> dict[str,Any]:
    """Run the checks that can safely be revalidated on demand from the bell UI."""
    run_health_checks(app)
    backup_bad: list[dict[str,Any]] = []
    try:
        backups = verify_backups(app.DATA_DIR)
        backup_bad = [x for x in backups[:3] if not x.get('ok')]
        _upsert_alert(
            app.STORE,
            'backup-integrity','backup','critical','Проблема резервной копии',
            str(backup_bad[0].get('path') or backup_bad[0].get('detail') or '') if backup_bad else '',
            bool(backup_bad),
        )
    except Exception as exc:
        backup_bad = [{'detail':str(exc)[:180]}]
    return {'backup_bad':len(backup_bad)}


def _upsert_alert(store, key: str, kind: str, level: str, title: str, detail: str, active: bool=True) -> None:
    now=utc_now(); event_id='reliability:'+str(key)
    with store.connection() as db:
        previous=db.execute("SELECT active FROM reliability_alerts WHERE alert_key=?",(key,)).fetchone()
        was_active=bool(previous['active'] if previous is not None and hasattr(previous,'keys') else previous[0] if previous else 0)
        db.execute('''INSERT INTO reliability_alerts(alert_key,kind,level,title,detail,active,first_seen,updated_at)
                      VALUES(?,?,?,?,?,?,?,?)
                      ON CONFLICT(alert_key) DO UPDATE SET kind=excluded.kind,level=excluded.level,title=excluded.title,
                      detail=excluded.detail,active=excluded.active,updated_at=excluded.updated_at''',
                   (key,kind,level,title,detail,1 if active else 0,now,now))
        # A dismissed warning stays hidden only for the current occurrence. Once the
        # underlying condition is healthy again, or a new occurrence starts, allow it
        # to notify the team again.
        if (not active) or (active and not was_active):
            db.execute("DELETE FROM reliability_notification_dismissals WHERE event_id=?",(event_id,))


def run_health_checks(app) -> None:
    db=database_check(app.DATABASE_PATH)
    _upsert_alert(app.STORE,'db-integrity','database','critical','Ошибка целостности базы',str(db.get('detail','')),not db.get('ok'))
    try:
        disk=shutil.disk_usage(app.DATA_DIR)
        low = disk.free < max(1_000_000_000, int(disk.total*0.05))
        _upsert_alert(app.STORE,'disk-space','disk','warning','Заканчивается место на диске',f'Свободно {disk.free} байт',low)
    except OSError: pass
    try:
        conn=app.connector_state_snapshot(); connected=bool(conn.get('connected'))
        _upsert_alert(app.STORE,'connector','whatsapp','critical','WhatsApp-коннектор недоступен',str(conn.get('status') or 'Нет связи'),not connected)
    except Exception: pass


def reliability_notification_events(app, limit: int = 20) -> tuple[int,list[dict[str,Any]]]:
    events: list[dict[str,Any]]=[]
    # Persisted infrastructure alerts.
    try:
        with app.STORE.connection() as db:
            rows=db.execute("SELECT * FROM reliability_alerts WHERE active=1 ORDER BY updated_at DESC LIMIT ?",(int(limit),)).fetchall()
        for r in rows:
            events.append({'event_id':'reliability:'+str(r['alert_key']),'kind':'system','title':str(r['title']),
                           'detail':str(r['detail'] or ''),'timestamp':int((_parse_dt(r['updated_at']) or datetime.now(timezone.utc)).timestamp()),
                           'level':str(r['level'] or 'warning')})
    except Exception: pass
    # SLA warning / overdue events are calculated live, without mutating ticket status.
    if str(app.STORE.get_setting('sla_alerts_enabled','1')).lower() in {'1','true','yes','on'}:
        try:
            tickets=app.STORE.list_tickets('new','','','',500,0)
            for t in tickets:
                state=app.ticket_sla_state(t)
                if not state.get('active') or not (state.get('warning') or state.get('overdue')): continue
                tid=int(t.get('id') or 0); overdue=bool(state.get('overdue')); remain=int(state.get('remaining_seconds') or 0)
                title=f"SLA {'ПРОСРОЧЕН' if overdue else 'приближается'} · заявка #{tid}"
                detail=f"{t.get('title','Заявка')} · {'просрочка' if overdue else 'осталось'} {abs(remain)//60} мин."
                events.append({'event_id':f'sla:{tid}:{"overdue" if overdue else "warn"}','kind':'sla','title':title,'detail':detail,
                               'timestamp':int(datetime.now(timezone.utc).timestamp()),'level':'critical' if overdue else 'warning'})
        except Exception: pass
    events.sort(key=lambda x:int(x.get('timestamp',0) or 0),reverse=True)
    reconcile_notification_dismissals(app.STORE,[e.get('event_id') for e in events],('reliability:','sla:'))
    events=filter_dismissed_notifications(app.STORE,events)
    return len(events),events[:limit]


def worker_loop(app, stop: threading.Event | None = None) -> None:
    stop=stop or threading.Event(); last_health=0.0; last_backup=0.0
    while not stop.wait(1.0):
        try: dispatch_due(app.STORE,25)
        except Exception as exc: print(f'Отложенная отправка: {exc}',flush=True)
        now=time.monotonic()
        if now-last_health >= 300:
            last_health=now
            try: run_health_checks(app)
            except Exception as exc: print(f'Диагностика: {exc}',flush=True)
        if now-last_backup >= 21600:
            last_backup=now
            try:
                backups=verify_backups(app.DATA_DIR)
                if backups:
                    bad=[x for x in backups[:3] if not x.get('ok')]
                    _upsert_alert(app.STORE,'backup-integrity','backup','critical','Проблема резервной копии',
                                  (str(bad[0].get('path')) if bad else ''),bool(bad))
            except Exception: pass
