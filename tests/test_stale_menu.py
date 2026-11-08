"""StaleMenuRecovery без HTTP: каким чатам без ответа возвращается меню, а какие не затрагиваются."""
import tempfile
import time
import unittest
from pathlib import Path

import queue_contextual_tickets
import queue_productivity
import queue_user_locale
from queue_bot_menu import BotMenu
from queue_bot_texts import language_selection_text_v3
from queue_contact_profiles import ContactProfiles
from queue_core import normalize_message
from queue_menu_config import MenuConfig
from queue_stale_menu import StaleMenuRecovery
from queue_store_conversations import MENU_CONTEXT
from ticketing import TicketStore

CHAT = "77010000005@c.us"
PHONE = "+77010000005"


class Events:
    def __init__(self):
        self.notifications = []

    def notify_outbound(self):
        self.notifications.append("outbound")
        return len(self.notifications)


class StaleMenuRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        language = queue_user_locale.current_language()
        self.addCleanup(queue_user_locale.set_language, language)
        self.store = TicketStore(Path(temp.name) / "tickets.db")
        for module in (queue_productivity, queue_contextual_tickets):
            module.initialize(self.store)
        self.menu = BotMenu(MenuConfig(self.store, lambda: {
            "greeting_ru": "Тестовое меню", "greeting_kz": "Тестовое меню",
            "categories": [{"number": 1, "key": "general", "label_ru": "Общее", "label_kz": "Жалпы",
                            "prompt_ru": "Опишите проблему", "prompt_kz": "Мәселені сипаттаңыз"}]}, {}))
        self.contacts = ContactProfiles(self.store)
        self.events = Events()
        self.auto_reply = True
        self.recovery = StaleMenuRecovery(self.store, self.menu, self.contacts, self.events, lambda: self.auto_reply)
        self.now = int(time.time())

    def inbound(self, minutes_ago: int, key: str = "m1") -> None:
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO whatsapp_chat_messages (message_key, chat_id, from_me, body, message_type,"
                " message_timestamp, sender_phone, created_at) VALUES (?, ?, 0, 'Здравствуйте', 'chat', ?, ?, '')",
                (key, CHAT, self.now - minutes_ago * 60, PHONE))

    def queued(self) -> list[str]:
        with self.store.connection() as connection:
            return [row["body"] for row in connection.execute(
                "SELECT body FROM outbound_messages WHERE chat_id = ? ORDER BY id", (CHAT,))]

    def register(self) -> None:
        self.contacts.set_contact_language(CHAT, PHONE, "ru")
        self.contacts.set_contact_profile(CHAT, PHONE, "Бухгалтерия", "Бухгалтер")

    def test_registered_contact_gets_the_main_menu_once(self):
        self.register()
        self.inbound(minutes_ago=40)
        self.assertEqual(self.recovery.recover_once(self.now), 1)
        self.assertEqual(len(self.queued()), 1)
        self.assertIn("Тестовое меню", self.queued()[0])
        self.assertEqual(self.store.get_conversation_context(CHAT)["pending_category"], MENU_CONTEXT)
        self.assertEqual(self.events.notifications, ["outbound"])

        self.assertEqual(self.recovery.recover_once(self.now), 0, "the reply is already queued")
        self.assertEqual(len(self.queued()), 1)

    def test_new_contact_is_asked_for_a_language_first(self):
        self.inbound(minutes_ago=40)
        self.assertEqual(self.recovery.recover_once(self.now), 1)
        self.assertEqual(self.queued(), [normalize_message(language_selection_text_v3())])
        self.assertIsNone(self.store.get_conversation_context(CHAT))

    def test_nothing_is_sent_when_auto_replies_are_off(self):
        self.register()
        self.inbound(minutes_ago=40)
        self.auto_reply = False
        self.assertEqual(self.recovery.recover_once(self.now), 0)
        self.assertEqual(self.queued(), [])

    def test_recent_message_is_left_for_the_normal_flow(self):
        self.register()
        self.inbound(minutes_ago=5)
        self.assertEqual(self.recovery.recover_once(self.now), 0)

    def test_contact_in_the_middle_of_a_ticket_is_not_interrupted(self):
        self.register()
        self.store.set_conversation_context(CHAT, "general", 0, False)
        self.inbound(minutes_ago=40)
        self.assertEqual(self.recovery.recover_once(self.now), 0)
        self.assertEqual(self.queued(), [])


if __name__ == "__main__":
    unittest.main()
