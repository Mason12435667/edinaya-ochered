import tempfile
import unittest
from pathlib import Path

from ticketing import (
    TicketStore,
    is_main_menu_command,
    menu_category,
    process_incoming_message,
)


class PublicSmokeTests(unittest.TestCase):
    def test_menu_routing(self):
        self.assertEqual("seal", menu_category("1"))
        self.assertEqual("keden", menu_category("4"))
        self.assertEqual("support", menu_category("9"))
        self.assertTrue(is_main_menu_command("0"))
        self.assertTrue(is_main_menu_command("назад"))

    def test_structured_request_is_classified(self):
        result = process_incoming_message(
            "Тестовый пользователь",
            "+77000000000",
            "Кеден не видит нашей перевозки 77125",
        )
        self.assertIsNotNone(result.get("ticket"))
        self.assertEqual("keden", result["ticket"]["category"])

    def test_ticket_store_persists_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            store = TicketStore(database)
            ticket_id = store.create_ticket(
                {
                    "source": "whatsapp",
                    "sender": "Тестовый пользователь",
                    "phone": "+77000000000",
                    "chat_id": "77000000000@c.us",
                    "category": "general",
                    "title": "Тестовая заявка",
                    "summary": "Демонстрационные данные",
                    "assigned_to": "Сотрудник 1",
                }
            )
            self.assertGreater(ticket_id, 0)
            reopened = TicketStore(database)
            ticket = reopened.get_ticket(ticket_id)
            self.assertIsNotNone(ticket)
            self.assertEqual("Тестовая заявка", ticket["title"])

    def test_discovered_group_can_be_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TicketStore(Path(directory) / "test.db")
            group_id = "120363000000000@g.us"
            store.replace_whatsapp_groups(
                [{"id": group_id, "name": "Демо-группа", "participant_count": 3}]
            )
            self.assertEqual(1, len(store.list_discovered_whatsapp_groups()))
            self.assertEqual([], store.list_whatsapp_groups())
            self.assertIsNotNone(store.save_whatsapp_group("Демо-группа", group_id))
            enabled = store.list_whatsapp_groups()
            self.assertEqual(1, len(enabled))
            self.assertEqual("Демо-группа", enabled[0]["name"])


if __name__ == "__main__":
    unittest.main()
