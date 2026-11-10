"""POST-сценарии целиком: каждый тест проверяет сохранённый результат, а не только ответ.

Истёкшая сессия отвечает 303 на /login, и такой же статус означает успех формы, поэтому все
запросы здесь идут через `send` и `upload`, которые считают это ошибкой.
"""
import itertools
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import queue_productivity
import queue_reliability
import queue_workflow

from tests import support
from tests.support import WhatsAppUser, admin_cookie, get, post_form

app = support.app
_names = itertools.count(1)


def in_an_hour() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def employee_cookie() -> str:
    username = f"flow_employee_{next(_names)}"
    password = "Employee-Pass-2026!"
    ok, message, _ = app.AUTH.create_user(username, "Сотрудник", password, role="employee")
    if not ok:
        raise AssertionError(message)
    cookie = post_form("/login", {"username": username, "password": password, "next": "/"}).cookies()
    if not cookie:
        raise AssertionError("employee login failed")
    return cookie


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self.admin = admin_cookie()

    def send(self, path, fields, cookie=None):
        response = post_form(path, fields, cookie or self.admin)
        self.assertFalse(response.location.startswith("/login"),
                         f"{path} was never handled: the session is not signed in")
        return response

    def ticket(self, title="Заявка для проверки") -> int:
        created = self.send("/manual", {"title": f"{title} {next(_names)}", "comment": "Описание"})
        return int(created.location.split("id=")[1].split("&")[0])

    def revisions(self, *ticket_ids) -> str:
        return json.dumps({str(t): app.STORE.get_ticket(t)["revision"] for t in ticket_ids})

    def stored_message(self, chat_id: str, key: str, body: str = "Сообщение") -> None:
        with app.STORE.connection() as connection:
            connection.execute(
                "INSERT INTO whatsapp_chat_messages (message_key, chat_id, from_me, body, message_type,"
                " message_timestamp, created_at) VALUES (?, ?, 0, ?, 'chat', ?, '')",
                (key, chat_id, body, int(time.time())))


class AccessTests(FlowTestCase):
    MUTATIONS = ("/api/workflow-internal-note", "/api/workflow-ticket-quick", "/api/workflow-bulk",
                 "/api/reliability-schedule", "/api/ticket-reminder", "/api/contact-meta",
                 "/admin/workflow", "/admin/reliability", "/admin/backups", "/admin/shift")
    ADMIN_ONLY = ("/admin/workflow", "/admin/reliability", "/admin/backups", "/admin/shift")

    def test_anonymous_request_does_not_reach_the_handler(self):
        for path in self.MUTATIONS:
            with self.subTest(path=path):
                response = post_form(path, {"ticket_id": 1})
                self.assertIn(response.status, (302, 303, 401, 403))

    def test_anonymous_request_always_gets_its_answer(self):
        # closing with the body unread resets the connection before the client reads the reply
        for _ in range(300):
            self.assertIn(post_form("/admin/workflow", {"field": "x" * 2000}).status, (302, 303))

    def test_refusals_that_never_read_the_body_are_answered_too(self):
        refusals = (
            ("/register", {}, 403),  # self-registration is off
            ("/api/chat-read", {"Host": "evil.example.com"}, 421),
        )
        for path, headers, status in refusals:
            with self.subTest(path=path):
                for _ in range(300):
                    response = support.request("POST", path, b"x" * 2000, {"Content-Type": "application/octet-stream", **headers})
                    self.assertEqual(response.status, status)

    def test_missing_csrf_token_is_refused(self):
        for path in self.MUTATIONS:
            with self.subTest(path=path):
                response = post_form(path, {"ticket_id": 1}, self.admin, csrf=False)
                if path.startswith("/api/"):
                    self.assertEqual((response.status, response.json()["csrf"]), (403, False))
                else:  # a browser form is sent back with an explanation
                    self.assertEqual(response.status, 303)
                    self.assertTrue(response.location.startswith("/?notice="), response.location)

    def test_form_without_csrf_token_changes_nothing(self):
        before = queue_workflow.retention_settings(app.STORE)
        post_form("/admin/workflow", {"action": "retention", "messages": 7, "media": 7, "tickets": 7}, self.admin, csrf=False)
        self.assertEqual(queue_workflow.retention_settings(app.STORE), before)

    def test_employee_cannot_use_admin_forms(self):
        cookie = employee_cookie()
        before = queue_workflow.retention_settings(app.STORE)
        for path in self.ADMIN_ONLY:
            with self.subTest(path=path):
                self.assertEqual(self.send(path, {"action": "retention", "messages": 1}, cookie).status, 403)
        self.assertEqual(queue_workflow.retention_settings(app.STORE), before)


