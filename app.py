from __future__ import annotations

import base64
import binascii
import csv
import io
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
import queue_contextual_tickets
import queue_workflow
import queue_workflow_http
import queue_reliability
import queue_reliability_http
import queue_performance
import queue_performance_http
import queue_uploads
import queue_avatars
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
import queue_chat_views
import queue_whatsapp_handlers
import queue_keden_checks
import queue_stale_menu
import queue_http_handler
import queue_ticket_service
import queue_work_context
import queue_ticket_views
import queue_admin_pages
from queue_user_locale import tr
from queue_language import parse_menu_number
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
QUEUE_1_00_4_POSTRELEASE_CLEANUP = True
QUEUE_1_00_5_REPLY_LIGHT_INSTRUCTION = True
QUEUE_1_00_6_CONTEXTUAL_TICKETS = True
QUEUE_1_00_6_1_OFFICIAL_AUTO_REPLY_AND_BIN_FLOW = True
QUEUE_1_00_6_2_GENERIC_GREETING = True
QUEUE_1_00_6_3_SCREENSHOT_MENU_TEXTS = True
QUEUE_1_00_6_4_BIN_RETURN_FIX = True
QUEUE_1_00_6_5_SCOPED_PRE_TICKET_MEDIA = True
QUEUE_1_00_6_6_VOICE_TRANSCRIPTION_DISABLED = True
QUEUE_1_00_6_7_GROUP_PREVIEW_MULTI_MEDIA = True
QUEUE_1_00_6_8_SEQUENTIAL_SCREENSHOTS = True
QUEUE_1_00_6_9_ATTACHMENT_PREVIEWS = True
QUEUE_1_00_6_10_VISIBLE_ATTACHMENT_TRAY = True
QUEUE_1_00_6_11_KZ_USER_WHATSAPP = True
QUEUE_1_00_6_12_LANGUAGE_GATE = True
QUEUE_1_00_6_13_UNCERTAIN_LOCAL_MEDIA_RECONCILE = True
QUEUE_1_00_6_14_GLOBAL_AUTO_REPLY_TOGGLE = True
QUEUE_1_00_6_15_AUTHORIZATION = True
QUEUE_1_00_6_16_MULTIUSER_BACKEND = True
QUEUE_1_00_6_17_REGISTER_RECOVERY = True
QUEUE_1_00_6_18_ACCESS_OPERATIONS = True
QUEUE_1_00_6_19_AUTO_SHIFT_CLEAN_AUTH = True
QUEUE_1_00_6_20_CHAT_CLEANUP = True
QUEUE_1_00_6_26_FINAL_AUTH_OPERATIONS = True
QUEUE_1_00_6_27_MODULARIZATION_PHASE1 = True
QUEUE_1_00_6_28_MODULARIZATION_PHASE2 = True
QUEUE_1_00_6_29_MODULARIZATION_PHASE3 = True
QUEUE_1_00_6_30_MODULARIZATION_PHASE4 = True
QUEUE_1_00_6_31_MODULARIZATION_PHASE5 = True
QUEUE_1_00_6_32_REALTIME_FASTPATH = True
QUEUE_1_00_6_33_USER_FLOW_FIXES = True
QUEUE_1_00_6_34_MEDIA_CONTEXT_FIX = True
QUEUE_1_00_6_139_KEDEN_TD_CHECK = True
QUEUE_1_00_6_140_STALE_MENU_RECOVERY = True
ROOT = Path(__file__).resolve().parent

# 1.00.6.27: first modularization phase. Keep the historic names in app.py as
# aliases so existing feature modules can continue receiving app as their context.
ALMATY_TIMEZONE = queue_core.ALMATY_TIMEZONE
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
STORE = TicketStore(DATABASE_PATH)
AUTH = queue_auth.AuthStore(DATABASE_PATH)
queue_operations.initialize(STORE)
queue_productivity.initialize(STORE)
queue_contextual_tickets.initialize(STORE)
queue_keden_checks.initialize(STORE)
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
_ticket_resolution_minutes = REPORTING.ticket_resolution_minutes
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


def suppress_inbound_auto_reply(payload: dict[str, object]) -> dict[str, object]:
    """Strip bot output while keeping inbound processing and ticket state."""
    result = dict(payload)
    if result.get("reply"):
        result["auto_reply_suppressed"] = True
    result.pop("reply", None)
    result.pop("menu_options", None)
    result["silent"] = True
    result["auto_reply_disabled"] = True
    return result
