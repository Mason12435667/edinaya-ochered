"""Templates and the code that fills them must stay in step."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests import support  # noqa: F401  (sets up the import path and environment)

import queue_templates

BACKEND = Path(queue_templates.__file__).parent


def template_calls():
    for path in sorted(BACKEND.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "render_template":
                yield path.name, node


def placeholders(name):
    return set(queue_templates.PLACEHOLDER.findall(queue_templates.load_template(name)))


class TemplateTests(unittest.TestCase):
    def test_every_call_passes_exactly_the_values_its_template_uses(self):
        calls = list(template_calls())
        self.assertGreater(len(calls), 5)
        for filename, call in calls:
            name = call.args[0].value
            with self.subTest(template=name, file=filename):
                self.assertEqual({keyword.arg for keyword in call.keywords}, placeholders(name))

    def test_every_template_is_used(self):
        used = {call.args[0].value for _, call in template_calls()}
        files = {path.relative_to(queue_templates.TEMPLATE_DIR).as_posix()
                 for path in queue_templates.TEMPLATE_DIR.rglob("*.html")}
        self.assertEqual(files, used)

    def test_values_are_inserted_as_they_are(self):
        original = queue_templates.load_template
        queue_templates.load_template = lambda name: "<b>{{ first }}</b>{{second}}"
        try:
            self.assertEqual(
                queue_templates.render_template("x", first="{{ second }}", second=1),
                "<b>{{ second }}</b>1",
            )
            with self.assertRaises(KeyError):
                queue_templates.render_template("x", first="only")
        finally:
            queue_templates.load_template = original

    def test_template_file_final_newline_is_not_content(self):
        self.assertFalse(queue_templates.load_template("auth/login.html").endswith("\n"))
