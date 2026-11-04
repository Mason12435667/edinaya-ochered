import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import queue_backup_bundle
import queue_reliability as reliability
from ticketing import TicketStore


class ReliabilityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TicketStore(self.root / 'tickets.db')
        reliability.initialize(self.store)

    def schedule(self):
        with self.store.connection() as db:
            return db.execute("INSERT INTO reliability_scheduled_messages(chat_id,body,actor,due_at,created_at) VALUES (?,?,?,?,?)",
                              ('77010001111@c.us', 'scheduled regression', 'tester',
                               '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')).lastrowid

    def test_two_dispatchers_enqueue_once(self):
        self.schedule()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: reliability.dispatch_due(self.store), range(2)))
        self.assertEqual(sum(counts), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbound_messages').fetchone()[0], 1)

    def test_unknown_send_result_is_not_automatically_retried(self):
        mid = self.store.queue_direct_message('77010001111@c.us', 'uncertain regression', 'tester')
        self.store.claim_outbound_message()
        self.store.begin_outbound_send(mid)
        with self.store.connection() as db:
            db.execute("UPDATE outbound_messages SET claimed_at='2020-01-01T00:00:00+00:00' WHERE id=?", (mid,))
        self.assertIsNone(self.store.claim_outbound_message())
        self.assertEqual(reliability.queue_problem_rows(self.store)[0]['status'], 'uncertain')

    def test_inbound_rollback_includes_dedup_marker(self):
        with self.assertLogs(level='WARNING'):
            with self.assertRaises(RuntimeError):
                with self.store.atomic_inbound():
                    self.store.claim_inbound_message('rollback-id', 'tester')
                    self.store.set_setting('regression', 'temporary')
                    raise RuntimeError('rollback')
        self.assertEqual(self.store.get_setting('regression', 'absent'), 'absent')
        self.assertTrue(self.store.claim_inbound_message('rollback-id', 'tester'))

    def test_parallel_request_id_returns_same_message(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.store.queue_direct_message(
                '77010001111@c.us', 'parallel regression', 'tester', request_id='same-id'), range(4)))
        self.assertEqual(len(set(ids)), 1)

    def test_backup_restores_database_and_attachment(self):
        media = self.root / 'outbound-media' / 'test.txt'
        media.parent.mkdir()
        media.write_text('attachment regression')
        self.store.queue_direct_message('77010001111@c.us', '', 'tester', media_path=str(media))
        destination = self.root / 'backups'
        destination.mkdir()
        target = queue_backup_bundle.create_bundle(self.store.database_path, self.root, destination)
        result = queue_backup_bundle.verify_restore(target.with_suffix('.tar.gz'), destination)
        self.assertTrue(result['ok'])
        self.assertEqual(result['files'], 2)

    def test_explicit_profile_import_can_replace_oversized_legacy_menu(self):
        legacy = [{'key': f'custom_c{i}', 'label': f'Category {i}', 'prompt': 'Describe',
                   'enabled': True, 'action': 'ticket'} for i in range(9)]
        self.store.set_setting('request_menu_json', json.dumps(legacy))
        config = self.root / 'company.json'
        config.write_text(json.dumps({'categories': [{'number': 9, 'key': 'general',
            'label_ru': 'Общее', 'label_kz': 'Жалпы', 'required_fields': ['reference', 'problem']}]}))
        project = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, str(project / 'scripts/import_company_profile.py'), str(config)],
                       env={**os.environ, 'QUEUE_DATA_DIR': str(self.root)},
                       check=True, capture_output=True, text=True)
        profile = json.loads(self.store.get_setting('company_profile_json'))
        self.assertEqual(profile['categories'][0]['number'], 9)
        self.assertEqual(profile['categories'][0]['required_fields'], ['reference', 'problem'])
        archive = json.loads(self.store.get_setting('request_category_archive_json'))
        self.assertEqual(len(archive), 9)
