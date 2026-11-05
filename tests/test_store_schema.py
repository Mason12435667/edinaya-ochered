"""Opening a database created by an older release upgrades it in place."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ticketing import TicketStore

# tickets as an early release created it: no shift/handoff columns, extra industry columns
OLD_TICKETS = """
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, sender TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '', chat_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal', title TEXT NOT NULL, summary TEXT NOT NULL,
    original_text TEXT NOT NULL DEFAULT '', attachment_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new', assigned_to TEXT NOT NULL DEFAULT '',
    bin_old TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO tickets (source, sender, category, title, summary, status, created_at, updated_at)
VALUES ('whatsapp', 'Старый отправитель', 'general', 'Старая заявка', 'Текст', 'in_progress',
        '2025-01-01T00:00:00+00:00', '2025-01-02T00:00:00+00:00');
"""


def create_old_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(OLD_TICKETS)


def index_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


class OldDatabaseUpgradeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def test_database_without_shift_columns_opens_and_keeps_its_tickets(self):
        path = self.dir / "old.db"
        create_old_database(path)

        store = TicketStore(path)  # used to fail: an index on shift_handoff came before the column

        ticket = store.get_ticket(1)
        self.assertEqual(ticket["title"], "Старая заявка")
        self.assertEqual(ticket["shift_handoff"], 0)
        self.assertEqual(ticket["first_response_at"], "2025-01-02T00:00:00+00:00")
        new_id = store.create_ticket({"title": "Новая заявка", "summary": "После обновления"})
        self.assertEqual([t["id"] for t in store.list_tickets()], [new_id, 1])

    def test_upgraded_database_has_the_same_indexes_as_a_new_one(self):
        old = self.dir / "old.db"
        create_old_database(old)
        TicketStore(old)
        TicketStore(self.dir / "new.db")
        self.assertEqual(index_names(old), index_names(self.dir / "new.db"))
        self.assertIn("idx_tickets_assignee_status", index_names(old))

    def test_reopening_changes_nothing(self):
        path = self.dir / "tickets.db"
        TicketStore(path).create_ticket({"title": "Заявка", "summary": "Текст"})
        before = index_names(path)
        store = TicketStore(path)
        self.assertEqual(index_names(path), before)
        self.assertEqual(len(store.list_tickets()), 1)


if __name__ == "__main__":
    unittest.main()
