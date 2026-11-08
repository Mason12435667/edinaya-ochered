"""Приоритет команд можно проверить без базы и HTTP-сервера."""
import unittest

import queue_bot_texts as texts
from queue_dialog_policy import (
    DialogAction, DialogInput, DialogSnapshot, RequestAction,
    choose_dialog_action, choose_request_action,
)
from queue_store_conversations import MENU_CONTEXT, MENU_GATE_CONTEXT


class DialogPolicyTests(unittest.TestCase):
    def test_menu_and_cancel_leave_every_mode(self):
        modes = ("", MENU_CONTEXT, MENU_GATE_CONTEXT, "general",
                 texts.ACTIVE_TICKETS_CONTEXT, texts.ACTIVE_TICKET_FOLLOWUP_CONTEXT,
                 texts.ERROR_REPORT_CONTEXT, texts.SUPPORT_MODE_CONTEXT)
        for mode in modes:
            for text in ("0", "меню", "отмена", "бас тарту"):
                with self.subTest(mode=mode, text=text):
                    self.assertIs(choose_dialog_action(DialogSnapshot(mode), DialogInput(text)),
                                  DialogAction.MAIN_MENU)

    def test_eight_is_data_in_a_draft_and_command_after_creation(self):
        message = DialogInput("8", typed_choice="8")
        self.assertIs(choose_dialog_action(DialogSnapshot("general", "general"), message),
                      DialogAction.PROCESS_REQUEST)
        for mode in ("", MENU_CONTEXT):
            self.assertIs(choose_dialog_action(DialogSnapshot(mode), message),
                          DialogAction.LIST_ACTIVE_TICKETS)

    def test_ticket_submenus_consume_input_before_ordinary_commands(self):
        for text in ("8", "спасибо", "мои заявки"):
            self.assertIs(choose_dialog_action(DialogSnapshot(texts.ACTIVE_TICKETS_CONTEXT),
                                               DialogInput(text, typed_choice="8")),
                          DialogAction.SELECT_ACTIVE_TICKET)
            self.assertIs(choose_dialog_action(DialogSnapshot(texts.ACTIVE_TICKET_FOLLOWUP_CONTEXT),
                                               DialogInput(text)),
                          DialogAction.SELECTED_TICKET_FOLLOWUP)

    def test_finish_flag_takes_priority_over_acknowledgement(self):
        state = DialogSnapshot("general", "general")
        self.assertIs(choose_dialog_action(state, DialogInput("ок")), DialogAction.IGNORE_ACK)
        self.assertIs(choose_dialog_action(state, DialogInput("ок", finish_requested=True)),
                      DialogAction.PROCESS_REQUEST)

    def test_menu_requires_an_explicit_category(self):
        state = DialogSnapshot(MENU_CONTEXT)
        self.assertIs(choose_dialog_action(state, DialogInput("Не печатает принтер")),
                      DialogAction.REQUIRE_CATEGORY)
        self.assertIs(choose_dialog_action(state, DialogInput("1", "1", "general")),
                      DialogAction.CHOOSE_CATEGORY)

    def test_legacy_sessions_keep_their_routes(self):
        for mode, expected in ((MENU_GATE_CONTEXT, DialogAction.UPGRADE_MENU),
                               (texts.ERROR_REPORT_CONTEXT, DialogAction.LEGACY_ERROR_REPORT),
                               (texts.SUPPORT_MODE_CONTEXT, DialogAction.LEGACY_SUPPORT)):
            self.assertIs(choose_dialog_action(DialogSnapshot(mode), DialogInput("Описание ошибки")),
                          expected)

    def test_draft_and_resume_take_priority_over_previous_ticket(self):
        self.assertIs(choose_request_action(DialogSnapshot("general", "general"),
                      has_active_ticket=True, can_resume_draft=True), RequestAction.COLLECT_DRAFT)
        self.assertIs(choose_request_action(DialogSnapshot(),
                      has_active_ticket=True, can_resume_draft=True), RequestAction.RESUME_DRAFT)
        self.assertIs(choose_request_action(DialogSnapshot(),
                      has_active_ticket=True, can_resume_draft=False), RequestAction.APPEND_FOLLOWUP)
        self.assertIs(choose_request_action(DialogSnapshot(),
                      has_active_ticket=False, can_resume_draft=False), RequestAction.COLLECT_DRAFT)
