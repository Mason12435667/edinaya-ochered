"""Errors that the code deliberately ignores still reach the log (queue_core.log_suppressed)."""
from __future__ import annotations

import unittest
from unittest import mock

import queue_core


def fail(where: str, error: Exception) -> None:
    try:
        raise error
    except Exception:
        queue_core.log_suppressed(where)


class LogSuppressedTests(unittest.TestCase):
    def setUp(self):
        queue_core._SUPPRESSED.clear()

    def test_logs_error_with_traceback(self):
        with self.assertLogs("queue", "WARNING") as logs:
            fail("tests.place", ValueError("boom"))
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertIn("tests.place", record.getMessage())
        self.assertIs(record.exc_info[0], ValueError)

    def test_same_error_is_throttled_and_counted(self):
        clock = [1000.0]
        with mock.patch.object(queue_core.time, "monotonic", lambda: clock[0]):
            with self.assertLogs("queue", "WARNING") as logs:
                fail("tests.loop", KeyError("x"))
                for _ in range(5):
                    fail("tests.loop", KeyError("x"))
                clock[0] += queue_core.SUPPRESSED_LOG_INTERVAL + 1
                fail("tests.loop", KeyError("x"))
        self.assertEqual(len(logs.records), 2)
        self.assertIn("repeated 5 more times", logs.records[1].getMessage())

    def test_different_errors_or_places_are_logged_separately(self):
        with self.assertLogs("queue", "WARNING") as logs:
            fail("tests.a", ValueError())
            fail("tests.a", TypeError())
            fail("tests.b", ValueError())
        self.assertEqual(len(logs.records), 3)


if __name__ == "__main__":
    unittest.main()
