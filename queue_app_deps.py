"""Everything feature modules use from app.py, declared in one place.

app.py builds one AppDependencies after all of its names exist and hands it to
the modules (bind() or a function argument). A module can use only what is
listed here; a name missing from app.py fails at startup, not on first use.
Field names match the app.py names they are filled from.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import queue_auth
    from queue_generic_profile import BotMenu
    from ticketing import TicketStore


@dataclass(frozen=True)
class AppDependencies:
    # settings fixed at startup
    ACTIVE_TICKETS_CONTEXT: str
    ACTIVE_TICKET_FOLLOWUP_CONTEXT: str
    ADMIN_SESSION_COOKIE: str
    ADMIN_SESSION_MAX_AGE: int
    ADMIN_SESSION_TOKEN: str
    ADMIN_USER: str
    APP_VERSION: str
    AUTH_SESSION_COOKIE: str
    AUTH_SESSION_MAX_AGE: int
    DATABASE_PATH: Path
    DATA_DIR: Path
    ERROR_REPORT_CONTEXT: str
    LOCAL_MODE: bool
    LOCAL_TIMEZONE: tzinfo
    MAX_EMPLOYEES: int
    MAX_MEDIA_BYTES: int
    MEDIA_DIR: Path
    MONITOR_LOG_PATH: Path
    OUTBOUND_MEDIA_DIR: Path
    PAGE_SIZE: int
    PRE_TICKET_IMAGE_LOOKBACK_SECONDS: int
    ROOT: Path
    SUPPORT_MODE_CONTEXT: str
    USER_PROFILE_CONTEXT: str
    WEBHOOK_TOKEN: str

    # shared mutable state (changed in place, never replaced)
    CATEGORY_ARCHIVE: dict
    CHAT_STATE: dict
    EMPLOYEES: list
    ERROR_REPORT_STATUS_LABELS: dict
    REACTION_EMOJIS: set
    READ_MARKED_AT: dict

    # services
    bot_menu: Callable[[], BotMenu]  # current menu; replaced when the profile changes
    AUTH: queue_auth.AuthStore
    CHAT_LOCK: Any
    REPORTING: Any
    STORE: TicketStore
    UPLOADS: Any

    # functions defined in app.py
    _analytics_snapshot: Callable[..., Any]
    _final_ticket_filter: Callable[..., Any]
    _language_identity: Callable[..., Any]
    activate_employee_shift: Callable[..., Any]
    active_ticket_selected_reply: Callable[..., Any]
    active_tickets_menu_number: Callable[..., Any]
    active_tickets_reply: Callable[..., Any]
    admin_layout: Callable[..., Any]
    apply_bot_profile: Callable[..., Any]
    apply_contact_profile_to_ticket: Callable[..., Any]
    clear_contact_language: Callable[..., Any]
    create_scheduled_backup: Callable[..., Any]
    csrf_token: Callable[..., Any]
    csrf_token_valid: Callable[..., Any]
    decorate_chat_messages: Callable[..., Any]
    error_report_prompt: Callable[..., Any]
    favorite_chat_ids: Callable[..., Any]
    get_contact_language: Callable[..., Any]
    get_contact_profile: Callable[..., Any]
    global_auto_reply_enabled: Callable[..., Any]
    is_active_tickets_command: Callable[..., Any]
    language_selection_text_v3: Callable[..., Any]
    mention_only_message: Callable[..., Any]
    parse_contact_profile: Callable[..., Any]
    preserved_system_outgoing_media: Callable[..., Any]
    process_resource_snapshot: Callable[..., Any]
    profile_prompt_text: Callable[..., Any]
    reconcile_uncertain_system_outgoing_media: Callable[..., Any]
    render_account: Callable[..., Any]
    render_admin_users: Callable[..., Any]
    render_forgot_password: Callable[..., Any]
    render_local_setup: Callable[..., Any]
    render_login: Callable[..., Any]
    render_register: Callable[..., Any]
    same_whatsapp_message_identity: Callable[..., Any]
    save_media_payload: Callable[..., Any]
    save_outbound_media: Callable[..., Any]
    save_outbound_media_stream: Callable[..., Any]
    scheduled_backup_snapshot: Callable[..., Any]
    scrub_deleted_whatsapp_message: Callable[..., Any]
    set_chat_favorite: Callable[..., Any]
    set_contact_language: Callable[..., Any]
    set_contact_profile: Callable[..., Any]
    support_question_accepted_reply: Callable[..., Any]
    suppress_inbound_auto_reply: Callable[..., Any]
    ticket_accepted_reply: Callable[..., Any]
    ticket_status_reply_text: Callable[..., Any]
    update_ticket_status: Callable[..., Any]
    v3_duplicate_reply: Callable[..., Any]
    v3_missing_fields_reply: Callable[..., Any]
