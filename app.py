from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import secrets
import threading
import webbrowser
import sys
import queue_message_identity
import queue_bot_texts
import queue_features
import queue_productivity
import queue_contextual_tickets
import queue_workflow
import queue_reliability
import queue_performance
import queue_uploads
from queue_avatars import AvatarCache
import queue_user_locale
import queue_auth
import queue_core
import queue_operations
import queue_runtime
import queue_reporting
import queue_realtime
import queue_auth_views
import queue_admin_views
import queue_chat_state
import queue_whatsapp_handlers
import queue_http_handler
import queue_ticket_service
import queue_work_context
import queue_ticket_views
import queue_admin_pages
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from ticketing import CATEGORIES, PRIORITIES, STATUSES, TicketStore
from queue_store_conversations import MENU_CONTEXT
from queue_core import log_suppressed, normalize_message
import queue_generic_profile
from queue_menu_config import MenuConfig
from queue_bot_menu import BotMenu
from queue_contact_profiles import ContactProfiles
from queue_inbound_service import InboundDependencies, InboundService
from queue_attachment_archive import AttachmentArchive
from queue_outbound_service import OutboundService
from queue_stale_menu import StaleMenuRecovery
from queue_productivity_http import ProductivityRoutes
from queue_reliability_http import ReliabilityRoutes
from queue_workflow_http import WorkflowRoutes

# Public release from CHANGELOG.md; reported by /api/integration/ping.
APP_VERSION = "v143"

# Modules split out of app.py read shared services and settings as app.NAME.
for _module in (queue_chat_state, queue_ticket_views, queue_admin_pages,
                queue_whatsapp_handlers, queue_http_handler):
    _module.bind(sys.modules[__name__])


ROOT =Path(__file__).resolve().parent

# 1.00.6.27: first modularization phase. Keep the historic names in app.py as
# aliases so existing feature modules can continue receiving app as their context.
LOCAL_TIMEZONE = queue_core.LOCAL_TIMEZONE
resolve_data_dir = queue_core.resolve_data_dir
prepare_database_path = queue_core.prepare_database_path
normalize_phone = queue_core.normalize_phone
display_phone = queue_core.display_phone
normalized_phone_digits = queue_core.normalized_phone_digits
e = queue_core.e
human_time = queue_core.human_time
epoch_time = queue_core.epoch_time
valid_chat_id = queue_core.valid_chat_id
valid_group_id = queue_core.valid_group_id
valid_conversation_id = queue_core.valid_conversation_id
content_disposition_header = queue_core.content_disposition_header
media_extension = queue_core.media_extension
human_bytes = queue_core.human_bytes
directory_size = queue_core.directory_size
parse_iso_datetime = queue_core.parse_iso_datetime


DATA_DIR = resolve_data_dir()
DATABASE_PATH = prepare_database_path(DATA_DIR)
AVATARS = AvatarCache(DATA_DIR / "avatars")
STORE = TicketStore(DATABASE_PATH)
AUTH = queue_auth.AuthStore(DATABASE_PATH)


def _csrf_secret() -> bytes:
    # Kept in the database so pages opened before a restart can still submit.
    value = STORE.get_setting("csrf_secret", "")
    if len(value) < 32:
        value = secrets.token_urlsafe(48)
        STORE.set_setting("csrf_secret", value)
    return value.encode("utf-8")


CSRF_SECRET = _csrf_secret()


