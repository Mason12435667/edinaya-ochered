"""Ticket operations with explicit dependencies, revision checks and handoff history."""
import html
import secrets
from urllib.parse import urlparse,quote


def initialize(store):
    with store.connection() as db:
        columns={r[1] for r in db.execute('PRAGMA table_info(tickets)')}
        if 'revision' not in columns: db.execute('ALTER TABLE tickets ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
        db.executescript('''
        CREATE TRIGGER IF NOT EXISTS ticket_revision_85 AFTER UPDATE ON tickets
        WHEN NEW.revision=OLD.revision BEGIN
          UPDATE tickets SET revision=OLD.revision+1 WHERE id=NEW.id;
        END;
        CREATE TABLE IF NOT EXISTS ticket_notifications(
          id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, status TEXT NOT NULL,
          message_id INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS ticket_handoffs(
          id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, actor TEXT NOT NULL,
          note TEXT NOT NULL, accepted_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          accepted_at TEXT NOT NULL DEFAULT '');
        ''')


def notification_panel(store,ticket_id):
    with store.connection() as db:
        row=db.execute('''SELECT n.*,o.status AS delivery,o.last_error,o.provider_id FROM ticket_notifications n
          LEFT JOIN outbound_messages o ON o.id=n.message_id WHERE n.ticket_id=? ORDER BY n.id DESC LIMIT 1''',(ticket_id,)).fetchone()
    if not row: label='Нет данных об уведомлении';detail='Статус появится после следующего закрытия заявки'
    else:
        states={'pending':'Ожидает отправки','processing':'Подготовка к отправке','sending':'Отправляется',
                'sent':'Отправлено в WhatsApp','uncertain':'Результат неизвестен: проверьте переписку','failed':'Ошибка отправки'}
        label=states.get(row['delivery'],'Не поставлено в очередь')
        detail=row['last_error'] or row['reason'] or ''
        if row['message_id'] and row['delivery'] is None: label='Запись отправки удалена по сроку хранения'
    return '<section class="panel"><h2>Уведомление пользователю</h2><strong>'+html.escape(label)+'</strong><p>'+html.escape(detail)+'</p><small>«Отправлено» не означает, что сообщение прочитано</small></section>'


def handoff_history(store,ticket_id):
    with store.connection() as db:rows=db.execute('SELECT * FROM ticket_handoffs WHERE ticket_id=? ORDER BY id DESC LIMIT 10',(ticket_id,)).fetchall()
    return ''.join('<p><strong>'+html.escape(r['actor'])+'</strong>: '+html.escape(r['note'])+'<br><small>'+html.escape('Принял: '+r['accepted_by'] if r['accepted_by'] else 'Ожидает принятия')+'</small></p>' for r in rows)


def get(handler,app):
    if urlparse(handler.path).path!='/shift-handoff':return False
    with app.STORE.connection() as db:
        tickets=[dict(r) for r in db.execute("SELECT * FROM tickets WHERE status NOT IN ('done','invalid') ORDER BY shift_handoff DESC,updated_at LIMIT 200")]
    cards=[]
    for t in tickets:
        action='accept' if t['shift_handoff'] else 'transfer'
        label='Принять' if t['shift_handoff'] else 'Передать смене'
        note='' if t['shift_handoff'] else '<label>Что сделано и что осталось<textarea name="handoff_note" maxlength="2000" required></textarea></label>'
        cards.append(f'''<section class="panel"><h2><a href="/ticket?id={t['id']}">#{t['id']} · {app.e(t['title'])}</a></h2>
        <p>Исполнитель: {app.e(t['assigned_to'] or 'Не назначен')}</p>{handoff_history(app.STORE,t['id'])}
        <form method="post" action="/handoff"><input type="hidden" name="ticket_id" value="{t['id']}">
        <input type="hidden" name="revision" value="{t['revision']}"><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}">
        <input type="hidden" name="next" value="/shift-handoff">{note}<button class="button" name="action" value="{action}">{label}</button></form></section>''')
    handler.html_response(app.layout('Передача смены','<h1>Передача смены</h1><p>До 200 незавершённых заявок. Принятие записывается от имени вашего аккаунта.</p>'+(''.join(cards) or '<p>Незавершённых заявок нет</p>'),show_admin=handler.is_admin()))
    return True


class Conflict(ValueError):pass


def post(handler,app):
    path=urlparse(handler.path).path
    if path=='/api/inbound-recovery-reply':
        payload=handler.read_authorized_json(150000)
        if payload is None:return True
        chat=app.valid_chat_id(str(payload.get('chat_id','')))
        external=str(payload.get('external_id',''))
        if not chat or not external:handler.json_response({'error':'invalid_identity'},400);return True
        if not app.global_auto_reply_enabled() or app.STORE.manual_chat_mode(chat) or app.STORE.manual_whatsapp_contact(chat,'') or app.queue_productivity.auto_reply_blocked(app.STORE,chat):
            handler.json_response({'suppressed':True});return True
        import hashlib
        request_id='inbound-recovery:'+hashlib.sha256(external.encode()).hexdigest()
        message=app.STORE.queue_direct_message(chat,str(payload.get('reply',''))[:32000],'Система',request_id=request_id)
        if message:app.queue_realtime.notify_outbound()
        handler.json_response({'queued':bool(message),'message_id':message},200 if message else 400)
        return True
    if path not in {'/status','/quick-status','/quick-priority','/classification','/handoff'}:return False
    form=handler.read_form()
    if not secrets.compare_digest(form.get('csrf_token',''),app.ADMIN_FORM_TOKEN):
        handler.json_response({'error':'Обновите страницу: сессия формы устарела'},403);return True
    try:
        tid=int(form.get('ticket_id','0'));revision=int(form.get('revision','-1'))
        actor=app.work_actor();message_id=0
        with app.STORE.atomic_inbound():
            ticket=app.STORE.get_ticket(tid)
            if not ticket:raise ValueError('Заявка не найдена')
            if revision<0 or ticket['revision']!=revision:raise Conflict('Заявку уже изменили. Обновите страницу и проверьте актуальные данные')
            if path in {'/status','/quick-status'}:
                status=form.get('status','');reason=form.get('close_reason','');comment=form.get('close_comment','')
                valid,error=app.queue_workflow.validate_close(app.STORE,tid,status,reason,comment)
                if not valid:raise ValueError(error)
                assignee=form.get('employee','') if path=='/status' else (actor if status=='in_progress' else ticket['assigned_to'])
                if assignee not in app.assignment_names():assignee=ticket['assigned_to'] or actor
                updated,message_id=app.update_ticket_status(tid,status,actor,assignee,allow_reopen=handler.is_admin(),close_reason=reason,close_comment=comment)
                if not updated:raise ValueError('Изменение статуса недоступно')
            elif path=='/handoff':
                accept=form.get('action')=='accept';note=form.get('handoff_note','').strip()[:2000]
                if accept and not ticket['shift_handoff']:raise Conflict('Заявка уже принята или не передавалась')
                if not accept and ticket['shift_handoff']:raise Conflict('Заявка уже передана смене')
                if not accept and not note:raise ValueError('Укажите, что сделано и что осталось')
                if not app.STORE.set_ticket_handoff(tid,not accept,actor):raise ValueError('Закрытую заявку нельзя передать')
                with app.STORE.connection() as db:
                    if accept:db.execute("UPDATE ticket_handoffs SET accepted_by=?,accepted_at=CURRENT_TIMESTAMP WHERE ticket_id=? AND accepted_by=''",(actor,tid))
                    else:db.execute('INSERT INTO ticket_handoffs(ticket_id,actor,note) VALUES(?,?,?)',(tid,actor,note))
            else:
                if not app.STORE.update_priority(tid,form.get('priority',''),actor):raise ValueError('Неверный приоритет')
        app.queue_realtime.notify('ticket')
        if message_id:app.queue_realtime.notify_outbound()
        if path.startswith('/quick-'):handler.json_response({'updated':True,'ticket_id':tid,'notification_queued':bool(message_id)})
        else:handler.redirect('/shift-handoff' if form.get('next')=='/shift-handoff' else f'/ticket?id={tid}')
    except (ValueError,TypeError) as error:
        code=409 if isinstance(error,Conflict) else 400
        if path.startswith('/quick-'):handler.json_response({'updated':False,'error':str(error)},code)
        else:handler.html_response(app.layout('Изменение не сохранено','<section class="panel"><h1>Изменение не сохранено</h1><p>'+app.e(str(error))+'</p><a href="/ticket?id='+str(handler.form_int(form,'ticket_id'))+'">Открыть актуальную заявку</a></section>',show_admin=handler.is_admin()),code)
    except Exception:
        handler.json_response({'updated':False,'error':'Не удалось сохранить изменение. Данные не изменены, повторите позже'},503)
    return True

