"""Outbound queue under failures: scheduled sends, stale claims, chat id lookups."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests import support  # noqa: F401  (configures the environment before app modules load)
import queue_reliability
from ticketing import TicketStore

CHAT = "77010009999@c.us"


def iso(delta_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).replace(microsecond=0).isoformat()


class FreshStore(unittest.TestCase):
    """Each test gets its own database, so rows from other tests cannot interfere."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="eo-queue-"))
        self.store = TicketStore(self.folder / "tickets.db")
        queue_reliability.initialize(self.store)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.store.connection() as db:
            return db.execute(sql, args).fetchall()

    def outbound(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM outbound_messages WHERE chat_id=? ORDER BY id", CHAT)

    def due_schedule(self, body: str = "Напоминание") -> int:
        schedule_id = queue_reliability.schedule_message(self.store, CHAT, body, "Тест", iso(60))
        with self.store.connection() as db:
            db.execute("UPDATE reliability_scheduled_messages SET due_at=? WHERE id=?", (iso(-5), schedule_id))
        return schedule_id


class ScheduledDispatchTests(FreshStore):
    def test_crash_between_queueing_and_status_update_does_not_duplicate(self):
        schedule_id = self.due_schedule()
        original = self.store.queue_direct_message

        def queue_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("process died before the schedule was marked as queued")

        with mock.patch.object(self.store, "queue_direct_message", side_effect=queue_then_crash):
            self.assertEqual(queue_reliability.dispatch_due(self.store), 0)
        self.assertEqual(self.outbound(), [], "a failed pass must not leave a queued message behind")

        self.assertEqual(queue_reliability.dispatch_due(self.store), 1)
        messages = self.outbound()
        self.assertEqual(len(messages), 1)
        schedule = self.rows("SELECT * FROM reliability_scheduled_messages WHERE id=?", schedule_id)[0]
        self.assertEqual(schedule["status"], "queued")
        self.assertEqual(schedule["outbound_id"], messages[0]["id"])

    def test_repeated_pass_reuses_the_same_outbound_message(self):
        schedule_id = self.due_schedule()
        self.assertEqual(queue_reliability.dispatch_due(self.store), 1)
        # simulate a lost status update: the schedule looks due again
        with self.store.connection() as db:
            db.execute("UPDATE reliability_scheduled_messages SET status='scheduled' WHERE id=?", (schedule_id,))
        queue_reliability.dispatch_due(self.store)
        self.assertEqual(len(self.outbound()), 1)


class StaleClaimTests(FreshStore):
    def claim_and_abandon(self) -> dict | None:
        row = self.store.claim_outbound_message()
        if row:
            with self.store.connection() as db:  # the connector died: claim is older than the 60 s cutoff
                db.execute("UPDATE outbound_messages SET claimed_at=? WHERE id=?", (iso(-120), row["id"]))
        return row

    def test_exhausted_attempts_end_as_failed_and_can_be_retried(self):
        message_id = self.store.queue_direct_message(CHAT, "Ответ пользователю", "Тест")
        max_attempts = self.outbound()[0]["max_attempts"]
        for _ in range(max_attempts):
            self.assertIsNotNone(self.claim_and_abandon())
        self.assertIsNone(self.store.claim_outbound_message())

        message = self.outbound()[0]
        self.assertEqual(message["status"], "failed")
        self.assertTrue(message["failed_at"])
        self.assertIn(message_id, [row["id"] for row in queue_reliability.queue_problem_rows(self.store)])

        queue_reliability.retry_outbound(self.store, message_id)
        retried = self.store.claim_outbound_message()
        self.assertEqual(retried["id"], message_id)

    def test_attempts_left_go_back_to_pending(self):
        self.store.queue_direct_message(CHAT, "Ответ", "Тест")
        self.claim_and_abandon()
        self.assertIsNotNone(self.store.claim_outbound_message())

    def test_rows_already_stuck_in_pending_are_repaired(self):
        message_id = self.store.queue_direct_message(CHAT, "Старая запись", "Тест")
        with self.store.connection() as db:
            db.execute("UPDATE outbound_messages SET attempt_count=max_attempts WHERE id=?", (message_id,))
        self.assertIsNone(self.store.claim_outbound_message())
        self.assertEqual(self.outbound()[0]["status"], "failed")


class ChatIdLookupTests(FreshStore):
    def count_connections(self, fn):
        opened = []
        original = self.store.connection

        def counting():
            opened.append(1)
            return original()

        with mock.patch.object(self.store, "connection", side_effect=counting):
            result = fn()
        return result, len(opened)

    def test_phone_and_group_ids_need_no_database(self):
        for chat_id in ("77010001111@c.us", "120363000000000001@g.us", ""):
            with self.subTest(chat_id=chat_id):
                result, opened = self.count_connections(lambda: self.store.canonical_whatsapp_chat_id(chat_id))
                self.assertEqual(result, chat_id)
                self.assertEqual(opened, 0)

    def test_lid_alias_is_still_resolved(self):
        self.store.remember_whatsapp_chat_alias("123456789@lid", "77010002222@c.us")
        result, opened = self.count_connections(lambda: self.store.canonical_whatsapp_chat_id("123456789@lid"))
        self.assertEqual(result, "77010002222@c.us")
        self.assertEqual(opened, 1)


if __name__ == "__main__":
    unittest.main()
