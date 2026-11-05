"""What the chat, group and notification APIs show when stored and live data disagree.

Tests go through HTTP and the store only, so they describe behaviour, not the module layout.
"""
import copy
import itertools
import json
import time
import unittest
from datetime import datetime, timedelta, timezone

from tests import support
from tests.support import WhatsAppUser, admin_cookie, get

app = support.app
_numbers = itertools.count(1)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def new_chat() -> str:
    return f"7701{next(_numbers):07d}@c.us"


def new_group() -> str:
    return f"120363{next(_numbers):012d}@g.us"


def seconds_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class ChatStateTestCase(unittest.TestCase):
    def setUp(self):
        self.cookie = admin_cookie()
        with app.CHAT_LOCK:
            saved = copy.deepcopy(app.CHAT_STATE)
        self.addCleanup(self.restore, saved)
        self.clear_caches()
        self.now = int(time.time())

    @staticmethod
    def restore(saved):
        with app.CHAT_LOCK:
            app.CHAT_STATE.clear()
            app.CHAT_STATE.update(saved)

    @staticmethod
    def clear_caches():
        with app.SNAPSHOT_CACHE_LOCK:
            app.SNAPSHOT_CACHE.clear()

    def live(self, **values):
        with app.CHAT_LOCK:
            app.CHAT_STATE.update(values)

    def state(self, chat=""):
        self.clear_caches()
        response = get(f"/api/chat-state?chat_id={chat}", self.cookie)
        self.assertEqual(response.status, 200, response.text)
        return response.json()

    def row(self, chat, requested=""):
        rows = [item for item in self.state(requested)["chats"] if item["id"] == chat]
        self.assertEqual(len(rows), 1, f"expected one row for {chat}: {rows}")
        return rows[0]

    def group_state(self, chat=""):
        self.clear_caches()
        response = get(f"/api/group-state?chat_id={chat}", self.cookie)
        self.assertEqual(response.status, 200, response.text)
        return response.json()

    def stored_chat(self, chat, name="", last_message="", timestamp=None):
        app.STORE.upsert_whatsapp_chats([{"id": chat, "name": name, "last_message": last_message,
                                          "timestamp": self.now if timestamp is None else timestamp}])

    def message(self, chat, key, body="Текст", timestamp=None, **extra):
        app.STORE.save_whatsapp_chat_messages(chat, [{
            "id": key, "body": body, "timestamp": self.now if timestamp is None else timestamp,
            "type": "chat", **extra}])


class ContactNameTests(ChatStateTestCase):
    def test_saved_contact_name_wins_over_the_whatsapp_name(self):
        chat = new_chat()
        self.stored_chat(chat, "Имя из WhatsApp", "Привет")
        app.STORE.save_whatsapp_contact("Сохранённое имя", "+" + chat.split("@")[0], chat)
        row = self.row(chat)
        self.assertEqual(row["name"], "Сохранённое имя")
        self.assertTrue(row["manual_contact"])

    def test_our_own_label_and_a_stranger_phone_are_never_a_name(self):
        for bad_name in ("Система", "+7 701 000 00 00"):
            with self.subTest(name=bad_name):
                chat = new_chat()
                self.stored_chat(chat, bad_name, "Привет")
                self.assertEqual(self.row(chat)["name"], "+" + chat.split("@")[0])

    def test_name_from_discovered_contacts_repairs_a_stored_row(self):
        chat = new_chat()
        self.stored_chat(chat, "Система", "Привет")
        self.live(discovered_contacts=[{"chat_id": chat, "name": "Настоящее Имя"}])
        self.assertEqual(self.row(chat)["name"], "Настоящее Имя")


