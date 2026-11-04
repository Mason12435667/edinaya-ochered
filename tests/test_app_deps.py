"""Feature modules use only what queue_app_deps.AppDependencies declares."""
from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path

import queue_app_deps
from tests import support

ROOT = Path(__file__).resolve().parent.parent
FIELDS = {field.name for field in dataclasses.fields(queue_app_deps.AppDependencies)}


class AppDependenciesTests(unittest.TestCase):
    def test_modules_read_only_declared_fields(self):
        for path in sorted(ROOT.glob("queue_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            used = {node.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "deps"}
            with self.subTest(module=path.name):
                self.assertEqual(sorted(used - FIELDS), [])

    def test_app_fills_every_field(self):
        deps = support.app.DEPS
        for name in FIELDS:
            with self.subTest(field=name):
                self.assertIsNotNone(getattr(deps, name))

    def test_bot_menu_follows_profile_changes(self):
        before = support.app.DEPS.bot_menu()
        support.app.apply_bot_profile(before.profile)
        self.assertIsNot(support.app.DEPS.bot_menu(), before)
        self.assertEqual(support.app.DEPS.bot_menu().category_items(), before.category_items())


if __name__ == "__main__":
    unittest.main()
