from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import sqlite3
import secrets
import shutil
import threading
import time
import webbrowser
import sys
import queue_inbound_media
import queue_names
import queue_message_identity
import queue_features
import queue_feature_http
import queue_productivity
import queue_productivity_http
import queue_workflow
import queue_workflow_http
import queue_reliability
import queue_reliability_http
import queue_performance
import queue_performance_http
import queue_uploads
import queue_avatars
from queue_language import parse_menu_number
from transcription import transcribe_audio_payload
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from ticketing import (
    CATEGORIES,
    MENU_CONTEXT,
    MENU_GATE_CONTEXT,
    PRIORITIES,
    STATUSES,
    TicketStore,
    category_prompt,
    build_missing_request_reply,
    apply_expected_request_answer,
    extract_request_draft,
    merge_request_draft,
    missing_request_fields,
    request_draft_text,
    is_acknowledgement,
    is_main_menu_command,
    is_support_trigger_message,
    normalize_message,
    process_incoming_message,
    request_detail_error_reply,
    request_detail_issues,
)


QUEUE_3_3_111_CHAT_LAYOUT_OVERFLOW_FIX = True
ROOT = Path(__file__).resolve().parent


def resolve_data_dir(
    environment: dict[str, str] | None = None,
    platform_name: str | None = None,
    project_root: Path | None = None,
) -> Path:
    environment = environment if environment is not None else os.environ
    platform_name = platform_name if platform_name is not None else os.name
    project_root = project_root if project_root is not None else ROOT
    configured = str(environment.get("QUEUE_DATA_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = str(environment.get("LOCALAPPDATA", "")).strip()
    if platform_name == "nt" and local_app_data:
        return Path(local_app_data) / "QueueLocal"
    if platform_name != "nt":
        xdg_data_home = str(environment.get("XDG_DATA_HOME", "")).strip()
        base = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
        return base / "queue-local"
    return project_root / "data"


def prepare_database_path(data_dir: Path, project_root: Path | None = None) -> Path:
    project_root = project_root if project_root is not None else ROOT
    database_path = data_dir / "tickets.db"
    legacy_path = project_root / "data" / "tickets.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    if database_path.absolute() != legacy_path.absolute():
        if not database_path.exists() and legacy_path.is_file():
            shutil.copy2(legacy_path, database_path)
    return database_path


DATA_DIR = resolve_data_dir()
DATABASE_PATH = prepare_database_path(DATA_DIR)
STORE = TicketStore(DATABASE_PATH)
queue_productivity.initialize(STORE)
queue_workflow.initialize(STORE)  # QUEUE_3_3_96_WORKFLOW
queue_reliability.initialize(STORE)  # QUEUE_3_3_99_RELIABILITY
MEDIA_DIR = DATA_DIR / "chat-media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
OUTBOUND_MEDIA_DIR = DATA_DIR / "outbound-media"
OUTBOUND_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MAX_MEDIA_BYTES = max(1, min(256, int(os.getenv("QUEUE_MAX_MEDIA_MB", "12")))) * 1024 * 1024
UPLOADS = queue_uploads.Uploads(OUTBOUND_MEDIA_DIR / "uploads", STORE)
VOICE_JOBS = queue_features.VoiceJobs(STORE, MEDIA_DIR, transcribe_audio_payload)
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
INSTRUCTION_URL = "/instruction"
INSTRUCTION_SOURCE_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1iIuhFXY35wlWkexv4hr061IfImIoyynRVvyCm12v7x4/edit?gid=0#gid=0"
)
INSTRUCTION_DATA_PATH = ROOT / "instruction_data.json"
DEFAULT_EMPLOYEES = [f"Сотрудник {number}" for number in range(1, 6)]
ADMIN_USER = os.getenv("QUEUE_ADMIN_USER", "queueadmin").strip().casefold()
ADMIN_FORM_TOKEN = secrets.token_urlsafe(32)
ADMIN_SESSION_TOKEN = secrets.token_urlsafe(48)
ADMIN_SESSION_COOKIE = "queue_admin_session"
ADMIN_SESSION_MAX_AGE = 12 * 60 * 60
MAX_EMPLOYEES = 50
MAX_MENU_OPTIONS = 30
MENU_ACTION_TICKET = "ticket"
MENU_ACTION_INSTRUCTION = "instruction"
MENU_ACTIONS = {MENU_ACTION_TICKET, MENU_ACTION_INSTRUCTION}
SUPPORT_MODE_CONTEXT = "__support_mode__"
ERROR_REPORT_CONTEXT = "__error_report__"
ACTIVE_TICKETS_CONTEXT = "__active_tickets__"
ACTIVE_TICKET_FOLLOWUP_CONTEXT = "__active_ticket_followup__"
REACTION_EMOJIS = {"👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "🎉", "✅", "👏", "🤔", "👀", "😡", "💯", "👌", "😁", "😍", "🥳"}
DEFAULT_MENU_KEYS = [
    "seal",
    "transport",
    "bin",
    "keden",
    "package",
    "database",
    "mobile",
    "general",
    "support",
]
DEFAULT_MENU_LABELS = {
    "seal": "Навигационная пломба / НП",
    "transport": "Перевозка",
    "bin": "Корректировка БИН",
    "keden": "Keden",
    "package": "Пакеты",
    "database": "База данных",
    "mobile": "Мобилка",
    "general": "Другая проблема",
    "support": "Задать вопрос в поддержку",
}

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
        chat_id = valid_conversation_id(str(value or ""))
        if not chat_id:
            continue
        if not chat_id.endswith("@g.us"):
            chat_id = STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        result.add(chat_id)
    return result


def set_chat_favorite(chat_id: str, favorite: bool) -> tuple[bool, str]:
    clean = valid_conversation_id(chat_id)
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


def load_category_archive() -> dict[str, str]:
    """Labels of removed custom categories, kept so old tickets stay readable."""
    raw = STORE.get_setting("request_category_archive_json", "")
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(values, dict):
        return {}
    cleaned: dict[str, str] = {}
    for raw_key, raw_label in values.items():
        key = re.sub(r"[^a-z0-9_]", "", str(raw_key))[:40]
        label = normalize_message(str(raw_label))[:80]
        if key and label:
            cleaned[key] = label
    return cleaned


CATEGORY_ARCHIVE = load_category_archive()


def load_menu_options() -> list[dict[str, object]]:
    raw = STORE.get_setting("request_menu_json", "")
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list):
                cleaned: list[dict[str, object]] = []
                seen: set[str] = set()
                for item in values[:MAX_MENU_OPTIONS]:
                    if not isinstance(item, dict):
                        continue
                    key = re.sub(r"[^a-z0-9_]", "", str(item.get("key", "")))[:40]
                    label = normalize_message(str(item.get("label", "")))[:80]
                    prompt = normalize_message(str(item.get("prompt", "")))[:1200]
                    action = str(item.get("action", MENU_ACTION_TICKET))
                    if action not in MENU_ACTIONS:
                        action = MENU_ACTION_TICKET
                    if not key or key in seen or not label:
                        continue
                    seen.add(key)
                    cleaned.append(
                        {
                            "key": key,
                            "label": label,
                            "prompt": prompt,
                            "enabled": bool(item.get("enabled", True)),
                            "action": action,
                        }
                    )
                if cleaned and any(bool(item["enabled"]) for item in cleaned):
                    return cleaned
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return [
        {
            "key": key,
            "label": DEFAULT_MENU_LABELS[key],
            "prompt": category_prompt(key),
            "enabled": True,
            "action": MENU_ACTION_TICKET,
        }
        for key in DEFAULT_MENU_KEYS
    ]


MENU_OPTIONS = load_menu_options()


def apply_menu_labels() -> None:
    # Removed custom categories stay in CATEGORIES only for historical tickets.
    for key, label in CATEGORY_ARCHIVE.items():
        CATEGORIES[str(key)] = str(label)
    for option in MENU_OPTIONS:
        CATEGORIES[str(option["key"])] = str(option["label"])


apply_menu_labels()


def enabled_menu_options() -> list[dict[str, object]]:
    return [item for item in MENU_OPTIONS if bool(item.get("enabled"))]


def numbered_enabled_menu_options() -> list[tuple[int, dict[str, object]]]:
    """Visible WhatsApp menu options with dynamic consecutive numbers.

    Numbers are intentionally never stored in settings. If an administrator hides
    or removes a category, all remaining visible items are numbered again from 1
    without gaps, and incoming numeric choices are resolved against this same list.
    """
    return list(enumerate(enabled_menu_options(), start=1))


def active_ticket_category_items() -> list[tuple[str, str]]:
    """Categories currently available for new tickets.

    Historical/removed categories remain in CATEGORIES for display and filtering,
    but do not appear in forms used to create a new ticket. Internal categories
    that are not represented in the WhatsApp menu keep their previous behavior.
    """
    menu_by_key = {str(item.get("key", "")): item for item in MENU_OPTIONS}
    rows: list[tuple[str, str]] = []
    for key, label in CATEGORIES.items():
        if key in CATEGORY_ARCHIVE:
            continue
        option = menu_by_key.get(key)
        if option is not None:
            if str(option.get("action", MENU_ACTION_TICKET)) != MENU_ACTION_TICKET:
                continue
            if not bool(option.get("enabled")):
                continue
            label = str(option.get("label", label))
        rows.append((str(key), str(label)))
    return rows


def error_report_menu_number() -> int:
    # Служебный пункт с репортами идёт после пользовательских категорий.
    return len(enabled_menu_options()) + 1


def active_tickets_menu_number() -> int:
    # Активные заявки идут после отдельного пункта «Репорт об ошибке».
    return len(enabled_menu_options()) + 2


def menu_payload() -> list[dict[str, str]]:
    rows = [
        {"number": str(index), "label": str(item["label"])}
        for index, item in numbered_enabled_menu_options()
    ]
    rows.append({"number": str(error_report_menu_number()), "label": "Репорт об ошибке"})
    rows.append({"number": str(active_tickets_menu_number()), "label": "Мои активные заявки"})
    return rows


def is_error_report_command(text: str) -> bool:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    return normalized in {
        "репорт об ошибке", "репорт ошибки", "сообщить об ошибке",
        "сообщить ошибку", "ошибка в системе", "баг", "report bug",
    }


def error_report_prompt() -> str:
    return (
        "Выбрана тема: Репорт об ошибке.\n\n"
        "Опишите ошибку обычным текстом: что вы делали и что произошло. "
        "Шаблон и обязательные поля не нужны.\n"
        "Можно приложить скриншот, фото, видео или файл.\n\n"
        "Репорт сохранится отдельно от заявок и будет доступен сотрудникам в разделе «Ошибки».\n"
        "Чтобы вернуться в главное меню, отправьте 0."
    )


def is_active_tickets_command(text: str) -> bool:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    return normalized in {
        "мои заявки", "мои активные заявки", "активные заявки",
        "показать заявки", "покажи заявки", "статус заявок",
    }


def configured_menu_category(choice: str) -> str:
    normalized = normalize_message(choice).casefold().strip(" .,!?:;-")
    match = re.fullmatch(r"(?:пункт\s*)?(\d{1,2})", normalized)
    if not match:
        return ""
    requested_number = int(match.group(1))
    for number, option in numbered_enabled_menu_options():
        if number == requested_number:
            return str(option["key"])
    return ""


OPTIONAL_PHOTO_NOTE = (
    "Фото или скриншот можно прикрепить к следующему сообщению, если это поможет. "
    "Это необязательно."
)


def configured_category_prompt(category: str) -> str:
    # «Вопрос в поддержку» — это свободный вопрос/ответ, а не заявка по шаблону.
    # Никакие обязательные поля, примеры заполнения или повторный разбор по БИН/НП
    # здесь не показываем.
    if category == "support":
        return (
            "Выбрана тема: Вопрос в поддержку.\n\n"
            "Напишите свой вопрос обычным текстом. Шаблон и специальные поля не нужны.\n"
            "При необходимости можно приложить фото, скриншот или файл.\n\n"
            "После отправки вопрос появится у сотрудников, и ответ придёт в этот же WhatsApp-чат.\n"
            "Чтобы вернуться в главное меню, отправьте 0."
        )
    option = next((item for item in MENU_OPTIONS if item["key"] == category), None)
    if option and str(option.get("prompt", "")).strip():
        prompt = str(option["prompt"]).strip()
        if re.search(r"(?:скопируйте.*шаблон|не удаляйте названия полей|одним сообщением|шаблон для заполнения)", prompt, re.IGNORECASE | re.DOTALL):
            prompt = category_prompt(category).strip()
    else:
        prompt = category_prompt(category).strip()
    if (
        configured_category_action(category) == MENU_ACTION_TICKET
        and not re.search(r"Фото\s+или\s+скриншот", prompt, re.IGNORECASE)
    ):
        prompt = f"{prompt}\n\n{OPTIONAL_PHOTO_NOTE}"
    return prompt


def configured_category_action(category: str) -> str:
    option = next((item for item in MENU_OPTIONS if item["key"] == category), None)
    action = str(option.get("action", MENU_ACTION_TICKET)) if option else MENU_ACTION_TICKET
    return action if action in MENU_ACTIONS else MENU_ACTION_TICKET


def start_menu_prompt() -> str:
    return (
        "Это автоматическая система регистрации заявок.\n\n"
        "Чтобы открыть меню заявок и выбрать нужную проблему, отправьте цифру 1.\n\n"
        "Затем выберите тему номером. Я подскажу, какие данные нужны, и помогу заполнить заявку."
    )


def start_menu_reminder() -> str:
    return (
        "Помогу оформить обращение. Сначала нужно выбрать тему.\n\n"
        "Сначала отправьте цифру 1, чтобы открыть меню заявок. "
        "После этого выберите номер нужной категории."
    )


def main_menu_text(error: bool = False) -> str:
    category_rows = [
        f"{index}. {item['label']}"
        for index, item in numbered_enabled_menu_options()
    ]
    category_rows.append(f"{error_report_menu_number()}. Репорт об ошибке")
    category_rows.append(f"{active_tickets_menu_number()}. Мои активные заявки")
    rows = "\n".join(category_rows)
    intro = "Меню обращений\n\nВыберите, с какой проблемой вы обращаетесь.\n\n"
    if error:
        intro = (
            "Это автоматическая система регистрации заявок.\n"
            "Нужно выбрать номер заявки из списка.\n\n"
        )
    return (
        f"{intro}{rows}\n\n"
        "Отправьте только номер нужного пункта, например 1, 2 или 3.\n"
        "После выбора система подскажет, какие данные нужны. Строгий шаблон не требуется.\n\n"
        "Чтобы вернуться к этому меню позже, отправьте 0 или слово «меню»."
    )


def menu_selection_reminder() -> str:
    return (
        "Пока не удалось определить выбранный пункт меню.\n\n"
        "Отправьте 0 или слово «меню», затем номер подходящей темы. "
        "После выбора можно описать проблему своими словами. Если тема не подходит, выберите поддержку."
    )


def media_extension(mimetype: str, filename: str = "") -> str:
    mime = (mimetype or "").split(";", 1)[0].strip().casefold()
    mapping = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "audio/ogg": ".ogg", "audio/opus": ".opus",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }
    if mime in mapping:
        return mapping[mime]
    suffix = Path(filename or "").suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return ".bin"


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



# QUEUE_3_3_64_SYSTEM_MEDIA_HISTORY
# Recover the preserved local attachment for a message that was sent from the
# Queue UI.  whatsapp-web.js can report the same outgoing message a moment later
# as text/caption only, even though WhatsApp itself received the image/file.
def preserved_system_outgoing_media(message_key: str) -> dict[str, str]:
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
            return False

    candidates = []
    try:
        with STORE.connection() as connection:
            exact = connection.execute(
                "SELECT id, provider_id, media_mime, media_name FROM outbound_messages "
                "WHERE status='sent' AND provider_id=? ORDER BY id DESC LIMIT 1",
                (safe_key,),
            ).fetchone()
            if exact:
                candidates.append(exact)
            else:
                # Small bounded fallback for provider-id formatting differences.
                candidates.extend(connection.execute(
                    "SELECT id, provider_id, media_mime, media_name FROM outbound_messages "
                    "WHERE status='sent' AND provider_id<>'' ORDER BY id DESC LIMIT 80"
                ).fetchall())
    except Exception:
        return {}

    for row in candidates:
        if not same_provider_id(row["provider_id"], safe_key):
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
            mention_names = {}
            participant_by_id = {}

    manual_names = {row["chat_id"]: row["name"] for row in cached_snapshot_value("manual_contacts", 2.5, STORE.list_manual_whatsapp_contacts)}
    quote_cache = {}
    result: list[dict[str, object]] = []
    generic_group_names = {"", "участник", "участник группы", "пользователь whatsapp"}
    for raw in messages:
        item = dict(raw)
        # Recover only a proven provider ID, never a caption/time guess.
        if item.get("from_me") and not item.get("deleted") and not item.get("media_path"):
            with STORE.connection() as db:
                sent_row = db.execute("SELECT id FROM outbound_messages WHERE chat_id=? AND provider_id=? AND status='sent' LIMIT 1", (chat_id, str(item.get("id", "")))).fetchone()
            media = queue_uploads.preserve_sent_attachment(sys.modules[__name__], sent_row["id"]) if sent_row else {}
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
                avatar_url = queue_avatars.url(sys.modules[__name__], avatar_id)
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


def load_employee_names() -> list[str]:
    raw = STORE.get_setting("employees_json", "")
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list) and 1 <= len(values) <= MAX_EMPLOYEES:
                cleaned = [str(item).strip()[:60] for item in values]
                if all(cleaned) and len({item.casefold() for item in cleaned}) == len(cleaned):
                    return cleaned
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return list(DEFAULT_EMPLOYEES)


EMPLOYEES = load_employee_names()
# Windows may not include the IANA time-zone database used by zoneinfo.
# Almaty uses UTC+5, so a fixed offset keeps the prototype dependency-free.
ALMATY_TIMEZONE = timezone(timedelta(hours=5), name="Asia/Almaty")
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


def cached_snapshot_value(key: str, ttl_seconds: float, loader):
    now = time.monotonic()
    with SNAPSHOT_CACHE_LOCK:
        cached = SNAPSHOT_CACHE.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
    value = loader()
    with SNAPSHOT_CACHE_LOCK:
        SNAPSHOT_CACHE[key] = (now, value)
    return value


def mark_whatsapp_read_throttled(chat_id: str, interval_seconds: float = 2.0) -> None:
    clean = str(chat_id or "").strip()
    if not clean:
        return
    now = time.monotonic()
    with READ_MARK_LOCK:
        previous = READ_MARKED_AT.get(clean, 0.0)
        if now - previous < interval_seconds:
            return
        READ_MARKED_AT[clean] = now
        if len(READ_MARKED_AT) > 500:
            cutoff = now - 300.0
            for key, marked_at in list(READ_MARKED_AT.items()):
                if marked_at < cutoff:
                    READ_MARKED_AT.pop(key, None)
    STORE.mark_whatsapp_chat_read(clean)


def active_employee() -> str:
    employee = STORE.get_setting("active_employee", EMPLOYEES[0])
    return employee if employee in EMPLOYEES else EMPLOYEES[0]


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return f"+{digits}" if 8 <= len(digits) <= 15 else ""


def display_phone(phone: str) -> str:
    normalized = normalize_phone(phone)
    digits = normalized.lstrip("+")
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    if len(digits) == 12 and digits.startswith("998"):
        return f"+998 ({digits[3:5]}) {digits[5:8]}-{digits[8:10]}-{digits[10:12]}"
    return normalized or "Номер не определён"


def normalized_phone_digits(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def flexible_contact_match(query: str, name: str, phone: str, chat_id: str = "") -> bool:
    clean = normalize_message(query).casefold()
    if not clean:
        return True
    haystack = normalize_message(f"{name} {phone} {chat_id}").casefold()
    phone_digits = normalized_phone_digits(phone or chat_id)
    query_digits = normalized_phone_digits(clean)
    if len(query_digits) >= 5 and re.fullmatch(r"[+\d\s().-]+", clean):
        return query_digits in phone_digits
    for token in [item for item in re.split(r"\s+", clean) if item]:
        digits = normalized_phone_digits(token)
        if digits and re.search(r"\d", token):
            if digits not in phone_digits:
                return False
            continue
        if token not in haystack:
            return False
    return True


def whatsapp_display_name(value: object) -> str:
    """Return only a human WhatsApp display name, never our sender label or a phone."""
    name = normalize_message(str(value or ""))[:100].strip()
    if not name:
        return ""
    if name.casefold() in {"система", "system", "рабочий whatsapp"}:
        return ""
    if re.fullmatch(r"[+\d\s().-]+", name):
        return ""
    return name


def e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def human_time(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ALMATY_TIMEZONE).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def epoch_time(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return 0


def valid_chat_id(value: str) -> str:
    chat_id = str(value or "").strip()[:120]
    if re.fullmatch(r"[A-Za-z0-9_.:-]+@(c\.us|lid)", chat_id):
        return chat_id
    return ""


def valid_group_id(value: str) -> str:
    chat_id = str(value or "").strip()[:120]
    return chat_id if re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", chat_id) else ""


def valid_conversation_id(value: str) -> str:
    return valid_chat_id(value) or valid_group_id(value)


def content_disposition_header(disposition: str, filename: str) -> str:
    """Build an ASCII-safe Content-Disposition header for Unicode filenames.

    BaseHTTPRequestHandler serializes response headers as latin-1. Sending a
    Cyrillic filename directly raises UnicodeEncodeError, closes the upstream
    connection and nginx reports 502. Keep a conservative ASCII fallback and
    add the RFC 5987 UTF-8 filename for modern browsers.
    """
    mode = "attachment" if str(disposition).lower() == "attachment" else "inline"
    raw = str(filename or "file").replace("\r", " ").replace("\n", " ").strip()[:180]
    if not raw:
        raw = "file"
    path_name = Path(raw)
    suffix = path_name.suffix if path_name.suffix.isascii() else ""
    suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:20]
    ascii_stem = path_name.stem.encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_stem).strip(" ._")
    ascii_base = f"{ascii_stem or 'file'}{suffix}"[:120].replace('\"', '_')
    encoded = quote(raw, safe="")
    return f"{mode}; filename=\"{ascii_base}\"; filename*=UTF-8''{encoded}"


