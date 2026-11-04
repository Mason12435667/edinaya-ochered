"""Configurable, domain-neutral profile for public or new-client deployments.

The JSON file seeds SQLite once; admin edits and bot replies share that snapshot.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

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


def read_profile(root: Path) -> dict[str, Any]:
    config = Path(os.environ.get("QUEUE_GENERIC_CONFIG", "") or (root / "config" / "company.json"))
    if not config.is_file():
        return _DEFAULT
    raw = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Generic profile JSON must be an object")
    merged = {**_DEFAULT, **raw}
    rows = merged.get("categories")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError("Generic profile needs 1-8 categories")
    numbers: set[str] = set()
    keys: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("Category must be an object")
        number = str(item.get("number", ""))
        key = str(item.get("key", ""))
        if number not in {str(v) for v in range(1, 8)} | {"9"} or number in numbers:
            raise ValueError("Category number must be unique and in 1-7, 9")
        if (key not in _ALLOWED and not re.fullmatch(r"custom_[a-z][a-z0-9_]{0,23}", key)) or key in keys:
            raise ValueError("Choose a unique generic key or custom_name (letters, digits and underscores)")
        if not str(item.get("label_ru", "")).strip() or not str(item.get("label_kz", "")).strip():
            raise ValueError("Both label_ru and label_kz are required")
        fields = item.get("required_fields", ["problem"])
        if not isinstance(fields, list) or any(f not in {"problem", "email", "reference"} for f in fields):
            raise ValueError("Unsupported required field")
        numbers.add(number)
        keys.add(key)
    return merged


def activate(namespace: dict[str, Any], root: Path) -> bool:
    """Bind bot and UI helpers to the persisted menu configuration."""
    if os.getenv("QUEUE_PROFILE", "generic").strip().lower() != "generic":
        return False
    from queue_menu_config import MenuConfig
    state = MenuConfig(namespace, lambda: read_profile(root))
    namespace["MENU_CONFIG"] = state

    def by_number():
        return {str(row["number"]): row for row in sorted(state.rows(), key=lambda r: int(r["number"]))}

    def by_key():
        return {row["key"]: row for row in state.profile["categories"]}

    def tr(ru: str, kz: str) -> str:
        return namespace["queue_user_locale"].tr(ru, kz)

    def configured_menu_category(choice: str) -> str:
        return str(by_number().get(str(namespace["parse_menu_number"](choice)), {}).get("key", ""))

    def user_menu_label(category: str, fallback: str = "") -> str:
        row = by_key().get(str(category))
        if row:
            return str(row["label_kz"] if namespace["queue_user_locale"].is_kz() else row["label_ru"])
        return fallback or str(category)

    def menu_payload() -> list[dict[str, str]]:
        result = [{"number": n, "label": user_menu_label(row["key"])} for n, row in by_number().items()]
        result.append({"number": "8", "label": tr("Мои активные заявки", "Менің белсенді өтінімдерім")})
        return result

    def main_menu_text(error: bool = False) -> str:
        kz = namespace["queue_user_locale"].is_kz()
        greeting = str(state.profile["greeting_kz"] if kz else state.profile["greeting_ru"])
        entries = "\n".join(f"{n}. {user_menu_label(row['key'])}" for n, row in by_number().items())
        active = tr("8. Мои активные заявки", "8. Менің белсенді өтінімдерім")
        prefix = tr("Выберите категорию обращения:", "Өтінім санатын таңдаңыз:")
        tail = tr("00 — сменить язык", "00 — тілді өзгерту")
        if error:
            prefix = tr("Выберите действительный номер категории:\n", "Санаттың дұрыс нөмірін таңдаңыз:\n") + prefix
        return f"{greeting}\n\n{prefix}\n{entries}\n{active}\n\n{tail}"

    def configured_category_prompt(category: str) -> str:
        row = by_key().get(str(category))
        if not row:
            return tr("Опишите ваш вопрос", "Сұрағыңызды жазыңыз")
        return str(row.get("prompt_kz" if namespace["queue_user_locale"].is_kz() else "prompt_ru", "")) or tr("Опишите ваш вопрос", "Сұрағыңызды жазыңыз")

    def configured_category_required_fields(category: str) -> list[str]:
        return list(by_key().get(str(category), {}).get("required_fields", ["problem"]))

    def active_ticket_category_items() -> list[tuple[str, str]]:
        return [(str(row["key"]), str(row["label_ru"])) for row in state.rows() if row.get("action", "ticket") == "ticket"]

    namespace.update({
        "configured_menu_category": configured_menu_category,
        "user_menu_label": user_menu_label,
        "menu_payload": menu_payload,
        "main_menu_text": main_menu_text,
        "start_menu_prompt": main_menu_text,
        "configured_category_prompt": configured_category_prompt,
        "configured_category_required_fields": configured_category_required_fields,
        "active_ticket_category_items": active_ticket_category_items,
    })
    return True
