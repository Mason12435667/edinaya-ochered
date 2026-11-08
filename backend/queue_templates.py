"""Page templates from frontend/templates: {{ name }} is replaced with a value.

Values are inserted as they are, so callers escape user data before passing it.
A value is never scanned again, so text like {{ x }} in user data stays literal.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "frontend" / "templates"
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@lru_cache(maxsize=None)
def load_template(name: str) -> str:
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return text[:-1] if text.endswith("\n") else text  # the file's final newline is not page content


def render_template(template_name: str, /, **values: object) -> str:
    return PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), load_template(template_name))
