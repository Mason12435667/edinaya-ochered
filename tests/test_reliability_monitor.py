"""ReliabilityMonitor without HTTP: alerts from its checks reach the notification feed."""
import tempfile
import unittest
from pathlib import Path

import queue_reliability
from queue_reliability import ReliabilityMonitor
from ticketing import TicketStore


class ReliabilityMonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = TicketStore(self.root / "tickets.db")
        queue_reliability.initialize(self.store)
        self.connector = {"connected": True, "status": "Подключён"}
        self.monitor = ReliabilityMonitor(self.store, self.root, self.root / "tickets.db", self.root,
                                          lambda: dict(self.connector), lambda ticket: {}, 5 * 1024 * 1024)

    def events(self):
        return [event["event_id"] for event in self.monitor.notification_events()[1]]

    def test_disconnected_connector_raises_and_then_clears_an_alert(self):
        self.connector = {"connected": False, "status": "Нет QR"}
        self.monitor.run_health_checks()
        self.assertIn("reliability:connector", self.events())

        self.connector = {"connected": True}
        self.monitor.run_health_checks()
        self.assertNotIn("reliability:connector", self.events())

    def test_dismissed_alert_stays_hidden_while_it_lasts(self):
        self.connector = {"connected": False}
        self.monitor.run_health_checks()
        queue_reliability.dismiss_notification(self.store, "reliability:connector")
        self.monitor.run_health_checks()
        self.assertNotIn("reliability:connector", self.events())

    def test_diagnostics_use_the_given_database_and_limits(self):
        snapshot = self.monitor.diagnostic_snapshot()
        self.assertTrue(snapshot["database"]["ok"])
        self.assertEqual(snapshot["connector"], self.connector)
        self.assertEqual(snapshot["max_media_bytes"], 5 * 1024 * 1024)
        self.assertEqual(snapshot["secret_storage"]["legacy_path"], str(self.root / ".connector_token"))
        self.assertFalse(snapshot["maintenance"])


if __name__ == "__main__":
    unittest.main()
