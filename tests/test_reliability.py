"""Reliability guarantees: races, rollbacks, backups, recovery replies, profile import.

Several scenarios come from the review branch fix/review-regressions-2026-11-04.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import support
import queue_backup_bundle
import queue_generic_profile
import queue_reliability as reliability
from ticketing import TicketStore

CHAT = "77010001111@c.us"
PROJECT = Path(__file__).resolve().parents[1]


class StoreGuaranteeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TicketStore(self.root / "tickets.db")
        reliability.initialize(self.store)

    def outbound_count(self) -> int:
        with self.store.connection() as db:
            return db.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0]

    def test_two_dispatchers_enqueue_once(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO reliability_scheduled_messages(chat_id,body,actor,due_at,created_at) VALUES (?,?,?,?,?)",
                       (CHAT, "scheduled", "tester", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: reliability.dispatch_due(self.store), range(2)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(self.outbound_count(), 1)

    def test_parallel_request_id_returns_same_message(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.store.queue_direct_message(CHAT, "parallel", "tester", request_id="same-id"), range(4)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.outbound_count(), 1)

    def test_unknown_send_result_is_not_retried(self):
        message_id = self.store.queue_direct_message(CHAT, "uncertain", "tester")
        self.store.claim_outbound_message()
        self.store.begin_outbound_send(message_id)  # the connector started sending, then vanished
        with self.store.connection() as db:
            db.execute("UPDATE outbound_messages SET claimed_at='2020-01-01T00:00:00+00:00' WHERE id=?", (message_id,))
        self.assertIsNone(self.store.claim_outbound_message())
        self.assertEqual(reliability.queue_problem_rows(self.store)[0]["status"], "uncertain")

    def test_inbound_rollback_includes_dedup_marker(self):
        with self.assertRaises(RuntimeError):
            with self.store.atomic_inbound():
                self.store.claim_inbound_message("rollback-id", "tester")
                self.store.set_setting("regression", "temporary")
                raise RuntimeError("rollback")
        self.assertEqual(self.store.get_setting("regression", "absent"), "absent")
        self.assertTrue(self.store.claim_inbound_message("rollback-id", "tester"), "a failed attempt can be retried")

    def test_backup_restores_database_and_attachment(self):
        media = self.root / "outbound-media" / "test.txt"
        media.parent.mkdir()
        media.write_text("attachment")
        self.store.queue_direct_message(CHAT, "", "tester", media_path=str(media))
        destination = self.root / "backups"
        destination.mkdir()
        target = queue_backup_bundle.create_bundle(self.store.database_path, self.root, destination)
        result = queue_backup_bundle.verify_restore(target.with_suffix(".tar.gz"), destination)
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"], 2)


class ProfileImportTests(unittest.TestCase):
    def test_import_replaces_the_stored_profile_and_archives_removed_categories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = TicketStore(root / "tickets.db")
            queue_generic_profile.save_profile(store, {"categories": [
                {"number": 1, "key": "general", "label_ru": "Общее", "label_kz": "Жалпы"},
                {"number": 2, "key": "custom_old", "label_ru": "Старая", "label_kz": "Ескі"}]})
            config = root / "company.json"
            config.write_text(json.dumps({"categories": [{"number": 9, "key": "general", "label_ru": "Общее",
                "label_kz": "Жалпы", "required_fields": ["reference", "problem"]}]}), encoding="utf-8")
            result = subprocess.run([sys.executable, str(PROJECT / "scripts" / "import_company_profile.py"), str(config)],
                                    env={**os.environ, "QUEUE_DATA_DIR": str(root)}, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            profile = json.loads(store.get_setting(queue_generic_profile.PROFILE_SETTING))
            self.assertEqual(profile["categories"][0]["number"], 9)
            self.assertEqual(profile["categories"][0]["required_fields"], ["reference", "problem"])
            self.assertEqual(json.loads(store.get_setting("request_category_archive_json"))["custom_old"], "Старая")


class RecoveryReplyTests(unittest.TestCase):
    """The connector's spool asks the server to answer messages delivered after an outage."""

    def post(self, body: dict, token: bool = True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Webhook-Token"] = support.app.WEBHOOK_TOKEN
        return support.request("POST", "/api/inbound-recovery-reply", json.dumps(body).encode(), headers)

    def test_requires_the_connector_token_and_is_idempotent(self):
        body = {"external_id": "recovery-1", "chat_id": "77010001234@c.us", "reply": "Ответ после восстановления"}
        self.assertEqual(self.post(body, token=False).status, 401)
        first, second = self.post(body), self.post(body)
        self.assertEqual(first.status, 200, first.text[:300])
        self.assertTrue(first.json()["queued"])
        self.assertEqual(first.json()["message_id"], second.json()["message_id"])


if __name__ == "__main__":
    unittest.main()
