"""Shared test server: one isolated app instance per test run.

Environment is configured before ``app`` is imported because the application
reads its configuration at import time. Every test uses its own phone number,
so tests do not depend on each other's state in the shared database.
"""
from __future__ import annotations

import atexit
import http.client
import itertools
import json
import os
import re
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

_DATA_DIR = Path(tempfile.mkdtemp(prefix="eo-tests-"))
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)

_COMPANY_CONFIG = _DATA_DIR / "company.json"
_COMPANY_CONFIG.write_text(json.dumps({
    "organization": "Тестовая организация",
    "greeting_ru": "Добро пожаловать в службу поддержки",
    "greeting_kz": "Қолдау қызметіне қош келдіңіз",
    "categories": [
        {"number": 1, "key": "general", "label_ru": "Общее обращение", "label_kz": "Жалпы өтініш",
         "prompt_ru": "Опишите проблему", "prompt_kz": "Мәселені сипаттаңыз", "required_fields": ["problem"]},
        {"number": 2, "key": "custom_printers", "label_ru": "Принтеры", "label_kz": "Принтерлер",
         "prompt_ru": "Укажите номер принтера и опишите проблему", "prompt_kz": "Принтер нөмірін көрсетіңіз",
         "required_fields": ["reference", "problem"]},
    ],
}, ensure_ascii=False), encoding="utf-8")

os.environ.update({
    "WEBHOOK_TOKEN": "test-webhook-token-" + "x" * 32,
    "QUEUE_LOCAL_MODE": "1",
    "QUEUE_DATA_DIR": str(_DATA_DIR / "data"),
    "QUEUE_WHATSAPP_AUTH_DIR": str(_DATA_DIR / "wa"),
    "QUEUE_GENERIC_CONFIG": str(_COMPANY_CONFIG),
    # defaults point at /var/..., which tests must not touch
    "QUEUE_MANUAL_BACKUP_DIR": str(_DATA_DIR / "backups"),
    "QUEUE_SOURCE_CACHE_DIR": str(_DATA_DIR / "source-cache"),
    "QUEUE_PROFILE": "generic",
})
os.environ.pop("QUEUE_ALLOW_LAN", None)
os.environ.pop("QUEUE_INSTRUCTION_SOURCE_URL", None)

import app  # noqa: E402  (must follow the environment setup above)

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Test-Password-2026!"

_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()
_message_ids = itertools.count(1)
_phones = itertools.count(1000)
_admin_cookie = ""


class _QuietHandler(app.TicketHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def server_port() -> int:
    global _server
    with _server_lock:
        if _server is None:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, name="test-http", daemon=True).start()
            atexit.register(_server.shutdown)
        return _server.server_address[1]


class Response:
    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> dict:
        return json.loads(self.body)

    @property
    def location(self) -> str:
        return self.headers.get("Location", "")

    def cookies(self) -> str:
        pairs = [value.split(";", 1)[0] for value in self.headers.get_all("Set-Cookie") or []]
        return "; ".join(pair for pair in pairs if not pair.endswith("="))


def request(method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None) -> Response:
    connection = http.client.HTTPConnection("127.0.0.1", server_port(), timeout=30)
    try:
        all_headers = {"Host": "127.0.0.1", "Content-Length": str(len(body))}
        all_headers.update(headers or {})
        connection.request(method, path, body, all_headers)
        raw = connection.getresponse()
        return Response(raw.status, raw.headers, raw.read())
    finally:
        connection.close()


_csrf_tokens: dict[str, str] = {}


def csrf_token(cookie: str) -> str:
    """The token a signed-in browser gets in <meta name="queue-csrf">."""
    if cookie not in _csrf_tokens:
        match = re.search(r'<meta name="queue-csrf" content="([^"]+)"', get("/", cookie).text)
        if not match:
            raise AssertionError("no queue-csrf meta tag on /")
        _csrf_tokens[cookie] = match.group(1)
    return _csrf_tokens[cookie]


def post_form(path: str, fields: dict[str, object], cookie: str = "", *, csrf: bool = True) -> Response:
    """POST a form; with a cookie the user's CSRF token is added unless csrf=False."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    fields = dict(fields)
    if cookie:
        headers["Cookie"] = cookie
        if csrf and "csrf_token" not in fields:
            fields["csrf_token"] = csrf_token(cookie)
    return request("POST", path, urlencode({k: str(v) for k, v in fields.items()}).encode(), headers)


def get(path: str, cookie: str = "") -> Response:
    return request("GET", path, headers={"Cookie": cookie} if cookie else None)


def admin_cookie() -> str:
    """Create the first administrator through /setup once, then reuse the session."""
    global _admin_cookie
    if not _admin_cookie:
        if app.AUTH.has_users():
            response = post_form("/login", {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/"})
        else:
            response = post_form("/setup", {
                "username": ADMIN_USERNAME, "display_name": "Администратор",
                "password": ADMIN_PASSWORD, "password2": ADMIN_PASSWORD,
            })
        if response.status not in (302, 303) or not response.cookies():
            raise AssertionError(f"admin login failed: {response.status} {response.text[:300]}")
        _admin_cookie = response.cookies()
    return _admin_cookie


class WhatsAppUser:
    """Simulates the WhatsApp connector posting one user's messages to /api/whatsapp."""

    def __init__(self) -> None:
        number = next(_phones)
        self.phone = f"+7701000{number:04d}"
        self.chat_id = f"7701000{number:04d}@c.us"

    def send(self, text: str, *, external_id: str = "", token: str | None = None) -> Response:
        stripped = text.strip()
        payload = {
            "external_id": external_id or f"test-message-{next(_message_ids)}",
            "sender": "Тестовый пользователь",
            "phone": self.phone,
            "chat_id": self.chat_id,
            "text": text,
            "menu_choice": stripped if stripped in set("012345678") else "",
            "draft_finish": stripped.casefold() == "готово",
            "attachment_name": "",
            "message_type": "chat",
            "message_timestamp": 1_800_000_000,
        }
        headers = {"Content-Type": "application/json",
                   "X-Webhook-Token": app.WEBHOOK_TOKEN if token is None else token}
        return request("POST", "/api/whatsapp", json.dumps(payload).encode(), headers)

    def say(self, text: str) -> dict:
        response = self.send(text)
        if response.status not in (200, 201):
            raise AssertionError(f"{text!r} -> HTTP {response.status}: {response.text[:300]}")
        return response.json()

    def register_profile(self, department: str = "Бухгалтерия", position: str = "Бухгалтер") -> dict:
        self.say("Здравствуйте")
        self.say("1")
        return self.say(f"Подразделение: {department}\nДолжность: {position}")

    def tickets(self) -> list[dict]:
        with app.STORE.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tickets WHERE phone = ? OR chat_id = ? ORDER BY id", (self.phone, self.chat_id)
            ).fetchall()
        return [dict(row) for row in rows]
