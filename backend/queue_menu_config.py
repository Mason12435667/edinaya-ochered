"""Сохраняет и публикует снимки меню, не изменяя глобальные объекты приложения."""
from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

from queue_generic_profile import PROFILE_SETTING, profile_from_form, validate_profile
from queue_menu_legacy import load_category_archive, load_menu_options
from ticketing import TicketStore

SETTING = PROFILE_SETTING
LEGACY_PROFILE_SETTING = "company_profile_json"
NUMBERS = (1, 2, 3, 4, 5, 6, 7, 9)


def persist_profile(store, profile, archive):
    """Write the profile and its old-format projection atomically.

    The projection supports upgrades/rollbacks; runtime readers use the profile.
    """
    with store.atomic():
        store.set_setting(SETTING, json.dumps(profile, ensure_ascii=False))
        store.set_setting("request_menu_json", json.dumps(MenuConfig.options(profile), ensure_ascii=False))
        store.set_setting("request_category_archive_json", json.dumps(archive, ensure_ascii=False))


@dataclass(frozen=True)
class MenuSnapshot:
    profile: dict
    archive: dict[str, str]


class MenuConfig:
    max_options = len(NUMBERS)

    def __init__(self, store: TicketStore, seed: Callable[[], dict], category_labels: dict[str, str] | None = None):
        self.store = store
        self.lock = threading.RLock()
        # Historical labels are shared with ticket views; selection uses snapshots.
        self.category_labels = category_labels if category_labels is not None else {}
        saved = store.get_setting(SETTING, "")
        previous_profile = store.get_setting(LEGACY_PROFILE_SETTING, "") if not saved else ""
        profile = validate_profile(json.loads(saved or previous_profile) if saved or previous_profile else seed(), allow_legacy_keys=bool(saved or previous_profile))
        self._snapshot = MenuSnapshot(profile, load_category_archive(store))
        if saved:
            self._publish(profile, self.archive)
        elif previous_profile:
            self.replace(profile, self.archive)
        else:
            self.save(load_menu_options(store, self.options(profile)), self.archive)

    @property
    def profile(self) -> dict:
        return copy.deepcopy(self._snapshot.profile)

    @property
    def archive(self) -> dict[str, str]:
        return dict(self._snapshot.archive)

    @staticmethod
    def options(profile):
        return [{"key": r["key"], "label": r["label_ru"], "prompt": r.get("prompt_ru", ""),
                 "enabled": r.get("enabled", True), "action": r.get("action", "ticket")}
                for r in profile["categories"]]

    def menu_options(self) -> list[dict]:
        return self.options(self._snapshot.profile)

    def rows(self) -> list[dict]:
        return [r for r in self.profile["categories"] if r.get("enabled", True)]

    def save(self, options, archive):
        with self.lock:
            if not 1 <= len(options) <= self.max_options:
                raise ValueError("Допустимо от 1 до 8 пунктов меню")
            if not any(o.get("enabled") for o in options):
                raise ValueError("Оставьте хотя бы один включённый пункт")
            base = self._snapshot.profile
            previous = {r["key"]: r for r in base["categories"]}
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
            self.replace({**base, "categories": rows}, archive)

    def update_from_form(self, form: dict[str, str]) -> None:
        with self.lock:
            self.replace(profile_from_form(form, self.profile), self.archive)

    def replace(self, profile: dict, archive: dict[str, str]) -> None:
        with self.lock:
            profile = validate_profile(profile, allow_legacy_keys=True)
            archive = dict(archive)
            keys = {r["key"] for r in profile["categories"]}
            for row in self._snapshot.profile["categories"]:
                if row["key"] not in keys:
                    archive[row["key"]] = row["label_ru"]
            persist_profile(self.store, profile, archive)
            self._publish(profile, archive)

    def _publish(self, profile, archive):
        self.category_labels.update(archive)
        self.category_labels.update({r["key"]: r["label_ru"] for r in profile["categories"]})
        # Inbound transactions read without waiting for an admin DB writer.
        self._snapshot = MenuSnapshot(profile, archive)
