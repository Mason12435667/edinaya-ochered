"""Connector delivery operations with explicit persistence and event dependencies."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Protocol

import queue_productivity
from queue_attachment_archive import AttachmentArchive
from queue_core import normalize_message, normalize_phone, valid_chat_id, valid_conversation_id
from queue_service_result import ServiceResult
from ticketing import TicketStore


class OutboundEvents(Protocol):
    def outbound_revision(self) -> int: ...
    def wait_for_outbound(self, revision: int, timeout: float) -> int: ...
    def notify(self, kind: str = "state") -> int: ...
    def notify_outbound(self) -> int: ...


class OutboundService:
    def __init__(self, store: TicketStore, attachments: AttachmentArchive, events: OutboundEvents,
                 auto_reply_enabled: Callable[[], bool]):
        self.store = store
        self.attachments = attachments
        self.events = events
        self.auto_reply_enabled = auto_reply_enabled

    def heartbeat(self, payload: dict, *, begin: bool = False) -> ServiceResult:
        try:
            operation = self.store.begin_outbound_send if begin else self.store.touch_outbound_message
            return ServiceResult({"updated": operation(int(payload.get("message_id", 0)))})
        except (ValueError, TypeError):
            return ServiceResult({"error": "invalid_id"}, 400)

    def recover_reply(self, payload: dict) -> ServiceResult:
        external_id = str(payload.get("external_id", "")).strip()
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        reply = str(payload.get("reply", "")).strip()
        if not external_id or not chat_id or not reply or len(reply) > 32000:
            return ServiceResult({"error": "invalid_recovery_reply"}, 400)
        # the same switches that mute a reply on the normal inbound path
        if (not self.auto_reply_enabled() or self.store.manual_chat_mode(chat_id)
                or self.store.manual_whatsapp_contact(chat_id, "")
                or queue_productivity.auto_reply_blocked(self.store, chat_id)):
            return ServiceResult({"queued": False, "silent": True})
        request_id = "inbound-recovery:" + hashlib.sha256(external_id.encode()).hexdigest()
        message_id = self.store.queue_direct_message(chat_id, reply, "Бот", request_id=request_id)
        self.events.notify_outbound()
        return ServiceResult({"queued": bool(message_id), "message_id": message_id})

    def template_error_result(self, payload: dict) -> ServiceResult:
        try:
            error_id = int(payload.get("template_error_id", 0))
        except (ValueError, TypeError):
            error_id = 0
        status = str(payload.get("status", ""))
        if error_id <= 0 or status not in {"sent", "failed"}:
            return ServiceResult({"error": "invalid_delivery"}, HTTPStatus.BAD_REQUEST)
        self.store.update_template_error_delivery(
            error_id,
            status,
            str(payload.get("provider_id", "")),
            str(payload.get("error", ""))[:500],
        )
        return ServiceResult({"updated": True})

    def claim(self, payload: dict) -> ServiceResult:
        try:
            created_after_epoch = int(payload.get("created_after", 0))
        except (ValueError, TypeError):
            created_after_epoch = 0
        created_after = ""
        if 0 < created_after_epoch <= int(datetime.now(timezone.utc).timestamp()) + 60:
            created_after = datetime.fromtimestamp(
                created_after_epoch,
                timezone.utc,
            ).replace(microsecond=0).isoformat()
        try:
            wait_ms = max(0, min(20000, int(payload.get("wait_ms", 0) or 0)))
        except (ValueError, TypeError):
            wait_ms = 0
        outbound_revision = self.events.outbound_revision()
        message = self.store.claim_outbound_message(created_after)
        if not message and wait_ms:
            self.events.wait_for_outbound(outbound_revision, wait_ms / 1000.0)
            message = self.store.claim_outbound_message(created_after)
        if not message:
            return ServiceResult({"message": None})
        # Preserve the original upload before WhatsApp can consume it.
        if message.get("media_path"):
            try:
                preserved = self.attachments.preserve(message["id"])
                if not preserved: raise OSError("Не удалось сохранить локальную копию вложения")
            except OSError as error:
                self.store.complete_outbound_message(message["id"], False, str(error))
                return ServiceResult({"message": None, "error": str(error)[:180]})
        try:
            mentions = json.loads(str(message.get("mentions_json", "[]") or "[]"))
            if not isinstance(mentions, list):
                mentions = []
        except (TypeError, ValueError, json.JSONDecodeError):
            mentions = []
        reply_key = normalize_message(str(message.get("reply_to_key", "")))[:160]
        reply_message = self.store.get_whatsapp_message(valid_conversation_id(str(message.get("chat_id", ""))), reply_key) if reply_key else None
        reply_preview_body = ""
        reply_preview_sender = ""
        if reply_message:
            reply_preview_body = normalize_message(str(reply_message.get("body", "") or reply_message.get("media_name", "") or "Вложение"))[:1200]
            reply_preview_sender = "Вы" if bool(reply_message.get("from_me")) else normalize_message(str(reply_message.get("sender", "") or "Пользователь"))[:100]
        return ServiceResult(
            {
                "message": {
                    "id": message["id"],
                    "ticket_id": message["ticket_id"],
                    "chat_id": message["chat_id"],
                    "phone": normalize_phone(message["phone"]),
                    "body": message["body"],
                    "mentions": mentions[:100],
                    "reply_to_key": message.get("reply_to_key", ""),
                    "reply_preview_body": reply_preview_body,
                    "reply_preview_sender": reply_preview_sender,
                    "media_path": message.get("media_path", ""),
                    "media_mime": message.get("media_mime", ""),
                    "media_name": message.get("media_name", ""),
                }
            }
        )

    def complete(self, payload: dict) -> ServiceResult:
        try:
            message_id = int(payload.get("message_id", 0))
        except (ValueError, TypeError):
            message_id = 0
        sent = payload.get("status") == "sent"
        if message_id <= 0 or payload.get("status") not in {"sent", "failed", "uncertain"}:
            return ServiceResult({"error": "invalid_result"}, HTTPStatus.BAD_REQUEST)
        provider_id = normalize_message(str(payload.get("provider_id", "")))[:180]
        if payload.get('status') == 'uncertain':
            updated = self.store.mark_outbound_uncertain(message_id, str(payload.get('error', '')))
            return ServiceResult({'updated': bool(updated), 'queue_status': str((updated or {}).get('status', ''))})
        sent_media = self.attachments.preserve(message_id) if sent else {}
        updated = self.store.complete_outbound_message(
            message_id,
            sent,
            str(payload.get("error", ""))[:500],
            provider_id,
        )
        # Сразу сохраняем успешное исходящее сообщение в истории. Это делает
        # ответ с цитатой видимым в нашей системе даже если whatsapp-web.js
        # вернул sentMessage без заполненного hasQuotedMsg и live-sync догонит позже.
        if sent and updated and provider_id:
            chat_id = valid_conversation_id(str(updated.get("chat_id", "")))
            if chat_id:
                reply_key = normalize_message(str(payload.get("reply_to_key", "") or updated.get("reply_to_key", "")))[:160]
                quoted = self.store.get_whatsapp_message(chat_id, reply_key) if reply_key else None
                quoted_body = normalize_message(str(payload.get("quoted_body", "")))[:1200]
                quoted_sender = normalize_message(str(payload.get("quoted_sender", "")))[:100]
                if quoted:
                    if not quoted_body:
                        quoted_body = normalize_message(str(quoted.get("body", "") or quoted.get("media_name", "") or "Вложение"))[:1200]
                    if not quoted_sender:
                        quoted_sender = "Вы" if bool(quoted.get("from_me")) else normalize_message(str(quoted.get("sender", "") or "Пользователь"))[:100]
                if reply_key and not quoted_body:
                    quoted_body = "Сообщение"
                if reply_key and not quoted_sender:
                    quoted_sender = "Сообщение"
                self.store.save_whatsapp_chat_messages(chat_id, [{
                    "id": provider_id,
                    "from_me": True,
                    "sender": normalize_message(str(updated.get("actor", "") or "Вы"))[:100],
                    "body": normalize_message(str(updated.get("body", "")))[:32000],
                    "type": "chat",
                    "timestamp": int(datetime.now(timezone.utc).timestamp()),
                    "ack": 1,
                    "quoted_message_key": reply_key,
                    "quoted_body": quoted_body,
                    "quoted_sender": quoted_sender,
                    **sent_media,
                }])
        if updated:
            self.events.notify("chat")
        return ServiceResult({"updated": bool(updated), "queue_status": str((updated or {}).get("status", ""))})
