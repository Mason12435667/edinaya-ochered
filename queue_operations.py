"""Ticket operations with explicit dependencies, revision checks and handoff history."""
import html


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


class Conflict(ValueError):pass
