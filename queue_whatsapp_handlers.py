from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from queue_inbound_service import InboundService
from queue_outbound_service import OutboundService

import queue_inbound_media
import queue_performance
import queue_realtime
import queue_reliability
import queue_uploads
import queue_workflow
from queue_chat_state import dedupe_whatsapp_messages, whatsapp_display_name
from queue_core import content_disposition_header, log_suppressed, normalize_message, normalize_phone, valid_chat_id, valid_conversation_id, valid_group_id
from queue_work_context import work_actor

# Application module (app.py): shared services, settings and helpers it defines.
app: Any = None


def bind(app_module: Any) -> None:
    global app
    app = app_module


class WhatsAppHandlerMixin:
    inbound_service: InboundService
    outbound_service: OutboundService

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

    def handle_chat_list_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        cleaned: list[dict[str, object]] = []
        raw_chats = payload.get("chats")
        if isinstance(raw_chats, list):
            for item in raw_chats[:100]:
                if not isinstance(item, dict):
                    continue
                chat_id = valid_chat_id(str(item.get("id", "")))
                if not chat_id:
                    continue
                # QUEUE_3_3_95_CHAT_LIST_CANONICAL
                chat_id = app.STORE.canonical_whatsapp_chat_id(chat_id)
                try:
                    timestamp = int(item.get("timestamp", 0))
                    unread = max(0, int(item.get("unread_count", 0)))
                except (ValueError, TypeError):
                    timestamp, unread = 0, 0
                cleaned.append(
                    {
                        "id": chat_id,
                        "name": normalize_message(str(item.get("name", "")))[:100]
                        or chat_id.split("@")[0],
                        "last_message": normalize_message(
                            str(item.get("last_message", ""))
                        )[:160],
                        "timestamp": timestamp,
                        "unread_count": unread,
                        "last_from_me": bool(item.get("last_from_me")),
                    }
                )
        # QUEUE_3_3_95_CHAT_LIST_DEDUPE
        deduped_chats: dict[str, dict[str, object]] = {}
        for row in cleaned:
            cid = str(row.get("id", ""))
            if not cid:
                continue
            previous = deduped_chats.get(cid)
            if previous is None:
                deduped_chats[cid] = row
                continue
            prev_ts = int(previous.get("timestamp", 0) or 0)
            row_ts = int(row.get("timestamp", 0) or 0)
            newer, older = (row, previous) if row_ts >= prev_ts else (previous, row)
            merged = dict(older)
            merged.update(newer)
            merged["unread_count"] = max(int(previous.get("unread_count", 0) or 0), int(row.get("unread_count", 0) or 0))
            # Preserve a useful name if the newer technical row has only digits.
            newer_name = str(merged.get("name", "") or "").strip()
            older_name = str(older.get("name", "") or "").strip()
            if (not newer_name or newer_name.replace("+", "").replace(" ", "").isdigit()) and older_name:
                merged["name"] = older_name
            deduped_chats[cid] = merged
        cleaned = list(deduped_chats.values())

        with app.CHAT_LOCK:
            app.CHAT_STATE["connected"] = bool(payload.get("connected", True))
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
            if cleaned or isinstance(raw_chats, list):
                app.CHAT_STATE["chats"] = cleaned
        app.STORE.upsert_whatsapp_chats(cleaned)
        if isinstance(raw_chats, list):
            queue_realtime.notify("chat-list")
        self.json_response({"updated": True, "chats": len(cleaned)})

    def handle_presence_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        rows = payload.get("items", [])
        if not isinstance(rows, list):
            rows = []
        cleaned: dict[str, dict[str, object]] = {}
        now = datetime.now(timezone.utc).isoformat()
        for row in rows[:80]:
            if not isinstance(row, dict):
                continue
            chat_id = valid_chat_id(str(row.get("chat_id", "") or ""))
            if not chat_id or chat_id.endswith("@g.us"):
                continue
            cleaned[chat_id] = {
                "known": bool(row.get("known")),
                "online": bool(row.get("online")),
                "state": str(row.get("state", "") or "")[:32],
                "updated_at": now,
            }
        with app.CHAT_LOCK:
            presence_map = app.CHAT_STATE.get("presence")
            if not isinstance(presence_map, dict):
                presence_map = {}
                app.CHAT_STATE["presence"] = presence_map
            presence_map.update(cleaned)
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "items": len(cleaned)})

    def handle_group_participants_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_group_id(str(payload.get("chat_id", "")))
        raw = payload.get("participants", [])
        if not chat_id or not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, object]] = []
        for item in raw[:300]:
            if not isinstance(item, dict):
                continue
            mention_id = valid_chat_id(str(item.get("mention_id", "")))
            if not mention_id:
                continue
            resolved_id = valid_chat_id(str(item.get("resolved_id", "")))
            cleaned.append({
                "mention_id": mention_id,
                "resolved_id": resolved_id,
                "name": normalize_message(str(item.get("name", "")))[:100] or mention_id.split("@")[0],
                "phone": normalize_phone(str(item.get("phone", ""))),
                "is_admin": bool(item.get("is_admin")),
                "is_me": bool(item.get("is_me")),
            })
        with app.CHAT_LOCK:
            participants_map = app.CHAT_STATE.get("group_participants")
            if not isinstance(participants_map, dict):
                participants_map = {}
                app.CHAT_STATE["group_participants"] = participants_map
            participants_map[chat_id] = cleaned
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "participants": len(cleaned)})

    def handle_group_message_identities_sync(self) -> None:
        payload = self.read_authorized_json(2_000_000)
        if payload is None:
            return
        chat_id = valid_group_id(str(payload.get("chat_id", "")))
        raw = payload.get("identities", [])
        if not chat_id or not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, str]] = []
        generic = {"", "участник", "участник группы", "пользователь whatsapp"}
        for item in raw[:100]:
            if not isinstance(item, dict):
                continue
            message_id = normalize_message(str(item.get("id", "")))[:160]
            if not message_id:
                continue
            sender = normalize_message(str(item.get("sender", "")))[:100]
            sender_phone = normalize_phone(str(item.get("sender_phone", "")))
            sender_id = valid_chat_id(str(item.get("sender_id", "")))
            cleaned.append({
                "id": message_id,
                "sender": sender,
                "sender_phone": sender_phone,
                "sender_id": sender_id,
            })
        updated = app.STORE.update_whatsapp_group_message_identities(chat_id, cleaned)

        # Apply the same enrichment to the in-memory copy so the open group
        # changes immediately, without requiring a page reload or DB roundtrip.
        by_id = {item["id"]: item for item in cleaned}
        with app.CHAT_LOCK:
            messages_map = app.CHAT_STATE.get("messages", {})
            messages = messages_map.get(chat_id, []) if isinstance(messages_map, dict) else []
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    patch = by_id.get(str(message.get("id", "")))
                    if not patch:
                        continue
                    current_sender = normalize_message(str(message.get("sender", "")))[:100]
                    current_folded = current_sender.casefold()
                    new_sender = patch.get("sender", "")
                    new_folded = new_sender.casefold()
                    current_generic = current_folded in generic or bool(re.fullmatch(r"[+\d\s().-]+", current_sender or ""))
                    new_useful = bool(new_sender) and new_folded not in generic and not bool(re.fullmatch(r"[+\d\s().-]+", new_sender))
                    if current_generic and new_useful:
                        message["sender"] = new_sender
                    if patch.get("sender_phone"):
                        message["sender_phone"] = patch["sender_phone"]
                    if patch.get("sender_id"):
                        message["sender_id"] = patch["sender_id"]
                app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "messages": updated})

    # EO_LIVE_CHAT_ALIAS_COLLAPSE_V26_20261001
    @staticmethod
    def _collapse_live_whatsapp_aliases_v26(
        live_rows: object,
        authoritative_names: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        """Canonicalize @lid rows already present in CHAT_STATE and merge duplicates."""
        if not isinstance(live_rows, list):
            return [], 0

        authoritative_names = authoritative_names or {}
        merged: dict[str, dict[str, object]] = {}
        removed = 0

        def useful_name(value: object) -> bool:
            text = str(value or "").strip()
            if not text:
                return False
            folded = text.casefold()
            if folded in {
                "пользователь whatsapp",
                "неизвестный отправитель",
                "direct",
                "рабочий whatsapp",
            }:
                return False
            compact = re.sub(r"[\s+().-]+", "", text)
            return not compact.isdigit()

        for raw_row in live_rows:
            if not isinstance(raw_row, dict):
                continue
            raw_id = valid_chat_id(str(raw_row.get("id", "")))
            if not raw_id:
                continue

            canonical_id = app.STORE.canonical_whatsapp_chat_id(raw_id) or raw_id
            row = dict(raw_row)
            row["id"] = canonical_id

            known_name = str(authoritative_names.get(canonical_id, "") or "").strip()
            if known_name:
                row["name"] = known_name

            previous = merged.get(canonical_id)
            if previous is None:
                merged[canonical_id] = row
                if canonical_id != raw_id:
                    removed += 1
                continue

            removed += 1
            try:
                previous_ts = int(previous.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                previous_ts = 0
            try:
                row_ts = int(row.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                row_ts = 0

            newer, older = (row, previous) if row_ts >= previous_ts else (previous, row)
            combined = dict(older)
            combined.update(newer)
            combined["id"] = canonical_id

            # Preserve the best contact name even if the newer row is a
            # technical phone/placeholder row generated by a system reply.
            preferred_name = str(authoritative_names.get(canonical_id, "") or "").strip()
            if preferred_name:
                combined["name"] = preferred_name
            else:
                newer_name = str(newer.get("name", "") or "").strip()
                older_name = str(older.get("name", "") or "").strip()
                if not useful_name(newer_name) and useful_name(older_name):
                    combined["name"] = older_name

            for counter in ("unread_count", "mention_unread_count"):
                try:
                    combined[counter] = max(
                        int(previous.get(counter, 0) or 0),
                        int(row.get(counter, 0) or 0),
                    )
                except (TypeError, ValueError):
                    pass

            # Boolean UI metadata should survive either side.
            for flag in ("favorite", "muted", "needs_reply"):
                if flag in previous or flag in row:
                    combined[flag] = bool(previous.get(flag)) or bool(row.get(flag))

            # Preserve a useful avatar/profile field when only one alias had it.
            for field in (
                "avatar_url",
                "profile_pic_url",
                "phone",
                "last_sender",
                "last_sender_avatar_url",
            ):
                if not combined.get(field):
                    combined[field] = previous.get(field) or row.get(field) or ""

            merged[canonical_id] = combined

        rows = list(merged.values())
        rows.sort(key=lambda item: int(item.get("timestamp", 0) or 0), reverse=True)
        return rows, removed

    def handle_contact_list_sync(self) -> None:
        payload = self.read_authorized_json(4_000_000)
        if payload is None:
            return
        raw = payload.get("contacts", [])
        if not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned = []
        for item in raw[:1000]:
            if not isinstance(item, dict):
                continue
            chat_id = valid_chat_id(str(item.get("chat_id", "")))
            phone = normalize_phone(str(item.get("phone", "")))
            if not chat_id and phone:
                chat_id = f"{phone.lstrip('+')}@c.us"
            if not chat_id:
                continue
            raw_id = valid_chat_id(str(item.get("raw_id", "")))
            cleaned.append({"chat_id": chat_id, "raw_id": raw_id, "phone": phone, "name": normalize_message(str(item.get("name", "")))[:100], "saved": bool(item.get("saved"))})
            if raw_id.endswith("@lid") and chat_id.endswith("@c.us") and raw_id != chat_id:
                app.STORE.remember_whatsapp_chat_alias(raw_id, chat_id)
        authoritative_names: dict[str, str] = {}
        for item in cleaned:
            display_name = whatsapp_display_name(item.get("name", ""))
            chat_key = valid_chat_id(str(item.get("chat_id", "")))
            phone = normalize_phone(str(item.get("phone", "")))
            if display_name and chat_key:
                authoritative_names[chat_key] = display_name
                app.STORE.update_whatsapp_contact_display_name(chat_key, display_name, phone)
            if display_name and phone:
                authoritative_names[f"{phone.lstrip('+')}@c.us"] = display_name
        collapsed_count_v26 = 0
        with app.CHAT_LOCK:
            app.CHAT_STATE["discovered_contacts"] = cleaned
            live = app.CHAT_STATE.get("chats", [])
            collapsed_live_v26, collapsed_count_v26 = self._collapse_live_whatsapp_aliases_v26(
                live,
                authoritative_names,
            )
            app.CHAT_STATE["chats"] = collapsed_live_v26
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        if collapsed_count_v26:
            queue_realtime.notify("chat-list")
            print(
                f"WhatsApp contact sync merged duplicate live chats: {collapsed_count_v26}",
                flush=True,
            )
        self.json_response({
            "updated": True,
            "contacts": len(cleaned),
            "collapsed_live_chats": collapsed_count_v26,
        })

    def handle_contact_profile_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        if not chat_id:
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        chat_id = app.STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        profile = {"chat_id": chat_id, "name": normalize_message(str(payload.get("name", "")))[:100], "phone": normalize_phone(str(payload.get("phone", ""))), "about": normalize_message(str(payload.get("about", "")))[:500], "profile_pic_url": str(payload.get("profile_pic_url", ""))[:2000], "is_business": bool(payload.get("is_business"))}
        display_name = whatsapp_display_name(profile.get("name", ""))
        if display_name:
            app.STORE.update_whatsapp_contact_display_name(chat_id, display_name, str(profile.get("phone", "")))
        with app.CHAT_LOCK:
            profiles = app.CHAT_STATE.get("contact_profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                app.CHAT_STATE["contact_profiles"] = profiles
            profiles[chat_id] = profile
            if display_name:
                live = app.CHAT_STATE.get("chats", [])
                if isinstance(live, list):
                    for chat in live:
                        if isinstance(chat, dict) and valid_chat_id(str(chat.get("id", ""))) == chat_id:
                            chat["name"] = display_name
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_group_refresh_check(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        self.json_response({"request": app.STORE.get_setting("group_refresh_request", "")})

    def handle_group_list_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        cleaned: list[dict[str, object]] = []
        raw_groups = payload.get("groups", [])
        if not isinstance(raw_groups, list):
            self.json_response({"error": "invalid_groups"}, HTTPStatus.BAD_REQUEST)
            return
        for item in raw_groups[:500]:
            if not isinstance(item, dict):
                continue
            chat_id = valid_group_id(str(item.get("id", "")))
            if not chat_id:
                continue
            try:
                participant_count = max(0, int(item.get("participant_count", 0) or 0))
            except (TypeError, ValueError):
                participant_count = 0
            cleaned.append(
                {
                    "id": chat_id,
                    "name": normalize_message(str(item.get("name", "")))[:100]
                    or "Группа WhatsApp",
                    "participant_count": participant_count,
                }
            )
        diagnostics = payload.get("diagnostics", {})
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        status = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": len(cleaned),
            "chats_ok": bool(diagnostics.get("chats_ok")),
            "chats_total": int(diagnostics.get("chats_total", 0) or 0),
            "chats_groups": int(diagnostics.get("chats_groups", 0) or 0),
            "contacts_ok": bool(diagnostics.get("contacts_ok")),
            "contacts_total": int(diagnostics.get("contacts_total", 0) or 0),
            "contacts_groups": int(diagnostics.get("contacts_groups", 0) or 0),
            "enriched": int(diagnostics.get("enriched", 0) or 0),
            "errors": [str(item)[:240] for item in diagnostics.get("errors", [])[:4]]
            if isinstance(diagnostics.get("errors", []), list)
            else [],
        }
        app.STORE.set_setting("group_sync_status", json.dumps(status, ensure_ascii=False))
        app.STORE.replace_whatsapp_groups(cleaned)
        self.json_response({"updated": True, "groups": len(cleaned)})

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
        with app.CHAT_LOCK:
            app.CHAT_STATE["connector_status"] = status
            app.CHAT_STATE["connected"] = status == "ready"
            app.CHAT_STATE["qr_data_url"] = qr_data_url if status == "qr" else ""
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_contact_policy(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        canonical_chat_id = app.STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        contact = app.STORE.manual_whatsapp_contact(canonical_chat_id, phone)
        # EO_BOT_HARD_RESET_20260930:
        # The connector consumes this generation token on the next inbound
        # message and clears only this user's in-memory bot caches.
        reset_token = app.STORE.get_setting(f"bot_hard_reset_v1:{canonical_chat_id}", "")
        if not reset_token and chat_id and chat_id != canonical_chat_id:
            reset_token = app.STORE.get_setting(f"bot_hard_reset_v1:{chat_id}", "")
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
        language = app.get_contact_language(chat_id, phone)
        if app.STORE.is_manual_whatsapp_contact(chat_id, phone):
            self.json_response({"allowed": True, "manual_contact": True, "language": language})
            return
        # Обычным пользователям звонки всегда запрещены. Старое одноразовое
        # разрешение (если осталось от версии с пунктом «Позвонить») просто
        # поглощаем и не используем.
        if chat_id:
            app.STORE.consume_whatsapp_call_permission(chat_id)
        self.json_response({"allowed": False, "manual_contact": False, "language": language})

    def handle_chat_messages_sync(self) -> None:
        payload = self.read_authorized_json(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        source_chat_id = valid_conversation_id(str(payload.get("source_chat_id", "")))
        # QUEUE_3_3_95_ALIAS_HINT
        if source_chat_id.endswith("@lid") and chat_id.endswith("@c.us") and source_chat_id != chat_id:
            try:
                app.STORE.remember_whatsapp_chat_alias(source_chat_id, chat_id)
                # EO_LIVE_CHAT_ALIAS_COLLAPSE_V26_20261001
                collapsed_count_v26 = 0
                with app.CHAT_LOCK:
                    collapsed_v26, collapsed_count_v26 = self._collapse_live_whatsapp_aliases_v26(
                        app.CHAT_STATE.get("chats", [])
                    )
                    if collapsed_count_v26:
                        app.CHAT_STATE["chats"] = collapsed_v26
                        app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
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
            chat_id = app.STORE.canonical_whatsapp_chat_id(chat_id)
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
                    stored_message = app.STORE.get_whatsapp_message(chat_id, message_id)
                    if stored_message:
                        old_media_path = str(stored_message.get("media_path", "") or "")
                except Exception:
                    log_suppressed("queue_whatsapp_handlers.handle_chat_messages_sync")
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
                media_path = app.save_media_payload(
                    chat_id, message_id, media_base64, media_mime, media_name
                )
            if item.get("media_receipt") and not is_deleted:
                media_path = queue_inbound_media.resolve(app.MEDIA_DIR, chat_id, message_id, item["media_receipt"]) or media_path
            # Strongest path for our own UI sends: the connector supplies the
            # exact outbound queue id after WhatsApp has accepted the message.
            # preserve_sent_attachment is idempotent and returns the already
            # preserved /media/outbound-N.bin even after the temporary upload
            # was deleted from the outbound queue.
            if bool(item.get("from_me")) and outbound_message_id > 0 and not media_path and not is_deleted:
                preserved_by_queue = app.ATTACHMENTS.preserve(outbound_message_id)
                if preserved_by_queue:
                    media_path = str(preserved_by_queue.get("media_path", ""))
                    media_mime = media_mime or str(preserved_by_queue.get("media_mime", ""))
                    media_name = media_name or str(preserved_by_queue.get("media_name", ""))
            # A message sent from this web UI may be echoed by WhatsApp as a
            # caption-only observation.  Reattach the copy preserved when the
            # outbound queue completed, so our own system shows the same media
            # that is visible in WhatsApp.
            if bool(item.get("from_me")) and message_id and not media_path and not is_deleted:
                preserved_media = app.preserved_system_outgoing_media(
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
                recovered_media = app.reconcile_uncertain_system_outgoing_media(
                    chat_id,
                    message_id,
                    timestamp,
                    str(item.get("body", "")),
                    str(item.get("type", "")),
                    str(item.get("quoted_message_key", "")),
                )
                if not recovered_media:
                    recovered_media = queue_uploads.recover_clipboard_attachment(
                        app.STORE,
                        app.ATTACHMENTS,
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
        # 3.3.117: reconcile the revoke event with the row that is already
        # visible in our history before touching live state or persisting a tombstone.
        matched_deleted_keys: set[str] = set()
        for deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me in deleted_payloads:
            resolved_key, matched_count = app.scrub_deleted_whatsapp_message(
                chat_id, deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me
            )
            if matched_count > 0 and resolved_key:
                matched_deleted_keys.add(resolved_key)
                for cleaned_item in cleaned:
                    if bool(cleaned_item.get("deleted")) and app.same_whatsapp_message_identity(
                        str(cleaned_item.get("id", "")), deleted_message_id
                    ):
                        cleaned_item["id"] = resolved_key

        with app.CHAT_LOCK:
            messages_by_chat = app.CHAT_STATE.get("messages")
            if not isinstance(messages_by_chat, dict):
                messages_by_chat = {}
                app.CHAT_STATE["messages"] = messages_by_chat
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
            app.CHAT_STATE["connected"] = True
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
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
                app.STORE.save_whatsapp_chat_messages(chat_id, persist_items)
            workflow_items = [item for item in cleaned if not bool(item.get("deleted"))]
            try:
                if workflow_items:
                    queue_workflow.auto_link_open_ticket(app.STORE, chat_id, workflow_items)
            except Exception as exc:
                print(f"Автопривязка сообщения к заявке пропущена: {exc}", flush=True)
            # 1.00.6.6: голосовые сохраняются и воспроизводятся как обычные медиа.
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
        updated = app.STORE.update_whatsapp_message_ack(chat_id, message_id, ack)
        with app.CHAT_LOCK:
            messages_by_chat = app.CHAT_STATE.get("messages", {})
            if isinstance(messages_by_chat, dict):
                for messages in messages_by_chat.values():
                    if not isinstance(messages, list):
                        continue
                    for item in messages:
                        if isinstance(item, dict) and item.get("id") == message_id:
                            item["ack"] = max(int(item.get("ack", 0) or 0), ack)
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        updated = app.STORE.update_whatsapp_message_reaction(
            chat_id, message_id, sender_id or "__me__", emoji, from_me=from_me
        )
        message = app.STORE.get_whatsapp_message(chat_id, message_id) if updated else None
        reactions = app.STORE._reaction_summary((message or {}).get("reactions_json", "{}"))
        with app.CHAT_LOCK:
            messages_by_chat = app.CHAT_STATE.get("messages", {})
            if isinstance(messages_by_chat, dict):
                live = messages_by_chat.get(chat_id, [])
                if isinstance(live, list):
                    for item in live:
                        if isinstance(item, dict) and str(item.get("id", "")) == message_id:
                            item["reactions"] = reactions
                            break
            app.CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        if updated:
            queue_realtime.notify("chat")
        self.json_response({"updated": updated, "reactions": reactions})

    def handle_chat_history(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        cursor = normalize_message(query.get("cursor", [""])[0])[:80]
        if not chat_id:
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Не выбран чат"}, HTTPStatus.BAD_REQUEST)
            return
        page = app.STORE.list_saved_whatsapp_messages_page(chat_id, 50, cursor)
        messages = app.decorate_chat_messages(chat_id, dedupe_whatsapp_messages(list(page.get("messages", []))))
        self.json_response({
            "messages": messages,
            "has_more": bool(page.get("has_more")),
            "cursor": str(page.get("cursor", "")),
        })

    def handle_chat_media(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        message_id = normalize_message(query.get("message_id", [""])[0])[:160]
        message = app.STORE.get_whatsapp_message(chat_id, message_id) if chat_id and message_id else None
        if not message or bool(message.get("deleted")):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        media_path = str(message.get("media_path", "") or "")
        if not media_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            path = Path(media_path).resolve()
            media_root = app.MEDIA_DIR.resolve()
            if media_root not in path.parents or not path.is_file():
                raise ValueError
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = str(message.get("media_mime", "") or "application/octet-stream")
        download = query.get("download", [""])[0] == "1"
        media_name = str(message.get("media_name", "") or "Вложение")
        # Active documents must not execute in the application's origin.
        mime = queue_uploads.safe_mime(content_type)
        previewable = mime.startswith(('audio/','video/')) or mime in {
            'image/jpeg','image/png','image/webp','image/gif','application/pdf','text/plain'
        }
        disposition = content_disposition_header("attachment" if download or not previewable else "inline", media_name)
        queue_uploads.serve_range(self, path, content_type, disposition)

    def handle_chat_media_send(self) -> None:
        payload = self.read_json_body(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        if not self.claim_conversation_write(chat_id):
            return
        body = normalize_message(str(payload.get("message", "")))[:1000]
        reply_to = normalize_message(str(payload.get("reply_to", "")))[:160]
        mimetype = normalize_message(str(payload.get("mimetype", "")))[:120]
        filename = normalize_message(str(payload.get("filename", "")))[:180] or "Вложение"
        media_base64 = str(payload.get("media_base64", "") or "")
        if not chat_id or not media_base64:
            self.json_response({"queued": False, "error": "Не выбран чат или файл"}, HTTPStatus.BAD_REQUEST)
            return
        valid_upload, upload_error = queue_reliability.upload_validation(filename, mimetype, len(media_base64), app.MAX_MEDIA_BYTES)
        if not valid_upload:
            self.json_response({"queued": False, "error": upload_error}, HTTPStatus.BAD_REQUEST)
            return
        media_path = app.save_outbound_media(media_base64, mimetype, filename)
        if not media_path:
            self.json_response({"queued": False, "error": queue_performance.friendly_error(f"Не удалось сохранить вложение. Проверьте файл и лимит {app.MAX_MEDIA_BYTES // (1024*1024)} МБ")}, HTTPStatus.BAD_REQUEST)
            return
        mentions = payload.get("mentions", [])
        if not isinstance(mentions, list):
            mentions = []
        message_id = app.STORE.queue_direct_message(
            chat_id, body, work_actor(), [str(x) for x in mentions[:100]],
            reply_to_key=reply_to, media_path=media_path, media_mime=mimetype, media_name=filename,
        )
        if not message_id:
            try: Path(media_path).unlink()
            except OSError: pass
        else:
            queue_realtime.notify_outbound()
        self.json_response({"queued": bool(message_id), "message_id": message_id}, HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST)

    def handle_chat_media_send_raw(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        if not self.claim_conversation_write(chat_id):
            return
        body = normalize_message(query.get("message", [""])[0])[:1000]
        reply_to = normalize_message(query.get("reply_to", [""])[0])[:160]
        mimetype = normalize_message(query.get("mimetype", [""])[0])[:120] or normalize_message(self.headers.get("Content-Type", ""))[:120]
        filename = normalize_message(query.get("filename", [""])[0])[:180] or "Вложение"
        raw_mentions = query.get("mentions", [""])[0]
        mentions = [
            item.strip() for item in raw_mentions.split(",")
            if valid_conversation_id(item.strip()) or valid_group_id(item.strip())
        ][:100]
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if not chat_id or content_length <= 0:
            self.json_response({"queued": False, "error": "Не выбран чат или файл"}, HTTPStatus.BAD_REQUEST)
            return
        if content_length > app.MAX_MEDIA_BYTES:
            self.json_response(
                {"queued": False, "error": f"Файл превышает лимит {app.MAX_MEDIA_BYTES // (1024*1024)} МБ"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        # Existing validation expects the encoded payload size. Keep the same
        # safety check while streaming the actual bytes directly to disk.
        encoded_length = ((content_length + 2) // 3) * 4
        valid_upload, upload_error = queue_reliability.upload_validation(filename, mimetype, encoded_length, app.MAX_MEDIA_BYTES)
        if not valid_upload:
            self.json_response({"queued": False, "error": upload_error}, HTTPStatus.BAD_REQUEST)
            return
        media_path = app.save_outbound_media_stream(self, content_length, mimetype, filename)
        if not media_path:
            self.json_response({"queued": False, "error": "Не удалось сохранить вложение"}, HTTPStatus.BAD_REQUEST)
            return
        message_id = app.STORE.queue_direct_message(
            chat_id, body, work_actor(), mentions,
            reply_to_key=reply_to, media_path=media_path, media_mime=mimetype, media_name=filename,
        )
        if not message_id:
            try:
                Path(media_path).unlink()
            except OSError:
                pass
        else:
            queue_realtime.notify_outbound()
        self.json_response(
            {"queued": bool(message_id), "message_id": message_id},
            HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
        )

    def handle_chat_forward(self) -> None:
        payload = self.read_json_body(250_000)
        if payload is None:
            return
        source_chat = valid_conversation_id(str(payload.get("source_chat_id", "")))
        target_chat = valid_conversation_id(str(payload.get("target_chat_id", "")))
        if target_chat and not self.claim_conversation_write(target_chat):
            return
        raw_ids = payload.get("message_ids", [])
        if not source_chat or not target_chat or not isinstance(raw_ids, list):
            self.json_response({"queued": False, "error": "Некорректные данные пересылки"}, HTTPStatus.BAD_REQUEST)
            return
        ids: list[str] = []
        for value in raw_ids[:20]:
            mid = normalize_message(str(value))[:160]
            if mid and mid not in ids and app.STORE.get_whatsapp_message(source_chat, mid):
                ids.append(mid)
        queued = []
        action_ids: list[int] = []
        action_message_ids: list[str] = []
        blocked_mentions = 0
        for mid in ids:
            message = app.STORE.get_whatsapp_message(source_chat, mid) or {}
            if app.mention_only_message(message):
                blocked_mentions += 1
                continue
            payload = json.dumps(
                {
                    "target_chat_id": target_chat,
                    "body": str(message.get("body", "") or ""),
                    "timestamp": int(message.get("message_timestamp", 0) or 0),
                    "media_name": str(message.get("media_name", "") or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            action_id = app.STORE.queue_whatsapp_action("forward", source_chat, mid, payload, work_actor())
            if action_id:
                queued.append(action_id)
                action_ids.append(int(action_id))
                action_message_ids.append(mid)
        if queued:
            queue_realtime.notify_outbound()
        if not queued and blocked_mentions:
            self.json_response(
                {"queued": False, "count": 0, "blocked_count": blocked_mentions, "error": "Нельзя пересылать сообщение, состоящее только из тега пользователя"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        self.json_response(
            {
                "queued": bool(queued), "count": len(queued), "blocked_count": blocked_mentions,
                "action_ids": action_ids, "action_message_ids": action_message_ids,
            },
            HTTPStatus.OK if queued else HTTPStatus.BAD_REQUEST,
        )

    def handle_whatsapp_action_claim(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        action = app.STORE.claim_whatsapp_action()
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
        action = app.STORE.complete_whatsapp_action(
            action_id, status == "sent", str(payload.get("error", ""))[:500]
        )
        if action and status == "sent":
            chat_id = str(action.get("chat_id", ""))
            message_key = str(action.get("message_key", ""))
            action_type = str(action.get("action_type", ""))
            with app.CHAT_LOCK:
                messages_by_chat = app.CHAT_STATE.get("messages", {})
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
                                stored = app.STORE.get_whatsapp_message(chat_id, message_key) or {}
                                item["reactions"] = app.STORE._reaction_summary(stored.get("reactions_json", "{}"))
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
        with app.CHAT_LOCK:
            requested = str(app.CHAT_STATE.get("requested_chat_id", ""))
            requested_group = str(app.CHAT_STATE.get("requested_group_id", ""))
        unresolved_group_message_ids: list[str] = []
        if requested_group.endswith("@g.us"):
            generic = {"", "участник", "участник группы", "пользователь whatsapp", "direct"}
            try:
                saved = app.STORE.list_saved_whatsapp_messages(requested_group, limit=80)
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
            except Exception:
                log_suppressed("queue_whatsapp_handlers.handle_chat_control")
                unresolved_group_message_ids = []
        quote_probe_ids: list[str] = []
        quote_chat_id = requested_group if requested_group.endswith("@g.us") else requested
        if quote_chat_id:
            try:
                for item in reversed(app.STORE.list_saved_whatsapp_messages(quote_chat_id, limit=30)):
                    if bool(item.get("deleted")):
                        continue
                    message_id = str(item.get("id", "") or "").strip()
                    if message_id and message_id not in quote_probe_ids:
                        quote_probe_ids.append(message_id)
                    if len(quote_probe_ids) >= 20:
                        break
            except Exception:
                log_suppressed("queue_whatsapp_handlers.handle_chat_control")
                quote_probe_ids = []
        presence_ids: list[str] = []
        if requested and not requested.endswith("@g.us"):
            presence_ids.append(requested)
        if requested_group.endswith("@g.us"):
            with app.CHAT_LOCK:
                participants_map = app.CHAT_STATE.get("group_participants", {})
                current_participants = list(participants_map.get(requested_group, [])) if isinstance(participants_map, dict) else []
            for participant in current_participants[:16]:
                if not isinstance(participant, dict) or participant.get("is_me"):
                    continue
                candidate = valid_chat_id(str(participant.get("resolved_id", "") or participant.get("mention_id", "") or ""))
                if candidate and not candidate.endswith("@g.us") and candidate not in presence_ids:
                    presence_ids.append(candidate)
        self.json_response({
            "read_chat_ids": [key for key, at in list(app.READ_MARKED_AT.items()) if time.monotonic() - at < 5],
            "requested_chat_id": requested,
            "requested_group_id": requested_group,
            "presence_ids": presence_ids[:16],
            "unresolved_group_message_ids": unresolved_group_message_ids,
            "quote_probe_chat_id": quote_chat_id,
            "quote_probe_message_ids": quote_probe_ids,
        })
