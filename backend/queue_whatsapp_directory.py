"""Адреса коннектора для списков: чаты, присутствие, контакты, группы и их участники, псевдонимы @lid.

Подмешивается в queue_whatsapp_handlers.WhatsAppHandlerMixin, откуда берёт `whatsapp`
(WhatsAppDependencies) и методы чтения запроса.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from http import HTTPStatus

import queue_realtime
from queue_chat_state import whatsapp_display_name
from queue_core import normalize_message, normalize_phone, valid_chat_id, valid_group_id


class WhatsAppDirectoryRoutes:
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
                chat_id = self.whatsapp.store.canonical_whatsapp_chat_id(chat_id)
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

        with self.whatsapp.chat_lock:
            self.whatsapp.chat_state["connected"] = bool(payload.get("connected", True))
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
            if cleaned or isinstance(raw_chats, list):
                self.whatsapp.chat_state["chats"] = cleaned
        self.whatsapp.store.upsert_whatsapp_chats(cleaned)
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
        with self.whatsapp.chat_lock:
            presence_map = self.whatsapp.chat_state.get("presence")
            if not isinstance(presence_map, dict):
                presence_map = {}
                self.whatsapp.chat_state["presence"] = presence_map
            presence_map.update(cleaned)
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        with self.whatsapp.chat_lock:
            participants_map = self.whatsapp.chat_state.get("group_participants")
            if not isinstance(participants_map, dict):
                participants_map = {}
                self.whatsapp.chat_state["group_participants"] = participants_map
            participants_map[chat_id] = cleaned
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        updated = self.whatsapp.store.update_whatsapp_group_message_identities(chat_id, cleaned)

        # Apply the same enrichment to the in-memory copy so the open group
        # changes immediately, without requiring a page reload or DB roundtrip.
        by_id = {item["id"]: item for item in cleaned}
        with self.whatsapp.chat_lock:
            messages_map = self.whatsapp.chat_state.get("messages", {})
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
                self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "messages": updated})

    def _collapse_live_whatsapp_aliases_v26(
        self,
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

            canonical_id = self.whatsapp.store.canonical_whatsapp_chat_id(raw_id) or raw_id
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
                self.whatsapp.store.remember_whatsapp_chat_alias(raw_id, chat_id)
        authoritative_names: dict[str, str] = {}
        for item in cleaned:
            display_name = whatsapp_display_name(item.get("name", ""))
            chat_key = valid_chat_id(str(item.get("chat_id", "")))
            phone = normalize_phone(str(item.get("phone", "")))
            if display_name and chat_key:
                authoritative_names[chat_key] = display_name
                self.whatsapp.store.update_whatsapp_contact_display_name(chat_key, display_name, phone)
            if display_name and phone:
                authoritative_names[f"{phone.lstrip('+')}@c.us"] = display_name
        collapsed_count_v26 = 0
        with self.whatsapp.chat_lock:
            self.whatsapp.chat_state["discovered_contacts"] = cleaned
            live = self.whatsapp.chat_state.get("chats", [])
            collapsed_live_v26, collapsed_count_v26 = self._collapse_live_whatsapp_aliases_v26(
                live,
                authoritative_names,
            )
            self.whatsapp.chat_state["chats"] = collapsed_live_v26
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        chat_id = self.whatsapp.store.canonical_whatsapp_chat_id(chat_id) or chat_id
        profile = {"chat_id": chat_id, "name": normalize_message(str(payload.get("name", "")))[:100], "phone": normalize_phone(str(payload.get("phone", ""))), "about": normalize_message(str(payload.get("about", "")))[:500], "profile_pic_url": str(payload.get("profile_pic_url", ""))[:2000], "is_business": bool(payload.get("is_business"))}
        display_name = whatsapp_display_name(profile.get("name", ""))
        if display_name:
            self.whatsapp.store.update_whatsapp_contact_display_name(chat_id, display_name, str(profile.get("phone", "")))
        with self.whatsapp.chat_lock:
            profiles = self.whatsapp.chat_state.get("contact_profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                self.whatsapp.chat_state["contact_profiles"] = profiles
            profiles[chat_id] = profile
            if display_name:
                live = self.whatsapp.chat_state.get("chats", [])
                if isinstance(live, list):
                    for chat in live:
                        if isinstance(chat, dict) and valid_chat_id(str(chat.get("id", ""))) == chat_id:
                            chat["name"] = display_name
            self.whatsapp.chat_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_group_refresh_check(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        self.json_response({"request": self.whatsapp.store.get_setting("group_refresh_request", "")})

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
        self.whatsapp.store.set_setting("group_sync_status", json.dumps(status, ensure_ascii=False))
        self.whatsapp.store.replace_whatsapp_groups(cleaned)
        self.json_response({"updated": True, "groups": len(cleaned)})
