"""Static pages: the server sends the same files to everyone and the data as JSON."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests import support
from tests.support import admin_cookie, get
from tests.test_csrf import second_user_cookie

import queue_ticket_views

PROJECT = Path(__file__).resolve().parents[1]
PAGES = PROJECT / "frontend" / "pages"


class StaticPageTests(unittest.TestCase):
    def test_pages_are_the_files_themselves(self):
        for path, name in (("/whatsapp", "whatsapp"), ("/groups", "groups")):
            with self.subTest(path=path):
                response = get(f"{path}?chat_id=<b>&notice=<i>", admin_cookie())
                self.assertEqual(response.status, 200)
                self.assertEqual(response.body, (PAGES / f"{name}.html").read_bytes())

    def test_admin_groups_is_the_same_page(self):
        self.assertEqual(get("/admin/groups", admin_cookie()).body, (PAGES / "groups.html").read_bytes())

    def test_signing_in_is_still_required(self):
        for path in ("/whatsapp", "/groups"):
            with self.subTest(path=path):
                response = get(path)
                self.assertIn(response.status, (302, 303))
                self.assertIn("/login", response.location)

    def test_employee_cannot_open_the_admin_address(self):
        response = get("/admin/groups", second_user_cookie())
        self.assertIn(response.status, (302, 303, 403))

    def test_pages_hold_no_placeholders_or_user_data(self):
        for page in PAGES.glob("*.html"):
            with self.subTest(page=page.name):
                text = page.read_text(encoding="utf-8")
                self.assertNotIn("{{", text)
                self.assertIn('<meta name="queue-csrf" content="">', text)

    def test_page_assets_are_served_in_the_layout_order(self):
        for page in PAGES.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            styles = tuple(re.findall(r'href="/static/([\w.-]+\.css)"', text))
            self.assertEqual(styles, queue_ticket_views.MAIN_STYLESHEETS, page.name)
            for name in (*styles, *re.findall(r'src="/static/([\w.-]+\.js)"', text)):
                self.assertEqual(get(f"/static/{name}").status, 200, name)

    def test_pages_run_the_shell_first_and_then_the_layout_scripts_in_order(self):
        # app.js fetches chat data at once, so the later scripts must not load after it asynchronously.
        for page, first in (("whatsapp", ("shell.js",)), ("groups", ("shell.js", "groups-page.js"))):
            with self.subTest(page=page):
                text = (PAGES / f"{page}.html").read_text(encoding="utf-8")
                scripts = tuple(re.findall(r'<script src="/static/([\w.-]+\.js)" defer></script>', text))
                self.assertEqual(scripts, (*first, *queue_ticket_views.MAIN_SCRIPTS))


class PageContextTests(unittest.TestCase):
    def test_admin_context(self):
        cookie = admin_cookie()
        context = get("/api/page-context", cookie).json()
        self.assertEqual(context["csrf_token"], support.csrf_token(cookie))
        self.assertTrue(context["show_admin"])
        self.assertIn("/admin/", [item["href"] for item in context["nav"]])
        self.assertEqual(context["user"]["role_label"], "Администратор")

    def test_employee_context_has_no_admin_link(self):
        context = get("/api/page-context", second_user_cookie()).json()
        self.assertFalse(context["show_admin"])
        self.assertNotIn("/admin/", [item["href"] for item in context["nav"]])
        self.assertEqual(context["user"]["role_label"], "Сотрудник")

    def test_context_requires_sign_in(self):
        response = get("/api/page-context")
        self.assertIn(response.status, (302, 303, 401))


class GroupsPageDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.app.STORE.save_whatsapp_group("<b>Склад</b> & офис", "120363000000000099@g.us")

    def group(self, data):
        return next(item for item in data["groups"] if item["chat_id"] == "120363000000000099@g.us")

    def test_names_stay_raw_for_the_page_to_escape(self):
        data = get("/api/groups-page", admin_cookie()).json()
        self.assertEqual(self.group(data)["name"], "<b>Склад</b> & офис")
        self.assertEqual(self.group(data)["initials"], "<&")

    def test_admin_sees_the_admin_panel_data(self):
        data = get("/api/groups-page", admin_cookie()).json()
        self.assertIsInstance(data["admin"]["discovered"], list)

    def test_employee_gets_no_admin_data(self):
        data = get("/api/groups-page", second_user_cookie()).json()
        self.assertIsNone(data["admin"])
        self.group(data)

    def test_groups_page_data_requires_sign_in(self):
        self.assertIn(get("/api/groups-page").status, (302, 303, 401))
