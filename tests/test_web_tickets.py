"""Web interface: authentication, ticket list, manual tickets and status changes."""
from __future__ import annotations

import re
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import queue_productivity

from tests import support
from tests.support import WhatsAppUser, admin_cookie, get, post_form


def ticket_id_from(location: str) -> int:
    return int(parse_qs(urlparse(location).query)["id"][0])


class AuthenticationTests(unittest.TestCase):
    def test_health(self):
        response = get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_anonymous_user_is_sent_to_login(self):
        response = get("/")
        self.assertIn(response.status, (302, 303))
        self.assertTrue(response.location.startswith("/login"))

    def test_wrong_password_is_rejected(self):
        admin_cookie()
        response = post_form("/login", {"username": support.ADMIN_USERNAME, "password": "wrong-password"})
        self.assertEqual(response.status, 401)
        self.assertEqual(response.cookies(), "")

    def test_setup_is_closed_once_a_user_exists(self):
        admin_cookie()
        response = post_form("/setup", {"username": "intruder", "display_name": "x",
                                        "password": "Another-Pass-2026!", "password2": "Another-Pass-2026!"})
        self.assertEqual(response.status, 404)

    def test_integration_api_key(self):
        self.assertEqual(support.request("GET", "/api/integration/ping").status, 401)
        ok, _message, token = support.app.AUTH.create_api_key({"username": "admin"}, "Тестовый ключ", "read")
        self.assertTrue(ok)
        response = support.request("GET", "/api/integration/ping", headers={"X-API-Key": token})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["version"], support.app.APP_VERSION)
        response = support.request("GET", "/api/integration/tickets?limit=5",
                                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status, 200)

    def test_foreign_host_header_is_rejected(self):
        response = support.request("GET", "/health", headers={"Host": "evil.example.com"})
        self.assertEqual(response.status, 421)


class BackupsPageTests(unittest.TestCase):
    def test_unwritable_backup_dir_does_not_break_the_page(self):
        with mock.patch.object(queue_productivity, "backup_dir", side_effect=PermissionError("no access")):
            with self.assertLogs("queue", "WARNING"):
                response = get("/admin/backups", admin_cookie())
        self.assertEqual(response.status, 200)


class StaticFilesTests(unittest.TestCase):
    def test_every_layout_asset_is_served(self):
        page = get("/", admin_cookie()).text
        assets = re.findall(r'/static/([\w.-]+)\?v=', page)
        self.assertGreaterEqual(len(assets), 20)
        for name in assets:
            with self.subTest(name=name):
                response = get(f"/static/{name}")
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.body), 0)

    def test_login_page_assets_are_served(self):
        page = get("/login").text
        for name in re.findall(r'/static/([\w.-]+)\?v=', page):
            with self.subTest(name=name):
                self.assertEqual(get(f"/static/{name}").status, 200)

    def test_unlisted_or_traversal_paths_are_not_served(self):
        for path in ("/static/nope.js", "/static/../app.py", "/static/%2e%2e/app.py", "/static/hotfix10068.js"):
            with self.subTest(path=path):
                self.assertEqual(get(path).status, 404)


class TicketWebTests(unittest.TestCase):
    def test_bot_ticket_is_visible_in_web_interface(self):
        user = WhatsAppUser()
        user.register_profile()
        user.say("1")
        user.say("Сломалась клавиатура в приёмной")
        ticket_id = user.say("Готово")["ticket_id"]

        cookie = admin_cookie()
        page = get(f"/ticket?id={ticket_id}", cookie)
        self.assertEqual(page.status, 200)
        self.assertIn("Сломалась клавиатура в приёмной", page.text)

        listing = get("/", cookie)
        self.assertEqual(listing.status, 200)
        self.assertIn("Сломалась клавиатура в приёмной", listing.text)

    def test_manual_ticket_creation(self):
        cookie = admin_cookie()
        response = post_form("/manual", {"title": "Заменить картридж", "reference": "PR-17",
                                         "comment": "Кабинет 204", "priority": "high"}, cookie)
        self.assertIn(response.status, (302, 303))
        ticket = support.app.STORE.get_ticket(ticket_id_from(response.location))
        self.assertEqual(ticket["source"], "manual")
        self.assertEqual(ticket["priority"], "high")
        self.assertEqual(ticket["status"], "new")
        self.assertIn("№ PR-17", ticket["summary"])

        duplicate = post_form("/manual", {"title": "Заменить картридж", "reference": "PR-17",
                                          "comment": "Кабинет 204", "priority": "high"}, cookie)
        self.assertEqual(ticket_id_from(duplicate.location), ticket["id"], "similar open ticket must be reused")

    def test_status_change(self):
        cookie = admin_cookie()
        created = post_form("/manual", {"title": "Проверка статуса", "comment": "Тест смены статуса"}, cookie)
        ticket_id = ticket_id_from(created.location)

        response = post_form("/status", {"ticket_id": ticket_id, "status": "in_progress"}, cookie)
        self.assertIn(response.status, (302, 303))
        self.assertEqual(support.app.STORE.get_ticket(ticket_id)["status"], "in_progress")

    def test_unknown_post_path_returns_404(self):
        response = post_form("/definitely-not-a-route", {"x": "1"}, admin_cookie())
        self.assertEqual(response.status, 404)

    def test_anonymous_post_is_not_allowed(self):
        response = post_form("/manual", {"title": "Аноним"})
        self.assertIn(response.status, (302, 303, 401, 403))
        self.assertFalse(re.search(r"/ticket\?id=\d+", response.location or ""))


if __name__ == "__main__":
    unittest.main()