def csrf_token(user: dict[str, object] | None = None) -> str:
    """Form/request token of the signed-in user (defaults to the request's user)."""
    user = user if user is not None else (queue_auth.current_user() or {})
    subject = f"{int(user.get('id', 0) or 0)}:{str(user.get('username', '') or '').casefold()}"
    return hmac.new(CSRF_SECRET, subject.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_token_valid(value: object, user: dict[str, object] | None = None) -> bool:
    supplied = str(value or "").encode("utf-8", "surrogateescape")
    return bool(supplied) and hmac.compare_digest(supplied, csrf_token(user).encode("ascii"))
queue_operations.initialize(STORE)
queue_productivity.initialize(STORE)
queue_contextual_tickets.initialize(STORE)
queue_workflow.initialize(STORE)  # QUEUE_3_3_96_WORKFLOW
queue_reliability.initialize(STORE)  # QUEUE_3_3_99_RELIABILITY
MEDIA_DIR = DATA_DIR / "chat-media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
OUTBOUND_MEDIA_DIR = DATA_DIR / "outbound-media"
OUTBOUND_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
GLOBAL_AUTO_REPLY_FLAG = DATA_DIR / "auto-reply.enabled"
RUNTIME = queue_runtime.RuntimeServices(STORE, AUTH, DATABASE_PATH, DATA_DIR)
scheduled_backup_hours = RUNTIME.scheduled_backup_hours
scheduled_backup_keep = RUNTIME.scheduled_backup_keep
list_scheduled_backups = RUNTIME.list_scheduled_backups
create_scheduled_backup = RUNTIME.create_scheduled_backup
scheduled_backup_snapshot = RUNTIME.scheduled_backup_snapshot
scheduled_backup_worker = RUNTIME.scheduled_backup_worker
process_resource_snapshot = RUNTIME.process_resource_snapshot
REPORTING = queue_reporting.ReportingService(STORE, AUTH)
_final_ticket_filter = REPORTING.filter_tickets
_analytics_snapshot = REPORTING.analytics_snapshot


def global_auto_reply_enabled() -> bool:
    """Return the global WhatsApp auto-reply switch state.

    Missing/unreadable flag is treated as enabled for backwards compatibility.
    The CLI writes either ``on`` or ``off`` and the value is read on every
    request, so switching does not require restarting the site or connector.
    """
    try:
        if not GLOBAL_AUTO_REPLY_FLAG.exists():
            return True
        value = GLOBAL_AUTO_REPLY_FLAG.read_text(encoding="utf-8").strip().casefold()
    except OSError:
        return True
    return value not in {"0", "off", "false", "no", "disabled", "выкл"}


MAX_MEDIA_BYTES = max(1, min(256, int(os.getenv("QUEUE_MAX_MEDIA_MB", "12")))) * 1024 * 1024
UPLOADS = queue_uploads.Uploads(OUTBOUND_MEDIA_DIR / "uploads", STORE)
RETENTION_DAYS = max(1, min(3650, int(os.getenv("QUEUE_RETENTION_DAYS", "20"))))
# Фото, присланные непосредственно перед текстом заявки, можно автоматически
# прикрепить к создаваемой заявке. Окно ограничено, чтобы старые скриншоты не
# подхватывались случайно. Значение можно переопределить через systemd env.
PRE_TICKET_IMAGE_LOOKBACK_SECONDS = max(60, min(3600, int(os.getenv("QUEUE_PRE_TICKET_IMAGE_LOOKBACK_SECONDS", "600"))))
HOST = os.getenv("TICKET_APP_HOST", "127.0.0.1")
PORT = int(os.getenv("TICKET_APP_PORT", "8000"))
LOCAL_MODE = os.getenv("QUEUE_LOCAL_MODE", "0") == "1"
MONITOR_LOG_PATH = DATA_DIR / "system-monitor.log"
MONITOR_HEARTBEAT_PATH = DATA_DIR / "system-monitor.heartbeat"
SLA_MINUTES = {
    "urgent": max(1, int(os.getenv("QUEUE_SLA_URGENT_MIN", "15"))),
    "high": max(1, int(os.getenv("QUEUE_SLA_HIGH_MIN", "30"))),
    "normal": max(1, int(os.getenv("QUEUE_SLA_NORMAL_MIN", "60"))),
    "low": max(1, int(os.getenv("QUEUE_SLA_LOW_MIN", "120"))),
}


def connector_token() -> str:
    configured = os.getenv("WEBHOOK_TOKEN", "").strip()
    if len(configured) < 32:
        raise RuntimeError(
            "WEBHOOK_TOKEN is not configured. Store secrets outside the project "
            "and load /etc/edinaya-ochered/secrets.env through systemd EnvironmentFile."
        )
    return configured


WEBHOOK_TOKEN = connector_token()
DEFAULT_EMPLOYEES = [f"Сотрудник {number}" for number in range(1, 6)]
ADMIN_USER = os.getenv("QUEUE_ADMIN_USER", "queueadmin").strip().casefold()
ADMIN_SESSION_TOKEN = secrets.token_urlsafe(48)
ADMIN_SESSION_COOKIE = "queue_admin_session"
ADMIN_SESSION_MAX_AGE = 12 * 60 * 60
AUTH_SESSION_COOKIE = "queue_session"
AUTH_SESSION_MAX_AGE = queue_auth.SESSION_HOURS * 60 * 60
MAX_EMPLOYEES = 50
MENU_ACTION_TICKET = queue_bot_texts.MENU_ACTION_TICKET
MENU_ACTION_INSTRUCTION = queue_bot_texts.MENU_ACTION_INSTRUCTION
MENU_ACTIONS = queue_bot_texts.MENU_ACTIONS
SUPPORT_MODE_CONTEXT = queue_bot_texts.SUPPORT_MODE_CONTEXT
ERROR_REPORT_CONTEXT = queue_bot_texts.ERROR_REPORT_CONTEXT
ACTIVE_TICKETS_CONTEXT = queue_bot_texts.ACTIVE_TICKETS_CONTEXT
ACTIVE_TICKET_FOLLOWUP_CONTEXT = queue_bot_texts.ACTIVE_TICKET_FOLLOWUP_CONTEXT
REACTION_EMOJIS = {"👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "🎉", "✅", "👏", "🤔", "👀", "😡", "💯", "👌", "😁", "😍", "🥳"}

# 1.00.6.11: ordinary WhatsApp users receive Kazakh texts. The employee/admin
# interface, stored ticket data, internal statuses and menu editor remain Russian.
USER_STATUS_LABELS_KZ = {
    "new": "Жаңа",
    "in_progress": "Жұмыста",
    "done": "Орындалды",
    "invalid": "Жарамсыз",
}
USER_CLOSE_REASON_LABELS_KZ = {
    "Решено": "Шешілді",
    "Дубль": "Қайталанған өтінім",
    "Недействительно": "Жарамсыз",
    "Ошибка пользователя": "Пайдаланушы қатесі",
    "Передано": "Берілді",
    "Другое": "Басқа",
}
USER_CATEGORY_PROMPTS_KZ = {'general': 'Мәселені немесе сұрақты сипаттаңыз. Қажет болса, скриншот немесе файл тіркеңіз.\n0 — негізгі мәзірге оралу'}


def user_status_label(status: str, fallback: str = "") -> str:
    return queue_user_locale.user_status_label(status, fallback)

CHAT_FAVORITES_SETTING = "whatsapp_favorite_chats"


def favorite_chat_ids() -> set[str]:
    """Return persistent starred WhatsApp chats/groups."""
    raw = STORE.get_setting(CHAT_FAVORITES_SETTING, "[]")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    if not isinstance(values, list):
        return set()
    result: set[str] = set()
    for value in values[:500]:
        chat_id = valid_conversation_id(str(value or "")) or valid_group_id(str(value or ""))
        if not chat_id:
            continue
        if not chat_id.endswith("@g.us"):
            chat_id = STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        result.add(chat_id)
    return result


def set_chat_favorite(chat_id: str, favorite: bool) -> tuple[bool, str]:
    clean = valid_conversation_id(chat_id) or valid_group_id(chat_id)
    if not clean:
        return False, ""
    if not clean.endswith("@g.us"):
        clean = STORE.canonical_whatsapp_chat_id(clean) or clean
    values = favorite_chat_ids()
    if favorite:
        values.add(clean)
    else:
        values.discard(clean)
    STORE.set_setting(CHAT_FAVORITES_SETTING, json.dumps(sorted(values), ensure_ascii=False))
    return True, clean


error_report_prompt = queue_bot_texts.error_report_prompt


is_active_tickets_command = queue_bot_texts.is_active_tickets_command




V3_FIELD_LABELS_RU = queue_bot_texts.V3_FIELD_LABELS_RU
V3_FIELD_LABELS_KZ = queue_bot_texts.V3_FIELD_LABELS_KZ


language_selection_text_v3 = queue_bot_texts.language_selection_text_v3


active_tickets_menu_number = queue_bot_texts.active_tickets_menu_number


v3_missing_fields_reply = queue_bot_texts.v3_missing_fields_reply


v3_duplicate_reply = queue_bot_texts.v3_duplicate_reply


def save_media_payload(
    chat_id: str, message_id: str, media_base64: str, mimetype: str, filename: str
) -> str:
    if not media_base64 or not chat_id or not message_id:
        return ""
    try:
        with queue_performance.media_slot():
            try:
                raw = base64.b64decode(media_base64, validate=True)
            except (ValueError, binascii.Error):
                return ""
            if not raw or len(raw) > MAX_MEDIA_BYTES:
                return ""
            digest = hashlib.sha256(f"{chat_id}|{message_id}".encode("utf-8")).hexdigest()
            path = MEDIA_DIR / f"{digest}{media_extension(mimetype, filename)}"
            try:
                temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
                try:
                    temporary.write_bytes(raw)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                try:
                    path.chmod(0o640)
                except OSError:
                    pass
            except OSError:
                return ""
            return str(path)
    except TimeoutError:
        return ""


def whatsapp_message_stanza_id(message_id: str) -> str:
    value = normalize_message(str(message_id or ""))[:160]
    if not value:
        return ""
    return value.rsplit("_", 1)[-1] if "_" in value else value


def same_whatsapp_message_identity(left: str, right: str) -> bool:
    left_value = normalize_message(str(left or ""))[:160]
    right_value = normalize_message(str(right or ""))[:160]
    if not left_value or not right_value:
        return False
    if left_value == right_value:
        return True
    left_stanza = whatsapp_message_stanza_id(left_value)
    right_stanza = whatsapp_message_stanza_id(right_value)
    return bool(left_stanza and right_stanza and left_stanza == right_stanza)


def scrub_deleted_whatsapp_message(
    chat_id: str,
    message_id: str,
    media_path: str = "",
    message_timestamp: int = 0,
    from_me: bool | None = None,
) -> tuple[str, int]:
    """Scrub the original row after WhatsApp 'delete for everyone'.

    WhatsApp may report the same direct chat as @lid while our DB already knows it
    as @c.us. 3.3.116 updated only an exact (chat_id, message_key) pair, which could
    leave the original row intact and insert a second tombstone. Here we reconcile
    by the stable WhatsApp stanza id and direction/time, scrub the original row(s),
    and return the original key so the caller does not create a duplicate bubble.
    """
    safe_chat_id = valid_conversation_id(chat_id)
    safe_message_id = normalize_message(message_id)[:160]
    if not safe_chat_id or not safe_message_id:
        return safe_message_id, 0

    resolved_key = safe_message_id
    matched_rows: list[tuple[int, str, str, str, int, int]] = []
    media_paths: set[str] = set()
    if media_path:
        media_paths.add(str(media_path))

    try:
        with sqlite3.connect(DATABASE_PATH, timeout=5.0) as connection:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(whatsapp_chat_messages)")}
            required = {"id", "chat_id", "message_key"}
            if required.issubset(columns):
                select_media = "media_path" if "media_path" in columns else "''"
                select_ts = "message_timestamp" if "message_timestamp" in columns else "0"
                select_from = "from_me" if "from_me" in columns else "0"
                rows = connection.execute(
                    f"SELECT id, chat_id, message_key, {select_media}, {select_ts}, {select_from} "
                    "FROM whatsapp_chat_messages WHERE message_key=? ORDER BY id DESC",
                    (safe_message_id,),
                ).fetchall()

                if not rows:
                    stanza = whatsapp_message_stanza_id(safe_message_id)
                    if stanza:
                        # Keep the query bounded to recent candidates when a timestamp is
                        # available; the stanza id is otherwise already highly specific.
                        if "message_timestamp" in columns and message_timestamp > 0:
                            rows = connection.execute(
                                f"SELECT id, chat_id, message_key, {select_media}, {select_ts}, {select_from} "
                                "FROM whatsapp_chat_messages "
                                "WHERE message_timestamp BETWEEN ? AND ? ORDER BY id DESC LIMIT 250",
                                (max(0, int(message_timestamp) - 300), int(message_timestamp) + 300),
                            ).fetchall()
                        else:
                            rows = connection.execute(
                                f"SELECT id, chat_id, message_key, {select_media}, {select_ts}, {select_from} "
                                "FROM whatsapp_chat_messages ORDER BY id DESC LIMIT 1200"
                            ).fetchall()
                        rows = [row for row in rows if whatsapp_message_stanza_id(str(row[2] or "")) == stanza]

                for row in rows:
                    row_from_me = bool(int(row[5] or 0))
                    if from_me is not None and row_from_me != bool(from_me):
                        continue
                    row_ts = int(row[4] or 0)
                    if message_timestamp > 0 and row_ts > 0 and abs(row_ts - int(message_timestamp)) > 300:
                        continue
                    matched_rows.append((int(row[0]), str(row[1] or ""), str(row[2] or ""), str(row[3] or ""), row_ts, int(row[5] or 0)))

                if matched_rows:
                    # Prefer the row whose chat id already canonicalizes to the current
                    # conversation, otherwise the newest exact/stanza match is safe.
                    canonical = STORE.canonical_whatsapp_chat_id(safe_chat_id) or safe_chat_id
                    preferred = matched_rows[0]
                    for candidate in matched_rows:
                        candidate_chat = STORE.canonical_whatsapp_chat_id(candidate[1]) or candidate[1]
                        if candidate_chat == canonical:
                            preferred = candidate
                            break
                    resolved_key = preferred[2] or safe_message_id
                    assignments: list[str] = []
                    for column in (
                        "body", "media_path", "media_url", "media_mime", "media_name",
                        "transcript", "quoted_body", "quoted_sender", "reactions_json"
                    ):
                        if column in columns:
                            assignments.append(f"{column}=''")
                    if "deleted" in columns:
                        assignments.append("deleted=1")
                    if assignments:
                        ids = [row[0] for row in matched_rows]
                        placeholders = ",".join("?" for _ in ids)
                        connection.execute(
                            f"UPDATE whatsapp_chat_messages SET {', '.join(assignments)} WHERE id IN ({placeholders})",
                            ids,
                        )
                        connection.commit()
                    for row in matched_rows:
                        if row[3]:
                            media_paths.add(row[3])
    except Exception as exc:
        print(f"WhatsApp remote-delete DB reconcile warning: {safe_message_id}: {exc}", flush=True)

    media_root = MEDIA_DIR.resolve()
    for value in media_paths:
        try:
            candidate = Path(value).resolve()
            if media_root in candidate.parents and candidate.is_file():
                candidate.unlink()
        except OSError as exc:
            print(f"WhatsApp remote-delete media cleanup warning: {safe_message_id}: {exc}", flush=True)

    return resolved_key, len(matched_rows)


def save_outbound_media(media_base64: str, mimetype: str, filename: str) -> str:
    try:
        with queue_performance.media_slot():
            try:
                raw = base64.b64decode(media_base64, validate=True)
            except (ValueError, binascii.Error):
                return ""
            if not raw or len(raw) > MAX_MEDIA_BYTES:
                return ""
            digest = hashlib.sha256(raw + secrets.token_bytes(8)).hexdigest()
            path = OUTBOUND_MEDIA_DIR / f"{digest}{media_extension(mimetype, filename)}"
            try:
                path.write_bytes(raw)
                path.chmod(0o640)
            except OSError:
                return ""
            return str(path)
    except TimeoutError:
        return ""


def save_outbound_media_stream(handler, content_length: int, mimetype: str, filename: str) -> str:
    """Save one raw upload directly to outbound-media without base64/RAM expansion."""
    if content_length <= 0 or content_length > MAX_MEDIA_BYTES:
        return ""
    try:
        with queue_performance.media_slot():
            extension = media_extension(mimetype, filename)
            final_path = OUTBOUND_MEDIA_DIR / f"{secrets.token_hex(24)}{extension}"
            temporary = final_path.with_name(final_path.name + "." + secrets.token_hex(6) + ".tmp")
            remaining = content_length
            # A small body may already have been read (CSRF check); replay it.
            source = io.BytesIO(handler.read_body(content_length) or b"") if handler.body_already_read() else handler.rfile
            try:
                with temporary.open("wb") as stream:
                    while remaining > 0:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise OSError("upload ended before Content-Length")
                        stream.write(chunk)
                        remaining -= len(chunk)
                if remaining != 0 or temporary.stat().st_size != content_length:
                    raise OSError("upload size mismatch")
                temporary.replace(final_path)
                final_path.chmod(0o640)
                return str(final_path)
            except OSError:
                temporary.unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
                return ""
    except TimeoutError:
        return ""


def cleanup_retention_once() -> None:
    try:
        result = queue_workflow.cleanup_retention(STORE)
        media_root = MEDIA_DIR.resolve()
        removed_files = 0
        for value in result.get("media_paths", []):
            try:
                path = Path(str(value)).resolve()
                if media_root in path.parents and path.is_file():
                    path.unlink()
                    removed_files += 1
            except OSError:
                pass
        # Outgoing attachments are temporary. Anything old is safe to remove.
        retention_policy = queue_workflow.retention_settings(STORE)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(retention_policy.get("media", RETENTION_DAYS)))
        for path in OUTBOUND_MEDIA_DIR.glob("*"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff:
                    path.unlink()
            except OSError:
                pass
        if result.get("messages") or result.get("tickets") or removed_files:
            policy = result.get("settings", {})
            print(f"Автоочистка: сообщения={policy.get('messages')}д, медиа={policy.get('media')}д, заявки={policy.get('tickets')}д; сообщений={result.get('messages', 0)}, заявок={result.get('tickets', 0)}, файлов={removed_files}")
    except Exception as exc:
        print(f"Автоочистка временно не выполнена: {exc}")


def retention_worker() -> None:
    waiter = threading.Event()
    while True:
        waiter.wait(6 * 60 * 60)
        cleanup_retention_once()

def mention_only_message(message: dict[str, object]) -> bool:
    """Return True only when the message contains mentions and no real text/media."""
    if not isinstance(message, dict):
        return False
    if str(message.get("media_path", "") or "").strip() or str(message.get("media_name", "") or "").strip():
        return False
    if str(message.get("quoted_message_key", "") or "").strip():
        return False
    body = str(message.get("body", "") or "").strip()
    if not body:
        return False

    rest = body
    raw_mentions = message.get("mentions", [])
    if isinstance(raw_mentions, list):
        exact_tokens: list[str] = []
        for mention in raw_mentions:
            if not isinstance(mention, dict):
                continue
            for key in ("name", "id", "resolved_id"):
                value = str(mention.get(key, "") or "").strip()
                if not value:
                    continue
                if "@" in value and not value.startswith("@"):
                    value = value.split("@", 1)[0]
                value = value.lstrip("@").strip()
                if value:
                    exact_tokens.append("@" + value)
        for token in sorted(set(exact_tokens), key=len, reverse=True):
            rest = re.sub(re.escape(token), "", rest, flags=re.IGNORECASE)

    # WhatsApp stores mention bodies using a compact @identifier. Keep the
    # fallback deliberately strict so normal text containing @ is never blocked.
    rest = re.sub(r"@[\w.+:-]+", "", rest, flags=re.UNICODE)
    return not rest.strip() and "@" in body


_MEDIA_PLACEHOLDER_BODIES = {
    "[фото]", "фото", "[image]", "image",
    "[видео]", "видео", "[video]", "video",
    "[аудио]", "аудио", "[audio]", "audio",
    "[документ]", "документ", "[document]", "document",
    "[вложение]", "вложение", "[attachment]", "attachment",
    "[стикер]", "стикер", "[sticker]", "sticker",
}


def _observed_outbound_body(body: object, message_type: object = "") -> str:
    value = normalize_message(str(body or ""))[:1000]
    safe_type = normalize_message(str(message_type or "")).casefold()
    if safe_type in {"image", "photo", "video", "audio", "ptt", "document", "sticker"}:
        if value.casefold() in _MEDIA_PLACEHOLDER_BODIES:
            return ""
    return value


def _outbound_media_matches_observation(
    row: object,
    observed_body: object = "",
    observed_reply_to: object = "",
    observed_type: object = "",
) -> bool:
    """Return True only when an outbound media row belongs to this WhatsApp echo.

    1.00.6.36: an old uncertain attachment could be rebound to the next plain
    text/reply in the same chat because an empty queued caption was treated as
    a wildcard. Body and reply context are now strict identity signals.
    """
    try:
        queued_body = normalize_message(str(row["body"] or ""))[:1000]
    except Exception:
        log_suppressed("app._outbound_media_matches_observation")
        queued_body = ""
    current_body = _observed_outbound_body(observed_body, observed_type)
    if queued_body != current_body:
        return False

    try:
        queued_reply = normalize_message(str(row["reply_to_key"] or ""))[:160]
    except Exception:
        log_suppressed("app._outbound_media_matches_observation")
        queued_reply = ""
    current_reply = normalize_message(str(observed_reply_to or ""))[:160]
    if bool(queued_reply) != bool(current_reply):
        return False
    if queued_reply and not same_whatsapp_message_identity(queued_reply, current_reply):
        return False
    return True


# QUEUE_3_3_64_SYSTEM_MEDIA_HISTORY
# Recover the preserved local attachment for a message that was sent from the
# Queue UI.  whatsapp-web.js can report the same outgoing message a moment later
# as text/caption only, even though WhatsApp itself received the image/file.
def preserved_system_outgoing_media(
    message_key: str,
    observed_body: str = "",
    observed_reply_to: str = "",
    observed_type: str = "",
) -> dict[str, str]:
    safe_key = normalize_message(str(message_key or ""))[:180]
    if not safe_key:
        return {}

    def same_provider_id(left: object, right: object) -> bool:
        a = str(left or "").strip()
        b = str(right or "").strip()
        if not a or not b:
            return False
        if a == b or a.endswith(b) or b.endswith(a):
            return True
        try:
            da, sa = queue_message_identity.parts(a)
            db, sb = queue_message_identity.parts(b)
            return bool(sa and sb and sa == sb and (not da or not db or da == db))
        except Exception:
            log_suppressed("app.same_provider_id")
            return False

    candidates = []
    try:
        with STORE.connection() as connection:
            exact = connection.execute(
                "SELECT id, provider_id, body, reply_to_key, media_mime, media_name FROM outbound_messages "
                "WHERE status='sent' AND provider_id=? ORDER BY id DESC LIMIT 1",
                (safe_key,),
            ).fetchone()
            if exact:
                candidates.append(exact)
            else:
                # Small bounded fallback for provider-id formatting differences.
                candidates.extend(connection.execute(
                    "SELECT id, provider_id, body, reply_to_key, media_mime, media_name FROM outbound_messages "
                    "WHERE status='sent' AND provider_id<>'' ORDER BY id DESC LIMIT 80"
                ).fetchall())
    except Exception:
        log_suppressed("app.preserved_system_outgoing_media")
        return {}

    for row in candidates:
        if not same_provider_id(row["provider_id"], safe_key):
            continue
        if not _outbound_media_matches_observation(
            row, observed_body, observed_reply_to, observed_type
        ):
            continue
        try:
            media_path = MEDIA_DIR / f"outbound-{int(row['id'])}.bin"
        except (TypeError, ValueError):
            continue
        if not media_path.is_file():
            continue
        return {
            "media_path": str(media_path),
            "media_mime": normalize_message(str(row["media_mime"] or ""))[:120],
            "media_name": normalize_message(str(row["media_name"] or ""))[:180],
        }
    return {}


# QUEUE_1_00_6_13_UNCERTAIN_LOCAL_MEDIA_RECONCILE
# whatsapp-web.js can occasionally return an empty send result even though the
# media was actually accepted by WhatsApp.  In that case the outbound row is
# intentionally left as "uncertain", while the fallback history sync observes
# the real outgoing provider id a moment later.  Reconcile only a media row
# from the same canonical chat and a tight timestamp window, then use the local
# preserved outbound copy instead of trying to download our own image back from
# WhatsApp.
def reconcile_uncertain_system_outgoing_media(
    chat_id: str,
    message_key: str,
    message_timestamp: int,
    body: str = "",
    message_type: str = "",
    quoted_message_key: str = "",
) -> dict[str, str]:
    safe_chat = valid_conversation_id(str(chat_id or ""))
    safe_key = normalize_message(str(message_key or ""))[:180]
    if not safe_chat or not safe_key:
        return {}
    try:
        timestamp = int(message_timestamp or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return {}

    safe_type = normalize_message(str(message_type or "")).casefold()
    expected_prefix = {
        "image": "image/",
        "video": "video/",
        "audio": "audio/",
        "ptt": "audio/",
        "sticker": "image/",
    }.get(safe_type, "")
    observed_body = normalize_message(str(body or ""))[:1000]

    candidates: list[tuple[int, int, sqlite3.Row]] = []
    try:
        with STORE.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, body, status, created_at, media_path,
                       media_mime, media_name, provider_id, reply_to_key
                  FROM outbound_messages
                 WHERE chat_id = ?
                   AND status IN ('uncertain','sending','processing')
                   AND (media_path <> '' OR media_mime <> '' OR media_name <> '')
                 ORDER BY id ASC
                 LIMIT 30
                """,
                (safe_chat,),
            ).fetchall()
    except Exception:
        log_suppressed("app.reconcile_uncertain_system_outgoing_media")
        return {}

    for row in rows:
        provider_id = normalize_message(str(row["provider_id"] or ""))[:180]
        if provider_id and provider_id != safe_key:
            continue
        row_mime = normalize_message(str(row["media_mime"] or ""))[:120]
        if expected_prefix and row_mime and not row_mime.casefold().startswith(expected_prefix):
            continue
        if not _outbound_media_matches_observation(
            row, observed_body, quoted_message_key, message_type
        ):
            continue
        try:
            created_raw = str(row["created_at"] or "").strip()
            created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            created_epoch = int(created_dt.timestamp())
        except (TypeError, ValueError, OverflowError):
            continue
        delta = abs(timestamp - created_epoch)
        # A WhatsApp echo normally appears within a few seconds.  Keep the
        # window deliberately narrow to avoid binding an unrelated attachment.
        if delta > 30:
            continue
        try:
            row_id = int(row["id"])
        except (TypeError, ValueError):
            continue
        candidates.append((delta, row_id, row))

    if not candidates:
        return {}
    _, row_id, row = min(candidates, key=lambda value: (value[0], value[1]))

    media: dict[str, str] = {}
    preserved_path = MEDIA_DIR / f"outbound-{row_id}.bin"
    if preserved_path.is_file():
        media = {
            "media_path": str(preserved_path),
            "media_mime": normalize_message(str(row["media_mime"] or ""))[:120],
            "media_name": normalize_message(str(row["media_name"] or ""))[:180],
        }
    else:
        try:
            preserved = ATTACHMENTS.preserve(row_id)
        except Exception:
            log_suppressed("app.reconcile_uncertain_system_outgoing_media")
            preserved = {}
        if preserved:
            media = {
                "media_path": str(preserved.get("media_path", "") or ""),
                "media_mime": normalize_message(str(preserved.get("media_mime", "") or row["media_mime"] or ""))[:120],
                "media_name": normalize_message(str(preserved.get("media_name", "") or row["media_name"] or ""))[:180],
            }

    if not media.get("media_path") or not Path(str(media["media_path"])).is_file():
        return {}

    try:
        updated = STORE.complete_outbound_message(row_id, True, "", safe_key)
    except Exception as error:
        print(f"Не удалось связать локальное вложение #{row_id} с {safe_key}: {error}", flush=True)
        return {}
    if not updated:
        return {}
    print(
        f"Локальное исходящее вложение восстановлено без скачивания из WhatsApp: "
        f"queue={row_id} provider={safe_key}",
        flush=True,
    )
    return media


def decorate_chat_messages(chat_id: str, messages: list[dict[str, object]]) -> list[dict[str, object]]:
    # Для групп подменяем технические WhatsApp LID в @упоминаниях на понятные
    # имена из уже загруженного списка участников. Одновременно используем
    # sender_id сообщения, чтобы заменить «Участник группы» на реальное имя и
    # показать номер даже если имя/номер догрузились уже после самого сообщения.
    mention_names: dict[str, str] = {}
    participant_by_id: dict[str, dict[str, object]] = {}
    if str(chat_id).endswith("@g.us"):
        try:
            with CHAT_LOCK:
                participants_map = CHAT_STATE.get("group_participants", {})
                participants = list(participants_map.get(chat_id, [])) if isinstance(participants_map, dict) else []
            for participant in participants:
                if not isinstance(participant, dict):
                    continue
                mention_id = str(participant.get("mention_id", "")).strip()
                resolved_id = str(participant.get("resolved_id", "")).strip()
                name = str(participant.get("name", "")).strip()
                phone = str(participant.get("phone", "")).strip()
                for participant_id in (mention_id, resolved_id):
                    if participant_id:
                        participant_by_id[participant_id] = participant
                if mention_id and name:
                    mention_names[mention_id.split("@")[0]] = name
                if resolved_id and name:
                    mention_names[resolved_id.split("@")[0]] = name
                if phone:
                    phone_digits = re.sub(r"\D", "", phone)
                    if phone_digits:
                        participant_by_id[f"{phone_digits}@c.us"] = participant
        except Exception:
            log_suppressed("app.decorate_chat_messages")
            mention_names = {}
            participant_by_id = {}

    manual_names = {row["chat_id"]: row["name"] for row in cached_snapshot_value("manual_contacts", 2.5, STORE.list_manual_whatsapp_contacts)}
    quote_cache = {}
    result: list[dict[str, object]] = []
    generic_group_names = {"", "участник", "участник группы", "пользователь whatsapp"}
    for raw in messages:
        item = dict(raw)
        # Recover only a proven provider ID, never a caption/time guess.
        sent_row = None
        if item.get("from_me") and not item.get("deleted") and str(item.get("id", "")):
            with STORE.connection() as db:
                sent_row = db.execute(
                    "SELECT id, body, reply_to_key, media_mime, media_name FROM outbound_messages "
                    "WHERE chat_id=? AND provider_id=? AND status='sent' LIMIT 1",
                    (chat_id, str(item.get("id", ""))),
                ).fetchone()

        # 1.00.6.36: repair old poisoned bindings created by the former fuzzy
        # uncertain-media matcher. A plain text/reply must never inherit a
        # previous screenshot merely because it was sent in the same time window.
        if (
            item.get("from_me")
            and not item.get("deleted")
            and item.get("media_path")
            and sent_row
            and not _outbound_media_matches_observation(
                sent_row,
                str(item.get("body", "")),
                str(item.get("quoted_message_key", "")),
                str(item.get("message_type", item.get("type", ""))),
            )
        ):
            item["media_path"] = ""
            item["media_mime"] = ""
            item["media_name"] = ""
            try:
                with STORE.connection() as db:
                    db.execute(
                        "UPDATE whatsapp_chat_messages SET media_path='', media_mime='', media_name='' "
                        "WHERE chat_id=? AND message_key=?",
                        (chat_id, str(item.get("id", ""))),
                    )
                    db.commit()
            except Exception:
                log_suppressed("app.decorate_chat_messages")

        if item.get("from_me") and not item.get("deleted") and not item.get("media_path"):
            media = {}
            if sent_row and _outbound_media_matches_observation(
                sent_row,
                str(item.get("body", "")),
                str(item.get("quoted_message_key", "")),
                str(item.get("message_type", item.get("type", ""))),
            ):
                media = ATTACHMENTS.preserve(sent_row["id"])
            if not media:
                media = reconcile_uncertain_system_outgoing_media(
                    chat_id,
                    str(item.get("id", "")),
                    int(item.get("timestamp", item.get("message_timestamp", 0)) or 0),
                    str(item.get("body", "")),
                    str(item.get("message_type", item.get("type", ""))),
                    str(item.get("quoted_message_key", "")),
                )
            if not media:
                # QUEUE_3_3_71_CLIPBOARD_LOCAL_RECONCILE: repair already saved
                # [Фото] rows by matching the still-present clipboard upload to
                # this exact chat and WhatsApp timestamp.
                media = queue_uploads.recover_clipboard_attachment(
                    sys.modules[__name__],
                    chat_id,
                    str(item.get("id", "")),
                    int(item.get("timestamp", item.get("message_timestamp", 0)) or 0),
                    str(item.get("body", "")),
                    str(item.get("message_type", item.get("type", ""))),
                )
            if media:
                item.update(media)
                STORE.save_whatsapp_chat_messages(chat_id, [item])
        own_ids = {key for key, participant in participant_by_id.items() if participant.get("is_me")}
        item["highlight_mention"] = queue_features.mention_match(item, EMPLOYEES, own_ids)
        if str(chat_id).endswith("@g.us"):
            sender_id = str(item.get("sender_id", "")).strip()
            participant = participant_by_id.get(sender_id) if participant_by_id else None
            if participant:
                participant_name = str(participant.get("name", "")).strip()
                participant_phone = str(participant.get("phone", "")).strip()
                current_sender = str(item.get("sender", "")).strip()
                current_folded = current_sender.casefold()
                if participant_name and (
                    current_folded in generic_group_names
                    or re.fullmatch(r"[+\d\s().-]+", current_sender or "") is not None
                ):
                    item["sender"] = participant_name
                if participant_phone and not str(item.get("sender_phone", "")).strip():
                    item["sender_phone"] = participant_phone

            avatar_candidates: list[str] = []
            if participant:
                avatar_candidates.extend([
                    str(participant.get("resolved_id", "") or "").strip(),
                    str(participant.get("mention_id", "") or "").strip(),
                ])
            avatar_candidates.append(sender_id)
            item["sender_avatar_url"] = ""
            for avatar_id in avatar_candidates:
                if not avatar_id:
                    continue
                avatar_url = AVATARS.url(avatar_id)
                if avatar_url:
                    item["sender_avatar_url"] = avatar_url
                    break
        if mention_names and item.get("body"):
            body = str(item.get("body", ""))
            for technical_id, display_name in mention_names.items():
                body = re.sub(rf"@{re.escape(technical_id)}(?!\d)", f"@{display_name}", body)
            item["body"] = body
        if not item.get("from_me"):
            admin_id = str(item.get("sender_id", "")) if str(chat_id).endswith("@g.us") else chat_id
            item["sender"] = display_names().name(admin_id, item.get("sender", ""), item.get("sender_phone", ""))
        qid = str(item.get("quoted_message_key", ""))
        if qid:
            if qid not in quote_cache: quote_cache[qid] = STORE.resolve_whatsapp_message(chat_id, qid)
            original = quote_cache[qid]
            if not original or original.get("deleted"):
                item["quoted_body"] = "Сообщение недоступно"
                item["quote_unavailable"] = True
            else:
                media = bool(original.get("media_path") or original.get("media_mime") or original.get("message_type") in {"image", "video", "audio", "ptt", "document", "sticker"})
                item["quoted_body"] = queue_message_identity.media_label(original) if media else str(original.get("body", ""))[:1200]
                sender_id = str(original.get("sender_id", "")) if str(chat_id).endswith("@g.us") else chat_id
                item["quoted_sender"] = "Вы" if original.get("from_me") else display_names().name(sender_id, original.get("sender", ""), original.get("sender_phone", ""))
                item["quoted_message_key"] = original["message_key"]
                if str(original.get("media_mime", "")).startswith("image/") and original.get("media_path"):
                    item["quote_preview_url"] = f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(original['message_key'])}"
        message_id = str(item.get("id", ""))
        media_path = str(item.pop("media_path", "") or "")
        if media_path and message_id and not bool(item.get("deleted")):
            item["media_url"] = (
                f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(message_id)}"
            )
        else:
            item["media_url"] = ""
        result.append(item)
    return result


# 1.00.6.30: phase 4 large modularization (queue_work_context).
def load_employee_names():
    return queue_work_context.load_employee_names(STORE, DEFAULT_EMPLOYEES, MAX_EMPLOYEES)
cached_snapshot_value = queue_work_context.cached_snapshot_value
mark_whatsapp_read_throttled = queue_work_context.mark_whatsapp_read_throttled
active_employee = queue_work_context.active_employee
employee_identity = queue_work_context.employee_identity

def activate_employee_shift(user):
    result = queue_work_context.activate_employee_shift(user)
    user = user or {}
    employee = str((result or {}).get("employee", "") or "").strip()
    if str(user.get("role", "")).strip().lower() == "employee" and employee in EMPLOYEES:
        # The employee account that logs in or becomes active marks the current shift.
        STORE.set_setting("active_employee", employee)
    return result

work_actor = queue_work_context.work_actor
assignment_names = queue_work_context.assignment_names
audit_admin_form_submission = queue_work_context.audit_admin_form_submission
monitor_log_entries = queue_work_context.monitor_log_entries
system_status_snapshot = queue_work_context.system_status_snapshot
ticket_sla_state = queue_work_context.ticket_sla_state
sla_badge = queue_work_context.sla_badge
status_badge = queue_work_context.status_badge
priority_badge = queue_work_context.priority_badge
employee_options = queue_work_context.employee_options
category_options = queue_work_context.category_options
priority_options = queue_work_context.priority_options
inline_priority_select = queue_work_context.inline_priority_select
render_ticket_rows = queue_work_context.render_ticket_rows
query_page = queue_work_context.query_page
render_pagination = queue_work_context.render_pagination


EMPLOYEES = load_employee_names()
# LOCAL_TIMEZONE is provided by queue_core.
PAGE_SIZE = 25
CHAT_LOCK = threading.Lock()
CHAT_STATE: dict[str, object] = {
    "connected": False,
    "connector_status": "offline",
    "qr_data_url": "",
    "updated_at": "",
    "chats": [],
    "messages": {},
    "requested_chat_id": "",
    "requested_group_id": "",
    "group_participants": {},
    "discovered_contacts": [],
    "contact_profiles": {},
    "presence": {},
}
SNAPSHOT_CACHE_LOCK = threading.Lock()
SNAPSHOT_CACHE: dict[str, tuple[float, object]] = {}
READ_MARK_LOCK = threading.Lock()
READ_MARKED_AT: dict[str, float] = {}


# 1.00.6.29: chat state/snapshot logic moved out of app.py.
lock_owned_by_current_user = queue_chat_state.lock_owned_by_current_user
conversation_lock_snapshot = queue_chat_state.conversation_lock_snapshot
flexible_contact_match = queue_chat_state.flexible_contact_match
whatsapp_display_name = queue_chat_state.whatsapp_display_name
pending_context_category = queue_chat_state.pending_context_category
dedupe_whatsapp_messages = queue_chat_state.dedupe_whatsapp_messages
latest_deleted_chat_previews = queue_chat_state.latest_deleted_chat_previews
apply_deleted_preview_override = queue_chat_state.apply_deleted_preview_override
forward_targets_snapshot = queue_chat_state.forward_targets_snapshot
display_names = queue_chat_state.display_names
_group_preview_mention_map = queue_chat_state._group_preview_mention_map
_latest_group_preview_mentions = queue_chat_state._latest_group_preview_mentions
humanize_group_preview_mentions = queue_chat_state.humanize_group_preview_mentions
enrich_chat_list = queue_chat_state.enrich_chat_list
chat_state_snapshot = queue_chat_state.chat_state_snapshot
group_state_snapshot = queue_chat_state.group_state_snapshot
connector_state_snapshot = queue_chat_state.connector_state_snapshot
_decorate_notification_event = queue_chat_state._decorate_notification_event
notifications_snapshot = queue_chat_state.notifications_snapshot


# Ticket views retain their existing interface.
layout = queue_ticket_views.layout
render_dashboard = queue_ticket_views.render_dashboard
render_ticket = queue_ticket_views.render_ticket
render_my_page = queue_ticket_views.render_my_page




def render_login(query: dict[str, list[str]] | None = None, error: str = "") -> str:
    return queue_auth_views.render_login(
        query, error, auth=AUTH, local_mode=LOCAL_MODE, escape=e,
        session_hours=queue_auth.SESSION_HOURS,
    )


def render_register(error: str = "") -> str:
    return queue_auth_views.render_register(error, auth=AUTH, local_mode=LOCAL_MODE, escape=e)


def render_forgot_password(error: str = "", notice: str = "") -> str:
    return queue_auth_views.render_forgot_password(error, notice, local_mode=LOCAL_MODE, escape=e)


def render_local_setup(error: str = "") -> str:
    return queue_auth_views.render_local_setup(error, escape=e)


def render_account(query: dict[str, list[str]]) -> str:
    return queue_auth_views.render_account(query, user=queue_auth.current_user(), escape=e, csrf_token=csrf_token())


def render_admin_users(query: dict[str, list[str]]) -> str:
    return queue_admin_views.render_admin_users(
        query, auth=AUTH, employees=EMPLOYEES, csrf_token=csrf_token(),
        escape=e, human_time=human_time, admin_layout_func=admin_layout,
    )


def admin_tabs(active: str) -> str:
    return queue_admin_views.admin_tabs(active, escape=e)


def admin_layout(title: str, content: str, active: str) -> str:
    return queue_admin_views.admin_layout(title, content, active, layout_func=layout, escape=e)


# 1.00.6.30: phase 4 large modularization (queue_admin_pages).
render_admin_home = queue_admin_pages.render_admin_home
error_report_status_badge = queue_admin_pages.error_report_status_badge
render_admin_errors = queue_admin_pages.render_admin_errors
render_admin_error = queue_admin_pages.render_admin_error
render_admin_audit = queue_admin_pages.render_admin_audit
_analytics_filters = queue_admin_pages._analytics_filters
render_admin_analytics_final = queue_admin_pages.render_admin_analytics_final
render_admin_manager_dashboard = queue_admin_pages.render_admin_manager_dashboard
render_admin_api_keys = queue_admin_pages.render_admin_api_keys
_unified_audit_rows = queue_admin_pages._unified_audit_rows
render_admin_system = queue_admin_pages.render_admin_system
admin_employee_count = queue_admin_pages.admin_employee_count
render_admin_employees = queue_admin_pages.render_admin_employees
render_admin_manual = queue_admin_pages.render_admin_manual
render_admin_menu = queue_admin_pages.render_admin_menu
render_admin_categories = queue_admin_pages.render_admin_categories
avatar_html = queue_admin_pages.avatar_html
discovered_contact_rows = queue_admin_pages.discovered_contact_rows
render_admin_contacts = queue_admin_pages.render_admin_contacts


ERROR_REPORT_STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "resolved": "Исправлена",
    "rejected": "Отклонена",
}




# ---------------------------------------------------------------------------
# 1.00.6.12: language selection before the ordinary-user WhatsApp flow.
# The selected language is persisted per WhatsApp user in app_settings.
# Admin/employee UI and stored ticket data stay Russian.
# ---------------------------------------------------------------------------

CONTACTS = ContactProfiles(STORE)
_language_identities = CONTACTS._language_identities
_language_identity = CONTACTS._language_identity
_language_setting_key = CONTACTS._language_setting_key
get_user_language = CONTACTS.get_user_language
set_user_language = CONTACTS.set_user_language
clear_user_language = CONTACTS.clear_user_language
get_contact_language = CONTACTS.get_contact_language
set_contact_language = CONTACTS.set_contact_language
clear_contact_language = CONTACTS.clear_contact_language
_profile_setting_key = CONTACTS._profile_setting_key
_clean_user_profile_value = CONTACTS._clean_user_profile_value
get_user_profile = CONTACTS.get_user_profile
set_user_profile = CONTACTS.set_user_profile
get_contact_profile = CONTACTS.get_contact_profile
set_contact_profile = CONTACTS.set_contact_profile
profile_prompt_text = CONTACTS.profile_prompt_text
parse_contact_profile = CONTACTS.parse_contact_profile
apply_contact_profile_to_ticket = CONTACTS.apply_contact_profile_to_ticket


# This public branch no longer contains the specialized legacy views/assets.
if os.getenv("QUEUE_PROFILE", "generic").strip().lower() != "generic":
    raise RuntimeError("The public source branch supports only QUEUE_PROFILE=generic")

MENU_CONFIG = MenuConfig(STORE, lambda: queue_generic_profile.read_profile(ROOT), CATEGORIES)
MENU = BotMenu(MENU_CONFIG)


def apply_bot_profile(profile):
    """Explicit compatibility entry point for profile updates."""
    MENU_CONFIG.replace(profile, MENU_CONFIG.archive)

configured_menu_category = MENU.configured_menu_category
user_menu_label = MENU.user_menu_label
menu_payload = MENU.menu_payload
main_menu_text = MENU.main_menu_text
start_menu_prompt = MENU.start_menu_prompt
configured_category_prompt = MENU.configured_category_prompt
configured_category_required_fields = MENU.configured_category_required_fields
active_ticket_category_items = MENU.active_ticket_category_items
enabled_menu_options = MENU.enabled_menu_options
configured_category_action = MENU.configured_category_action

TICKET_SERVICE = queue_ticket_service.TicketService(queue_ticket_service.TicketDependencies(
    store=STORE, statuses=STATUSES, categories=CATEGORIES, menu_context=MENU_CONTEXT,
    close_reason_labels_kz=USER_CLOSE_REASON_LABELS_KZ,
    contact_language=CONTACTS.get_contact_language,
    auto_reply_enabled=global_auto_reply_enabled,
    main_menu=MENU.main_menu_text,
    menu_label=MENU.user_menu_label,
    status_label=user_status_label,
))
ticket_accepted_reply = TICKET_SERVICE.ticket_accepted_reply
active_tickets_reply = TICKET_SERVICE.active_tickets_reply
active_ticket_selected_reply = TICKET_SERVICE.active_ticket_selected_reply
support_question_accepted_reply = TICKET_SERVICE.support_question_accepted_reply
ticket_status_reply_text = TICKET_SERVICE.status_reply_text
update_ticket_status = TICKET_SERVICE.update_status

# Configure explicit work dependencies after application services are ready.
queue_work_context.configure(queue_work_context.WorkDependencies(
    store=STORE,
    auth=AUTH,
    employees=EMPLOYEES,
    max_employees=MAX_EMPLOYEES,
    categories=CATEGORIES,
    statuses=STATUSES,
    priorities=PRIORITIES,
    sla_minutes=SLA_MINUTES,
    data_dir=DATA_DIR,
    database_path=DATABASE_PATH,
    media_dir=MEDIA_DIR,
    outbound_media_dir=OUTBOUND_MEDIA_DIR,
    monitor_log_path=MONITOR_LOG_PATH,
    monitor_heartbeat_path=MONITOR_HEARTBEAT_PATH,
    local_mode=LOCAL_MODE,
    snapshot_cache=SNAPSHOT_CACHE,
    snapshot_cache_lock=SNAPSHOT_CACHE_LOCK,
    read_marked_at=READ_MARKED_AT,
    read_mark_lock=READ_MARK_LOCK,
    category_items=active_ticket_category_items,
    connector_snapshot=connector_state_snapshot,
))


INBOUND_SERVICE = InboundService(InboundDependencies(
    store=STORE, menu=MENU, contacts=CONTACTS, tickets=TICKET_SERVICE,
    media_dir=MEDIA_DIR, save_media=save_media_payload,
    active_employee=active_employee, image_lookback_seconds=PRE_TICKET_IMAGE_LOOKBACK_SECONDS,
    auto_reply_enabled=global_auto_reply_enabled,
))


ATTACHMENTS = AttachmentArchive(STORE, MEDIA_DIR, OUTBOUND_MEDIA_DIR, UPLOADS.root)
OUTBOUND_SERVICE = OutboundService(STORE, ATTACHMENTS, queue_realtime, global_auto_reply_enabled)
STALE_MENU = StaleMenuRecovery(STORE, MENU, CONTACTS, queue_realtime, global_auto_reply_enabled)
RELIABILITY = queue_reliability.ReliabilityMonitor(STORE, DATA_DIR, DATABASE_PATH, ROOT, connector_state_snapshot,
                                                   ticket_sla_state, MAX_MEDIA_BYTES)
PRODUCTIVITY_ROUTES = ProductivityRoutes(STORE, DATA_DIR, DATABASE_PATH, csrf_token=csrf_token, layout=layout,
                                         admin_layout=admin_layout, category_items=active_ticket_category_items,
                                         active_employee=active_employee)
WORKFLOW_ROUTES = WorkflowRoutes(STORE, csrf_token=csrf_token, layout=layout, admin_layout=admin_layout,
                                 category_items=active_ticket_category_items, categories=CATEGORIES,
                                 employees=EMPLOYEES, work_actor=work_actor,
                                 update_ticket_status=update_ticket_status, events=queue_realtime)
RELIABILITY_ROUTES = ReliabilityRoutes(STORE, RELIABILITY, csrf_token=csrf_token, admin_layout=admin_layout,
                                       max_media_bytes=MAX_MEDIA_BYTES, active_employee=active_employee,
                                       chat_lock=CHAT_LOCK, chat_state=CHAT_STATE, avatars=AVATARS)

# Connector/chat handlers (queue_whatsapp_handlers) and core HTTP routing
# (queue_http_handler) live in dedicated mixins.
class TicketHandler(queue_whatsapp_handlers.WhatsAppHandlerMixin, queue_http_handler.QueueHTTPHandlerMixin, BaseHTTPRequestHandler):
    inbound_service = INBOUND_SERVICE
    outbound_service = OUTBOUND_SERVICE
    productivity_routes = PRODUCTIVITY_ROUTES
    workflow_routes = WORKFLOW_ROUTES
    reliability_routes = RELIABILITY_ROUTES
    server_version = "UnifiedQueue/1.00.1"

    def _generic_domain_path(self):
        if os.getenv("QUEUE_PROFILE", "generic").strip().lower() != "generic":
            return False
        path = urlparse(self.path).path.lower()
        return path.startswith(('/приложения', '/api/приложения', '/admin/приложения', '/api/внешнего сервиса', '/admin/внешнего сервиса', '/static/приложения-'))

    def do_GET(self):
        if self._generic_domain_path():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        return super().do_GET()

    def do_POST(self):
        if self._generic_domain_path():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        return super().do_POST()


def main() -> None:
    # stderr ends up in journald under systemd; QUEUE_LOG_LEVEL=INFO/DEBUG for more detail.
    logging.basicConfig(
        level=os.getenv("QUEUE_LOG_LEVEL", "WARNING").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cleanup_retention_once()
    threading.Thread(target=retention_worker, name="queue-retention", daemon=True).start()
    threading.Thread(target=scheduled_backup_worker, name="queue-scheduled-backup", daemon=True).start()
    threading.Thread(target=RELIABILITY.worker_loop, name="queue-reliability", daemon=True).start()
    threading.Thread(target=queue_performance.worker_loop, args=(DATA_DIR, DATABASE_PATH), name="queue-performance", daemon=True).start()
    threading.Thread(target=STALE_MENU.worker_loop, name="queue-stale-menu", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), TicketHandler)
    address = f"http://127.0.0.1:{PORT}"
    print(f"Единая очередь запущена: {address}")
    print(f"База заявок: {DATABASE_PATH}")
    print("Для остановки нажмите Ctrl+C")
    if os.getenv("OPEN_BROWSER") == "1":
        browser_address = os.getenv("QUEUE_BROWSER_URL", address)
        threading.Timer(0.4, lambda: webbrowser.open(browser_address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