class ChatListMergeTests(ChatStateTestCase):
    def test_contact_without_messages_is_listed(self):
        chat = new_chat()
        app.STORE.save_whatsapp_contact("Без переписки", "+" + chat.split("@")[0], chat)
        row = self.row(chat)
        self.assertEqual(row["name"], "Без переписки")
        self.assertEqual(row["unread_count"], 0)

    def test_newer_live_preview_replaces_the_stored_one(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Старое", self.now - 100)
        self.live(chats=[{"id": chat, "name": "Контакт", "last_message": "Новое", "timestamp": self.now}])
        self.assertEqual(self.row(chat)["last_message"], "Новое")

    def test_older_live_preview_does_not_overwrite_the_stored_one(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Из базы", self.now)
        self.live(chats=[{"id": chat, "name": "Контакт", "last_message": "Устаревшее", "timestamp": self.now - 100}])
        row = self.row(chat)
        self.assertEqual((row["last_message"], row["timestamp"]), ("Из базы", self.now))

    def test_same_person_seen_as_lid_and_phone_is_one_row(self):
        phone_chat = new_chat()
        lid = f"{next(_numbers)}@lid"
        self.stored_chat(phone_chat, "Контакт", "Старое", self.now - 100)
        self.live(chats=[{"id": lid, "phone": "+" + phone_chat.split("@")[0], "name": "Контакт",
                          "last_message": "Новое", "timestamp": self.now}])
        row = self.row(phone_chat)
        self.assertEqual(row["last_message"], "Новое")
        self.assertNotIn(lid, [item["id"] for item in self.state()["chats"]])

    def test_known_lid_alias_points_to_the_phone_chat(self):
        phone_chat = new_chat()
        lid = f"{next(_numbers)}@lid"
        self.stored_chat(phone_chat, "Контакт", "Привет")
        app.STORE.remember_whatsapp_chat_alias(lid, phone_chat)
        self.assertEqual(self.state(lid)["selected_chat_id"], phone_chat)

    def test_newest_deleted_message_replaces_the_preview(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Секрет", self.now - 5)
        self.message(chat, "true_chat_deleted", "Секрет", self.now, deleted=True)
        self.assertEqual(self.row(chat)["last_message"], "Сообщение удалено")

    def test_newer_message_beats_an_older_deletion(self):
        chat = new_chat()
        self.message(chat, "true_chat_gone", "Удалено", self.now - 50, deleted=True)
        self.message(chat, "true_chat_after", "После удаления", self.now)
        self.stored_chat(chat, "Контакт", "После удаления", self.now)
        self.assertEqual(self.row(chat)["last_message"], "После удаления")

    def test_live_message_newer_than_the_deletion_keeps_its_preview(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Секрет", self.now - 50)
        self.message(chat, "true_chat_old_delete", "Секрет", self.now - 50, deleted=True)
        self.live(chats=[{"id": chat, "name": "Контакт", "last_message": "Только что", "timestamp": self.now}])
        self.assertEqual(self.row(chat)["last_message"], "Только что")

    def test_favorite_chat_is_flagged(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        before = app.STORE.get_setting(app.CHAT_FAVORITES_SETTING, "[]")
        self.addCleanup(app.STORE.set_setting, app.CHAT_FAVORITES_SETTING, before)
        app.STORE.set_setting(app.CHAT_FAVORITES_SETTING, json.dumps([chat]))
        self.assertTrue(self.row(chat)["favorite"])

    def test_forward_targets_offer_chats_and_groups(self):
        chat, group = new_chat(), new_group()
        self.stored_chat(chat, "Контакт", "Привет")
        app.STORE.save_whatsapp_group("Рабочая группа", group)
        targets = {item["id"]: item["name"] for item in self.state()["forward_targets"]}
        self.assertEqual(targets[chat], "Контакт")
        self.assertEqual(targets[group], "Рабочая группа")


class SelectedChatTests(ChatStateTestCase):
    def test_repeated_observation_of_a_message_is_shown_once(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        self.message(chat, "false_chat_ABCDEF1234", "Привет")
        self.message(chat, "true_chat_ABCDEF9999", "Другое", self.now + 1, from_me=True)
        self.message(chat, "false_chat_ABCDEF1234", "Привет")
        bodies = [item["body"] for item in self.state(chat)["messages"]]
        self.assertEqual(bodies, ["Привет", "Другое"])

    def test_deleted_message_keeps_its_place_without_the_text(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        self.message(chat, "false_chat_DELETED0001", "Секрет", deleted=True)
        messages = self.state(chat)["messages"]
        self.assertEqual([(m["body"], m["deleted"]) for m in messages], [("", True)])

    def test_opening_a_chat_requests_it_from_the_connector_and_clears_live_unread(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        self.live(chats=[{"id": chat, "name": "Контакт", "timestamp": self.now, "unread_count": 4}])
        self.state(chat)
        with app.CHAT_LOCK:
            self.assertEqual(app.CHAT_STATE["requested_chat_id"], chat)
            self.assertEqual(app.CHAT_STATE["chats"][0]["unread_count"], 0)

    def test_presence_is_shown_only_while_fresh(self):
        chat, stale = new_chat(), new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        self.stored_chat(stale, "Контакт", "Привет")
        self.live(presence={
            chat: {"known": True, "online": True, "state": "typing", "updated_at": seconds_ago(5)},
            stale: {"known": True, "online": True, "updated_at": seconds_ago(120)},
        })
        self.assertEqual(self.state(chat)["presence"], {"known": True, "online": True, "state": "typing"})
        self.assertEqual(self.state(stale)["presence"], {"known": False, "online": False})

    def test_connector_counts_as_connected_only_with_a_fresh_heartbeat(self):
        self.live(connected=True, updated_at=seconds_ago(5))
        self.assertTrue(self.state()["connected"])
        self.live(connected=True, updated_at=seconds_ago(300))
        self.assertFalse(self.state()["connected"])

    def test_open_tickets_of_the_contact_are_in_the_context(self):
        user = WhatsAppUser()
        user.register_profile()
        user.say("1")
        user.say("Сломался принтер в кабинете")
        ticket_id = user.say("Готово")["ticket_id"]
        context = self.state(user.chat_id)["ui_context"]
        self.assertIn(ticket_id, [item["id"] for item in context["active_tickets"]])

    def test_cached_avatar_is_linked_on_the_row_and_the_profile(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        path = app.AVATARS.path(chat)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        state = self.state(chat)
        row = next(item for item in state["chats"] if item["id"] == chat)
        self.assertTrue(row["avatar_url"].startswith("/api/avatar?"))
        self.assertEqual(state["profile"]["profile_pic_url"], row["avatar_url"])


class ConversationLockTests(ChatStateTestCase):
    def user(self):
        username = f"chat_state_user_{next(_numbers)}"
        ok, message, _ = app.AUTH.create_user(username, "Занятый сотрудник", "Employee-Pass-2026!", role="employee")
        self.assertTrue(ok, message)
        return next(u for u in app.AUTH.list_users() if u["username"] == username)

    def test_free_chat_can_be_written(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        lock = self.state(chat)["conversation_lock"]
        self.assertEqual((lock["locked"], lock["can_write"]), (False, True))

    def test_chat_held_by_someone_else_is_read_only(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет")
        ok, _ = app.AUTH.claim_conversation(chat, "chat", self.user(), display_name="Занятый сотрудник")
        self.assertTrue(ok)
        lock = self.state(chat)["conversation_lock"]
        self.assertEqual((lock["locked"], lock["owned_by_me"], lock["can_write"]), (True, False, False))
        self.assertEqual(lock["owner"]["display_name"], "Занятый сотрудник")

    def test_no_chat_selected_means_nothing_to_write(self):
        self.assertFalse(self.state()["conversation_lock"]["can_write"])


class GroupStateTests(ChatStateTestCase):
    def group(self, name="Рабочая группа", last_message="Привет"):
        chat = new_group()
        app.STORE.save_whatsapp_group(name, chat)
        self.stored_chat(chat, name, last_message)
        return chat

    def test_requested_group_is_selected_with_its_mute_flag(self):
        chat = self.group()
        app.STORE.set_whatsapp_group_muted(chat, True)
        state = self.group_state(chat)
        self.assertEqual(state["selected_chat_id"], chat)
        self.assertTrue(state["selected_muted"])
        with app.CHAT_LOCK:
            self.assertEqual(app.CHAT_STATE["requested_group_id"], chat)

    def test_first_group_is_opened_when_none_is_requested(self):
        self.group()
        state = self.group_state()
        self.assertEqual(state["selected_chat_id"], state["chats"][0]["id"])

    def test_participants_carry_fresh_presence_and_avatars(self):
        chat = self.group()
        member, gone = new_chat(), new_chat()
        path = app.AVATARS.path(member)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        self.live(group_participants={chat: [{"resolved_id": member, "name": "Участник"}, {"resolved_id": gone, "name": "Другой"}]},
                  presence={member: {"known": True, "online": True, "updated_at": seconds_ago(5)},
                            gone: {"known": True, "online": True, "updated_at": seconds_ago(120)}})
        participants = {item["resolved_id"]: item for item in self.group_state(chat)["participants"]}
        self.assertTrue(participants[member]["avatar_url"].startswith("/api/avatar?"))
        self.assertTrue(participants[member]["presence"]["online"])
        self.assertEqual(participants[gone]["avatar_url"], "")
        self.assertFalse(participants[gone]["presence"]["online"])

    def test_mention_in_the_preview_uses_the_participant_name(self):
        chat = self.group(last_message="@77010009999 посмотрите заявку")
        self.live(group_participants={chat: [{"mention_id": "77010009999@c.us", "name": "Анна"}]})
        row = next(item for item in self.group_state(chat)["chats"] if item["id"] == chat)
        self.assertEqual(row["last_message"], "@Анна посмотрите заявку")

    def test_short_mention_id_uses_the_participant_name_too(self):
        # shorter than the ids that also trigger the lookup in the last saved message
        chat = self.group(last_message="@7701 посмотрите заявку")
        self.live(group_participants={chat: [{"mention_id": "7701@lid", "name": "Анна"}]})
        row = next(item for item in self.group_state(chat)["chats"] if item["id"] == chat)
        self.assertEqual(row["last_message"], "@Анна посмотрите заявку")

    def test_deleted_group_message_keeps_its_place_without_the_text(self):
        chat = self.group()
        self.message(chat, "false_group_deleted2", "Секрет", deleted=True, media_path="/tmp/secret.jpg")
        messages = self.group_state(chat)["messages"]
        self.assertEqual([(m["body"], m["deleted"], m.get("media_path", "")) for m in messages], [("", True, "")])

    def test_group_preview_of_a_deleted_message(self):
        chat = self.group(last_message="Секрет")
        self.message(chat, "true_group_deleted1", "Секрет", self.now + 1, deleted=True)
        row = next(item for item in self.group_state(chat)["chats"] if item["id"] == chat)
        self.assertEqual(row["last_message"], "Сообщение удалено")


class ConnectorStateTests(ChatStateTestCase):
    def health(self):
        return get("/health/connector").json()

    def test_stale_heartbeat_reads_as_offline(self):
        self.live(connected=True, connector_status="ready", updated_at=seconds_ago(300))
        state = self.health()
        self.assertEqual((state["connected"], state["status"]), (False, "offline"))

    def test_fresh_heartbeat_keeps_the_reported_status(self):
        self.live(connected=True, connector_status="ready", updated_at=seconds_ago(1))
        state = self.health()
        self.assertEqual((state["connected"], state["status"]), (True, "ready"))

    def test_qr_code_is_returned_only_while_waiting_for_a_scan(self):
        self.live(connected=False, connector_status="qr", qr_data_url="data:image/png;base64,AA", updated_at="")
        self.assertEqual(self.health()["qr_data_url"], "data:image/png;base64,AA")
        self.live(connector_status="ready", updated_at=seconds_ago(1))
        self.assertEqual(self.health()["qr_data_url"], "")


class NotificationTests(ChatStateTestCase):
    def notifications(self):
        self.clear_caches()
        response = get("/api/notifications", self.cookie)
        self.assertEqual(response.status, 200, response.text)
        return response.json()

    def event(self, chat):
        events = [item for item in self.notifications()["events"] if item.get("chat_id") == chat]
        self.assertTrue(events, f"no notification for {chat}")
        return events[0]

    def test_unread_message_is_announced_under_the_contact_name(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Привет", self.now + 100)
        app.STORE.save_whatsapp_contact("Сохранённое имя", "+" + chat.split("@")[0], chat)
        self.message(chat, "false_note_UNREAD0001", "Привет", self.now + 100)
        self.assertEqual(self.event(chat)["title"], "Сохранённое имя")

    def test_image_message_carries_a_preview_link(self):
        chat = new_chat()
        self.stored_chat(chat, "Контакт", "Фото", self.now + 200)
        self.message(chat, "false_note_IMAGE00001", "Фото", self.now + 200,
                     media_path="/tmp/photo.jpg", media_mime="image/jpeg", media_name="photo.jpg")
        event = self.event(chat)
        self.assertEqual(event["media_mime"], "image/jpeg")
        self.assertIn("/api/chat-media?", event["media_url"])

    def test_group_message_is_announced_under_the_sender(self):
        chat, sender = new_group(), new_chat()
        app.STORE.save_whatsapp_group("Рабочая группа", chat)
        self.stored_chat(chat, "Рабочая группа", "Привет", self.now + 300)
        self.live(discovered_contacts=[{"chat_id": sender, "name": "Пётр Иванов"}])
        self.message(chat, "false_note_GROUP00001", "Привет", self.now + 300,
                     sender="Пётр Иванов", sender_id=sender)
        self.assertTrue(self.event(chat)["title"].startswith("Пётр Иванов · "))

    def test_badge_total_is_the_sum_of_its_counters(self):
        state = self.notifications()
        self.assertEqual(state["total"], sum(state["counts"].values()))
        self.assertNotIn("reliability", state["counts"])

    def test_reconnected_connector_clears_the_persisted_alert_at_once(self):
        import queue_reliability
        queue_reliability._upsert_alert(app.STORE, "connector", "whatsapp", "critical", "WhatsApp-коннектор недоступен", "Нет связи", True)
        self.live(connected=True, connector_status="ready", updated_at=seconds_ago(1))
        ids = [item.get("id") for item in self.notifications()["events"]]
        self.assertNotIn("reliability:connector", ids)

    def test_active_alert_is_listed_with_a_link_to_the_system_page(self):
        import queue_reliability
        self.addCleanup(queue_reliability.resolve_persisted_alert, app.STORE, "chat-state-test")
        queue_reliability._upsert_alert(app.STORE, "chat-state-test", "disk", "warning", "Проверка", "Подробности", True)
        event = next(item for item in self.notifications()["events"] if item.get("id") == "reliability:chat-state-test")
        self.assertEqual((event["source"], event["href"]), ("Система", "/admin/system"))


if __name__ == "__main__":
    unittest.main()
