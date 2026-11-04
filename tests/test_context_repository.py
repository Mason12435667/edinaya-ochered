"""Persistence compatibility and failures across multi-write context operations."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import queue_contextual_tickets as context
from queue_context_repository import ContextRepository
from ticketing import TicketStore


class ContextRepositoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = TicketStore(Path(temp.name) / "tickets.db")
        context.initialize(self.store)
        self.repository = ContextRepository(self.store)
        self.key = "77010000001@c.us"

    def collect(self, text="Не печатает принтер", message_key="details"):
        return context.collect_request(self.store, self.key, "general", text,
                                       message_key=message_key, chat_id=self.key)

    def commit(self):
        ticket_id = self.store.create_ticket({"chat_id": self.key, "original_text": "Обращение"})
        context.commit_ticket(self.store, self.key, ticket_id, "general", message_key="finish")
        return ticket_id

    def rows(self, table):
        # Table names are constants in this test, never user input.
        with self.store.connection() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_existing_state_and_history_survive_reinitialization(self):
        self.collect()
        state = self.repository.state(self.key)
        # These are the serialized columns used by earlier releases.
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE contextual_conversation_state SET skipped_json=?, field_meta_json=? WHERE contact_key=?",
                (json.dumps(["reference"]), json.dumps({"problem": {"confidence": 0.8}}), self.key),
            )
        context.initialize(self.store)
        restored = ContextRepository(self.store).state(self.key)
        self.assertEqual(restored["session_id"], state["session_id"])
        self.assertEqual(restored["skipped"], ["reference"])
        self.assertEqual(restored["field_meta"]["problem"]["confidence"], 0.8)
        self.assertEqual(context.request_fragments(self.store, self.key), ["Не печатает принтер"])

    def test_failed_collection_rolls_back_draft_history_and_state(self):
        self.collect()
        before = {table: self.rows(table) for table in (
            "contextual_conversation_state", "contextual_message_analysis", "ticket_context_history")}
        draft = self.store.get_conversation_draft(self.key)
        with patch.object(ContextRepository, "record_analysis", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.collect("Также не открывается почта", "more")
        self.assertEqual(self.store.get_conversation_draft(self.key), draft)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)
        self.collect("Также не открывается почта", "more")
        self.assertEqual(len(context.request_fragments(self.store, self.key)), 2)

    def test_commit_keeps_field_sources_and_links_only_current_session(self):
        self.store.save_whatsapp_chat_messages(self.key, [
            {"id": "old", "body": "Старое обращение"},
            {"id": "current", "body": "Не открывается почта"},
        ])
        self.collect("Старое обращение", "old")
        context.reset_context(self.store, self.key, "general")
        self.store.clear_conversation_draft(self.key)
        self.collect("Не открывается почта", "current")
        ticket_id = self.commit()
        fields = {row["field_name"]: row for row in context.ticket_fields(self.store, ticket_id)}
        self.assertEqual(fields["problem"]["source_message_key"], "current")
        history = context.ticket_history(self.store, ticket_id)
        self.assertTrue(any(row["message_key"] == "current" for row in history))
        self.assertFalse(any(row["message_key"] == "old" for row in history))
        self.assertEqual(self.repository.state(self.key)["state"], "active")
        messages = {row["message_key"]: row["ticket_id"] for row in self.rows("whatsapp_chat_messages")}
        self.assertEqual(messages, {"old": 0, "current": ticket_id})

    def test_failed_commit_does_not_leave_partial_ticket_context(self):
        self.collect()
        history = self.rows("ticket_context_history")
        state = self.repository.state(self.key)
        ticket_id = self.store.create_ticket({"chat_id": self.key})
        with patch.object(ContextRepository, "write_state", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                context.commit_ticket(self.store, self.key, ticket_id, "general")
        self.assertEqual(context.ticket_fields(self.store, ticket_id), [])
        self.assertEqual(self.rows("ticket_context_history"), history)
        self.assertEqual(self.repository.state(self.key), state)

    def test_failed_followup_rolls_back_fields_and_ticket_text(self):
        self.collect()
        ticket_id = self.commit()
        fields = context.ticket_fields(self.store, ticket_id)
        original = self.store.get_ticket(ticket_id)["original_text"]
        history = context.ticket_history(self.store, ticket_id)
        record_history = ContextRepository.record_history

        def fail_after_ticket_update(repository, **event):
            if event["event_type"] == "followup":
                raise RuntimeError("disk failure")
            return record_history(repository, **event)

        with patch.object(ContextRepository, "record_history", autospec=True, side_effect=fail_after_ticket_update):
            with self.assertRaises(RuntimeError):
                context.record_followup(self.store, self.key, ticket_id, text="Не открывается почта")
        self.assertEqual(context.ticket_fields(self.store, ticket_id), fields)
        self.assertEqual(self.store.get_ticket(ticket_id)["original_text"], original)
        self.assertEqual(context.ticket_history(self.store, ticket_id), history)

    def test_history_application_and_audit_are_atomic(self):
        self.collect()
        ticket_id = self.commit()
        self.repository.record_history(session_id="old", ticket_id=ticket_id, contact_key=self.key,
                                       chat_id=self.key, event_type="field_found", new_value="123456")
        history = context.ticket_history(self.store, ticket_id)
        history_id = history[0]["id"]
        fields = context.ticket_fields(self.store, ticket_id)
        with patch.object(ContextRepository, "add_event", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                context.apply_history_item(self.store, ticket_id, history_id, "reference", "Иван")
        self.assertEqual(context.ticket_fields(self.store, ticket_id), fields)
        self.assertEqual(context.ticket_history(self.store, ticket_id), history)
        self.assertTrue(context.apply_history_item(self.store, ticket_id, history_id, "reference", "Иван")[0])
        fields = {row["field_name"]: row["value"] for row in context.ticket_fields(self.store, ticket_id)}
        self.assertEqual(fields["reference"], "123456")
        self.assertIn("reference", [row["field_name"] for row in context.ticket_history(self.store, ticket_id)])

    def test_history_cannot_be_copied_from_another_ticket(self):
        self.collect()
        ticket_id = self.commit()
        other = self.store.create_ticket({})
        history_id = context.ticket_history(self.store, ticket_id)[0]["id"]
        self.assertFalse(context.apply_history_item(self.store, other, history_id, "problem", "Иван")[0])
        self.assertEqual(context.ticket_fields(self.store, other), [])
