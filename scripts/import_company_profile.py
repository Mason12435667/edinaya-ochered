"""Заменяет профиль бота в базе содержимым файла company.json.

После первого запуска меню бота хранится в базе и редактируется на /admin/categories, а
config/company.json больше не читается. Этот скрипт нужен, чтобы осознанно загрузить файл заново
(например, новый шаблон для установки).

    python scripts/import_company_profile.py [path/to/company.json]

Использует QUEUE_DATA_DIR, как и сервер. Работающий сервер держит меню в памяти, поэтому после
запуска скрипта его нужно перезапустить. Удалённые категории сохраняют название в старых заявках.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import queue_generic_profile  # noqa: E402
import queue_menu_config  # noqa: E402
from queue_core import prepare_database_path, resolve_data_dir  # noqa: E402
from ticketing import TicketStore  # noqa: E402

ARCHIVE_SETTING = "request_category_archive_json"


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        os.environ["QUEUE_GENERIC_CONFIG"] = str(Path(argv[1]).resolve())
    try:
        profile = queue_generic_profile.read_profile(ROOT)
    except (ValueError, json.JSONDecodeError, OSError) as error:
        print(f"Файл профиля не принят: {error}", file=sys.stderr)
        return 1
    store = TicketStore(prepare_database_path(resolve_data_dir()))
    previous = json.loads(store.get_setting(queue_generic_profile.PROFILE_SETTING, "") or "{}")
    archive = json.loads(store.get_setting(ARCHIVE_SETTING, "") or "{}")
    kept = {row["key"] for row in profile["categories"]}
    for row in previous.get("categories", []):
        if row.get("key") not in kept:
            archive[str(row["key"])] = str(row.get("label_ru", row["key"]))
    # profile, its old-format projection and the archive change together
    queue_menu_config.persist_profile(store, profile, archive)
    labels = ", ".join(f"{row['number']}. {row['label_ru']}" for row in profile["categories"])
    print(f"Профиль бота обновлён: {labels}. Перезапустите сервер.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
