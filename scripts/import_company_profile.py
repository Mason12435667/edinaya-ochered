"""Replace the bot profile stored in the database with a company.json file.

After the first start the bot menu lives in the database and is edited on
/admin/categories; config/company.json is no longer read. Use this script to
load the file again on purpose (e.g. a new deployment template).

    python scripts/import_company_profile.py [path/to/company.json]

Uses QUEUE_DATA_DIR like the server. A running server keeps its menu in memory:
restart it afterwards. Removed categories keep their label on old tickets.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import queue_generic_profile  # noqa: E402
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
    store.set_setting(ARCHIVE_SETTING, json.dumps(archive, ensure_ascii=False))
    queue_generic_profile.save_profile(store, profile)
    labels = ", ".join(f"{row['number']}. {row['label_ru']}" for row in profile["categories"])
    print(f"Профиль бота обновлён: {labels}. Перезапустите сервер.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
