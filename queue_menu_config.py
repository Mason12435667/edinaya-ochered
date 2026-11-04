"""Persistent menu configuration; JSON is a seed for the first startup only."""
from __future__ import annotations

import copy
import json
import threading

SETTING = "company_profile_json"
NUMBERS = (1, 2, 3, 4, 5, 6, 7, 9)


def persist_profile(store, profile, archive):
    """Persist all menu settings together, including the compatibility projection."""
    with store.atomic():
        store.set_setting(SETTING, json.dumps(profile, ensure_ascii=False))
        store.set_setting("request_menu_json", json.dumps(MenuConfig.options(profile), ensure_ascii=False))
        store.set_setting("request_category_archive_json", json.dumps(archive, ensure_ascii=False))


class MenuConfig:
    def __init__(self, namespace, seed):
        self.namespace = namespace
        self.store = namespace["STORE"]
        self.lock = threading.RLock()
        saved = self.store.get_setting(SETTING, "")
        self.profile = json.loads(saved) if saved else copy.deepcopy(seed())
        if saved:
            self.publish(self.profile)
        else:
            # Preserve edits made by the old admin UI before this upgrade.
            legacy = self.store.get_setting("request_menu_json", "")
            options = namespace["load_menu_options"]() if legacy else self.options(self.profile)
            self.save(options, namespace["CATEGORY_ARCHIVE"])

    @staticmethod
    def options(profile):
        return [{"key": r["key"], "label": r["label_ru"], "prompt": r.get("prompt_ru", ""),
                 "enabled": r.get("enabled", True), "action": r.get("action", "ticket")}
                for r in profile["categories"]]

    def rows(self):
        # Each reader sees one published snapshot; it never waits for a DB writer.
        return [r for r in self.profile["categories"] if r.get("enabled", True)]

    def save(self, options, archive):
        with self.lock:
            archive = dict(archive)
            if not 1 <= len(options) <= len(NUMBERS):
                raise ValueError("Допустимо от 1 до 8 пунктов меню")
            if not any(o.get("enabled") for o in options):
                raise ValueError("Оставьте хотя бы один включённый пункт")
            base = self.profile
            previous = {r["key"]: r for r in base["categories"]}
            # Existing numbers are stable; a new category takes a free slot.
            used = {int(previous[o["key"]]["number"]) for o in options if o["key"] in previous}
            free = iter(n for n in NUMBERS if n not in used)
            rows = []
            for option in options:
                old = previous.get(option["key"])
                row = dict(old) if old else {"number": next(free), "key": option["key"],
                                          "required_fields": ["problem"]}
                row.update(label_ru=option["label"], prompt_ru=option["prompt"],
                           enabled=bool(option["enabled"]), action=option["action"])
                if not old:
                    row.update(label_kz=option["label"], prompt_kz=option["prompt"])
                rows.append(row)
            profile = {**base, "categories": rows}
            persist_profile(self.store, profile, archive)
            self.namespace["CATEGORY_ARCHIVE"].clear()
            self.namespace["CATEGORY_ARCHIVE"].update(archive)
            self.publish(profile)

    def publish(self, profile):
        ns = self.namespace
        ns["MENU_OPTIONS"][:] = self.options(profile)
        ns["MAX_MENU_OPTIONS"] = len(NUMBERS)
        ns["CATEGORIES"].update(ns["CATEGORY_ARCHIVE"])
        ns["CATEGORIES"].update({r["key"]: r["label_ru"] for r in profile["categories"]})
        enabled = [r for r in profile["categories"] if r.get("enabled", True)]
        mappings = {
            "V3_MENU_CATEGORIES": {str(r["number"]): r["key"] for r in enabled},
            "V3_MENU_LABELS_RU": {str(r["number"]): r["label_ru"] for r in enabled},
            "V3_MENU_LABELS_KZ": {str(r["number"]): r["label_kz"] for r in enabled},
            "V3_REQUIRED_FIELDS": {r["key"]: r.get("required_fields", ["problem"]) for r in enabled},
            "V3_CATEGORY_LABELS_KZ": {r["key"]: r["label_kz"] for r in enabled},
        }
        for name, values in mappings.items():
            ns[name].clear()
            ns[name].update(values)
        ns["V3_MENU_LABELS_RU"]["8"] = "Мои активные заявки"
        ns["V3_MENU_LABELS_KZ"]["8"] = "Менің белсенді өтінімдерім"
        self.profile = profile