class WorkflowTests(FlowTestCase):
    def test_quick_edit_changes_the_ticket(self):
        ticket = self.ticket()
        response = self.send("/api/workflow-ticket-quick", {
            "ticket_id": ticket, "priority": "high", "revisions": self.revisions(ticket)})
        self.assertEqual(response.status, 200, response.text)
        self.assertTrue(response.json()["updated"])
        self.assertEqual(app.STORE.get_ticket(ticket)["priority"], "high")

    def test_stale_revision_is_a_conflict_and_changes_nothing(self):
        ticket = self.ticket()
        stale = self.revisions(ticket)
        app.STORE.update_priority(ticket, "low", "Другой сотрудник")
        response = self.send("/api/workflow-ticket-quick", {"ticket_id": ticket, "priority": "urgent", "revisions": stale})
        self.assertEqual(response.status, 409, response.text)
        self.assertEqual(app.STORE.get_ticket(ticket)["priority"], "low")

    def test_bulk_edit_applies_to_every_selected_ticket(self):
        first, second = self.ticket(), self.ticket()
        response = self.send("/api/workflow-bulk", {
            "ticket_ids": json.dumps([first, second]), "priority": "urgent", "revisions": self.revisions(first, second)})
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(response.json()["updated"], 2)
        self.assertEqual({app.STORE.get_ticket(t)["priority"] for t in (first, second)}, {"urgent"})

    def test_bulk_edit_rolls_back_entirely_when_one_ticket_is_stale(self):
        first, second = self.ticket(), self.ticket()
        revisions = json.loads(self.revisions(first, second))
        revisions[str(second)] = -1
        response = self.send("/api/workflow-bulk", {
            "ticket_ids": json.dumps([first, second]), "priority": "urgent", "revisions": json.dumps(revisions)})
        self.assertEqual(response.status, 409, response.text)
        self.assertEqual(app.STORE.get_ticket(first)["priority"], "normal")

    def test_internal_note_is_shown_in_ticket_details(self):
        ticket = self.ticket()
        self.assertEqual(self.send("/api/workflow-internal-note", {"ticket_id": ticket, "note": "Только для своих"}).status, 200)
        details = get(f"/api/workflow-ticket-details?ticket_id={ticket}", self.admin).json()
        self.assertEqual([note["note"] for note in details["notes"]], ["Только для своих"])

    def test_split_creates_a_second_ticket(self):
        ticket = self.ticket()
        response = self.send("/api/workflow-ticket-split", {
            "source_id": ticket, "title": "Отдельный вопрос", "summary": "Выделено", "revisions": self.revisions(ticket)})
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(app.STORE.get_ticket(response.json()["ticket_id"])["title"], "Отдельный вопрос")

    def test_retention_settings_are_saved(self):
        before = queue_workflow.retention_settings(app.STORE)
        self.addCleanup(queue_workflow.save_retention_settings, app.STORE, before["messages"], before["media"], before["tickets"])
        response = self.send("/admin/workflow", {"action": "retention", "messages": 41, "media": 42, "tickets": 43})
        self.assertTrue(response.location.startswith("/admin/workflow?notice="), response.location)
        self.assertEqual(queue_workflow.retention_settings(app.STORE), {"messages": 41, "media": 42, "tickets": 43})


