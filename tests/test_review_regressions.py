import copy
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import support
from tests.support import WhatsAppUser, admin_cookie, get, post_form


class InboundRegressionTests(unittest.TestCase):
    def test_duplicate_category_does_not_reset_draft(self):
        user = WhatsAppUser()
        user.register_profile()
        user.send('1', external_id='category-repeat-regression')
        user.say('Не работает сканер в кабинете 207')
        before = support.app.STORE.get_conversation_context(user.chat_id)
        repeat = user.send('1', external_id='category-repeat-regression')
        self.assertTrue(repeat.json()['duplicate'])
        self.assertEqual(support.app.STORE.get_conversation_context(user.chat_id), before)
        self.assertTrue(user.say('Готово')['created'])
        self.assertEqual(len(user.tickets()), 1)

    def test_recovery_reply_is_authorized_and_idempotent(self):
        payload = json.dumps({'external_id': 'recovery-regression', 'chat_id': '77010001234@c.us', 'reply': 'Recovered reply'}).encode()
        headers = {'Content-Type': 'application/json'}
        self.assertEqual(support.request('POST', '/api/inbound-recovery-reply', payload, headers).status, 401)
        headers['X-Webhook-Token'] = support.app.WEBHOOK_TOKEN
        first = support.request('POST', '/api/inbound-recovery-reply', payload, headers)
        second = support.request('POST', '/api/inbound-recovery-reply', payload, headers)
        self.assertEqual(first.status, 200)
        self.assertTrue(first.json()['queued'])
        self.assertEqual(first.json()['message_id'], second.json()['message_id'])


class RegistrationRegressionTests(unittest.TestCase):
    def test_registration_disabled_by_default(self):
        admin_cookie()
        with patch.dict(os.environ, {'QUEUE_ALLOW_SELF_REGISTRATION': '0'}):
            before = len(support.app.AUTH.list_users())
            response = post_form('/register', {'username': 'uninvited', 'display_name': 'Uninvited',
                                              'password': 'TestPassword123!', 'password2': 'TestPassword123!'})
            self.assertEqual(response.status, 403)
            self.assertEqual(len(support.app.AUTH.list_users()), before)
            self.assertFalse(support.app.AUTH.self_register_employee('uninvited', 'Uninvited', 'TestPassword123!')[0])
            self.assertEqual(get('/register').status, 403)
            self.assertNotIn('href="/register"', get('/login').text)

    def test_administrator_can_still_create_employee(self):
        admin_cookie()
        with patch.dict(os.environ, {'QUEUE_ALLOW_SELF_REGISTRATION': '0'}):
            result = support.app.AUTH.create_user('invited_regression', 'Invited', 'TestPassword123!', role='employee')
            self.assertTrue(result[0])
            self.assertTrue(support.app.AUTH.authenticate('invited_regression', 'TestPassword123!')['ok'])

    def test_open_registration_requires_explicit_opt_in(self):
        admin_cookie()
        with patch.dict(os.environ, {'QUEUE_ALLOW_SELF_REGISTRATION': '1'}):
            response = post_form('/register', {'username': 'optin_regression', 'display_name': 'Opt-in',
                'password': 'TestPassword123!', 'password2': 'TestPassword123!'})
            self.assertEqual(response.status, 303)
            self.assertTrue(response.cookies())


class MenuRegressionTests(unittest.TestCase):
    def setUp(self):
        self.cookie = admin_cookie()
        self.state = support.app.MENU_CONFIG
        profile = copy.deepcopy(self.state.profile)
        archive = dict(support.app.CATEGORY_ARCHIVE)
        categories = dict(support.app.CATEGORIES)
        def restore():
            self.state.profile = profile
            self.state.save(self.state.options(profile), archive)
            support.app.CATEGORIES.clear()
            support.app.CATEGORIES.update(categories)
        self.addCleanup(restore)

    def test_added_category_is_in_bot_menu_and_survives_restart(self):
        response = post_form('/admin/categories', {'action': 'add', 'new_label': 'Новая категория'}, self.cookie)
        self.assertEqual(response.status, 303)
        menu = support.app.menu_payload()
        self.assertIn('Новая категория', [r['label'] for r in menu])
        number = next(r['number'] for r in menu if r['label'] == 'Новая категория')
        user = WhatsAppUser()
        user.register_profile()
        self.assertEqual(user.say(number)['category'], support.app.configured_menu_category(number))
        fresh = subprocess.check_output([sys.executable, '-c',
            'import json, app; print(json.dumps(app.menu_payload(), ensure_ascii=False))'],
            cwd=support.app.ROOT, text=True)
        self.assertEqual(json.loads(fresh), menu)

    def test_menu_editor_updates_bot_prompt_and_keeps_kazakh_metadata(self):
        fields = {'row_count': len(support.app.MENU_OPTIONS)}
        previous = copy.deepcopy(self.state.profile)
        for i, row in enumerate(support.app.MENU_OPTIONS, 1):
            fields.update({f'key_{i}': row['key'], f'label_{i}': row['label'], f'prompt_{i}': 'Новая подсказка',
                           f'enabled_{i}': '1', f'action_{i}': row['action']})
        response = post_form('/admin/menu', fields, self.cookie)
        self.assertEqual(response.status, 303)
        self.assertEqual(support.app.configured_category_prompt('general'), 'Новая подсказка')
        self.assertEqual(self.state.profile['categories'][0]['prompt_kz'], previous['categories'][0]['prompt_kz'])
        self.assertEqual(support.app.configured_category_required_fields('custom_printers'), ['reference', 'problem'])

    def test_disabled_category_disappears_from_bot_menu(self):
        fields = {'row_count': len(support.app.MENU_OPTIONS)}
        disabled_key = support.app.MENU_OPTIONS[-1]['key']
        disabled_number = next(str(r['number']) for r in self.state.profile['categories'] if r['key'] == disabled_key)
        for i, row in enumerate(support.app.MENU_OPTIONS, 1):
            fields.update({f'key_{i}': row['key'], f'label_{i}': row['label'],
                           f'enabled_{i}': '0' if row['key'] == disabled_key else '1'})
        self.assertEqual(post_form('/admin/categories', fields, self.cookie).status, 303)
        self.assertEqual(support.app.configured_menu_category(disabled_number), '')
        self.assertNotIn(disabled_number, [r['number'] for r in support.app.menu_payload()])

    def test_failed_save_does_not_publish_partial_configuration(self):
        options = self.state.options(self.state.profile)
        options[0]['label'] = 'Must not be published'
        before = copy.deepcopy(self.state.profile)
        with patch.object(self.state.store, 'set_setting', side_effect=RuntimeError('disk error')):
            with self.assertLogs(level='WARNING'), self.assertRaises(RuntimeError):
                self.state.save(options, support.app.CATEGORY_ARCHIVE)
        self.assertEqual(self.state.profile, before)
        self.assertNotIn('Must not be published', support.app.main_menu_text())
