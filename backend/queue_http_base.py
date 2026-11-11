"""Основа обработчика HTTP: чтение тела запроса, JSON и форм, проверка CSRF, ответы и заголовки безопасности."""
from __future__ import annotations

import json
import os
import secrets
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, quote

from queue_core import content_disposition_header


# Largest request body read just to refuse it; anything bigger is left unread.
MAX_UNREAD_BODY = 64 * 1024
UNREAD_BODY_SECONDS = 2


class HttpBase:
    def handle_one_request(self) -> None:
        # A browser leaving the page closes its pending long poll: not a server error.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if not any(line.lower().startswith(b"cache-control:") for line in getattr(self,"_headers_buffer",[])):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def request_host_allowed(self) -> bool:
        host = self.headers.get("Host", "").lower().strip()
        hostname = host.split(":", 1)[0].strip("[]")
        configured_hosts = {
            item.strip().casefold()
            for item in os.getenv("QUEUE_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if (
            os.getenv("QUEUE_ALLOW_LAN") == "1"
            or hostname in {"127.0.0.1", "localhost", "::1"}
            or hostname in configured_hosts
        ):
            return True
        self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "Invalid site name")
        return False

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def csv_bytes_response(self, body: bytes, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", content_disposition_header("attachment", filename))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def page_response(self, name: str) -> None:
        self.file_response(self.deps.root / "frontend" / "pages" / f"{name}.html", "text/html; charset=utf-8")

    def discard_unread_body(self, consumed: int = 0) -> None:
        """Read what a refusing route left of the request body. Closing with it unread
        resets the connection before the client reads the answer."""
        try:
            left = int(self.headers.get("Content-Length", "0")) - consumed
            if 0 < left <= MAX_UNREAD_BODY:
                # the answer is already sent: never wait long for bytes that may not come
                waited = self.connection.gettimeout()
                self.connection.settimeout(UNREAD_BODY_SECONDS)
                try:
                    self.rfile.read(left)
                finally:
                    self.connection.settimeout(waited)
        except (ValueError, OSError):
            pass

    def read_body(self, max_bytes: int) -> bytes | None:
        """The whole request body, read from the socket at most once (the CSRF check
        may need it before the route). None if missing, malformed or over max_bytes."""
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached if len(cached) <= max_bytes else None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > max_bytes:
            return None
        self._body_cache = self.rfile.read(length)
        return self._body_cache

    def body_already_read(self) -> bool:
        return getattr(self, "_body_cache", None) is not None

    def read_json_body(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        try:
            raw = self.read_body(max_bytes)
            if raw is None:
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_authorized_json(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        supplied = self.headers.get("X-Webhook-Token", "").encode("utf-8", "surrogateescape")
        if not secrets.compare_digest(supplied, self.deps.webhook_token.encode("utf-8")):
            self.json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return None
        try:
            raw = self.read_body(max_bytes)
            if raw is None:
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_form(self) -> dict[str, str]:
        raw = self.read_body(1_000_000)
        if raw is None:
            return {}
        body = raw.decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(body).items()}

    def csrf_check_passed(self, path: str) -> bool:
        """Signed-in POSTs must carry the user's token: X-CSRF-Token header or csrf_token field."""
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied and self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            supplied = self.read_form().get("csrf_token", "")
        if self.deps.csrf_token_valid(supplied):
            return True
        message = "Страница устарела. Обновите её и повторите действие"
        if path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or "X-CSRF-Token" in self.headers:
            self.json_response({"error": message, "csrf": False}, HTTPStatus.FORBIDDEN)
        else:
            self.redirect("/?notice=" + quote(message))
        return False

    @staticmethod
    def form_int(form: dict[str, str], key: str) -> int:
        try:
            return int(form.get(key, "0"))
        except ValueError:
            return 0

    @staticmethod
    def query_int(query: dict[str, list[str]], key: str) -> int:
        try:
            return int(query.get(key, ["0"])[0])
        except ValueError:
            return 0

    def file_response(self, path: Path, content_type: str) -> None:
        self.uncached_response(path.read_bytes(), content_type)

    def uncached_response(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def cached_file_response(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(body)

    def html_response(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def json_response(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()
