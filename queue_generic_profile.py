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


def activate(namespace: dict[str, Any], root: Path, profile: dict[str, Any] | None = None) -> bool:
    """Apply the bot profile to the app module's menu tables and menu functions."""
    if os.getenv("QUEUE_PROFILE", "generic").strip().lower() != "generic":
        return False
    profile = profile or load_profile(namespace["STORE"], root)
    rows = sorted(profile["categories"], key=lambda item: int(item["number"]))
    by_number = {str(row["number"]): row for row in rows}
    by_key = {str(row["key"]): row for row in rows}
    generic_keys = set(by_key)
    categories = namespace["CATEGORIES"]
    for key in list(categories):
        if key not in generic_keys:
            categories[key] = "Не используется"
    for row in rows:
        categories[row["key"]] = str(row["label_ru"]).strip()[:80]

    menu_rows = [
        {"key": str(row["key"]), "label": str(row["label_ru"]),
         "prompt": str(row.get("prompt_ru", "")).strip(), "enabled": True, "action": "ticket"}
        for row in rows
    ]
    namespace["MENU_OPTIONS"][:] = menu_rows
    namespace["DEFAULT_MENU_KEYS"][:] = [item["key"] for item in menu_rows]
    for key in list(namespace["DEFAULT_MENU_LABELS"]):
        namespace["DEFAULT_MENU_LABELS"][key] = "Не используется"
    for item in menu_rows:
        namespace["DEFAULT_MENU_LABELS"][item["key"]] = item["label"]

    namespace["V3_MENU_CATEGORIES"].clear()
    namespace["V3_MENU_CATEGORIES"].update({n: row["key"] for n, row in by_number.items()})
    for k in list(namespace["V3_MENU_LABELS_RU"]):
        namespace["V3_MENU_LABELS_RU"][k] = "Не настроено"
        namespace["V3_MENU_LABELS_KZ"][k] = "Бапталмаған"
    for n, row in by_number.items():
        namespace["V3_MENU_LABELS_RU"][n] = str(row["label_ru"])
        namespace["V3_MENU_LABELS_KZ"][n] = str(row["label_kz"])
    namespace["V3_MENU_LABELS_RU"]["8"] = "Мои активные заявки"
    namespace["V3_MENU_LABELS_KZ"]["8"] = "Менің белсенді өтінімдерім"
    namespace["V3_REQUIRED_FIELDS"].clear()
    namespace["V3_REQUIRED_FIELDS"].update({k: list(by_key[k].get("required_fields", ["problem"])) for k in generic_keys})
    namespace["V3_CATEGORY_LABELS_KZ"].clear()
    namespace["V3_CATEGORY_LABELS_KZ"].update({k: str(row["label_kz"]) for k, row in by_key.items()})
    namespace["USER_MENU_LABELS_KZ"].update({k: str(row["label_kz"]) for k, row in by_key.items()})

    def tr(ru: str, kz: str) -> str:
        return namespace["queue_user_locale"].tr(ru, kz)

    def configured_menu_category(choice: str) -> str:
        return str(by_number.get(str(namespace["parse_menu_number"](choice)), {}).get("key", ""))

    def user_menu_label(category: str, fallback: str = "") -> str:
        row = by_key.get(str(category))
        if row:
            return str(row["label_kz"] if namespace["queue_user_locale"].is_kz() else row["label_ru"])
        return fallback or str(category)

    def menu_payload() -> list[dict[str, str]]:
        result = [{"number": n, "label": user_menu_label(row["key"])} for n, row in by_number.items()]
        result.append({"number": "8", "label": tr("Мои активные заявки", "Менің белсенді өтінімдерім")})
        return result

    def main_menu_text(error: bool = False) -> str:
        kz = namespace["queue_user_locale"].is_kz()
        greeting = str(profile["greeting_kz"] if kz else profile["greeting_ru"])
        entries = "\n".join(f"{n}. {user_menu_label(row['key'])}" for n, row in by_number.items())
        active = tr("8. Мои активные заявки", "8. Менің белсенді өтінімдерім")
        prefix = tr("Выберите категорию обращения:", "Өтінім санатын таңдаңыз:")
        tail = tr("00 — сменить язык", "00 — тілді өзгерту")
        if error:
            prefix = tr("Выберите действительный номер категории:\n", "Санаттың дұрыс нөмірін таңдаңыз:\n") + prefix
        return f"{greeting}\n\n{prefix}\n{entries}\n{active}\n\n{tail}"

    def configured_category_prompt(category: str) -> str:
        row = by_key.get(str(category))
        if not row:
            return tr("Опишите ваш вопрос", "Сұрағыңызды жазыңыз")
        return str(row.get("prompt_kz" if namespace["queue_user_locale"].is_kz() else "prompt_ru", "")) or tr("Опишите ваш вопрос", "Сұрағыңызды жазыңыз")

    def configured_category_required_fields(category: str) -> list[str]:
        return list(by_key.get(str(category), {}).get("required_fields", ["problem"]))

    def active_ticket_category_items() -> list[tuple[str, str]]:
        return [(str(row["key"]), str(row["label_ru"])) for row in rows]

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
