"""Адреса, которыми пользуется WhatsApp-коннектор, и действия с чатом с сайта (queue_whatsapp_handlers).

Запросы идут через HTTP, как их шлёт коннектор; проверяется то, что после них видно в базе,
в текущем состоянии чатов и в ответах сайта.
"""
import base64
import json
import unittest

from tests import support
from tests.support import get
from tests.test_chat_state import PNG, ChatStateTestCase, new_chat, new_group

app = support.app


def connector(path: str, payload: dict, token: str | None = None) -> support.Response:
    headers = {"Content-Type": "application/json", "X-Webhook-Token": app.WEBHOOK_TOKEN if token is None else token}
    return support.request("POST", path, json.dumps(payload).encode(), headers)


class ConnectorTestCase(ChatStateTestCase):
    def ok(self, path: str, payload: dict) -> dict:
        response = connector(path, payload)
        self.assertEqual(response.status, 200, f"{path}: {response.text[:300]}")
        return response.json()

    def site(self, path: str, payload: dict) -> support.Response:
        headers = {"Content-Type": "application/json", "Cookie": self.cookie, "X-CSRF-Token": support.csrf_token(self.cookie)}
        return support.request("POST", path, json.dumps(payload).encode(), headers)

    def live_state(self, key: str):
        with app.CHAT_LOCK:
            return app.CHAT_STATE.get(key)

    def sync_message(self, chat: str, key: str, body: str = "Текст", **extra) -> dict:
        return self.ok("/api/chat-messages-sync", {"chat_id": chat, "append": True, "messages": [
            {"id": key, "body": body, "timestamp": self.now, "type": "chat", **extra}]})

    def queued(self, message_id: int) -> dict:
        with app.STORE.connection() as connection:
            return dict(connection.execute("SELECT * FROM outbound_messages WHERE id = ?", (message_id,)).fetchone())


