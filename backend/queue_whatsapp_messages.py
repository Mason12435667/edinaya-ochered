"""Адреса коннектора для сообщений чата: синхронизация истории, отметки доставки и реакции.

Подмешивается в queue_whatsapp_handlers.WhatsAppHandlerMixin, откуда берёт `whatsapp`
(WhatsAppDependencies) и методы чтения запроса.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus

import queue_inbound_media
import queue_realtime
import queue_uploads
import queue_workflow
from queue_core import (
    log_suppressed, normalize_message, normalize_phone, valid_chat_id, valid_conversation_id,
)


class WhatsAppMessageRoutes:
    def handle_chat_messages_sync(self) -> None:
        payload = self.read_authorized_json(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        source_chat_id = valid_conversation_id(str(payload.get("source_chat_id", "")))
        if source_chat_id.endswith("@lid") and chat_id.endswith("@c.us") and source_chat_id != chat_id:
            try:
                self.whatsapp.store.remember_whatsapp_chat_alias(source_chat_id, chat_id)
                collapsed_count_v26 = 0
                with self.whatsapp.chat_lock:
                    collapsed_v26, collapsed_count_v26 = self._collapse_live_whatsapp_aliases_v26(
                        self.whatsapp.chat_state.get("chats", [])
                    )
                    if collapsed_count_v26:
                        self.whatsapp.chat_state["chats"] = collapsed_v26
                        self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
                if collapsed_count_v26:
                    queue_realtime.notify("chat-list")
                    print(
                        f"WhatsApp live duplicate merged: {source_chat_id} -> {chat_id}; "
                        f"removed={collapsed_count_v26}",
                        flush=True,
                    )
            except Exception as exc:
                print(f"WhatsApp alias merge warning: {source_chat_id} -> {chat_id}: {exc}", flush=True)
        if chat_id:
            chat_id = self.whatsapp.store.canonical_whatsapp_chat_id(chat_id)
        raw_messages = payload.get("messages", [])
        if not chat_id or not isinstance(raw_messages, list):
            self.json_response({"error": "invalid_chat"}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, object]] = []
        local_media_recovered: list[str] = []
        deleted_payloads: list[tuple[str, str, int, bool]] = []
        for item in raw_messages[-80:]:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = int(item.get("timestamp", 0))
                ack = max(0, min(4, int(item.get("ack", 0) or 0)))
            except (ValueError, TypeError):
                timestamp, ack = 0, 0
            message_id = normalize_message(str(item.get("id", "")))[:160]
            is_deleted = bool(item.get("deleted"))
            if is_deleted and message_id:
                old_media_path = ""
                try:
                    stored_message = self.whatsapp.store.get_whatsapp_message(chat_id, message_id)
                    if stored_message:
                        old_media_path = str(stored_message.get("media_path", "") or "")
                except sqlite3.Error:
                    log_suppressed("queue_whatsapp_messages.handle_chat_messages_sync")
                    old_media_path = ""
                deleted_payloads.append((message_id, old_media_path, timestamp, bool(item.get("from_me"))))
            try:
                outbound_message_id = max(0, int(item.get("outbound_message_id", 0) or 0))
            except (ValueError, TypeError):
                outbound_message_id = 0
            media_mime = normalize_message(str(item.get("media_mime", "")))[:120]
            media_name = normalize_message(str(item.get("media_name", "")))[:180]
            media_path = ""
            media_base64 = str(item.get("media_base64", "") or "")
            if media_base64 and message_id and not is_deleted:
                media_path = self.whatsapp.save_media_payload(
                    chat_id, message_id, media_base64, media_mime, media_name
                )
            if item.get("media_receipt") and not is_deleted:
                media_path = queue_inbound_media.resolve(self.whatsapp.media_dir, chat_id, message_id, item["media_receipt"]) or media_path
            # Strongest path for our own UI sends: the connector supplies the
            # exact outbound queue id after WhatsApp has accepted the message.
            # preserve_sent_attachment is idempotent and returns the already
            # preserved /media/outbound-N.bin even after the temporary upload
            # was deleted from the outbound queue.
            if bool(item.get("from_me")) and outbound_message_id > 0 and not media_path and not is_deleted:
                preserved_by_queue = self.whatsapp.attachments.preserve(outbound_message_id)
                if preserved_by_queue:
                    media_path = str(preserved_by_queue.get("media_path", ""))
                    media_mime = media_mime or str(preserved_by_queue.get("media_mime", ""))
                    media_name = media_name or str(preserved_by_queue.get("media_name", ""))
            # A message sent from this web UI may be echoed by WhatsApp as a
            # caption-only observation.  Reattach the copy preserved when the
            # outbound queue completed, so our own system shows the same media
            # that is visible in WhatsApp.
            if bool(item.get("from_me")) and message_id and not media_path and not is_deleted:
                preserved_media = self.whatsapp.preserved_system_outgoing_media(
                    message_id,
                    str(item.get("body", "")),
                    str(item.get("quoted_message_key", "")),
                    str(item.get("type", "")),
                )
                if preserved_media:
                    media_path = str(preserved_media.get("media_path", ""))
                    media_mime = media_mime or str(preserved_media.get("media_mime", ""))
                    media_name = media_name or str(preserved_media.get("media_name", ""))
            if bool(item.get("from_me")) and message_id and not media_path and not is_deleted:
                recovered_media = self.whatsapp.reconcile_uncertain_system_outgoing_media(
                    chat_id,
                    message_id,
                    timestamp,
                    str(item.get("body", "")),
                    str(item.get("type", "")),
                    str(item.get("quoted_message_key", "")),
                )
                if not recovered_media:
                    recovered_media = queue_uploads.recover_clipboard_attachment(
                        self.whatsapp.store,
                        self.whatsapp.attachments,
                        chat_id,
                        message_id,
                        timestamp,
                        str(item.get("body", "")),
                        str(item.get("type", "")),
                    )
                if recovered_media:
                    media_path = str(recovered_media.get("media_path", ""))
                    media_mime = media_mime or str(recovered_media.get("media_mime", ""))
                    media_name = media_name or str(recovered_media.get("media_name", ""))
                    local_media_recovered.append(message_id)
            raw_mentions = item.get("mentions", [])
            if not isinstance(raw_mentions, list):
                raw_mentions = []
            cleaned.append(
                {
                    "id": message_id,
                    "from_me": bool(item.get("from_me")),
                    "sender": normalize_message(str(item.get("sender", "")))[:100],
                    "sender_phone": normalize_phone(str(item.get("sender_phone", ""))),
                    "sender_id": valid_chat_id(str(item.get("sender_id", ""))),
                    "body": normalize_message(str(item.get("body", "")))[:32000],
                    "type": normalize_message(str(item.get("type", "chat")))[:40],
                    "timestamp": timestamp,
                    "ack": ack,
                    "notify": bool(item.get("notify")),
                    "media_path": media_path,
                    "media_mime": media_mime,
                    "media_name": media_name,
                    "transcript": normalize_message(str(item.get("transcript", "")))[:12000] if media_mime.startswith("audio/") else "",
                    "mentions": raw_mentions[:100],
                    "quoted_message_key": normalize_message(str(item.get("quoted_message_key", "")))[:160],
                    "quoted_body": normalize_message(str(item.get("quoted_body", "")))[:1200],
                    "quoted_sender": normalize_message(str(item.get("quoted_sender", "")))[:100],
                    "forwarded": bool(item.get("forwarded")),
                    "edited": bool(item.get("edited")),
                    "edit_timestamp": max(0, int(item.get("edit_timestamp", 0) or 0)),
                    "deleted": is_deleted,
                    "reactions": item.get("reactions", {}) if isinstance(item.get("reactions", {}), (dict, list)) else {},
                }
            )
        # Reconcile the revoke event with the row that is already
        # visible in our history before touching live state or persisting a tombstone.
        matched_deleted_keys: set[str] = set()
        for deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me in deleted_payloads:
            resolved_key, matched_count = self.whatsapp.scrub_deleted_whatsapp_message(
                chat_id, deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me
            )
            if matched_count > 0 and resolved_key:
                matched_deleted_keys.add(resolved_key)
                for cleaned_item in cleaned:
                    if bool(cleaned_item.get("deleted")) and self.whatsapp.same_whatsapp_message_identity(
                        str(cleaned_item.get("id", "")), deleted_message_id
                    ):
                        cleaned_item["id"] = resolved_key

        with self.whatsapp.chat_lock:
            messages_by_chat = self.whatsapp.chat_state.get("messages")
            if not isinstance(messages_by_chat, dict):
                messages_by_chat = {}
                self.whatsapp.chat_state["messages"] = messages_by_chat
            if payload.get("append"):
                combined = [
                    *list(messages_by_chat.get(chat_id, [])),
                    *cleaned,
                ]
                unique: dict[str, dict[str, object]] = {}
                for item in combined:
                    message_id = str(item.get("id", ""))
                    key = message_id or (
                        f"{int(item.get('timestamp', 0) or 0)}:"
                        f"{int(bool(item.get('from_me')))}:{item.get('body', '')}"
                    )
                    unique[key] = item
                messages_by_chat[chat_id] = sorted(
                    unique.values(),
                    key=lambda item: int(item.get("timestamp", 0) or 0),
                )[-80:]
            else:
                messages_by_chat[chat_id] = cleaned
            if len(messages_by_chat) > 20:
                for old_chat_id in list(messages_by_chat)[:-20]:
                    messages_by_chat.pop(old_chat_id, None)
            self.whatsapp.chat_state["connected"] = True
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Only live append events are persisted. Historical messages from the
        # linked WhatsApp account are deliberately never imported.
        if payload.get("append"):
            # If the original DB row was found, it was already scrubbed in place.
            # Do not insert a second "Сообщение удалено" row under a different
            # @lid/@c.us alias. Only persist a tombstone when no original exists.
            persist_items = [
                item for item in cleaned
                if not bool(item.get("deleted")) or str(item.get("id", "")) not in matched_deleted_keys
            ]
            if persist_items:
                self.whatsapp.store.save_whatsapp_chat_messages(chat_id, persist_items)
            workflow_items = [item for item in cleaned if not bool(item.get("deleted"))]
            try:
                if workflow_items:
                    queue_workflow.auto_link_open_ticket(self.whatsapp.store, chat_id, workflow_items)
            except Exception as exc:
                print(f"Автопривязка сообщения к заявке пропущена: {exc}", flush=True)
            # Голосовые сохраняются и воспроизводятся как обычные медиа.
            # Задания на нейросетевую расшифровку больше не создаются.
            # Важно: исходящее сообщение, в том числе системный автоответ, не
            # должно автоматически выключать автоответчик. Состояние меняет
            # только сотрудник через /chat-mode. Старое поле
            # enable_manual_mode намеренно игнорируется для совместимости с
            # коннектором предыдущей версии.
        if payload.get("append") and cleaned:
            queue_realtime.notify("chat")
        self.json_response({
            "updated": True,
            "messages": len(cleaned),
            "local_media_recovered": list(dict.fromkeys(local_media_recovered)),
        })

    def handle_chat_message_ack(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        message_id = normalize_message(str(payload.get("message_id", "")))[:160]
        try:
            ack = max(0, min(4, int(payload.get("ack", 0))))
        except (ValueError, TypeError):
            ack = -1
        if not chat_id or not message_id or ack < 0:
            self.json_response({"error": "invalid_ack"}, HTTPStatus.BAD_REQUEST)
            return
        updated = self.whatsapp.store.update_whatsapp_message_ack(chat_id, message_id, ack)
        with self.whatsapp.chat_lock:
            messages_by_chat = self.whatsapp.chat_state.get("messages", {})
            if isinstance(messages_by_chat, dict):
                for messages in messages_by_chat.values():
                    if not isinstance(messages, list):
                        continue
                    for item in messages:
                        if isinstance(item, dict) and item.get("id") == message_id:
                            item["ack"] = max(int(item.get("ack", 0) or 0), ack)
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if updated:
            queue_realtime.notify("chat")
        self.json_response({"updated": updated, "ack": ack})

    def handle_chat_reaction_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        message_id = normalize_message(str(payload.get("message_id", "")))[:160]
        sender_id = normalize_message(str(payload.get("sender_id", "")))[:160]
        emoji = str(payload.get("emoji", "") or "")[:32]
        from_me = bool(payload.get("from_me"))
        if not chat_id or not message_id or (not from_me and not sender_id):
            self.json_response({"updated": False, "error": "invalid_reaction"}, HTTPStatus.BAD_REQUEST)
            return
        updated = self.whatsapp.store.update_whatsapp_message_reaction(
            chat_id, message_id, sender_id or "__me__", emoji, from_me=from_me
        )
        message = self.whatsapp.store.get_whatsapp_message(chat_id, message_id) if updated else None
        reactions = self.whatsapp.store._reaction_summary((message or {}).get("reactions_json", "{}"))
        with self.whatsapp.chat_lock:
            messages_by_chat = self.whatsapp.chat_state.get("messages", {})
            if isinstance(messages_by_chat, dict):
                live = messages_by_chat.get(chat_id, [])
                if isinstance(live, list):
                    for item in live:
                        if isinstance(item, dict) and str(item.get("id", "")) == message_id:
                            item["reactions"] = reactions
                            break
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if updated:
            queue_realtime.notify("chat")
        self.json_response({"updated": updated, "reactions": reactions})
