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
        for path, name in (("/whatsapp", "whatsapp"), ("/groups", "groups"), ("/", "tickets"), ("/ticket", "ticket")):
            with self.subTest(path=path):
                response = get(f"{path}?chat_id=<b>&notice=<i>", admin_cookie())
                self.assertEqual(response.status, 200)
                self.assertEqual(response.body, (PAGES / f"{name}.html").read_bytes())

    def test_admin_groups_is_the_same_page(self):
        self.assertEqual(get("/admin/groups", admin_cookie()).body, (PAGES / "groups.html").read_bytes())

    def test_signing_in_is_still_required(self):
        for path in ("/whatsapp", "/groups", "/", "/ticket?id=1"):
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
        for page, first in (("whatsapp", ("shell.js",)), ("groups", ("shell.js", "groups-page.js")),
                            ("tickets", ("shell.js", "ticket-render.js", "dashboard-page.js")),
                            ("ticket", ("shell.js", "ticket-render.js", "ticket-page.js"))):
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


def make_ticket(title: str, status: str = "new", cookie: str = "") -> int:
    response = support.post_form("/manual", {"title": title, "status": status, "priority": "normal"}, cookie or admin_cookie())
    return int(re.search(r"id=(\d+)", response.location).group(1))


class DashboardDataTests(unittest.TestCase):
    def test_rows_are_data_and_titles_stay_raw(self):
        make_ticket("<b>Сломан</b> & принтер")
        data = get("/api/dashboard-data", admin_cookie()).json()
        self.assertNotIn("rows_html", data)
        self.assertNotIn("pagination_html", data)
        row = next(item for item in data["rows"] if item["title"] == "<b>Сломан</b> & принтер")
        self.assertEqual(row["status_label"], "Не тронута")
        self.assertIsInstance(row["revision"], int)
        self.assertIn(["normal", "Обычный"], data["options"]["priorities"])

    def test_filter_values_are_returned_for_the_form(self):
        filters = get("/api/dashboard-data?status=done&view=bad&no_answer_minutes=x", admin_cookie()).json()["filters"]
        self.assertEqual(filters["status"], "done")
        self.assertEqual(filters["view"], "")
        self.assertEqual(filters["no_answer_minutes"], 0)

    def test_pagination_links_keep_filters_and_drop_the_page(self):
        data = support.app.queue_work_context.pagination_data({"status": ["new"], "page": ["2"]}, 100, 2, 20)
        self.assertEqual(data["prev_url"], "/?status=new")
        self.assertEqual(data["next_url"], "/?status=new&page=3")
        self.assertTrue(any(item.get("current") and item["number"] == 2 for item in data["items"]))

    def test_data_requires_sign_in(self):
        self.assertIn(get("/api/dashboard-data").status, (302, 303, 401))


class TicketPageDataTests(unittest.TestCase):
    def test_missing_ticket(self):
        self.assertEqual(get("/api/ticket-page?id=999999", admin_cookie()).json(), {"found": False})

    def test_open_ticket(self):
        ticket_id = make_ticket("Карточка заявки")
        data = get(f"/api/ticket-page?id={ticket_id}", admin_cookie()).json()
        self.assertTrue(data["found"])
        self.assertEqual(data["title"], "Карточка заявки")
        self.assertFalse(data["status_locked"])
        self.assertEqual(data["handoff"]["pending"], False)
        self.assertTrue(data["events"])
        self.assertIn("label", data["notification"])

    def test_closed_ticket_is_locked_for_employees_only(self):
        ticket_id = make_ticket("Закрытая заявка", "done")
        self.assertTrue(get(f"/api/ticket-page?id={ticket_id}", second_user_cookie()).json()["status_locked"])
        data = get(f"/api/ticket-page?id={ticket_id}", admin_cookie()).json()
        self.assertFalse(data["status_locked"])
        self.assertIsNone(data["handoff"])

    def test_data_requires_sign_in(self):
        self.assertIn(get("/api/ticket-page?id=1").status, (302, 303, 401))


ADMIN_PAGES = ("home", "users", "employees", "manual", "menu", "categories", "errors", "error", "contacts", "audit", "manager", "system")


class AdminPagesTests(unittest.TestCase):
    def test_admin_addresses_are_the_static_page(self):
        body = (PAGES / "admin.html").read_bytes()
        for path in ("/admin/", "/admin", "/admin/users", "/admin/system", "/admin/error?id=1"):
            with self.subTest(path=path):
                self.assertEqual(get(path, admin_cookie()).body, body)

    def test_employee_is_kept_out(self):
        cookie = second_user_cookie()
        for path in ("/admin/users", "/api/admin-page?page=users", "/api/admin-page?page=system"):
            with self.subTest(path=path):
                response = get(path, cookie)
                self.assertNotEqual(response.status, 200)
                self.assertNotIn("session_hash", response.text)

    def test_anonymous_is_kept_out(self):
        self.assertNotEqual(get("/api/admin-page?page=users").status, 200)

    def test_every_page_has_data_and_tabs(self):
        for page in ADMIN_PAGES:
            with self.subTest(page=page):
                data = get(f"/api/admin-page?page={page}", admin_cookie()).json()
                self.assertTrue(data["title"])
                self.assertIn("users", [tab["key"] for tab in data["tabs"]])

    def test_unknown_page(self):
        self.assertEqual(get("/api/admin-page?page=nope", admin_cookie()).status, 404)

    def test_data_holds_no_markup(self):
        for page in ADMIN_PAGES:
            with self.subTest(page=page):
                self.assertNotRegex(get(f"/api/admin-page?page={page}", admin_cookie()).text, r"<(tr|td|form|option|section|div)[ >]")

    def test_templates_are_served_but_not_outside_the_folder(self):
        self.assertEqual(get("/static/admin/home.html").status, 200)
        self.assertEqual(get("/static/admin/../app.js").status, 404)
        self.assertEqual(get("/static/admin/nope.html").status, 404)

    def test_missing_error_report(self):
        self.assertFalse(get("/api/admin-page?page=error&id=999999", admin_cookie()).json()["found"])

