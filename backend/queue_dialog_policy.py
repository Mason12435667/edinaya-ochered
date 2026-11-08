"""Правила выбора действия в диалоге, без хранения и побочных эффектов.

InboundService обрабатывает глобальные номера категорий и проверки языка и профиля до обращения
к этим правилам. Порядок ниже является частью протокола: ввод из списка заявок и дополнения к
выбранной заявке важнее обычных команд; в черновике без выбранной заявки 8 считается данными,
а не командой меню.
"""
from dataclasses import dataclass
from enum import Enum

import queue_bot_texts as texts
from queue_message_rules import is_acknowledgement, is_main_menu_command
from queue_store_conversations import MENU_CONTEXT, MENU_GATE_CONTEXT


class DialogAction(Enum):
    MAIN_MENU = "main_menu"
    UPGRADE_MENU = "upgrade_menu"
    SELECT_ACTIVE_TICKET = "select_active_ticket"
    SELECTED_TICKET_FOLLOWUP = "selected_ticket_followup"
    LIST_ACTIVE_TICKETS = "list_active_tickets"
    IGNORE_ACK = "ignore_ack"
    LEGACY_ERROR_REPORT = "legacy_error_report"
    LEGACY_SUPPORT = "legacy_support"
    CHOOSE_CATEGORY = "choose_category"
    REQUIRE_CATEGORY = "require_category"
    PROCESS_REQUEST = "process_request"


@dataclass(frozen=True)
class DialogSnapshot:
    mode: str = ""
    pending_category: str = ""


@dataclass(frozen=True)
class DialogInput:
    text: str
    typed_choice: str = ""
    chosen_category: str = ""
    finish_requested: bool = False


def choose_dialog_action(state: DialogSnapshot, message: DialogInput) -> DialogAction:
    if is_main_menu_command(message.text) or message.text.casefold().strip() in {"отмена", "бас тарту"}:
        return DialogAction.MAIN_MENU
    if state.mode == MENU_GATE_CONTEXT:
        return DialogAction.UPGRADE_MENU
    if state.mode == texts.ACTIVE_TICKETS_CONTEXT:
        return DialogAction.SELECT_ACTIVE_TICKET
    if state.mode == texts.ACTIVE_TICKET_FOLLOWUP_CONTEXT:
        return DialogAction.SELECTED_TICKET_FOLLOWUP
    if ((state.mode in {MENU_CONTEXT, ""}
         and message.typed_choice == str(texts.active_tickets_menu_number()))
            or texts.is_active_tickets_command(message.text)):
        return DialogAction.LIST_ACTIVE_TICKETS
    if is_acknowledgement(message.text) and not message.finish_requested:
        return DialogAction.IGNORE_ACK
    if state.mode == texts.ERROR_REPORT_CONTEXT:
        return DialogAction.LEGACY_ERROR_REPORT
    if state.mode == texts.SUPPORT_MODE_CONTEXT:
        return DialogAction.LEGACY_SUPPORT
    if message.chosen_category:
        return DialogAction.CHOOSE_CATEGORY
    if state.mode == MENU_CONTEXT:
        return DialogAction.REQUIRE_CATEGORY
    return DialogAction.PROCESS_REQUEST


class RequestAction(Enum):
    RESUME_DRAFT = "resume_draft"
    APPEND_FOLLOWUP = "append_followup"
    COLLECT_DRAFT = "collect_draft"


def choose_request_action(state: DialogSnapshot, *, has_active_ticket: bool,
                          can_resume_draft: bool) -> RequestAction:
    if not state.pending_category:
        if can_resume_draft:
            return RequestAction.RESUME_DRAFT
        if has_active_ticket:
            return RequestAction.APPEND_FOLLOWUP
    return RequestAction.COLLECT_DRAFT
