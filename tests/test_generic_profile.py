"""Проверка config/company.json (queue_generic_profile.read_profile)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import queue_generic_profile


def category(number=1, key="general", **extra):
    row = {"number": number, "key": key, "label_ru": "Общее", "label_kz": "Жалпы"}
    row.update(extra)
    return row


class ReadProfileTests(unittest.TestCase):
    def read(self, config) -> dict:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "company.json"
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"QUEUE_GENERIC_CONFIG": str(path)}):
                return queue_generic_profile.read_profile(Path(folder))

    def assertRejected(self, config):
        with self.assertRaises(ValueError):
            self.read(config)

    def test_missing_file_uses_default_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / "absent.json")
            with mock.patch.dict(os.environ, {"QUEUE_GENERIC_CONFIG": missing}):
                profile = queue_generic_profile.read_profile(Path(folder))
        self.assertEqual([row["key"] for row in profile["categories"]], ["general"])

    def test_valid_profile_is_merged_with_defaults(self):
        profile = self.read({"organization": "ООО Тест", "categories": [
            category(1, "general"), category(9, "custom_printers", required_fields=["reference", "problem"]),
        ]})
        self.assertEqual(profile["organization"], "ООО Тест")
        self.assertEqual(profile["product"], "Единая очередь")
        self.assertEqual(len(profile["categories"]), 2)

    def test_reserved_menu_number_is_rejected(self):
        self.assertRejected({"categories": [category(8)]})
        self.assertRejected({"categories": [category(0)]})

    def test_duplicate_number_or_key_is_rejected(self):
        self.assertRejected({"categories": [category(1, "general"), category(1, "support")]})
        self.assertRejected({"categories": [category(1, "general"), category(2, "general")]})

    def test_bad_key_is_rejected(self):
        self.assertRejected({"categories": [category(1, "printers")]})
        self.assertRejected({"categories": [category(1, "custom_Printers")]})

    def test_labels_are_required(self):
        self.assertRejected({"categories": [category(1, label_kz="")]})

    def test_unknown_required_field_is_rejected(self):
        self.assertRejected({"categories": [category(1, required_fields=["phone"])]})

    def test_category_count_limits(self):
        self.assertRejected({"categories": []})
        nine = [category(n, f"custom_c{n}") for n in (1, 2, 3, 4, 5, 6, 7, 9)] + [category(9, "custom_extra")]
        self.assertRejected({"categories": nine})


if __name__ == "__main__":
    unittest.main()
