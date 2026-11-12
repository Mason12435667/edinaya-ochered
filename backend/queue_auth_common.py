"""Общее для учётных записей: настройки входа, хэш пароля, время в UTC и пользователь текущего запроса."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any


PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = max(200_000, int(os.getenv("QUEUE_AUTH_PBKDF2_ITERATIONS", "390000")))
SESSION_HOURS = max(1, min(168, int(os.getenv("QUEUE_AUTH_SESSION_HOURS", "12"))))
LOCKOUT_ATTEMPTS = max(3, min(20, int(os.getenv("QUEUE_AUTH_LOCKOUT_ATTEMPTS", "5"))))
LOCKOUT_MINUTES = max(1, min(120, int(os.getenv("QUEUE_AUTH_LOCKOUT_MINUTES", "10"))))
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ROLES = {"admin", "employee"}
_THREAD_CONTEXT = threading.local()


def self_registration_enabled() -> bool:
    """Open sign-up on /register; off by default (accounts are created by an administrator)."""
    return os.getenv("QUEUE_ALLOW_SELF_REGISTRATION", "0") == "1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).replace(microsecond=0).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_username(value: str) -> str:
    return str(value or "").strip().lower()


def password_error(password: str) -> str:
    value = str(password or "")
    if len(value) < 10:
        return "Пароль должен содержать минимум 10 символов"
    if len(value) > 256:
        return "Пароль слишком длинный"
    return ""


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    problem = password_error(password)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=32,
    )
    return f"pbkdf2_{PBKDF2_ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = str(encoded).split("$", 3)
        if scheme != f"pbkdf2_{PBKDF2_ALGORITHM}":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 5_000_000:
            return False
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
        actual = hashlib.pbkdf2_hmac(
            PBKDF2_ALGORITHM,
            str(password or "").encode("utf-8"),
            salt,
            iterations,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def set_current_user(user: dict[str, Any] | None) -> None:
    _THREAD_CONTEXT.user = dict(user) if user else None


def current_user() -> dict[str, Any] | None:
    user = getattr(_THREAD_CONTEXT, "user", None)
    return dict(user) if isinstance(user, dict) else None


def clear_current_user() -> None:
    if hasattr(_THREAD_CONTEXT, "user"):
        delattr(_THREAD_CONTEXT, "user")
