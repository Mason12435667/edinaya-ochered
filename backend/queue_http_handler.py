"""Обработчик HTTP-запросов: таблицы маршрутов GET и POST и порядок их обработки.

Сами маршруты лежат по темам: queue_http_sessions (вход и сессии), queue_http_chat (чаты и
коннектор), queue_http_tickets (заявки), queue_http_admin (админка); общая основа — queue_http_base.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import queue_auth
import queue_performance_http
import queue_realtime
import queue_reliability
import queue_ticket_views
from queue_avatars import AvatarCache
from queue_core import log_suppressed
from queue_menu_config import MenuConfig
from queue_work_context import audit_admin_form_submission
from ticketing import TicketStore
from queue_http_base import HttpBase
from queue_http_sessions import SessionRoutes
from queue_http_admin import AdminRoutes
from queue_http_chat import ChatRoutes
from queue_http_tickets import TicketRoutes


@dataclass(frozen=True)
class HttpDependencies:
    store: TicketStore
    auth: queue_auth.AuthStore
    root: Path
    media_dir: Path
    monitor_log_path: Path
    avatars: AvatarCache
    menu_config: MenuConfig
    reliability: queue_reliability.ReliabilityMonitor
    categories: Mapping[str, str]
    employees: list[str]  # app.py edits this list in place
    max_employees: int
    page_size: int
    local_mode: bool
    app_version: str
    webhook_token: str
    reaction_emojis: set[str]
    admin_user: str
    admin_session_token: str
    admin_session_cookie: str
    admin_session_max_age: int
    auth_session_cookie: str
    auth_session_max_age: int
    csrf_token: Callable[..., str]
    csrf_token_valid: Callable[..., bool]
    render_login: Callable[..., str]
    render_register: Callable[..., str]
    render_forgot_password: Callable[..., str]
    render_local_setup: Callable[..., str]
    render_account: Callable[..., str]
    admin_users_data: Callable[[], dict[str, object]]
    final_ticket_filter: Callable[..., list]
    update_ticket_status: Callable[..., tuple]
    set_chat_favorite: Callable[[str, bool], tuple[bool, str]]
    create_scheduled_backup: Callable[..., object]


# Files under static/ that may be served (every page asset is part of the main layout).
STATIC_FILES = {
    "favicon.png": "image/png",
    **{name: "text/css; charset=utf-8" for name in queue_ticket_views.MAIN_STYLESHEETS},
    **{name: "text/javascript; charset=utf-8" for name in (*queue_ticket_views.MAIN_SCRIPTS, *queue_ticket_views.PAGE_SCRIPTS)},
    **{f"templates/{path.name}": "text/html; charset=utf-8"
       for path in (Path(__file__).resolve().parents[1] / "frontend" / "static" / "templates").glob("*.html")},
}
# Served names that are several files sent as one, in this order.
STATIC_BUNDLES = {"app.js": tuple(f"app/{name}" for name in queue_ticket_views.APP_SCRIPT_PARTS)}


class _CountedBody:
    """The request body stream, counting what the routes have read from it."""

    def __init__(self, raw):
        self.raw = raw
        self.count = 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.count += len(data)
        return data

    def readline(self, size=-1):
        data = self.raw.readline(size)
        self.count += len(data)
        return data

    def readinto(self, buffer):
        size = self.raw.readinto(buffer) or 0
        self.count += size
        return size

    def __getattr__(self, name):
        return getattr(self.raw, name)


class QueueHTTPHandlerMixin(ChatRoutes, AdminRoutes, TicketRoutes, SessionRoutes, HttpBase):
    @staticmethod
    def service_post_path(path: str) -> bool:
        return path in {
            "/api/media-stage",
            "/api/whatsapp",
            "/api/inbound-recovery-reply",
            "/api/template-error-delivery",
            "/api/outbound/claim",
            "/api/outbound/heartbeat",
            "/api/outbound/begin",
            "/api/outbound/result",
            "/api/chat-list-sync",
            "/api/group-list-sync",
            "/api/presence-sync",
            "/api/group-participants-sync",
            "/api/group-message-identities-sync",
            "/api/contact-list-sync",
            "/api/avatar-sync",
            "/api/contact-profile-sync",
            "/api/group-refresh-check",
            "/api/connector-state",
            "/api/contact-policy",
            "/api/call-permission",
            "/api/chat-messages-sync",
            "/api/chat-message-ack",
            "/api/chat-reaction-sync",
            "/api/whatsapp-action/claim",
            "/api/whatsapp-action/result",
            "/api/chat-control",
        }

    def do_GET(self) -> None:
        queue_auth.clear_current_user()
        if not self.request_host_allowed():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        public_get = (
            parsed.path in {"/login", "/register", "/forgot-password", "/setup", "/health", "/health/connector", "/admin/login"}
            or parsed.path.startswith("/static/")
            or parsed.path.startswith("/api/integration/")
            or parsed.path == "/favicon.ico"
        )
        if not public_get and not self.require_user():
            return
        user = self.effective_user()
        if user:
            queue_auth.set_current_user(user)
        route = self.EARLY_GET_ROUTES.get(parsed.path)
        if route:
            route(self, query)
            return
        if queue_performance_http.get(self):
            return
        if self.reliability_routes.get(self):
            return
        if self.workflow_routes.get(self):
            return
        if self.productivity_routes.get(self):
            return
        if self.feature_routes.get(self):
            return
        route = self.ADMIN_GET_ROUTES.get(parsed.path)
        if route:
            if self.require_admin():
                route(self, query)
            return
        route = self.GET_ROUTES.get(parsed.path)
        if route:
            route(self, query)
            return
        name = parsed.path[len("/static/"):]
        if parsed.path.startswith("/static/") and name in STATIC_FILES:
            folder = self.deps.root / "frontend" / "static"
            parts = STATIC_BUNDLES.get(name, (name,))
            self.uncached_response(b"".join((folder / part).read_bytes() for part in parts), STATIC_FILES[name])
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _get_api_realtime_wait(self, query) -> None:
        try:
            since = max(0, int(query.get("since", ["0"])[0] or 0))
        except (TypeError, ValueError):
            since = 0
        try:
            timeout_ms = max(250, min(25000, int(query.get("timeout", ["25000"])[0] or 25000)))
        except (TypeError, ValueError):
            timeout_ms = 25000
        state = queue_realtime.wait_for_change(since, timeout_ms / 1000.0)
        self.json_response(state)

    def _get_api_app_page(self, query) -> None:
        page = query.get("page", [""])[0]
        providers = {
            "me": self.ticket_views.my_page_data,
            "accounts": lambda: self.ticket_views.accounts_data(self.saved_account_sessions(touch=False), self.current_auth_slot()),
            "reminders": self.productivity_routes.reminders_data,
            "recent": self.workflow_routes.recent_data,
        }
        if page not in providers:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.json_response(providers[page]())

    def _get_api_page_context(self, query) -> None:
        self.json_response(self.ticket_views.page_context(self.is_admin()))

    def _get_favicon(self, query) -> None:
        self.file_response(self.deps.root / "frontend" / "static" / "favicon.png", "image/png")

    def _get_health(self, query) -> None:
        self.json_response({"status": "ok"})

    def do_POST(self) -> None:
        body = _CountedBody(self.rfile)
        self.rfile = body
        try:
            self._route_post()
        finally:
            self.rfile = body.raw
            self.discard_unread_body(body.count)

    def _route_post(self) -> None:
        queue_auth.clear_current_user()
        self._body_cache = None
        if not self.request_host_allowed():
            return
        parsed = urlparse(self.path)
        route = self.PUBLIC_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed)
            return
        if not self.service_post_path(parsed.path) and not self.require_user():
            return
        user = self.effective_user()
        if user:
            queue_auth.set_current_user(user)
        # Connector endpoints authenticate with X-Webhook-Token instead.
        if not self.service_post_path(parsed.path) and not self.csrf_check_passed(parsed.path):
            return
        if parsed.path.startswith("/admin/") and self.is_admin():
            try:
                self.deps.auth.record_audit(user or {}, "admin_post", object_type="admin_settings", object_id=parsed.path, details="Отправлено изменение в административном разделе")
            except sqlite3.Error:
                log_suppressed("queue_http_handler.do_POST")
        route = self.EARLY_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed)
            return
        if queue_performance_http.post(self):
            return
        if self.reliability_routes.post(self):
            return
        if queue_reliability.maintenance_active(self.deps.store) and queue_reliability.should_block_mutation(parsed.path, self.is_admin()):
            if parsed.path.startswith("/api/") or self.headers.get("X-Requested-With") == "fetch" or parsed.path.startswith("/chat"):
                self.json_response({"error":"Режим обслуживания: изменения временно заблокированы администратором"}, HTTPStatus.LOCKED)
            else:
                self.redirect("/?notice=" + quote("Режим обслуживания: изменения временно заблокированы"))
            return
        if self.workflow_routes.post(self):
            return
        if self.productivity_routes.post(self):
            return
        if self.feature_routes.post(self):
            return
        if self.upload_routes.post(self):
            return
        route = self.SERVICE_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed)
            return
        form = self.read_form()
        route = self.FORM_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed, form)
            return
        audit_admin_form_submission(parsed.path, form, self.effective_user())
        route = self.AUDITED_FORM_POST_ROUTES.get(parsed.path)
        if route:
            route(self, parsed, form)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    # signed-in, before feature modules
    EARLY_GET_ROUTES = {
        "/search": ChatRoutes._get_search,
        "/bookmarks": ChatRoutes._get_bookmarks,
        "/api/message-bookmarks": ChatRoutes._get_api_message_bookmarks,
        "/api/bookmarks": ChatRoutes._get_api_message_bookmarks,
        "/api/global-search": ChatRoutes._get_api_global_search,
        "/api/account-preferences": SessionRoutes._get_api_account_preferences,
        "/api/realtime-wait": _get_api_realtime_wait,
    }

    # after feature modules; administrators only (others are sent to /admin/login)
    ADMIN_GET_ROUTES = {
        "/admin": AdminRoutes._get_admin_home,
        "/admin/": AdminRoutes._get_admin_home,
        "/admin/users": AdminRoutes._get_admin_users,
        "/admin/employees": AdminRoutes._get_admin_employees,
        "/admin/manual": AdminRoutes._get_admin_manual,
        "/admin/menu": AdminRoutes._get_admin_menu,
        "/admin/categories": AdminRoutes._get_admin_categories,
        "/admin/errors": AdminRoutes._get_admin_errors,
        "/admin/error": AdminRoutes._get_admin_error,
        "/admin/contacts": AdminRoutes._get_admin_contacts,
        "/admin/audit": AdminRoutes._get_admin_audit,
        "/admin/manager": AdminRoutes._get_admin_manager,
        "/admin/api-keys": AdminRoutes._get_admin_api_keys,
        "/admin/tickets-export.csv": AdminRoutes._get_admin_tickets_export,
        "/admin/system": AdminRoutes._get_admin_system,
        "/api/admin/contact-search": AdminRoutes._get_api_admin_contact_search,
        "/api/admin-page": AdminRoutes._get_api_admin_page,
        "/admin/groups": AdminRoutes._get_admin_groups,
        "/admin/whatsapp": AdminRoutes._get_to_admin_home,
        "/api/admin/connector-state": ChatRoutes._get_connector_state,
        "/qr": AdminRoutes._get_to_admin_home,
    }

    # after feature modules; public paths are listed in do_GET
    GET_ROUTES = {
        "/login": SessionRoutes._get_login,
        "/accounts": SessionRoutes._get_accounts,
        "/register": SessionRoutes._get_register,
        "/forgot-password": SessionRoutes._get_forgot_password,
        "/setup": SessionRoutes._get_setup,
        "/logout": SessionRoutes._get_logout,
        "/account": SessionRoutes._get_account,
        "/me": SessionRoutes._get_me,
        "/": TicketRoutes._get_dashboard,
        "/admin/login": SessionRoutes._get_admin_login,
        "/admin/logout": SessionRoutes._get_admin_logout,
        "/ticket": TicketRoutes._get_ticket,
        "/whatsapp": ChatRoutes._get_whatsapp,
        "/groups": ChatRoutes._get_groups,
        "/api/integration/ping": TicketRoutes._get_api_integration_ping,
        "/api/integration/tickets": TicketRoutes._get_api_integration_tickets,
        "/api/dashboard-data": TicketRoutes._get_api_dashboard_data,
        "/api/ticket-page": TicketRoutes._get_api_ticket_page,
        "/api/page-context": _get_api_page_context,
        "/api/app-page": _get_api_app_page,
        "/api/groups-page": ChatRoutes._get_api_groups_page,
        "/api/chat-state": ChatRoutes._get_api_chat_state,
        "/api/group-state": ChatRoutes._get_api_group_state,
        "/api/chat-history": ChatRoutes._get_api_chat_history,
        "/api/chat-action-status": ChatRoutes._get_api_chat_action_status,
        "/api/notifications": ChatRoutes._get_api_notifications,
        "/api/conversation-users": ChatRoutes._get_api_conversation_users,
        "/api/chat-media": ChatRoutes._get_api_chat_media,
        "/favicon.ico": _get_favicon,
        "/health/connector": ChatRoutes._get_health_connector,
        "/health": _get_health,
    }

    # before sign-in
    PUBLIC_POST_ROUTES = {
        "/login": SessionRoutes._post_login,
        "/register": SessionRoutes._post_register,
        "/forgot-password": SessionRoutes._post_forgot_password,
        "/setup": SessionRoutes._post_setup,
    }

    # signed-in, before feature modules and the maintenance gate
    EARLY_POST_ROUTES = {
        "/api/global-search": ChatRoutes._post_api_global_search,
        "/api/message-bookmarks": ChatRoutes._post_api_message_bookmarks,
        "/api/bookmarks": ChatRoutes._post_api_message_bookmarks,
        "/accounts/switch": SessionRoutes._post_accounts_switch,
        "/admin/api-keys": AdminRoutes._post_admin_api_keys,
    }

    # connector/JSON endpoints that read their own request body
    SERVICE_POST_ROUTES = {
        "/api/system-warning-action": AdminRoutes._post_api_system_warning_action,
        "/api/media-stage": ChatRoutes._post_api_media_stage,
        "/api/whatsapp": ChatRoutes._post_api_whatsapp,
        "/api/inbound-recovery-reply": ChatRoutes._post_inbound_recovery_reply,
        "/api/template-error-delivery": ChatRoutes._post_api_template_error_delivery,
        "/api/outbound/claim": ChatRoutes._post_api_outbound_claim,
        "/api/outbound/heartbeat": ChatRoutes._post_api_outbound_heartbeat,
        "/api/outbound/begin": ChatRoutes._post_api_outbound_heartbeat,
        "/api/outbound/result": ChatRoutes._post_api_outbound_result,
        "/api/chat-list-sync": ChatRoutes._post_api_chat_list_sync,
        "/api/group-list-sync": ChatRoutes._post_api_group_list_sync,
        "/api/presence-sync": ChatRoutes._post_api_presence_sync,
        "/api/group-participants-sync": ChatRoutes._post_api_group_participants_sync,
        "/api/group-message-identities-sync": ChatRoutes._post_api_group_message_identities_sync,
        "/api/contact-list-sync": ChatRoutes._post_api_contact_list_sync,
        "/api/avatar-sync": ChatRoutes._post_api_avatar_sync,
        "/api/contact-profile-sync": ChatRoutes._post_api_contact_profile_sync,
        "/api/group-refresh-check": ChatRoutes._post_api_group_refresh_check,
        "/api/connector-state": ChatRoutes._post_api_connector_state,
        "/api/contact-policy": ChatRoutes._post_api_contact_policy,
        "/api/call-permission": ChatRoutes._post_api_call_permission,
        "/api/chat-messages-sync": ChatRoutes._post_api_chat_messages_sync,
        "/api/chat-message-ack": ChatRoutes._post_api_chat_message_ack,
        "/api/chat-reaction-sync": ChatRoutes._post_api_chat_reaction_sync,
        "/chat-media-send": ChatRoutes._post_chat_media_send,
        "/chat-media-send-raw": ChatRoutes._post_chat_media_send_raw,
        "/chat-forward": ChatRoutes._post_chat_forward,
        "/api/whatsapp-action/claim": ChatRoutes._post_api_whatsapp_action_claim,
        "/api/whatsapp-action/result": ChatRoutes._post_api_whatsapp_action_result,
        "/api/chat-control": ChatRoutes._post_api_chat_control,
    }

    # form posts that are not written to the admin form audit
    FORM_POST_ROUTES = {
        "/api/account-preferences": SessionRoutes._post_api_account_preferences,
    }

    # form posts recorded by audit_admin_form_submission
    AUDITED_FORM_POST_ROUTES = {
        "/conversation-lock": ChatRoutes._post_conversation_lock,
        "/account/password": SessionRoutes._post_account_password,
        "/admin/users": AdminRoutes._post_admin_users,
        "/context-history/apply": TicketRoutes._post_context_history_apply,
        "/chat-favorite": ChatRoutes._post_chat_favorite,
        "/group-mute": ChatRoutes._post_group_mute,
        "/admin/employees": AdminRoutes._post_admin_employees,
        "/admin/ticket-delete": AdminRoutes._post_admin_ticket_delete,
        "/admin/contacts": AdminRoutes._post_admin_contacts,
        "/admin/manual": AdminRoutes._post_admin_manual,
        "/admin/system": AdminRoutes._post_admin_system,
        "/admin/groups": AdminRoutes._post_admin_groups,
        "/admin/errors": AdminRoutes._post_admin_errors,
        "/admin/categories": AdminRoutes._post_admin_categories,
        "/active-employee": AdminRoutes._post_active_employee,
        "/handoff": TicketRoutes._post_handoff,
        "/status": TicketRoutes._post_status,
        "/quick-status": TicketRoutes._post_quick_status,
        "/quick-priority": TicketRoutes._post_quick_priority,
        "/classification": TicketRoutes._post_classification,
        "/send-reply": TicketRoutes._post_send_reply,
        "/chat-send": ChatRoutes._post_chat_send,
        "/chat-mode": ChatRoutes._post_chat_mode,
        "/group-send": ChatRoutes._post_group_send,
        "/chat-action": ChatRoutes._post_chat_action,
        "/manual": TicketRoutes._post_manual,
    }
