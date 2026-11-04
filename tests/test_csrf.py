"""CSRF protection: every signed-in POST needs the user's token."""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import unittest

from tests import support
from tests.support import WhatsAppUser, admin_cookie, csrf_token, get, post_form

PAGES = [
    "/", "/me", "/whatsapp", "/groups", "/ticket?id=1",
    "/admin/", "/admin/users", "/admin/employees", "/admin/manual", "/admin/menu", "/admin/categories",
    "/admin/errors", "/admin/contacts", "/admin/audit", "/admin/analytics", "/admin/manager",
    "/admin/api-keys", "/admin/system", "/admin/groups", "/admin/reliability", "/admin/workflow",
    "/admin/backups", "/admin/shift",
]


def tickets_titled(title: str) -> int:
    with support.app.STORE.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM tickets WHERE title = ?", (title,)).fetchone()[0]


def second_user_cookie() -> str:
    username, password = "csrf_employee", "Employee-Pass-2026!"
    if not support.app.AUTH.authenticate(username, password).get("ok"):
        ok, message, _ = support.app.AUTH.create_user(username, "Сотрудник CSRF", password, role="employee")
        assert ok, message
    response = post_form("/login", {"username": username, "password": password, "next": "/"})
    assert response.cookies(), response.status
    return response.cookies()


class CsrfTests(unittest.TestCase):
    def test_form_post_without_token_is_rejected(self):
        response = post_form("/manual", {"title": "CSRF без токена"}, admin_cookie(), csrf=False)
        self.assertIn(response.status, (302, 303))
        self.assertTrue(response.location.startswith("/?notice="))
        self.assertEqual(tickets_titled("CSRF без токена"), 0)

    def test_form_post_with_wrong_token_is_rejected(self):
        response = post_form("/manual", {"title": "CSRF чужой токен", "csrf_token": "x" * 64}, admin_cookie())
        self.assertTrue(response.location.startswith("/?notice="))
        self.assertEqual(tickets_titled("CSRF чужой токен"), 0)

    def test_form_post_with_token_works(self):
        response = post_form("/manual", {"title": "CSRF с токеном"}, admin_cookie())
        self.assertTrue(response.location.startswith("/ticket?id="))
        self.assertEqual(tickets_titled("CSRF с токеном"), 1)

    def test_fetch_post_needs_header(self):
        cookie = admin_cookie()
        body = b"theme=dark"
        headers = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "fetch"}
        denied = support.request("POST", "/api/account-preferences", body, headers)
        self.assertEqual(denied.status, 403)
        self.assertFalse(denied.json()["csrf"])
        allowed = support.request("POST", "/api/account-preferences", body, {**headers, "X-CSRF-Token": csrf_token(cookie)})
        self.assertEqual(allowed.status, 200)

    def test_json_post_needs_header(self):
        cookie = admin_cookie()
        body = json.dumps({"action": "check"}).encode()
        headers = {"Cookie": cookie, "Content-Type": "application/json"}
        self.assertEqual(support.request("POST", "/api/system-warning-action", body, headers).status, 403)
        ok = support.request("POST", "/api/system-warning-action", body, {**headers, "X-CSRF-Token": csrf_token(cookie)})
        self.assertEqual(ok.status, 200)

    def test_form_encoded_body_on_json_route_does_not_hang(self):
        # The CSRF check reads urlencoded bodies; the JSON route must reuse them, not wait for more bytes.
        cookie = admin_cookie()
        body = f"csrf_token={csrf_token(cookie)}".encode()
        connection = http.client.HTTPConnection("127.0.0.1", support.server_port(), timeout=5)
        try:
            connection.request("POST", "/api/system-warning-action", body, {
                "Host": "127.0.0.1", "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body))})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())["error"], "invalid_json")
        finally:
            connection.close()

    def test_token_belongs_to_one_user(self):
        admin, employee = admin_cookie(), second_user_cookie()
        self.assertNotEqual(csrf_token(admin), csrf_token(employee))
        response = post_form("/manual", {"title": "CSRF токен админа", "csrf_token": csrf_token(admin)}, employee)
        self.assertTrue(response.location.startswith("/?notice="))
        self.assertEqual(tickets_titled("CSRF токен админа"), 0)

    def test_token_survives_restart(self):
        # A second process on the same database is a real restart: it must issue the same token.
        user = {"id": 1, "username": support.ADMIN_USERNAME}
        script = "import json, sys, app; print(app.csrf_token(json.loads(sys.argv[1])))"
        result = subprocess.run(
            [sys.executable, "-c", script, json.dumps(user)],
            cwd=Path(support.app.__file__).parent, env=dict(os.environ), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip().splitlines()[-1], support.app.csrf_token(user))

    def test_connector_endpoints_use_webhook_token_instead(self):
        reply = WhatsAppUser().say("Здравствуйте")
        self.assertTrue(reply.get("language_required"))

    def test_every_post_form_carries_the_token(self):
        cookie = admin_cookie()
        token = csrf_token(cookie)
        missing = []
        for page in PAGES:
            response = get(page, cookie)
            if response.status != 200:
                missing.append(f"{page}: HTTP {response.status} (page not checked)")
                continue
            for form in re.findall(r"<form\b[^>]*>.*?</form>", response.text, re.S | re.I):
                opening = form[:form.index(">") + 1]
                if not re.search(r'method=["\']?post', opening, re.I):
                    continue
                if not re.search(rf"""name=["']csrf_token["'] value=["']{token}["']""", form):
                    action = re.search(r'action=["\']([^"\']*)', opening)
                    missing.append(f"{page}: form action={action.group(1) if action else '?'}")
        self.assertEqual(sorted(set(missing)), [])


if __name__ == "__main__":
    unittest.main()