class ReliabilityTests(FlowTestCase):
    def test_message_can_be_scheduled_and_cancelled(self):
        chat = WhatsAppUser().chat_id
        created = self.send("/api/reliability-schedule", {"chat_id": chat, "body": "Позже", "due_at": in_an_hour()})
        self.assertEqual(created.status, 200, created.text)
        schedule_id = created.json()["id"]
        listed = get(f"/api/reliability-scheduled?chat_id={chat}", self.admin).json()["items"]
        self.assertEqual([item["id"] for item in listed], [schedule_id])

        self.assertEqual(self.send("/api/reliability-scheduled-cancel", {"id": schedule_id}).status, 200)
        self.assertEqual(get(f"/api/reliability-scheduled?chat_id={chat}", self.admin).json()["items"], [])

    def test_schedule_in_the_past_is_refused(self):
        chat = WhatsAppUser().chat_id
        response = self.send("/api/reliability-schedule", {"chat_id": chat, "body": "Поздно", "due_at": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(response.status, 400)
        self.assertEqual(queue_reliability.scheduled_messages(app.STORE, chat), [])

    def test_maintenance_blocks_employees_but_not_administrators(self):
        self.addCleanup(queue_reliability.set_maintenance, app.STORE, False)
        self.send("/admin/reliability", {"action": "maintenance", "enabled": 1})
        self.assertTrue(queue_reliability.maintenance_active(app.STORE))

        ticket = self.ticket()
        blocked = self.send("/api/workflow-internal-note", {"ticket_id": ticket, "note": "Нельзя"}, employee_cookie())
        self.assertEqual(blocked.status, 423, blocked.text)
        self.assertEqual(self.send("/api/workflow-internal-note", {"ticket_id": ticket, "note": "Можно"}).status, 200)

    def test_profile_refresh_drops_the_cached_avatar_and_requests_the_chat(self):
        chat = WhatsAppUser().chat_id
        path = app.AVATARS.path(chat)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached")
        self.assertEqual(self.send("/api/reliability-profile-refresh", {"chat_id": chat}).status, 200)
        self.assertFalse(path.exists())
        self.assertEqual(app.CHAT_STATE["requested_chat_id"], chat)


class ProductivityTests(FlowTestCase):
    def test_reminder_is_listed_until_done(self):
        ticket = self.ticket()
        created = self.send("/api/ticket-reminder", {"ticket_id": ticket, "remind_at": in_an_hour(), "note": "Позвонить"})
        self.assertEqual(created.status, 200, created.text)
        reminders = get(f"/api/ticket-reminders?ticket_id={ticket}", self.admin).json()["reminders"]
        self.assertEqual([r["note"] for r in reminders], ["Позвонить"])

        self.assertEqual(self.send("/api/reminder-done", {"id": created.json()["id"]}).status, 200)
        self.assertEqual(get(f"/api/ticket-reminders?ticket_id={ticket}", self.admin).json()["reminders"], [])

    def test_contact_meta_can_block_auto_replies(self):
        chat = WhatsAppUser().chat_id
        saved = self.send("/api/contact-meta", {"chat_id": chat, "organization": "Филиал", "auto_reply_blocked": 1})
        self.assertEqual(saved.status, 200, saved.text)
        state = get(f"/api/productivity-state?chat_id={chat}", self.admin).json()
        self.assertEqual(state["contact"]["organization"], "Филиал")
        self.assertTrue(queue_productivity.auto_reply_blocked(app.STORE, chat))

    def test_ticket_can_be_created_from_a_message(self):
        chat = WhatsAppUser().chat_id
        self.stored_message(chat, "flow-message", "Сломался принтер")
        response = self.send("/api/message-ticket-create", {"chat_id": chat, "message_id": "flow-message", "category": "general"})
        self.assertEqual(response.status, 200, response.text)
        ticket = app.STORE.get_ticket(response.json()["ticket_id"])
        self.assertIn("Сломался принтер", ticket["original_text"] + ticket["summary"])
        self.assertEqual(app.STORE.get_whatsapp_message(chat, "flow-message")["ticket_id"], ticket["id"])

    def test_backup_created_from_the_page_is_listed(self):
        response = self.send("/admin/backups", {"action": "create"})
        self.assertIn("/admin/backups?notice=", response.location)
        self.assertEqual(get("/admin/backups", self.admin).status, 200)
        rows = get("/api/admin-page?page=backups", self.admin).json()["rows"]
        self.assertTrue(any(row["ok"] for row in rows))


class UploadTests(FlowTestCase):
    DATA = b"%PDF-1.4 test\n" * 20

    def upload(self, path, body, content_type="application/json", cookie=None):
        cookie = cookie or self.admin
        response = support.request("POST", path, body, {
            "Cookie": cookie, "Content-Type": content_type, "X-CSRF-Token": support.csrf_token(cookie)})
        self.assertFalse(response.location.startswith("/login"), f"{path} was never handled")
        return response

    def start(self, chat, cookie=None):
        response = self.upload("/upload/start", json.dumps({
            "chat_id": chat, "filename": "отчёт.pdf", "size": len(self.DATA),
            "mimetype": "application/pdf", "message": "Файл"}).encode(), cookie=cookie)
        self.assertEqual(response.status, 200, response.text)
        return response.json()["upload_id"]

    def chunk(self, upload_id, offset=0, data=None, cookie=None):
        return self.upload(f"/upload/chunk?id={upload_id}&offset={offset}", data or self.DATA,
                           "application/octet-stream", cookie)

    def test_uploaded_file_is_queued_for_the_connector(self):
        chat = WhatsAppUser().chat_id
        upload_id = self.start(chat)
        self.assertEqual(self.chunk(upload_id).json()["offset"], len(self.DATA))
        finished = self.upload("/upload/finish", json.dumps({"upload_id": upload_id}).encode())
        self.assertEqual(finished.status, 200, finished.text)

        with app.STORE.connection() as connection:
            row = connection.execute("SELECT * FROM outbound_messages WHERE id = ?", (finished.json()["message_id"],)).fetchone()
        self.assertEqual((row["chat_id"], row["media_name"], row["media_mime"], row["status"]),
                         (chat, "отчёт.pdf", "application/pdf", "pending"))
        self.assertEqual(Path(row["media_path"]).read_bytes(), self.DATA)

    def test_chunks_out_of_order_are_refused(self):
        upload_id = self.start(WhatsAppUser().chat_id)
        self.addCleanup(self.upload, "/upload/cancel", json.dumps({"upload_id": upload_id}).encode())
        # small enough to fit the file: only the order check can refuse it
        self.assertEqual(self.chunk(upload_id, offset=5, data=b"abc").status, 400)
        self.assertEqual(app.UPLOADS.sessions[upload_id]["offset"], 0)

    def test_incomplete_upload_cannot_be_finished(self):
        upload_id = self.start(WhatsAppUser().chat_id)
        self.addCleanup(self.upload, "/upload/cancel", json.dumps({"upload_id": upload_id}).encode())
        self.assertEqual(self.upload("/upload/finish", json.dumps({"upload_id": upload_id}).encode()).status, 400)

    def test_upload_of_another_account_is_forbidden(self):
        upload_id = self.start(WhatsAppUser().chat_id)
        self.addCleanup(self.upload, "/upload/cancel", json.dumps({"upload_id": upload_id}).encode())
        stranger = employee_cookie()
        self.assertEqual(self.chunk(upload_id, cookie=stranger).status, 403)

    def test_cancel_removes_the_partial_file(self):
        upload_id = self.start(WhatsAppUser().chat_id)
        partial = app.UPLOADS.sessions[upload_id]["path"]
        self.assertTrue(partial.exists())
        self.assertEqual(self.upload("/upload/cancel", json.dumps({"upload_id": upload_id}).encode()).status, 200)
        self.assertFalse(partial.exists())

    def test_unknown_operation_is_not_found(self):
        self.assertEqual(self.upload("/upload/nope", b"{}").status, 404)


class FeatureTests(FlowTestCase):
    def test_shift_announcement_is_saved(self):
        before = app.STORE.get_setting("shift_announcement_text", "")
        self.addCleanup(app.STORE.set_setting, "shift_announcement_text", before)
        response = self.send("/admin/shift", {"group": "", "text": "Сегодня работает {employee}"})
        self.assertEqual(response.location, "/admin/shift")
        self.assertEqual(app.STORE.get_setting("shift_announcement_text"), "Сегодня работает {employee}")

    def test_shift_announcement_refuses_an_unknown_group(self):
        self.assertEqual(self.send("/admin/shift", {"group": "1@g.us", "text": "x"}).status, 400)

    def test_message_window_finds_a_stored_message(self):
        chat = WhatsAppUser().chat_id
        self.stored_message(chat, "quoted-message", "Исходное сообщение")
        window = get(f"/api/message-window?chat_id={chat}&message_id=quoted-message", self.admin)
        self.assertEqual(window.status, 200, window.text)
        self.assertIn("Исходное сообщение", [m["body"] for m in window.json()["messages"]])
        self.assertEqual(get(f"/api/message-window?chat_id={chat}&message_id=missing", self.admin).status, 404)

    def test_saved_avatar_is_served(self):
        chat = WhatsAppUser().chat_id
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        path = app.AVATARS.path(chat)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        response = get(f"/api/avatar?chat_id={chat}", self.admin)
        self.assertEqual((response.status, response.headers["Content-Type"], response.body), (200, "image/png", png))


if __name__ == "__main__":
    unittest.main()
