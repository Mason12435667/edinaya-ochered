"""Bot profile: greeting and WhatsApp menu categories.

The profile lives in the database (setting PROFILE_SETTING) and is edited on
/admin/categories. config/company.json only seeds the database on first start;
after that the file is not read again.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import queue_user_locale
from queue_language import parse_menu_number
from queue_user_locale import tr

PROFILE_SETTING = "bot_profile_json"
REQUIRED_FIELD_NAMES = ("problem", "reference", "email")
MENU_NUMBERS = tuple(str(v) for v in range(1, 8)) + ("9",)

_ALLOWED = {"general", "support", "incident", "database", "mobile"}
_RESERVED = {"0", "8", "00"}
_DEFAULT = {
    "organization": "",
    "product": "Единая очередь",
    "greeting_ru": "Добро пожаловать в службу поддержки",
    "greeting_kz": "Қолдау қызметіне қош келдіңіз",
    "categories": [
        {
            "number": 1,
            "key": "general",
            "label_ru": "Общее обращение",
            "label_kz": "Жалпы өтініш",
            "prompt_ru": "Опишите проблему или вопрос. При необходимости приложите файл или снимок экрана",
            "prompt_kz": "Мәселені немесе сұрақты сипаттаңыз. Қажет болса файл немесе скриншот тіркеңіз",
            "required_fields": ["problem"],
        }
    ],
}


def validate_profile(raw: Any) -> dict[str, Any]:
    """Return a clean profile or raise ValueError with a message for the administrator."""
    if not isinstance(raw, dict):
        raise ValueError("Профиль бота должен быть JSON-объектом")
    merged = {**_DEFAULT, **raw}
    for field in ("greeting_ru", "greeting_kz"):
        merged[field] = str(merged.get(field, "") or "").strip()[:500] or _DEFAULT[field]
    merged["organization"] = str(merged.get("organization", "") or "").strip()[:120]
    rows = merged.get("categories")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError("Нужно от 1 до 8 категорий")
    numbers: set[str] = set()
    keys: set[str] = set()
    clean_rows = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("Категория должна быть JSON-объектом")
        number = str(item.get("number", ""))
        key = str(item.get("key", ""))
        if number not in MENU_NUMBERS or number in numbers:
            raise ValueError("Номера категорий должны быть разными: 1–7 или 9 (8 — «Мои активные заявки»)")
        if (key not in _ALLOWED and not re.fullmatch(r"custom_[a-z][a-z0-9_]{0,23}", key)) or key in keys:
            raise ValueError("Ключ категории: general, support, incident, database, mobile или custom_имя, без повторов")
        label_ru = str(item.get("label_ru", "")).strip()[:80]
        label_kz = str(item.get("label_kz", "")).strip()[:80]
        if not label_ru or not label_kz:
            raise ValueError("У каждой категории нужны названия на русском и казахском")
        fields = item.get("required_fields", ["problem"])
        if not isinstance(fields, list) or any(f not in REQUIRED_FIELD_NAMES for f in fields):
            raise ValueError("Неизвестное обязательное поле")
        numbers.add(number)
        keys.add(key)
        clean_rows.append({
            "number": int(number), "key": key, "label_ru": label_ru, "label_kz": label_kz,
            "prompt_ru": str(item.get("prompt_ru", "") or "").strip()[:1200],
            "prompt_kz": str(item.get("prompt_kz", "") or "").strip()[:1200],
            "required_fields": list(dict.fromkeys(fields)),  # configured order: the bot asks in this order
        })
    merged["categories"] = sorted(clean_rows, key=lambda row: row["number"])
    return merged


def read_profile(root: Path) -> dict[str, Any]:
    """The profile from config/company.json (QUEUE_GENERIC_CONFIG), or the built-in default."""
    config = Path(os.environ.get("QUEUE_GENERIC_CONFIG", "") or (root / "config" / "company.json"))
    if not config.is_file():
        return validate_profile(_DEFAULT)
    return validate_profile(json.loads(config.read_text(encoding="utf-8")))


def load_profile(store, root: Path) -> dict[str, Any]:
    """The stored profile; on first start it is seeded from company.json."""
    raw = store.get_setting(PROFILE_SETTING, "")
    if raw:
        return validate_profile(json.loads(raw))
    return save_profile(store, read_profile(root))


def save_profile(store, profile: Any) -> dict[str, Any]:
    clean = validate_profile(profile)
    store.set_setting(PROFILE_SETTING, json.dumps(clean, ensure_ascii=False))
    return clean


def profile_from_form(form: dict[str, str], current: dict[str, Any]) -> dict[str, Any]:
    """Build a profile from the /admin/categories form (validated later by save_profile)."""
    known = {str(row["key"]): row for row in current.get("categories", [])}
    rows = []
    for index in range(1, 10):
        key = form.get(f"key_{index}", "")
        if not key:
            continue
        if key not in known:
            raise ValueError("Категории изменились, обновите страницу и повторите")
        if form.get(f"delete_{index}") == "1":
            continue
        rows.append(_form_row(form, str(index), key, known[key].get("required_fields") or []))
    if form.get("label_ru_new", "").strip():
        rows.append(_form_row(form, "new", f"custom_c{secrets.token_hex(4)}", []))
    return {
        **current,
        "greeting_ru": form.get("greeting_ru", current.get("greeting_ru", "")),
        "greeting_kz": form.get("greeting_kz", current.get("greeting_kz", "")),
        "categories": rows,
    }


def _form_row(form: dict[str, str], suffix: str, key: str, previous: list[str]) -> dict[str, Any]:
    checked = [name for name in REQUIRED_FIELD_NAMES if form.get(f"req_{name}_{suffix}") == "1"]
    # keep the order the bot already asks in; newly ticked fields go last
    fields = [name for name in previous if name in checked] + [name for name in checked if name not in previous]
    return {
        "number": form.get(f"number_{suffix}", ""),
        "key": key,
        "label_ru": form.get(f"label_ru_{suffix}", ""),
        "label_kz": form.get(f"label_kz_{suffix}", "") or form.get(f"label_ru_{suffix}", ""),
        "prompt_ru": form.get(f"prompt_ru_{suffix}", ""),
        "prompt_kz": form.get(f"prompt_kz_{suffix}", ""),
        "required_fields": fields or ["problem"],
    }


class BotMenu:
    """The WhatsApp menu built from one validated profile.

    Read-only: a profile change builds a new BotMenu instead of editing this one.
    Texts for users follow the language of the current conversation (queue_user_locale).
    """

    def __init__(self, profile: dict[str, Any]):
        self.profile = profile
        self.rows = sorted(profile["categories"], key=lambda item: int(item["number"]))
        self._by_number = {str(row["number"]): row for row in self.rows}
        self._by_key = {str(row["key"]): row for row in self.rows}

    def has_category(self, category: str) -> bool:
        return str(category) in self._by_key

    def category_for_choice(self, choice: str) -> str:
        """Category key for a menu number typed by the user, or ""."""
        return str(self._by_number.get(str(parse_menu_number(choice)), {}).get("key", ""))

    def label(self, category: str, fallback: str = "") -> str:
        """Category name in the user's language."""
        row = self._by_key.get(str(category))
        if row:
            return str(row["label_kz"] if queue_user_locale.is_kz() else row["label_ru"])
        return fallback or str(category)

    def staff_label(self, category: str) -> str:
        """Russian category name (the staff interface), or "" for an unknown category."""
        return str(self._by_key.get(str(category), {}).get("label_ru", ""))

    def category_items(self) -> list[tuple[str, str]]:
        """(key, Russian name) of every category, in menu order."""
        return [(str(row["key"]), str(row["label_ru"])) for row in self.rows]

    def numbered_categories(self) -> list[tuple[str, str]]:
        """(menu number, category key) in menu order."""
        return [(number, str(row["key"])) for number, row in self._by_number.items()]

    def payload(self) -> list[dict[str, str]]:
        result = [{"number": n, "label": self.label(row["key"])} for n, row in self._by_number.items()]
        result.append({"number": "8", "label": tr("Мои активные заявки", "Менің белсенді өтінімдерім")})
        return result

    def text(self, error: bool = False) -> str:
        greeting = str(self.profile["greeting_kz"] if queue_user_locale.is_kz() else self.profile["greeting_ru"])
        entries = "\n".join(f"{n}. {self.label(row['key'])}" for n, row in self._by_number.items())
        active = tr("8. Мои активные заявки", "8. Менің белсенді өтінімдерім")
        prefix = tr("Выберите категорию обращения:", "Өтінім санатын таңдаңыз:")
        tail = tr("00 — сменить язык", "00 — тілді өзгерту")
        if error:
            prefix = tr("Выберите действительный номер категории:\n", "Санаттың дұрыс нөмірін таңдаңыз:\n") + prefix
        return f"{greeting}\n\n{prefix}\n{entries}\n{active}\n\n{tail}"

    def prompt(self, category: str) -> str:
        row = self._by_key.get(str(category))
        configured = str(row.get("prompt_kz" if queue_user_locale.is_kz() else "prompt_ru", "")) if row else ""
        return configured or tr("Опишите ваш вопрос", "Сұрағыңызды жазыңыз")

    def required_fields(self, category: str) -> list[str]:
        return list(self._by_key.get(str(category), {}).get("required_fields", ["problem"]))
