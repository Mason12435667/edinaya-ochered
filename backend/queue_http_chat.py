"""Маршруты чатов: состояние и история для сайта, закладки, отправка и действия с сообщениями, адреса коннектора."""
from __future__ import annotations

import json
import re
import secrets
import threading
from http import HTTPStatus
from urllib.parse import parse_qs

import queue_contextual_tickets
import queue_dialog_flow
import queue_inbound_media
import queue_realtime
from queue_chat_views import groups_page_data
from queue_core import normalize_message, valid_chat_id, valid_conversation_id, valid_group_id
from queue_store_conversations import MENU_CONTEXT
from queue_work_context import active_employee, work_actor
from ticketing import TicketStore


# The bookmarks live in existing app_settings; no new SQLite schema or media changes.
_BOOKMARKS_LOCK = threading.RLock()
_BOOKMARKS_MAX = 300


def _bookmark_setting(user_id: int) -> str:
    return f"whatsapp_message_bookmarks_v1:{int(user_id)}"


def _load_message_bookmarks(store: TicketStore, user_id: int) -> list[dict[str, object]]:
    if user_id <= 0:
        return []
    raw = store.get_setting(_bookmark_setting(user_id), "[]")
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        values = []
    if not isinstance(values, list):
        return []
    result = []
    for entry in values[:_BOOKMARKS_MAX]:
        if isinstance(entry, dict) and entry.get("chat_id") and entry.get("message_id"):
            result.append(entry)
    return result