class AuthorizationTests(ConnectorTestCase):
    PATHS = (
        "/api/chat-list-sync", "/api/group-list-sync", "/api/presence-sync", "/api/group-participants-sync",
        "/api/group-message-identities-sync", "/api/contact-list-sync", "/api/contact-profile-sync",
        "/api/group-refresh-check", "/api/connector-state", "/api/contact-policy", "/api/call-permission",
        "/api/chat-messages-sync", "/api/chat-message-ack", "/api/chat-reaction-sync",
        "/api/whatsapp-action/claim", "/api/whatsapp-action/result", "/api/chat-control",
    )

    def test_a_wrong_token_is_refused_everywhere(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                response = connector(path, {"status": "ready", "chats": []}, token="wrong")
                self.assertEqual((response.status, response.json()), (401, {"error": "unauthorized"}))

    def test_a_body_that_is_not_an_object_is_refused(self):
        headers = {"Content-Type": "application/json", "X-Webhook-Token": app.WEBHOOK_TOKEN}
        for path in self.PATHS:
            with self.subTest(path=path):
                self.assertEqual(support.request("POST", path, b"[1]", headers).status, 400)


class ChatListTests(ConnectorTestCase):
    def test_chat_list_replaces_the_live_list_and_merges_repeated_rows(self):
        chat, other = new_chat(), new_chat()
        result = self.ok("/api/chat-list-sync", {"connected": True, "chats": [
            {"id": chat, "name": "Анна", "last_message": "Старое", "timestamp": self.now - 10, "unread_count": 3},
            {"id": chat, "name": "+7 701 000", "last_message": "Новое", "timestamp": self.now, "unread_count": 1},
            {"id": other, "name": "Борис", "timestamp": self.now - 5},
            {"id": "not a chat"}, "junk",
        ]})
        self.assertEqual(result, {"updated": True, "chats": 2})
        rows = {row["id"]: row for row in self.live_state("chats")}
        self.assertEqual(set(rows), {chat, other})
        self.assertEqual((rows[chat]["name"], rows[chat]["last_message"], rows[chat]["unread_count"]), ("Анна", "Новое", 3))
        self.assertTrue(self.live_state("connected"))
        self.assertEqual(self.row(chat)["last_message"], "Новое")

    def test_presence_is_kept_for_private_chats_only(self):
        chat = new_chat()
        result = self.ok("/api/presence-sync", {"items": [
            {"chat_id": chat, "known": True, "online": True, "state": "available"},
            {"chat_id": new_group(), "online": True}, "junk",
        ]})
        self.assertEqual(result, {"updated": True, "items": 1})
        presence = self.live_state("presence")[chat]
        self.assertEqual((presence["known"], presence["online"], presence["state"]), (True, True, "available"))

    def test_connector_state_drives_the_public_status(self):
        self.ok("/api/connector-state", {"status": "ready"})
        health = get("/health/connector").json()
        self.assertEqual((health["connected"], health["status"]), (True, "ready"))
        self.ok("/api/connector-state", {"status": "qr", "qr_data_url": "data:image/png;base64,AA"})
        self.assertEqual(self.live_state("qr_data_url"), "data:image/png;base64,AA")
        self.assertFalse(self.live_state("connected"))
        self.assertEqual(connector("/api/connector-state", {"status": "launched"}).json(), {"error": "invalid_state"})
        self.assertEqual(connector("/api/connector-state", {"status": "qr", "qr_data_url": "http://x"}).json(), {"error": "invalid_qr"})

    def test_chat_control_tells_the_connector_what_the_site_is_looking_at(self):
        chat, group = new_chat(), new_group()
        self.sync_message(chat, "false_control_1")
        self.sync_message(group, "false_control_2", sender="Участник группы")
        self.live(requested_chat_id=chat, requested_group_id="")
        control = self.ok("/api/chat-control", {})
        self.assertEqual((control["requested_chat_id"], control["quote_probe_chat_id"]), (chat, chat))
        self.assertEqual(control["quote_probe_message_ids"], ["false_control_1"])
        self.assertEqual(control["presence_ids"], [chat])
        member = new_chat()
        self.ok("/api/group-participants-sync", {"chat_id": group, "participants": [
            {"mention_id": member, "name": "Вера"}, {"mention_id": new_chat(), "is_me": True}]})
        self.live(requested_chat_id="", requested_group_id=group)
        control = self.ok("/api/chat-control", {})
        self.assertEqual(control["unresolved_group_message_ids"], ["false_control_2"])
        self.assertEqual(control["presence_ids"], [member])


class ContactTests(ConnectorTestCase):
    def test_contact_list_remembers_lid_aliases_and_merges_live_rows(self):
        phone_chat = new_chat()
        lid = f"{phone_chat.split('@')[0]}99@lid"
        self.live(chats=[{"id": lid, "name": "+7 701", "timestamp": self.now, "unread_count": 2},
                         {"id": phone_chat, "name": "", "timestamp": self.now - 5, "unread_count": 0}])
        result = self.ok("/api/contact-list-sync", {"contacts": [
            {"chat_id": phone_chat, "raw_id": lid, "phone": "+" + phone_chat.split("@")[0], "name": "Галина", "saved": True},
            {"phone": "+77015550000", "name": "Только телефон"}, {"name": "без адреса"}, "junk",
        ]})
        self.assertEqual((result["contacts"], result["collapsed_live_chats"]), (2, 2))
        self.assertEqual(app.STORE.canonical_whatsapp_chat_id(lid), phone_chat)
        rows = self.live_state("chats")
        self.assertEqual([(row["id"], row["name"]) for row in rows], [(phone_chat, "Галина")])
        self.assertEqual(self.live_state("discovered_contacts")[1]["chat_id"], "77015550000@c.us")
        self.assertEqual(connector("/api/contact-list-sync", {"contacts": "x"}).status, 400)

    def test_contact_profile_renames_the_live_chat(self):
        chat = new_chat()
        self.live(chats=[{"id": chat, "name": "+7 701", "timestamp": self.now}])
        self.ok("/api/contact-profile-sync", {"chat_id": chat, "name": "Дарья", "about": "На связи", "is_business": 1})
        self.assertEqual(self.live_state("chats")[0]["name"], "Дарья")
        profile = self.live_state("contact_profiles")[chat]
        self.assertEqual((profile["about"], profile["is_business"]), ("На связи", True))
        self.assertEqual(connector("/api/contact-profile-sync", {"name": "x"}).status, 400)

    def test_contact_policy_and_calls_follow_the_saved_contact(self):
        saved, stranger = new_chat(), new_chat()
        app.STORE.save_whatsapp_contact("Свой человек", "+" + saved.split("@")[0], saved)
        policy = self.ok("/api/contact-policy", {"chat_id": saved})
        self.assertEqual((policy["manual_contact"], policy["name"], policy["allow_calls"]), (True, "Свой человек", True))
        policy = self.ok("/api/contact-policy", {"chat_id": stranger})
        self.assertEqual((policy["manual_contact"], policy["bot_reset_token"]), (False, ""))
        self.assertEqual(self.ok("/api/call-permission", {"chat_id": saved})["allowed"], True)
        self.assertEqual(self.ok("/api/call-permission", {"chat_id": stranger}),
                         {"allowed": False, "manual_contact": False, "language": ""})


class GroupTests(ConnectorTestCase):
    def test_group_list_replaces_the_discovered_groups(self):
        group = new_group()
        result = self.ok("/api/group-list-sync", {"groups": [
            {"id": group, "name": "Смена", "participant_count": "7"}, {"id": new_chat(), "name": "не группа"}],
            "diagnostics": {"chats_ok": True, "chats_total": 5, "errors": ["e1"]}})
        self.assertEqual(result, {"updated": True, "groups": 1})
        status = json.loads(app.STORE.get_setting("group_sync_status", "{}"))
        self.assertEqual((status["groups"], status["chats_total"], status["errors"]), (1, 5, ["e1"]))
        self.assertEqual(connector("/api/group-list-sync", {"groups": "x"}).json(), {"error": "invalid_groups"})

    def test_group_refresh_request_is_passed_to_the_connector(self):
        app.STORE.set_setting("group_refresh_request", "now-1")
        self.assertEqual(self.ok("/api/group-refresh-check", {}), {"request": "now-1"})

    def test_participants_are_kept_per_group(self):
        group, member = new_group(), new_chat()
        result = self.ok("/api/group-participants-sync", {"chat_id": group, "participants": [
            {"mention_id": member, "phone": "+" + member.split("@")[0], "is_admin": True}, {"name": "без адреса"}]})
        self.assertEqual(result, {"updated": True, "participants": 1})
        row = self.live_state("group_participants")[group][0]
        self.assertEqual((row["mention_id"], row["name"], row["is_admin"]), (member, member.split("@")[0], True))
        self.assertEqual(connector("/api/group-participants-sync", {"chat_id": new_chat(), "participants": []}).status, 400)

    def test_late_sender_identity_repairs_a_group_message(self):
        group, member = new_group(), new_chat()
        self.sync_message(group, "false_identity_1", sender="Участник группы")
        result = self.ok("/api/group-message-identities-sync", {"chat_id": group, "identities": [
            {"id": "false_identity_1", "sender": "Елена", "sender_phone": "+" + member.split("@")[0], "sender_id": member},
            {"sender": "без id"}]})
        self.assertEqual(result["updated"], True)
        live = self.live_state("messages")[group][0]
        self.assertEqual((live["sender"], live["sender_id"]), ("Елена", member))
        self.assertEqual(app.STORE.get_whatsapp_message(group, "false_identity_1")["sender"], "Елена")


class MessageSyncTests(ConnectorTestCase):
    def test_only_appended_messages_are_saved(self):
        chat = new_chat()
        self.ok("/api/chat-messages-sync", {"chat_id": chat, "messages": [
            {"id": "false_history_1", "body": "Старая история", "timestamp": self.now - 100}]})
        self.assertIsNone(app.STORE.get_whatsapp_message(chat, "false_history_1"))
        self.assertEqual(self.live_state("messages")[chat][0]["body"], "Старая история")
        result = self.sync_message(chat, "false_live_1", "Новое сообщение", sender="Жанна", forwarded=True)
        self.assertEqual((result["updated"], result["messages"]), (True, 1))
        stored = app.STORE.get_whatsapp_message(chat, "false_live_1")
        self.assertEqual((stored["body"], stored["sender"], bool(stored["forwarded"])), ("Новое сообщение", "Жанна", True))
        self.assertEqual([item["id"] for item in self.live_state("messages")[chat]], ["false_history_1", "false_live_1"])
        self.assertEqual(connector("/api/chat-messages-sync", {"chat_id": "", "messages": []}).json(), {"error": "invalid_chat"})

    def test_a_lid_source_is_merged_into_the_phone_chat(self):
        chat = new_chat()
        lid = f"{chat.split('@')[0]}77@lid"
        self.ok("/api/chat-messages-sync", {"chat_id": chat, "source_chat_id": lid, "append": True, "messages": [
            {"id": "false_alias_1", "body": "С номера", "timestamp": self.now}]})
        self.sync_message(lid, "true_alias_2", "Ответ под @lid", from_me=True)
        self.assertEqual(app.STORE.canonical_whatsapp_chat_id(lid), chat)
        self.assertEqual(app.STORE.get_whatsapp_message(chat, "true_alias_2")["body"], "Ответ под @lid")

    def test_attachment_is_saved_and_served_back(self):
        chat = new_chat()
        self.sync_message(chat, "false_media_1", "Фото", type="image", media_mime="image/png", media_name="a.png",
                          media_base64=base64.b64encode(PNG).decode())
        response = get(f"/api/chat-media?chat_id={chat}&message_id=false_media_1", self.cookie)
        self.assertEqual((response.status, response.body, response.headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertEqual(get(f"/api/chat-media?chat_id={chat}&message_id=nope", self.cookie).status, 404)
        self.sync_message(chat, "false_media_2", "Без файла")
        self.assertEqual(get(f"/api/chat-media?chat_id={chat}&message_id=false_media_2", self.cookie).status, 404)

    def test_delete_for_everyone_scrubs_the_saved_message(self):
        chat = new_chat()
        self.sync_message(chat, "false_revoke_1", "Секрет")
        self.sync_message(chat, "false_revoke_1", "", deleted=True)
        stored = app.STORE.get_whatsapp_message(chat, "false_revoke_1")
        self.assertEqual((bool(stored["deleted"]), stored["body"]), (True, ""))
        self.assertEqual(get(f"/api/chat-media?chat_id={chat}&message_id=false_revoke_1", self.cookie).status, 404)

    def test_ack_and_reaction_update_the_saved_and_the_live_message(self):
        chat = new_chat()
        self.sync_message(chat, "true_ack_1", "Отправлено", from_me=True, ack=1)
        self.assertEqual(self.ok("/api/chat-message-ack", {"chat_id": chat, "message_id": "true_ack_1", "ack": 3}),
                         {"updated": True, "ack": 3})
        self.assertEqual(self.live_state("messages")[chat][0]["ack"], 3)
        self.assertEqual(connector("/api/chat-message-ack", {"chat_id": chat, "message_id": "true_ack_1", "ack": "x"}).status, 400)
        result = self.ok("/api/chat-reaction-sync", {"chat_id": chat, "message_id": "true_ack_1", "sender_id": chat, "emoji": "👍"})
        self.assertTrue(result["updated"])
        self.assertEqual(self.live_state("messages")[chat][0]["reactions"], result["reactions"])
        self.assertIn("👍", json.dumps(result["reactions"], ensure_ascii=False))
        self.assertEqual(connector("/api/chat-reaction-sync", {"chat_id": chat, "message_id": "true_ack_1"}).status, 400)

    def test_history_page_returns_saved_messages(self):
        chat = new_chat()
        self.sync_message(chat, "false_page_1", "Первое")
        self.sync_message(chat, "false_page_2", "Второе", timestamp=self.now + 1)
        page = get(f"/api/chat-history?chat_id={chat}", self.cookie).json()
        self.assertEqual([item["body"] for item in page["messages"]], ["Первое", "Второе"])
        self.assertFalse(page["has_more"])
        self.assertEqual(get("/api/chat-history?chat_id=", self.cookie).status, 400)


class SiteActionTests(ConnectorTestCase):
    def test_attachment_from_the_site_is_queued_for_the_connector(self):
        chat = new_chat()
        response = self.site("/chat-media-send", {"chat_id": chat, "message": "Подпись", "mimetype": "image/png",
                                                  "filename": "a.png", "media_base64": base64.b64encode(PNG).decode()})
        self.assertEqual(response.status, 200, response.text)
        queued = self.queued(response.json()["message_id"])
        self.assertEqual((queued["chat_id"], queued["body"], queued["media_name"], queued["media_mime"]),
                         (chat, "Подпись", "a.png", "image/png"))
        with open(queued["media_path"], "rb") as stream:
            self.assertEqual(stream.read(), PNG)
        self.assertEqual(self.site("/chat-media-send", {"chat_id": chat, "message": "нет файла"}).status, 400)
        self.assertEqual(self.site("/chat-media-send", {"chat_id": chat, "mimetype": "image/png", "filename": "a.png",
                                                        "media_base64": "не base64"}).status, 400)

    def test_sent_attachment_is_kept_for_the_history_by_its_queue_number(self):
        chat = new_chat()
        queued_id = self.site("/chat-media-send", {"chat_id": chat, "mimetype": "image/png", "filename": "kept.png",
                                                   "media_base64": base64.b64encode(PNG).decode()}).json()["message_id"]
        # the connector reports the sent message without the file, only with the queue number
        self.sync_message(chat, "true_kept_1", "", from_me=True, type="image", outbound_message_id=queued_id)
        stored = app.STORE.get_whatsapp_message(chat, "true_kept_1")
        self.assertEqual((stored["media_name"], stored["media_mime"]), ("kept.png", "image/png"))
        response = get(f"/api/chat-media?chat_id={chat}&message_id=true_kept_1", self.cookie)
        self.assertEqual((response.status, response.body), (200, PNG))

    def test_raw_upload_from_the_site_is_queued_too(self):
        chat = new_chat()
        headers = {"Content-Type": "image/png", "Cookie": self.cookie, "X-CSRF-Token": support.csrf_token(self.cookie)}
        response = support.request("POST", f"/chat-media-send-raw?chat_id={chat}&filename=b.png&message=Raw", PNG, headers)
        self.assertEqual(response.status, 200, response.text)
        queued = self.queued(response.json()["message_id"])
        self.assertEqual((queued["body"], queued["media_name"]), ("Raw", "b.png"))
        with open(queued["media_path"], "rb") as stream:
            self.assertEqual(stream.read(), PNG)
        self.assertEqual(support.request("POST", "/chat-media-send-raw?chat_id=&filename=b.png", PNG, headers).status, 400)

    def test_forward_is_queued_as_an_action_and_completed_by_the_connector(self):
        source, target = new_chat(), new_chat()
        self.sync_message(source, "false_forward_1", "Перешлите это")
        response = self.site("/chat-forward", {"source_chat_id": source, "target_chat_id": target,
                                               "message_ids": ["false_forward_1", "нет такого"]})
        self.assertEqual(response.status, 200, response.text)
        result = response.json()
        self.assertEqual((result["count"], result["action_message_ids"]), (1, ["false_forward_1"]))
        action = None
        while (claimed := self.ok("/api/whatsapp-action/claim", {})["action"]) is not None:
            action = claimed if claimed["id"] == result["action_ids"][0] else action
        self.assertEqual((action["action_type"], action["message_key"]), ("forward", "false_forward_1"))
        self.assertEqual(json.loads(action["body"])["target_chat_id"], target)
        done = self.ok("/api/whatsapp-action/result", {"action_id": action["id"], "status": "sent"})
        self.assertEqual(done, {"updated": True, "sent": True, "error": ""})
        self.assertEqual(connector("/api/whatsapp-action/result", {"action_id": action["id"], "status": "maybe"}).status, 400)
        self.assertEqual(self.site("/chat-forward", {"source_chat_id": source, "target_chat_id": target,
                                                     "message_ids": ["нет такого"]}).status, 400)

    def test_a_confirmed_delete_action_hides_the_live_message(self):
        chat = new_chat()
        self.sync_message(chat, "true_action_1", "Удалить меня", from_me=True)
        action_id = app.STORE.queue_whatsapp_action("delete", chat, "true_action_1", "", "Тест")
        self.ok("/api/whatsapp-action/result", {"action_id": action_id, "status": "sent"})
        live = self.live_state("messages")[chat][0]
        self.assertEqual((live["deleted"], live["body"]), (True, ""))


if __name__ == "__main__":
    unittest.main()
