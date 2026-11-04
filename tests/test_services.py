"""Exercise application services directly, with independent stores and no HTTP server."""
import concurrent.futures
import itertools
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import queue_contextual_tickets
import queue_operations
import queue_productivity
import queue_reliability
import queue_user_locale
import queue_workflow
from queue_attachment_archive import AttachmentArchive
from queue_bot_menu import BotMenu
from queue_contact_profiles import ContactProfiles
from queue_inbound_service import InboundDependencies, InboundService
from queue_menu_config import LEGACY_PROFILE_SETTING, SETTING, MenuConfig
from queue_outbound_service import OutboundService
from queue_ticket_service import TicketDependencies, TicketService
from ticketing import MENU_CONTEXT, STATUSES, TicketStore


class Events:
    def __init__(self):
        self.notifications = []

    def outbound_revision(self):
        return 1

    def wait_for_outbound(self, revision, timeout):
        return 1

    def notify(self, kind="state"):
        self.notifications.append(kind)
        return len(self.notifications)

    def notify_outbound(self):
        return self.notify("outbound")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ids = itertools.count(1)
        language = queue_user_locale.current_language()
        self.addCleanup(queue_user_locale.set_language, language)

    def services(self, name="one"):
        root = self.root / name
        root.mkdir()
        store = TicketStore(root / "tickets.db")
        for module in (queue_operations, queue_productivity, queue_contextual_tickets,
                       queue_workflow, queue_reliability):
            module.initialize(store)
        categories = {}
        config = MenuConfig(store, lambda: {"greeting_ru": name, "greeting_kz": name,
            "categories": [{"number": 1, "key": "general", "label_ru": name, "label_kz": name,
                            "prompt_ru": "Опишите проблему", "prompt_kz": "Мәселені сипаттаңыз"}]}, categories)
        menu = BotMenu(config)
        contacts = ContactProfiles(store)
        tickets = TicketService(TicketDependencies(store, STATUSES, categories, MENU_CONTEXT, {},
            contacts.get_contact_language, lambda: True, menu.main_menu_text,
            menu.user_menu_label, lambda key, fallback: STATUSES.get(key, fallback)))
        media = root / "media"
        outgoing = root / "outbound"
        outgoing.mkdir()
        inbound = InboundService(InboundDependencies(store, menu, contacts, tickets, media,
            lambda *args: "", lambda: "Тестовый сотрудник", lambda: True))
        events = Events()
        archive = AttachmentArchive(store, media, outgoing, outgoing / "uploads")
        outbound = OutboundService(store, archive, events, lambda: True)
        return SimpleNamespace(store=store, config=config, menu=menu, contacts=contacts, inbound=inbound,
                               outbound=outbound, archive=archive, events=events, outgoing=outgoing)

    def send(self, service, text, chat="77010000001@c.us", external_id=""):
        result = service.inbound.handle({"external_id": external_id or f"service-{next(self.ids)}",
            "chat_id": chat, "phone": "+" + chat.split("@")[0], "sender": "Пользователь",
            "text": text, "message_type": "chat"})
        self.assertIn(result.status, (200, 201), result.payload)
        return result.payload

    def register(self, service, chat="77010000001@c.us", language="1"):
        self.send(service, "Здравствуйте", chat)
        self.send(service, language, chat)
        return self.send(service, "Подразделение: Бухгалтерия\nДолжность: Бухгалтер", chat)

    def test_services_import_without_initializing_application(self):
        result = subprocess.run([sys.executable, "-c",
            "import sys, queue_inbound_service, queue_outbound_service; assert 'app' not in sys.modules"],
            cwd=Path(__file__).resolve().parents[1], env={k:v for k,v in os.environ.items() if k != 'WEBHOOK_TOKEN'},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_instances_keep_menus_contacts_and_dedup_independent(self):
        first, second = self.services("first"), self.services("second")
        self.register(first)
        self.register(second, language="2")
        self.assertEqual(first.contacts.get_contact_language("77010000001@c.us"), "ru")
        self.assertEqual(second.contacts.get_contact_language("77010000001@c.us"), "kz")
        for service, name in ((first, "first"), (second, "second")):
            response = self.send(service, "0", external_id="shared-provider-id")
            self.assertNotIn("duplicate", response)
            self.assertIn(name, response["reply"])
            self.send(service, "1")
            self.send(service, "Не открывается почта")
            created = self.send(service, "Готово")
            self.assertEqual(service.store.get_ticket(created["ticket_id"])["title"], name)
        self.assertTrue(self.send(first, "0", external_id="shared-provider-id")["duplicate"])

    def test_direct_dialog_creates_ticket_and_keeps_locale(self):
        service = self.services()
        self.register(service)
        queue_user_locale.set_language("kz")
        self.send(service, "1")
        self.send(service, "Не открывается приложение после обновления")
        result = self.send(service, "Готово")
        self.assertTrue(result["created"])
        ticket = service.store.get_ticket(result["ticket_id"])
        self.assertEqual(ticket["assigned_to"], "Тестовый сотрудник")
        self.assertIn("Бухгалтерия", ticket["summary"])
        self.assertEqual(queue_user_locale.current_language(), "kz")

    def test_concurrent_dialogs_do_not_share_response_state(self):
        service = self.services()
        chats = [f"7701000000{i}@c.us" for i in range(1, 5)]
        for index, chat in enumerate(chats):
            self.register(service, chat, language="1" if index % 2 == 0 else "2")
        def process(chat):
            response = service.inbound.handle({"external_id": chat, "chat_id": chat, "text": "1"})
            return response.status, response.payload.get("category"), response.payload.get("reply")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(process, chats))
        for index, (status, category, reply) in enumerate(responses):
            self.assertEqual((status, category), (200, "general"))
            self.assertIn("Опишите проблему" if index % 2 == 0 else "Мәселені сипаттаңыз", reply)

    def test_auto_reply_switch_mutes_output_without_losing_dialog_changes(self):
        service = self.services()
        service.inbound = InboundService(replace(service.inbound.dependencies, auto_reply_enabled=lambda: False))
        response = self.register(service)
        self.assertTrue(response["auto_reply_disabled"])
        self.assertNotIn("reply", response)
        self.assertNotIn("menu_options", response)
        self.send(service, "1")
        self.send(service, "Не открывается приложение после обновления")
        created = self.send(service, "Готово")
        self.assertTrue(created["created"])
        self.assertNotIn("reply", created)
        self.assertIsNotNone(service.store.get_ticket(created["ticket_id"]))

    def test_failed_dialog_rolls_back_changes_and_can_be_retried(self):
        service = self.services()
        original = service.contacts.set_contact_language
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("storage interrupted")
        payload = {"external_id": "retry-language", "chat_id": "77010000001@c.us", "text": "1"}
        with patch.object(service.contacts, "set_contact_language", side_effect=interrupted), self.assertLogs(level="WARNING"):
            result = service.inbound.handle(payload)
        self.assertEqual(result.status, 503)
        self.assertEqual(service.contacts.get_contact_language(payload["chat_id"]), "")
        result = service.inbound.handle(payload)
        self.assertEqual(result.status, 200)
        self.assertNotIn("duplicate", result.payload)
        self.assertEqual(service.contacts.get_contact_language(payload["chat_id"]), "ru")

    def test_menu_snapshots_cannot_be_mutated_by_readers(self):
        service = self.services()
        profile = service.config.profile
        profile["categories"][0]["label_ru"] = "external mutation"
        options = service.config.menu_options()
        options[0]["enabled"] = False
        self.assertEqual(service.menu.configured_menu_category("1"), "general")
        self.assertEqual(service.config.profile["categories"][0]["label_ru"], "one")

    def test_failed_menu_save_rolls_back_storage_and_keeps_published_snapshot(self):
        service = self.services()
        before = service.config.profile
        persisted = service.store.get_setting(SETTING)
        changed = service.config.profile
        changed["categories"][0]["label_ru"] = "Uncommitted label"
        original = service.store.set_setting
        def fail_archive(key, value):
            if key == "request_category_archive_json":
                raise OSError("disk error")
            return original(key, value)
        with patch.object(service.store, "set_setting", side_effect=fail_archive), self.assertLogs(level="WARNING"):
            with self.assertRaises(OSError):
                service.config.replace(changed, service.config.archive)
        self.assertEqual(service.config.profile, before)
        self.assertEqual(service.store.get_setting(SETTING), persisted)

    def test_previous_patch_profile_is_migrated_once(self):
        import json
        service = self.services()
        profile = service.config.profile
        profile["greeting_ru"] = "Сохранённое приветствие"
        service.store.set_setting(SETTING, "")
        service.store.set_setting(LEGACY_PROFILE_SETTING, json.dumps(profile))
        migrated = MenuConfig(service.store, lambda: self.fail("must migrate saved profile"))
        self.assertEqual(migrated.profile["greeting_ru"], "Сохранённое приветствие")
        self.assertTrue(service.store.get_setting(SETTING))
        service.store.set_setting(LEGACY_PROFILE_SETTING, "invalid old JSON")
        self.assertEqual(MenuConfig(service.store, lambda: self.fail("must use new profile")).profile, migrated.profile)

    def test_legacy_generated_category_keys_survive_migration(self):
        import json
        service = self.services()
        service.store.set_setting(SETTING, "")
        service.store.set_setting("request_menu_json", json.dumps([{
            "key": "custom_12345", "label": "Старая категория", "prompt": "Описание", "enabled": True}]))
        migrated = MenuConfig(service.store, lambda: service.config.profile)
        self.assertEqual(migrated.rows()[0]["key"], "custom_12345")
        restarted = MenuConfig(service.store, lambda: self.fail("must read persisted profile"))
        self.assertEqual(restarted.rows(), migrated.rows())

    def test_outbound_preserves_attachment_and_quote_on_repeated_result(self):
        service = self.services()
        chat = "77010000001@c.us"
        source = service.outgoing / "document.txt"
        source.write_text("attachment bytes")
        service.store.save_whatsapp_chat_messages(chat, [{"id":"incoming", "body":"Вопрос", "sender":"Пользователь", "from_me":False}])
        message_id = service.store.queue_direct_message(chat, "Ответ", "Сотрудник", media_path=str(source),
            media_mime="text/plain", media_name="document.txt", reply_to_key="incoming")
        claimed = service.outbound.claim({}).payload["message"]
        self.assertEqual(claimed["id"], message_id)
        self.assertEqual(claimed["reply_preview_body"], "Вопрос")
        self.assertTrue(service.outbound.heartbeat({"message_id": message_id}, begin=True).payload["updated"])
        payload = {"message_id": message_id, "status": "sent", "provider_id": "provider-outgoing"}
        self.assertEqual(service.outbound.complete(payload).payload["queue_status"], "sent")
        service.outbound.complete(payload)
        saved = service.store.get_whatsapp_message(chat, "provider-outgoing")
        self.assertEqual(saved["quoted_body"], "Вопрос")
        self.assertEqual(Path(saved["media_path"]).read_text(), "attachment bytes")
        self.assertEqual(len(service.store.list_saved_whatsapp_messages(chat)), 2)

    def test_outbound_uncertain_result_is_not_requeued(self):
        service = self.services()
        mid = service.store.queue_direct_message("77010000001@c.us", "Ответ", "Сотрудник")
        service.outbound.claim({})
        service.outbound.heartbeat({"message_id":mid}, begin=True)
        response = service.outbound.complete({"message_id":mid, "status":"uncertain", "error":"network lost"})
        self.assertEqual(response.payload["queue_status"], "uncertain")
        self.assertIsNone(service.outbound.claim({}).payload["message"])

    def test_missing_attachment_does_not_reach_connector(self):
        service = self.services()
        service.store.queue_direct_message("77010000001@c.us", "Файл", "Сотрудник",
            media_path=str(service.outgoing / "missing.txt"), media_mime="text/plain", media_name="missing.txt")
        response = service.outbound.claim({})
        self.assertIsNone(response.payload["message"])
        self.assertIn("error", response.payload)

    def test_stale_category_context_cannot_start_a_draft(self):
        import queue_dialog_flow
        service = self.services()
        self.register(service)
        service.store.set_conversation_context("77010000001@c.us", "general")
        queue_dialog_flow.save(service.store, "77010000001@c.us", {})
        result = self.send(service, "Не печатает принтер")
        self.assertTrue(result.get("strict_category_required"))
        self.assertEqual(service.store.get_conversation_draft("77010000001@c.us"), {})

    def test_category_switch_discards_previous_draft_and_fragments(self):
        service = self.services()
        profile = service.config.profile
        profile["categories"].append({"number": 2, "key": "custom_email",
            "label_ru": "Почта", "label_kz": "Пошта", "prompt_ru": "Опишите проблему"})
        service.config.replace(profile, service.config.archive)
        self.register(service)
        self.send(service, "1")
        self.send(service, "Не печатает принтер")
        self.send(service, "2")
        self.assertEqual(service.store.get_conversation_draft("77010000001@c.us"), {})
        self.assertEqual(queue_contextual_tickets.request_fragments(service.store, "77010000001@c.us"), [])
        self.send(service, "Не открывается почта")
        created = self.send(service, "Готово")
        ticket = service.store.get_ticket(created["ticket_id"])
        self.assertEqual(ticket["category"], "custom_email")
        self.assertNotIn("принтер", ticket["original_text"])
        self.assertIn("Не открывается почта", ticket["original_text"])

    def test_selected_ticket_consumes_followup_and_closed_ticket_returns_to_list(self):
        from queue_bot_texts import ACTIVE_TICKET_FOLLOWUP_CONTEXT
        service = self.services()
        self.register(service)
        ticket_id = service.store.create_ticket({"chat_id": "77010000001@c.us"})
        service.store.set_conversation_context("77010000001@c.us", ACTIVE_TICKET_FOLLOWUP_CONTEXT, ticket_id)
        response = self.send(service, "Спасибо")
        self.assertTrue(response.get("linked"))
        self.assertEqual(response["ticket_id"], ticket_id)
        self.assertIn("Спасибо", service.store.get_ticket(ticket_id)["original_text"])
        closed = service.store.create_ticket({"chat_id": "77010000001@c.us", "status": "done"})
        service.store.set_conversation_context("77010000001@c.us", ACTIVE_TICKET_FOLLOWUP_CONTEXT, closed)
        self.assertTrue(self.send(service, "Дополнение").get("active_tickets"))
        self.assertNotIn("Дополнение", service.store.get_ticket(closed)["original_text"])

    def test_context_storage_failure_allows_redelivery_without_losing_details(self):
        from queue_context_repository import ContextRepository
        service = self.services()
        self.register(service)
        self.send(service, "1")
        payload = {"chat_id": "77010000001@c.us", "external_id": "retry-details",
                   "text": "Не печатает принтер"}
        with patch.object(ContextRepository, "record_analysis", side_effect=RuntimeError("disk failure")):
            self.assertEqual(service.inbound.handle(payload).status, 503)
        self.assertEqual(service.store.get_conversation_draft("77010000001@c.us"), {})
        result = service.inbound.handle(payload)
        self.assertTrue(result.payload.get("draft_ready"))
        self.assertTrue(service.inbound.handle(payload).payload.get("duplicate"))
        created = self.send(service, "Готово")
        self.assertIn("Не печатает принтер", service.store.get_ticket(created["ticket_id"])["original_text"])