MAX_MEDIA_BYTES = max(1, min(256, int(os.getenv("QUEUE_MAX_MEDIA_MB", "12")))) * 1024 * 1024
UPLOADS = queue_uploads.Uploads(OUTBOUND_MEDIA_DIR / "uploads", STORE)
class DisabledVoiceJobs:
    """Compatibility object: voice transcription is intentionally disabled."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.states: dict[tuple[str, str], dict[str, str]] = {}

    def submit(self, chat_id: str, message_id: str) -> dict[str, str]:
        key = (str(chat_id), str(message_id))
        state = {
            "status": "error",
            "reason": "Расшифровка голосовых отключена администратором",
        }
        with self.lock:
            self.states[key] = dict(state)
        return state


VOICE_JOBS = DisabledVoiceJobs()
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
INSTRUCTION_SOURCE_URL = os.getenv("QUEUE_INSTRUCTION_SOURCE_URL", "").strip()
INSTRUCTION_DATA_PATH = ROOT / "instruction_data.json"
DEFAULT_EMPLOYEES = [f"Сотрудник {number}" for number in range(1, 6)]
ADMIN_USER = os.getenv("QUEUE_ADMIN_USER", "queueadmin").strip().casefold()
ADMIN_FORM_TOKEN = secrets.token_urlsafe(32)
ADMIN_SESSION_TOKEN = secrets.token_urlsafe(48)
ADMIN_SESSION_COOKIE = "queue_admin_session"
ADMIN_SESSION_MAX_AGE = 12 * 60 * 60
AUTH_SESSION_COOKIE = "queue_session"
AUTH_SESSION_MAX_AGE = queue_auth.SESSION_HOURS * 60 * 60
MAX_EMPLOYEES = 50
MAX_MENU_OPTIONS = 30
MENU_ACTION_TICKET = "ticket"
MENU_ACTION_INSTRUCTION = "instruction"
MENU_ACTIONS = {MENU_ACTION_TICKET, MENU_ACTION_INSTRUCTION}
SUPPORT_MODE_CONTEXT = "__support_mode__"
ERROR_REPORT_CONTEXT = "__error_report__"
ACTIVE_TICKETS_CONTEXT = "__active_tickets__"
ACTIVE_TICKET_FOLLOWUP_CONTEXT = "__active_ticket_followup__"
BIN_MENU_CONTEXT = "__bin_menu__"
BIN_COMPANY_NAME_CATEGORY = "bin_company_name"
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

# 1.00.6.11: ordinary WhatsApp users receive Kazakh texts. The employee/admin
# interface, stored ticket data, internal statuses and menu editor remain Russian.
USER_MENU_LABELS_KZ = {
    "seal": "Навигациялық пломба / НП (шешу, тағу, қосу, НП мәселесі)",
    "transport": "Тасымалдау (КЕДЕН-де көрінбейді, аяқталмаған тасымалдаулар, КҚ/жүргізуші мәселелері, ТД жетіспейді)",
    "bin": "БИН-ді түзету",
    "keden": "КЕДЕН (тасымалдау көрінбейді)",
    "package": "Пакеттер",
    "database": "TRANSIT дерекқоры (рөл беру, пост, бұғаттан шығару)",
    "mobile": "TRANSIT мобильді қосымшасы",
    "general": "Басқа мәселе",
    "support": "Қолдау қызметіне сұрақ қою",
    "bin_company_name": "Компания атауын өзгерту",
}
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
USER_CATEGORY_PROMPTS_KZ = {
    "seal": (
        "Тақырып таңдалды: Навигациялық пломба.\n"
        "НП немесе пломба нөмірін көрсетіп, не істеу керектігін немесе не жұмыс істемейтінін жазыңыз. "
        "Мысал: НП 398423874 ашылмайды.\n"
        "Егер мәселені анықтауға көмектессе, фото немесе скриншотты осы хабарламаға тіркеуге болады. Бұл міндетті емес.\n"
        "0 — негізгі мәзірге оралу"
    ),
    "transport": (
        "Тақырып таңдалды: Тасымалдау.\n"
        "Тасымалдау нөмірін, КҚ, пломба немесе ТД нөмірін көрсетіп, мәселені толық сипаттаңыз.\n"
        "Егер мәселені анықтауға көмектессе, фото немесе скриншотты осы хабарламаға тіркеуге болады. Бұл міндетті емес.\n"
        "0 — негізгі мәзірге оралу"
    ),
    "keden": (
        "Егер КЕДЕН біздің тасымалдауды (НП) көрмесе, КЕДЕН техникалық қолдау қызметіне жазыңыз. "
        "Олар осы ТД бойынша пакеттерді қайта жібереді."
    ),
    "package": (
        "Тақырып таңдалды: Пакеттер.\n"
        "Пакет, тасымалдау немесе ТД нөмірін көрсетіп, мәселені толық сипаттаңыз.\n"
        "Егер мәселені анықтауға көмектессе, фото немесе скриншотты осы хабарламаға тіркеуге болады. Бұл міндетті емес.\n"
        "0 — негізгі мәзірге оралу"
    ),
    "database": (
        "Тақырып таңдалды: Дерекқор.\n"
        "Қандай мәселе туындағанын жазыңыз және өзіңіздің немесе басқа пайдаланушының электрондық поштасын көрсетіңіз.\n"
        "Егер мәселені анықтауға көмектессе, фото немесе скриншотты осы хабарламаға тіркеуге болады. Бұл міндетті емес.\n"
        "0 — негізгі мәзірге оралу"
    ),
    "mobile": (
        "Тақырып таңдалды: TRANSIT мобильді қосымшасы.\n"
        "Мобильді қосымшаның қай бөлімі жұмыс істемейтінін, нені басатыныңызды және қандай қате шығатынын жазыңыз.\n"
        "Егер мәселені анықтауға көмектессе, фото немесе скриншотты осы хабарламаға тіркеуге болады. Бұл міндетті емес.\n"
        "0 — негізгі мәзірге оралу"
    ),
    "general": (
        "Тақырып таңдалды: Басқа өтінім.\n"
        "Егер жоғарыдағы санаттардың ешқайсысы мәселеңізге сәйкес келмесе, мәселені сипаттап жазыңыз.\n"
        "Егер мәселені анықтауға көмектессе, фото немесе скриншотты осы хабарламаға тіркеуге болады. Бұл міндетті емес.\n"
        "0 — негізгі мәзірге оралу"
    ),
    "support": (
        "Тақырып таңдалды: Қолдау қызметіне сұрақ.\n"
        "Сұрағыңызды еркін түрде жазыңыз. Арнайы үлгі қажет емес. Егер өтініш жоғарыдағы санаттардың біріне қатысты болса, ол қабылданбауы мүмкін.\n"
        "Егер мәселені анықтауға көмектессе, фото немесе скриншотты осы хабарламаға тіркеуге болады. Бұл міндетті емес.\n"
        "0 — негізгі мәзірге оралу"
    ),
    "bin_company_name": (
        "Сіз «Компания атауын өзгерту» операциясын таңдадыңыз.\n\n"
        "Өтінімді тіркеу үшін мыналарды ұсыну қажет:\n"
        "• компанияның БИН-і;\n"
        "• компанияның қазіргі атауы;\n"
        "• компанияның жаңа атауы.\n\n"
        "Мәліметтерді бір хабарламамен немесе кезекпен жіберуге болады. Жүйе алынған ақпаратты сақтап, тек жетіспейтін мәліметтерді сұрайды.\n"
        "Қажет болған жағдайда растаушы файлды немесе скриншотты тіркеңіз.\n\n"
        "Негізгі мәзірге оралу үшін 0 жіберіңіз."
    ),
}


def user_menu_label(category: str, fallback: str = "") -> str:
    return queue_user_locale.user_menu_label(category, fallback)


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
        if key in CATEGORY_ARCHIVE or key == BIN_COMPANY_NAME_CATEGORY:
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
        {"number": str(index), "label": user_menu_label(str(item["key"]), str(item["label"]))}
        for index, item in numbered_enabled_menu_options()
    ]
    rows.append({
        "number": str(error_report_menu_number()),
        "label": tr("Репорт об ошибке", "Қате туралы хабарлама"),
    })
    rows.append({
        "number": str(active_tickets_menu_number()),
        "label": tr("Мои активные заявки", "Менің белсенді өтінімдерім"),
    })
    return rows


def is_error_report_command(text: str) -> bool:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    return normalized in {
        "репорт об ошибке", "репорт ошибки", "сообщить об ошибке",
        "сообщить ошибку", "ошибка в системе", "баг", "report bug",
        "қате туралы хабарлама", "қате туралы есеп", "қате хабарламасы",
    }


def user_greeting(sender: str) -> str:
    return tr("Здравствуйте, пользователь.", "Сәлеметсіз бе, пайдаланушы.")


def with_user_greeting(sender: str, text: str) -> str:
    clean = str(text or "").strip()
    folded = clean.casefold()
    if folded.startswith("здравствуйте") or folded.startswith("сәлеметсіз"):
        return clean
    return f"{user_greeting(sender)}\n\n{clean}"


def bin_submenu_text() -> str:
    return tr(
        "Вы выбрали раздел «Корректировка БИН».\n\n"
        "Уточните необходимое действие:\n"
        "1. Изменить название компании\n"
        "2. Получить инструкцию по другой операции с БИН\n\n"
        "Заявка создаётся только для операции «Изменить название компании». "
        "По остальным операциям система предоставит инструкцию без регистрации заявки.\n\n"
        "Отправьте номер 1 или 2. Для возврата в главное меню отправьте 0.",
        "Сіз «БИН-ді түзету» бөлімін таңдадыңыз.\n\n"
        "Қажетті әрекетті таңдаңыз:\n"
        "1. Компания атауын өзгерту\n"
        "2. БИН бойынша басқа операцияға нұсқаулық алу\n\n"
        "Өтінім тек «Компания атауын өзгерту» операциясы үшін жасалады. "
        "Қалған операциялар бойынша жүйе өтінімді тіркемей, нұсқаулық береді.\n\n"
        "1 немесе 2 нөмірін жіберіңіз. Негізгі мәзірге оралу үшін 0 жіберіңіз.",
    )


def bin_instruction_text() -> str:
    return tr(
        "Инструкция по операциям с БИН\n\n"
        "Для выполнения корректировки БИН в Менеджере необходимо открыть соответствующую перевозку, "
        "нажать левой кнопкой мыши на наименование компании, выбрать необходимый элемент и заполнить "
        "все обязательные поля в соответствии с требованиями системы.\n"
        "Администраторы этим больше не занимаются. Если у вас возникнут вопросы, выберите категорию "
        "«Вопрос в поддержку», чтобы уточнить, как это сделать.\n\n"
        "Если требуется изменить название компании, отправьте 1. Для возврата в главное меню отправьте 0.",
        "БИН бойынша операцияларға арналған нұсқаулық\n\n"
        "Менеджерде БИН-ді түзету үшін тиісті тасымалдауды ашып, компания атауын тінтуірдің сол жақ батырмасымен басып, "
        "қажетті элементті таңдап, жүйе талаптарына сәйкес барлық міндетті өрістерді толтыру қажет.\n"
        "Әкімшілер енді бұл жұмыспен айналыспайды. Сұрақтарыңыз болса, оны қалай орындау керектігін нақтылау үшін "
        "техникалық қолдау қызметіне сұрау санатын таңдаңыз.\n\n"
        "Егер компания атауын өзгерту қажет болса, 1 жіберіңіз. Негізгі мәзірге оралу үшін 0 жіберіңіз.",
    )


def configured_category_required_fields(category: str) -> list[str]:
    # Required-field inference remains based on the Russian admin-configured text.
    # Only ordinary-user output is localized to Kazakh.
    prompt = category_prompt(category) if category == BIN_COMPANY_NAME_CATEGORY else configured_category_prompt_internal(category)
    return queue_contextual_tickets.infer_required_fields(category, prompt)


def error_report_prompt() -> str:
    return tr(
        "Вы выбрали категорию «Репорт об ошибке».\n\n"
        "Опишите, пожалуйста, последовательность действий и возникшую ошибку. При необходимости приложите скриншот, фото, видео или файл.\n\n"
        "Сообщение будет сохранено отдельно от обычных заявок и доступно сотрудникам в разделе «Ошибки».\n"
        "Для возврата в главное меню отправьте 0.",
        "Сіз «Қате туралы хабарлама» санатын таңдадыңыз.\n\n"
        "Орындаған әрекеттеріңізді және пайда болған қатені сипаттаңыз. Қажет болса, скриншот, фото, видео немесе файл тіркеңіз.\n\n"
        "Хабарлама қарапайым өтінімдерден бөлек сақталады және қызметкерлерге «Қателер» бөлімінде қолжетімді болады.\n"
        "Негізгі мәзірге оралу үшін 0 жіберіңіз.",
    )


def is_active_tickets_command(text: str) -> bool:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    return normalized in {
        "мои заявки", "мои активные заявки", "активные заявки",
        "показать заявки", "покажи заявки", "статус заявок",
        "менің өтінімдерім", "менің белсенді өтінімдерім", "белсенді өтінімдер",
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
    "При необходимости к обращению можно приложить фото, скриншот или файл. "
    "Вложение не является обязательным, если категория не требует его отдельно."
)


def configured_category_prompt_internal(category: str) -> str:
    # Raw text comes from the Russian admin menu editor and is also used for
    # required-field inference. Keep it separate from the user-facing locale.
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


def configured_category_prompt(category: str) -> str:
    if queue_user_locale.is_kz() and category in queue_user_locale.USER_CATEGORY_PROMPTS_KZ:
        return queue_user_locale.USER_CATEGORY_PROMPTS_KZ[category]
    return configured_category_prompt_internal(category)


def configured_category_action(category: str) -> str:
    option = next((item for item in MENU_OPTIONS if item["key"] == category), None)
    action = str(option.get("action", MENU_ACTION_TICKET)) if option else MENU_ACTION_TICKET
    return action if action in MENU_ACTIONS else MENU_ACTION_TICKET


def start_menu_prompt() -> str:
    # Outside an open menu a bare number must not silently select a category.
    # The single command for returning to/opening the main menu is 0.
    return tr(
        "Вы обратились в службу технической поддержки. Чтобы открыть главное меню заявок, отправьте 0.",
        "Сіз техникалық қолдау қызметіне жүгіндіңіз. Өтінімдердің негізгі мәзірін ашу үшін 0 жіберіңіз.",
    )


def start_menu_reminder() -> str:
    return tr(
        "Для продолжения необходимо выбрать категорию обращения.",
        "Жалғастыру үшін өтінім санатын таңдау қажет.",
    )


def main_menu_text(error: bool = False) -> str:
    category_rows = [
        f"{index}. {user_menu_label(str(item['key']), str(item['label']))}"
        for index, item in numbered_enabled_menu_options()
    ]
    category_rows.append(f"{error_report_menu_number()}. {tr('Репорт об ошибке', 'Қате туралы хабарлама')}")
    category_rows.append(f"{active_tickets_menu_number()}. {tr('Мои активные заявки', 'Менің белсенді өтінімдерім')}")
    rows = "\n".join(category_rows)
    if error:
        intro = tr(
            "Не удалось определить выбранную категорию.\nПожалуйста, выберите один из доступных пунктов:\n\n",
            "Таңдалған санатты анықтау мүмкін болмады.\nҚолжетімді тармақтардың бірін таңдаңыз:\n\n",
        )
    else:
        intro = tr(
            "Главное меню обращений\n\nВыберите категорию обращения:\n\n",
            "Өтінімдердің негізгі мәзірі\n\nӨтінім санатын таңдаңыз:\n\n",
        )
    return (
        f"{intro}{rows}\n\n"
        + tr(
            "В ответ отправьте только номер нужного пункта. После выбора система запросит сведения, необходимые именно для этой категории.\n\n"
            "Для повторного открытия меню отправьте 0 или слово «меню». Чтобы сменить язык, отправьте слово «язык».",
            "Жауап ретінде тек қажетті тармақтың нөмірін жіберіңіз. Таңдағаннан кейін жүйе осы санатқа қажетті мәліметтерді сұрайды.\n\n"
            "Мәзірді қайта ашу үшін 0 немесе «мәзір» сөзін жіберіңіз. Тілді ауыстыру үшін «тіл» сөзін жіберіңіз.",
        )
    )


def menu_selection_reminder() -> str:
    return tr(
        "Для продолжения выберите номер категории из меню. После выбора система запросит сведения, необходимые для регистрации обращения.\n\n"
        "Чтобы повторно открыть главное меню, отправьте 0 или слово «меню».",
        "Жалғастыру үшін мәзірден санат нөмірін таңдаңыз. Таңдағаннан кейін жүйе өтінімді тіркеуге қажетті мәліметтерді сұрайды.\n\n"
        "Негізгі мәзірді қайта ашу үшін 0 немесе «мәзір» сөзін жіберіңіз.",
    )


# ---------------------------------------------------------------------------
# 1.00.6.98: approved ordinary-user WhatsApp flow ("Текст системы_v3").
# Staff/admin UI and internal ticket statuses remain Russian. Public menu
# numbering is fixed to 1-8 so an old request_menu_json cannot change it.
# ---------------------------------------------------------------------------
QUEUE_1_00_6_98_USER_FLOW_V3 = True
QUEUE_1_00_6_99_USER_PROFILE_V3 = True

# Keep staff-facing category names aligned with the new public flow while preserving
# the existing internal keys and historical ticket records.
CATEGORIES["seal"] = "Проблема с навигационной пломбой (НП)"
CATEGORIES["transport"] = "Проблема с оформлением перевозки"
CATEGORIES[BIN_COMPANY_NAME_CATEGORY] = "Корректировка БИН"
CATEGORIES["keden"] = "Проблемы с КЕДЕН"
CATEGORIES["database"] = "Доступ к ИС TRANSIT"
CATEGORIES["mobile"] = "Мобильное приложение TRANSIT"
CATEGORIES["general"] = "Другая проблема / Вопрос / Ошибка"
CATEGORIES["check_td"] = "Проверить ТД / пакеты Кеден"

V3_MENU_CATEGORIES = {
    "1": "seal",
    "2": "transport",
    "3": BIN_COMPANY_NAME_CATEGORY,
    "4": "keden",
    "5": "database",
    "6": "mobile",
    "7": "general",
    "9": "check_td",
}
V3_MENU_LABELS_RU = {
    "1": "Проблема с навигационной пломбой (НП)",
    "2": "Проблема с оформлением перевозки",
    "3": "Корректировка БИН",
    "4": "Проблемы с КЕДЕН (не видят перевозку или не отображается номер НП в ТД)",
    "5": "Доступ к ИС TRANSIT (выдача ролей, снятие блокировки)",
    "6": "Мобильное приложение TRANSIT",
    "7": "Другая проблема / Задать вопрос / Сообщить об ошибке",
    "8": "Мои активные заявки",
    "9": "Проверить ТД / пакеты Кеден",
}
V3_MENU_LABELS_KZ = {
    "1": "Навигациялық пломба (НП) мәселесі",
    "2": "Тасымалдауды ресімдеу мәселесі",
    "3": "БИН-ді түзету",
    "4": "КЕДЕН мәселелері (тасымалдау көрінбейді немесе ТД-да НП нөмірі жоқ)",
    "5": "TRANSIT жүйесіне кіру (рөлдер беру, бұғаттан шығару)",
    "6": "TRANSIT мобильді қосымшасы",
    "7": "Басқа мәселе / Сұрақ қою / Қате туралы хабарлау",
    "8": "Менің белсенді өтінімдерім",
    "9": "ТД / КЕДЕН пакеттерін тексеру",
}
V3_REQUIRED_FIELDS = {
    "seal": ["reference", "problem"],
    "transport": ["reference", "problem"],
    BIN_COMPANY_NAME_CATEGORY: ["bin_number", "company_old", "company_new"],
    "keden": ["reference"],
    "database": ["email", "problem"],
    "mobile": ["problem"],
    "general": ["problem"],
    "check_td": ["reference"],
}
V3_FIELD_LABELS_RU = {
    "reference": "номер / идентификатор",
    "problem": "описание проблемы",
    "bin_number": "БИН",
    "company_old": "текущее название компании",
    "company_new": "новое название компании",
    "email": "email/логин пользователя",
}
V3_FIELD_LABELS_KZ = {
    "reference": "нөмір / идентификатор",
    "problem": "мәселенің сипаттамасы",
    "bin_number": "БИН",
    "company_old": "компанияның қазіргі атауы",
    "company_new": "компанияның жаңа атауы",
    "email": "пайдаланушының email/логині",
}
V3_CATEGORY_LABELS_KZ = {
    "seal": V3_MENU_LABELS_KZ["1"],
    "transport": V3_MENU_LABELS_KZ["2"],
    BIN_COMPANY_NAME_CATEGORY: V3_MENU_LABELS_KZ["3"],
    "keden": V3_MENU_LABELS_KZ["4"],
    "database": V3_MENU_LABELS_KZ["5"],
    "mobile": V3_MENU_LABELS_KZ["6"],
    "general": V3_MENU_LABELS_KZ["7"],
    "check_td": V3_MENU_LABELS_KZ["9"],
}


def user_menu_label(category: str, fallback: str = "") -> str:
    if queue_user_locale.is_kz():
        return V3_CATEGORY_LABELS_KZ.get(str(category), queue_user_locale.user_menu_label(category, fallback))
    return fallback or str(category)


def language_selection_text_v3() -> str:
    return (
        "Здравствуйте! / Сәлеметсіз бе!\n"
        "Выберите язык обслуживания / Қызмет көрсету тілін таңдаңыз (1-2):\n"
        "1️⃣ — Русский\n"
        "2️⃣ — Қазақша\n\n"
        "После выбора языка укажите свой пост и должность / "
        "Тілді таңдағаннан кейін бекетіңіз бен лауазымыңызды көрсетіңіз."
    )


def active_tickets_menu_number() -> int:
    return 8


def error_report_menu_number() -> int:
    # Separate bug-report mode stays available to staff, but is not a public
    # numbered item in the v3 WhatsApp flow.
    return 999


def menu_payload() -> list[dict[str, str]]:
    labels = V3_MENU_LABELS_KZ if queue_user_locale.is_kz() else V3_MENU_LABELS_RU
    return [{"number": str(number), "label": labels[str(number)]} for number in range(1, 10)]


def configured_menu_category(choice: str) -> str:
    return V3_MENU_CATEGORIES.get(parse_menu_number(choice), "")


def main_menu_text(error: bool = False) -> str:
    if queue_user_locale.is_kz():
        lead = (
            "ИС Транзит техникалық қолдау қызметіне қош келдіңіз. 🛠\n\n"
            "Өтінімді жалғастыру үшін өтінім санатын таңдаңыз (санын жіберіңіз):\n"
        )
        labels = V3_MENU_LABELS_KZ
        tail = "\n*(00 — тілді өзгерту)*"
        if error:
            lead = "Жалғастыру үшін мәзірден санатты таңдаңыз.\n\n" + lead
    else:
        lead = (
            "Добро пожаловать в техподдержку ИС Транзит. 🛠\n\n"
            "Для продолжения выберите категорию обращения (отправьте её номер):\n"
        )
        labels = V3_MENU_LABELS_RU
        tail = "\n*(00 — сменить язык)*"
        if error:
            lead = "Для продолжения выберите категорию из меню.\n\n" + lead
    rows = "\n".join(f"{number}. {labels[str(number)]}" for number in range(1, 10))
    return f"{lead}{rows}{tail}"


def start_menu_prompt() -> str:
    return main_menu_text()


def start_menu_reminder() -> str:
    return menu_selection_reminder()


def menu_selection_reminder() -> str:
    return tr(
        "Для новой заявки сначала выберите категорию: отправьте цифру 1–7 или 9. Пункт 8 — ваши активные заявки.\n*(00 — сменить язык)*",
        "Жаңа өтінім үшін алдымен санатты таңдаңыз: 1–7 немесе 9 санын жіберіңіз. 8 — белсенді өтінімдеріңіз.\n*(00 — тілді өзгерту)*",
    )


def configured_category_prompt(category: str) -> str:
    prompts_ru = {
        "seal": "📍 Напишите номер пломбы (НП) и опишите проблему (не открывается, не навешивается, повреждена).\n📸 При необходимости прикрепите фото.\n*(0 — главное меню)*",
        "transport": "🚚 Укажите номер перевозки (ТС или ТД) и опишите проблему (не завершается перевозка, ошибка в данных). Можно приложить скриншот.\n*(0 — главное меню)*",
        BIN_COMPANY_NAME_CATEGORY: "🏢 Отправьте одним сообщением:\n1. БИН\n2. Текущее название компании\n3. Новое название компании\n*(0 — главное меню)*",
        "keden": "⚠️ Укажите номер перевозки или номер ТД. Мы переотправим пакеты данных в систему КЕДЕН.\n*(0 — главное меню)*",
        "database": "🔑 Напишите email/логин пользователя и требуемое действие (выдать роль, прикрепить пост, снять блокировку).\n*(0 — главное меню)*",
        "mobile": "📱 Напишите, какой именно раздел приложения не работает, и приложите скриншот экрана с ошибкой.\n*(0 — главное меню)*",
        "general": "Опишите вашу проблему, задайте вопрос или опишите ошибку свободным текстом. Приложите файлы или скриншоты, если они помогут разобраться.\n*(0 — главное меню)*",
        "check_td": "🔎 Отправьте номер ТД или перевозки. Система автоматически проверит Пакеты Кеден и пришлёт результат в этот WhatsApp.\n*(0 — главное меню)*",
    }
    prompts_kz = {
        "seal": "📍 Навигациялық пломбаның (НП) нөмірін жазып, мәселені сипаттаңыз (ашылмайды, тағылмайды, зақымдалған).\n📸 Қажет болса, фото тіркеңіз.\n*(0 — негізгі мәзір)*",
        "transport": "🚚 Тасымалдау нөмірін (КҚ немесе ТД) көрсетіп, мәселені сипаттаңыз (тасымалдау аяқталмайды, деректер қате). Скриншот тіркеуге болады.\n*(0 — негізгі мәзір)*",
        BIN_COMPANY_NAME_CATEGORY: "🏢 Бір хабарламамен жіберіңіз:\n1. БИН\n2. Компанияның қазіргі атауы\n3. Компанияның жаңа атауы\n*(0 — негізгі мәзір)*",
        "keden": "⚠️ Тасымалдау немесе ТД нөмірін көрсетіңіз. Біз КЕДЕН жүйесіне деректер пакетін қайта жібереміз.\n*(0 — негізгі мәзір)*",
        "database": "🔑 Пайдаланушының email/логинін және қажетті әрекетті жазыңыз (рөл беру, бекетті бекіту, бұғаттан шығару).\n*(0 — негізгі мәзір)*",
        "mobile": "📱 Қосымшаның нақты қай бөлімі жұмыс істемейтінін жазыңыз және қате шыққан экранның скриншотын тіркеңіз.\n*(0 — негізгі мәзір)*",
        "general": "Мәселеңізді сипаттаңыз, сұрағыңызды қойыңыз немесе қате туралы еркін мәтінмен хабарлаңыз. Түсінуге көмектесетін болса, скриншот немесе файлдар тіркеңіз.\n*(0 — негізгі мәзір)*",
        "check_td": "🔎 ТД немесе тасымалдау нөмірін жіберіңіз. Жүйе КЕДЕН пакеттерін автоматты түрде тексеріп, нәтижені осы WhatsApp-қа жібереді.\n*(0 — негізгі мәзір)*",
    }
    prompts = prompts_kz if queue_user_locale.is_kz() else prompts_ru
    return prompts.get(category, configured_category_prompt_internal(category))


def configured_category_required_fields(category: str) -> list[str]:
    if category in V3_REQUIRED_FIELDS:
        return list(V3_REQUIRED_FIELDS[category])
    return queue_contextual_tickets.infer_required_fields(category, configured_category_prompt_internal(category))


def v3_missing_fields_reply(missing_fields) -> str:
    fields = [str(item or "").strip() for item in (missing_fields or []) if str(item or "").strip()]
    labels_map = V3_FIELD_LABELS_KZ if queue_user_locale.is_kz() else V3_FIELD_LABELS_RU
    labels = [labels_map.get(field, field) for field in fields]
    joined = ", ".join(labels) if labels else tr("недостающие данные", "жетіспейтін мәліметтер")
    return tr(
        f"Пожалуйста, уточните: {joined} (остальные данные уже зафиксированы).",
        f"Нақтылап жіберіңізші: {joined} (қалған мәліметтер сақталды).",
    )


def v3_duplicate_reply(ticket_id: int) -> str:
    return tr(
        f"⚠️ Заявка по данному вопросу уже в работе (№{ticket_id}). Мы добавили ваше сообщение к ней, повторную заявку создавать не требуется.",
        f"⚠️ Бұл мәселе бойынша өтінім қазір жұмыс барысында (№{ticket_id}). Хабарламаңыз оған қосылды, жаңа өтінім жасаудың қажеті жоқ.",
    )


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
            try:
                with temporary.open("wb") as stream:
                    while remaining > 0:
                        chunk = handler.rfile.read(min(1024 * 1024, remaining))
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
        queued_body = ""
    current_body = _observed_outbound_body(observed_body, observed_type)
    if queued_body != current_body:
        return False

    try:
        queued_reply = normalize_message(str(row["reply_to_key"] or ""))[:160]
    except Exception:
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
            preserved = queue_uploads.preserve_sent_attachment(sys.modules[__name__], row_id)
        except Exception:
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
                pass

        if item.get("from_me") and not item.get("deleted") and not item.get("media_path"):
            media = {}
            if sent_row and _outbound_media_matches_observation(
                sent_row,
                str(item.get("body", "")),
                str(item.get("quoted_message_key", "")),
                str(item.get("message_type", item.get("type", ""))),
            ):
                media = queue_uploads.preserve_sent_attachment(sys.modules[__name__], sent_row["id"])
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
# ALMATY_TIMEZONE is provided by queue_core.
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
queue_chat_state.bind(globals(), sys.modules[__name__])
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


TICKET_SERVICE = queue_ticket_service.TicketService(queue_ticket_service.TicketDependencies(
    store=STORE, statuses=STATUSES, categories=CATEGORIES, menu_context=MENU_CONTEXT,
    close_reason_labels_kz=USER_CLOSE_REASON_LABELS_KZ,
    contact_language=lambda chat, phone: get_contact_language(chat, phone),
    auto_reply_enabled=lambda: global_auto_reply_enabled(),
    main_menu=lambda: main_menu_text(),
    menu_label=lambda category, fallback: user_menu_label(category, fallback),
    status_label=lambda status, fallback: user_status_label(status, fallback),
))
ticket_accepted_reply = TICKET_SERVICE.ticket_accepted_reply
active_tickets_reply = TICKET_SERVICE.active_tickets_reply
active_ticket_selected_reply = TICKET_SERVICE.active_ticket_selected_reply
support_question_accepted_reply = TICKET_SERVICE.support_question_accepted_reply
ticket_status_reply_text = TICKET_SERVICE.status_reply_text
update_ticket_status = TICKET_SERVICE.update_status

# Ticket views retain their existing interface.
queue_ticket_views.bind(globals())
layout = queue_ticket_views.layout
instruction_text_html = queue_ticket_views.instruction_text_html
load_instruction_data = queue_ticket_views.load_instruction_data
render_instruction = queue_ticket_views.render_instruction
render_dashboard = queue_ticket_views.render_dashboard
render_ticket = queue_ticket_views.render_ticket
render_my_page = queue_ticket_views.render_my_page


INSTRUCTION_LINK_RE = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>]+)")


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
    return queue_auth_views.render_account(query, user=queue_auth.current_user(), escape=e)


def render_admin_users(query: dict[str, list[str]]) -> str:
    return queue_admin_views.render_admin_users(
        query, auth=AUTH, employees=EMPLOYEES, csrf_token=ADMIN_FORM_TOKEN,
        escape=e, human_time=human_time, admin_layout_func=admin_layout,
    )


def admin_tabs(active: str) -> str:
    return queue_admin_views.admin_tabs(active, escape=e)


def admin_layout(title: str, content: str, active: str) -> str:
    return queue_admin_views.admin_layout(title, content, active, layout_func=layout, escape=e)


# 1.00.6.30: phase 4 large modularization (queue_admin_pages).
queue_admin_pages.bind(globals())
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


# 1.00.6.29: chat page HTML moved out of app.py.
queue_chat_views.bind(globals())
render_groups = queue_chat_views.render_groups
render_whatsapp = queue_chat_views.render_whatsapp


# ---------------------------------------------------------------------------
# 1.00.6.12: language selection before the ordinary-user WhatsApp flow.
# The selected language is persisted per WhatsApp user in app_settings.
# Admin/employee UI and stored ticket data stay Russian.
# ---------------------------------------------------------------------------
LANGUAGE_SETTING_PREFIX = "whatsapp_user_language:"


def _language_identities(chat_id: str = "", phone: str = "") -> list[str]:
    """Return every stable identity we know for one WhatsApp user.

    WhatsApp can alternate between phone-backed @c.us and LID identifiers.
    Persisting the language under both aliases prevents a previously selected
    language from appearing to flip when the connector reports another alias.
    """
    identities: list[str] = []
    phone_digits = re.sub(r"\D", "", normalize_phone(phone))
    if len(phone_digits) >= 10:
        identities.append(f"phone:{phone_digits}")
    clean_chat = valid_chat_id(chat_id)
    if clean_chat:
        try:
            clean_chat = STORE.canonical_whatsapp_chat_id(clean_chat) or clean_chat
        except Exception:
            pass
        identities.append(f"chat:{clean_chat}")
    result: list[str] = []
    for identity in identities:
        if identity and identity not in result:
            result.append(identity)
    return result


def _language_identity(chat_id: str = "", phone: str = "") -> str:
    identities = _language_identities(chat_id, phone)
    return identities[0] if identities else ""


def _language_setting_key(identity: str) -> str:
    if not identity:
        return ""
    digest = hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()
    return f"{LANGUAGE_SETTING_PREFIX}{digest}"


def get_user_language(identity: str) -> str:
    key = _language_setting_key(identity)
    if not key:
        return ""
    value = STORE.get_setting(key, "").strip().casefold()
    return value if value in {queue_user_locale.LANG_RU, queue_user_locale.LANG_KZ} else ""


def set_user_language(identity: str, language: str) -> str:
    key = _language_setting_key(identity)
    lang = queue_user_locale.normalize_language(language)
    if key:
        STORE.set_setting(key, lang)
    return lang


def clear_user_language(identity: str) -> None:
    key = _language_setting_key(identity)
    if key:
        STORE.set_setting(key, "")


def get_contact_language(chat_id: str = "", phone: str = "") -> str:
    """Resolve language across phone/chat aliases and heal stale conflicts."""
    identities = _language_identities(chat_id, phone)
    values = [(identity, get_user_language(identity)) for identity in identities]
    selected = next((value for _identity, value in values if value), "")
    if selected:
        for identity, value in values:
            if value != selected:
                set_user_language(identity, selected)
    return selected


def set_contact_language(chat_id: str, phone: str, language: str) -> str:
    lang = queue_user_locale.normalize_language(language)
    identities = _language_identities(chat_id, phone)
    if not identities:
        return lang
    for identity in identities:
        set_user_language(identity, lang)
    return lang


def clear_contact_language(chat_id: str = "", phone: str = "") -> None:
    for identity in _language_identities(chat_id, phone):
        clear_user_language(identity)


USER_PROFILE_SETTING_PREFIX = "whatsapp_user_profile:"
USER_PROFILE_CONTEXT = "__user_profile__"


def _profile_setting_key(identity: str) -> str:
    if not identity:
        return ""
    digest = hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()
    return f"{USER_PROFILE_SETTING_PREFIX}{digest}"


def _clean_user_profile_value(value: object) -> str:
    clean = normalize_message(str(value or "")).strip(" \t\r\n,;.")[:120]
    return clean if len(clean) >= 2 else ""


def get_user_profile(identity: str) -> dict[str, str]:
    key = _profile_setting_key(identity)
    if not key:
        return {}
    raw = STORE.get_setting(key, "")
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        return {}
    post_name = _clean_user_profile_value(payload.get("post", ""))
    position = _clean_user_profile_value(payload.get("position", ""))
    return {"post": post_name, "position": position} if post_name and position else {}


def set_user_profile(identity: str, post_name: str, position: str) -> dict[str, str]:
    key = _profile_setting_key(identity)
    profile = {
        "post": _clean_user_profile_value(post_name),
        "position": _clean_user_profile_value(position),
    }
    if key and profile["post"] and profile["position"]:
        STORE.set_setting(key, json.dumps(profile, ensure_ascii=False, separators=(",", ":")))
        return profile
    return {}


def get_contact_profile(chat_id: str = "", phone: str = "") -> dict[str, str]:
    """Resolve saved post/position across phone/chat aliases and heal them."""
    identities = _language_identities(chat_id, phone)
    values = [(identity, get_user_profile(identity)) for identity in identities]
    selected = next((profile for _identity, profile in values if profile), {})
    if selected:
        for identity, profile in values:
            if profile != selected:
                set_user_profile(identity, selected["post"], selected["position"])
    return dict(selected)


def set_contact_profile(chat_id: str, phone: str, post_name: str, position: str) -> dict[str, str]:
    post_value = _clean_user_profile_value(post_name)
    position_value = _clean_user_profile_value(position)
    if not post_value or not position_value:
        return {}
    profile = {"post": post_value, "position": position_value}
    for identity in _language_identities(chat_id, phone):
        set_user_profile(identity, post_value, position_value)
    return profile


def profile_prompt_text(error: bool = False) -> str:
    if queue_user_locale.is_kz():
        prefix = "Екі жолды да көрсетіңіз.\n\n" if error else ""
        return (
            prefix
            + "Өтініммен жұмысты бастамас бұрын, постыңыз бен лауазымыңызды бір хабарламамен жазыңыз:\n"
            "Бекет: [бекет атауы]\n"
            "Лауазым: [лауазымыңыз]\n\n"
            "Бұл мәліметтер сақталады, келесі өтінімдерде қайта енгізудің қажеті жоқ."
        )
    prefix = "Укажите оба поля.\n\n" if error else ""
    return (
        prefix
        + "Перед началом работы с заявками укажите одним сообщением свой пост и должность:\n"
        "Пост: [название поста]\n"
        "Должность: [ваша должность]\n\n"
        "Эти данные сохранятся, и в следующих заявках вводить их повторно не потребуется."
    )


def parse_contact_profile(text: str) -> dict[str, str]:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return {}
    post_name = ""
    position = ""
    unlabeled: list[str] = []
    for source_line in raw.split("\n"):
        line = source_line.strip().lstrip("•*-—– ").strip()
        if not line:
            continue
        match = re.match(r"(?i)^(?:пост|бекет)\s*[:=\-–—]\s*(.+)$", line)
        if match:
            post_name = _clean_user_profile_value(match.group(1))
            continue
        match = re.match(r"(?i)^(?:должность|лауазым)\s*[:=\-–—]\s*(.+)$", line)
        if match:
            position = _clean_user_profile_value(match.group(1))
            continue
        unlabeled.append(line)
    if not post_name and not position and len(unlabeled) == 2:
        post_name = _clean_user_profile_value(unlabeled[0])
        position = _clean_user_profile_value(unlabeled[1])
    if not post_name or not position:
        return {}
    # A vehicle/document identifier is not an occupation, even in a two-line reply.
    if not re.search(r"[^\W\d_]{3,}", position, re.UNICODE):
        return {}
    if any(re.search(r"\d", token) for token in position.split()) and not re.search(r"[^\W\d_]{3,}", " ".join(t for t in position.split() if not re.search(r"\d", t)), re.UNICODE):
        return {}
    return {"post": post_name, "position": position}


def apply_contact_profile_to_ticket(ticket: dict[str, object], profile: dict[str, str]) -> None:
    post_name = _clean_user_profile_value(profile.get("post", ""))
    position = _clean_user_profile_value(profile.get("position", ""))
    if not post_name or not position:
        return
    profile_summary = f"Пост: {post_name} · Должность: {position}"
    current_summary = normalize_message(str(ticket.get("summary", "") or ""))
    if current_summary and profile_summary not in current_summary:
        ticket["summary"] = f"{profile_summary} · {current_summary}"
    elif not current_summary:
        ticket["summary"] = profile_summary
    current_original = str(ticket.get("original_text", "") or "").strip()
    profile_block = f"Пост: {post_name}\nДолжность: {position}"
    if profile_block not in current_original:
        ticket["original_text"] = f"{profile_block}\n\n{current_original}".strip()


def after_language_intro() -> str:
    return tr(
        "Вы обратились в службу технической поддержки.",
        "Сіз техникалық қолдау қызметіне жүгіндіңіз.",
    )


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
queue_ticket_views.bind(globals())
queue_admin_pages.bind(globals())

# 1.00.6.29: connector/chat HTTP handlers live in a dedicated mixin.
queue_whatsapp_handlers.bind(globals(), sys.modules[__name__])

# 1.00.6.31: core HTTP routing/session/response handlers moved to a dedicated mixin.
queue_http_handler.bind(globals(), sys.modules[__name__])

class TicketHandler(queue_whatsapp_handlers.WhatsAppHandlerMixin, queue_http_handler.QueueHTTPHandlerMixin, BaseHTTPRequestHandler):
    server_version = "UnifiedQueue/1.00.1"


def main() -> None:
    cleanup_retention_once()
    threading.Thread(target=retention_worker, name="queue-retention", daemon=True).start()
    threading.Thread(target=scheduled_backup_worker, name="queue-scheduled-backup", daemon=True).start()
    threading.Thread(target=queue_reliability.worker_loop, args=(sys.modules[__name__],), name="queue-reliability", daemon=True).start()
    threading.Thread(target=queue_performance.worker_loop, args=(sys.modules[__name__],), name="queue-performance", daemon=True).start()
    threading.Thread(target=queue_stale_menu.worker_loop, args=(sys.modules[__name__],), name="queue-stale-menu", daemon=True).start()
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
