"""Transactional inbound dialog service; independent of HTTP handlers and app globals."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import queue_bot_texts as texts
from queue_bot_input import all_information_confirmed, remove_all_information_phrase
import queue_contextual_tickets
import queue_dialog_flow
import queue_inbound_media
import queue_productivity
import queue_user_locale
from queue_bot_menu import BotMenu
from queue_contact_profiles import ContactProfiles, USER_PROFILE_CONTEXT
from queue_core import normalize_message, normalize_phone, valid_chat_id
from queue_dialog_flow import pending_context_category
from queue_dialog_policy import (DialogAction, DialogInput, DialogSnapshot, RequestAction,
                                 choose_dialog_action, choose_request_action)
from queue_language import parse_menu_number
from queue_service_result import ServiceResult
from queue_ticket_service import TicketService
from queue_user_locale import tr
from queue_message_rules import process_incoming_message
from queue_store_conversations import MENU_CONTEXT, MENU_GATE_CONTEXT
from ticketing import TicketStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundDependencies:
    store: TicketStore
    menu: BotMenu
    contacts: ContactProfiles
    tickets: TicketService
    media_dir: Path
    save_media: Callable[[str, str, str, str, str], str]
    active_employee: Callable[[], str]
    auto_reply_enabled: Callable[[], bool]
    image_lookback_seconds: int = 600


@dataclass
class InboundMessage:
    payload: dict
    external_id: str = ""
    phone: str = ""
    sender: str = ""
    text: str = ""
    chat_id: str = ""
    contact_key: str = ""
    media_mime: str = ""
    media_name: str = ""
    media_path: str = ""
    message_timestamp: int = 0
    attachment_name: str = ""
    active_ticket: dict | None = None
    active_ticket_id: int = 0
    contact_profile: dict = field(default_factory=dict)
    context_mode: str = ""
    pending_category: str = ""
    chosen_category: str = ""
    forced_category: str = ""
    typed_choice: str = ""
    draft_finalized: bool = False
    draft_finish_command: bool = False
    force_new_requested: bool = False
    combined_request_text: str = ""
    strict_state: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)


class InboundService:
    def __init__(self, dependencies: InboundDependencies):
        self.dependencies = dependencies

    def handle(self, payload: dict) -> ServiceResult:
        # Per-message state must never be shared by concurrent HTTP requests.
        result = _InboundRun(self.dependencies).handle(payload)
        if self.dependencies.auto_reply_enabled():
            return result
        # The switch mutes replies while incoming messages still update the DB.
        muted = dict(result.payload)
        if muted.get("reply"):
            muted["auto_reply_suppressed"] = True
        muted.pop("reply", None)
        muted.pop("menu_options", None)
        muted.update(silent=True, auto_reply_disabled=True)
        return ServiceResult(muted, result.status)


class _InboundRun:
    def __init__(self, dependencies: InboundDependencies):
        self.deps = dependencies
        self.store = dependencies.store
        self.menu = dependencies.menu
        self.contacts = dependencies.contacts
        self._inbound_result: tuple[dict, int] | None = None

    def handle(self, payload: dict) -> ServiceResult:
        language = queue_user_locale.current_language()
        try:
            with self.store.atomic_inbound():
                external_id = str(payload.get("external_id", ""))
                contact = valid_chat_id(str(payload.get("chat_id", ""))) or normalize_phone(str(payload.get("phone", "")))
                if external_id and not self.store.claim_inbound_message(external_id, contact):
                    self._respond({"created": False, "duplicate": True})
                else:
                    self._process_api_message(payload)
                if self._inbound_result is None:
                    raise RuntimeError("Inbound response missing")
        except Exception as error:
            logger.error("Inbound transaction rolled back: %s", type(error).__name__)
            return ServiceResult({"error": "inbound_processing_failed", "retryable": True}, 503)
        finally:
            queue_user_locale.set_language(language)
        return ServiceResult(*self._inbound_result)

    def _respond(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        # Serialize before committing; only send the response after COMMIT.
        json.dumps(payload, ensure_ascii=False)
        self._inbound_result = (payload, status)

    def _ticket_category_is_selectable(self, category: str) -> bool:
        category = str(category or "").strip()
        if not category:
            return False
        options = self.menu.enabled_menu_options()
        for option in options:
            if not bool(option.get("enabled", True)):
                continue
            if str(option.get("key", "")) != category:
                continue
            return self.menu.configured_category_action(category) == texts.MENU_ACTION_TICKET
        return False

    def _category_required_reply(self) -> dict[str, object]:
        return {
            "created": False,
            "awaiting_category": True,
            "main_menu": True,
            "force_menu": True,
            "reply": self.menu.main_menu_text(error=True),
            "menu_options": self.menu.menu_payload(),
        }


    def _process_api_message(self, payload: dict) -> None:
        key = valid_chat_id(str(payload.get("chat_id", ""))) or normalize_phone(str(payload.get("phone", "")))
        text = normalize_message(str(payload.get("text", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        state = queue_dialog_flow.load(self.store, key)
        command = text.casefold().strip()

        # EO_GLOBAL_MENU_DIGITS_20260930
        # Exact menu digits are commands, not free-form text.
        # 0 always means "main menu".
        # 1..9 always select the corresponding ENABLED configured category,
        # regardless of the previous dialog/category context.
        # Long numbers (e.g. 39855502/010926/9017998) are NOT affected.
        global_menu_digit = parse_menu_number(text)
        global_menu_category = self.menu.configured_menu_category(global_menu_digit) if global_menu_digit else ""
        global_menu_command = bool(
            command == "0"
            or (global_menu_digit and global_menu_category)
        )
        if global_menu_command:
            # EO_STRICT_CATEGORY_SELECTION_20260930
            # A ticket category is armed only by a separate, explicit numeric
            # menu choice. Text written before that choice is never recycled as
            # ticket details.
            state.pop("topic", None)
            state.pop("topic_attempts", None)
            state.pop("buffer", None)
            if command == "0":
                state.pop("strict_category", None)
                state.pop("strict_category_message", None)
            elif global_menu_digit and global_menu_category:
                state["strict_category"] = str(global_menu_category)
                state["strict_category_message"] = str(payload.get("external_id", "") or "")
            queue_dialog_flow.save(self.store, key, state)

            # Force the core into the main-menu namespace so a digit never gets
            # swallowed by active-ticket/follow-up submenus.
            active_for_menu = self.store.active_context_ticket(key)
            active_for_menu_id = int(active_for_menu["id"]) if active_for_menu else 0
            queue_contextual_tickets.reset_context(self.store, key, "")
            self.store.set_conversation_context(
                key,
                MENU_CONTEXT,
                active_for_menu_id,
                bool(active_for_menu),
            )
            if global_menu_digit and global_menu_category:
                payload = {**payload, "menu_choice": global_menu_digit}

        eligible = bool(key and self.contacts.get_contact_language(key, phone) and self.contacts.get_contact_profile(key, phone)
                        and not self.store.manual_whatsapp_contact(key, phone) and not self.store.manual_chat_mode(key)
                        and not queue_productivity.auto_reply_blocked(self.store, key))
        context = self.store.get_conversation_context(key) or {}
        active = self.store.active_context_ticket(key)
        intent = state.get("topic") if eligible else None
        if eligible and (intent or (active and not pending_context_category(context, self.menu.config.category_labels)
                and str(context.get("pending_category", "")) in {"", texts.ACTIVE_TICKET_FOLLOWUP_CONTEXT}
                and queue_dialog_flow.other_topic(text, str(active.get("category", ""))))):
            if intent and command in {"дополнение", "қосымша"}:
                state.pop("topic",None);state.pop("topic_attempts",None);queue_dialog_flow.save(self.store,key,state)
                self._process_api_message_core({**payload, **intent, "chat_id":key, "phone":phone})
                return
            if intent and command in {"новая", "жаңа", "0"}:
                state.pop("topic",None);state.pop("topic_attempts",None);queue_dialog_flow.save(self.store,key,state)
                queue_dialog_flow.remember(self.store,key,intent)
                self._process_api_message_core({**payload,"text":"0"})
                return
            if command in {"0","00","отмена","бас тарту"}:
                state.pop("topic",None);state.pop("topic_attempts",None);queue_dialog_flow.save(self.store,key,state)
                self._process_api_message_core(payload);return
            if not intent:
                state['topic'] = {k:payload.get(k,'') for k in ('text','attachment_name','external_id','media_mime')}
            state['topic_attempts'] = int(state.get('topic_attempts',0)) + 1
            queue_dialog_flow.save(self.store,key,state)
            if state['topic_attempts'] >= 3:
                queue_dialog_flow.review(self.store,key,'Не удалось определить: новая проблема или дополнение')
                self._respond({"created":False,"needs_review":True,"reply":tr("Передаю диалог сотруднику для разбора.","Диалогты маманға жіберемін.")});return
            self._respond({"created":False,"reply":queue_dialog_flow.topic_prompt()});return
        if command in {"0","отмена","бас тарту"}:
            state.pop('buffer',None);state.pop('topic',None);state.pop('topic_attempts',None)
            queue_dialog_flow.save(self.store,key,state)
        self._process_api_message_core(payload)
        result, status = self._inbound_result
        if result.get('duplicate') or result.get('silent') or result.get('manual_contact'): return
        pre_menu = any(result.get(k) for k in ('language_required','profile_required','awaiting_category','menu_gate','menu_reminder'))
        if pre_menu and command not in {'0','00','отмена','бас тарту'} and not (re.search(r'(?im)^(?:подразделение|отдел|филиал|бөлімше)\s*:', text) and re.search(r'(?im)^(?:должность|лауазым)\s*:', text)):
            if result.get('awaiting_category') or result.get('menu_gate') or result.get('menu_reminder'):
                # Strict category mode: messages before a category choice stay
                # visible in the chat but are never reused to create a ticket.
                flow_state = queue_dialog_flow.load(self.store, key)
                flow_state.pop('buffer', None)
                queue_dialog_flow.save(self.store, key, flow_state)
            else:
                queue_dialog_flow.remember(self.store, key, payload)

    def _process_api_message_core(self, payload: dict) -> None:
        msg = InboundMessage(payload=payload)
        self._inbound_parse(msg)
        self._inbound_store_chat_message(msg)
        # Access and onboarding gates precede application dialog commands.
        for gate in (self._inbound_manual_contact, self._inbound_manual_mode,
                     self._inbound_language, self._inbound_profile):
            if gate(msg):
                return
        self._inbound_dialog_state(msg)
        action = choose_dialog_action(
            DialogSnapshot(msg.context_mode, msg.pending_category),
            DialogInput(msg.text, msg.typed_choice, msg.chosen_category, msg.draft_finish_command),
        )
        handlers = {
            DialogAction.MAIN_MENU: self._enter_main_menu,
            DialogAction.UPGRADE_MENU: self._upgrade_menu_gate,
            DialogAction.SELECT_ACTIVE_TICKET: self._select_active_ticket,
            DialogAction.SELECTED_TICKET_FOLLOWUP: self._selected_ticket_followup,
            DialogAction.LIST_ACTIVE_TICKETS: self._list_active_tickets,
            DialogAction.IGNORE_ACK: self._ignore_ack,
            DialogAction.LEGACY_ERROR_REPORT: self._legacy_error_report,
            DialogAction.LEGACY_SUPPORT: self._legacy_support,
            DialogAction.CHOOSE_CATEGORY: self._choose_category,
            DialogAction.REQUIRE_CATEGORY: self._require_category,
            DialogAction.PROCESS_REQUEST: self._process_request,
        }
        handlers[action](msg)

    def _process_request(self, msg: InboundMessage) -> None:
        if self._inbound_ticket_followup(msg):
            return
        if self._inbound_validate_draft_category(msg):
            return
        if self._inbound_collect_draft(msg):
            return
        self._inbound_create_ticket(msg)

    def _inbound_parse(self, msg: InboundMessage) -> None:
        # Parse an already claimed message; deduplication precedes all state changes.
        msg.external_id = str(msg.payload.get("external_id", ""))
        msg.phone = normalize_phone(str(msg.payload.get("phone", "")))
        msg.sender = str(msg.payload.get("sender", "")).strip() or "Неизвестный отправитель"
        msg.text = normalize_message(str(msg.payload.get("text", "")))
        msg.chat_id = valid_chat_id(str(msg.payload.get("chat_id", "")))
        msg.contact_key = msg.chat_id or msg.phone
        msg.media_mime = normalize_message(str(msg.payload.get("media_mime", "")))[:120]
        msg.media_name = normalize_message(str(msg.payload.get("media_name", "") or msg.payload.get("attachment_name", "")))[:180]
        msg.media_path = ""
        media_base64 = str(msg.payload.get("media_base64", "") or "")
        if media_base64 and msg.chat_id and msg.external_id:
            msg.media_path = self.deps.save_media(msg.chat_id, msg.external_id, media_base64, msg.media_mime, msg.media_name)
        if msg.payload.get("media_receipt"):
            msg.media_path = queue_inbound_media.resolve(self.deps.media_dir, msg.chat_id, msg.external_id, msg.payload["media_receipt"]) or msg.media_path
        return

    def _inbound_store_chat_message(self, msg: InboundMessage) -> None:
        # Mirror an attachment message into the chat history.
        if msg.chat_id and msg.external_id and (msg.media_path or msg.media_name):
            try:
                msg.message_timestamp = max(0, int(msg.payload.get("message_timestamp", 0) or 0))
            except (ValueError, TypeError):
                msg.message_timestamp = 0
            self.store.save_whatsapp_chat_messages(
                msg.chat_id,
                [
                    {
                        "id": msg.external_id,
                        "from_me": False,
                        "sender": msg.sender,
                        "body": msg.text or ("[Вложение]" if msg.media_name else ""),
                        "type": normalize_message(str(msg.payload.get("message_type", "chat")))[:40] or "chat",
                        "timestamp": msg.message_timestamp,
                        "ack": 0,
                        "media_path": msg.media_path,
                        "media_mime": msg.media_mime,
                        "media_name": msg.media_name,
                    }
                ],
            )
        return

    def _inbound_manual_contact(self, msg: InboundMessage) -> bool:
        # Manual contacts are recorded but never answered by the bot.
        manual_contact = self.store.manual_whatsapp_contact(msg.chat_id, msg.phone)
        if manual_contact:
            try:
                msg.message_timestamp = max(0, int(msg.payload.get("message_timestamp", 0) or 0))
            except (ValueError, TypeError):
                msg.message_timestamp = 0
            self.store.save_whatsapp_chat_messages(
                msg.chat_id,
                [
                    {
                        "id": msg.external_id,
                        "from_me": False,
                        "sender": str(manual_contact.get("name", "")) or msg.sender,
                        "body": msg.text or ("[Вложение]" if msg.payload.get("attachment_name") else ""),
                        "type": normalize_message(str(msg.payload.get("message_type", "chat")))[:40] or "chat",
                        "timestamp": msg.message_timestamp,
                        "ack": 0,
                        "media_path": msg.media_path,
                        "media_mime": msg.media_mime,
                        "media_name": msg.media_name,
                    }
                ],
            )
            self._respond(
                {
                    "created": False,
                    "manual_contact": True,
                    "ignored": True,
                    "reason": "Контакт администратора",
                    "name": str(manual_contact.get("name", "")),
                }
            )
            return True
        return False

    def _inbound_manual_mode(self, msg: InboundMessage) -> bool:
        # An operator took over the chat, or auto-replies are blocked for it.
        manual_mode = self.store.manual_chat_mode(msg.chat_id) if msg.chat_id else None
        if manual_mode:
            # EO_GLOBAL_MENU_DIGITS_20260930:
            # A bot-generated review pause must never trap the user. An exact
            # menu digit immediately returns control to the bot. A pause set by
            # a real employee remains authoritative and is NOT bypassed.
            menu_digit_now = parse_menu_number(msg.text)
            menu_category_now = self.menu.configured_menu_category(menu_digit_now) if menu_digit_now else ""
            menu_command_now = bool(
                normalize_message(msg.text).strip() == "0"
                or (menu_digit_now and menu_category_now)
            )
            manual_actor = normalize_message(str(manual_mode.get("actor", "")))
            automatic_review_pause = manual_actor.casefold() == "бот: нужен разбор".casefold()
            if menu_command_now and automatic_review_pause and msg.chat_id:
                self.store.disable_manual_chat_mode(msg.chat_id, "Бот: команда меню")
                manual_mode = None
                print(
                    f"Автоматическая пауза снята командой меню {normalize_message(msg.text).strip()}: {msg.chat_id}",
                    flush=True,
                )

        if manual_mode:
            self._respond(
                {
                    "created": False,
                    "ignored": True,
                    "silent": True,
                    "reason": "Ручной диалог",
                    "manual_mode": manual_mode,
                }
            )
            return True

        if msg.chat_id and queue_productivity.auto_reply_blocked(self.store, msg.chat_id):
            self._respond({
                "created": False,
                "ignored": True,
                "silent": True,
                "reason": "Контакт исключён из автоответов",
            })
            return True
        return False

    def _inbound_language(self, msg: InboundMessage) -> bool:
        # Language selection (first contact and «00»).
        language_identity = self.contacts._language_identity(msg.chat_id, msg.phone)
        selected_language = self.contacts.get_contact_language(msg.chat_id, msg.phone)
        if selected_language:
            queue_user_locale.set_language(selected_language)

        if selected_language and (normalize_message(msg.text).strip() == "00" or queue_user_locale.is_language_change_command(msg.text)):
            self.contacts.clear_contact_language(msg.chat_id, msg.phone)
            self.store.clear_conversation_draft(msg.contact_key)
            queue_contextual_tickets.reset_context(self.store, msg.contact_key, "")
            self.store.set_conversation_context(msg.contact_key, "", 0, False)
            queue_user_locale.set_language(queue_user_locale.LANG_RU)
            self._respond({
                "created": False,
                "language_required": True,
                "reply": texts.language_selection_text_v3(),
            })
            return True

        if not selected_language:
            language_choice = queue_user_locale.parse_language_choice(msg.text)
            if not language_choice:
                queue_user_locale.set_language(queue_user_locale.LANG_RU)
                self._respond({
                    "created": False,
                    "language_required": True,
                    "reply": texts.language_selection_text_v3(),
                })
                return True

            selected_language = self.contacts.set_contact_language(msg.chat_id, msg.phone, language_choice)
            queue_user_locale.set_language(selected_language)
            existing_active_ticket = self.store.active_context_ticket(msg.contact_key)
            existing_active_ticket_id = int(existing_active_ticket["id"]) if existing_active_ticket else 0
            self.store.clear_conversation_draft(msg.contact_key)
            queue_contextual_tickets.reset_context(self.store, msg.contact_key, "")
            msg.contact_profile = self.contacts.get_contact_profile(msg.chat_id, msg.phone)
            if not msg.contact_profile:
                self.store.set_conversation_context(
                    msg.contact_key, USER_PROFILE_CONTEXT, existing_active_ticket_id, bool(existing_active_ticket)
                )
                self._respond({
                    "created": False,
                    "language_selected": selected_language,
                    "profile_required": True,
                    "reply": self.contacts.profile_prompt_text(),
                })
                return True
            self.store.set_conversation_context(
                msg.contact_key, MENU_CONTEXT, existing_active_ticket_id, bool(existing_active_ticket)
            )
            self._respond({
                "created": False,
                "language_selected": selected_language,
                "awaiting_category": True,
                "main_menu": True,
                "force_menu": True,
                "reply": self.menu.main_menu_text(),
                "menu_options": self.menu.menu_payload(),
            })
            return True
        return False

    def _inbound_profile(self, msg: InboundMessage) -> bool:
        # Department/position profile is required once per contact.
        msg.contact_profile = self.contacts.get_contact_profile(msg.chat_id, msg.phone)
        if not msg.contact_profile:
            parsed_profile = self.contacts.parse_flexible(msg.text)
            active_before_profile = self.store.active_context_ticket(msg.contact_key)
            active_before_profile_id = int(active_before_profile["id"]) if active_before_profile else 0
            if not parsed_profile:
                self.store.set_conversation_context(
                    msg.contact_key, USER_PROFILE_CONTEXT, active_before_profile_id, bool(active_before_profile)
                )
                self._respond({
                    "created": False,
                    "profile_required": True,
                    "reply": self.contacts.profile_prompt_text(error=True),
                })
                return True
            msg.contact_profile = self.contacts.set_contact_profile(
                msg.chat_id, msg.phone, parsed_profile["post"], parsed_profile["position"]
            )
            self.store.clear_conversation_draft(msg.contact_key)
            queue_contextual_tickets.reset_context(self.store, msg.contact_key, "")
            self.store.set_conversation_context(
                msg.contact_key, MENU_CONTEXT, active_before_profile_id, bool(active_before_profile)
            )
            self._respond({
                "created": False,
                "profile_saved": True,
                "post": msg.contact_profile.get("post", ""),
                "position": msg.contact_profile.get("position", ""),
                "main_menu": True,
                "force_menu": True,
                "reply": self.menu.main_menu_text(),
                "menu_options": self.menu.menu_payload(),
            })
            return True
        return False

    def _inbound_dialog_state(self, msg: InboundMessage) -> None:
        # Load the conversation context and interpret menu digits.
        context = self.store.get_conversation_context(msg.contact_key)
        msg.active_ticket = self.store.active_context_ticket(msg.contact_key)
        msg.active_ticket_id = int(msg.active_ticket["id"]) if msg.active_ticket else 0
        msg.context_mode = str(context.get("pending_category", "")) if context else ""
        msg.pending_category = pending_context_category(context, self.menu.config.category_labels)
        msg.force_new_requested = bool(context and context.get("force_new"))
        menu_choice = normalize_message(str(msg.payload.get("menu_choice", "")))
        msg.typed_choice = parse_menu_number(msg.text)

        # Strict category binding:
        # - only an exact configured menu digit can choose a ticket category;
        # - category names/keywords are NOT inferred from free-form text;
        # - "2. Доступ ..." in one message is NOT accepted as selection+details.
        explicit_menu_choice = menu_choice if re.fullmatch(r"[1-9]", menu_choice) else ""
        typed_category_choice = msg.typed_choice if msg.context_mode == MENU_CONTEXT else ""
        msg.chosen_category = self.menu.configured_menu_category(explicit_menu_choice or typed_category_choice)
        msg.draft_finish_command = bool(msg.payload.get("draft_finish")) or all_information_confirmed(msg.text)
        return

    def _enter_main_menu(self, msg: InboundMessage) -> None:
        self.store.clear_conversation_draft(msg.contact_key)
        queue_contextual_tickets.reset_context(self.store, msg.contact_key, "")
        self._upgrade_menu_gate(msg)

    def _upgrade_menu_gate(self, msg: InboundMessage) -> None:
        self.store.set_conversation_context(
            msg.contact_key, MENU_CONTEXT, msg.active_ticket_id, bool(msg.active_ticket)
        )
        self._respond({
            "created": False,
            "awaiting_category": True,
            "main_menu": True,
            "force_menu": True,
            "reply": self.menu.main_menu_text(),
            "menu_options": self.menu.menu_payload(),
        })
        return

    def _select_active_ticket(self, msg: InboundMessage) -> None:
        active_rows = self.store.list_active_user_tickets(msg.chat_id, msg.phone, 10)
        try:
            selected_index = int(msg.typed_choice or "0") - 1
        except (TypeError, ValueError):
            selected_index = -1
        if 0 <= selected_index < len(active_rows):
            selected_ticket = active_rows[selected_index]
            selected_id = int(selected_ticket.get("id", 0) or 0)
            self.store.set_conversation_context(
                msg.contact_key, texts.ACTIVE_TICKET_FOLLOWUP_CONTEXT, selected_id, False
            )
            self._respond({
                "created": False,
                "active_ticket_selected": True,
                "ticket_id": selected_id,
                "reply": self.deps.tickets.active_ticket_selected_reply(selected_ticket),
            })
        else:
            self._respond({
                "created": False,
                "active_tickets": True,
                "reply": self.deps.tickets.active_tickets_reply(active_rows),
            })
        return

    def _save_ticket_followup(self, msg: InboundMessage) -> None:
        msg.attachment_name = normalize_message(str(msg.payload.get("attachment_name", "")))[:240]
        self.store.note_context_followup(msg.active_ticket_id, msg.text, msg.attachment_name)
        queue_contextual_tickets.record_followup(
            self.store, msg.contact_key, msg.active_ticket_id, chat_id=msg.chat_id,
            message_key=msg.external_id, text=msg.text, media_name=msg.attachment_name,
        )
        if msg.chat_id and msg.external_id:
            self.store.link_whatsapp_message_to_ticket(msg.chat_id, msg.external_id, msg.active_ticket_id)

    def _selected_ticket_followup(self, msg: InboundMessage) -> None:
        if not msg.active_ticket:
            active_rows = self.store.list_active_user_tickets(msg.chat_id, msg.phone, 10)
            self.store.set_conversation_context(msg.contact_key, texts.ACTIVE_TICKETS_CONTEXT, 0, False)
            self._respond({
                "created": False,
                "active_tickets": True,
                "reply": (
                    tr("Выбранная заявка уже закрыта или больше не активна.\n\n", "Таңдалған өтінім жабылған немесе енді белсенді емес.\n\n")
                    + self.deps.tickets.active_tickets_reply(active_rows)
                ),
            })
            return
        self._save_ticket_followup(msg)
        self._respond({
            "created": False,
            "linked": True,
            "ticket_id": msg.active_ticket_id,
            "reply": tr(
                f"Дополнительная информация добавлена к заявке №{msg.active_ticket_id}.",
                f"Қосымша ақпарат №{msg.active_ticket_id} өтінімге қосылды.",
            ),
        })
        return

    def _list_active_tickets(self, msg: InboundMessage) -> None:
        active_rows = self.store.list_active_user_tickets(msg.chat_id, msg.phone, 10)
        self.store.clear_conversation_draft(msg.contact_key)
        self.store.set_conversation_context(msg.contact_key, texts.ACTIVE_TICKETS_CONTEXT, msg.active_ticket_id, False)
        self._respond({
            "created": False,
            "active_tickets": True,
            "reply": self.deps.tickets.active_tickets_reply(active_rows),
        })
        return

    def _ignore_ack(self, msg: InboundMessage) -> None:
        self._respond(
            {
                "created": False,
                "ignored": True,
                "silent": True,
                "reason": "Короткий ответ без новой заявки",
            }
        )
        return

    def _legacy_error_report(self, msg: InboundMessage) -> None:
        description = msg.text or (
            f"Вложение: {msg.media_name or str(msg.payload.get('attachment_name', ''))}"
            if (msg.media_name or msg.payload.get("attachment_name")) else ""
        )
        if not description:
            self._respond({
                "created": False,
                "error_report": True,
                "awaiting_details": True,
                "reply": tr(
                    "Опишите ошибку текстом или приложите файл / скриншот.",
                    "Қатені мәтінмен сипаттаңыз немесе файл / скриншот тіркеңіз.",
                ),
            })
            return
        report_id = self.store.create_error_report(
            sender=msg.sender,
            phone=msg.phone,
            chat_id=msg.chat_id,
            external_id=msg.external_id,
            description=description,
            attachment_name=msg.media_name or normalize_message(str(msg.payload.get("attachment_name", "")))[:240],
        )
        self.store.clear_conversation_draft(msg.contact_key)
        self.store.set_conversation_context(msg.contact_key, MENU_CONTEXT, msg.active_ticket_id, bool(msg.active_ticket))
        self._respond({
            "created": False,
            "error_report_created": True,
            "error_report_id": report_id,
            "reply": tr(
                f"Репорт об ошибке №{report_id} сохранён. Он находится отдельно от заявок.\n"
                "Сотрудники смогут посмотреть его в разделе «Ошибки».\n\n"
                "Чтобы открыть меню снова, отправьте 0.",
                f"№{report_id} қате туралы хабарлама сақталды. Ол өтінімдерден бөлек орналасқан.\n"
                "Қызметкерлер оны «Қателер» бөлімінен көре алады.\n\n"
                "Мәзірді қайта ашу үшін 0 жіберіңіз.",
            ),
        })
        return

    def _legacy_support(self, msg: InboundMessage) -> None:
        self.store.set_conversation_context(
            msg.contact_key,
            "support",
            msg.active_ticket_id,
            msg.force_new_requested or bool(msg.active_ticket),
        )
        self._respond(
            {
                "created": False,
                "awaiting_details": True,
                "category": "support",
                "reply": self.menu.configured_category_prompt("support"),
            }
        )
        return

    def _choose_category(self, msg: InboundMessage) -> None:
        msg.strict_state = queue_dialog_flow.load(self.store, msg.contact_key)
        msg.strict_state.pop("buffer", None)
        msg.strict_state["strict_category"] = str(msg.chosen_category)
        msg.strict_state["strict_category_message"] = str(msg.external_id or "")
        queue_dialog_flow.save(self.store, msg.contact_key, msg.strict_state)

        self.store.clear_conversation_draft(msg.contact_key)
        queue_contextual_tickets.reset_context(self.store, msg.contact_key, msg.chosen_category)
        if self.menu.configured_category_action(msg.chosen_category) == texts.MENU_ACTION_INSTRUCTION:
            self.store.set_conversation_context(
                msg.contact_key,
                MENU_CONTEXT,
                msg.active_ticket_id,
                bool(msg.active_ticket),
            )
            self._respond(
                {
                    "created": False,
                    "instruction_only": True,
                    "category": msg.chosen_category,
                    "reply": self.menu.configured_category_prompt(msg.chosen_category),
                }
            )
            return
        if msg.chosen_category == "support":
            self.store.set_conversation_context(
                msg.contact_key,
                "support",
                msg.active_ticket_id,
                msg.force_new_requested or bool(msg.active_ticket),
            )
            self._respond(
                {
                    "created": False,
                    "awaiting_details": True,
                    "category": "support",
                    "reply": self.menu.configured_category_prompt("support"),
                }
            )
            return
        self.store.set_conversation_context(
            msg.contact_key,
            msg.chosen_category,
            msg.active_ticket_id,
            msg.force_new_requested or bool(msg.active_ticket),
        )

        # Strict mode intentionally does NOT process ticket details from
        # the same message as the category choice. The next user message is
        # the first allowed details message.

        self._respond(
            {
                "created": False,
                "awaiting_details": True,
                "category": msg.chosen_category,
                "draft_collecting": True,
                "reply": (
                    self.menu.configured_category_prompt(msg.chosen_category)
                    + "\n\n"
                    + tr(
                        "Можно отправить данные несколькими сообщениями. "
                        "Я сохраню их в одной заявке. Когда закончите — напишите «Готово».",
                        "Деректерді бірнеше хабарламамен жібере аласыз. "
                        "Оларды бір өтінімге жинаймын. Аяқтағанда «Дайын» деп жазыңыз.",
                    )
                ),
            }
        )
        return

    def _require_category(self, msg: InboundMessage) -> None:
        msg.response = self._category_required_reply()
        msg.response["menu_reminder"] = True
        self._respond(msg.response)
        return


    def _inbound_ticket_followup(self, msg: InboundMessage) -> bool:
        remembered_category = ""
        can_resume = False
        if msg.draft_finish_command and not msg.pending_category:
            finish_state = queue_dialog_flow.load(self.store, msg.contact_key)
            remembered_category = str(finish_state.get("strict_category", "") or "").strip()
            can_resume = bool(
                self._ticket_category_is_selectable(remembered_category)
                and queue_contextual_tickets.request_fragments(self.store, msg.contact_key)
            )
        action = choose_request_action(
            DialogSnapshot(msg.context_mode, msg.pending_category),
            has_active_ticket=bool(msg.active_ticket), can_resume_draft=can_resume,
        )
        if action is RequestAction.RESUME_DRAFT:
            self.store.set_conversation_context(
                msg.contact_key, remembered_category, msg.active_ticket_id, True,
            )
            msg.pending_category = remembered_category
            msg.context_mode = remembered_category
        elif action is RequestAction.APPEND_FOLLOWUP:
            self._save_ticket_followup(msg)
            self._respond(
                {
                    "created": False,
                    "linked": True,
                    "silent": True,
                    "ticket_id": msg.active_ticket_id,
                    "reason": "Уточнение к активной заявке",
                }
            )
            return True
        return False

    def _inbound_validate_draft_category(self, msg: InboundMessage) -> bool:
        # Only an explicitly chosen, still selectable category may collect details.
        msg.forced_category = msg.pending_category
        msg.strict_state = queue_dialog_flow.load(self.store, msg.contact_key)
        strict_selected_category = str(msg.strict_state.get("strict_category", "") or "").strip()

        if msg.forced_category and strict_selected_category != str(msg.forced_category):
            # Old/stale/inferred contexts are not trusted. Force a fresh explicit
            # menu selection before any ticket details can be collected.
            self.store.clear_conversation_draft(msg.contact_key)
            queue_contextual_tickets.reset_context(self.store, msg.contact_key, "")
            msg.strict_state.pop("strict_category", None)
            msg.strict_state.pop("strict_category_message", None)
            msg.strict_state.pop("buffer", None)
            queue_dialog_flow.save(self.store, msg.contact_key, msg.strict_state)
            self.store.set_conversation_context(
                msg.contact_key, MENU_CONTEXT, msg.active_ticket_id,
                msg.force_new_requested or bool(msg.active_ticket),
            )
            msg.response = self._category_required_reply()
            msg.response["strict_category_required"] = True
            self._respond(msg.response)
            return True

        if not self._ticket_category_is_selectable(msg.forced_category):
            self.store.clear_conversation_draft(msg.contact_key)
            queue_contextual_tickets.reset_context(self.store, msg.contact_key, "")
            self.store.set_conversation_context(
                msg.contact_key, MENU_CONTEXT, msg.active_ticket_id,
                msg.force_new_requested or bool(msg.active_ticket),
            )
            self._respond(self._category_required_reply())
            return True
        return False

    def _inbound_collect_draft(self, msg: InboundMessage) -> bool:
        # Collect messages; keep ready drafts open until explicit confirmation.
        msg.combined_request_text = msg.text
        msg.draft_finalized = False
        collect_text = msg.text

        finish_requested = msg.draft_finish_command
        contextual_decision = queue_contextual_tickets.collect_request(
            self.store, msg.contact_key, msg.forced_category, collect_text,
            attachment_name=normalize_message(str(msg.payload.get("attachment_name", "")))[:240],
            message_key=msg.external_id, chat_id=msg.chat_id, active_ticket_id=0,
            required_fields_override=self.menu.configured_category_required_fields(msg.forced_category),
        )

        raw_fragments = queue_contextual_tickets.request_fragments(self.store, msg.contact_key)
        draft_fragments = [
            normalize_message(str(fragment))
            for fragment in raw_fragments
            if normalize_message(str(fragment))
            and not all_information_confirmed(str(fragment))
        ]
        accumulated = normalize_message("\n".join(draft_fragments))
        if not accumulated:
            accumulated = remove_all_information_phrase(
                str(contextual_decision.get("request_text") or "")
            )

        if not finish_requested:
            # Even if all required fields are already present, keep the
            # request as a draft until the user explicitly says "Готово".
            missing = contextual_decision.get("missing", [])
            if contextual_decision.get("ready"):
                reply = tr(
                    "✅ Данные сохранены. Можете отправить ещё информацию, фото или документы. "
                    "Когда закончите — напишите «Готово».",
                    "✅ Деректер сақталды. Қосымша ақпарат, фото немесе құжат жібере аласыз. "
                    "Аяқтағанда «Дайын» деп жазыңыз.",
                )
            else:
                missing_reply = (
                    contextual_decision.get("reply")
                    or texts.v3_missing_fields_reply(missing)
                )
                reply = tr(
                    "✅ Данные сохранены.",
                    "✅ Деректер сақталды.",
                )
                if missing_reply:
                    reply += "\n" + str(missing_reply)
                reply += "\n" + tr(
                    "Можно продолжать отправлять данные отдельными сообщениями. "
                    "Когда закончите — «Готово».",
                    "Деректерді бөлек хабарламалармен жібере беріңіз. "
                    "Аяқтағанда — «Дайын».",
                )

            self._respond(
                {
                    "created": False,
                    "awaiting_details": True,
                    "draft_collecting": True,
                    "draft_ready": bool(contextual_decision.get("ready")),
                    "category": msg.forced_category,
                    "missing_fields": missing,
                    "contextual": True,
                    "confidence": contextual_decision.get("confidence", {}),
                    "clarify": bool(contextual_decision.get("clarify")),
                    "reply": reply,
                }
            )
            return True

        # "Готово / Вся инфа / Дайын" is the only normal-ticket commit.
        # If there is still some information in the draft, respect the
        # user's explicit confirmation and create one ticket from all
        # accumulated messages.
        if not accumulated:
            self._respond(
                {
                    "created": False,
                    "awaiting_details": True,
                    "draft_collecting": True,
                    "category": msg.forced_category,
                    "reply": tr(
                        "Сначала отправьте данные по обращению, затем напишите «Готово».",
                        "Алдымен өтінім бойынша деректерді жіберіңіз, содан кейін «Дайын» деп жазыңыз.",
                    ),
                }
            )
            return True

        msg.combined_request_text = accumulated
        msg.draft_finalized = True
        return False

    def _inbound_create_ticket(self, msg: InboundMessage) -> bool:
        # Create the ticket (or report a template error).
        if not msg.forced_category:
            # Первое сообщение обычного пользователя только предлагает открыть меню.
            # Контакты из админки отсекаются выше и автоответов не получают.
            self.store.set_conversation_context(
                msg.contact_key, MENU_GATE_CONTEXT, msg.active_ticket_id, bool(msg.active_ticket)
            )
            self._respond(
                {
                    "created": False,
                    "menu_gate": True,
                    "force_menu": True,
                    "reply": self.menu.start_menu_prompt(),
                }
            )
            return True

        result = process_incoming_message(
            msg.sender,
            msg.phone,
            msg.combined_request_text,
            str(msg.payload.get("attachment_name", "")),
            msg.chat_id,
            msg.external_id,
            msg.forced_category,
            category_labels=self.menu.config.category_labels,
        )
        if result["created"]:
            if not self._ticket_category_is_selectable(msg.forced_category):
                self.store.set_conversation_context(
                    msg.contact_key, MENU_CONTEXT, msg.active_ticket_id,
                    msg.force_new_requested or bool(msg.active_ticket),
                )
                self._respond(self._category_required_reply())
                return True
            fragments = queue_contextual_tickets.request_fragments(self.store, msg.contact_key)
            if fragments and not msg.draft_finalized:
                result["ticket"]["original_text"] = str(result["ticket"].get("original_text", "")) + "\n\nСообщения пользователя:\n" + "\n".join(fragments)
            result["ticket"]["assigned_to"] = self.deps.active_employee()
            self.contacts.apply_contact_profile_to_ticket(result["ticket"], msg.contact_profile)
            similar = self.store.find_similar_open_ticket(result["ticket"], hours=72)
            if similar:
                similar_id = int(similar.get("id", 0) or 0)
                queue_contextual_tickets.commit_ticket(
                    self.store, msg.contact_key, similar_id, msg.forced_category,
                    message_key=msg.external_id, source="WhatsApp · дубль",
                )
                if msg.chat_id and msg.external_id:
                    self.store.link_whatsapp_message_to_ticket(msg.chat_id, msg.external_id, similar_id)
                self.store.clear_conversation_draft(msg.contact_key)
                msg.strict_state = queue_dialog_flow.load(self.store, msg.contact_key)
                msg.strict_state.pop("strict_category", None)
                msg.strict_state.pop("strict_category_message", None)
                msg.strict_state.pop("buffer", None)
                queue_dialog_flow.save(self.store, msg.contact_key, msg.strict_state)
                self.store.set_conversation_context(
                    msg.contact_key, MENU_CONTEXT, similar_id, False
                )
                self._respond(
                    {
                        "created": False,
                        "duplicate_ticket": True,
                        "ticket_id": similar_id,
                        "reply": texts.v3_duplicate_reply(similar_id),
                    }
                )
                return True
            ticket_id = self.store.create_ticket(result["ticket"])
            queue_contextual_tickets.commit_ticket(
                self.store, msg.contact_key, ticket_id, msg.forced_category,
                message_key=msg.external_id, source="WhatsApp",
            )
            self.store.clear_conversation_draft(msg.contact_key)
            msg.strict_state = queue_dialog_flow.load(self.store, msg.contact_key)
            msg.strict_state.pop("strict_category", None)
            msg.strict_state.pop("strict_category_message", None)
            msg.strict_state.pop("buffer", None)
            queue_dialog_flow.save(self.store, msg.contact_key, msg.strict_state)
            self.store.set_conversation_context(msg.contact_key, "", ticket_id)
            if msg.chat_id and msg.external_id:
                self.store.link_whatsapp_message_to_ticket(msg.chat_id, msg.external_id, ticket_id)
            # Пользователи иногда отправляют скриншот до финального текста заявки.
            # С 1.00.6.5 автоматически привязываются только изображения, которые
            # уже попали в контекст ТЕКУЩЕЙ заявки после выбора её категории.
            # Просто недавние изображения из этого чата больше не подтягиваются.
            try:
                ticket_message_timestamp = max(0, int(msg.payload.get("message_timestamp", 0) or 0))
            except (TypeError, ValueError):
                ticket_message_timestamp = 0
            pre_ticket_images = self.store.link_recent_whatsapp_images_to_ticket(
                msg.chat_id,
                ticket_id,
                before_timestamp=ticket_message_timestamp,
                lookback_seconds=self.deps.image_lookback_seconds,
                limit=5,
            ) if msg.chat_id else []
            self._respond(
                {
                    "created": True,
                    "ticket_id": ticket_id,
                    "pre_ticket_images": len(pre_ticket_images),
                    "category": result["ticket"]["category"],
                    "title": result["ticket"]["title"],
                    "reply": (
                        self.deps.tickets.support_question_accepted_reply(ticket_id)
                        if result["ticket"]["category"] == "support"
                        else self.deps.tickets.ticket_accepted_reply(ticket_id)
                    ),
                },
                HTTPStatus.CREATED,
            )
            return True
        self.store.set_conversation_context(
            msg.contact_key,
            str(msg.forced_category or msg.pending_category or ""),
            msg.active_ticket_id,
            msg.force_new_requested,
        )
        error_id = self.store.add_template_error(msg.sender, msg.phone, result["missing"], result["reply"])
        self._respond(
            {
                "created": False,
                "template_error_id": error_id,
                "missing": result["missing"],
                "reply": result["reply"],
            }
        )
        return False
