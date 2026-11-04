"""Import a company profile while the Python server is stopped.

Use the same QUEUE_DATA_DIR as the server.
"""
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('config', type=Path)
args = parser.parse_args()
if not args.config.is_file():
    parser.error('Configuration file does not exist')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['QUEUE_GENERIC_CONFIG'] = str(args.config.resolve())

import queue_generic_profile
# Validate before opening the application database.
profile = queue_generic_profile.read_profile(args.config.parent)
from queue_core import prepare_database_path, resolve_data_dir
from queue_menu_config import SETTING, persist_profile
from ticketing import TicketStore

store = TicketStore(prepare_database_path(resolve_data_dir()))
with store.atomic():
    archive = json.loads(store.get_setting('request_category_archive_json', '{}'))
    old_profile = json.loads(store.get_setting(SETTING, '{}'))
    old_menu = json.loads(store.get_setting('request_menu_json', '[]'))
    new_keys = {row['key'] for row in profile['categories']}
    for row in [*old_menu, *old_profile.get('categories', [])]:
        if row['key'] not in new_keys:
            archive[row['key']] = row.get('label_ru', row.get('label', row['key']))
    persist_profile(store, profile, archive)
print(f'Профиль сохранён: {len(profile["categories"])} категорий. Запустите Python-сервер.')
