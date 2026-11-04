"""WhatsApp bot dialog: language, profile, menu, ticket draft and creation."""
from __future__ import annotations

import unittest

from tests.support import WhatsAppUser


class BotDialogTests(unittest.TestCase):
    def test_first_message_asks_for_language(self):
        reply = WhatsAppUser().say("Здравствуйте")
        self.assertTrue(reply.get("language_required"))
        self.assertIn("Русский", reply["reply"])
        self.assertIn("Қазақша", reply["reply"])
        self.assertFalse(reply["created"])

    def test_language_choice_asks_for_profile(self):
        user = WhatsAppUser()
        user.say("Здравствуйте")
        reply = user.say("1")
        self.assertEqual(reply.get("language_selected"), "ru")
        self.assertTrue(reply.get("profile_required"))
        self.assertIn("Подразделение", reply["reply"])

    def test_incomplete_profile_is_rejected(self):
        user = WhatsAppUser()
        user.say("Здравствуйте")
        user.say("1")
        reply = user.say("Бухгалтерия")
        self.assertTrue(reply.get("profile_required"))
        self.assertIn("Укажите оба поля", reply["reply"])

    def test_profile_saved_shows_configured_menu(self):
        reply = WhatsAppUser().register_profile("Бухгалтерия", "Главный бухгалтер")
        self.assertTrue(reply.get("profile_saved"))
        self.assertEqual(reply.get("post"), "Бухгалтерия")
        self.assertEqual(reply.get("position"), "Главный бухгалтер")
        numbers = [item["number"] for item in reply["menu_options"]]
        self.assertEqual(numbers, ["1", "2", "8"])
        self.assertIn("1. Общее обращение", reply["reply"])
        self.assertIn("2. Принтеры", reply["reply"])

    def test_full_dialog_creates_one_ticket(self):
        user = WhatsAppUser()
        user.register_profile("Бухгалтерия", "Главный бухгалтер")
        category = user.say("1")
        self.assertEqual(category.get("category"), "general")
        self.assertTrue(category.get("awaiting_details"))

        details = user.say("Не печатает принтер в кабинете 12")
        self.assertFalse(details["created"])
        self.assertTrue(details.get("draft_ready"))
        self.assertEqual(user.tickets(), [], "ticket must not be created before «Готово»")

        done = user.send("Готово")
        self.assertEqual(done.status, 201)
        created = done.json()
        self.assertTrue(created["created"])
        self.assertIn(f"№{created['ticket_id']}", created["reply"])

        tickets = user.tickets()
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket["id"], created["ticket_id"])
        self.assertEqual(ticket["category"], "general")
        self.assertEqual(ticket["status"], "new")
        self.assertEqual(ticket["source"], "whatsapp")
        self.assertIn("Не печатает принтер в кабинете 12", ticket["summary"])
        self.assertIn("Подразделение: Бухгалтерия", ticket["original_text"])
        self.assertIn("Должность: Главный бухгалтер", ticket["original_text"])

    def test_category_with_reference_field_asks_for_it(self):
        user = WhatsAppUser()
        user.register_profile()
        reply = user.say("2")
        self.assertEqual(reply.get("category"), "custom_printers")
        self.assertIn("номер принтера", reply["reply"])
        reply = user.say("Не печатает")
        self.assertFalse(reply["created"])
        self.assertIn("reference", reply.get("missing_fields", []))

    def test_duplicate_message_id_is_processed_once(self):
        user = WhatsAppUser()
        user.register_profile()
        user.say("1")
        user.say("Не работает почта")
        first = user.send("Готово", external_id="duplicate-check-1")
        second = user.send("Готово", external_id="duplicate-check-1")
        self.assertEqual(first.status, 201)
        self.assertTrue(second.json().get("duplicate"))
        self.assertEqual(len(user.tickets()), 1)

    def test_redelivered_menu_digit_does_not_reset_the_draft(self):
        user = WhatsAppUser()
        user.register_profile()
        self.assertEqual(user.send("1", external_id="redeliver-menu-1").json().get("category"), "general")
        user.say("Не работает сканер в бухгалтерии")
        # the connector delivers the old "1" again after the user moved on
        again = user.send("1", external_id="redeliver-menu-1").json()
        self.assertTrue(again.get("duplicate"))
        done = user.send("Готово")
        self.assertEqual(done.status, 201, done.text[:300])
        self.assertIn("Не работает сканер в бухгалтерии", user.tickets()[0]["summary"])

    def test_redelivered_zero_does_not_reopen_the_menu(self):
        user = WhatsAppUser()
        user.register_profile()
        user.send("0", external_id="redeliver-zero-1")
        user.say("1")
        user.say("Нужен доступ к папке")
        self.assertTrue(user.send("0", external_id="redeliver-zero-1").json().get("duplicate"))
        self.assertEqual(user.send("Готово").status, 201)

    def test_zero_reopens_menu_after_ticket(self):
        user = WhatsAppUser()
        user.register_profile()
        user.say("1")
        user.say("Нужен доступ к папке")
        user.say("Готово")
        reply = user.say("0")
        self.assertTrue(reply.get("main_menu"))
        self.assertIn("Выберите категорию обращения", reply["reply"])

    def test_eight_lists_active_tickets_right_after_creation(self):
        user = WhatsAppUser()
        user.register_profile()
        user.say("1")
        user.say("Не работает сканер")
        ticket_id = user.say("Готово")["ticket_id"]
        reply = user.say("8")
        self.assertTrue(reply.get("active_tickets"))
        self.assertIn(f"№{ticket_id}", reply["reply"])
        tickets = user.tickets()
        self.assertEqual(len(tickets), 1)
        self.assertNotIn("\n8", tickets[0]["original_text"], "«8» must not be stored as a clarification")

    def test_eight_from_menu_lists_active_tickets(self):
        user = WhatsAppUser()
        user.register_profile()
        reply = user.say("8")
        self.assertTrue(reply.get("active_tickets"))

    def test_eight_inside_a_draft_is_kept_as_data(self):
        user = WhatsAppUser()
        user.register_profile()
        user.say("2")
        reply = user.say("8")
        self.assertFalse(reply.get("active_tickets"))
        self.assertEqual(reply.get("category"), "custom_printers")

    def test_double_zero_switches_language_to_kazakh(self):
        user = WhatsAppUser()
        user.register_profile()
        self.assertTrue(user.say("00").get("language_required"))
        reply = user.say("2")
        self.assertEqual(reply.get("language_selected"), "kz")
        self.assertIn("Өтінім санатын таңдаңыз", reply["reply"])
        self.assertIn("2. Принтерлер", reply["reply"])

    def test_wrong_webhook_token_is_rejected(self):
        user = WhatsAppUser()
        response = user.send("Здравствуйте", token="wrong-token")
        self.assertEqual(response.status, 401)
        response = user.send("Здравствуйте", token="")
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