def merge_chat_alias_rows(store: TicketStore, state: dict[str, object]) -> None:
    """Collapse @lid/@c.us rows of one private chat in a chat-state snapshot, in place.

    Last-mile protection for old live duplicates: even if CHAT_STATE still holds both
    rows, the browser receives one canonical row, so visible duplicates disappear
    without waiting for another alias event.
    """
    raw_chats = state.get("chats", [])
    if isinstance(raw_chats, list):
        merged_chats: dict[str, dict[str, object]] = {}

        def _useful_chat_name_v27(value: object) -> bool:
            text = str(value or "").strip()
            if not text:
                return False
            if text.casefold() in {
                "пользователь whatsapp",
                "неизвестный отправитель",
                "direct",
                "рабочий whatsapp",
            }:
                return False
            compact = re.sub(r"[\s+().-]+", "", text)
            return not compact.isdigit()

        for raw_row in raw_chats:
            if not isinstance(raw_row, dict):
                continue
            raw_id = valid_chat_id(str(raw_row.get("id", "")))
            if not raw_id:
                continue
            canonical_id = store.canonical_whatsapp_chat_id(raw_id) or raw_id
            row = dict(raw_row)
            row["id"] = canonical_id

            previous = merged_chats.get(canonical_id)
            if previous is None:
                merged_chats[canonical_id] = row
                continue

            try:
                previous_ts = int(previous.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                previous_ts = 0
            try:
                row_ts = int(row.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                row_ts = 0

            newer, older = (
                (row, previous)
                if row_ts >= previous_ts
                else (previous, row)
            )
            combined = dict(older)
            combined.update(newer)
            combined["id"] = canonical_id

            newer_name = str(newer.get("name", "") or "").strip()
            older_name = str(older.get("name", "") or "").strip()
            if not _useful_chat_name_v27(newer_name) and _useful_chat_name_v27(older_name):
                combined["name"] = older_name

            for counter in ("unread_count", "mention_unread_count"):
                try:
                    combined[counter] = max(
                        int(previous.get(counter, 0) or 0),
                        int(row.get(counter, 0) or 0),
                    )
                except (TypeError, ValueError):
                    pass

            for flag in ("favorite", "muted", "needs_reply"):
                if flag in previous or flag in row:
                    combined[flag] = bool(previous.get(flag)) or bool(row.get(flag))

            for field in (
                "avatar_url",
                "profile_pic_url",
                "phone",
                "last_sender",
                "last_sender_avatar_url",
            ):
                if not combined.get(field):
                    combined[field] = previous.get(field) or row.get(field) or ""

            merged_chats[canonical_id] = combined

        deduped = list(merged_chats.values())
        deduped.sort(
            key=lambda item: (
                int(bool(item.get("favorite"))),
                int(item.get("timestamp", 0) or 0),
            ),
            reverse=True,
        )
        state["chats"] = deduped

        # Forward target menu must use the same identity list.
        raw_targets = state.get("forward_targets")
        if isinstance(raw_targets, list):
            target_map: dict[str, dict[str, object]] = {}
            for target in raw_targets:
                if not isinstance(target, dict):
                    continue
                raw_id = valid_conversation_id(str(target.get("id", "")))
                if not raw_id:
                    continue
                canonical_id = store.canonical_whatsapp_chat_id(raw_id) or raw_id
                row = dict(target)
                row["id"] = canonical_id
                previous = target_map.get(canonical_id)
                if previous is None:
                    target_map[canonical_id] = row
                    continue
                # Prefer the row with a useful name.
                old_name = str(previous.get("name", "") or "").strip()
                new_name = str(row.get("name", "") or "").strip()
                if _useful_chat_name_v27(new_name) or not _useful_chat_name_v27(old_name):
                    target_map[canonical_id] = {**previous, **row, "id": canonical_id}
            state["forward_targets"] = list(target_map.values())

    selected = valid_chat_id(str(state.get("selected_chat_id", "") or ""))
    if selected:
        state["selected_chat_id"] = store.canonical_whatsapp_chat_id(selected) or selected


class ChatRoutes:
    def claim_conversation_write(self, chat_id: str) -> bool:
        chat_id = valid_conversation_id(chat_id) or valid_group_id(chat_id)
        if not chat_id:
            self.json_response({"queued": False, "error": "Диалог не выбран"}, HTTPStatus.BAD_REQUEST)
            return False
        user = self.effective_user() or {}
        kind = "group" if chat_id.endswith("@g.us") else "chat"
        ok, lock = self.deps.auth.claim_conversation(chat_id, kind, user, display_name=work_actor())
        if ok:
            return True
        owner = lock or {}
        owner_name = str(owner.get("owner_display_name", "") or owner.get("owner_username", "") or "другой пользователь")
        self.json_response(
            {
                "queued": False,
                "error": "conversation_locked",
                "message": f"Диалог уже ведёт {owner_name}. Доступен только просмотр.",
                "conversation_lock": self.chat_views.conversation_lock_snapshot(chat_id),
            },
            HTTPStatus.LOCKED,
        )
        return False

    def _get_search(self, query) -> None:
        self.redirect("/whatsapp")

    def _get_bookmarks(self, query) -> None:
        self.redirect("/whatsapp?bookmarks=1")

    def _get_api_message_bookmarks(self, query) -> None:
        user_id = int((self.effective_user() or {}).get("id", 0) or 0)
        if user_id <= 0:
            self.json_response({"error": "Требуется вход"}, HTTPStatus.UNAUTHORIZED)
            return
        rows = _load_message_bookmarks(self.deps.store, user_id)
        self.json_response({"bookmarks": rows, "items": rows})

    def _get_api_global_search(self, query) -> None:
        self.json_response({"error": "Глобальный поиск пока недоступен"}, HTTPStatus.GONE)

    def _get_whatsapp(self, query) -> None:
        self.page_response("whatsapp")

    def _get_api_groups_page(self, query) -> None:
        self.json_response(groups_page_data(self.is_admin(), store=self.deps.store, avatars=self.deps.avatars))

    def _get_groups(self, query) -> None:
        self.page_response("groups")

    def _get_api_chat_state(self, query) -> None:
        state = self.chat_views.chat_state_snapshot(
            query.get("chat_id", [""])[0],
            query.get("read", ["0"])[0] == "1",
        )
        if isinstance(state, dict):
            merge_chat_alias_rows(self.deps.store, state)
        self.json_response(state)

    def _get_api_group_state(self, query) -> None:
        self.json_response(self.chat_views.group_state_snapshot(query.get("chat_id", [""])[0], query.get("read", ["0"])[0] == "1"))

    def _get_api_chat_history(self, query) -> None:
        self.handle_chat_history(query)

    def _get_api_chat_action_status(self, query) -> None:
        action_id = self.query_int(query, "id")
        action = self.deps.store.whatsapp_action_status(action_id)
        self.json_response({"found": bool(action), "action": action or {}})

    def _get_api_notifications(self, query) -> None:
        self.json_response(self.chat_views.notifications_snapshot())

    def _get_api_conversation_users(self, query) -> None:
        users=[]
        for item in self.deps.auth.list_users():
            if not item.get("enabled"): continue
            users.append({"id":int(item.get("id",0) or 0),"username":str(item.get("username","") or ""),"display_name":str(item.get("display_name","") or ""),"role":str(item.get("role","") or ""),"employee_name":str(item.get("employee_name","") or "")})
        current=self.effective_user() or {}
        self.json_response({
            "users": users,
            "current_user_id": int(current.get("id", 0) or 0),
            "is_admin": self.is_admin(),
            "active_shift_employee": str(self.deps.store.get_setting("active_employee", active_employee()) or active_employee()),
        })

    def _get_api_chat_media(self, query) -> None:
        self.handle_chat_media(query)

    def _get_connector_state(self, query) -> None:
        self.json_response(self.chat_views.connector.snapshot())

    def _get_health_connector(self, query) -> None:
        # Open without signing in: whoever scans the QR code links a phone to the work WhatsApp.
        self.json_response({**self.chat_views.connector.snapshot(), "qr_data_url": ""})

    def _post_api_global_search(self, parsed) -> None:
        # POST /api/global-search
        self.json_response({"updated": False, "error": "Глобальный поиск пока недоступен"}, HTTPStatus.GONE)

    def _post_api_message_bookmarks(self, parsed) -> None:
        # POST /api/message-bookmarks, /api/bookmarks
        form = self.read_form()
        user_id = int((self.effective_user() or {}).get("id", 0) or 0)
        chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
        message_id = str(form.get("message_id", "") or "").strip()
        if user_id <= 0:
            self.json_response({"updated": False, "error": "Требуется вход"}, HTTPStatus.UNAUTHORIZED)
            return
        if not chat_id or not 1 <= len(message_id) <= 240 or any(ord(c) < 32 for c in message_id):
            self.json_response({"updated": False, "error": "Некорректное сообщение"}, HTTPStatus.BAD_REQUEST)
            return
        if not chat_id.endswith("@g.us"):
            chat_id = self.deps.store.canonical_whatsapp_chat_id(chat_id) or chat_id
        is_favorite = str(form.get("favorite", "1")).strip().lower() in {"1", "true", "yes", "on"}
        with _BOOKMARKS_LOCK:
            items = _load_message_bookmarks(self.deps.store, user_id)
            items = [item for item in items if not (str(item.get("chat_id")) == chat_id and str(item.get("message_id")) == message_id)]
            if is_favorite:
                preview = str(form.get("preview", "") or "").replace("\x00", "").strip()[:280]
                name = str(form.get("chat_name", "") or "").replace("\x00", "").strip()[:100]
                sender = str(form.get("sender", "") or "").replace("\x00", "").strip()[:100]
                try:
                    timestamp = max(0, int(form.get("timestamp", "0") or 0))
                except ValueError:
                    timestamp = 0
                items.insert(0, {"chat_id": chat_id, "message_id": message_id, "chat_name": name,
                                 "preview": preview, "sender": sender, "timestamp": timestamp})
            items = items[:_BOOKMARKS_MAX]
            self.deps.store.set_setting(_bookmark_setting(user_id), json.dumps(items, ensure_ascii=False))
        self.json_response({"updated": True, "favorite": is_favorite, "bookmarks": items})

    def _post_api_media_stage(self, parsed) -> None:
        # POST /api/media-stage
        payload = self.read_authorized_json(800_000)
        if payload is not None:
            try: self.json_response(queue_inbound_media.receive(self.deps.media_dir, payload))
            except (ValueError, OSError) as error: self.json_response({"error": str(error)[:180]}, 400)

    def _post_api_whatsapp(self, parsed) -> None:
        # POST /api/whatsapp
        self.handle_api_message()
        queue_realtime.notify("ticket")

    def _post_api_template_error_delivery(self, parsed) -> None:
        # POST /api/template-error-delivery
        self.handle_template_error_delivery()

    def _post_api_outbound_claim(self, parsed) -> None:
        # POST /api/outbound/claim
        self.handle_outbound_claim()

    def _post_inbound_recovery_reply(self, parsed) -> None:
        payload = self.read_authorized_json(120_000)
        if payload is not None:
            result = self.outbound_service.recover_reply(payload)
            self.json_response(result.payload, result.status)

    def _post_api_outbound_heartbeat(self, parsed) -> None:
        payload = self.read_authorized_json()
        if payload is not None:
            result = self.outbound_service.heartbeat(payload, begin=parsed.path.endswith('/begin'))
            self.json_response(result.payload, result.status)

    def _post_api_outbound_result(self, parsed) -> None:
        # POST /api/outbound/result
        self.handle_outbound_result()

    def _post_api_chat_list_sync(self, parsed) -> None:
        # POST /api/chat-list-sync
        self.handle_chat_list_sync()

    def _post_api_group_list_sync(self, parsed) -> None:
        # POST /api/group-list-sync
        self.handle_group_list_sync()

    def _post_api_presence_sync(self, parsed) -> None:
        # POST /api/presence-sync
        self.handle_presence_sync()

    def _post_api_group_participants_sync(self, parsed) -> None:
        # POST /api/group-participants-sync
        self.handle_group_participants_sync()

    def _post_api_group_message_identities_sync(self, parsed) -> None:
        # POST /api/group-message-identities-sync
        self.handle_group_message_identities_sync()

    def _post_api_contact_list_sync(self, parsed) -> None:
        # POST /api/contact-list-sync
        self.handle_contact_list_sync()

    def _post_api_avatar_sync(self, parsed) -> None:
        # POST /api/avatar-sync
        payload = self.read_authorized_json()
        if payload is not None:
            try:
                self.deps.avatars.save(payload)
                self.json_response({"updated":True})
            except (ValueError,TypeError,OSError):
                self.json_response({"error":"Некорректный аватар"},400)

    def _post_api_contact_profile_sync(self, parsed) -> None:
        # POST /api/contact-profile-sync
        self.handle_contact_profile_sync()

    def _post_api_group_refresh_check(self, parsed) -> None:
        # POST /api/group-refresh-check
        self.handle_group_refresh_check()

    def _post_api_connector_state(self, parsed) -> None:
        # POST /api/connector-state
        self.handle_connector_state()

    def _post_api_contact_policy(self, parsed) -> None:
        # POST /api/contact-policy
        self.handle_contact_policy()

    def _post_api_call_permission(self, parsed) -> None:
        # POST /api/call-permission
        self.handle_call_permission()

    def _post_api_chat_messages_sync(self, parsed) -> None:
        # POST /api/chat-messages-sync
        self.handle_chat_messages_sync()

    def _post_api_chat_message_ack(self, parsed) -> None:
        # POST /api/chat-message-ack
        self.handle_chat_message_ack()

    def _post_api_chat_reaction_sync(self, parsed) -> None:
        # POST /api/chat-reaction-sync
        self.handle_chat_reaction_sync()

    def _post_chat_media_send(self, parsed) -> None:
        # POST /chat-media-send
        self.handle_chat_media_send()

    def _post_chat_media_send_raw(self, parsed) -> None:
        # POST /chat-media-send-raw
        self.handle_chat_media_send_raw(parse_qs(parsed.query))

    def _post_chat_forward(self, parsed) -> None:
        # POST /chat-forward
        self.handle_chat_forward()

    def _post_api_whatsapp_action_claim(self, parsed) -> None:
        # POST /api/whatsapp-action/claim
        self.handle_whatsapp_action_claim()

    def _post_api_whatsapp_action_result(self, parsed) -> None:
        # POST /api/whatsapp-action/result
        self.handle_whatsapp_action_result()

    def _post_api_chat_control(self, parsed) -> None:
        # POST /api/chat-control
        self.handle_chat_control()

    def _post_conversation_lock(self, parsed, form) -> None:
        # POST /conversation-lock
        chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
        action = str(form.get("action", "claim") or "claim").strip().lower()
        user = self.effective_user() or {}
        if not chat_id:
            self.json_response({"updated": False, "error": "Диалог не выбран"}, HTTPStatus.BAD_REQUEST)
            return
        if action == "claim":
            ok, _lock = self.deps.auth.claim_conversation(
                chat_id, "group" if chat_id.endswith("@g.us") else "chat", user, display_name=work_actor()
            )
            state = self.chat_views.conversation_lock_snapshot(chat_id)
            if ok:
                self.deps.auth.record_audit(user, "conversation_claim", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=work_actor())
                queue_realtime.notify_outbound()
            self.json_response({"updated": ok, "conversation_lock": state}, HTTPStatus.OK if ok else HTTPStatus.LOCKED)
            return
        if action == "release":
            ok, message = self.deps.auth.release_conversation(
                chat_id, user, force=self.is_admin() and form.get("force", "") == "1"
            )
            if ok:
                self.deps.auth.record_audit(user, "conversation_release", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=message)
                queue_realtime.notify_outbound()
            self.json_response({"updated": ok, "message": message, "conversation_lock": self.chat_views.conversation_lock_snapshot(chat_id)}, HTTPStatus.OK if ok else HTTPStatus.LOCKED)
            return
        if action == "transfer":
            try:
                target_user_id = int(form.get("target_user_id", "0") or 0)
            except ValueError:
                target_user_id = 0
            target = self.deps.auth.get_user(target_user_id) if target_user_id > 0 else None
            target_name = ""
            if target:
                target_name = (
                    str(target.get("employee_name", "") or "").strip()
                    or str(target.get("display_name", "") or target.get("username", "")).strip()
                )
            ok, message, _lock = self.deps.auth.transfer_conversation(
                chat_id, user, target_user_id, target_display_name=target_name
            )
            if ok:
                self.deps.auth.record_audit(user, "conversation_transfer", object_type="group" if chat_id.endswith("@g.us") else "chat", object_id=chat_id, details=f"Передано user_id={target_user_id}")
                queue_realtime.notify_outbound()
            self.json_response({"updated": ok, "message": message, "conversation_lock": self.chat_views.conversation_lock_snapshot(chat_id)}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
            return
        self.json_response({"updated": False, "error": "Неизвестное действие блокировки"}, HTTPStatus.BAD_REQUEST)

    def _post_chat_favorite(self, parsed, form) -> None:
        # POST /chat-favorite
        chat_id = valid_conversation_id(form.get("chat_id", "")) or valid_group_id(form.get("chat_id", ""))
        if not chat_id:
            self.json_response({"updated": False, "error": "Чат не найден"}, HTTPStatus.BAD_REQUEST)
            return
        favorite = str(form.get("favorite", "0")).lower() in {"1", "true", "yes", "on"}
        updated, canonical = self.deps.set_chat_favorite(chat_id, favorite)
        self.json_response({"updated": updated, "chat_id": canonical, "favorite": favorite},
                           HTTPStatus.OK if updated else HTTPStatus.BAD_REQUEST)

    def _post_group_mute(self, parsed, form) -> None:
        # POST /group-mute
        chat_id = valid_group_id(form.get("chat_id", ""))
        muted = form.get("muted", "0") in {"1", "true", "yes", "on"}
        updated = self.deps.store.set_whatsapp_group_muted(chat_id, muted)
        if not updated:
            self.json_response({"updated": False, "error": "group_not_found"}, HTTPStatus.NOT_FOUND)
        else:
            self.json_response({"updated": True, "chat_id": chat_id, "muted": muted})

    def _post_chat_send(self, parsed, form) -> None:
        # POST /chat-send
        chat_id = valid_chat_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(chat_id):
            return
        message_id = self.deps.store.queue_direct_message(
            chat_id,
            form.get("message", ""),
            work_actor(),
            reply_to_key=normalize_message(form.get("reply_to", ""))[:160],
            request_id=form.get("request_id", ""),
        )
        if message_id:
            queue_realtime.notify_outbound()
        # Отправка сообщения сама по себе больше не меняет состояние
        # автоответчика. Его включает/выключает только сотрудник кнопкой
        # в карточке диалога.
        self.json_response(
            {"queued": bool(message_id), "message_id": message_id},
            HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
        )

    def _post_chat_mode(self, parsed, form) -> None:
        # POST /chat-mode
        raw_chat_id = valid_chat_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(raw_chat_id):
            return
        if not raw_chat_id:
            self.json_response({"updated": False, "error": "Выберите личный чат"}, HTTPStatus.BAD_REQUEST)
            return
        chat_id = self.deps.store.canonical_whatsapp_chat_id(raw_chat_id) or raw_chat_id
        if self.deps.store.manual_whatsapp_contact(chat_id, ""):
            self.json_response({
                "updated": False,
                "manual_contact": True,
                "error": "Контакт добавлен в админке — автоответчик для него отключён постоянно",
            }, HTTPStatus.CONFLICT)
            return

        action = normalize_message(form.get("action", "reset")).casefold()

        if action in {"disable", "pause", "off"}:
            actor = work_actor()
            self.deps.store.enable_manual_chat_mode(chat_id, actor)
            if raw_chat_id != chat_id:
                self.deps.store.enable_manual_chat_mode(raw_chat_id, actor)
            manual_mode = self.deps.store.manual_chat_mode(chat_id) or self.deps.store.manual_chat_mode(raw_chat_id) or {}
            queue_realtime.notify("chat")
            self.json_response({
                "updated": True,
                "auto_reply_enabled": False,
                "manual_mode": manual_mode,
                "message": "Автоответы для этого пользователя отключены. Входящие сообщения продолжат отображаться в системе.",
            })
            return

        if action in {"enable", "resume", "on"}:
            actor = work_actor()
            self.deps.store.disable_manual_chat_mode(chat_id, actor)
            if raw_chat_id != chat_id:
                self.deps.store.disable_manual_chat_mode(raw_chat_id, actor)
            queue_realtime.notify("chat")
            self.json_response({
                "updated": True,
                "auto_reply_enabled": True,
                "manual_mode": {},
                "message": "Автоответы для этого пользователя включены.",
            })
            return

        if action != "reset":
            self.json_response({"updated": False, "error": "Неизвестное действие"}, HTTPStatus.BAD_REQUEST)
            return

        # Existing per-user hard reset. It also guarantees that auto-replies
        # are enabled again after the reset.
        self.deps.store.disable_manual_chat_mode(chat_id, work_actor())
        if raw_chat_id != chat_id:
            self.deps.store.disable_manual_chat_mode(raw_chat_id, work_actor())
        try:
            with self.deps.store.connection() as db:
                db.execute(
                    "UPDATE contact_meta SET auto_reply_blocked = 0 WHERE chat_id IN (?, ?)",
                    (chat_id, raw_chat_id),
                )
                db.execute(
                    "DELETE FROM auto_reply_cooldowns WHERE contact_key IN (?, ?)",
                    (chat_id, raw_chat_id),
                )
        except Exception as exc:
            print(f"Bot hard-reset metadata warning for {chat_id}: {exc}", flush=True)

        for reset_key in dict.fromkeys((chat_id, raw_chat_id)):
            if not reset_key:
                continue
            self.deps.store.clear_conversation_draft(reset_key)
            try:
                queue_dialog_flow.save(self.deps.store, reset_key, {})
            except Exception as exc:
                print(f"Bot hard-reset dialog-flow warning for {reset_key}: {exc}", flush=True)
            try:
                queue_contextual_tickets.reset_context(self.deps.store, reset_key, "")
            except Exception as exc:
                print(f"Bot hard-reset contextual-flow warning for {reset_key}: {exc}", flush=True)
            self.deps.store.set_conversation_context(reset_key, MENU_CONTEXT, 0, False)

        reset_token = secrets.token_urlsafe(18)
        self.deps.store.set_setting(f"bot_hard_reset_v1:{chat_id}", reset_token)
        if raw_chat_id != chat_id:
            self.deps.store.set_setting(f"bot_hard_reset_v1:{raw_chat_id}", reset_token)

        queue_realtime.notify("chat")
        print(f"Bot hard reset completed for {chat_id}", flush=True)
        self.json_response({
            "updated": True,
            "bot_restarted": True,
            "hard_reset": True,
            "reset_token": reset_token,
            "auto_reply_enabled": True,
            "manual_mode": {},
            "message": "Бот полностью перезагружен для этого пользователя. Автоответы включены.",
        })

    def _post_group_send(self, parsed, form) -> None:
        # POST /group-send
        chat_id = valid_group_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(chat_id):
            return
        mention_ids = [
            item for item in form.get("mentions", "").split(",")
            if valid_conversation_id(item.strip()) or valid_group_id(item.strip())
        ]
        message_id = self.deps.store.queue_direct_message(
            chat_id,
            form.get("message", ""),
            work_actor(),
            mention_ids,
            reply_to_key=normalize_message(form.get("reply_to", ""))[:160],
            request_id=form.get("request_id", ""),
        )
        if message_id:
            queue_realtime.notify_outbound()
        self.json_response(
            {"queued": bool(message_id), "message_id": message_id},
            HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
        )

    def _post_chat_action(self, parsed, form) -> None:
        # POST /chat-action
        chat_id = valid_conversation_id(form.get("chat_id", ""))
        if not self.claim_conversation_write(chat_id):
            return
        message_key = normalize_message(form.get("message_id", ""))[:160]
        action = form.get("action", "")
        message = self.deps.store.get_whatsapp_message(chat_id, message_key) if chat_id and message_key else None
        if not message or bool(message.get("deleted")):
            self.json_response({"queued": False, "error": "Недоступное сообщение"}, HTTPStatus.BAD_REQUEST)
            return
        if action in {"edit", "delete"} and not bool(message.get("from_me")):
            self.json_response({"queued": False, "error": "Изменять и удалять можно только наши сообщения"}, HTTPStatus.BAD_REQUEST)
            return
        if action not in {"edit", "delete", "react"}:
            self.json_response({"queued": False, "error": "Неизвестное действие"}, HTTPStatus.BAD_REQUEST)
            return
        action_body = form.get("message", "")
        if action == "react":
            action_body = str(action_body or "")[:32]
            if action_body and action_body not in self.deps.reaction_emojis:
                self.json_response({"queued": False, "error": "Эта реакция пока не поддерживается"}, HTTPStatus.BAD_REQUEST)
                return
        if action == "delete":
            action_body = json.dumps(
                {
                    "body": str(message.get("body", "") or ""),
                    "timestamp": int(message.get("message_timestamp", 0) or 0),
                    "media_name": str(message.get("media_name", "") or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        action_id = self.deps.store.queue_whatsapp_action(action, chat_id, message_key, action_body, work_actor())
        if action_id:
            queue_realtime.notify_outbound()
        self.json_response(
            {"queued": bool(action_id), "action_id": action_id},
            HTTPStatus.OK if action_id else HTTPStatus.BAD_REQUEST,
        )
