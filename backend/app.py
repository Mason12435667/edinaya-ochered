"""Сборка приложения: создаёт хранилище, сервисы и маршруты, связывает их и запускает HTTP-сервер."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import webbrowser
import queue_bot_texts
import queue_productivity
import queue_contextual_tickets
import queue_workflow
import queue_retention
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
import queue_whatsapp_handlers
import queue_http_handler
import queue_ticket_service
import queue_work_context
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ticketing import CATEGORIES, PRIORITIES, STATUSES, TicketStore
from queue_store_conversations import MENU_CONTEXT
import queue_generic_profile
from queue_menu_config import MenuConfig
from queue_bot_menu import BotMenu
from queue_contact_profiles import ContactProfiles
from queue_inbound_service import InboundDependencies, InboundService
from queue_attachment_archive import AttachmentArchive
from queue_outbound_service import OutboundService
from queue_chat_media import ChatMedia, mention_only_message, same_whatsapp_message_identity
from queue_chat_state import ChatViews, ConnectorState
from queue_ticket_pages import TicketPages
from queue_ticket_views import TicketViews
from queue_admin_pages import AdminPageDependencies, AdminPages
from queue_stale_menu import StaleMenuRecovery
from queue_productivity_http import ProductivityRoutes
from queue_reliability_http import ReliabilityRoutes
from queue_workflow_http import WorkflowRoutes
from queue_feature_http import FeatureRoutes

# Public release from CHANGELOG.md; reported by /api/integration/ping.
APP_VERSION = "v143"


ROOT = Path(__file__).resolve().parents[1]

# First modularization phase. Keep the historic names in app.py as
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
queue_workflow.initialize(STORE)
queue_reliability.initialize(STORE)
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

# Ordinary WhatsApp users receive Kazakh texts. The employee/admin
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


def cleanup_retention_once() -> None:
    try:
        result = queue_retention.cleanup_retention(STORE)
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
        retention_policy = queue_retention.retention_settings(STORE)
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

# Phase 4 large modularization (queue_work_context).
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
status_badge = queue_work_context.status_badge
priority_badge = queue_work_context.priority_badge
employee_options = queue_work_context.employee_options
category_options = queue_work_context.category_options
priority_options = queue_work_context.priority_options
query_page = queue_work_context.query_page


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


# ReliabilityMonitor reads the connector state and ChatViews reads its alerts: this order has no cycle.
CONNECTOR_STATE = ConnectorState(CHAT_LOCK, CHAT_STATE)
connector_state_snapshot = CONNECTOR_STATE.snapshot
RELIABILITY = queue_reliability.ReliabilityMonitor(STORE, DATA_DIR, DATABASE_PATH, ROOT, connector_state_snapshot,
                                                   ticket_sla_state, MAX_MEDIA_BYTES)
ATTACHMENTS = AttachmentArchive(STORE, MEDIA_DIR, OUTBOUND_MEDIA_DIR, UPLOADS.root)
# ChatViews takes decorate_chat_messages, and decorating reads names from ChatViews, so names are looked up per call.
CHAT_MEDIA = ChatMedia(STORE, ATTACHMENTS, AVATARS, database_path=DATABASE_PATH, media_dir=MEDIA_DIR,
                       outbound_media_dir=OUTBOUND_MEDIA_DIR, max_media_bytes=MAX_MEDIA_BYTES,
                       chat_lock=CHAT_LOCK, chat_state=CHAT_STATE, employees=EMPLOYEES,
                       display_names=lambda: display_names())
save_media_payload = CHAT_MEDIA.save_media_payload
scrub_deleted_whatsapp_message = CHAT_MEDIA.scrub_deleted_whatsapp_message
save_outbound_media = CHAT_MEDIA.save_outbound_media
save_outbound_media_stream = CHAT_MEDIA.save_outbound_media_stream
preserved_system_outgoing_media = CHAT_MEDIA.preserved_system_outgoing_media
reconcile_uncertain_system_outgoing_media = CHAT_MEDIA.reconcile_uncertain_system_outgoing_media
decorate_chat_messages = CHAT_MEDIA.decorate_chat_messages
CHAT_VIEWS = ChatViews(STORE, AUTH, chat_lock=CHAT_LOCK, chat_state=CHAT_STATE, connector=CONNECTOR_STATE,
                       reliability=RELIABILITY, avatars=AVATARS, categories=CATEGORIES,
                       database_path=DATABASE_PATH, data_dir=DATA_DIR, favorite_chat_ids=favorite_chat_ids,
                       decorate_chat_messages=decorate_chat_messages)
display_names = CHAT_VIEWS.display_names


TICKET_VIEWS = TicketViews(STORE, AUTH, csrf_token=csrf_token, categories=CATEGORIES,
                           local_mode=LOCAL_MODE, page_size=PAGE_SIZE)
TICKET_PAGES = TicketPages(STORE, categories=CATEGORIES, page_size=PAGE_SIZE)




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


def admin_users_data() -> dict[str, object]:
    return queue_admin_views.admin_users_data(auth=AUTH, employees=EMPLOYEES, human_time=human_time)


def admin_tabs(active: str) -> str:
    return queue_admin_views.admin_tabs(active, escape=e)


# ---------------------------------------------------------------------------
# Language selection before the ordinary-user WhatsApp flow.
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


OUTBOUND_SERVICE = OutboundService(STORE, ATTACHMENTS, queue_realtime, global_auto_reply_enabled)
STALE_MENU = StaleMenuRecovery(STORE, MENU, CONTACTS, queue_realtime, global_auto_reply_enabled)
PRODUCTIVITY_ROUTES = ProductivityRoutes(STORE, DATA_DIR, DATABASE_PATH,
                                         category_items=active_ticket_category_items,
                                         active_employee=active_employee)
WORKFLOW_ROUTES = WorkflowRoutes(STORE,
                                 category_items=active_ticket_category_items,
                                 employees=EMPLOYEES, work_actor=work_actor,
                                 update_ticket_status=update_ticket_status, events=queue_realtime)
RELIABILITY_ROUTES = ReliabilityRoutes(STORE, RELIABILITY,
                                       max_media_bytes=MAX_MEDIA_BYTES, active_employee=active_employee,
                                       chat_lock=CHAT_LOCK, chat_state=CHAT_STATE, avatars=AVATARS)
FEATURE_ROUTES = FeatureRoutes(STORE, AVATARS, ROOT, decorate_chat_messages=decorate_chat_messages)
UPLOAD_ROUTES = queue_uploads.UploadRoutes(UPLOADS, work_actor)
ADMIN_PAGES = AdminPages(AdminPageDependencies(
    store=STORE, auth=AUTH, menu=MENU, menu_config=MENU_CONFIG, contacts=CONTACTS, tickets=TICKET_SERVICE,
    reporting=REPORTING, runtime=RUNTIME, avatars=AVATARS, employees=EMPLOYEES, max_employees=MAX_EMPLOYEES,
    local_mode=LOCAL_MODE, chat_lock=CHAT_LOCK, chat_state=CHAT_STATE, categories=CATEGORIES,
))
HTTP_DEPENDENCIES = queue_http_handler.HttpDependencies(
    store=STORE, auth=AUTH, root=ROOT, media_dir=MEDIA_DIR, monitor_log_path=MONITOR_LOG_PATH, avatars=AVATARS,
    menu_config=MENU_CONFIG, reliability=RELIABILITY, categories=CATEGORIES, employees=EMPLOYEES,
    max_employees=MAX_EMPLOYEES, page_size=PAGE_SIZE, local_mode=LOCAL_MODE, app_version=APP_VERSION,
    webhook_token=WEBHOOK_TOKEN, reaction_emojis=REACTION_EMOJIS, admin_user=ADMIN_USER,
    admin_session_token=ADMIN_SESSION_TOKEN, admin_session_cookie=ADMIN_SESSION_COOKIE,
    admin_session_max_age=ADMIN_SESSION_MAX_AGE, auth_session_cookie=AUTH_SESSION_COOKIE,
    auth_session_max_age=AUTH_SESSION_MAX_AGE, csrf_token=csrf_token, csrf_token_valid=csrf_token_valid,
    render_login=render_login, render_register=render_register, render_forgot_password=render_forgot_password,
    render_local_setup=render_local_setup, render_account=render_account, admin_users_data=admin_users_data,
    final_ticket_filter=_final_ticket_filter, update_ticket_status=update_ticket_status,
    set_chat_favorite=set_chat_favorite, create_scheduled_backup=create_scheduled_backup,
)
WHATSAPP_DEPENDENCIES = queue_whatsapp_handlers.WhatsAppDependencies(
    store=STORE, attachments=ATTACHMENTS, chat_lock=CHAT_LOCK, chat_state=CHAT_STATE,
    read_marked_at=READ_MARKED_AT, media_dir=MEDIA_DIR, max_media_bytes=MAX_MEDIA_BYTES,
    get_contact_language=get_contact_language, decorate_chat_messages=decorate_chat_messages,
    mention_only_message=mention_only_message, same_whatsapp_message_identity=same_whatsapp_message_identity,
    scrub_deleted_whatsapp_message=scrub_deleted_whatsapp_message, save_media_payload=save_media_payload,
    save_outbound_media=save_outbound_media, save_outbound_media_stream=save_outbound_media_stream,
    preserved_system_outgoing_media=preserved_system_outgoing_media,
    reconcile_uncertain_system_outgoing_media=reconcile_uncertain_system_outgoing_media,
)

# Connector/chat handlers (queue_whatsapp_handlers) and core HTTP routing
# (queue_http_handler) live in dedicated mixins.
class TicketHandler(queue_whatsapp_handlers.WhatsAppHandlerMixin, queue_http_handler.QueueHTTPHandlerMixin, BaseHTTPRequestHandler):
    inbound_service = INBOUND_SERVICE
    outbound_service = OUTBOUND_SERVICE
    productivity_routes = PRODUCTIVITY_ROUTES
    workflow_routes = WORKFLOW_ROUTES
    reliability_routes = RELIABILITY_ROUTES
    feature_routes = FEATURE_ROUTES
    upload_routes = UPLOAD_ROUTES
    chat_views = CHAT_VIEWS
    ticket_views = TICKET_VIEWS
    ticket_pages = TICKET_PAGES
    admin_pages = ADMIN_PAGES
    deps = HTTP_DEPENDENCIES
    whatsapp = WHATSAPP_DEPENDENCIES
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
            self.discard_unread_body()
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