def pending_context_category(context: dict[str, object] | None) -> str:
    if not context or context.get("pending_category") not in CATEGORIES:
        return ""
    try:
        updated = datetime.fromisoformat(str(context["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, KeyError):
        return ""
    if datetime.now(timezone.utc) - updated > timedelta(hours=24):
        return ""
    return str(context["pending_category"])




def dedupe_whatsapp_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge repeated observations of the exact provider ID, never identical text."""
    result: dict[str, dict[str, object]] = {}
    for index, item in enumerate(messages):
        message_key = str(item.get("id", ""))
        if not message_key: continue
        direction, stanza = queue_message_identity.parts(message_key)
        key = (direction or str(bool(item.get("from_me"))).lower(), stanza)
        previous = result.get(key, {})
        merged = {**previous, **item}
        for field in ("media_path", "media_url", "media_mime", "media_name", "transcript",
                      "mentions", "quoted_message_key", "quoted_body", "quoted_sender", "sender_phone", "sender_id"):
            if not merged.get(field) and previous.get(field):
                merged[field] = previous[field]
        if previous.get("id"): merged["id"] = previous["id"]
        merged["ack"] = max(int(previous.get("ack", 0) or 0), int(item.get("ack", 0) or 0))
        if previous.get("edited") and not item.get("edited"):
            merged["body"] = previous.get("body", "")
        for flag in ("edited", "deleted", "forwarded"):
            merged[flag] = bool(previous.get(flag) or item.get(flag))
        if merged["deleted"]:
            merged.update(body="", media_path="", media_url="")
        result[key] = merged
    return list(result.values())

# QUEUE_1_00_1_DELETE_PREVIEW_SYNC
def latest_deleted_chat_previews() -> dict[str, dict[str, object]]:
    """Return chats whose newest persisted message is a deletion tombstone.

    whatsapp_chats/live WhatsApp may briefly keep the old preview text after
    message_revoke_everyone. Message history is authoritative, so the sidebar
    preview is corrected from the newest saved message without deleting history.
    """
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=5.0) as connection:
            rows = connection.execute(
                """
                SELECT m.chat_id, m.message_timestamp, m.from_me
                FROM whatsapp_chat_messages AS m
                WHERE m.deleted=1
                  AND m.id=(
                    SELECT newer.id
                    FROM whatsapp_chat_messages AS newer
                    WHERE newer.chat_id=m.chat_id
                    ORDER BY newer.message_timestamp DESC, newer.id DESC
                    LIMIT 1
                  )
                """
            ).fetchall()
    except Exception as exc:
        print(f"WhatsApp deleted-preview sync warning: {exc}", flush=True)
        return {}
    result: dict[str, dict[str, object]] = {}
    for chat_id, timestamp, from_me in rows:
        safe_id = valid_conversation_id(str(chat_id or ""))
        if not safe_id:
            continue
        try:
            ts = max(0, int(timestamp or 0))
        except (TypeError, ValueError):
            ts = 0
        result[safe_id] = {
            "timestamp": ts,
            "last_from_me": bool(from_me),
        }
    return result


def apply_deleted_preview_override(chats: list[dict[str, object]]) -> list[dict[str, object]]:
    tombstones = latest_deleted_chat_previews()
    if not tombstones:
        return chats
    for chat in chats:
        chat_id = valid_conversation_id(str(chat.get("id", "")))
        if not chat_id:
            continue
        canonical = STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        tombstone = tombstones.get(canonical) or tombstones.get(chat_id)
        if not tombstone:
            continue
        try:
            current_ts = max(0, int(chat.get("timestamp", 0) or 0))
        except (TypeError, ValueError):
            current_ts = 0
        deleted_ts = int(tombstone.get("timestamp", 0) or 0)
        # A newer live message must always win over an older deletion.
        if deleted_ts < current_ts:
            continue
        chat["last_message"] = "Сообщение удалено"
        chat["timestamp"] = deleted_ts
        chat["last_from_me"] = bool(tombstone.get("last_from_me"))
    return chats


def forward_targets_snapshot() -> list[dict[str, str]]:
    def load() -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        seen: set[str] = set()
        for chat in STORE.list_saved_whatsapp_chats(120):
            chat_id = valid_chat_id(str(chat.get("id", "")))
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                targets.append({"id": chat_id, "name": whatsapp_display_name(chat.get("name", "")) or "Пользователь WhatsApp"})
        for group in STORE.list_whatsapp_groups():
            chat_id = valid_group_id(str(group.get("chat_id", "")))
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                targets.append({"id": chat_id, "name": str(group.get("name", "") or "Группа WhatsApp")})
        return targets[:200]
    return list(cached_snapshot_value("forward_targets", 2.5, load))


def display_names():
    def load():
        manual = STORE.list_manual_whatsapp_contacts()
        with CHAT_LOCK:
            records = [dict(x) for x in CHAT_STATE.get("chats", []) if isinstance(x, dict)]
            records += [dict(x, id=k) for k,x in CHAT_STATE.get("profiles", {}).items() if isinstance(x, dict)]
            for participants in CHAT_STATE.get("group_participants", {}).values():
                records.extend(dict(x) for x in participants if isinstance(x, dict))
        return queue_names.Names(manual, records)
    return cached_snapshot_value("display_names_356", 1.0, load)

def enrich_chat_list(chats):
    activity = cached_snapshot_value("chat_activity_356", 1.0, STORE.chat_activity)
    manual = {row["chat_id"]: row["name"] for row in cached_snapshot_value("manual_contacts", 2.5, STORE.list_manual_whatsapp_contacts)}
    for chat in chats:
        cid = str(chat.get("id", ""))
        row = dict(activity.get(cid, {}))
        chat.update(row)
        is_group = cid.endswith("@g.us")
        # Группы используются для обычного общения: очередь «Нужен ответ» и
        # таймер ожидания относятся только к личным чатам. Это также не даёт
        # старой активности группы случайно показывать «Ждёт N мин».
        if is_group:
            chat["needs_reply"] = False
            chat["waiting_since"] = 0
        else:
            try:
                waiting_since = max(0, int(chat.get("waiting_since", 0) or 0))
            except (TypeError, ValueError):
                waiting_since = 0
            chat["needs_reply"] = bool(chat.get("needs_reply")) and waiting_since > 0
            chat["waiting_since"] = waiting_since if chat["needs_reply"] else 0
        if not is_group:
            chat["name"] = display_names().name(cid, chat.get("name", ""), chat.get("phone", ""))
        if not cid.endswith("@g.us") and manual.get(cid):
            chat["name"] = manual[cid]
        sid = str(row.get("last_sender_id", ""))
        if manual.get(sid):
            chat["last_sender"] = manual[sid]
        chat["last_sender_avatar_url"] = queue_avatars.url(sys.modules[__name__], sid) if sid else ""
    return chats


def chat_state_snapshot(requested_chat_id: str = "", read_visible: bool = False) -> dict[str, object]:
    requested = STORE.canonical_whatsapp_chat_id(valid_chat_id(requested_chat_id))
    if requested and read_visible:
        mark_whatsapp_read_throttled(requested)
    stored_chats: list[dict[str, object]] = list(STORE.list_saved_whatsapp_chats(100))
    manual_contacts = list(cached_snapshot_value("manual_contacts", 2.5, STORE.list_manual_whatsapp_contacts))
    manual_by_chat: dict[str, dict[str, object]] = {}
    for manual in manual_contacts:
        if not isinstance(manual, dict):
            continue
        manual_chat_id = valid_chat_id(str(manual.get("chat_id", "")))
        phone_digits = re.sub(r"\D", "", str(manual.get("phone", "")))
        if manual_chat_id:
            manual_by_chat[manual_chat_id] = manual
        if phone_digits:
            manual_by_chat[f"{phone_digits}@c.us"] = manual
    for item in stored_chats:
        item_chat_id = str(item.get("id", ""))
        manual = manual_by_chat.get(item_chat_id)
        if manual:
            item["name"] = str(manual.get("name", "")) or item.get("name", "")
            item["manual_contact"] = True
    known_stored_ids = {str(item.get("id", "")) for item in stored_chats}
    stored_contacts = list(cached_snapshot_value("whatsapp_contacts", 2.5, lambda: STORE.list_whatsapp_contacts(100)))
    for contact in stored_contacts:
        chat_id = valid_chat_id(str(contact.get("id", "")))
        if not chat_id or chat_id in known_stored_ids:
            continue
        stored_chats.append(
            {
                "id": chat_id,
                "name": str(contact.get("name", ""))[:100],
                "last_message": str(contact.get("last_message", ""))[:160],
                "timestamp": epoch_time(str(contact.get("created_at", ""))),
                "unread_count": 0,
                "last_from_me": False,
                "manual_contact": bool(contact.get("manual")),
            }
        )
    with CHAT_LOCK:
        if requested:
            CHAT_STATE["requested_chat_id"] = requested
            live_chat_items = CHAT_STATE.get("chats", [])
            if isinstance(live_chat_items, list):
                for item in live_chat_items:
                    if isinstance(item, dict) and item.get("id") == requested:
                        item["unread_count"] = 0
        selected = requested
        messages_by_chat = CHAT_STATE.get("messages", {})
        live_messages: list[dict[str, object]] = []
        if isinstance(messages_by_chat, dict):
            live_messages = list(messages_by_chat.get(selected, []))
        updated_at = str(CHAT_STATE.get("updated_at", ""))
        connection_is_fresh = False
        if updated_at:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                connection_is_fresh = datetime.now(timezone.utc) - updated < timedelta(seconds=45)
            except ValueError:
                pass
        live_chats = list(CHAT_STATE.get("chats", []))
        discovered_contacts = list(CHAT_STATE.get("discovered_contacts", []))
        profiles_snapshot = dict(CHAT_STATE.get("contact_profiles", {})) if isinstance(CHAT_STATE.get("contact_profiles", {}), dict) else {}
        presence_snapshot = dict(CHAT_STATE.get("presence", {})) if isinstance(CHAT_STATE.get("presence", {}), dict) else {}
        connected = bool(CHAT_STATE.get("connected")) and connection_is_fresh

    # WhatsApp contact discovery/profile data is authoritative for the visible name.
    # It repairs old rows that were accidentally renamed to "Система" by our own
    # outgoing auto-replies and also prevents a raw phone number from being shown.
    whatsapp_names: dict[str, str] = {}
    for contact in discovered_contacts:
        if not isinstance(contact, dict):
            continue
        contact_id = valid_chat_id(str(contact.get("chat_id", "")))
        display_name = whatsapp_display_name(contact.get("name", ""))
        if contact_id and display_name:
            whatsapp_names[contact_id] = display_name
    for contact_id, profile_data in profiles_snapshot.items():
        if not isinstance(profile_data, dict):
            continue
        display_name = whatsapp_display_name(profile_data.get("name", ""))
        if display_name:
            whatsapp_names[str(contact_id)] = display_name

    for manual_id, manual in manual_by_chat.items():
        if str(manual.get("name", "")).strip():
            whatsapp_names[manual_id] = str(manual["name"]).strip()

    history_page = STORE.list_saved_whatsapp_messages_page(selected, 50) if selected else {"messages": [], "has_more": False, "cursor": ""}
    stored_messages = list(history_page.get("messages", []))
    merged_messages: dict[str, dict[str, object]] = {}
    for item in stored_messages:
        message_id = str(item.get("id", ""))
        key = message_id or (
            f"{int(item.get('timestamp', 0) or 0)}:"
            f"{int(bool(item.get('from_me')))}:{item.get('body', '')}"
        )
        previous = merged_messages.get(key, {})
        merged = {**previous, **item}
        for preserve in ("media_path", "media_mime", "media_name", "transcript", "mentions", "quoted_message_key", "quoted_body", "quoted_sender", "forwarded", "sender_phone", "sender_id", "reactions"):
            if not merged.get(preserve) and previous.get(preserve):
                merged[preserve] = previous[preserve]
        # БД является авторитетной для удаления/редактирования. Live-снимок
        # коннектора может ещё несколько секунд содержать старую версию сообщения.
        merged["deleted"] = bool(previous.get("deleted") or item.get("deleted"))
        merged["edited"] = bool(previous.get("edited") or item.get("edited"))
        if previous.get("edited"):
            merged["body"] = previous.get("body", "")
        if merged["deleted"]:
            merged["body"] = ""
            merged["media_path"] = ""
            merged["media_url"] = ""
        merged_messages[key] = merged
    raw_messages = sorted(
        merged_messages.values(),
        key=lambda item: int(item.get("timestamp", 0) or 0),
    )[-50:]
    raw_messages = dedupe_whatsapp_messages(raw_messages)
    messages = decorate_chat_messages(selected, raw_messages) if selected else []
    merged_chats: dict[str, dict[str, object]] = {}
    for item in stored_chats:
        chat_id = str(item.get("id", ""))
        if not chat_id:
            continue
        clean_name = whatsapp_names.get(chat_id) or whatsapp_display_name(item.get("name", ""))
        merged_chats[chat_id] = {
            **item,
            "name": clean_name or "Пользователь WhatsApp",
        }
    for item in live_chats:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        chat_id = STORE.canonical_whatsapp_chat_id(str(item["id"]))
        if not chat_id:
            continue
        item = {**item, "id": chat_id}
        previous = merged_chats.get(chat_id, {})
        manual = manual_by_chat.get(chat_id)
        live_name = whatsapp_display_name(item.get("name", ""))
        previous_name = whatsapp_display_name(previous.get("name", ""))
        previous_timestamp = int(previous.get("timestamp", 0) or 0)
        live_timestamp = int(item.get("timestamp", 0) or 0)
        merged = {
            **previous,
            **item,
            # Never show our own label "Система" or a raw phone as a contact name.
            # Prefer the name WhatsApp itself reported for this contact.
            "name": whatsapp_names.get(chat_id) or live_name or previous_name or "Пользователь WhatsApp",
            "manual_contact": bool(manual) or bool(item.get("manual_contact")) or bool(previous.get("manual_contact")),
        }
        # Резервная синхронизация может записать более новое исходящее сообщение
        # прямо в БД, пока live-список ещё содержит старый preview. Не даём
        # старому live-состоянию затереть пометку «Вы:».
        if previous_timestamp > live_timestamp:
            merged["last_message"] = previous.get("last_message", "")
            merged["timestamp"] = previous_timestamp
            merged["last_from_me"] = bool(previous.get("last_from_me"))
        merged["unread_count"] = int(previous.get("unread_count", 0) or 0)
        merged_chats[chat_id] = merged
    # Финальная защита от дублей личных чатов. WhatsApp иногда одновременно
    # отдаёт одного человека как @lid и @c.us. Если обе строки содержат один и
    # тот же подтверждённый номер телефона, показываем одну карточку, сохраняя
    # более свежее превью и максимальный счётчик непрочитанных. Группы никогда
    # не объединяем по имени или другим эвристикам.
    deduped_private: dict[str, dict[str, object]] = {}
    for raw_chat in merged_chats.values():
        chat = dict(raw_chat)
        cid = valid_chat_id(str(chat.get("id", "")))
        if not cid:
            continue
        if cid.endswith("@g.us"):
            identity = f"group:{cid}"
            target_id = cid
        else:
            phone_digits = re.sub(r"\D", "", str(chat.get("phone", "")))
            if not phone_digits and cid.endswith("@c.us"):
                phone_digits = re.sub(r"\D", "", cid.split("@", 1)[0])
            canonical_cid = STORE.canonical_whatsapp_chat_id(cid)
            target_id = f"{phone_digits}@c.us" if phone_digits else canonical_cid
            # Важно: ключ дедупликации тоже строим по каноническому ID.
            # Раньше @lid и его уже известный @c.us могли получить одинаковый
            # target_id, но разные identity, поэтому в UI оставались две карточки.
            identity = f"phone:{phone_digits}" if phone_digits else f"id:{target_id}"
        chat["id"] = target_id
        previous = deduped_private.get(identity)
        if not previous:
            deduped_private[identity] = chat
            continue
        prev_ts = int(previous.get("timestamp", 0) or 0)
        cur_ts = int(chat.get("timestamp", 0) or 0)
        newer, older = (chat, previous) if cur_ts >= prev_ts else (previous, chat)
        merged = {**older, **newer}
        merged["id"] = target_id
        merged["unread_count"] = max(int(previous.get("unread_count", 0) or 0), int(chat.get("unread_count", 0) or 0))
        merged["manual_contact"] = bool(previous.get("manual_contact")) or bool(chat.get("manual_contact"))
        # Не даём пустому/техническому имени затереть нормальное имя контакта.
        prev_name = whatsapp_display_name(previous.get("name", ""))
        cur_name = whatsapp_display_name(chat.get("name", ""))
        merged["name"] = cur_name or prev_name or "Пользователь WhatsApp"
        deduped_private[identity] = merged
    favorite_ids = favorite_chat_ids()
    chats = apply_deleted_preview_override(list(deduped_private.values()))
    for chat in chats:
        chat_id = str(chat.get("id", "") or "")
        chat["favorite"] = chat_id in favorite_ids
    chats = sorted(
        chats,
        key=lambda item: (bool(item.get("favorite")), int(item.get("timestamp", 0) or 0)),
        reverse=True,
    )[:100]
    now_presence = datetime.now(timezone.utc)
    def fresh_presence(chat_id: str) -> dict[str, object]:
        raw = presence_snapshot.get(chat_id, {}) if isinstance(presence_snapshot, dict) else {}
        if not isinstance(raw, dict):
            return {"known": False, "online": False}
        updated_raw = str(raw.get("updated_at", "") or "")
        if updated_raw:
            try:
                updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if now_presence - updated > timedelta(seconds=35):
                    return {"known": False, "online": False}
            except ValueError:
                return {"known": False, "online": False}
        return {"known": bool(raw.get("known")), "online": bool(raw.get("online")), "state": str(raw.get("state", "") or "")}
    for chat in chats:
        chat_id = str(chat["id"])
        chat["avatar_url"] = queue_avatars.url(sys.modules[__name__],chat_id)
        if not chat_id.endswith("@g.us"):
            chat["presence"] = fresh_presence(chat_id)
    manual_mode = STORE.manual_chat_mode(selected) if selected else None
    selected_manual = manual_by_chat.get(selected) if selected else None
    profile = dict(profiles_snapshot.get(selected, {})) if selected else {}
    selected_presence = fresh_presence(selected) if selected else {"known": False, "online": False}
    if selected:
        profile["profile_pic_url"] = queue_avatars.url(sys.modules[__name__],selected)
        profile["presence"] = selected_presence
    if profile:
        profile["name"] = display_names().name(selected, whatsapp_names.get(selected) or profile.get("name", ""), profile.get("phone", ""))
    # 3.3.105: read-only context for the optional information drawer.
    # This must never affect ticket routing or WhatsApp state. If the helper is
    # unavailable for any reason, the normal chat API stays unchanged.
    ui_context: dict[str, object] = {"active_tickets": []}
    if selected:
        raw_phone = str(profile.get("phone", "") or "").strip()
        if not raw_phone and selected.endswith("@c.us"):
            digits = re.sub(r"\D", "", selected.split("@", 1)[0])
            raw_phone = f"+{digits}" if digits else ""
        try:
            cache_key = f"ui_active_tickets:{selected}:{raw_phone}"
            active_rows = cached_snapshot_value(cache_key, 2.5, lambda: STORE.list_active_user_tickets(selected, raw_phone, 8))
        except Exception:
            active_rows = []
        active_items: list[dict[str, object]] = []
        for raw_row in active_rows:
            try:
                row = dict(raw_row)
            except Exception:
                row = raw_row if isinstance(raw_row, dict) else {}
            try:
                ticket_id = int(row.get("id", 0) or 0)
            except (TypeError, ValueError):
                ticket_id = 0
            if ticket_id <= 0:
                continue
            active_items.append({
                "id": ticket_id,
                "category": str(row.get("category", "") or row.get("category_name", "") or "Заявка"),
                "status": str(row.get("status", "") or ""),
                "priority": str(row.get("priority", "") or ""),
                "problem": str(row.get("problem", "") or row.get("description", "") or "")[:180],
                "updated_at": str(row.get("updated_at", "") or row.get("created_at", "") or ""),
            })
        ui_context = {"chat_id": selected, "phone": raw_phone, "active_tickets": active_items}
    return {
        "connected": connected,
        "updated_at": updated_at,
        "selected_chat_id": selected,
        "chats": enrich_chat_list(chats),
        "messages": messages,
        "history_has_more": bool(history_page.get("has_more")),
        "history_cursor": str(history_page.get("cursor", "")),
        "manual_mode": manual_mode or {},
        "manual_contact": bool(selected_manual),
        "profile": profile,
        "presence": selected_presence,
        "ui_context": ui_context,
        "forward_targets": forward_targets_snapshot(),
    }


def group_state_snapshot(requested_group_id: str = "", read_visible: bool = False) -> dict[str, object]:
    requested = valid_group_id(requested_group_id)
    if requested and read_visible:
        mark_whatsapp_read_throttled(requested)
    groups = STORE.list_whatsapp_groups()
    group_preview_rows = [
        {
            "id": str(group.get("chat_id", "")),
            "last_message": str(group.get("last_message", "")),
            "timestamp": int(group.get("last_timestamp", 0) or 0),
            "last_from_me": bool(group.get("last_from_me")),
        }
        for group in groups
    ]
    group_preview_rows = apply_deleted_preview_override(group_preview_rows)
    group_preview_by_id = {str(row.get("id", "")): row for row in group_preview_rows}
    for group in groups:
        override = group_preview_by_id.get(str(group.get("chat_id", "")))
        if override:
            group["last_message"] = str(override.get("last_message", group.get("last_message", "")))
            group["last_timestamp"] = int(override.get("timestamp", group.get("last_timestamp", 0)) or 0)
            group["last_from_me"] = bool(override.get("last_from_me", group.get("last_from_me")))
    selected = requested or (str(groups[0]["chat_id"]) if groups else "")
    with CHAT_LOCK:
        if selected:
            CHAT_STATE["requested_group_id"] = selected
        participants_map = CHAT_STATE.get("group_participants", {})
        participants = (
            list(participants_map.get(selected, []))
            if isinstance(participants_map, dict) and selected
            else []
        )
        presence_snapshot = dict(CHAT_STATE.get("presence", {})) if isinstance(CHAT_STATE.get("presence", {}), dict) else {}
    participant_rows: list[dict[str, object]] = []
    for raw_participant in participants:
        if not isinstance(raw_participant, dict):
            continue
        participant = dict(raw_participant)
        participant["avatar_url"] = ""
        for avatar_id in (str(participant.get("resolved_id", "") or "").strip(), str(participant.get("mention_id", "") or "").strip()):
            if not avatar_id:
                continue
            avatar_url = queue_avatars.url(sys.modules[__name__], avatar_id)
            if avatar_url:
                participant["avatar_url"] = avatar_url
                break
        presence_value = {"known": False, "online": False}
        for presence_id in (str(participant.get("resolved_id", "") or "").strip(), str(participant.get("mention_id", "") or "").strip()):
            raw_presence = presence_snapshot.get(presence_id, {}) if presence_id else {}
            if not isinstance(raw_presence, dict):
                continue
            updated_raw = str(raw_presence.get("updated_at", "") or "")
            fresh = False
            if updated_raw:
                try:
                    updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    fresh = datetime.now(timezone.utc) - updated < timedelta(seconds=35)
                except ValueError:
                    fresh = False
            if fresh:
                presence_value = {"known": bool(raw_presence.get("known")), "online": bool(raw_presence.get("online")), "state": str(raw_presence.get("state", "") or "")}
                break
        participant["presence"] = presence_value
        participant_rows.append(participant)
    participants = participant_rows
    connection = connector_state_snapshot()
    favorite_ids = favorite_chat_ids()
    history_page = STORE.list_saved_whatsapp_messages_page(selected, 50) if selected else {"messages": [], "has_more": False, "cursor": ""}
    raw_messages = dedupe_whatsapp_messages(list(history_page.get("messages", [])))
    return {
        "connected": connection["connected"],
        "selected_chat_id": selected,
        "selected_muted": bool(next((group.get("muted") for group in groups if str(group.get("chat_id", "")) == selected), 0)),
        "chats": enrich_chat_list([
            {
                "id": str(group["chat_id"]),
                "avatar_url": queue_avatars.url(sys.modules[__name__],str(group["chat_id"])),
                "name": str(group["name"]),
                "last_message": str(group.get("last_message", "")),
                "timestamp": int(group.get("last_timestamp", 0) or 0),
                "unread_count": int(group.get("unread_count", 0) or 0),
                "mention_unread_count": int(group.get("mention_unread_count", 0) or 0),
                "muted": bool(group.get("muted")),
                "favorite": str(group.get("chat_id", "")) in favorite_ids,
                "last_from_me": bool(group.get("last_from_me")),
                # В списке групп показываем обычный числовой счётчик непрочитанных
                # справа, как в WhatsApp. @упоминание подсвечивается уже внутри чата.
                "mentioned": False,
            }
            for group in groups
        ]),
        "messages": decorate_chat_messages(selected, raw_messages) if selected else [],
        "history_has_more": bool(history_page.get("has_more")),
        "history_cursor": str(history_page.get("cursor", "")),
        "participants": participants,
        "forward_targets": forward_targets_snapshot(),
    }


def connector_state_snapshot() -> dict[str, object]:
    with CHAT_LOCK:
        updated_at = str(CHAT_STATE.get("updated_at", ""))
        status = str(CHAT_STATE.get("connector_status", "offline"))
        qr_data_url = str(CHAT_STATE.get("qr_data_url", ""))
        fresh = False
        if updated_at:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                fresh = datetime.now(timezone.utc) - updated < timedelta(seconds=45)
            except ValueError:
                pass
        connected = bool(CHAT_STATE.get("connected")) and fresh
    if not fresh and status != "qr":
        status = "offline"
    return {
        "connected": connected,
        "status": status,
        "updated_at": updated_at,
        "qr_data_url": qr_data_url if status == "qr" else "",
    }


def _decorate_notification_event(event: dict[str, object]) -> dict[str, object]:
    item = dict(event)
    event_id = str(item.get("id", "") or "")
    kind = str(item.get("kind", "") or "")
    chat_id = ""
    if event_id.startswith("contact:"):
        chat_id = event_id[len("contact:"):].rsplit(":", 1)[0]
    elif event_id.startswith("group:"):
        chat_id = event_id[len("group:"):].rsplit(":", 1)[0]
    chat_id = str(item.get("chat_id") or chat_id)
    if not chat_id:
        return item
    if kind == "contact": item["title"] = display_names().name(chat_id, item.get("title", ""))

    event_ts = int(item.get("timestamp", 0) or 0)
    row = None
    try:
        with STORE.connection() as connection:
            row = connection.execute(
                """SELECT message_key, sender, sender_id, body, media_path, media_mime, media_name, message_timestamp
                   FROM whatsapp_chat_messages
                   WHERE chat_id=? AND from_me=0 AND deleted=0 AND message_key=?
                   ORDER BY id DESC LIMIT 1""",
                (chat_id, str(item.get("message_key", ""))),
            ).fetchone()
    except Exception:
        row = None

    avatar_candidates: list[str] = []
    if row is not None and kind == "group":
        item["title"] = display_names().name(row["sender_id"], row["sender"]) + " · " + str(item.get("title", "Группа"))
        avatar_candidates.append(str(row["sender_id"] or "").strip())
    avatar_candidates.append(chat_id)
    for avatar_id in avatar_candidates:
        if not avatar_id:
            continue
        avatar_url = queue_avatars.url(sys.modules[__name__], avatar_id)
        if avatar_url:
            item["avatar_url"] = avatar_url
            break

    if row is not None:
        mime = str(row["media_mime"] or "").strip().lower()
        media_path = str(row["media_path"] or "").strip()
        message_key = str(row["message_key"] or "").strip()
        if media_path and message_key and mime.startswith("image/"):
            item["media_url"] = f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(message_key)}"
            item["media_mime"] = mime
            if not str(item.get("detail", "") or "").strip() or str(item.get("detail", "")).startswith("["):
                item["detail"] = str(row["body"] or row["media_name"] or "Фото")
    return item


def notifications_snapshot() -> dict[str, object]:
    counts = queue_productivity.notification_counts(STORE, STORE.notification_counts())
    base_events = [_decorate_notification_event(item) for item in STORE.notification_events(20)]
    base_events = queue_productivity.filter_notification_events(STORE, base_events)
    connector = connector_state_snapshot()
    reminder_count, reminder_events = queue_productivity.reminder_events(STORE)
    system_events = queue_productivity.system_alerts(STORE, DATA_DIR, connector)
    # 3.3.104: if the live connector is already healthy, immediately retire the
    # persisted reliability alert instead of waiting up to five minutes for the
    # background health loop. This prevents a stale red warning after reconnect.
    if bool(connector.get("connected")):
        try: queue_reliability.resolve_persisted_alert(STORE, "connector")
        except Exception: pass
    reliability_count, reliability_events = queue_reliability.reliability_notification_events(sys.modules[__name__])
    queue_reliability.reconcile_notification_dismissals(
        STORE, [item.get("id") or item.get("event_id") for item in system_events], ("system:",)
    )
    system_events = queue_reliability.filter_dismissed_notifications(STORE, system_events)
    # 3.3.103: normalise reliability/SLA events so they behave like every other
    # notification in the UI. queue_reliability historically used event_id, while
    # the notification centre expects id/href/source. Keep the original payload too.
    normalized_reliability_events: list[dict[str, object]] = []
    for raw_event in reliability_events:
        event = dict(raw_event)
        event_id = str(event.get("id") or event.get("event_id") or "").strip()
        if event_id:
            event["id"] = event_id
        if not str(event.get("source") or "").strip():
            event["source"] = "SLA" if event_id.startswith("sla:") else "Система"
        if not str(event.get("href") or "").strip():
            if event_id.startswith("sla:"):
                parts = event_id.split(":")
                ticket_id = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
                event["href"] = f"/ticket?id={ticket_id}" if ticket_id else "/admin/system"
            else:
                event["href"] = "/admin/system"
        normalized_reliability_events.append(event)
    reliability_events = normalized_reliability_events
    counts["reminders"] = int(reminder_count)
    # Reliability/SLA alerts belong to the same visible "Системные предупреждения"
    # row in the notification center. Keep one canonical counter so the red badge
    # can never disagree with the rows shown in the popover.
    counts["system"] = len(system_events) + int(reliability_count)
    counts.pop("reliability", None)
    total = sum(int(value or 0) for value in counts.values())
    events = [*reliability_events, *system_events, *reminder_events, *base_events]
    return {
        "counts": counts,
        "total": total,
        "group_chat_id": queue_productivity.first_unmuted_chat(STORE, True),
        "contact_chat_id": queue_productivity.first_unmuted_chat(STORE, False),
        "events": events[:20],
    }


def human_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def monitor_log_entries(limit: int = 40) -> list[str]:
    try:
        lines = MONITOR_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [normalize_message(line)[:600] for line in lines[-max(1, min(limit, 200)):]][::-1]


def system_status_snapshot() -> dict[str, object]:
    connector = connector_state_snapshot()
    database = STORE.database_health()
    queue = STORE.outbound_queue_snapshot(30)
    try:
        disk = shutil.disk_usage(DATA_DIR)
        disk_total, disk_used, disk_free = int(disk.total), int(disk.used), int(disk.free)
    except OSError:
        disk_total = disk_used = disk_free = 0
    db_size = DATABASE_PATH.stat().st_size if DATABASE_PATH.exists() else 0
    media_size = directory_size(MEDIA_DIR)
    outbound_media_size = directory_size(OUTBOUND_MEDIA_DIR)
    heartbeat_age = None
    if MONITOR_HEARTBEAT_PATH.exists():
        try:
            heartbeat_age = max(0, int(datetime.now(timezone.utc).timestamp() - MONITOR_HEARTBEAT_PATH.stat().st_mtime))
        except OSError:
            heartbeat_age = None
    return {
        "site_ok": True,
        "connector": connector,
        "database": database,
        "queue": queue,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "db_size": db_size,
        "media_size": media_size,
        "outbound_media_size": outbound_media_size,
        "heartbeat_age": heartbeat_age,
        "monitor_ok": True if LOCAL_MODE else (heartbeat_age is not None and heartbeat_age < 300),
        "monitor_mode": "local" if LOCAL_MODE else "server",
        "logs": monitor_log_entries(40),
    }


def parse_iso_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def ticket_sla_state(ticket: dict[str, object]) -> dict[str, object]:
    priority = str(ticket.get("priority", "normal"))
    limit_minutes = int(SLA_MINUTES.get(priority, SLA_MINUTES["normal"]))
    created = parse_iso_datetime(ticket.get("created_at"))
    first_response = parse_iso_datetime(ticket.get("first_response_at"))
    now = datetime.now(timezone.utc)
    if not created:
        return {"active": False, "overdue": False, "warning": False, "limit_minutes": limit_minutes}
    due = created + timedelta(minutes=limit_minutes)
    status = str(ticket.get("status", ""))
    active = status == "new" and not first_response
    reference = now if active else (first_response or now)
    elapsed = max(0, int((reference - created).total_seconds()))
    overdue = active and now > due
    warning = active and not overdue and elapsed >= int(limit_minutes * 60 * 0.75)
    remaining = int((due - now).total_seconds()) if active else 0
    return {
        "active": active, "overdue": overdue, "warning": warning,
        "limit_minutes": limit_minutes, "due_epoch": int(due.timestamp()),
        "elapsed_seconds": elapsed, "remaining_seconds": remaining,
    }


def sla_badge(ticket: dict[str, object]) -> str:
    state = ticket_sla_state(ticket)
    if not state.get("active"):
        return ""
    remaining = int(state.get("remaining_seconds", 0) or 0)
    overdue = bool(state.get("overdue"))
    warning = bool(state.get("warning"))
    seconds = abs(remaining)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    short = f"{hours}ч {minutes}м" if hours else f"{minutes}м"
    label = f"SLA просрочен {short}" if overdue else f"SLA {short}"
    cls = "sla-overdue" if overdue else "sla-warning" if warning else "sla-ok"
    return (
        f'<span class="sla-badge {cls}" data-sla-deadline="{int(state.get("due_epoch",0) or 0)}" '
        f'data-sla-active="1">{e(label)}</span>'
    )


def status_badge(status: str) -> str:
    return f'<span class="status status-{e(status)}">{e(STATUSES.get(status, status))}</span>'


def priority_badge(priority: str) -> str:
    return f'<span class="priority priority-{e(priority)}">{e(PRIORITIES.get(priority, priority))}</span>'


def employee_options(selected: str = "") -> str:
    selected = selected if selected in EMPLOYEES else active_employee()
    return "".join(
        f'<option value="{e(name)}" {"selected" if name == selected else ""}>{e(name)}</option>'
        for name in EMPLOYEES
    )


def category_options(selected: str = "", include_all: bool = False) -> str:
    prefix = '<option value="">Все категории</option>' if include_all else ""
    items = list(CATEGORIES.items()) if include_all else active_ticket_category_items()
    return prefix + "".join(
        f'<option value="{e(key)}" {"selected" if key == selected else ""}>{e(label)}</option>'
        for key, label in items
    )


def ticket_accepted_reply(ticket_id: int) -> str:
    return (
        f"Заявка №{ticket_id} зарегистрирована и передана в работу.\n\n"
        "Если появится другая проблема, отправьте 0 или слово «меню» и выберите новую тему."
    )


def active_tickets_reply(tickets: list[dict[str, object]]) -> str:
    if not tickets:
        return (
            "Активных заявок сейчас нет.\n\n"
            "Чтобы создать новую заявку, отправьте 0 или слово «меню»."
        )
    rows: list[str] = []
    for index, ticket in enumerate(tickets, start=1):
        ticket_id = int(ticket.get("id", 0) or 0)
        category = CATEGORIES.get(str(ticket.get("category", "")), str(ticket.get("category", "")))
        status = STATUSES.get(str(ticket.get("status", "")), str(ticket.get("status", "")))
        summary = normalize_message(str(ticket.get("summary", "") or ticket.get("original_text", ""))).replace("\n", " ")
        if len(summary) > 90:
            summary = summary[:87].rstrip() + "..."
        detail = f"\n   {summary}" if summary else ""
        rows.append(f"{index}. Заявка №{ticket_id} · {category} · {status}{detail}")
    return (
        "Ваши активные заявки:\n\n" + "\n".join(rows) +
        "\n\nОтправьте номер заявки из этого списка, если хотите добавить к ней информацию. "
        "После выбора можно прислать текст, фото или файл.\n"
        "Чтобы вернуться в главное меню, отправьте 0."
    )


def active_ticket_selected_reply(ticket: dict[str, object]) -> str:
    ticket_id = int(ticket.get("id", 0) or 0)
    category = CATEGORIES.get(str(ticket.get("category", "")), str(ticket.get("category", "")))
    status = STATUSES.get(str(ticket.get("status", "")), str(ticket.get("status", "")))
    return (
        f"Выбрана заявка №{ticket_id} · {category} · {status}.\n\n"
        "Отправьте дополнительную информацию обычным сообщением. Можно приложить фото, скриншот или файл. "
        "Все следующие сообщения будут добавляться к этой заявке, пока вы не отправите 0 или слово «меню»."
    )


def support_question_accepted_reply(ticket_id: int) -> str:
    return (
        f"Вопрос №{ticket_id} передан в поддержку. Ответ сотрудника придёт в этот же чат.\n\n"
        "Дополнительные сообщения, фото и файлы можно отправлять сюда же — они будут относиться к этому вопросу.\n"
        "Чтобы задать новый отдельный вопрос, отправьте 0 или слово «меню»."
    )


def update_ticket_status(
    ticket_id: int,
    status: str,
    actor: str,
    assigned_to: str,
    allow_reopen: bool = False,
    close_reason: str = "",
    close_comment: str = "",
) -> tuple[bool, int]:
    # QUEUE_3_3_108_CLOSE_REASON_TO_USER
    before = STORE.get_ticket(ticket_id)
    if not before or status not in STATUSES:
        return False, 0
    previous_status = str(before.get("status", ""))
    # Выполненные и недействительные заявки считаются отработанными.
    # Обычный сотрудник не может вернуть такую заявку в работу; это может только админ.
    if previous_status in {"done", "invalid"} and status != previous_status and not allow_reopen:
        return False, 0
    # Повторный выбор того же статуса не создаёт лишних событий и автоответов.
    if previous_status == status:
        return True, 0
    updated = STORE.update_status(ticket_id, status, actor, assigned_to)
    if not updated or before.get("source") != "whatsapp":
        return updated, 0

    reason_key = normalize_message(str(close_reason or "")).strip()
    reason_label = queue_workflow.ALLOWED_CLOSE_REASONS.get(reason_key, "")
    comment_text = normalize_message(str(close_comment or "")).strip()
    close_details: list[str] = []
    if reason_label:
        close_details.append(f"Причина: {reason_label}")
    if comment_text:
        close_details.append(f"Комментарий: {comment_text[:1500]}")
    close_details_text = "\n".join(close_details)

    body = ""
    if status == "done":
        body = f"Заявка №{ticket_id} выполнена."
        if close_details_text:
            body += f"\n\n{close_details_text}"
        body += (
            "\n\nЕсли у вас появится новый запрос, отправьте 0, слово «меню» или сообщение с описанием проблемы. "
            "Обычная переписка больше не запускает автоответчик."
        )
    elif status == "invalid":
        body = f"Заявка №{ticket_id} отмечена как недействительная."
        if close_details_text:
            body += f"\n\n{close_details_text}"
        body += (
            "\n\nПожалуйста, создайте запрос заново по правильной форме.\n\n"
            f"{main_menu_text()}"
        )
        contact_key = str(before.get("chat_id", "")) or normalize_phone(
            str(before.get("phone", ""))
        )
        STORE.set_conversation_context(contact_key, MENU_CONTEXT, ticket_id, True)
    message_id = STORE.queue_outbound_message(ticket_id, body, "Система") if body else 0
    return updated, message_id


def priority_options(selected: str = "", include_all: bool = False) -> str:
    prefix = '<option value="">Все приоритеты</option>' if include_all else ""
    return prefix + "".join(
        f'<option value="{e(key)}" {"selected" if key == selected else ""}>{e(label)}</option>'
        for key, label in PRIORITIES.items()
    )


def inline_priority_select(ticket_id: int, selected: str) -> str:
    return (
        f'<select class="priority-select priority-select-{e(selected)}" '
        f'data-quick-priority data-ticket-id="{ticket_id}" '
        f'aria-label="Приоритет заявки #{ticket_id}">'
        f'{priority_options(selected)}</select>'
    )


def render_ticket_rows(tickets: list[dict[str, object]]) -> str:
    rows: list[str] = []
    for ticket in tickets:
        source_label = {
            "whatsapp": "WhatsApp",
            "telegram_manual": "Telegram вручную",
            "admin_manual": "Вручную",
        }.get(str(ticket["source"]), "Вручную")
        sla = ticket_sla_state(ticket)
        row_sla_class = " sla-row-overdue" if sla.get("overdue") else " sla-row-warning" if sla.get("warning") else ""
        rows.append(
            f"""<tr class="{row_sla_class.strip()}" data-ticket-row data-ticket-id="{ticket['id']}" data-ticket-title="{e(ticket['title'])}" data-assigned-to="{e(ticket['assigned_to'])}" data-ticket-status="{e(ticket['status'])}">
              <td class="priority-cell">{inline_priority_select(int(ticket['id']), str(ticket['priority']))}</td>
              <td class="ticket-number">#{ticket['id']}</td>
              <td><span class="source source-{e(ticket['source'])}">{e(source_label)}</span></td>
              <td><strong>{e(ticket['sender'])}</strong><small>{e(display_phone(str(ticket['phone'])))}</small></td>
              <td><a class="ticket-title-link" href="/ticket?id={ticket['id']}"><strong>{e(ticket['title'])}</strong></a><small class="summary">{e(ticket['summary'])}</small></td>
              <td>{status_badge(str(ticket['status']))}{'<span class="handoff-badge">Следующая смена</span>' if int(ticket.get('shift_handoff', 0) or 0) else ''}{sla_badge(ticket)}<small>{e(ticket['assigned_to'])}</small></td>
              <td><time>{e(human_time(str(ticket['created_at'])))}</time></td>
            </tr>"""
        )
    return "".join(rows) or '<tr><td colspan="7" class="empty">Заявок пока нет. Запусти WhatsApp QR-коннектор или добавь заявку вручную.</td></tr>'


def query_page(query: dict[str, list[str]]) -> int:
    try:
        return max(1, int(query.get("page", ["1"])[0]))
    except ValueError:
        return 1


def render_pagination(
    query: dict[str, list[str]],
    total: int,
    page: int,
    page_size: int = PAGE_SIZE,
) -> str:
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    if pages <= 1:
        return f'<span class="pagination-total">Всего заявок: {total}</span>'

    def page_url(number: int) -> str:
        values = {
            key: items[0]
            for key, items in query.items()
            if items and items[0] and key != "page"
        }
        if number > 1:
            values["page"] = str(number)
        encoded = urlencode(values)
        return f"/?{encoded}" if encoded else "/"

    visible = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    links: list[str] = []
    previous = 0
    for number in visible:
        if previous and number - previous > 1:
            links.append('<span class="pagination-gap">…</span>')
        current = " current" if number == page else ""
        links.append(
            f'<a class="pagination-link{current}" href="{e(page_url(number))}">{number}</a>'
        )
        previous = number
    prev_link = (
        f'<a class="pagination-link" href="{e(page_url(page - 1))}">Назад</a>'
        if page > 1
        else '<span class="pagination-link disabled">Назад</span>'
    )
    next_link = (
        f'<a class="pagination-link" href="{e(page_url(page + 1))}">Вперёд</a>'
        if page < pages
        else '<span class="pagination-link disabled">Вперёд</span>'
    )
    return (
        f'<span class="pagination-total">Страница {page} из {pages} · заявок {total}</span>'
        f'<div class="pagination-links">{prev_link}{"".join(links)}{next_link}</div>'
    )


def layout(
    title: str,
    content: str,
    active: str = "tickets",
    show_admin: bool = False,
) -> str:
    nav_items = [
        ("tickets", "/", "Заявки", ""),
        ("whatsapp", "/whatsapp", "WhatsApp", ""),
        ("groups", "/groups", "Группы", ""),
        ("search", "/search", "Поиск", ""),
        ("bookmarks", "/bookmarks", "Закладки", ""),
        ("reminders", "/reminders", "Напоминания", ""),
        ("recent", "/recent", "Недавние", ""),
        ("instruction", INSTRUCTION_URL, "Инструкция", ""),
    ]
    if show_admin:
        nav_items.append(("admin", "/admin/", "Админка", ""))
        nav_items.append(("logout", "/admin/logout", "Выйти", ""))
    else:
        nav_items.append(("admin-login", "/admin/login", "Админ-вход", ""))
    nav = "".join(
        f'<a class="nav-link {"active" if key == active else ""}" href="{e(href)}"{attributes}>{e(label)}</a>'
        for key, href, label, attributes in nav_items
    )
    current_employee = active_employee()
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="queue-csrf" content="{e(ADMIN_FORM_TOKEN)}">
  <title>{e(title)} · Единая очередь</title>
  <meta name="theme-color" content="#112a25">
  <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon.png?v=3.3.79">
  <link rel="apple-touch-icon" href="/static/favicon.png?v=3.3.79">
  <script>document.documentElement.dataset.theme = localStorage.getItem("queue-theme") || "light";</script>
  <link rel="stylesheet" href="/static/style.css?v=3.3.79">
  <link rel="stylesheet" href="/static/ui.css?v=3.3.79">
  <link rel="stylesheet" href="/static/instruction.css?v=3.3.85">
  <link rel="stylesheet" href="/static/global-theme.css?v=3.3.88">
  <link rel="stylesheet" href="/static/productivity.css?v=3.3.90">
  <link rel="stylesheet" href="/static/workflow.css?v=3.3.97">
  <link rel="stylesheet" href="/static/reliability.css?v=3.3.99">
  <link rel="stylesheet" href="/static/notification-hotfix.css?v=3.3.104">
  <link rel="stylesheet" href="/static/interface105.css?v=3.3.111">
  <link rel="stylesheet" href="/static/performance107.css?v=3.3.107">
</head>
<body data-admin="{'1' if show_admin else '0'}">
  <header class="topbar">
    <a class="brand" href="/"><span class="brand-mark">Q</span><span>Единая очередь</span></a>
    {'<span class="local-mode-badge">WINDOWS · ТЕСТ</span>' if LOCAL_MODE else ''}
    <nav>{nav}</nav>
    <button class="notification-button" type="button" data-notification-center aria-label="Уведомления">
      <span aria-hidden="true">🔔</span><b data-notification-count hidden>0</b>
    </button>
    <div class="notification-popover" data-notification-popover hidden>
      <strong>Уведомления</strong><div data-notification-items>Проверка новых событий...</div>
    </div>
    <form class="shift-form" method="post" action="/active-employee" data-shift-form>
      <span>На смене</span>
      <select name="employee" aria-label="Сотрудник на смене">{employee_options(current_employee)}</select>
    </form>
    <button class="theme-toggle" type="button" data-theme-toggle aria-label="Сменить оформление">Тёмная тема</button>
  </header>
  <div class="toast-stack" data-toast-stack aria-live="polite" aria-atomic="false"></div>
  {'<div class="maintenance-banner">Режим обслуживания включён: просмотр доступен, изменения сотрудников временно заблокированы.</div>' if queue_reliability.maintenance_active(STORE) else ''}
  <main class="page">{content}</main>
  <footer>Единая очередь · рабочая система обработки заявок</footer>
  <script src="/static/ui.js?v=3.3.79" defer></script>
  <script src="/static/app.js?v=3.3.104" defer></script>
  <script src="/static/productivity.js?v=3.3.90" defer></script>
  <script src="/static/workflow.js?v=3.3.111" defer></script>
  <script src="/static/reliability.js?v=3.3.99" defer></script>
  <script src="/static/interface105.js?v=3.3.106" defer></script>
  <script src="/static/performance107.js?v=3.3.107" defer></script>
</body>
</html>"""



INSTRUCTION_LINK_RE = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>]+)")


def instruction_text_html(value: object) -> str:
    """Escape instruction text and turn explicit web URLs into safe links."""
    raw = str(value or "")
    parts: list[str] = []
    cursor = 0
    for match in INSTRUCTION_LINK_RE.finditer(raw):
        start, end = match.span(1)
        parts.append(e(raw[cursor:start]))
        token = match.group(1)
        trailing = ""
        while token and token[-1] in ".,;:!?)]:":
            trailing = token[-1] + trailing
            token = token[:-1]
        href = token if token.lower().startswith(("http://", "https://")) else f"https://{token}"
        parts.append(
            f'<a class="instruction-link" href="{e(href)}" target="_blank" rel="noopener noreferrer">{e(token)}</a>'
        )
        parts.append(e(trailing))
        cursor = end
    parts.append(e(raw[cursor:]))
    return "".join(parts).replace("\n", "<br>")


def load_instruction_data() -> dict[str, object]:
    try:
        payload = json.loads(INSTRUCTION_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"title": "Инструкция", "snapshot_date": "", "sections": []}
    if not isinstance(payload, dict):
        return {"title": "Инструкция", "snapshot_date": "", "sections": []}
    sections = payload.get("sections", [])
    if not isinstance(sections, list):
        sections = []
    cleaned_sections: list[dict[str, object]] = []
    for index, section in enumerate(sections[:50], start=1):
        if not isinstance(section, dict):
            continue
        title = str(section.get("title", "") or "").strip()[:160]
        section_id = re.sub(r"[^a-z0-9_-]+", "-", str(section.get("id", "") or "").casefold()).strip("-")
        if not section_id:
            section_id = f"section-{index}"
        items = section.get("items", [])
        if not isinstance(items, list):
            items = []
        cleaned_items = [str(item or "").strip()[:12000] for item in items if str(item or "").strip()]
        if title and cleaned_items:
            cleaned_sections.append({"id": section_id, "title": title, "items": cleaned_items[:100]})
    return {
        "title": str(payload.get("title", "Инструкция") or "Инструкция")[:160],
        "source_name": str(payload.get("source_name", "") or "")[:200],
        "source_url": str(payload.get("source_url", INSTRUCTION_SOURCE_URL) or INSTRUCTION_SOURCE_URL)[:1000],
        "snapshot_date": str(payload.get("snapshot_date", "") or "")[:40],
        "sections": cleaned_sections,
    }


def render_instruction(show_admin: bool = False) -> str:
    data = load_instruction_data()
    sections = data.get("sections", []) if isinstance(data, dict) else []
    section_html: list[str] = []
    nav_html: list[str] = []
    total_items = 0
    total_sections = 0

    for section_index, section in enumerate(sections if isinstance(sections, list) else [], start=1):
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id", "") or "")
        title = str(section.get("title", "") or "")
        items = section.get("items", [])
        if not isinstance(items, list):
            items = []

        item_html: list[str] = []
        for item_index, item in enumerate(items, start=1):
            text = str(item or "").strip()
            if not text:
                continue
            total_items += 1
            search_text = normalize_message(text).casefold()
            item_html.append(
                f'<article class="instruction-item" data-instruction-item data-search="{e(search_text)}" '
                f'>'
                f'<div class="instruction-item-index">{item_index:02d}</div>'
                f'<div class="instruction-item-text">{instruction_text_html(text)}</div>'
                f'<button class="instruction-copy" type="button" data-copy-instruction aria-label="Скопировать пункт" title="Скопировать">'
                f'<span aria-hidden="true">⧉</span></button>'
                f'</article>'
            )
        if not item_html:
            continue

        total_sections += 1
        nav_html.append(
            f'<a class="instruction-chip" data-instruction-chip="{e(section_id)}" '
            f'href="#instruction-{e(section_id)}"><span>{section_index:02d}</span>{e(title)}</a>'
        )
        section_search = normalize_message(f"{title} {' '.join(str(item or '') for item in items)}").casefold()
        section_html.append(
            f'<section class="instruction-section" id="instruction-{e(section_id)}" '
            f'data-instruction-section data-section-id="{e(section_id)}" data-search="{e(section_search)}" '
            f'>'
            f'<button class="instruction-section-head" type="button" data-instruction-toggle aria-expanded="true">'
            f'<span class="instruction-section-number">{section_index:02d}</span>'
            f'<span class="instruction-section-copy"><span class="instruction-kicker">Раздел {section_index}</span><strong>{e(title)}</strong></span>'
            f'<span class="instruction-section-count">{len(item_html)} пунктов</span>'
            f'<span class="instruction-chevron" aria-hidden="true">⌄</span>'
            f'</button>'
            f'<div class="instruction-section-body"><div class="instruction-section-body-inner">'
            f'<div class="instruction-items">{"".join(item_html)}</div>'
            f'</div></div></section>'
        )

    snapshot = str(data.get("snapshot_date", "") or "") if isinstance(data, dict) else ""
    source_url = str(data.get("source_url", INSTRUCTION_SOURCE_URL) or INSTRUCTION_SOURCE_URL) if isinstance(data, dict) else INSTRUCTION_SOURCE_URL
    if not source_url.startswith(("http://", "https://")):
        source_url = INSTRUCTION_SOURCE_URL

    snapshot_card = (
        f'<div class="instruction-stat"><span>Снимок</span><strong>{e(snapshot)}</strong></div>'
        if snapshot else
        '<div class="instruction-stat"><span>Источник</span><strong>Локальная копия</strong></div>'
    )
    empty = '' if section_html else '<section class="panel empty"><h2>Инструкция пока не загружена</h2><p>Проверьте файл instruction_data.json.</p></section>'
    content = f"""
      <section class="instruction-hero">
        <div class="instruction-orb instruction-orb-a" aria-hidden="true"></div>
        <div class="instruction-orb instruction-orb-b" aria-hidden="true"></div>
        <div class="instruction-hero-grid" aria-hidden="true"></div>
        <div class="instruction-hero-content">
          <div class="instruction-badge"><span class="instruction-badge-dot"></span>Внутренняя база знаний</div>
          <h1>Инструкция</h1>
          <p>Все рабочие памятки Service Desk в одном месте. Ищите по слову, номеру, БИН, НП, KEDEN или перевозке.</p>
          <div class="instruction-hero-actions">
            <button class="button instruction-focus-search" type="button" data-focus-instruction-search>Найти инструкцию</button>
            <a class="button ghost" href="{e(source_url)}" target="_blank" rel="noopener noreferrer">Исходная таблица ↗</a>
          </div>
        </div>
        <div class="instruction-stats" aria-label="Статистика инструкции">
          <div class="instruction-stat"><span>Разделов</span><strong>{total_sections}</strong></div>
          <div class="instruction-stat"><span>Пунктов</span><strong data-instruction-count>{total_items}</strong></div>
          {snapshot_card}
        </div>
      </section>

      <section class="instruction-toolbar" data-instruction-toolbar>
        <div class="instruction-search-wrap">
          <span class="instruction-search-icon" aria-hidden="true">⌕</span>
          <input id="instruction-search" type="search" placeholder="Поиск по инструкции..." autocomplete="off" data-instruction-search aria-label="Поиск по инструкции">
          <kbd class="instruction-search-key">/</kbd>
          <button class="instruction-search-clear" type="button" data-instruction-clear hidden aria-label="Очистить поиск">×</button>
        </div>
        <div class="instruction-toolbar-actions">
          <button class="instruction-mini-button" type="button" data-expand-all>Развернуть всё</button>
          <button class="instruction-mini-button" type="button" data-collapse-all>Свернуть всё</button>
        </div>
      </section>

      <nav class="instruction-chips" aria-label="Разделы инструкции">{"".join(nav_html)}</nav>
      <div class="instruction-search-status" data-instruction-search-status aria-live="polite"></div>
      <div class="instruction-list" data-instruction-list>{"".join(section_html)}{empty}</div>
      <div class="instruction-no-results" data-instruction-empty hidden>
        <div class="instruction-no-results-icon">⌕</div>
        <h2>Ничего не найдено</h2>
        <p>Попробуйте изменить запрос или очистить поиск.</p>
        <button class="button ghost" type="button" data-instruction-empty-clear>Очистить поиск</button>
      </div>
      <div class="instruction-toast" data-instruction-toast role="status" aria-live="polite">Пункт скопирован</div>

      <script>
        (() => {{
          const input = document.querySelector('[data-instruction-search]');
          const sections = [...document.querySelectorAll('[data-instruction-section]')];
          const chips = [...document.querySelectorAll('[data-instruction-chip]')];
          const counter = document.querySelector('[data-instruction-count]');
          const empty = document.querySelector('[data-instruction-empty]');
          const clearButton = document.querySelector('[data-instruction-clear]');
          const emptyClear = document.querySelector('[data-instruction-empty-clear]');
          const searchWrap = input?.closest('.instruction-search-wrap');
          const status = document.querySelector('[data-instruction-search-status]');
          const toast = document.querySelector('[data-instruction-toast]');
          if (!input) return;

          const normalize = (value) => String(value || '').toLocaleLowerCase('ru-RU').replace(/ё/g,'е').trim();
          let toastTimer = null;

          const setCollapsed = (section, collapsed) => {{
            section.classList.toggle('is-collapsed', collapsed);
            const button = section.querySelector('[data-instruction-toggle]');
            if (button) button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
          }};

          document.querySelectorAll('[data-instruction-toggle]').forEach((button) => {{
            button.addEventListener('click', () => {{
              const section = button.closest('[data-instruction-section]');
              if (section) setCollapsed(section, !section.classList.contains('is-collapsed'));
            }});
          }});

          document.querySelector('[data-expand-all]')?.addEventListener('click', () => sections.forEach((section) => setCollapsed(section, false)));
          document.querySelector('[data-collapse-all]')?.addEventListener('click', () => sections.forEach((section) => setCollapsed(section, true)));
          document.querySelector('[data-focus-instruction-search]')?.addEventListener('click', () => {{
            input.focus({{preventScroll:true}});
            document.querySelector('[data-instruction-toolbar]')?.scrollIntoView({{behavior:'smooth',block:'start'}});
          }});

          const clearSearch = () => {{
            input.value = '';
            input.dispatchEvent(new Event('input'));
            input.focus();
          }};
          clearButton?.addEventListener('click', clearSearch);
          emptyClear?.addEventListener('click', clearSearch);

          document.addEventListener('keydown', (event) => {{
            const target = event.target;
            const typing = target && (target.matches?.('input,textarea,select') || target.isContentEditable);
            if (event.key === '/' && !typing) {{
              event.preventDefault();
              input.focus();
            }}
            if (event.key === 'Escape' && document.activeElement === input && input.value) clearSearch();
          }});

          const apply = () => {{
            const query = normalize(input.value);
            const terms = query.split(/\\s+/).filter(Boolean);
            let visibleItems = 0;
            let visibleSections = 0;
            sections.forEach((section) => {{
              let sectionCount = 0;
              section.querySelectorAll('[data-instruction-item]').forEach((item) => {{
                const haystack = normalize((item.dataset.search || '') + ' ' + item.textContent);
                const show = !terms.length || terms.every((term) => haystack.includes(term));
                item.hidden = !show;
                item.classList.toggle('is-search-match', show && terms.length > 0);
                if (show) sectionCount += 1;
              }});
              section.hidden = sectionCount === 0;
              if (sectionCount) {{
                visibleSections += 1;
                if (terms.length) setCollapsed(section, false);
              }}
              visibleItems += sectionCount;
            }});
            if (counter) counter.textContent = String(visibleItems);
            if (empty) empty.hidden = visibleSections !== 0;
            if (clearButton) clearButton.hidden = !query;
            searchWrap?.classList.toggle('has-value', Boolean(query));
            if (status) status.textContent = query ? `Найдено: ${{visibleItems}} пунктов в ${{visibleSections}} разделах` : '';
          }};
          input.addEventListener('input', apply);

          chips.forEach((chip) => {{
            chip.addEventListener('click', () => {{
              const id = chip.dataset.instructionChip;
              const section = sections.find((node) => node.dataset.sectionId === id);
              if (!section) return;
              setCollapsed(section, false);
              section.classList.add('is-target');
              window.setTimeout(() => section.classList.remove('is-target'), 1100);
            }});
          }});

          if ('IntersectionObserver' in window) {{
            const observer = new IntersectionObserver((entries) => {{
              const visible = entries.filter((entry) => entry.isIntersecting).sort((a,b) => b.intersectionRatio - a.intersectionRatio)[0];
              if (!visible) return;
              const id = visible.target.dataset.sectionId;
              chips.forEach((chip) => chip.classList.toggle('is-active', chip.dataset.instructionChip === id));
            }}, {{rootMargin:'-22% 0px -58% 0px',threshold:[0,.15,.35]}});
            sections.forEach((section) => observer.observe(section));
          }}

          document.querySelectorAll('[data-copy-instruction]').forEach((button) => {{
            button.addEventListener('click', async () => {{
              const item = button.closest('[data-instruction-item]');
              const text = item?.querySelector('.instruction-item-text')?.innerText?.trim() || '';
              if (!text) return;
              try {{
                await navigator.clipboard.writeText(text);
                if (toast) {{
                  toast.classList.add('is-visible');
                  window.clearTimeout(toastTimer);
                  toastTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 1400);
                }}
              }} catch (_) {{}}
            }});
          }});

          apply();
        }})();
      </script>
    """
    return layout("Инструкция", content, "instruction", show_admin)

def render_dashboard(query: dict[str, list[str]], show_admin: bool = False) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    selected_status = query.get("status", [""])[0]
    selected_category = query.get("category", [""])[0]
    selected_priority = query.get("priority", [""])[0]
    selected_view = query.get("view", [""])[0]
    if selected_view not in {"", "mine", "handoff", "open", "urgent"}:
        selected_view = ""
    search = query.get("q", [""])[0]
    try:
        no_answer_minutes = max(0, min(10080, int(query.get("no_answer_minutes", ["0"])[0] or 0)))
    except ValueError:
        no_answer_minutes = 0
    current_employee = active_employee()
    if no_answer_minutes:
        all_filtered = STORE.list_tickets(
            selected_status, selected_category, selected_priority, search, 5000, 0, selected_view, current_employee,
        )
        all_filtered = [item for item in all_filtered if ticket_sla_state(item).get("active") and int(ticket_sla_state(item).get("elapsed_seconds",0) or 0) >= no_answer_minutes * 60]
        total = len(all_filtered)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = all_filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    else:
        total = STORE.count_tickets(
            selected_status, selected_category, selected_priority, search, selected_view, current_employee,
        )
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = STORE.list_tickets(
            selected_status,
            selected_category,
            selected_priority,
            search,
            PAGE_SIZE,
            (page - 1) * PAGE_SIZE,
            selected_view,
            current_employee,
        )
    counts = STORE.counts()
    shift_summary = STORE.shift_summary(current_employee)
    cards = "".join(
        f"""<a class="metric metric-{key}" href="/{'?status=' + key if key != 'all' else ''}">
          <span>{label}</span><strong data-count-status="{key}">{counts.get(key, 0)}</strong>
        </a>"""
        for key, label in [
            ("all", "Всего"),
            ("new", "Не тронуты"),
            ("in_progress", "В работе"),
            ("done", "Сделано"),
            ("invalid", "Недействительные"),
        ]
    )
    shift_views = [
        ("mine", "Мои заявки", int(shift_summary.get("mine", 0) or 0)),
        ("handoff", "От предыдущей смены", int(shift_summary.get("handoff", 0) or 0)),
        ("open", "Незакрытые", int(shift_summary.get("open", 0) or 0)),
        ("urgent", "Срочные", int(shift_summary.get("urgent", 0) or 0)),
    ]
    shift_cards = "".join(
        f'<a class="shift-metric {"active" if selected_view == key else ""}" href="/?view={key}"><span>{e(label)}</span><strong>{amount}</strong></a>'
        for key, label, amount in shift_views
    )
    previous_employee = str(shift_summary.get("previous_employee", "") or "")
    shift_note = f'<small>Предыдущая смена: {e(previous_employee)}</small>' if previous_employee else '<small>Передача начнёт заполняться при смене сотрудника</small>'
    status_options = '<option value="">Все статусы</option>' + "".join(
        f'<option value="{e(key)}" {"selected" if key == selected_status else ""}>{e(label)}</option>'
        for key, label in STATUSES.items()
    )
    table_body = render_ticket_rows(tickets)
    handoff_tickets = [item for item in STORE.list_tickets("", "", "", "", 500, 0) if int(item.get("shift_handoff", 0) or 0)][:20]
    if handoff_tickets:
        handoff_items = "".join(
            f'<a class="handoff-item" href="/ticket?id={int(item["id"])}"><strong>#{int(item["id"])} · {e(str(item.get("title", "Заявка")))}</strong><span>{e(str(item.get("sender", "")))}</span><small>Передал: {e(str(item.get("handoff_from", "")))} · {e(human_time(str(item.get("handoff_at", ""))))}</small></a>'
            for item in handoff_tickets
        )
        handoff_board = f'<section class="panel handoff-board"><div class="section-heading"><div><p class="eyebrow">Передача смены</p><h2>Переданные заявки <span class="handoff-badge large">{len(handoff_tickets)}</span></h2><p>Отдельный список заявок, которые ждут следующую смену</p></div></div><div class="handoff-list">{handoff_items}</div></section>'
    else:
        handoff_board = ""
    content = f"""
      <section class="page-heading">
        <div><p class="eyebrow">Service Desk</p><h1>Заявки</h1><p>WhatsApp-запросы и ручные заявки из Telegram в одной очереди</p></div>
        <a class="button primary" href="/whatsapp">Открыть WhatsApp</a>
      </section>
      {notice_html}
      <section class="metrics">{cards}</section>
      <section class="smart-shift-panel"><div><strong>Текущая смена: {e(current_employee)}</strong>{shift_note}</div><div class="shift-metrics">{shift_cards}</div></section>
      {handoff_board}
      <section class="panel filters-panel">
        <form class="filters" method="get" action="/" data-dashboard-filter-form>
          <input type="hidden" name="view" value="{e(selected_view)}">
          <input type="search" name="q" value="{e(search)}" placeholder="№ заявки, ФИО, телефон, пост, БИН, ТД, текст..." data-live-ticket-search autocomplete="off">
          <select name="status">{status_options}</select>
          <select name="category">{category_options(selected_category, True)}</select>
          <select name="priority">{priority_options(selected_priority, True)}</select>
          <input type="number" name="no_answer_minutes" value="{no_answer_minutes or ''}" min="0" max="10080" step="5" placeholder="Без ответа > N мин" title="Показать заявки без первого ответа дольше N минут">
          <button class="button" type="submit">Применить</button>
          <a class="button ghost" href="/">Сбросить</a>
        </form>
      </section>
      <section class="panel table-panel" data-dashboard data-dashboard-version="{e(STORE.dashboard_version())}">
        <div class="table-hint"><span>Приоритет меняется в левом столбце. Правой кнопкой мыши по заявке можно быстро изменить статус.</span><span id="auto-refresh-state">Автообновление включено</span></div>
        <div class="table-scroll"><table>
          <thead><tr><th>Приоритет</th><th>№</th><th>Источник</th><th>Отправитель</th><th>Запрос</th><th>Статус</th><th>Время Алматы</th></tr></thead>
          <tbody id="ticket-table-body">{table_body}</tbody>
        </table></div>
        <div class="pagination" id="ticket-pagination">{render_pagination(query, total, page)}</div>
      </section>
      <section class="panel manual-panel">
        <div><p class="eyebrow">Telegram вручную</p><h2>Быстро добавить запрос</h2><p>Для заявок, которые сотрудник переносит из Telegram вручную</p></div>
        <form class="manual-form" method="post" action="/manual">
          <label>Тип запроса<input name="title" value="Открытие НП" required></label>
          <label>Номер пломбы / перевозки<input name="reference" placeholder="Например, 784512"></label>
          <label class="wide">Комментарий<textarea name="comment" rows="2" placeholder="Дополнительная информация"></textarea></label>
          <label>Приоритет<select name="priority">{priority_options('normal')}</select></label>
          <label>Сотрудник<select name="employee">{employee_options()}</select></label>
          <label>Начальный статус<select name="status"><option value="new">Не сделано</option><option value="done">Сделано</option></select></label>
          <button class="button primary" type="submit">Добавить</button>
        </form>
      </section>
      <div id="ticket-context-menu" class="context-menu" hidden>
        <div class="context-heading"><small>Изменить статус</small><strong id="context-ticket-title">Заявка</strong></div>
        <button type="button" data-quick-status="new"><span class="context-dot dot-new"></span>Не тронута</button>
        <button type="button" data-quick-status="in_progress"><span class="context-dot dot-in_progress"></span>В работе</button>
        <button type="button" data-quick-status="done"><span class="context-dot dot-done"></span>Сделано</button>
        <button type="button" data-quick-status="invalid"><span class="context-dot dot-invalid"></span>Недействительная</button>
        <button type="button" data-quick-handoff="1"><span class="context-dot dot-handoff"></span>Передать следующей смене</button>
      </div>
    """
    return layout("Заявки", content, show_admin=show_admin)


def render_ticket(
    ticket_id: int,
    query: dict[str, list[str]],
    show_admin: bool = False,
) -> str:
    ticket = STORE.get_ticket(ticket_id)
    if not ticket:
        return layout(
            "Не найдено",
            '<section class="panel empty">Заявка не найдена</section>',
            show_admin=show_admin,
        )
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    events = STORE.ticket_events(ticket_id)
    event_rows = "".join(
        f'<li><span>{e(event["action"])}</span><small>{e(event["actor"])} · {e(human_time(event["created_at"]))}</small></li>'
        for event in events
    )
    bin_details = ""
    if ticket["category"] == "bin":
        bin_details = f"""
          <div class="detail-grid bin-grid">
            <div><span>Старый БИН</span><strong>{e(ticket['bin_old'])}</strong></div>
            <div><span>Новый БИН</span><strong>{e(ticket['bin_new'])}</strong></div>
            <div><span>Номер АТС</span><strong>{e(ticket['ats_number'])}</strong></div>
            <div><span>Компания</span><strong>{e(ticket['company'])}</strong></div>
            <div><span>Страна</span><strong>{e(ticket['country'])}</strong></div>
          </div>"""

    chat_id = str(ticket.get("chat_id", "") or "").strip()
    if not chat_id:
        phone_digits = re.sub(r"\D", "", normalize_phone(str(ticket.get("phone", ""))))
        chat_id = f"{phone_digits}@c.us" if phone_digits else ""

    attachment = ""
    external_id = str(ticket.get("external_id", "") or "")
    media_items = STORE.list_ticket_whatsapp_media(ticket_id, chat_id, external_id, 24)
    if media_items:
        media_cards: list[str] = []
        for media_item in media_items:
            message_key = str(media_item.get("message_key", "") or "")
            if not message_key:
                continue
            media_url = f"/api/chat-media?chat_id={quote(str(media_item.get('chat_id', chat_id) or chat_id))}&message_id={quote(message_key)}"
            mime = str(media_item.get("media_mime", "") or "")
            name = str(media_item.get("media_name", "") or "Вложение")
            sender_name = str(media_item.get("sender", "") or ("Рабочий WhatsApp" if media_item.get("from_me") else ticket.get("sender", "Пользователь")))
            when = ""
            try:
                when = datetime.fromtimestamp(int(media_item.get("message_timestamp", 0) or 0), tz=timezone.utc).astimezone(ALMATY_TIMEZONE).strftime("%d.%m %H:%M")
            except (ValueError, TypeError, OSError):
                when = ""
            if mime.startswith("image/"):
                preview = (
                    f'<button class="media-zoom-button" type="button" data-media-zoom="{e(media_url)}" '
                    f'data-media-name="{e(name)}"><img class="ticket-media-image" src="{e(media_url)}" alt="{e(name)}"></button>'
                )
            elif mime.startswith("audio/"):
                preview = f'<audio class="ticket-media-audio" controls preload="metadata" src="{e(media_url)}"></audio>'
            elif mime.startswith("video/"):
                preview = f'<video class="ticket-media-video" controls preload="metadata" src="{e(media_url)}"></video>'
            else:
                preview = (
                    f'<div class="media-file-actions"><a class="button" target="_blank" href="{e(media_url)}">Открыть</a>'
                    f'<a class="button ghost" href="{e(media_url + "&download=1")}">Скачать</a></div>'
                )
            transcript = str(media_item.get("transcript", "") or "") if mime.startswith("audio/") else ""
            transcript_html = f'<div class="media-transcript"><strong>Расшифровка:</strong><p>{e(transcript)}</p></div>' if transcript else ""
            meta = " · ".join(part for part in (sender_name, when) if part)
            media_cards.append(
                f'<article class="ticket-media-card"><div class="attachment-name">{e(name)}</div>'
                f'<small class="ticket-media-meta">{e(meta)}</small>{preview}{transcript_html}</article>'
            )
        if media_cards:
            attachment = (
                '<div class="ticket-media-block"><h3>Файлы и медиа по заявке</h3>'
                '<p class="muted">Все медиа, связанные с этой заявкой, доступны прямо здесь. Фото открываются поверх текущей страницы.</p>'
                f'<div class="ticket-media-grid">{"".join(media_cards)}</div></div>'
            )
    elif ticket.get("attachment_name"):
        attachment = f'<div class="attachment">Вложение: {e(ticket["attachment_name"])}. Медиафайл не был сохранён старой версией системы.</div>'

    terminal_status = str(ticket["status"]) in {"done", "invalid"}
    if terminal_status and not show_admin:
        status_buttons = (
            '<div class="notice warning-notice status-lock-note">'
            'Заявка отработана. Изменить её статус может только администратор.'
            '</div>'
        )
    else:
        status_buttons = "".join(
            f'<button name="status" value="{e(key)}" class="status-action action-{e(key)}" type="submit">{e(label)}</button>'
            for key, label in STATUSES.items()
        )

    chat_button = (
        f'<a class="button whatsapp-button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат WhatsApp</a>'
        if chat_id
        else '<span class="button disabled" title="WhatsApp не передал телефонный номер">Номер для связи не определён</span>'
    )

    sla_state = ticket_sla_state(ticket)
    if sla_state.get("active"):
        sla_panel = f'''
          <section class="panel sla-panel {'sla-panel-overdue' if sla_state.get('overdue') else ''}">
            <h2>SLA реакции</h2>
            <div>{sla_badge(ticket)}</div>
            <p class="muted">Лимит для приоритета «{e(PRIORITIES.get(str(ticket.get('priority','normal')), str(ticket.get('priority','normal'))))}»: {int(sla_state.get('limit_minutes',0) or 0)} мин.</p>
          </section>'''
    else:
        first_response = parse_iso_datetime(ticket.get("first_response_at"))
        created = parse_iso_datetime(ticket.get("created_at"))
        if first_response and created:
            reacted_minutes = max(0, int((first_response - created).total_seconds() // 60))
            sla_panel = f'<section class="panel sla-panel"><h2>SLA реакции</h2><strong>Реакция через {reacted_minutes} мин.</strong></section>'
        else:
            sla_panel = ""
    handoff_enabled = bool(int(ticket.get("shift_handoff", 0) or 0))
    if terminal_status:
        handoff_panel = ""
    elif handoff_enabled:
        handoff_panel = f"""
          <section class="panel handoff-panel">
            <h2>Передача смене</h2>
            <div class="notice warning-notice">Передано следующей смене: {e(ticket.get('handoff_from', ''))}<br><small>{e(human_time(str(ticket.get('handoff_at', ''))))}</small></div>
            <form method="post" action="/handoff">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <button class="button primary" name="action" value="accept" type="submit">Принять со смены</button>
            </form>
          </section>"""
    else:
        handoff_panel = f"""
          <section class="panel handoff-panel">
            <h2>Передача смене</h2>
            <p class="muted">Если не успеваете закончить заявку, отметьте её для следующей смены.</p>
            <form method="post" action="/handoff">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <button class="button" name="action" value="transfer" type="submit">Передать следующей смене</button>
            </form>
          </section>"""

    handoff_badge = '<span class="handoff-badge large">Следующая смена</span>' if handoff_enabled else ""
    admin_delete_panel = ""
    if show_admin:
        admin_delete_panel = f"""
          <section class="panel danger-panel">
            <h2>Администратор</h2>
            <p class="muted">Удаление происходит только внутри системы. Пользователю ничего не отправляется.</p>
            <form method="post" action="/admin/ticket-delete" onsubmit="return confirm('Удалить заявку №{ticket['id']}? Пользователь не получит уведомление.')">
              <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <button class="button danger" type="submit">Удалить заявку</button>
            </form>
          </section>"""
    content = f"""
      <a class="back" href="/">← Все заявки</a>
      {notice_html}
      <section class="ticket-header">
        <div><p class="eyebrow">Заявка #{ticket['id']}</p><h1>{e(ticket['title'])}</h1><p>{e(ticket['summary'])}</p></div>
        <div class="ticket-badges">{priority_badge(ticket['priority'])}{status_badge(ticket['status'])}{handoff_badge}</div>
      </section>
      <div class="ticket-layout">
        <section class="panel ticket-main">
          <div class="ticket-main-heading"><h2>Данные запроса</h2>{chat_button}</div>
          
          <div class="detail-grid">
            <div><span>Источник</span><strong>{e(ticket['source'])}</strong></div>
            <div><span>Отправитель</span><strong>{e(ticket['sender'])}</strong></div>
            <div><span>Телефон</span><strong>{e(display_phone(ticket['phone']))}</strong></div>
            <div><span>Категория</span><strong>{e(CATEGORIES.get(ticket['category'], ticket['category']))}</strong></div>
            <div><span>Приоритет</span><strong>{e(PRIORITIES.get(ticket['priority'], ticket['priority']))}</strong></div>
            <div><span>Создана</span><strong>{e(human_time(ticket['created_at']))}</strong></div>
          </div>
          {bin_details}
          <h3>Исходное сообщение</h3>
          <pre class="message">{e(ticket['original_text'] or ticket['summary'])}</pre>
          {attachment}
        </section>
        <aside>
          <section class="panel actions-panel">
            <h2>Изменить статус</h2>
            <form method="post" action="/status">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <label>Сотрудник<select name="employee">{employee_options(ticket['assigned_to'])}</select></label>
              <div class="status-actions">{status_buttons}</div>
            </form>
          </section>
          {sla_panel}
          {handoff_panel}
          <section class="panel classification-panel">
            <h2>Приоритет</h2>
            <form method="post" action="/classification">
              <input type="hidden" name="ticket_id" value="{ticket['id']}">
              <label>Приоритет<select name="priority">{priority_options(ticket['priority'])}</select></label>
              <button class="button" type="submit">Сохранить</button>
            </form>
          </section>
          {admin_delete_panel}
          <section class="panel history-panel"><h2>История заявки</h2><ul>{event_rows}</ul></section>
        </aside>
      </div>
    """
    return layout(f"Заявка #{ticket_id}", content, show_admin=show_admin)


def admin_tabs(active: str) -> str:
    items = [
        ("home", "/admin/", "Обзор"),
        ("employees", "/admin/employees", "Сотрудники"),
        ("shift", "/admin/shift", "Объявление о смене"),
        ("manual", "/admin/manual", "Ручная заявка"),
        ("menu", "/admin/menu", "Меню обращений"),
        ("categories", "/admin/categories", "Категории"),
        ("errors", "/admin/errors", "Ошибки"),
        ("contacts", "/admin/contacts", "Контакты WhatsApp"),
        ("audit", "/admin/audit", "Журнал"),
        ("analytics", "/admin/analytics", "Аналитика"),
        ("backups", "/admin/backups", "Бэкапы"),
        ("workflow", "/admin/workflow", "Хранение / процессы"),
        ("reliability", "/admin/reliability", "Надёжность"),
        ("system", "/admin/system", "Система"),
    ]
    return '<nav class="admin-tabs">' + "".join(
        f'<a class="admin-tab {"active" if key == active else ""}" href="{href}">{e(label)}</a>'
        for key, href, label in items
    ) + "</nav>"


def admin_layout(title: str, content: str, active: str) -> str:
    heading = """
      <section class="page-heading admin-heading">
        <div><p class="eyebrow">Управление системой</p><h1>Админка</h1><p>Сотрудники, контакты, группы и настройки обращений</p></div>
      </section>
    """
    return layout(title, heading + admin_tabs(active) + content, "admin", True)


def render_admin_home() -> str:
    contacts = len(STORE.list_manual_whatsapp_contacts())
    groups = len(STORE.list_whatsapp_groups())
    error_counts = STORE.error_report_counts()
    open_errors = int(error_counts.get("new", 0)) + int(error_counts.get("in_progress", 0))
    cards = f"""
      <section class="admin-card-grid">
        <a class="panel admin-card" href="/admin/employees"><span>Сотрудники</span><strong>{len(EMPLOYEES)}</strong><small>Имена и количество сотрудников</small></a>
        <a class="panel admin-card" href="/admin/manual"><span>Ручная заявка</span><strong>+</strong><small>Создать запрос от имени сотрудника</small></a>
        <a class="panel admin-card" href="/admin/menu"><span>Меню обращений</span><strong>{len(enabled_menu_options())}</strong><small>Названия, подсказки и видимые пункты</small></a>
        <a class="panel admin-card" href="/admin/categories"><span>Категории</span><strong>{len(active_ticket_category_items())}</strong><small>Добавить, убрать или переименовать виды заявок</small></a>
        <a class="panel admin-card" href="/admin/errors"><span>Ошибки</span><strong>{open_errors}</strong><small>Отдельные репорты пользователей, не являющиеся заявками</small></a>
        <a class="panel admin-card" href="/admin/contacts"><span>Контакты WhatsApp</span><strong>{contacts}</strong><small>Добавление пользователя по номеру</small></a>
        <a class="panel admin-card" href="/groups"><span>Группы WhatsApp</span><strong>{groups}</strong><small>Общая переписка и отправка сообщений</small></a>
        <a class="panel admin-card" href="/admin/audit"><span>Журнал</span><strong>≡</strong><small>Действия сотрудников, сообщения и ошибки</small></a>
        <a class="panel admin-card" href="/admin/analytics"><span>Аналитика</span><strong>↗</strong><small>Категории, сотрудники и время обработки</small></a>
        <a class="panel admin-card" href="/admin/backups"><span>Бэкапы</span><strong>⟳</strong><small>Создание, проверка и восстановление базы</small></a>
        <a class="panel admin-card" href="/admin/workflow"><span>Хранение / процессы</span><strong>⚙</strong><small>Сроки хранения, очистка медиа и правила закрытия</small></a>
        <a class="panel admin-card" href="/admin/reliability"><span>Надёжность</span><strong>🛡</strong><small>Диагностика, очередь ошибок, бэкапы и обслуживание</small></a>
        <a class="panel admin-card" href="/admin/system"><span>Система</span><strong>●</strong><small>Состояние, очередь отправки и хранилище</small></a>
      </section>
      <section class="panel integration-status">
        <h2>{'Локальный тестовый режим' if LOCAL_MODE else 'Постоянный доступ администратора'}</h2>
        <div><span>Логин администратора</span><strong>{e(ADMIN_USER)}</strong></div>
        <p class="muted">{'На Windows локальный доступ разрешён только с этого компьютера; Nginx и серверный пароль не используются.' if LOCAL_MODE else 'Права хранятся в серверной конфигурации, а пароль в защищённом файле Nginx. Обновление программы их не удаляет.'}</p>
      </section>
    """
    return admin_layout("Админка", cards, "home")




ERROR_REPORT_STATUS_LABELS = {
    "new": "Новая",
    "in_progress": "В работе",
    "resolved": "Исправлена",
    "rejected": "Отклонена",
}


def error_report_status_badge(status: str) -> str:
    label = ERROR_REPORT_STATUS_LABELS.get(status, status or "Новая")
    css = {
        "new": "status-new",
        "in_progress": "status-in_progress",
        "resolved": "status-done",
        "rejected": "status-invalid",
    }.get(status, "status-new")
    return f'<span class="status {css}">{e(label)}</span>'


def render_admin_errors(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    selected_status = query.get("status", [""])[0]
    search = query.get("q", [""])[0]
    reports = STORE.list_error_reports(selected_status, search, 300)
    rows: list[str] = []
    for report in reports:
        report_id = int(report.get("id", 0) or 0)
        description = normalize_message(str(report.get("description", "")))
        sender = str(report.get("sender", "") or "Неизвестный пользователь")
        phone = display_phone(str(report.get("phone", ""))) if report.get("phone") else "—"
        attachment = str(report.get("attachment_name", "") or "")
        rows.append(
            f'<tr><td><a class="ticket-title-link" href="/admin/error?id={report_id}">#{report_id}</a></td>'
            f'<td>{error_report_status_badge(str(report.get("status", "new")))}</td>'
            f'<td><strong>{e(sender)}</strong><small>{e(phone)}</small></td>'
            f'<td><strong>{e(description[:150] or "Без текста")}</strong>'
            f'<small>{e(("Вложение: " + attachment) if attachment else "")}</small></td>'
            f'<td>{e(human_time(str(report.get("created_at", ""))) or "—")}</td></tr>'
        )
    table_rows = "".join(rows) or '<tr><td colspan="5" class="empty">Репортов по этому фильтру нет</td></tr>'
    options = '<option value="">Все статусы</option>' + "".join(
        f'<option value="{key}" {"selected" if selected_status == key else ""}>{e(label)}</option>'
        for key, label in ERROR_REPORT_STATUS_LABELS.items()
    )
    content = f'''\
      {notice_html}
      <section class="panel table-panel">
        <div class="section-heading"><div><p class="eyebrow">Отдельно от заявок</p><h2>Репорты об ошибках</h2><p>Сообщения пользователей о багах системы. Они не участвуют в SLA и не попадают в список заявок.</p></div></div>
        <form class="filters" method="get" action="/admin/errors">
          <input type="search" name="q" value="{e(search)}" placeholder="Текст, имя, телефон, номер репорта...">
          <select name="status">{options}</select>
          <button class="button" type="submit">Найти</button><a class="button ghost" href="/admin/errors">Сбросить</a>
        </form>
        <div class="table-scroll"><table><thead><tr><th>ID</th><th>Статус</th><th>Пользователь</th><th>Описание</th><th>Создан</th></tr></thead><tbody>{table_rows}</tbody></table></div>
      </section>
    '''
    return admin_layout("Ошибки", content, "errors")


def render_admin_error(report_id: int, query: dict[str, list[str]]) -> str:
    report = STORE.get_error_report(report_id)
    if not report:
        return admin_layout("Ошибка не найдена", '<section class="panel empty"><h2>Репорт не найден</h2><a class="button" href="/admin/errors">К списку ошибок</a></section>', "errors")
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    chat_id = str(report.get("chat_id", "") or "")
    external_id = str(report.get("external_id", "") or "")
    message = STORE.get_whatsapp_message(chat_id, external_id) if chat_id and external_id else None
    attachment_html = ""
    if message and str(message.get("media_path", "") or ""):
        media_url = f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(external_id)}"
        mime = str(message.get("media_mime", "") or "")
        name = str(message.get("media_name", "") or report.get("attachment_name", "") or "Вложение")
        if mime.startswith("image/"):
            attachment_html = f'<div class="panel"><h3>Вложение</h3><a href="{e(media_url)}" target="_blank" rel="noopener"><img src="{e(media_url)}" alt="{e(name)}" style="max-width:720px;max-height:520px;width:auto;height:auto;border-radius:12px"></a><p>{e(name)}</p></div>'
        else:
            attachment_html = f'<div class="panel"><h3>Вложение</h3><a class="button" href="{e(media_url)}" target="_blank" rel="noopener">Открыть / скачать {e(name)}</a></div>'
    elif report.get("attachment_name"):
        attachment_html = f'<div class="panel"><h3>Вложение</h3><p>{e(str(report.get("attachment_name", "")))}</p><p class="muted">Файл указан в репорте, но локальная копия сейчас недоступна.</p></div>'
    chat_link = f'<a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат WhatsApp</a>' if chat_id else ""
    status_options = "".join(
        f'<option value="{key}" {"selected" if str(report.get("status", "new")) == key else ""}>{e(label)}</option>'
        for key, label in ERROR_REPORT_STATUS_LABELS.items()
    )
    content = f'''\
      {notice_html}
      <section class="section-heading"><div><p class="eyebrow">Репорт #{int(report.get("id", 0) or 0)}</p><h2>Ошибка пользователя</h2><p>{e(human_time(str(report.get("created_at", ""))))}</p></div>{chat_link}</section>
      <section class="panel ticket-details">
        <div><span>Статус</span><strong>{error_report_status_badge(str(report.get("status", "new")))}</strong></div>
        <div><span>Пользователь</span><strong>{e(str(report.get("sender", "") or "Неизвестный"))}</strong></div>
        <div><span>Телефон</span><strong>{e(display_phone(str(report.get("phone", ""))) if report.get("phone") else "—")}</strong></div>
        <div><span>Описание</span><strong style="white-space:pre-wrap">{e(str(report.get("description", "") or "Без текста"))}</strong></div>
      </section>
      {attachment_html}
      <section class="panel">
        <div class="section-heading"><div><h2>Обработка репорта</h2><p>Статус и служебная заметка видны только внутри «Единой очереди».</p></div></div>
        <form class="manual-form" method="post" action="/admin/errors">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <input type="hidden" name="report_id" value="{int(report.get("id", 0) or 0)}">
          <label>Статус<select name="status">{status_options}</select></label>
          <label class="wide">Заметка<textarea name="admin_note" rows="4" maxlength="3000" placeholder="Что проверили / как исправили">{e(str(report.get("admin_note", "") or ""))}</textarea></label>
          <button class="button primary" type="submit">Сохранить</button>
          <a class="button ghost" href="/admin/errors">К списку ошибок</a>
        </form>
      </section>
    '''
    return admin_layout(f"Ошибка #{report_id}", content, "errors")

def render_admin_audit(query: dict[str, list[str]]) -> str:
    level = query.get("level", [""])[0]
    search = query.get("q", [""])[0]
    entries = STORE.list_audit_entries(300, level, search)
    rows: list[str] = []
    for item in entries:
        level_name = {"info": "Инфо", "warning": "Внимание", "error": "Ошибка"}.get(str(item.get("level", "")), str(item.get("level", "")))
        obj = " · ".join(part for part in [str(item.get("object_type", "")), str(item.get("object_id", ""))] if part)
        rows.append(
            f'<tr class="audit-{e(str(item.get("level","info")))}"><td>{e(human_time(str(item.get("created_at",""))))}</td>'
            f'<td><span class="audit-level">{e(level_name)}</span></td><td>{e(str(item.get("actor","") or "Система"))}</td>'
            f'<td><strong>{e(str(item.get("action","")))}</strong><small>{e(str(item.get("details","") or ""))}</small></td>'
            f'<td>{e(obj or "—")}</td></tr>'
        )
    audit_rows = "".join(rows) or '<tr><td colspan="5" class="empty">Записей по этому фильтру нет</td></tr>'
    monitor_errors = [line for line in monitor_log_entries(100) if " ERROR " in f" {line} " or " WARN " in f" {line} "][:30]
    monitor_html = "".join(f'<li><code>{e(line)}</code></li>' for line in monitor_errors) or '<li class="empty">Свежих системных ошибок нет</li>'
    levels = '<option value="">Все уровни</option>' + "".join(
        f'<option value="{key}" {"selected" if level == key else ""}>{label}</option>'
        for key, label in [("info","Инфо"),("warning","Внимание"),("error","Ошибка")]
    )
    content = f'''
      <section class="panel audit-panel">
        <div class="section-heading"><div><p class="eyebrow">Аудит</p><h2>Журнал действий и ошибок</h2><p>Кто менял заявки, отправлял сообщения, переключал автоответчик и какие ошибки возникали</p></div></div>
        <form class="filters audit-filters" method="get" action="/admin/audit">
          <input type="search" name="q" value="{e(search)}" placeholder="Сотрудник, действие, № заявки, чат...">
          <select name="level">{levels}</select><button class="button" type="submit">Найти</button><a class="button ghost" href="/admin/audit">Сбросить</a>
        </form>
        <div class="table-scroll"><table class="audit-table"><thead><tr><th>Время</th><th>Уровень</th><th>Кто</th><th>Действие</th><th>Объект</th></tr></thead><tbody>{audit_rows}</tbody></table></div>
      </section>
      <section class="panel history-panel"><h2>Системные предупреждения</h2><ul>{monitor_html}</ul></section>
    '''
    return admin_layout("Журнал", content, "audit")


def render_admin_system(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    state = system_status_snapshot()
    connector = state["connector"]
    database = state["database"]
    queue = state["queue"]
    counts = queue.get("counts", {}) if isinstance(queue, dict) else {}
    disk_total = int(state.get("disk_total", 0) or 0)
    disk_free = int(state.get("disk_free", 0) or 0)
    disk_percent = int(round((disk_free / disk_total) * 100)) if disk_total else 0
    connector_ok = bool(connector.get("connected")) if isinstance(connector, dict) else False
    connector_status = str(connector.get("status", "offline")) if isinstance(connector, dict) else "offline"
    heartbeat_age = state.get("heartbeat_age")
    local_monitor = state.get("monitor_mode") == "local"
    heartbeat_text = (
        "не используется в локальном Windows-режиме"
        if local_monitor
        else ("нет данных" if heartbeat_age is None else ("только что" if int(heartbeat_age) < 60 else f"{int(heartbeat_age)//60} мин назад"))
    )
    monitor_label = "Локальный режим" if local_monitor else ("Активно" if state.get("monitor_ok") else "Нет свежего сигнала")
    cards = f"""
      <section class="system-status-grid">
        <div class="panel system-card"><span>Сайт</span><strong class="system-ok">Работает</strong><small>HTTP-приложение отвечает</small></div>
        <div class="panel system-card"><span>WhatsApp</span><strong class="{'system-ok' if connector_ok else 'system-bad'}">{e('Подключён' if connector_ok else connector_status)}</strong><small>Последний сигнал: {e(str(connector.get('updated_at',''))[:19] if isinstance(connector,dict) else '')}</small></div>
        <div class="panel system-card"><span>База данных</span><strong class="{'system-ok' if database.get('ok') else 'system-bad'}">{e('OK' if database.get('ok') else 'Ошибка')}</strong><small>{e(str(database.get('result','')))}</small></div>
        <div class="panel system-card"><span>Автовосстановление</span><strong class="{'system-ok' if state.get('monitor_ok') else 'system-warn'}">{e(monitor_label)}</strong><small>{e('На Windows watchdog отключён' if local_monitor else 'Проверка: ' + heartbeat_text)}</small></div>
      </section>
    """
    queue_rows = []
    status_labels = {"pending":"ожидает", "processing":"отправляется", "sent":"отправлено", "failed":"ошибка", "sending":"отправляется", "uncertain":"проверьте переписку"}
    for row in queue.get("rows", []) if isinstance(queue, dict) else []:
        status = str(row.get("status", ""))
        target = str(row.get("chat_id") or row.get("phone") or "")
        err = str(row.get("last_error") or "")
        queue_rows.append(
            f"<tr><td>#{int(row.get('id',0) or 0)}</td><td>{e(status_labels.get(status,status))}</td>"
            f"<td>{int(row.get('attempt_count',0) or 0)}/{int(row.get('max_attempts',4) or 4)}</td>"
            f"<td>{e(target[:34])}</td><td>{e(err[:120] or '—')}</td><td>{e(human_time(str(row.get('updated_at') or row.get('created_at') or '')))}</td></tr>"
        )
    queue_table = "".join(queue_rows) or '<tr><td colspan="6" class="empty">Очередь пока пуста</td></tr>'
    logs = "".join(f"<li><code>{e(line)}</code></li>" for line in state.get("logs", [])) or '<li class="empty">Журнал мониторинга пока пуст</li>'
    content = f"""
      {notice_html}
      {cards}
      <section class="panel system-storage">
        <div class="section-heading"><div><h2>Хранилище</h2><p>Свободно {human_bytes(disk_free)} из {human_bytes(disk_total)} ({disk_percent}%)</p></div></div>
        <div class="system-metrics"><div><span>База</span><strong>{human_bytes(int(state.get('db_size',0) or 0))}</strong></div><div><span>Медиа чатов</span><strong>{human_bytes(int(state.get('media_size',0) or 0))}</strong></div><div><span>Исходящие файлы</span><strong>{human_bytes(int(state.get('outbound_media_size',0) or 0))}</strong></div><div><span>Хранение</span><strong>{RETENTION_DAYS} дней</strong></div></div>
        <form class="system-actions" method="post" action="/admin/system"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="cleanup"><button class="button" type="submit" onclick="return confirm('Запустить очистку старых данных сейчас?')">Очистить сейчас</button></form>
      </section>
      <section class="panel table-panel">
        <div class="section-heading"><div><h2>Очередь отправки</h2><p>Ожидает: {int(counts.get('pending',0) or 0)} · Отправляется: {int(counts.get('processing',0) or 0) + int(counts.get('sending',0) or 0)} · Отправлено: {int(counts.get('sent',0) or 0)} · Требует проверки: {int(counts.get('uncertain',0) or 0)} · Ошибка: {int(counts.get('failed',0) or 0)}</p></div>
        <form method="post" action="/admin/system"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="retry_failed"><button class="button" type="submit">Повторить ошибки</button></form></div>
        <div class="table-scroll"><table><thead><tr><th>ID</th><th>Статус</th><th>Попытки</th><th>Получатель</th><th>Ошибка</th><th>Обновлено</th></tr></thead><tbody>{queue_table}</tbody></table></div>
      </section>
      <section class="panel system-log"><div class="section-heading"><div><h2>Журнал мониторинга</h2><p>Последние проверки и автоматические перезапуски</p></div></div><ul>{logs}</ul></section>
    """
    return admin_layout("Система", content, "system")


def admin_employee_count(query: dict[str, list[str]]) -> int:
    try:
        requested = int(query.get("count", [str(len(EMPLOYEES))])[0])
    except ValueError:
        requested = len(EMPLOYEES)
    return max(1, min(MAX_EMPLOYEES, requested))


def render_admin_employees(query: dict[str, list[str]]) -> str:
    count = admin_employee_count(query)
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""

    proposed = list(EMPLOYEES[:count])
    while len(proposed) < count:
        proposed.append(f"Сотрудник {len(proposed) + 1}")

    fields = "".join(
        f'<label>Сотрудник {index}'
        f'<input name="employee_{index}" value="{e(name)}" maxlength="60" required>'
        f'</label>'
        for index, name in enumerate(proposed, start=1)
    )

    content = f"""
      <section class="section-heading"><div><h2>Сотрудники</h2><p>Количество, имена и список выбора сотрудника на смене</p></div></section>
      {notice_html}
      <section class="panel manual-panel">
        <div>
          <h2>Количество сотрудников</h2>
          <p>Сейчас в системе: <strong>{len(EMPLOYEES)}</strong></p>
        </div>
        <form class="manual-form" method="get" action="/admin/employees">
          <label>Количество
            <input type="number" name="count" value="{count}" min="1" max="{MAX_EMPLOYEES}" required>
          </label>
          <button class="button" type="submit">Применить количество</button>
        </form>
      </section>
      <section class="panel">
        <h2>Имена сотрудников</h2>
        <form class="manual-form" method="post" action="/admin/employees">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <input type="hidden" name="employee_count" value="{count}">
          {fields}
          <button class="button primary" type="submit">Сохранить</button>
        </form>
        <p class="muted">Новые имена применяются к спискам выбора и новым назначениям. История уже созданных заявок не переписывается.</p>
      </section>
    """
    return admin_layout("Сотрудники", content, "employees")


def render_admin_manual(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    content = f"""
      {notice_html}
      <section class="section-heading"><div><h2>Ручное создание заявки</h2><p>Для обращения, которое нужно завести в систему без WhatsApp</p></div></section>
      <section class="panel">
        <form class="manual-form" method="post" action="/admin/manual">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <label>Название<input name="title" maxlength="160" placeholder="Например, Открытие НП" required></label>
          <label>Категория<select name="category">{category_options('general')}</select></label>
          <label>Приоритет<select name="priority">{priority_options('normal')}</select></label>
          <label>Сотрудник<select name="employee">{employee_options()}</select></label>
          <label>Статус<select name="status"><option value="new">Не тронута</option><option value="in_progress">В работе</option><option value="done">Сделано</option><option value="invalid">Недействительная</option></select></label>
          <label class="wide">Описание<textarea name="summary" rows="4" maxlength="2000" placeholder="Номер НП, перевозки, БИН или подробности" required></textarea></label>
          <button class="button primary" type="submit">Создать заявку</button>
        </form>
      </section>
    """
    return admin_layout("Ручная заявка", content, "manual")


def render_admin_menu(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows = []
    visible_numbers = {
        str(option.get("key", "")): number
        for number, option in numbered_enabled_menu_options()
    }
    for index, option in enumerate(MENU_OPTIONS, start=1):
        number = str(visible_numbers.get(str(option.get("key", "")), "Скрыт"))
        checked = "checked" if option.get("enabled") else ""
        selected_action = str(option.get("action", MENU_ACTION_TICKET))
        action_options = "".join(
            f'<option value="{key}" {"selected" if key == selected_action else ""}>{label}</option>'
            for key, label in [
                (MENU_ACTION_TICKET, "Создавать заявку"),
                (MENU_ACTION_INSTRUCTION, "Только отправить инструкцию"),
            ]
        )
        rows.append(
            f"""<article class="menu-editor-row panel">
              <div class="menu-editor-number">{e(number)}</div>
              <input type="hidden" name="key_{index}" value="{e(option['key'])}">
              <label>Название пункта<input name="label_{index}" maxlength="80" value="{e(option['label'])}" required></label>
              <label>Что делать после выбора<select name="action_{index}">{action_options}</select></label>
              <label class="wide">Сообщение пользователю<textarea name="prompt_{index}" rows="4" maxlength="1200" required>{e(option['prompt'])}</textarea></label>
              <label class="checkbox-label"><input type="checkbox" name="enabled_{index}" value="1" {checked}>Показывать в меню</label>
            </article>"""
        )
    content = f"""
      {notice_html}
      <section class="section-heading"><div><h2>Меню обращений</h2><p>Для каждого пункта можно создать заявку или только отправить пользователю готовую инструкцию</p></div></section>
      <form class="menu-editor" method="post" action="/admin/menu">
        <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
        <input type="hidden" name="action" value="save">
        <input type="hidden" name="row_count" value="{len(MENU_OPTIONS)}">
        {''.join(rows)}
        <button class="button primary" type="submit">Сохранить меню</button>
      </form>
      <section class="panel add-menu-option">
        <div><h2>Добавить вариант запроса</h2><p>Новый вариант появится последним пунктом меню. Выберите, должна ли по нему создаваться заявка.</p></div>
        <form class="manual-form" method="post" action="/admin/menu">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <input type="hidden" name="action" value="add">
          <label>Название<input name="new_label" maxlength="80" placeholder="Например, Доступ к системе" required></label>
          <label>Режим<select name="new_action"><option value="ticket">Создавать заявку</option><option value="instruction">Только отправить инструкцию</option></select></label>
          <label class="wide">Сообщение пользователю<textarea name="new_prompt" rows="3" maxlength="1200" placeholder="Подсказка для заявки или готовая инструкция" required></textarea></label>
          <button class="button" type="submit">Добавить вариант</button>
        </form>
      </section>
    """
    return admin_layout("Меню обращений", content, "menu")


def render_admin_categories(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows: list[str] = []
    ticket_count = 0
    for index, option in enumerate(MENU_OPTIONS, start=1):
        if str(option.get("action", MENU_ACTION_TICKET)) != MENU_ACTION_TICKET:
            continue
        ticket_count += 1
        checked = "checked" if bool(option.get("enabled")) else ""
        rows.append(
            f"""<article class="menu-editor-row panel">
              <div class="menu-editor-number">{ticket_count}</div>
              <input type="hidden" name="key_{index}" value="{e(str(option['key']))}">
              <label>Название категории<input name="label_{index}" maxlength="80" value="{e(str(option['label']))}" required></label>
              <label class="checkbox-label"><input type="checkbox" name="enabled_{index}" value="1" {checked}>Доступна для новых заявок</label>
              <label class="checkbox-label danger-choice"><input type="checkbox" name="delete_{index}" value="1">Удалить категорию</label>
            </article>"""
        )
    empty = '<div class="panel empty">Категорий заявок пока нет</div>' if not rows else ""
    content = f"""
      {notice_html}
      <section class="section-heading"><div><h2>Категории заявок</h2><p>Добавляйте, переименовывайте или убирайте виды категорий без изменения кода</p></div></section>
      <form class="menu-editor" method="post" action="/admin/categories">
        <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
        <input type="hidden" name="action" value="save">
        <input type="hidden" name="row_count" value="{len(MENU_OPTIONS)}">
        {''.join(rows)}
        {empty}
        <button class="button primary" type="submit">Сохранить категории</button>
      </form>
      <section class="panel add-menu-option">
        <div><h2>Добавить категорию</h2><p>Новая категория сразу появится в меню обращений, ручной заявке и выборе категории. Для неё используется универсальный сбор описания проблемы.</p></div>
        <form class="manual-form" method="post" action="/admin/categories">
          <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
          <input type="hidden" name="action" value="add">
          <label>Название<input name="new_label" maxlength="80" placeholder="Например, Доступ к системе" required></label>
          <button class="button" type="submit">Добавить категорию</button>
        </form>
        <p class="muted">При удалении старые заявки не ломаются: название категории сохраняется в истории, но для новых обращений она больше не показывается.</p>
      </section>
    """
    return admin_layout("Категории", content, "categories")


def avatar_html(chat_id: str, name: str) -> str:
    url = queue_avatars.url(sys.modules[__name__],chat_id)
    letters = ''.join(word[0] for word in name.split()[:2]) or '?'
    pic = f'<img src="{e(url)}" alt="" loading="lazy" onerror="this.remove()">' if url else ''
    return f'<span class="chat-avatar table-avatar">{e(letters)}{pic}</span>'


def discovered_contact_rows(search_raw: str = "", limit: int = 80) -> str:
    with CHAT_LOCK:
        discovered = list(CHAT_STATE.get("discovered_contacts", [])) if isinstance(CHAT_STATE.get("discovered_contacts", []), list) else []
    found_rows: list[str] = []
    for item in discovered:
        if not isinstance(item, dict):
            continue
        name = normalize_message(str(item.get("name", "")))[:100]
        phone = normalize_phone(str(item.get("phone", "")))
        chat_id = valid_chat_id(str(item.get("chat_id", "")))
        if not phone or not chat_id or not flexible_contact_match(search_raw, name, phone, chat_id):
            continue
        saved_text = "Да" if item.get("saved") else "Нет"
        default_name = name or display_phone(phone)
        duplicate = bool(STORE.manual_whatsapp_contact(chat_id, phone))
        if duplicate:
            action = '<span class="duplicate-badge">Уже добавлен</span>'
        else:
            action = f"""<form method="post" action="/admin/contacts"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="save"><input type="hidden" name="name" value="{e(default_name)}"><input type="hidden" name="phone" value="{e(phone)}"><button class="button primary" type="submit">Добавить</button></form>"""
        found_rows.append(
            f"""<tr><td><span class="avatar-name">{avatar_html(chat_id,default_name)}<strong>{e(default_name)}</strong></span></td><td>{e(display_phone(phone))}</td><td>{e(saved_text)}</td><td class="table-actions"><a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Открыть чат</a>{action}</td></tr>"""
        )
        if len(found_rows) >= max(1, min(limit, 200)):
            break
    return "".join(found_rows) or '<tr><td colspan="4" class="empty">Ничего не найдено. Попробуйте имя, часть номера или обновите контакты.</td></tr>'


def render_admin_contacts(query: dict[str, list[str]]) -> str:
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows = []
    for contact in STORE.list_manual_whatsapp_contacts():
        chat_id = str(contact["chat_id"])
        rows.append(f"""<tr><td><span class="avatar-name">{avatar_html(chat_id,str(contact['name']))}<strong>{e(contact['name'])}</strong></span></td><td>{e(display_phone(str(contact['phone'])))}</td><td>{e(human_time(str(contact['created_at'])))}</td><td class="table-actions"><a class="button" href="/whatsapp?chat_id={quote(chat_id)}">Профиль / чат</a><form method="post" action="/admin/contacts" onsubmit="return confirm('Удалить контакт из системы?')"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="delete"><input type="hidden" name="chat_id" value="{e(chat_id)}"><button class="button ghost" type="submit">Удалить</button></form></td></tr>""")
    table = "".join(rows) or '<tr><td colspan="4" class="empty">Контакты вручную ещё не добавлены</td></tr>'
    search_raw = query.get("q", [""])[0]
    found_table = discovered_contact_rows(search_raw, 80)
    content = f"""
      {notice_html}
      <section class="panel manual-panel admin-contact-form"><div><h2>Добавить пользователя WhatsApp</h2><p>У добавленных контактов автоответчик всегда отключён. Они используются для обычной личной переписки.</p></div><form class="manual-form" method="post" action="/admin/contacts"><input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="save"><label>Имя<input name="name" maxlength="100" required></label><label>Мобильный номер<input name="phone" inputmode="tel" maxlength="30" placeholder="+7 777 123 45 67" required></label><button class="button primary" type="submit">Добавить контакт</button></form></section>
      <section class="panel table-panel"><div class="section-heading"><div><h2>Добавленные контакты</h2><p>Для них нет переключателя автоответчика: автоматика выключена постоянно.</p></div></div><div class="table-scroll"><table><thead><tr><th>Имя</th><th>Номер</th><th>Добавлен</th><th>Действия</th></tr></thead><tbody>{table}</tbody></table></div></section>
      <section class="panel table-panel" data-contact-search-panel><div class="section-heading"><div><h2>Поиск контактов WhatsApp</h2><p>Можно вводить имя, часть номера, +7 или 8. Пробелы, скобки и дефисы не мешают поиску.</p></div><a class="button" href="/admin/contacts?q={quote(search_raw)}">Обновить</a></div><form class="filters" method="get" action="/admin/contacts" data-contact-search-form><input type="search" name="q" value="{e(search_raw)}" placeholder="Имя или номер" data-live-contact-search autocomplete="off"><button class="button primary" type="submit">Найти</button><a class="button ghost" href="/admin/contacts">Сбросить</a></form><div class="table-scroll"><table><thead><tr><th>Имя</th><th>Номер</th><th>Сохранён в WhatsApp</th><th>Действия</th></tr></thead><tbody data-contact-search-results>{found_table}</tbody></table></div></section>
    """
    return admin_layout("Контакты WhatsApp", content, "contacts")


def render_groups(query: dict[str, list[str]], show_admin: bool = False) -> str:
    initial_group_id = valid_group_id(query.get("chat_id", [""])[0])
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows = []
    for group in STORE.list_whatsapp_groups():
        chat_id = str(group["chat_id"])
        admin_action = (
            f'''<form method="post" action="/admin/groups" onsubmit="return confirm('Удалить группу из системы?')">
                  <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
                  <input type="hidden" name="action" value="delete">
                  <input type="hidden" name="chat_id" value="{e(chat_id)}">
                  <button class="button ghost" type="submit">Удалить</button>
                </form>'''
            if show_admin
            else ""
        )
        rows.append(
            f"""<tr><td><a class="ticket-title-link avatar-name" href="/groups?chat_id={quote(chat_id)}">{avatar_html(chat_id,str(group['name']))}<strong>{e(group['name'])}</strong></a></td><td>{int(group['participant_count']) or 'Не определено'}</td><td>{e(human_time(str(group['updated_at'])))}</td><td>{admin_action}</td></tr>"""
        )
    table = "".join(rows) or '<tr><td colspan="4" class="empty">Группы пока не добавлены</td></tr>'
    admin_form = ""
    auto_refresh_html = ""
    if show_admin:
        sync_status_html = ""
        raw_sync_status = STORE.get_setting("group_sync_status", "")
        if raw_sync_status:
            try:
                sync_status = json.loads(raw_sync_status)
                found_count = int(sync_status.get("groups", 0) or 0)
                chats_groups = int(sync_status.get("chats_groups", 0) or 0)
                chats_total = int(sync_status.get("chats_total", 0) or 0)
                contacts_groups = int(sync_status.get("contacts_groups", 0) or 0)
                contacts_total = int(sync_status.get("contacts_total", 0) or 0)
                updated_at = human_time(str(sync_status.get("updated_at", "")))
                errors = sync_status.get("errors", [])
                error_html = ""
                if isinstance(errors, list) and errors:
                    error_html = f'<small class="muted">Часть способов поиска вернула ошибку: {e(" | ".join(str(item) for item in errors))}</small>'
                sync_status_html = (
                    '<div class="notice">'
                    f'Последнее сканирование WhatsApp: найдено групп <strong>{found_count}</strong>. '
                    f'Чаты: {chats_groups}/{chats_total}, контакты: {contacts_groups}/{contacts_total}. '
                    f'{e(updated_at)}{error_html}'
                    '</div>'
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                sync_status_html = ""
        discovered_groups = STORE.list_discovered_whatsapp_groups()
        discovered_rows = []
        for group in discovered_groups:
            group_id = str(group["chat_id"])
            discovered_rows.append(
                f"""<tr>
                  <td><strong>{e(str(group['name']))}</strong></td>
                  <td>{int(group.get('participant_count', 0) or 0) or 'Не определено'}</td>
                  <td>
                    <form method="post" action="/admin/groups">
                      <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
                      <input type="hidden" name="action" value="add_discovered">
                      <input type="hidden" name="chat_id" value="{e(group_id)}">
                      <button class="button primary" type="submit">Добавить</button>
                    </form>
                  </td>
                </tr>"""
            )
        discovered_table = (
            '<div class="table-scroll"><table><thead><tr><th>Группа WhatsApp</th><th>Участники</th><th></th></tr></thead><tbody>'
            + "".join(discovered_rows)
            + '</tbody></table></div>'
            if discovered_rows
            else '<p class="muted">Доступных для добавления групп пока нет. Нажмите «Обновить список из WhatsApp».</p>'
        )
        admin_form = f"""
          <section class="panel manual-panel admin-contact-form">
            <div class="section-heading">
              <div><h2>Добавить группу из WhatsApp</h2><p>Система сама получает группы рабочего аккаунта. ID искать и вводить не нужно.</p></div>
              <form method="post" action="/admin/groups">
                <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
                <input type="hidden" name="action" value="refresh">
                <button class="button primary" type="submit">Обновить список из WhatsApp</button>
              </form>
            </div>
            {sync_status_html}
            {discovered_table}
          </section>
        """
        if query.get("refresh", [""])[0] == "1":
            auto_refresh_html = '<script>setTimeout(function(){window.location.href="/groups";},7000);</script>'
    content = f"""
      {notice_html}
      {auto_refresh_html}
      <section class="page-heading whatsapp-heading">
        <div><p class="eyebrow">Рабочие группы</p><h1>Группы WhatsApp</h1><p>Показываются сообщения, пришедшие после запуска системы</p></div>
        <span class="connection-badge offline" id="whatsapp-connection">Проверка подключения</span>
      </section>
      <section class="chat-shell" data-conversation-page data-state-endpoint="/api/group-state" data-send-endpoint="/group-send" data-media-send-endpoint="/chat-media-send" data-forward-endpoint="/chat-forward" data-initial-chat-id="{e(initial_group_id)}" data-list-empty="Группы пока не добавлены" data-select-title="Выберите группу">
        <aside class="panel chat-sidebar">
          <div class="chat-sidebar-head"><h2>Группы</h2><button class="icon-button" type="button" data-chat-refresh title="Обновить">Обновить</button></div>
          <input type="search" data-chat-search placeholder="Поиск по названию группы">
          <div class="chat-list" data-chat-list><p class="chat-placeholder">Загрузка групп...</p></div>
        </aside>
        <section class="panel chat-main">
          <header class="chat-header">
            <div><small>Группа WhatsApp</small><h2 data-chat-title>Выберите группу</h2></div>
            <div class="chat-header-actions">
              <span class="chat-memory-note">Новые сообщения сохраняются в БД</span>
              <button class="button compact ghost" type="button" data-group-mute-toggle hidden>🔕 Заглушить</button>
              <details class="participants-details" data-participants-details>
                <summary>Участники <b data-participants-count>0</b></summary>
                <div class="participants-list" data-group-participants><span class="muted">Выберите группу</span></div>
              </details>
            </div>
          </header>
          <div class="chat-messages" data-chat-messages><p class="chat-placeholder">Выберите группу слева, чтобы увидеть сообщения</p></div>
          <div class="forward-toolbar" data-forward-toolbar hidden><strong data-forward-count>0 сообщений выбрано</strong><select data-forward-target aria-label="Куда переслать"></select><button class="button compact" type="button" data-forward-cancel>Отмена</button><button class="button primary compact" type="button" data-forward-send>Переслать</button></div>
          <form class="chat-composer" data-chat-composer>
            <input type="hidden" name="chat_id" data-chat-id>
            <input type="hidden" name="mentions" data-mentions-input>
            <input type="hidden" name="reply_to" data-reply-to>
            <div class="reply-preview" data-reply-preview hidden><div><strong data-reply-sender></strong><span data-reply-body></span></div><button type="button" data-reply-cancel aria-label="Отменить ответ">×</button></div>
            <span class="composer-file-name" data-media-name></span>
            <textarea class="composer-message-input" name="message" rows="2" maxlength="32000" placeholder="Напишите сообщение в группу"></textarea>
            <button class="button primary composer-send" type="submit">Отправить</button>
            <div class="composer-tools"><button class="composer-tool" type="button" data-emoji-toggle title="Смайлы">☺</button><label class="composer-tool composer-attach" title="Прикрепить файл">📎<input type="file" data-media-input accept="image/*,video/*,audio/*,.pdf,.txt,.csv,.docx,.xlsx,.json,.xml,.zip,.rar,.7z" hidden></label></div>
            <div class="emoji-picker" data-emoji-picker hidden></div>
          </form>
        </section>
      </section>
      {admin_form}
      <section class="panel table-panel"><div class="section-heading"><div><h2>Все добавленные группы</h2><p>Добавлять и удалять группы может только администратор</p></div></div><div class="table-scroll"><table><thead><tr><th>Группа</th><th>Участники</th><th>Обновление</th><th></th></tr></thead><tbody>{table}</tbody></table></div></section>
    """
    return layout("Группы WhatsApp", content, "groups", show_admin)

def render_whatsapp(query: dict[str, list[str]], show_admin: bool = False) -> str:
    initial_chat_id = query.get("chat_id", [""])[0]
    content = f"""
      <section class="page-heading whatsapp-heading">
        <div><p class="eyebrow">Рабочий мессенджер</p><h1>WhatsApp</h1><p>Показываются и сохраняются только сообщения, полученные после запуска этой системы</p></div>
        <span class="connection-badge offline" id="whatsapp-connection">Проверка подключения</span>
      </section>
      <section class="chat-shell" data-conversation-page data-whatsapp-page data-state-endpoint="/api/chat-state" data-send-endpoint="/chat-send" data-media-send-endpoint="/chat-media-send" data-forward-endpoint="/chat-forward" data-initial-chat-id="{e(initial_chat_id)}" data-list-empty="Личные чаты пока не загружены" data-select-title="Выберите пользователя">
        <aside class="panel chat-sidebar">
          <div class="chat-sidebar-head">
            <h2>Чаты</h2>
            <button class="icon-button" type="button" data-chat-refresh title="Обновить">Обновить</button>
          </div>
          <input type="search" data-chat-search placeholder="Поиск по имени или номеру">
          <div class="chat-list" data-chat-list><p class="chat-placeholder">Загрузка чатов...</p></div>
        </aside>
        <section class="panel chat-main">
          <header class="chat-header">
            <div><small>Диалог WhatsApp</small><h2 data-chat-title>Выберите пользователя</h2><span class="contact-presence" data-contact-presence hidden></span><span class="manual-mode-state" data-manual-mode-state></span><div class="contact-profile-summary" data-contact-profile-summary hidden></div></div>
            <div class="chat-header-actions">
              <span class="chat-memory-note">Новые сообщения сохраняются в БД</span>
              <button class="button compact" type="button" data-profile-toggle disabled>Профиль</button>
              <button class="button compact" type="button" data-manual-mode-toggle disabled>Выключить автоответчик</button>
            </div>
          </header>
          <div class="chat-messages" data-chat-messages>
            <p class="chat-placeholder">Выберите чат слева, чтобы увидеть последние сообщения</p>
          </div>
          <div class="forward-toolbar" data-forward-toolbar hidden><strong data-forward-count>0 сообщений выбрано</strong><select data-forward-target aria-label="Куда переслать"></select><button class="button compact" type="button" data-forward-cancel>Отмена</button><button class="button primary compact" type="button" data-forward-send>Переслать</button></div>
          <form class="chat-composer" data-chat-composer>
            <input type="hidden" name="chat_id" data-chat-id>
            <input type="hidden" name="reply_to" data-reply-to>
            <div class="reply-preview" data-reply-preview hidden><div><strong data-reply-sender></strong><span data-reply-body></span></div><button type="button" data-reply-cancel aria-label="Отменить ответ">×</button></div>
            <span class="composer-file-name" data-media-name></span>
            <textarea class="composer-message-input" name="message" rows="2" maxlength="32000" placeholder="Введите сообщение"></textarea>
            <button class="button primary composer-send" type="submit">Отправить</button>
            <div class="composer-tools"><button class="composer-tool" type="button" data-emoji-toggle title="Смайлы">☺</button><label class="composer-tool composer-attach" title="Прикрепить файл">📎<input type="file" data-media-input accept="image/*,video/*,audio/*,.pdf,.txt,.csv,.docx,.xlsx,.json,.xml,.zip,.rar,.7z" hidden></label></div>
            <div class="emoji-picker" data-emoji-picker hidden></div>
          </form>
        </section>
      </section>
      <p class="muted chat-page-note">Для работы вкладки должны быть запущены программа и WhatsApp-коннектор. Телефон постоянно держать рядом не требуется.</p>
    """
    return layout("WhatsApp", content, "whatsapp", show_admin)


class TicketHandler(BaseHTTPRequestHandler):
    server_version = "UnifiedQueue/1.00.1"
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
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

    def authenticated_user(self) -> str:
        # В локальном Windows-режиме приложение доступно только с localhost,
        # поэтому отдельный Nginx/Basic Auth для тестовой копии не требуется.
        if LOCAL_MODE and self.client_address and self.client_address[0] in {"127.0.0.1", "::1"}:
            return ADMIN_USER
        # На боевом сервере доверяем имени пользователя только от локального Nginx.
        # Клиентский Authorization здесь намеренно не разбирается.
        remote_user = self.headers.get("X-Remote-User", "").strip()
        if self.client_address and self.client_address[0] in {"127.0.0.1", "::1"}:
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", remote_user):
                return remote_user
        return ""

    def has_admin_session(self) -> bool:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return False
        try:
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
            morsel = cookies.get(ADMIN_SESSION_COOKIE)
            return bool(morsel) and secrets.compare_digest(morsel.value, ADMIN_SESSION_TOKEN)
        except (CookieError, ValueError):
            return False

    def is_admin(self) -> bool:
        username = self.authenticated_user().casefold()
        return (bool(username) and username == ADMIN_USER) or self.has_admin_session()

    def require_admin(self) -> bool:
        if self.is_admin():
            return True
        if self.command == "GET":
            self.redirect("/admin/login")
        else:
            self.send_error(HTTPStatus.FORBIDDEN, "Administrator access required")
        return False

    def start_admin_session(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/admin/")
        self.send_header(
            "Set-Cookie",
            f"{ADMIN_SESSION_COOKIE}={ADMIN_SESSION_TOKEN}; Path=/; Max-Age={ADMIN_SESSION_MAX_AGE}; HttpOnly; {'Secure; ' if not LOCAL_MODE else ''}SameSite=Lax",
        )
        self.end_headers()

    def end_admin_session(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{ADMIN_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax",
        )
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self) -> None:
        if not self.request_host_allowed():
            return
        if queue_performance_http.get(self, sys.modules[__name__]):
            return
        if queue_reliability_http.get(self, sys.modules[__name__]):
            return
        if queue_workflow_http.get(self, sys.modules[__name__]):
            return
        if queue_productivity_http.get(self, sys.modules[__name__]):
            return
        if queue_feature_http.get(self, sys.modules[__name__]):
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        show_admin = self.is_admin()
        if parsed.path == "/":
            self.html_response(render_dashboard(query, show_admin))
        elif parsed.path == "/instruction":
            self.html_response(render_instruction(show_admin))
        elif parsed.path == "/admin/login":
            if self.authenticated_user().casefold() != ADMIN_USER:
                self.send_error(HTTPStatus.FORBIDDEN, "Administrator login required")
                return
            self.start_admin_session()
        elif parsed.path == "/admin/logout":
            self.end_admin_session()
        elif parsed.path == "/ticket":
            self.html_response(render_ticket(self.query_int(query, "id"), query, show_admin))
        elif parsed.path in {"/admin", "/admin/"}:
            if not self.require_admin():
                return
            self.html_response(render_admin_home())
        elif parsed.path == "/admin/employees":
            if not self.require_admin():
                return
            self.html_response(render_admin_employees(query))
        elif parsed.path == "/admin/manual":
            if not self.require_admin():
                return
            self.html_response(render_admin_manual(query))
        elif parsed.path == "/admin/menu":
            if not self.require_admin():
                return
            self.html_response(render_admin_menu(query))
        elif parsed.path == "/admin/categories":
            if not self.require_admin():
                return
            self.html_response(render_admin_categories(query))
        elif parsed.path == "/admin/errors":
            if not self.require_admin():
                return
            self.html_response(render_admin_errors(query))
        elif parsed.path == "/admin/error":
            if not self.require_admin():
                return
            self.html_response(render_admin_error(self.query_int(query, "id"), query))
        elif parsed.path == "/admin/contacts":
            if not self.require_admin():
                return
            self.html_response(render_admin_contacts(query))
        elif parsed.path == "/admin/audit":
            if not self.require_admin():
                return
            self.html_response(render_admin_audit(query))
        elif parsed.path == "/admin/system":
            if not self.require_admin():
                return
            self.html_response(render_admin_system(query))
        elif parsed.path == "/api/admin/contact-search":
            if not self.require_admin():
                return
            search_raw = query.get("q", [""])[0]
            self.json_response({"rows_html": discovered_contact_rows(search_raw, 80)})
        elif parsed.path == "/admin/groups":
            if not self.require_admin():
                return
            self.html_response(render_groups(query, True))
        elif parsed.path == "/admin/whatsapp":
            if not self.require_admin():
                return
            self.redirect("/admin/")
        elif parsed.path == "/api/admin/connector-state":
            if not self.require_admin():
                return
            self.json_response(connector_state_snapshot())
        elif parsed.path == "/whatsapp":
            self.html_response(render_whatsapp(query, show_admin))
        elif parsed.path == "/groups":
            self.html_response(render_groups(query, show_admin))
        elif parsed.path == "/qr":
            if not self.require_admin():
                return
            self.redirect("/admin/")
        elif parsed.path == "/api/dashboard-data":
            self.handle_dashboard_data(query)
        elif parsed.path == "/api/chat-state":
            self.json_response(chat_state_snapshot(query.get("chat_id", [""])[0], query.get("read", ["0"])[0] == "1"))
        elif parsed.path == "/api/group-state":
            self.json_response(group_state_snapshot(query.get("chat_id", [""])[0], query.get("read", ["0"])[0] == "1"))
        elif parsed.path == "/api/chat-history":
            self.handle_chat_history(query)
        elif parsed.path == "/api/message-window":
            self.handle_message_window(query)
        elif parsed.path == "/api/chat-action-status":
            action_id = self.query_int(query, "id")
            action = STORE.whatsapp_action_status(action_id)
            self.json_response({"found": bool(action), "action": action or {}})
        elif parsed.path == "/api/notifications":
            self.json_response(notifications_snapshot())
        elif parsed.path == "/api/chat-media":
            self.handle_chat_media(query)
        elif parsed.path == "/static/style.css":
            self.file_response(ROOT / "static" / "style.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/instruction.css":
            self.file_response(ROOT / "static" / "instruction.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/global-theme.css":
            self.file_response(ROOT / "static" / "global-theme.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/productivity.css":
            self.file_response(ROOT / "static" / "productivity.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/productivity.js":
            self.file_response(ROOT / "static" / "productivity.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/workflow.css":
            self.file_response(ROOT / "static" / "workflow.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/workflow.js":
            self.file_response(ROOT / "static" / "workflow.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/reliability.css":
            self.file_response(ROOT / "static" / "reliability.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/reliability.js":
            self.file_response(ROOT / "static" / "reliability.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/notification-hotfix.css":
            self.file_response(ROOT / "static" / "notification-hotfix.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/interface105.css":
            self.file_response(ROOT / "static" / "interface105.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/interface105.js":
            self.file_response(ROOT / "static" / "interface105.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/performance107.css":
            self.file_response(ROOT / "static" / "performance107.css", "text/css; charset=utf-8")
        elif parsed.path == "/static/performance107.js":
            self.file_response(ROOT / "static" / "performance107.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/app.js":
            self.file_response(ROOT / "static" / "app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/static/favicon.png" or parsed.path == "/favicon.ico":
            self.file_response(ROOT / "static" / "favicon.png", "image/png")
        elif parsed.path == "/health/connector":
            self.json_response(connector_state_snapshot())
        elif parsed.path == "/health":
            self.json_response({"status": "ok"})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.request_host_allowed():
            return
        parsed = urlparse(self.path)
        if queue_performance_http.post(self, sys.modules[__name__]):
            return
        if queue_reliability_http.post(self, sys.modules[__name__]):
            return
        if queue_reliability.maintenance_active(STORE) and queue_reliability.should_block_mutation(parsed.path, self.is_admin()):
            if parsed.path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or parsed.path.startswith("/chat"):
                self.json_response({"error":"Режим обслуживания: изменения временно заблокированы администратором"}, HTTPStatus.LOCKED)
            else:
                self.redirect("/?notice=" + quote("Режим обслуживания: изменения временно заблокированы"))
            return
        if queue_workflow_http.post(self, sys.modules[__name__]):
            return
        if queue_productivity_http.post(self, sys.modules[__name__]):
            return
        if queue_feature_http.post(self, sys.modules[__name__]):
            return
        if queue_uploads.post(self, sys.modules[__name__]):
            return
        if parsed.path == "/api/system-warning-action":
            payload = self.read_json_body(120_000)
            if payload is None:
                return
            action = str(payload.get("action") or "").strip().lower()
            event_id = str(payload.get("event_id") or "").strip()
            try:
                if action == "dismiss":
                    if not queue_reliability.dismiss_notification(STORE, event_id):
                        self.json_response({"error":"invalid_warning_id"}, HTTPStatus.BAD_REQUEST)
                        return
                    self.json_response({"ok":True,"dismissed":1})
                    return
                if action == "dismiss_all":
                    snapshot = notifications_snapshot()
                    ids = [str(item.get("id") or item.get("event_id") or "") for item in snapshot.get("events",[]) if isinstance(item,dict)]
                    count = queue_reliability.dismiss_notifications(STORE, ids)
                    self.json_response({"ok":True,"dismissed":count})
                    return
                if action in {"check","check_all"}:
                    queue_reliability.refresh_active_alerts(sys.modules[__name__])
                    self.json_response({"ok":True,"checked":True})
                    return
            except Exception as error:
                self.json_response({"error":str(error)[:180]}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.json_response({"error":"invalid_action"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/media-stage":
            payload = self.read_authorized_json(800_000)
            if payload is not None:
                try: self.json_response(queue_inbound_media.receive(MEDIA_DIR, payload))
                except (ValueError, OSError) as error: self.json_response({"error": str(error)[:180]}, 400)
            return
        if parsed.path == "/api/whatsapp":
            self.handle_api_message()
            return
        if parsed.path == "/api/template-error-delivery":
            self.handle_template_error_delivery()
            return
        if parsed.path == "/api/outbound/claim":
            self.handle_outbound_claim()
            return
        if parsed.path in {"/api/outbound/heartbeat", "/api/outbound/begin"}:
            payload = self.read_authorized_json()
            if payload is not None:
                try:
                    operation = STORE.begin_outbound_send if parsed.path.endswith('/begin') else STORE.touch_outbound_message
                    alive = operation(int(payload.get("message_id", 0)))
                    self.json_response({"updated": alive})
                except (ValueError, TypeError):
                    self.json_response({"error": "invalid_id"}, 400)
            return
        if parsed.path == "/api/outbound/result":
            self.handle_outbound_result()
            return
        if parsed.path == "/api/chat-list-sync":
            self.handle_chat_list_sync()
            return
        if parsed.path == "/api/group-list-sync":
            self.handle_group_list_sync()
            return
        if parsed.path == "/api/presence-sync":
            self.handle_presence_sync()
            return
        elif parsed.path == "/api/group-participants-sync":
            self.handle_group_participants_sync()
            return
        if parsed.path == "/api/group-message-identities-sync":
            self.handle_group_message_identities_sync()
            return
        if parsed.path == "/api/contact-list-sync":
            self.handle_contact_list_sync()
            return
        if parsed.path == "/api/avatar-sync":
            payload = self.read_authorized_json()
            if payload is not None:
                try:
                    queue_avatars.save(sys.modules[__name__],payload)
                    self.json_response({"updated":True})
                except (ValueError,TypeError,OSError):
                    self.json_response({"error":"Некорректный аватар"},400)
            return
        if parsed.path == "/api/contact-profile-sync":
            self.handle_contact_profile_sync()
            return
        if parsed.path == "/api/group-refresh-check":
            self.handle_group_refresh_check()
            return
        if parsed.path == "/api/connector-state":
            self.handle_connector_state()
            return
        if parsed.path == "/api/contact-policy":
            self.handle_contact_policy()
            return
        if parsed.path == "/api/call-permission":
            self.handle_call_permission()
            return
        if parsed.path == "/api/chat-messages-sync":
            self.handle_chat_messages_sync()
            return
        if parsed.path == "/api/chat-message-ack":
            self.handle_chat_message_ack()
            return
        if parsed.path == "/api/chat-reaction-sync":
            self.handle_chat_reaction_sync()
            return
        if parsed.path == "/chat-media-send":
            self.handle_chat_media_send()
            return
        if parsed.path == "/chat-forward":
            self.handle_chat_forward()
            return
        if parsed.path == "/api/whatsapp-action/claim":
            self.handle_whatsapp_action_claim()
            return
        if parsed.path == "/api/whatsapp-action/result":
            self.handle_whatsapp_action_result()
            return
        if parsed.path == "/api/chat-control":
            self.handle_chat_control()
            return
        form = self.read_form()
        if parsed.path == "/chat-favorite":
            favorite = form.get("favorite", "0").strip().lower() in {"1", "true", "yes", "on"}
            updated, chat_id = set_chat_favorite(form.get("chat_id", ""), favorite)
            if not updated:
                self.json_response({"updated": False, "error": "invalid_chat"}, HTTPStatus.BAD_REQUEST)
            else:
                self.json_response({"updated": True, "chat_id": chat_id, "favorite": favorite})
            return
        if parsed.path == "/group-mute":
            chat_id = valid_group_id(form.get("chat_id", ""))
            muted = form.get("muted", "0") in {"1", "true", "yes", "on"}
            updated = STORE.set_whatsapp_group_muted(chat_id, muted)
            if not updated:
                self.json_response({"updated": False, "error": "group_not_found"}, HTTPStatus.NOT_FOUND)
            else:
                self.json_response({"updated": True, "chat_id": chat_id, "muted": muted})
            return
        if parsed.path == "/admin/employees":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            try:
                count = int(form.get("employee_count", "0"))
            except ValueError:
                count = 0
            if count < 1 or count > MAX_EMPLOYEES:
                self.redirect(f"/admin/employees?notice={quote('Некорректное количество сотрудников')}")
                return
            names = [
                normalize_message(form.get(f"employee_{index}", ""))[:60].strip()
                for index in range(1, count + 1)
            ]
            if any(not name for name in names):
                self.redirect(f"/admin/employees?count={count}&notice={quote('Все имена должны быть заполнены')}")
                return
            if len({name.casefold() for name in names}) != len(names):
                self.redirect(f"/admin/employees?count={count}&notice={quote('Имена сотрудников должны отличаться')}")
                return
            old_names = list(EMPLOYEES)
            old_active = active_employee()
            STORE.set_setting("employees_json", json.dumps(names, ensure_ascii=False))
            EMPLOYEES[:] = names
            if old_active in old_names:
                old_index = old_names.index(old_active)
                if old_index < len(names):
                    STORE.set_setting("active_employee", names[old_index])
                else:
                    STORE.set_setting("active_employee", names[0])
            elif old_active not in names:
                STORE.set_setting("active_employee", names[0])
            self.redirect(f"/admin/employees?notice={quote('Список сотрудников сохранён')}")
            return
        if parsed.path == "/admin/ticket-delete":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            ticket_id = self.form_int(form, "ticket_id")
            deleted = STORE.delete_ticket(ticket_id, f"Администратор · {active_employee()}")
            notice = (
                f"Заявка №{ticket_id} удалена только из системы. Сообщение пользователю не отправлялось"
                if deleted else "Заявка не найдена"
            )
            self.redirect(f"/?notice={quote(notice)}")
            return
        if parsed.path == "/admin/contacts":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "delete":
                contact_chat_id = valid_chat_id(form.get("chat_id", ""))
                existing_contact = STORE.manual_whatsapp_contact(contact_chat_id, "")
                deleted = STORE.delete_whatsapp_contact(contact_chat_id)
                if deleted:
                    # После удаления из исключений следующий текст снова должен
                    # запускать обычную автоматику, а не продолжать старый контекст
                    # заявки, существовавший до добавления пользователя в контакты.
                    reset_keys = {contact_chat_id}
                    if existing_contact:
                        contact_phone = normalize_phone(str(existing_contact.get("phone", "")))
                        if contact_phone:
                            reset_keys.add(f"{contact_phone.lstrip('+')}@c.us")
                    for reset_key in reset_keys:
                        if reset_key:
                            STORE.set_conversation_context(reset_key, MENU_CONTEXT, 0, False)
                notice = "Контакт удалён. Автообработка для него снова включена" if deleted else "Контакт не найден"
                self.redirect(f"/admin/contacts?notice={quote(notice)}")
                return
            name = normalize_message(form.get("name", ""))[:100]
            phone = normalize_phone(form.get("phone", ""))
            chat_id = f"{phone.lstrip('+')}@c.us" if phone else ""
            existing = STORE.manual_whatsapp_contact(chat_id, phone) if phone else None
            if existing:
                notice = f"Номер {display_phone(phone)} уже добавлен как {existing.get('name') or 'контакт'}"
                self.redirect(f"/admin/contacts?notice={quote(notice)}")
                return
            saved = STORE.save_whatsapp_contact(name, phone, chat_id)
            notice = "Контакт добавлен" if saved else "Проверьте имя и мобильный номер"
            self.redirect(f"/admin/contacts?notice={quote(notice)}")
            return
        if parsed.path == "/admin/manual":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            employee = form.get("employee", "")
            if employee not in EMPLOYEES:
                employee = active_employee()
            category = form.get("category", "general")
            if category not in CATEGORIES:
                category = "general"
            status = form.get("status", "new")
            if status not in STATUSES:
                status = "new"
            ticket_id = STORE.create_ticket(
                {
                    "source": "admin_manual",
                    "sender": employee,
                    "category": category,
                    "priority": form.get("priority", "normal"),
                    "title": normalize_message(form.get("title", ""))[:160] or "Ручная заявка",
                    "summary": normalize_message(form.get("summary", ""))[:2000],
                    "original_text": normalize_message(form.get("summary", ""))[:2000],
                    "status": status,
                    "assigned_to": employee,
                }
            )
            self.redirect(f"/ticket?id={ticket_id}")
            return
        if parsed.path == "/admin/system":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "")
            if action == "cleanup":
                result = queue_workflow.cleanup_retention(STORE)
                notice = f"Очистка завершена: сообщений {result.get('messages',0)}, медиа {result.get('media_rows',0)}, заявок {result.get('tickets',0)}"
            elif action == "retry_failed":
                count = STORE.retry_failed_outbound()
                notice = f"На повторную отправку возвращено: {count}"
            else:
                notice = "Неизвестное действие"
            try:
                with MONITOR_LOG_PATH.open("a", encoding="utf-8") as log:
                    log.write(f"{datetime.now(timezone.utc).isoformat()} ADMIN {notice}\n")
            except OSError:
                pass
            self.redirect(f"/admin/system?notice={quote(notice)}")
            return
        if parsed.path == "/admin/groups":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "refresh":
                STORE.set_setting("group_refresh_request", secrets.token_urlsafe(18))
                notice = "Запрос отправлен QR-коннектору. Список обновится примерно через 5–7 секунд"
                self.redirect(f"/groups?notice={quote(notice)}&refresh=1")
                return
            if action == "delete":
                deleted = STORE.delete_whatsapp_group(valid_group_id(form.get("chat_id", "")))
                notice = "Группа удалена" if deleted else "Группа не найдена"
            elif action == "add_discovered":
                chat_id = valid_group_id(form.get("chat_id", ""))
                discovered = next(
                    (item for item in STORE.list_discovered_whatsapp_groups() if str(item.get("chat_id", "")) == chat_id),
                    None,
                )
                saved = STORE.save_whatsapp_group(str(discovered.get("name", "")), chat_id) if discovered else None
                notice = "Группа добавлена" if saved else "Группа не найдена в списке WhatsApp"
            else:
                saved = STORE.save_whatsapp_group(
                    form.get("name", ""),
                    valid_group_id(form.get("chat_id", "")),
                )
                notice = "Группа добавлена" if saved else "Укажите название и корректный WhatsApp ID группы"
            self.redirect(f"/groups?notice={quote(notice)}")
            return
        if parsed.path == "/admin/errors":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            report_id = self.form_int(form, "report_id")
            status = form.get("status", "new")
            admin_note = normalize_message(form.get("admin_note", ""))[:3000]
            updated = STORE.update_error_report(report_id, status, admin_note, f"Администратор · {active_employee()}")
            notice = "Репорт обновлён" if updated else "Репорт не найден"
            self.redirect(f"/admin/error?id={report_id}&notice={quote(notice)}")
            return
        if parsed.path == "/admin/categories":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "add":
                if len(MENU_OPTIONS) >= MAX_MENU_OPTIONS:
                    self.redirect(
                        f"/admin/categories?notice={quote(f'Можно добавить не больше {MAX_MENU_OPTIONS} пунктов меню и категорий суммарно')}"
                    )
                    return
                label = normalize_message(form.get("new_label", ""))[:80]
                if not label:
                    self.redirect(f"/admin/categories?notice={quote('Укажите название категории')}")
                    return
                key = f"custom_{secrets.token_hex(5)}"
                CATEGORIES[key] = label
                MENU_OPTIONS.append(
                    {
                        "key": key,
                        "label": label,
                        "prompt": category_prompt(key),
                        "enabled": True,
                        "action": MENU_ACTION_TICKET,
                    }
                )
                notice = "Категория добавлена"
            else:
                try:
                    count = int(form.get("row_count", "0"))
                except ValueError:
                    count = 0
                if count != len(MENU_OPTIONS):
                    self.redirect(f"/admin/categories?notice={quote('Категории изменились, обновите страницу и повторите')}")
                    return
                updated_options: list[dict[str, object]] = []
                enabled_ticket_count = 0
                for index, current in enumerate(MENU_OPTIONS, start=1):
                    if str(current.get("action", MENU_ACTION_TICKET)) != MENU_ACTION_TICKET:
                        updated_options.append(current)
                        continue
                    key = form.get(f"key_{index}", "")
                    if key != str(current.get("key", "")):
                        self.send_error(HTTPStatus.BAD_REQUEST, "Invalid category row")
                        return
                    label = normalize_message(form.get(f"label_{index}", ""))[:80]
                    if not label:
                        self.redirect(f"/admin/categories?notice={quote('У каждой категории должно быть название')}")
                        return
                    if form.get(f"delete_{index}") == "1":
                        # Keep the label for historical tickets, but remove the category
                        # from all new-ticket selectors and from the WhatsApp request menu.
                        CATEGORY_ARCHIVE[key] = label
                        CATEGORIES[key] = label
                        continue
                    enabled = form.get(f"enabled_{index}") == "1"
                    old_label = str(current.get("label", ""))
                    old_prompt = str(current.get("prompt", ""))
                    updated = dict(current)
                    updated["label"] = label
                    updated["enabled"] = enabled
                    CATEGORIES[key] = label
                    # If the prompt is still the automatically generated one, keep
                    # its heading in sync with a renamed category. Hand-edited prompts
                    # from the menu editor are left untouched.
                    if old_prompt.startswith(f"Вы выбрали: {old_label}."):
                        updated["prompt"] = category_prompt(key)
                    updated_options.append(updated)
                    if enabled:
                        enabled_ticket_count += 1
                if enabled_ticket_count < 1:
                    self.redirect(f"/admin/categories?notice={quote('Оставьте хотя бы одну активную категорию для заявок')}")
                    return
                MENU_OPTIONS[:] = updated_options
                notice = "Категории сохранены. Нумерация меню для пользователей обновлена автоматически"
            apply_menu_labels()
            STORE.set_setting("request_menu_json", json.dumps(MENU_OPTIONS, ensure_ascii=False))
            STORE.set_setting("request_category_archive_json", json.dumps(CATEGORY_ARCHIVE, ensure_ascii=False))
            self.redirect(f"/admin/categories?notice={quote(notice)}")
            return
        if parsed.path == "/admin/menu":
            if not self.require_admin():
                return
            supplied_token = form.get("csrf_token", "")
            if not supplied_token or not secrets.compare_digest(supplied_token, ADMIN_FORM_TOKEN):
                self.send_error(HTTPStatus.FORBIDDEN, "Invalid admin form token")
                return
            action = form.get("action", "save")
            if action == "add":
                if len(MENU_OPTIONS) >= MAX_MENU_OPTIONS:
                    self.redirect(
                        f"/admin/menu?notice={quote(f'Можно добавить не больше {MAX_MENU_OPTIONS} вариантов')}"
                    )
                    return
                label = normalize_message(form.get("new_label", ""))[:80]
                prompt = normalize_message(form.get("new_prompt", ""))[:1200]
                menu_action = form.get("new_action", MENU_ACTION_TICKET)
                if menu_action not in MENU_ACTIONS:
                    menu_action = MENU_ACTION_TICKET
                if not label or not prompt:
                    self.redirect(f"/admin/menu?notice={quote('Заполните название и подсказку')}")
                    return
                MENU_OPTIONS.append(
                    {
                        "key": f"custom_{secrets.token_hex(5)}",
                        "label": label,
                        "prompt": prompt,
                        "enabled": True,
                        "action": menu_action,
                    }
                )
            else:
                try:
                    count = int(form.get("row_count", "0"))
                except ValueError:
                    count = 0
                if count != len(MENU_OPTIONS):
                    self.redirect(f"/admin/menu?notice={quote('Меню изменилось, обновите страницу и повторите')}")
                    return
                updated_options: list[dict[str, object]] = []
                for index, current in enumerate(MENU_OPTIONS, start=1):
                    key = form.get(f"key_{index}", "")
                    if key != current["key"]:
                        self.send_error(HTTPStatus.BAD_REQUEST, "Invalid menu row")
                        return
                    label = normalize_message(form.get(f"label_{index}", ""))[:80]
                    prompt = normalize_message(form.get(f"prompt_{index}", ""))[:1200]
                    menu_action = form.get(f"action_{index}", MENU_ACTION_TICKET)
                    if menu_action not in MENU_ACTIONS:
                        menu_action = MENU_ACTION_TICKET
                    if not label or not prompt:
                        self.redirect(f"/admin/menu?notice={quote('У каждого варианта должны быть название и подсказка')}")
                        return
                    updated_options.append(
                        {
                            "key": key,
                            "label": label,
                            "prompt": prompt,
                            "enabled": form.get(f"enabled_{index}") == "1",
                            "action": menu_action,
                        }
                    )
                if not any(bool(item["enabled"]) for item in updated_options):
                    self.redirect(f"/admin/menu?notice={quote('Оставьте включённым хотя бы один вариант')}")
                    return
                MENU_OPTIONS[:] = updated_options
            apply_menu_labels()
            STORE.set_setting("request_menu_json", json.dumps(MENU_OPTIONS, ensure_ascii=False))
            self.redirect(f"/admin/menu?notice={quote('Меню обращений сохранено. Нумерация видимых пунктов обновлена автоматически')}")
            return
        if parsed.path == "/active-employee":
            employee = form.get("employee", "")
            if employee not in EMPLOYEES:
                self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
                return
            previous_employee = active_employee()
            handed_off = STORE.switch_shift(previous_employee, employee) if previous_employee != employee else 0
            STORE.set_setting("active_employee", employee)
            queue_features.announce_shift(STORE, previous_employee, employee)
            if self.headers.get("X-Requested-With") == "fetch":
                self.json_response({"updated": True, "employee": employee, "handed_off": handed_off})
            else:
                notice = f"Смена: {employee}. Передано заявок: {handed_off}" if handed_off else f"На смене: {employee}"
                self.redirect(f"/?notice={quote(notice)}")
        elif parsed.path == "/handoff":
            ticket_id = self.form_int(form, "ticket_id")
            action = form.get("action", "transfer")
            updated = STORE.set_ticket_handoff(
                ticket_id, action != "accept", active_employee()
            )
            notice = (
                "Заявка передана следующей смене"
                if updated and action != "accept"
                else "Заявка принята текущей сменой"
                if updated
                else "Не удалось изменить передачу смене"
            )
            self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")
        elif parsed.path == "/status":
            ticket_id = self.form_int(form, "ticket_id")
            ticket = STORE.get_ticket(ticket_id)
            assignee = form.get("employee", "")
            if assignee not in EMPLOYEES:
                assignee = str((ticket or {}).get("assigned_to", "")) or active_employee()
            requested_status = form.get("status", "")
            close_reason = form.get("close_reason", "")
            close_comment = form.get("close_comment", "")
            valid_close, close_error = queue_workflow.validate_close(STORE, ticket_id, requested_status, close_reason, close_comment)
            if not valid_close:
                self.redirect(f"/ticket?id={ticket_id}&notice={quote(close_error)}")
                return
            updated, _message_id = update_ticket_status(
                ticket_id,
                requested_status,
                active_employee(),
                assignee,
                allow_reopen=self.is_admin(),
                close_reason=close_reason,
                close_comment=close_comment,
            )
            if updated:
                queue_workflow.record_close_meta(STORE, ticket_id, requested_status, close_reason, close_comment, active_employee())
            notice = "" if updated else "Отработанную заявку может вернуть в работу только администратор"
            suffix = f"&notice={quote(notice)}" if notice else ""
            self.redirect(f"/ticket?id={ticket_id}{suffix}")
        elif parsed.path == "/quick-status":
            ticket_id = self.form_int(form, "ticket_id")
            ticket = STORE.get_ticket(ticket_id)
            actor = active_employee()
            assignee = str((ticket or {}).get("assigned_to", "")) or actor
            requested_status = form.get("status", "")
            close_reason = form.get("close_reason", "")
            close_comment = form.get("close_comment", "")
            valid_close, close_error = queue_workflow.validate_close(STORE, ticket_id, requested_status, close_reason, close_comment)
            if not valid_close:
                self.json_response({"updated":False,"error":close_error}, HTTPStatus.BAD_REQUEST)
                return
            updated, message_id = update_ticket_status(
                ticket_id,
                requested_status,
                actor,
                assignee,
                allow_reopen=self.is_admin(),
                close_reason=close_reason,
                close_comment=close_comment,
            )
            if updated:
                queue_workflow.record_close_meta(STORE, ticket_id, requested_status, close_reason, close_comment, actor)
            self.json_response(
                {
                    "updated": updated,
                    "ticket_id": ticket_id,
                    "notification_queued": bool(message_id),
                },
                HTTPStatus.OK if updated else (
                    HTTPStatus.FORBIDDEN
                    if ticket and str(ticket.get("status", "")) in {"done", "invalid"} and not self.is_admin()
                    else HTTPStatus.BAD_REQUEST
                ),
            )
        elif parsed.path == "/quick-priority":
            ticket_id = self.form_int(form, "ticket_id")
            updated = STORE.update_priority(
                ticket_id,
                form.get("priority", ""),
                active_employee(),
            )
            self.json_response(
                {"updated": updated, "ticket_id": ticket_id},
                HTTPStatus.OK if updated else HTTPStatus.BAD_REQUEST,
            )
        elif parsed.path == "/classification":
            ticket_id = self.form_int(form, "ticket_id")
            updated = STORE.update_priority(
                ticket_id,
                form.get("priority", ""),
                active_employee(),
            )
            notice = "Приоритет сохранён" if updated else "Не удалось изменить приоритет"
            self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")
        elif parsed.path == "/send-reply":
            ticket_id = self.form_int(form, "ticket_id")
            if form.get("send_custom") == "1":
                reply = form.get("custom_reply", "")
            else:
                reply = form.get("quick_reply", "")
            message_id = STORE.queue_outbound_message(
                ticket_id,
                reply,
                active_employee(),
            )
            notice = (
                "Ответ поставлен в очередь отправки через WhatsApp"
                if message_id
                else "Не удалось подготовить ответ: проверьте текст и WhatsApp-адрес"
            )
            self.redirect(f"/ticket?id={ticket_id}&notice={quote(notice)}")
        elif parsed.path == "/chat-send":
            chat_id = valid_chat_id(form.get("chat_id", ""))
            message_id = STORE.queue_direct_message(
                chat_id,
                form.get("message", ""),
                active_employee(),
                reply_to_key=normalize_message(form.get("reply_to", ""))[:160],
                request_id=form.get("request_id", ""),
            )
            # Отправка сообщения сама по себе больше не меняет состояние
            # автоответчика. Его включает/выключает только сотрудник кнопкой
            # в карточке диалога.
            self.json_response(
                {"queued": bool(message_id), "message_id": message_id},
                HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
            )
        elif parsed.path == "/chat-mode":
            chat_id = valid_chat_id(form.get("chat_id", ""))
            action = form.get("action", "enable")
            if not chat_id:
                self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
                return
            if action == "disable":
                updated = STORE.disable_manual_chat_mode(chat_id, active_employee())
            else:
                # Явное действие сотрудника: выключить автоответчик до тех пор,
                # пока сотрудник не включит его обратно этой же кнопкой.
                updated = STORE.enable_manual_chat_mode(chat_id, active_employee())
            self.json_response({"updated": bool(updated), "manual_mode": STORE.manual_chat_mode(chat_id) or {}})
        elif parsed.path == "/group-send":
            chat_id = valid_group_id(form.get("chat_id", ""))
            mention_ids = [
                item for item in form.get("mentions", "").split(",")
                if valid_conversation_id(item.strip()) or valid_group_id(item.strip())
            ]
            message_id = STORE.queue_direct_message(
                chat_id,
                form.get("message", ""),
                active_employee(),
                mention_ids,
                reply_to_key=normalize_message(form.get("reply_to", ""))[:160],
                request_id=form.get("request_id", ""),
            )
            self.json_response(
                {"queued": bool(message_id), "message_id": message_id},
                HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
            )
        elif parsed.path == "/chat-action":
            chat_id = valid_conversation_id(form.get("chat_id", ""))
            message_key = normalize_message(form.get("message_id", ""))[:160]
            action = form.get("action", "")
            message = STORE.get_whatsapp_message(chat_id, message_key) if chat_id and message_key else None
            if not message or bool(message.get("deleted")):
                self.json_response({"queued": False, "error": "Недоступное сообщение"}, HTTPStatus.BAD_REQUEST)
                return
            if action in {"edit", "delete"} and not bool(message.get("from_me")):
                self.json_response({"queued": False, "error": "Изменять и удалять можно только наши сообщения"}, HTTPStatus.BAD_REQUEST)
                return
            if action not in {"edit", "delete", "react"}:
                self.json_response({"queued": False, "error": "Неизвестное действие"}, HTTPStatus.BAD_REQUEST)
                return
            action_body = form.get("message", "")
            if action == "react":
                action_body = str(action_body or "")[:32]
                if action_body and action_body not in REACTION_EMOJIS:
                    self.json_response({"queued": False, "error": "Эта реакция пока не поддерживается"}, HTTPStatus.BAD_REQUEST)
                    return
            if action == "delete":
                action_body = json.dumps(
                    {
                        "body": str(message.get("body", "") or ""),
                        "timestamp": int(message.get("message_timestamp", 0) or 0),
                        "media_name": str(message.get("media_name", "") or ""),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            action_id = STORE.queue_whatsapp_action(action, chat_id, message_key, action_body, active_employee())
            self.json_response(
                {"queued": bool(action_id), "action_id": action_id},
                HTTPStatus.OK if action_id else HTTPStatus.BAD_REQUEST,
            )
        elif parsed.path == "/manual":
            title = form.get("title", "Открытие НП").strip()
            reference = form.get("reference", "").strip()
            comment = form.get("comment", "").strip()
            summary_parts = [title]
            if reference:
                summary_parts.append(f"№ {reference}")
            if comment:
                summary_parts.append(comment)
            employee = form.get("employee", "")
            if employee not in EMPLOYEES:
                employee = active_employee()
            manual_payload = {
                "source": "telegram_manual",
                "sender": employee,
                "category": "telegram_manual",
                "priority": form.get("priority", "normal"),
                "title": title,
                "summary": " · ".join(summary_parts),
                "original_text": comment,
                "status": form.get("status", "new"),
                "assigned_to": employee,
            }
            similar = STORE.find_similar_open_ticket(manual_payload, hours=72)
            if similar:
                similar_id = int(similar.get("id", 0) or 0)
                self.redirect(f"/ticket?id={similar_id}&notice={quote(f'Похожая открытая заявка уже существует: №{similar_id}. Новая заявка не создана')}")
                return
            ticket_id = STORE.create_ticket(manual_payload)
            self.redirect(f"/ticket?id={ticket_id}")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def handle_dashboard_data(self, query: dict[str, list[str]]) -> None:
        status = query.get("status", [""])[0]
        category = query.get("category", [""])[0]
        priority = query.get("priority", [""])[0]
        search = query.get("q", [""])[0]
        view = query.get("view", [""])[0]
        if view not in {"", "mine", "handoff", "open", "urgent"}:
            view = ""
        employee = active_employee()
        total = STORE.count_tickets(status, category, priority, search, view, employee)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(query_page(query), pages)
        tickets = STORE.list_tickets(
            status, category, priority, search, PAGE_SIZE, (page - 1) * PAGE_SIZE, view, employee
        )
        self.json_response(
            {
                "version": STORE.dashboard_version(),
                "rows_html": render_ticket_rows(tickets),
                "counts": STORE.counts(),
                "pagination_html": render_pagination(query, total, page),
            }
        )

    def handle_api_message(self) -> None:
        # Фото и голосовые приходят base64 и могут быть заметно больше обычного JSON.
        payload = self.read_authorized_json(18_000_000)
        if payload is None:
            return
        external_id = str(payload.get("external_id", ""))
        phone = normalize_phone(str(payload.get("phone", "")))
        sender = str(payload.get("sender", "")).strip() or "Неизвестный отправитель"
        text = normalize_message(str(payload.get("text", "")))
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        contact_key = chat_id or phone
        media_mime = normalize_message(str(payload.get("media_mime", "")))[:120]
        media_name = normalize_message(str(payload.get("media_name", "") or payload.get("attachment_name", "")))[:180]
        media_path = ""
        media_base64 = str(payload.get("media_base64", "") or "")
        if media_base64 and chat_id and external_id:
            media_path = save_media_payload(chat_id, external_id, media_base64, media_mime, media_name)
        if payload.get("media_receipt"):
            media_path = queue_inbound_media.resolve(MEDIA_DIR, chat_id, external_id, payload["media_receipt"]) or media_path
        if external_id and not STORE.claim_inbound_message(external_id, contact_key):
            self.json_response({"created": False, "duplicate": True})
            return

        # Дополнительная страховка для медиа: если live-sync коннектора не успел
        # сохранить фото/голосовое, API обработки заявки всё равно закрепит файл
        # за тем же сообщением в истории чата. Upsert по message id не создаёт дубль.
        if chat_id and external_id and (media_path or media_name):
            try:
                message_timestamp = max(0, int(payload.get("message_timestamp", 0) or 0))
            except (ValueError, TypeError):
                message_timestamp = 0
            STORE.save_whatsapp_chat_messages(
                chat_id,
                [
                    {
                        "id": external_id,
                        "from_me": False,
                        "sender": sender,
                        "body": text or ("[Вложение]" if media_name else ""),
                        "type": normalize_message(str(payload.get("message_type", "chat")))[:40] or "chat",
                        "timestamp": message_timestamp,
                        "ack": 0,
                        "media_path": media_path,
                        "media_mime": media_mime,
                        "media_name": media_name,
                    }
                ],
            )

        # Контакты из админки остаются полноценной личной перепиской, но без
        # заявок, меню, антиспам-ответов и других автоответов. Важно сохранять
        # входящее сообщение именно здесь: прежний ранний return в коннекторе
        # мог приводить к тому, что сообщение не попадало в панель.
        manual_contact = STORE.manual_whatsapp_contact(chat_id, phone)
        if manual_contact:
            try:
                message_timestamp = max(0, int(payload.get("message_timestamp", 0) or 0))
            except (ValueError, TypeError):
                message_timestamp = 0
            STORE.save_whatsapp_chat_messages(
                chat_id,
                [
                    {
                        "id": external_id,
                        "from_me": False,
                        "sender": str(manual_contact.get("name", "")) or sender,
                        "body": text or ("[Вложение]" if payload.get("attachment_name") else ""),
                        "type": normalize_message(str(payload.get("message_type", "chat")))[:40] or "chat",
                        "timestamp": message_timestamp,
                        "ack": 0,
                        "media_path": media_path,
                        "media_mime": media_mime,
                        "media_name": media_name,
                    }
                ],
            )
            self.json_response(
                {
                    "created": False,
                    "manual_contact": True,
                    "ignored": True,
                    "reason": "Контакт администратора",
                    "name": str(manual_contact.get("name", "")),
                }
            )
            return

        # Если сотрудник явно выключил автоответчик для этого пользователя,
        # автоматика молчит. Состояние меняется только кнопкой сотрудника и не
        # зависит от отправки сообщений вручную или системных автоответов.
        manual_mode = STORE.manual_chat_mode(chat_id) if chat_id else None
        if manual_mode:
            self.json_response(
                {
                    "created": False,
                    "ignored": True,
                    "silent": True,
                    "reason": "Ручной диалог",
                    "manual_mode": manual_mode,
                }
            )
            return

        if chat_id and queue_productivity.auto_reply_blocked(STORE, chat_id):
            self.json_response({
                "created": False,
                "ignored": True,
                "silent": True,
                "reason": "Контакт исключён из автоответов",
            })
            return

        context = STORE.get_conversation_context(contact_key)
        active_ticket = STORE.active_context_ticket(contact_key)
        active_ticket_id = int(active_ticket["id"]) if active_ticket else 0
        context_mode = str(context.get("pending_category", "")) if context else ""
        pending_category = pending_context_category(context)
        force_new_requested = bool(context and context.get("force_new"))
        menu_choice = normalize_message(str(payload.get("menu_choice", "")))
        selected_from_menu = bool(menu_choice)
        typed_choice = parse_menu_number(text)
        chosen_category = configured_menu_category(menu_choice or typed_choice)
        error_report_choice = (
            (context_mode == MENU_CONTEXT and typed_choice == str(error_report_menu_number()))
            or is_error_report_command(text)
        )
        active_tickets_choice = (
            (context_mode == MENU_CONTEXT and typed_choice == str(active_tickets_menu_number()))
            or is_active_tickets_command(text)
        )

        # Первое автоматическое сообщение не показывает меню сразу.
        # Список категорий открывается только после явной цифры 1.
        if context_mode == MENU_GATE_CONTEXT:
            if typed_choice == "1" or is_main_menu_command(text):
                STORE.set_conversation_context(
                    contact_key, MENU_CONTEXT, active_ticket_id, bool(active_ticket)
                )
                self.json_response(
                    {
                        "created": False,
                        "awaiting_category": True,
                        "main_menu": True,
                        "force_menu": True,
                        "reply": main_menu_text(),
                        "menu_options": menu_payload(),
                    }
                )
            else:
                self.json_response(
                    {
                        "created": False,
                        "menu_gate": True,
                        "reply": menu_selection_reminder(),
                    }
                )
            return

        if context_mode == ACTIVE_TICKETS_CONTEXT:
            active_rows = STORE.list_active_user_tickets(chat_id, phone, 10)
            try:
                selected_index = int(typed_choice or "0") - 1
            except (TypeError, ValueError):
                selected_index = -1
            if 0 <= selected_index < len(active_rows):
                selected_ticket = active_rows[selected_index]
                selected_id = int(selected_ticket.get("id", 0) or 0)
                STORE.set_conversation_context(
                    contact_key, ACTIVE_TICKET_FOLLOWUP_CONTEXT, selected_id, False
                )
                self.json_response({
                    "created": False,
                    "active_ticket_selected": True,
                    "ticket_id": selected_id,
                    "reply": active_ticket_selected_reply(selected_ticket),
                })
            else:
                self.json_response({
                    "created": False,
                    "active_tickets": True,
                    "reply": active_tickets_reply(active_rows),
                })
            return

        if context_mode == ACTIVE_TICKET_FOLLOWUP_CONTEXT:
            # Пользователь явно выбрал одну из своих активных заявок. Любой текст
            # или вложение теперь считается дополнением именно к ней до команды 0.
            if not active_ticket:
                active_rows = STORE.list_active_user_tickets(chat_id, phone, 10)
                STORE.set_conversation_context(contact_key, ACTIVE_TICKETS_CONTEXT, 0, False)
                self.json_response({
                    "created": False,
                    "active_tickets": True,
                    "reply": (
                        "Выбранная заявка уже закрыта или больше не активна.\n\n"
                        + active_tickets_reply(active_rows)
                    ),
                })
                return
            attachment_name = normalize_message(str(payload.get("attachment_name", "")))[:240]
            STORE.note_context_followup(active_ticket_id, text, attachment_name)
            if chat_id and external_id:
                STORE.link_whatsapp_message_to_ticket(chat_id, external_id, active_ticket_id)
            self.json_response({
                "created": False,
                "linked": True,
                "ticket_id": active_ticket_id,
                "reply": f"Дополнительная информация добавлена к заявке №{active_ticket_id}.",
            })
            return

        if active_tickets_choice:
            active_rows = STORE.list_active_user_tickets(chat_id, phone, 10)
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(contact_key, ACTIVE_TICKETS_CONTEXT, active_ticket_id, False)
            self.json_response({
                "created": False,
                "active_tickets": True,
                "reply": active_tickets_reply(active_rows),
            })
            return

        if is_acknowledgement(text):
            # «Спасибо», «рахмет», «ок», «понял» и похожие ответы не должны
            # превращаться в заявку даже если до этого остался выбранный пункт меню.
            self.json_response(
                {
                    "created": False,
                    "ignored": True,
                    "silent": True,
                    "reason": "Короткий ответ без новой заявки",
                }
            )
            return

        if is_main_menu_command(text):
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(
                contact_key,
                MENU_CONTEXT,
                active_ticket_id,
                bool(active_ticket),
            )
            self.json_response(
                {
                    "created": False,
                    "awaiting_category": True,
                    "main_menu": True,
                    "force_menu": True,
                    "reply": main_menu_text(),
                    "menu_options": menu_payload(),
                }
            )
            return

        if context_mode == ERROR_REPORT_CONTEXT:
            description = text or (
                f"Вложение: {media_name or str(payload.get('attachment_name', ''))}"
                if (media_name or payload.get("attachment_name")) else ""
            )
            if not description:
                self.json_response({
                    "created": False,
                    "error_report": True,
                    "awaiting_details": True,
                    "reply": "Опишите ошибку текстом или приложите файл / скриншот.",
                })
                return
            report_id = STORE.create_error_report(
                sender=sender,
                phone=phone,
                chat_id=chat_id,
                external_id=external_id,
                description=description,
                attachment_name=media_name or normalize_message(str(payload.get("attachment_name", "")))[:240],
            )
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(contact_key, MENU_CONTEXT, active_ticket_id, bool(active_ticket))
            self.json_response({
                "created": False,
                "error_report_created": True,
                "error_report_id": report_id,
                "reply": (
                    f"Репорт об ошибке №{report_id} сохранён. Он находится отдельно от заявок.\n"
                    "Сотрудники смогут посмотреть его в разделе «Ошибки».\n\n"
                    "Чтобы открыть меню снова, отправьте 0."
                ),
            })
            return

        if error_report_choice:
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(contact_key, ERROR_REPORT_CONTEXT, active_ticket_id, bool(active_ticket))
            self.json_response({
                "created": False,
                "error_report": True,
                "awaiting_details": True,
                "reply": error_report_prompt(),
            })
            return

        if context_mode == SUPPORT_MODE_CONTEXT:
            # В старых сессиях мог сохраниться выбор способа связи. После
            # 3.3.10 звонки для обычных пользователей больше не разрешаются:
            # любой выбор ведёт в текстовую поддержку.
            STORE.set_conversation_context(
                contact_key,
                "support",
                active_ticket_id,
                force_new_requested or bool(active_ticket),
            )
            self.json_response(
                {
                    "created": False,
                    "awaiting_details": True,
                    "category": "support",
                    "reply": configured_category_prompt("support"),
                }
            )
            return

        if (
            active_ticket
            and chosen_category == "package"
            and not context_mode
            and not selected_from_menu
        ):
            STORE.set_conversation_context(
                contact_key,
                MENU_CONTEXT,
                active_ticket_id,
                True,
            )
            self.json_response(
                {
                    "created": False,
                    "awaiting_category": True,
                    "main_menu": True,
                    "force_new": True,
                    "reply": main_menu_text(),
                    "menu_options": menu_payload(),
                }
            )
            return

        if chosen_category:
            STORE.clear_conversation_draft(contact_key)
            if configured_category_action(chosen_category) == MENU_ACTION_INSTRUCTION:
                STORE.set_conversation_context(
                    contact_key,
                    MENU_CONTEXT,
                    active_ticket_id,
                    bool(active_ticket),
                )
                self.json_response(
                    {
                        "created": False,
                        "instruction_only": True,
                        "category": chosen_category,
                        "reply": (
                            f"{configured_category_prompt(chosen_category)}\n\n"
                            "Заявка по этому пункту не создаётся. "
                            "Чтобы вернуться в главное меню, отправьте 0"
                        ),
                    }
                )
                return
            if chosen_category == "support":
                STORE.set_conversation_context(
                    contact_key,
                    "support",
                    active_ticket_id,
                    force_new_requested or bool(active_ticket),
                )
                self.json_response(
                    {
                        "created": False,
                        "awaiting_details": True,
                        "category": "support",
                        "reply": configured_category_prompt("support"),
                    }
                )
                return
            STORE.set_conversation_context(
                contact_key,
                chosen_category,
                active_ticket_id,
                force_new_requested or bool(active_ticket),
            )
            self.json_response(
                {
                    "created": False,
                    "awaiting_details": True,
                    "category": chosen_category,
                    "reply": configured_category_prompt(chosen_category),
                }
            )
            return

        if context_mode == MENU_CONTEXT and not chosen_category:
            # Keep the menu open, so one mistyped answer does not reset the flow.
            self.json_response({
                "created": False, "awaiting_category": True, "menu_reminder": True,
                "reply": "Нужно выбрать номер темы из меню выше. Например, 1 или 2. "
                         "Описание можно прислать сразу после выбора.\n\n"
                         "Если меню потерялось, отправьте 0, и я покажу его снова.",
            })
            return

        # После создания заявки обычные уточнения по ней принимаются молча:
        # система не засыпает человека меню на каждую следующую фразу. Новая
        # заявка начинается только после явной команды 0/«меню» или выбора темы.
        if active_ticket and not pending_category and not chosen_category:
            STORE.note_context_followup(
                active_ticket_id, text, normalize_message(str(payload.get("attachment_name", "")))[:240]
            )
            if chat_id and external_id:
                STORE.link_whatsapp_message_to_ticket(chat_id, external_id, active_ticket_id)
            self.json_response(
                {
                    "created": False,
                    "linked": True,
                    "silent": True,
                    "ticket_id": active_ticket_id,
                    "reason": "Уточнение к активной заявке",
                }
            )
            return

        forced_category = pending_category
        combined_request_text = text
        if forced_category and forced_category != "support":
            existing_draft = STORE.get_conversation_draft(contact_key)
            incoming_draft = extract_request_draft(text, forced_category)
            incoming_draft = apply_expected_request_answer(
                text, forced_category, existing_draft, incoming_draft
            )
            request_draft = merge_request_draft(existing_draft, incoming_draft)
            STORE.set_conversation_draft(contact_key, request_draft)
            missing_keys = missing_request_fields(forced_category, request_draft)
            if missing_keys:
                self.json_response(
                    {
                        "created": False,
                        "awaiting_details": True,
                        "category": forced_category,
                        "missing_fields": missing_keys,
                        "reply": build_missing_request_reply(forced_category, missing_keys),
                    }
                )
                return
            combined_request_text = request_draft_text(forced_category, request_draft) or text
        elif forced_category == "support":
            # В поддержке весь текст является вопросом. Никаких обязательных полей
            # и повторной классификации по словам «БИН», «НП», «перевозка» и т.п.
            # Если пришёл только файл, оставляем понятное описание для карточки.
            combined_request_text = text or (
                f"Вложение: {payload.get('attachment_name', '')}" if payload.get("attachment_name") else ""
            )

        if not forced_category:
            # Первое сообщение обычного пользователя только предлагает открыть меню.
            # Контакты из админки отсекаются выше и автоответов не получают.
            STORE.set_conversation_context(
                contact_key, MENU_GATE_CONTEXT, active_ticket_id, bool(active_ticket)
            )
            self.json_response(
                {
                    "created": False,
                    "menu_gate": True,
                    "force_menu": True,
                    "reply": menu_selection_reminder(),
                }
            )
            return

        result = process_incoming_message(
            sender,
            phone,
            combined_request_text,
            str(payload.get("attachment_name", "")),
            chat_id,
            external_id,
            forced_category,
        )
        if result["created"]:
            result["ticket"]["assigned_to"] = active_employee()
            similar = STORE.find_similar_open_ticket(result["ticket"], hours=72)
            if similar:
                similar_id = int(similar.get("id", 0) or 0)
                STORE.clear_conversation_draft(contact_key)
                STORE.set_conversation_context(
                    contact_key, MENU_CONTEXT, similar_id, False
                )
                self.json_response(
                    {
                        "created": False,
                        "duplicate_ticket": True,
                        "ticket_id": similar_id,
                        "reply": (
                            f"Такая или очень похожая заявка №{similar_id} уже зарегистрирована. "
                            "Повторная заявка не создана, чтобы не создавать дубли.\n\n"
                            "Если это другая проблема, отправьте 0 или слово «меню» и выберите другую тему."
                        ),
                    }
                )
                return
            ticket_id = STORE.create_ticket(result["ticket"])
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(contact_key, "", ticket_id)
            if chat_id and external_id:
                STORE.link_whatsapp_message_to_ticket(chat_id, external_id, ticket_id)
            # Пользователи часто сначала отправляют один или несколько скриншотов,
            # а уже следующим сообщением пишут описание. Такие изображения уже
            # лежат в истории чата с ticket_id=0. Привязываем только свежие
            # входящие изображения этого личного чата и только те, что были
            # отправлены ДО сообщения, создавшего заявку.
            try:
                ticket_message_timestamp = max(0, int(payload.get("message_timestamp", 0) or 0))
            except (TypeError, ValueError):
                ticket_message_timestamp = 0
            pre_ticket_images = STORE.link_recent_whatsapp_images_to_ticket(
                chat_id,
                ticket_id,
                before_timestamp=ticket_message_timestamp,
                lookback_seconds=PRE_TICKET_IMAGE_LOOKBACK_SECONDS,
                limit=5,
            ) if chat_id else []
            self.json_response(
                {
                    "created": True,
                    "ticket_id": ticket_id,
                    "pre_ticket_images": len(pre_ticket_images),
                    "category": result["ticket"]["category"],
                    "title": result["ticket"]["title"],
                    "reply": (
                        support_question_accepted_reply(ticket_id)
                        if result["ticket"]["category"] == "support"
                        else ticket_accepted_reply(ticket_id)
                    ),
                },
                HTTPStatus.CREATED,
            )
            return
        STORE.set_conversation_context(
            contact_key,
            "bin",
            active_ticket_id,
            force_new_requested and pending_category == "bin",
        )
        error_id = STORE.add_template_error(sender, phone, result["missing"], result["reply"])
        self.json_response(
            {
                "created": False,
                "template_error_id": error_id,
                "missing": result["missing"],
                "reply": result["reply"],
            }
        )

    def handle_template_error_delivery(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            error_id = int(payload.get("template_error_id", 0))
        except (ValueError, TypeError):
            error_id = 0
        status = str(payload.get("status", ""))
        if error_id <= 0 or status not in {"sent", "failed"}:
            self.json_response({"error": "invalid_delivery"}, HTTPStatus.BAD_REQUEST)
            return
        STORE.update_template_error_delivery(
            error_id,
            status,
            str(payload.get("provider_id", "")),
            str(payload.get("error", ""))[:500],
        )
        self.json_response({"updated": True})

    def handle_outbound_claim(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            created_after_epoch = int(payload.get("created_after", 0))
        except (ValueError, TypeError):
            created_after_epoch = 0
        created_after = ""
        if 0 < created_after_epoch <= int(datetime.now(timezone.utc).timestamp()) + 60:
            created_after = datetime.fromtimestamp(
                created_after_epoch,
                timezone.utc,
            ).replace(microsecond=0).isoformat()
        message = STORE.claim_outbound_message(created_after)
        if not message:
            self.json_response({"message": None})
            return
        # Preserve the original upload before WhatsApp can consume it.
        if message.get("media_path"):
            try:
                preserved = queue_uploads.preserve_sent_attachment(sys.modules[__name__], message["id"])
                if not preserved: raise OSError("Не удалось сохранить локальную копию вложения")
            except OSError as error:
                STORE.complete_outbound_message(message["id"], False, str(error))
                self.json_response({"message": None, "error": str(error)[:180]})
                return
        try:
            mentions = json.loads(str(message.get("mentions_json", "[]") or "[]"))
            if not isinstance(mentions, list):
                mentions = []
        except (TypeError, ValueError, json.JSONDecodeError):
            mentions = []
        reply_key = normalize_message(str(message.get("reply_to_key", "")))[:160]
        reply_message = STORE.get_whatsapp_message(valid_conversation_id(str(message.get("chat_id", ""))), reply_key) if reply_key else None
        reply_preview_body = ""
        reply_preview_sender = ""
        if reply_message:
            reply_preview_body = normalize_message(str(reply_message.get("body", "") or reply_message.get("media_name", "") or "Вложение"))[:1200]
            reply_preview_sender = "Вы" if bool(reply_message.get("from_me")) else normalize_message(str(reply_message.get("sender", "") or "Пользователь"))[:100]
        self.json_response(
            {
                "message": {
                    "id": message["id"],
                    "ticket_id": message["ticket_id"],
                    "chat_id": message["chat_id"],
                    "phone": normalize_phone(message["phone"]),
                    "body": message["body"],
                    "mentions": mentions[:100],
                    "reply_to_key": message.get("reply_to_key", ""),
                    "reply_preview_body": reply_preview_body,
                    "reply_preview_sender": reply_preview_sender,
                    "media_path": message.get("media_path", ""),
                    "media_mime": message.get("media_mime", ""),
                    "media_name": message.get("media_name", ""),
                }
            }
        )

    def handle_outbound_result(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            message_id = int(payload.get("message_id", 0))
        except (ValueError, TypeError):
            message_id = 0
        sent = payload.get("status") == "sent"
        if message_id <= 0 or payload.get("status") not in {"sent", "failed", "uncertain"}:
            self.json_response({"error": "invalid_result"}, HTTPStatus.BAD_REQUEST)
            return
        provider_id = normalize_message(str(payload.get("provider_id", "")))[:180]
        if payload.get('status') == 'uncertain':
            updated = STORE.mark_outbound_uncertain(message_id, str(payload.get('error', '')))
            self.json_response({'updated': bool(updated), 'queue_status': str((updated or {}).get('status', ''))})
            return
        sent_media = queue_uploads.preserve_sent_attachment(sys.modules[__name__],message_id) if sent else {}
        updated = STORE.complete_outbound_message(
            message_id,
            sent,
            str(payload.get("error", ""))[:500],
            provider_id,
        )
        # Сразу сохраняем успешное исходящее сообщение в истории. Это делает
        # ответ с цитатой видимым в нашей системе даже если whatsapp-web.js
        # вернул sentMessage без заполненного hasQuotedMsg и live-sync догонит позже.
        if sent and updated and provider_id:
            chat_id = valid_conversation_id(str(updated.get("chat_id", "")))
            if chat_id:
                reply_key = normalize_message(str(payload.get("reply_to_key", "") or updated.get("reply_to_key", "")))[:160]
                quoted = STORE.get_whatsapp_message(chat_id, reply_key) if reply_key else None
                quoted_body = normalize_message(str(payload.get("quoted_body", "")))[:1200]
                quoted_sender = normalize_message(str(payload.get("quoted_sender", "")))[:100]
                if quoted:
                    if not quoted_body:
                        quoted_body = normalize_message(str(quoted.get("body", "") or quoted.get("media_name", "") or "Вложение"))[:1200]
                    if not quoted_sender:
                        quoted_sender = "Вы" if bool(quoted.get("from_me")) else normalize_message(str(quoted.get("sender", "") or "Пользователь"))[:100]
                if reply_key and not quoted_body:
                    quoted_body = "Сообщение"
                if reply_key and not quoted_sender:
                    quoted_sender = "Сообщение"
                STORE.save_whatsapp_chat_messages(chat_id, [{
                    "id": provider_id,
                    "from_me": True,
                    "sender": normalize_message(str(updated.get("actor", "") or "Вы"))[:100],
                    "body": normalize_message(str(updated.get("body", "")))[:32000],
                    "type": "chat",
                    "timestamp": int(datetime.now(timezone.utc).timestamp()),
                    "ack": 1,
                    "quoted_message_key": reply_key,
                    "quoted_body": quoted_body,
                    "quoted_sender": quoted_sender,
                    **sent_media,
                }])
        self.json_response({"updated": bool(updated), "queue_status": str((updated or {}).get("status", ""))})

    def handle_chat_list_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        cleaned: list[dict[str, object]] = []
        raw_chats = payload.get("chats")
        if isinstance(raw_chats, list):
            for item in raw_chats[:100]:
                if not isinstance(item, dict):
                    continue
                chat_id = valid_chat_id(str(item.get("id", "")))
                if not chat_id:
                    continue
                # QUEUE_3_3_95_CHAT_LIST_CANONICAL
                chat_id = STORE.canonical_whatsapp_chat_id(chat_id)
                try:
                    timestamp = int(item.get("timestamp", 0))
                    unread = max(0, int(item.get("unread_count", 0)))
                except (ValueError, TypeError):
                    timestamp, unread = 0, 0
                cleaned.append(
                    {
                        "id": chat_id,
                        "name": normalize_message(str(item.get("name", "")))[:100]
                        or chat_id.split("@")[0],
                        "last_message": normalize_message(
                            str(item.get("last_message", ""))
                        )[:160],
                        "timestamp": timestamp,
                        "unread_count": unread,
                        "last_from_me": bool(item.get("last_from_me")),
                    }
                )
        # QUEUE_3_3_95_CHAT_LIST_DEDUPE
        deduped_chats: dict[str, dict[str, object]] = {}
        for row in cleaned:
            cid = str(row.get("id", ""))
            if not cid:
                continue
            previous = deduped_chats.get(cid)
            if previous is None:
                deduped_chats[cid] = row
                continue
            prev_ts = int(previous.get("timestamp", 0) or 0)
            row_ts = int(row.get("timestamp", 0) or 0)
            newer, older = (row, previous) if row_ts >= prev_ts else (previous, row)
            merged = dict(older)
            merged.update(newer)
            merged["unread_count"] = max(int(previous.get("unread_count", 0) or 0), int(row.get("unread_count", 0) or 0))
            # Preserve a useful name if the newer technical row has only digits.
            newer_name = str(merged.get("name", "") or "").strip()
            older_name = str(older.get("name", "") or "").strip()
            if (not newer_name or newer_name.replace("+", "").replace(" ", "").isdigit()) and older_name:
                merged["name"] = older_name
            deduped_chats[cid] = merged
        cleaned = list(deduped_chats.values())

        with CHAT_LOCK:
            CHAT_STATE["connected"] = bool(payload.get("connected", True))
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
            if cleaned or isinstance(raw_chats, list):
                CHAT_STATE["chats"] = cleaned
        STORE.upsert_whatsapp_chats(cleaned)
        self.json_response({"updated": True, "chats": len(cleaned)})

    def handle_presence_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        rows = payload.get("items", [])
        if not isinstance(rows, list):
            rows = []
        cleaned: dict[str, dict[str, object]] = {}
        now = datetime.now(timezone.utc).isoformat()
        for row in rows[:80]:
            if not isinstance(row, dict):
                continue
            chat_id = valid_chat_id(str(row.get("chat_id", "") or ""))
            if not chat_id or chat_id.endswith("@g.us"):
                continue
            cleaned[chat_id] = {
                "known": bool(row.get("known")),
                "online": bool(row.get("online")),
                "state": str(row.get("state", "") or "")[:32],
                "updated_at": now,
            }
        with CHAT_LOCK:
            presence_map = CHAT_STATE.get("presence")
            if not isinstance(presence_map, dict):
                presence_map = {}
                CHAT_STATE["presence"] = presence_map
            presence_map.update(cleaned)
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "items": len(cleaned)})

    def handle_group_participants_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_group_id(str(payload.get("chat_id", "")))
        raw = payload.get("participants", [])
        if not chat_id or not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, object]] = []
        for item in raw[:300]:
            if not isinstance(item, dict):
                continue
            mention_id = valid_chat_id(str(item.get("mention_id", "")))
            if not mention_id:
                continue
            resolved_id = valid_chat_id(str(item.get("resolved_id", "")))
            cleaned.append({
                "mention_id": mention_id,
                "resolved_id": resolved_id,
                "name": normalize_message(str(item.get("name", "")))[:100] or mention_id.split("@")[0],
                "phone": normalize_phone(str(item.get("phone", ""))),
                "is_admin": bool(item.get("is_admin")),
                "is_me": bool(item.get("is_me")),
            })
        with CHAT_LOCK:
            participants_map = CHAT_STATE.get("group_participants")
            if not isinstance(participants_map, dict):
                participants_map = {}
                CHAT_STATE["group_participants"] = participants_map
            participants_map[chat_id] = cleaned
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "participants": len(cleaned)})

    def handle_group_message_identities_sync(self) -> None:
        payload = self.read_authorized_json(2_000_000)
        if payload is None:
            return
        chat_id = valid_group_id(str(payload.get("chat_id", "")))
        raw = payload.get("identities", [])
        if not chat_id or not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, str]] = []
        generic = {"", "участник", "участник группы", "пользователь whatsapp"}
        for item in raw[:100]:
            if not isinstance(item, dict):
                continue
            message_id = normalize_message(str(item.get("id", "")))[:160]
            if not message_id:
                continue
            sender = normalize_message(str(item.get("sender", "")))[:100]
            sender_phone = normalize_phone(str(item.get("sender_phone", "")))
            sender_id = valid_chat_id(str(item.get("sender_id", "")))
            cleaned.append({
                "id": message_id,
                "sender": sender,
                "sender_phone": sender_phone,
                "sender_id": sender_id,
            })
        updated = STORE.update_whatsapp_group_message_identities(chat_id, cleaned)

        # Apply the same enrichment to the in-memory copy so the open group
        # changes immediately, without requiring a page reload or DB roundtrip.
        by_id = {item["id"]: item for item in cleaned}
        with CHAT_LOCK:
            messages_map = CHAT_STATE.get("messages", {})
            messages = messages_map.get(chat_id, []) if isinstance(messages_map, dict) else []
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    patch = by_id.get(str(message.get("id", "")))
                    if not patch:
                        continue
                    current_sender = normalize_message(str(message.get("sender", "")))[:100]
                    current_folded = current_sender.casefold()
                    new_sender = patch.get("sender", "")
                    new_folded = new_sender.casefold()
                    current_generic = current_folded in generic or bool(re.fullmatch(r"[+\d\s().-]+", current_sender or ""))
                    new_useful = bool(new_sender) and new_folded not in generic and not bool(re.fullmatch(r"[+\d\s().-]+", new_sender))
                    if current_generic and new_useful:
                        message["sender"] = new_sender
                    if patch.get("sender_phone"):
                        message["sender_phone"] = patch["sender_phone"]
                    if patch.get("sender_id"):
                        message["sender_id"] = patch["sender_id"]
                CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "messages": updated})

    def handle_contact_list_sync(self) -> None:
        payload = self.read_authorized_json(4_000_000)
        if payload is None:
            return
        raw = payload.get("contacts", [])
        if not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned = []
        for item in raw[:1000]:
            if not isinstance(item, dict):
                continue
            chat_id = valid_chat_id(str(item.get("chat_id", "")))
            phone = normalize_phone(str(item.get("phone", "")))
            if not chat_id and phone:
                chat_id = f"{phone.lstrip('+')}@c.us"
            if not chat_id:
                continue
            raw_id = valid_chat_id(str(item.get("raw_id", "")))
            cleaned.append({"chat_id": chat_id, "raw_id": raw_id, "phone": phone, "name": normalize_message(str(item.get("name", "")))[:100], "saved": bool(item.get("saved"))})
            if raw_id.endswith("@lid") and chat_id.endswith("@c.us") and raw_id != chat_id:
                STORE.remember_whatsapp_chat_alias(raw_id, chat_id)
        authoritative_names: dict[str, str] = {}
        for item in cleaned:
            display_name = whatsapp_display_name(item.get("name", ""))
            chat_key = valid_chat_id(str(item.get("chat_id", "")))
            phone = normalize_phone(str(item.get("phone", "")))
            if display_name and chat_key:
                authoritative_names[chat_key] = display_name
                STORE.update_whatsapp_contact_display_name(chat_key, display_name, phone)
            if display_name and phone:
                authoritative_names[f"{phone.lstrip('+')}@c.us"] = display_name
        with CHAT_LOCK:
            CHAT_STATE["discovered_contacts"] = cleaned
            live = CHAT_STATE.get("chats", [])
            if isinstance(live, list):
                for chat in live:
                    if not isinstance(chat, dict):
                        continue
                    cid = valid_chat_id(str(chat.get("id", "")))
                    if cid and cid in authoritative_names:
                        chat["name"] = authoritative_names[cid]
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "contacts": len(cleaned)})

    def handle_contact_profile_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        if not chat_id:
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        profile = {"chat_id": chat_id, "name": normalize_message(str(payload.get("name", "")))[:100], "phone": normalize_phone(str(payload.get("phone", ""))), "about": normalize_message(str(payload.get("about", "")))[:500], "profile_pic_url": str(payload.get("profile_pic_url", ""))[:2000], "is_business": bool(payload.get("is_business"))}
        display_name = whatsapp_display_name(profile.get("name", ""))
        if display_name:
            STORE.update_whatsapp_contact_display_name(chat_id, display_name, str(profile.get("phone", "")))
        with CHAT_LOCK:
            profiles = CHAT_STATE.get("contact_profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                CHAT_STATE["contact_profiles"] = profiles
            profiles[chat_id] = profile
            if display_name:
                live = CHAT_STATE.get("chats", [])
                if isinstance(live, list):
                    for chat in live:
                        if isinstance(chat, dict) and valid_chat_id(str(chat.get("id", ""))) == chat_id:
                            chat["name"] = display_name
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_group_refresh_check(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        self.json_response({"request": STORE.get_setting("group_refresh_request", "")})

    def handle_group_list_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        cleaned: list[dict[str, object]] = []
        raw_groups = payload.get("groups", [])
        if not isinstance(raw_groups, list):
            self.json_response({"error": "invalid_groups"}, HTTPStatus.BAD_REQUEST)
            return
        for item in raw_groups[:500]:
            if not isinstance(item, dict):
                continue
            chat_id = valid_group_id(str(item.get("id", "")))
            if not chat_id:
                continue
            try:
                participant_count = max(0, int(item.get("participant_count", 0) or 0))
            except (TypeError, ValueError):
                participant_count = 0
            cleaned.append(
                {
                    "id": chat_id,
                    "name": normalize_message(str(item.get("name", "")))[:100]
                    or "Группа WhatsApp",
                    "participant_count": participant_count,
                }
            )
        diagnostics = payload.get("diagnostics", {})
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        status = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": len(cleaned),
            "chats_ok": bool(diagnostics.get("chats_ok")),
            "chats_total": int(diagnostics.get("chats_total", 0) or 0),
            "chats_groups": int(diagnostics.get("chats_groups", 0) or 0),
            "contacts_ok": bool(diagnostics.get("contacts_ok")),
            "contacts_total": int(diagnostics.get("contacts_total", 0) or 0),
            "contacts_groups": int(diagnostics.get("contacts_groups", 0) or 0),
            "enriched": int(diagnostics.get("enriched", 0) or 0),
            "errors": [str(item)[:240] for item in diagnostics.get("errors", [])[:4]]
            if isinstance(diagnostics.get("errors", []), list)
            else [],
        }
        STORE.set_setting("group_sync_status", json.dumps(status, ensure_ascii=False))
        STORE.replace_whatsapp_groups(cleaned)
        self.json_response({"updated": True, "groups": len(cleaned)})

    def handle_connector_state(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        status = normalize_message(str(payload.get("status", "offline"))).casefold()
        if status not in {"offline", "connecting", "qr", "ready"}:
            self.json_response({"error": "invalid_state"}, HTTPStatus.BAD_REQUEST)
            return
        qr_data_url = str(payload.get("qr_data_url", ""))
        if qr_data_url and (
            not qr_data_url.startswith("data:image/png;base64,")
            or len(qr_data_url) > 1_000_000
        ):
            self.json_response({"error": "invalid_qr"}, HTTPStatus.BAD_REQUEST)
            return
        with CHAT_LOCK:
            CHAT_STATE["connector_status"] = status
            CHAT_STATE["connected"] = status == "ready"
            CHAT_STATE["qr_data_url"] = qr_data_url if status == "qr" else ""
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_contact_policy(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        contact = STORE.manual_whatsapp_contact(chat_id, phone)
        self.json_response(
            {
                "manual_contact": bool(contact),
                "name": str((contact or {}).get("name", "")),
                "allow_calls": bool(contact),
            }
        )

    def handle_call_permission(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        if STORE.is_manual_whatsapp_contact(chat_id, phone):
            self.json_response({"allowed": True, "manual_contact": True})
            return
        # Обычным пользователям звонки всегда запрещены. Старое одноразовое
        # разрешение (если осталось от версии с пунктом «Позвонить») просто
        # поглощаем и не используем.
        if chat_id:
            STORE.consume_whatsapp_call_permission(chat_id)
        self.json_response({"allowed": False, "manual_contact": False})

    def handle_chat_messages_sync(self) -> None:
        payload = self.read_authorized_json(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        source_chat_id = valid_conversation_id(str(payload.get("source_chat_id", "")))
        # QUEUE_3_3_95_ALIAS_HINT
        if source_chat_id.endswith("@lid") and chat_id.endswith("@c.us") and source_chat_id != chat_id:
            try:
                STORE.remember_whatsapp_chat_alias(source_chat_id, chat_id)
            except Exception as exc:
                print(f"WhatsApp alias merge warning: {source_chat_id} -> {chat_id}: {exc}", flush=True)
        if chat_id:
            chat_id = STORE.canonical_whatsapp_chat_id(chat_id)
        raw_messages = payload.get("messages", [])
        if not chat_id or not isinstance(raw_messages, list):
            self.json_response({"error": "invalid_chat"}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, object]] = []
        local_media_recovered: list[str] = []
        deleted_payloads: list[tuple[str, str, int, bool]] = []
        for item in raw_messages[-80:]:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = int(item.get("timestamp", 0))
                ack = max(0, min(4, int(item.get("ack", 0) or 0)))
            except (ValueError, TypeError):
                timestamp, ack = 0, 0
            message_id = normalize_message(str(item.get("id", "")))[:160]
            is_deleted = bool(item.get("deleted"))
            if is_deleted and message_id:
                old_media_path = ""
                try:
                    stored_message = STORE.get_whatsapp_message(chat_id, message_id)
                    if stored_message:
                        old_media_path = str(stored_message.get("media_path", "") or "")
                except Exception:
                    old_media_path = ""
                deleted_payloads.append((message_id, old_media_path, timestamp, bool(item.get("from_me"))))
            try:
                outbound_message_id = max(0, int(item.get("outbound_message_id", 0) or 0))
            except (ValueError, TypeError):
                outbound_message_id = 0
            media_mime = normalize_message(str(item.get("media_mime", "")))[:120]
            media_name = normalize_message(str(item.get("media_name", "")))[:180]
            media_path = ""
            media_base64 = str(item.get("media_base64", "") or "")
            if media_base64 and message_id and not is_deleted:
                media_path = save_media_payload(
                    chat_id, message_id, media_base64, media_mime, media_name
                )
            if item.get("media_receipt") and not is_deleted:
                media_path = queue_inbound_media.resolve(MEDIA_DIR, chat_id, message_id, item["media_receipt"]) or media_path
            # Strongest path for our own UI sends: the connector supplies the
            # exact outbound queue id after WhatsApp has accepted the message.
            # preserve_sent_attachment is idempotent and returns the already
            # preserved /media/outbound-N.bin even after the temporary upload
            # was deleted from the outbound queue.
            if bool(item.get("from_me")) and outbound_message_id > 0 and not media_path and not is_deleted:
                preserved_by_queue = queue_uploads.preserve_sent_attachment(sys.modules[__name__], outbound_message_id)
                if preserved_by_queue:
                    media_path = str(preserved_by_queue.get("media_path", ""))
                    media_mime = media_mime or str(preserved_by_queue.get("media_mime", ""))
                    media_name = media_name or str(preserved_by_queue.get("media_name", ""))
            # A message sent from this web UI may be echoed by WhatsApp as a
            # caption-only observation.  Reattach the copy preserved when the
            # outbound queue completed, so our own system shows the same media
            # that is visible in WhatsApp.
            if bool(item.get("from_me")) and message_id and not media_path and not is_deleted:
                preserved_media = preserved_system_outgoing_media(message_id)
                if preserved_media:
                    media_path = str(preserved_media.get("media_path", ""))
                    media_mime = media_mime or str(preserved_media.get("media_mime", ""))
                    media_name = media_name or str(preserved_media.get("media_name", ""))
            if bool(item.get("from_me")) and message_id and not media_path and not is_deleted:
                recovered_media = queue_uploads.recover_clipboard_attachment(
                    sys.modules[__name__],
                    chat_id,
                    message_id,
                    timestamp,
                    str(item.get("body", "")),
                    str(item.get("type", "")),
                )
                if recovered_media:
                    media_path = str(recovered_media.get("media_path", ""))
                    media_mime = media_mime or str(recovered_media.get("media_mime", ""))
                    media_name = media_name or str(recovered_media.get("media_name", ""))
                    local_media_recovered.append(message_id)
            raw_mentions = item.get("mentions", [])
            if not isinstance(raw_mentions, list):
                raw_mentions = []
            cleaned.append(
                {
                    "id": message_id,
                    "from_me": bool(item.get("from_me")),
                    "sender": normalize_message(str(item.get("sender", "")))[:100],
                    "sender_phone": normalize_phone(str(item.get("sender_phone", ""))),
                    "sender_id": valid_chat_id(str(item.get("sender_id", ""))),
                    "body": normalize_message(str(item.get("body", "")))[:32000],
                    "type": normalize_message(str(item.get("type", "chat")))[:40],
                    "timestamp": timestamp,
                    "ack": ack,
                    "notify": bool(item.get("notify")),
                    "media_path": media_path,
                    "media_mime": media_mime,
                    "media_name": media_name,
                    "transcript": normalize_message(str(item.get("transcript", "")))[:12000] if media_mime.startswith("audio/") else "",
                    "mentions": raw_mentions[:100],
                    "quoted_message_key": normalize_message(str(item.get("quoted_message_key", "")))[:160],
                    "quoted_body": normalize_message(str(item.get("quoted_body", "")))[:1200],
                    "quoted_sender": normalize_message(str(item.get("quoted_sender", "")))[:100],
                    "forwarded": bool(item.get("forwarded")),
                    "edited": bool(item.get("edited")),
                    "edit_timestamp": max(0, int(item.get("edit_timestamp", 0) or 0)),
                    "deleted": is_deleted,
                    "reactions": item.get("reactions", {}) if isinstance(item.get("reactions", {}), (dict, list)) else {},
                }
            )
        # 3.3.117: reconcile the revoke event with the row that is already
        # visible in our history before touching live state or persisting a tombstone.
        matched_deleted_keys: set[str] = set()
        for deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me in deleted_payloads:
            resolved_key, matched_count = scrub_deleted_whatsapp_message(
                chat_id, deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me
            )
            if matched_count > 0 and resolved_key:
                matched_deleted_keys.add(resolved_key)
                for cleaned_item in cleaned:
                    if bool(cleaned_item.get("deleted")) and same_whatsapp_message_identity(
                        str(cleaned_item.get("id", "")), deleted_message_id
                    ):
                        cleaned_item["id"] = resolved_key

        with CHAT_LOCK:
            messages_by_chat = CHAT_STATE.get("messages")
            if not isinstance(messages_by_chat, dict):
                messages_by_chat = {}
                CHAT_STATE["messages"] = messages_by_chat
            if payload.get("append"):
                combined = [
                    *list(messages_by_chat.get(chat_id, [])),
                    *cleaned,
                ]
                unique: dict[str, dict[str, object]] = {}
                for item in combined:
                    message_id = str(item.get("id", ""))
                    key = message_id or (
                        f"{int(item.get('timestamp', 0) or 0)}:"
                        f"{int(bool(item.get('from_me')))}:{item.get('body', '')}"
                    )
                    unique[key] = item
                messages_by_chat[chat_id] = sorted(
                    unique.values(),
                    key=lambda item: int(item.get("timestamp", 0) or 0),
                )[-80:]
            else:
                messages_by_chat[chat_id] = cleaned
            if len(messages_by_chat) > 20:
                for old_chat_id in list(messages_by_chat)[:-20]:
                    messages_by_chat.pop(old_chat_id, None)
            CHAT_STATE["connected"] = True
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Only live append events are persisted. Historical messages from the
        # linked WhatsApp account are deliberately never imported.
        if payload.get("append"):
            # If the original DB row was found, it was already scrubbed in place.
            # Do not insert a second "Сообщение удалено" row under a different
            # @lid/@c.us alias. Only persist a tombstone when no original exists.
            persist_items = [
                item for item in cleaned
                if not bool(item.get("deleted")) or str(item.get("id", "")) not in matched_deleted_keys
            ]
            if persist_items:
                STORE.save_whatsapp_chat_messages(chat_id, persist_items)
            workflow_items = [item for item in cleaned if not bool(item.get("deleted"))]
            try:
                if workflow_items:
                    queue_workflow.auto_link_open_ticket(STORE, chat_id, workflow_items)
            except Exception as exc:
                print(f"Автопривязка сообщения к заявке пропущена: {exc}", flush=True)
            for voice in workflow_items:
                if not voice.get("from_me") and str(voice.get("media_mime","")).startswith("audio/") and not voice.get("transcript"):
                    key = (chat_id,str(voice.get("id","")))
                    with VOICE_JOBS.lock:
                        unseen = key not in VOICE_JOBS.states
                    if unseen:
                        VOICE_JOBS.submit(*key)
            # Важно: исходящее сообщение, в том числе системный автоответ, не
            # должно автоматически выключать автоответчик. Состояние меняет
            # только сотрудник через /chat-mode. Старое поле
            # enable_manual_mode намеренно игнорируется для совместимости с
            # коннектором предыдущей версии.
        self.json_response({
            "updated": True,
            "messages": len(cleaned),
            "local_media_recovered": list(dict.fromkeys(local_media_recovered)),
        })

    def handle_chat_message_ack(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        message_id = normalize_message(str(payload.get("message_id", "")))[:160]
        try:
            ack = max(0, min(4, int(payload.get("ack", 0))))
        except (ValueError, TypeError):
            ack = -1
        if not chat_id or not message_id or ack < 0:
            self.json_response({"error": "invalid_ack"}, HTTPStatus.BAD_REQUEST)
            return
        updated = STORE.update_whatsapp_message_ack(chat_id, message_id, ack)
        with CHAT_LOCK:
            messages_by_chat = CHAT_STATE.get("messages", {})
            if isinstance(messages_by_chat, dict):
                for messages in messages_by_chat.values():
                    if not isinstance(messages, list):
                        continue
                    for item in messages:
                        if isinstance(item, dict) and item.get("id") == message_id:
                            item["ack"] = max(int(item.get("ack", 0) or 0), ack)
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": updated, "ack": ack})

    def handle_chat_reaction_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        message_id = normalize_message(str(payload.get("message_id", "")))[:160]
        sender_id = normalize_message(str(payload.get("sender_id", "")))[:160]
        emoji = str(payload.get("emoji", "") or "")[:32]
        from_me = bool(payload.get("from_me"))
        if not chat_id or not message_id or (not from_me and not sender_id):
            self.json_response({"updated": False, "error": "invalid_reaction"}, HTTPStatus.BAD_REQUEST)
            return
        updated = STORE.update_whatsapp_message_reaction(
            chat_id, message_id, sender_id or "__me__", emoji, from_me=from_me
        )
        message = STORE.get_whatsapp_message(chat_id, message_id) if updated else None
        reactions = STORE._reaction_summary((message or {}).get("reactions_json", "{}"))
        with CHAT_LOCK:
            messages_by_chat = CHAT_STATE.get("messages", {})
            if isinstance(messages_by_chat, dict):
                live = messages_by_chat.get(chat_id, [])
                if isinstance(live, list):
                    for item in live:
                        if isinstance(item, dict) and str(item.get("id", "")) == message_id:
                            item["reactions"] = reactions
                            break
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": updated, "reactions": reactions})

    def handle_chat_history(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        cursor = normalize_message(query.get("cursor", [""])[0])[:80]
        if not chat_id:
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Не выбран чат"}, HTTPStatus.BAD_REQUEST)
            return
        page = STORE.list_saved_whatsapp_messages_page(chat_id, 50, cursor)
        messages = decorate_chat_messages(chat_id, dedupe_whatsapp_messages(list(page.get("messages", []))))
        self.json_response({
            "messages": messages,
            "has_more": bool(page.get("has_more")),
            "cursor": str(page.get("cursor", "")),
        })

    def handle_message_window(self, query: dict[str, list[str]]) -> None:
        """Return a history page containing the message referenced by a quote.

        WhatsApp Web does not always expose the same identifier for a quoted
        message that whatsapp-web.js exposes for the original message.  Some
        builds return only stanzaId.  Therefore we first try the exact id and,
        if that fails, resolve the original by quoted text/sender immediately
        before the reply.
        """
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        requested_id = normalize_message(query.get("message_id", [""])[0])[:160]
        quoted_body = normalize_message(query.get("quoted_body", [""])[0])[:1200]
        quoted_sender = normalize_message(query.get("quoted_sender", [""])[0])[:100]
        try:
            before_ts = max(0, int(query.get("before_ts", ["0"])[0] or 0))
        except (TypeError, ValueError):
            before_ts = 0
        if not chat_id or not (requested_id or quoted_body):
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Не выбран чат или сообщение"}, HTTPStatus.BAD_REQUEST)
            return

        target = STORE.resolve_whatsapp_message(chat_id, requested_id)
        resolved_id = str(target["message_key"]) if target else ""
        found_page = None

        if not target or bool(target.get("deleted")):
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Сообщение недоступно"}, HTTPStatus.NOT_FOUND)
            return

        if found_page is not None:
            page = found_page
            raw_messages = list(page.get("messages", []))
        else:
            try:
                target_ts = int(target.get("message_timestamp") or target.get("timestamp") or 0)
                target_row = int(target.get("id") or target.get("history_row_id") or 0)
            except (TypeError, ValueError):
                target_ts = target_row = 0
            cursor_before_target = f"{target_ts}:{target_row + 1}" if target_row > 0 else ""
            page = STORE.list_saved_whatsapp_messages_page(chat_id, 100, cursor_before_target)
            raw_messages = list(page.get("messages", []))
            if not any(str(item.get("id", "")) == resolved_id for item in raw_messages):
                raw_messages = []
                cursor = ""
                for _ in range(50):
                    candidate = STORE.list_saved_whatsapp_messages_page(chat_id, 100, cursor)
                    batch = list(candidate.get("messages", []))
                    if any(str(item.get("id", "")) == resolved_id for item in batch):
                        raw_messages = batch
                        page = candidate
                        break
                    if not candidate.get("has_more") or not candidate.get("cursor"):
                        break
                    cursor = str(candidate.get("cursor", ""))
        if not raw_messages:
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Сообщение не найдено в истории"}, HTTPStatus.NOT_FOUND)
            return
        messages = decorate_chat_messages(chat_id, dedupe_whatsapp_messages(raw_messages))
        self.json_response({
            "messages": messages,
            "has_more": bool(page.get("has_more")),
            "cursor": str(page.get("cursor", "")),
            "target_id": resolved_id,
        })

    def handle_chat_media(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        message_id = normalize_message(query.get("message_id", [""])[0])[:160]
        message = STORE.get_whatsapp_message(chat_id, message_id) if chat_id and message_id else None
        if not message or bool(message.get("deleted")):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        media_path = str(message.get("media_path", "") or "")
        if not media_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            path = Path(media_path).resolve()
            media_root = MEDIA_DIR.resolve()
            if media_root not in path.parents or not path.is_file():
                raise ValueError
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = str(message.get("media_mime", "") or "application/octet-stream")
        download = query.get("download", [""])[0] == "1"
        media_name = str(message.get("media_name", "") or "Вложение")
        # Active documents must not execute in the application's origin.
        mime = queue_uploads.safe_mime(content_type)
        previewable = mime.startswith(('audio/','video/')) or mime in {
            'image/jpeg','image/png','image/webp','image/gif','application/pdf','text/plain'
        }
        disposition = content_disposition_header("attachment" if download or not previewable else "inline", media_name)
        queue_uploads.serve_range(self, path, content_type, disposition)

    def handle_chat_media_send(self) -> None:
        payload = self.read_json_body(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        body = normalize_message(str(payload.get("message", "")))[:1000]
        reply_to = normalize_message(str(payload.get("reply_to", "")))[:160]
        mimetype = normalize_message(str(payload.get("mimetype", "")))[:120]
        filename = normalize_message(str(payload.get("filename", "")))[:180] or "Вложение"
        media_base64 = str(payload.get("media_base64", "") or "")
        if not chat_id or not media_base64:
            self.json_response({"queued": False, "error": "Не выбран чат или файл"}, HTTPStatus.BAD_REQUEST)
            return
        valid_upload, upload_error = queue_reliability.upload_validation(filename, mimetype, len(media_base64), MAX_MEDIA_BYTES)
        if not valid_upload:
            self.json_response({"queued": False, "error": upload_error}, HTTPStatus.BAD_REQUEST)
            return
        media_path = save_outbound_media(media_base64, mimetype, filename)
        if not media_path:
            self.json_response({"queued": False, "error": queue_performance.friendly_error(f"Не удалось сохранить вложение. Проверьте файл и лимит {MAX_MEDIA_BYTES // (1024*1024)} МБ")}, HTTPStatus.BAD_REQUEST)
            return
        mentions = payload.get("mentions", [])
        if not isinstance(mentions, list):
            mentions = []
        message_id = STORE.queue_direct_message(
            chat_id, body, active_employee(), [str(x) for x in mentions[:100]],
            reply_to_key=reply_to, media_path=media_path, media_mime=mimetype, media_name=filename,
        )
        if not message_id:
            try: Path(media_path).unlink()
            except OSError: pass
        self.json_response({"queued": bool(message_id), "message_id": message_id}, HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST)

    def handle_chat_forward(self) -> None:
        payload = self.read_json_body(250_000)
        if payload is None:
            return
        source_chat = valid_conversation_id(str(payload.get("source_chat_id", "")))
        target_chat = valid_conversation_id(str(payload.get("target_chat_id", "")))
        raw_ids = payload.get("message_ids", [])
        if not source_chat or not target_chat or not isinstance(raw_ids, list):
            self.json_response({"queued": False, "error": "Некорректные данные пересылки"}, HTTPStatus.BAD_REQUEST)
            return
        ids: list[str] = []
        for value in raw_ids[:20]:
            mid = normalize_message(str(value))[:160]
            if mid and mid not in ids and STORE.get_whatsapp_message(source_chat, mid):
                ids.append(mid)
        queued = []
        blocked_mentions = 0
        for mid in ids:
            message = STORE.get_whatsapp_message(source_chat, mid) or {}
            if mention_only_message(message):
                blocked_mentions += 1
                continue
            payload = json.dumps(
                {
                    "target_chat_id": target_chat,
                    "body": str(message.get("body", "") or ""),
                    "timestamp": int(message.get("message_timestamp", 0) or 0),
                    "media_name": str(message.get("media_name", "") or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            action_id = STORE.queue_whatsapp_action("forward", source_chat, mid, payload, active_employee())
            if action_id:
                queued.append(action_id)
        if not queued and blocked_mentions:
            self.json_response(
                {"queued": False, "count": 0, "blocked_count": blocked_mentions, "error": "Нельзя пересылать сообщение, состоящее только из тега пользователя"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        self.json_response(
            {"queued": bool(queued), "count": len(queued), "blocked_count": blocked_mentions},
            HTTPStatus.OK if queued else HTTPStatus.BAD_REQUEST,
        )

    def handle_whatsapp_action_claim(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        action = STORE.claim_whatsapp_action()
        self.json_response({"action": action})

    def handle_whatsapp_action_result(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            action_id = int(payload.get("action_id", 0) or 0)
        except (ValueError, TypeError):
            action_id = 0
        status = str(payload.get("status", ""))
        if action_id <= 0 or status not in {"sent", "failed"}:
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        action = STORE.complete_whatsapp_action(
            action_id, status == "sent", str(payload.get("error", ""))[:500]
        )
        if action and status == "sent":
            chat_id = str(action.get("chat_id", ""))
            message_key = str(action.get("message_key", ""))
            action_type = str(action.get("action_type", ""))
            with CHAT_LOCK:
                messages_by_chat = CHAT_STATE.get("messages", {})
                if isinstance(messages_by_chat, dict):
                    live = messages_by_chat.get(chat_id, [])
                    if isinstance(live, list):
                        for item in live:
                            if not isinstance(item, dict) or str(item.get("id", "")) != message_key:
                                continue
                            if action_type == "delete":
                                item["deleted"] = True
                                item["body"] = ""
                                item["media_path"] = ""
                                item["media_url"] = ""
                            elif action_type == "edit":
                                item["body"] = str(action.get("body", ""))
                                item["edited"] = True
                            elif action_type == "react":
                                stored = STORE.get_whatsapp_message(chat_id, message_key) or {}
                                item["reactions"] = STORE._reaction_summary(stored.get("reactions_json", "{}"))
                            break
        self.json_response({
            "updated": bool(action),
            "sent": status == "sent",
            "error": str((action or {}).get("error", "")),
        })

    def handle_chat_control(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        with CHAT_LOCK:
            requested = str(CHAT_STATE.get("requested_chat_id", ""))
            requested_group = str(CHAT_STATE.get("requested_group_id", ""))
        unresolved_group_message_ids: list[str] = []
        if requested_group.endswith("@g.us"):
            generic = {"", "участник", "участник группы", "пользователь whatsapp", "direct"}
            try:
                saved = STORE.list_saved_whatsapp_messages(requested_group, limit=80)
                for item in reversed(saved):
                    if bool(item.get("from_me")):
                        continue
                    sender = str(item.get("sender", "") or "").strip().casefold()
                    phone = str(item.get("sender_phone", "") or "").strip()
                    sender_id = str(item.get("sender_id", "") or "").strip()
                    if sender in generic or not phone or not sender_id:
                        message_id = str(item.get("id", "") or "").strip()
                        if message_id:
                            unresolved_group_message_ids.append(message_id)
                    if len(unresolved_group_message_ids) >= 30:
                        break
            except Exception:
                unresolved_group_message_ids = []
        quote_probe_ids: list[str] = []
        quote_chat_id = requested_group if requested_group.endswith("@g.us") else requested
        if quote_chat_id:
            try:
                for item in reversed(STORE.list_saved_whatsapp_messages(quote_chat_id, limit=30)):
                    if bool(item.get("deleted")):
                        continue
                    message_id = str(item.get("id", "") or "").strip()
                    if message_id and message_id not in quote_probe_ids:
                        quote_probe_ids.append(message_id)
                    if len(quote_probe_ids) >= 20:
                        break
            except Exception:
                quote_probe_ids = []
        presence_ids: list[str] = []
        if requested and not requested.endswith("@g.us"):
            presence_ids.append(requested)
        if requested_group.endswith("@g.us"):
            with CHAT_LOCK:
                participants_map = CHAT_STATE.get("group_participants", {})
                current_participants = list(participants_map.get(requested_group, [])) if isinstance(participants_map, dict) else []
            for participant in current_participants[:16]:
                if not isinstance(participant, dict) or participant.get("is_me"):
                    continue
                candidate = valid_chat_id(str(participant.get("resolved_id", "") or participant.get("mention_id", "") or ""))
                if candidate and not candidate.endswith("@g.us") and candidate not in presence_ids:
                    presence_ids.append(candidate)
        self.json_response({
            "read_chat_ids": [key for key, at in list(READ_MARKED_AT.items()) if time.monotonic() - at < 5],
            "requested_chat_id": requested,
            "requested_group_id": requested_group,
            "presence_ids": presence_ids[:16],
            "unresolved_group_message_ids": unresolved_group_message_ids,
            "quote_probe_chat_id": quote_chat_id,
            "quote_probe_message_ids": quote_probe_ids,
        })

    def read_json_body(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > max_bytes:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_authorized_json(self, max_bytes: int = 2_000_000) -> dict[str, object] | None:
        if self.headers.get("X-Webhook-Token") != WEBHOOK_TOKEN:
            self.json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > max_bytes:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, json.JSONDecodeError):
            self.json_response({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None

    def read_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return {key: values[0] for key, values in parse_qs(body).items()}

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
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
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


def main() -> None:
    cleanup_retention_once()
    threading.Thread(target=retention_worker, name="queue-retention", daemon=True).start()
    threading.Thread(target=queue_reliability.worker_loop, args=(sys.modules[__name__],), name="queue-reliability", daemon=True).start()
    threading.Thread(target=queue_performance.worker_loop, args=(sys.modules[__name__],), name="queue-performance", daemon=True).start()
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
