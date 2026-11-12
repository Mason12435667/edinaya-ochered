"""Заголовок X-Remote-User: без QUEUE_TRUST_PROXY_USER=1 сервер ему не верит.

Все запросы через nginx на том же сервере приходят с 127.0.0.1, поэтому адрес не доказывает, что
заголовок поставил nginx, а не посетитель.
"""
import dataclasses
import unittest
from unittest import mock

from tests import support
from tests.support import admin_cookie, request

app = support.app
AS_ADMIN = {"X-Remote-User": app.ADMIN_USER}


class RemoteUserHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        admin_cookie()  # an account exists, so pages ask for a login instead of /setup

    def test_the_header_alone_does_not_open_admin_pages(self):
        response = request("GET", "/admin/users", headers=AS_ADMIN)
        self.assertEqual(response.status, 303)
        self.assertTrue(response.location.startswith("/login"), response.location)

    def test_the_header_does_not_give_an_admin_cookie(self):
        response = request("GET", "/admin/login", headers=AS_ADMIN)
        self.assertEqual(response.status, 403)
        self.assertNotIn(app.ADMIN_SESSION_COOKIE, response.cookies())

    def test_a_trusted_proxy_still_signs_the_administrator_in(self):
        trusted = dataclasses.replace(app.TicketHandler.deps, trust_proxy_user=True)
        with mock.patch.object(app.TicketHandler, "deps", trusted):
            self.assertEqual(request("GET", "/admin/users", headers=AS_ADMIN).status, 200)
            self.assertEqual(request("GET", "/admin/users").status, 303)


if __name__ == "__main__":
    unittest.main()
