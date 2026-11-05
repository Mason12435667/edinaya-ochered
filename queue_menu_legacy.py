"""One-time readers for menu settings written before company profiles."""
import json
import re
from queue_core import normalize_message

MENU_ACTION_TICKET = "ticket"
MENU_ACTIONS = {"ticket", "instruction"}

def load_category_archive(store) -> dict[str, str]:
    """Labels of removed custom categories, kept so old tickets stay readable."""
    raw = store.get_setting("request_category_archive_json", "")
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(values, dict):
        return {}
    cleaned: dict[str, str] = {}
    for raw_key, raw_label in values.items():
        key = re.sub(r"[^a-z0-9_]", "", str(raw_key))[:40]
        label = normalize_message(str(raw_label))[:80]
        if key and label:
            cleaned[key] = label
    return cleaned

def load_menu_options(store, fallback) -> list[dict[str, object]]:
    raw = store.get_setting("request_menu_json", "")
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list):
                cleaned: list[dict[str, object]] = []
                seen: set[str] = set()
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    key = re.sub(r"[^a-z0-9_]", "", str(item.get("key", "")))[:40]
                    label = normalize_message(str(item.get("label", "")))[:80]
                    prompt = normalize_message(str(item.get("prompt", "")))[:1200]
                    action = str(item.get("action", MENU_ACTION_TICKET))
                    if action not in MENU_ACTIONS:
                        action = MENU_ACTION_TICKET
                    if not key or key in seen or not label:
                        continue
                    seen.add(key)
                    cleaned.append(
                        {
                            "key": key,
                            "label": label,
                            "prompt": prompt,
                            "enabled": bool(item.get("enabled", True)),
                            "action": action,
                        }
                    )
                if cleaned and any(bool(item["enabled"]) for item in cleaned):
                    return cleaned
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return fallback
