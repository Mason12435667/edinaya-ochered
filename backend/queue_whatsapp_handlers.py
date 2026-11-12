"""HTTP-адреса для WhatsApp-коннектора: входящие сообщения, очередь отправки, состояние коннектора и действия с сообщениями.

Синхронизация списков, сообщений и адреса чата для сайта подмешаны в WhatsAppHandlerMixin из
queue_whatsapp_directory, queue_whatsapp_messages и queue_whatsapp_chat.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from queue_inbound_service import InboundService
from queue_outbound_service import OutboundService

import queue_realtime
from queue_attachment_archive import AttachmentArchive
from queue_core import log_suppressed, normalize_message, normalize_phone, valid_chat_id
from ticketing import TicketStore
from queue_whatsapp_directory import WhatsAppDirectoryRoutes
from queue_whatsapp_messages import WhatsAppMessageRoutes
from queue_whatsapp_chat import WhatsAppChatRoutes


@dataclass(frozen=True)
class WhatsAppDependencies:
    """What the connector and chat handlers use from app.py; the handler class holds it as `whatsapp`."""
    store: TicketStore
    attachments: AttachmentArchive
    chat_lock: Lock
    chat_state: dict[str, object]  # changed in place under chat_lock, never replaced
    read_marked_at: dict[str, float]
    media_dir: Path
    max_media_bytes: int
    get_contact_language: Callable[..., str]
    decorate_chat_messages: Callable[[str, list[dict[str, object]]], list[dict[str, object]]]
    mention_only_message: Callable[[dict[str, object]], bool]
    same_whatsapp_message_identity: Callable[[str, str], bool]
    scrub_deleted_whatsapp_message: Callable[..., tuple[str, int]]
    save_media_payload: Callable[..., str]
    save_outbound_media: Callable[..., str]
    save_outbound_media_stream: Callable[..., str]
    preserved_system_outgoing_media: Callable[..., dict[str, str]]
    reconcile_uncertain_system_outgoing_media: Callable[..., dict[str, str]]


class WhatsAppHandlerMixin(WhatsAppDirectoryRoutes, WhatsAppMessageRoutes, WhatsAppChatRoutes):
    inbound_service: InboundService
    outbound_service: OutboundService
    whatsapp: WhatsAppDependencies

    def handle_api_message(self) -> None:
        payload = self.read_authorized_json(18_000_000)
        if payload is None:
            return
        result = self.inbound_service.handle(payload)
        self.json_response(result.payload, result.status)

    def handle_template_error_delivery(self) -> None:
        payload = self.read_authorized_json()
        if payload is not None:
            result = self.outbound_service.template_error_result(payload)
            self.json_response(result.payload, result.status)

    def handle_outbound_claim(self) -> None:
        payload = self.read_authorized_json()
        if payload is not None:
            result = self.outbound_service.claim(payload)
            self.json_response(result.payload, result.status)

    def handle_outbound_result(self) -> None:
        payload = self.read_authorized_json()
        if payload is not None:
            result = self.outbound_service.complete(payload)
            self.json_response(result.payload, result.status)

    def handle_connector_state(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        status = normalize_message(str(payload.get("status", "offline"))).casefold()
        if status not in {"offline", "connecting", "qr", "ready"}:
            self.json_response({"error": "invalid_state"}, HTTPStatus.BAD_REQUEST)
            return
        qr_data_url = str(payload.get("qr_data_url", ""))
        if qr_data_url and (
            not qr_data_url.startswith("data:image/png;base64,")
            or len(qr_data_url) > 1_000_000
        ):
            self.json_response({"error": "invalid_qr"}, HTTPStatus.BAD_REQUEST)
            return
        with self.whatsapp.chat_lock:
            self.whatsapp.chat_state["connector_status"] = status
            self.whatsapp.chat_state["connected"] = status == "ready"
            self.whatsapp.chat_state["qr_data_url"] = qr_data_url if status == "qr" else ""
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_contact_policy(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        canonical_chat_id = self.whatsapp.store.canonical_whatsapp_chat_id(chat_id) or chat_id
        contact = self.whatsapp.store.manual_whatsapp_contact(canonical_chat_id, phone)
        # The connector consumes this generation token on the next inbound
        # message and clears only this user's in-memory bot caches.
        reset_token = self.whatsapp.store.get_setting(f"bot_hard_reset_v1:{canonical_chat_id}", "")
        if not reset_token and chat_id and chat_id != canonical_chat_id:
            reset_token = self.whatsapp.store.get_setting(f"bot_hard_reset_v1:{chat_id}", "")
        self.json_response(
            {
                "manual_contact": bool(contact),
                "name": str((contact or {}).get("name", "")),
                "allow_calls": bool(contact),
                "bot_reset_token": str(reset_token or ""),
            }
        )

    def handle_call_permission(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        language = self.whatsapp.get_contact_language(chat_id, phone)
        if self.whatsapp.store.is_manual_whatsapp_contact(chat_id, phone):
            self.json_response({"allowed": True, "manual_contact": True, "language": language})
            return
        # Обычным пользователям звонки всегда запрещены. Старое одноразовое
        # разрешение (если осталось от версии с пунктом «Позвонить») просто
        # поглощаем и не используем.
        if chat_id:
            self.whatsapp.store.consume_whatsapp_call_permission(chat_id)
        self.json_response({"allowed": False, "manual_contact": False, "language": language})

    def handle_whatsapp_action_claim(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        action = self.whatsapp.store.claim_whatsapp_action()
        self.json_response({"action": action})

    def handle_whatsapp_action_result(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            action_id = int(payload.get("action_id", 0) or 0)
        except (ValueError, TypeError):
            action_id = 0
        status = str(payload.get("status", ""))
        if action_id <= 0 or status not in {"sent", "failed"}:
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        action = self.whatsapp.store.complete_whatsapp_action(
            action_id, status == "sent", str(payload.get("error", ""))[:500]
        )
        if action and status == "sent":
            chat_id = str(action.get("chat_id", ""))
            message_key = str(action.get("message_key", ""))
            action_type = str(action.get("action_type", ""))
            with self.whatsapp.chat_lock:
                messages_by_chat = self.whatsapp.chat_state.get("messages", {})
                if isinstance(messages_by_chat, dict):
                    live = messages_by_chat.get(chat_id, [])
                    if isinstance(live, list):
                        for item in live:
                            if not isinstance(item, dict) or str(item.get("id", "")) != message_key:
                                continue
                            if action_type == "delete":
                                item["deleted"] = True
                                item["body"] = ""
                                item["media_path"] = ""
                                item["media_url"] = ""
                            elif action_type == "edit":
                                item["body"] = str(action.get("body", ""))
                                item["edited"] = True
                            elif action_type == "react":
                                stored = self.whatsapp.store.get_whatsapp_message(chat_id, message_key) or {}
                                item["reactions"] = self.whatsapp.store._reaction_summary(stored.get("reactions_json", "{}"))
                            break
        if action:
            queue_realtime.notify("chat")
        self.json_response({
            "updated": bool(action),
            "sent": status == "sent",
            "error": str((action or {}).get("error", "")),
        })

    def handle_chat_control(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        with self.whatsapp.chat_lock:
            requested = str(self.whatsapp.chat_state.get("requested_chat_id", ""))
            requested_group = str(self.whatsapp.chat_state.get("requested_group_id", ""))
        unresolved_group_message_ids: list[str] = []
        if requested_group.endswith("@g.us"):
            generic = {"", "участник", "участник группы", "пользователь whatsapp", "direct"}
            try:
                saved = self.whatsapp.store.list_saved_whatsapp_messages(requested_group, limit=80)
                for item in reversed(saved):
                    if bool(item.get("from_me")):
                        continue
                    sender = str(item.get("sender", "") or "").strip().casefold()
                    phone = str(item.get("sender_phone", "") or "").strip()
                    sender_id = str(item.get("sender_id", "") or "").strip()
                    if sender in generic or not phone or not sender_id:
                        message_id = str(item.get("id", "") or "").strip()
                        if message_id:
                            unresolved_group_message_ids.append(message_id)
                    if len(unresolved_group_message_ids) >= 30:
                        break
            except sqlite3.Error:
                log_suppressed("queue_whatsapp_handlers.handle_chat_control")
                unresolved_group_message_ids = []
        quote_probe_ids: list[str] = []
        quote_chat_id = requested_group if requested_group.endswith("@g.us") else requested
        if quote_chat_id:
            try:
                for item in reversed(self.whatsapp.store.list_saved_whatsapp_messages(quote_chat_id, limit=30)):
                    if bool(item.get("deleted")):
                        continue
                    message_id = str(item.get("id", "") or "").strip()
                    if message_id and message_id not in quote_probe_ids:
                        quote_probe_ids.append(message_id)
                    if len(quote_probe_ids) >= 20:
                        break
            except sqlite3.Error:
                log_suppressed("queue_whatsapp_handlers.handle_chat_control")
                quote_probe_ids = []
        presence_ids: list[str] = []
        if requested and not requested.endswith("@g.us"):
            presence_ids.append(requested)
        if requested_group.endswith("@g.us"):
            with self.whatsapp.chat_lock:
                participants_map = self.whatsapp.chat_state.get("group_participants", {})
                current_participants = list(participants_map.get(requested_group, [])) if isinstance(participants_map, dict) else []
            for participant in current_participants[:16]:
                if not isinstance(participant, dict) or participant.get("is_me"):
                    continue
                candidate = valid_chat_id(str(participant.get("resolved_id", "") or participant.get("mention_id", "") or ""))
                if candidate and not candidate.endswith("@g.us") and candidate not in presence_ids:
                    presence_ids.append(candidate)
        self.json_response({
            "read_chat_ids": [key for key, at in list(self.whatsapp.read_marked_at.items()) if time.monotonic() - at < 5],
            "requested_chat_id": requested,
            "requested_group_id": requested_group,
            "presence_ids": presence_ids[:16],
            "unresolved_group_message_ids": unresolved_group_message_ids,
            "quote_probe_chat_id": quote_chat_id,
            "quote_probe_message_ids": quote_probe_ids,
        })
