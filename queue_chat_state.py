from __future__ import annotations

from typing import Any
import queue_dialog_flow

APP_MODULE: Any = None

def bind(context: dict[str, Any], app_module: Any) -> None:
    global APP_MODULE
    APP_MODULE = app_module
    protected = {"bind", "APP_MODULE", "Any"}
    for name, value in context.items():
        if name.startswith("__") or name in protected:
            continue
        globals()[name] = value

def lock_owned_by_current_user(lock: dict[str, object] | None) -> bool:
    if not lock:
        return False
    user = queue_auth.current_user() or {}
    user_id = int(user.get("id", 0) or 0)
    owner_user_id = int(lock.get("owner_user_id", 0) or 0)
    if user_id > 0 and owner_user_id > 0:
        return user_id == owner_user_id
    username = str(user.get("username", "") or "").strip().casefold()
    owner_username = str(lock.get("owner_username", "") or "").strip().casefold()
    return bool(username and owner_username and username == owner_username)


def conversation_lock_snapshot(conversation_id: str) -> dict[str, object]:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return {"locked": False, "owned_by_me": False, "can_write": False, "owner": {}}
    lock = AUTH.conversation_lock(conversation_id)
    if not lock:
        return {"locked": False, "owned_by_me": False, "can_write": True, "owner": {}}
    owned_by_me = lock_owned_by_current_user(lock)
    return {
        "locked": True,
        "owned_by_me": owned_by_me,
        "can_write": owned_by_me,
        "owner": {
            "display_name": str(lock.get("owner_display_name", "") or ""),
            "username": str(lock.get("owner_username", "") or ""),
            "role": str(lock.get("owner_role", "") or ""),
        },
        "acquired_at": str(lock.get("acquired_at", "") or ""),
        "updated_at": str(lock.get("updated_at", "") or ""),
    }








def flexible_contact_match(query: str, name: str, phone: str, chat_id: str = "") -> bool:
    clean = normalize_message(query).casefold()
    if not clean:
        return True
    haystack = normalize_message(f"{name} {phone} {chat_id}").casefold()
    phone_digits = normalized_phone_digits(phone or chat_id)
    query_digits = normalized_phone_digits(clean)
    if len(query_digits) >= 5 and re.fullmatch(r"[+\d\s().-]+", clean):
        return query_digits in phone_digits
    for token in [item for item in re.split(r"\s+", clean) if item]:
        digits = normalized_phone_digits(token)
        if digits and re.search(r"\d", token):
            if digits not in phone_digits:
                return False
            continue
        if token not in haystack:
            return False
    return True


def whatsapp_display_name(value: object) -> str:
    """Return only a human WhatsApp display name, never our sender label or a phone."""
    name = normalize_message(str(value or ""))[:100].strip()
    if not name:
        return ""
    if name.casefold() in {"система", "system", "рабочий whatsapp"}:
        return ""
    if re.fullmatch(r"[+\d\s().-]+", name):
        return ""
    return name
















def pending_context_category(context: dict[str, object] | None) -> str:
    if not context or context.get("pending_category") not in CATEGORIES:
        return ""
    try:
        updated = datetime.fromisoformat(str(context["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, KeyError):
        return ""
    if datetime.now(timezone.utc) - updated > timedelta(hours=24):
        return ""
    return str(context["pending_category"])




def dedupe_whatsapp_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge repeated observations of the exact provider ID, never identical text."""
    result: dict[str, dict[str, object]] = {}
    for index, item in enumerate(messages):
        message_key = str(item.get("id", ""))
        if not message_key: continue
        direction, stanza = queue_message_identity.parts(message_key)
        key = (direction or str(bool(item.get("from_me"))).lower(), stanza)
        previous = result.get(key, {})
        merged = {**previous, **item}
        for field in ("media_path", "media_url", "media_mime", "media_name", "transcript",
                      "mentions", "quoted_message_key", "quoted_body", "quoted_sender", "sender_phone", "sender_id"):
            if not merged.get(field) and previous.get(field):
                merged[field] = previous[field]
        if previous.get("id"): merged["id"] = previous["id"]
        merged["ack"] = max(int(previous.get("ack", 0) or 0), int(item.get("ack", 0) or 0))
        if previous.get("edited") and not item.get("edited"):
            merged["body"] = previous.get("body", "")
        for flag in ("edited", "deleted", "forwarded"):
            merged[flag] = bool(previous.get(flag) or item.get(flag))
        if merged["deleted"]:
            merged.update(body="", media_path="", media_url="")
        result[key] = merged
    return list(result.values())

# QUEUE_1_00_1_DELETE_PREVIEW_SYNC
def latest_deleted_chat_previews() -> dict[str, dict[str, object]]:
    """Return chats whose newest persisted message is a deletion tombstone.

    whatsapp_chats/live WhatsApp may briefly keep the old preview text after
    message_revoke_everyone. Message history is authoritative, so the sidebar
    preview is corrected from the newest saved message without deleting history.
    """
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=5.0) as connection:
            rows = connection.execute(
                """
                SELECT m.chat_id, m.message_timestamp, m.from_me
                FROM whatsapp_chat_messages AS m
                WHERE m.deleted=1
                  AND m.id=(
                    SELECT newer.id
                    FROM whatsapp_chat_messages AS newer
                    WHERE newer.chat_id=m.chat_id
                    ORDER BY newer.message_timestamp DESC, newer.id DESC
                    LIMIT 1
                  )
                """
            ).fetchall()
    except Exception as exc:
        print(f"WhatsApp deleted-preview sync warning: {exc}", flush=True)
        return {}
    result: dict[str, dict[str, object]] = {}
    for chat_id, timestamp, from_me in rows:
        safe_id = valid_conversation_id(str(chat_id or ""))
        if not safe_id:
            continue
        try:
            ts = max(0, int(timestamp or 0))
        except (TypeError, ValueError):
            ts = 0
        result[safe_id] = {
            "timestamp": ts,
            "last_from_me": bool(from_me),
        }
    return result


def apply_deleted_preview_override(chats: list[dict[str, object]]) -> list[dict[str, object]]:
    tombstones = latest_deleted_chat_previews()
    if not tombstones:
        return chats
    for chat in chats:
        chat_id = valid_conversation_id(str(chat.get("id", "")))
        if not chat_id:
            continue
        canonical = STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        tombstone = tombstones.get(canonical) or tombstones.get(chat_id)
        if not tombstone:
            continue
        try:
            current_ts = max(0, int(chat.get("timestamp", 0) or 0))
        except (TypeError, ValueError):
            current_ts = 0
        deleted_ts = int(tombstone.get("timestamp", 0) or 0)
        # A newer live message must always win over an older deletion.
        if deleted_ts < current_ts:
            continue
        chat["last_message"] = "Сообщение удалено"
        chat["timestamp"] = deleted_ts
        chat["last_from_me"] = bool(tombstone.get("last_from_me"))
    return chats


def forward_targets_snapshot() -> list[dict[str, str]]:
    def load() -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        seen: set[str] = set()
        for chat in STORE.list_saved_whatsapp_chats(120):
            chat_id = valid_chat_id(str(chat.get("id", "")))
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                targets.append({"id": chat_id, "name": whatsapp_display_name(chat.get("name", "")) or "Пользователь WhatsApp"})
        for group in STORE.list_whatsapp_groups():
            chat_id = valid_group_id(str(group.get("chat_id", "")))
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                targets.append({"id": chat_id, "name": str(group.get("name", "") or "Группа WhatsApp")})
        return targets[:200]
    return list(cached_snapshot_value("forward_targets", 2.5, load))


def display_names():
    def load():
        manual = STORE.list_manual_whatsapp_contacts()
        with CHAT_LOCK:
            records = [dict(x) for x in CHAT_STATE.get("chats", []) if isinstance(x, dict)]
            records += [dict(x, id=k) for k,x in CHAT_STATE.get("profiles", {}).items() if isinstance(x, dict)]
            for participants in CHAT_STATE.get("group_participants", {}).values():
                records.extend(dict(x) for x in participants if isinstance(x, dict))
        return queue_names.Names(manual, records)
    return cached_snapshot_value("display_names_356", 1.0, load)

def _group_preview_mention_map(chat_id: str, mentions=None) -> dict[str, str]:
    """Map raw WhatsApp mention ids to the same readable tag names used in messages."""
    mapping: dict[str, str] = {}
    generic = {"", "участник", "участник группы", "пользователь whatsapp", "неизвестный"}

    def add(raw_id: object, raw_name: object, is_me: bool = False) -> None:
        identifier = str(raw_id or "").strip()
        name = str(raw_name or "").strip().lstrip("@")
        if is_me and name.casefold() in {"рабочий whatsapp", "рабочий ватсап"}:
            name = "Рабочий"
        if not identifier or not name or name.casefold() in generic:
            return
        if re.fullmatch(r"\+?[\d\s().-]+", name):
            return
        compact = identifier.split("@", 1)[0].strip()
        if not compact:
            return
        mapping[compact] = name
        digits = re.sub(r"\D", "", compact)
        if digits:
            mapping[digits] = name

    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            name = mention.get("name", "") or mention.get("display_name", "")
            own = bool(mention.get("is_me"))
            for key in ("id", "mention_id", "resolved_id", "jid"):
                add(mention.get(key, ""), name, own)

    if str(chat_id).endswith("@g.us"):
        try:
            with CHAT_LOCK:
                participants_map = CHAT_STATE.get("group_participants", {})
                participants = list(participants_map.get(chat_id, [])) if isinstance(participants_map, dict) else []
            for participant in participants:
                if not isinstance(participant, dict):
                    continue
                name = participant.get("name", "")
                own = bool(participant.get("is_me"))
                add(participant.get("mention_id", ""), name, own)
                add(participant.get("resolved_id", ""), name, own)
                add(participant.get("phone", ""), name, own)
        except Exception:
            pass
    return mapping


def _latest_group_preview_mentions(chat_id: str) -> list[dict[str, object]]:
    def load():
        try:
            page = STORE.list_saved_whatsapp_messages_page(chat_id, 3)
            rows = [dict(item) for item in page.get("messages", []) if isinstance(item, dict)]
            if not rows:
                return []
            latest = max(rows, key=lambda item: int(item.get("timestamp", item.get("message_timestamp", 0)) or 0))
            value = latest.get("mentions", [])
            return value if isinstance(value, list) else []
        except Exception:
            return []
    return list(cached_snapshot_value(f"group_preview_mentions_10067:{chat_id}", 2.5, load))


def humanize_group_preview_mentions(chat_id: str, body: object) -> str:
    text = str(body or "")
    if not text or "@" not in text or not str(chat_id).endswith("@g.us"):
        return text
    mapping = _group_preview_mention_map(chat_id)
    # The last saved message carries exact mention metadata. Use it as a second
    # source when the participant cache has not finished resolving yet.
    if re.search(r"@\d{6,}", text):
        mapping.update(_group_preview_mention_map(chat_id, _latest_group_preview_mentions(chat_id)))
    for technical_id, display_name in mapping.items():
        text = re.sub(rf"@{re.escape(technical_id)}(?!\d)", f"@{display_name}", text)
    return text


def enrich_chat_list(chats):
    # Personal chats and groups share the same persisted favorites setting.
    favorites = favorite_chat_ids()
    activity = cached_snapshot_value("chat_activity_356", 1.0, STORE.chat_activity)
    manual = {row["chat_id"]: row["name"] for row in cached_snapshot_value("manual_contacts", 2.5, STORE.list_manual_whatsapp_contacts)}
    for chat in chats:
        cid = str(chat.get("id", ""))
        canonical = cid if cid.endswith("@g.us") else (STORE.canonical_whatsapp_chat_id(cid) or cid)
        row = dict(activity.get(cid, {}))
        chat.update(row)
        chat["favorite"] = cid in favorites or canonical in favorites
        is_group = cid.endswith("@g.us")
        if is_group:
            # The message body already shows readable @tags in the open chat;
            # apply the same transformation to the compact preview on the left.
            chat["last_message"] = humanize_group_preview_mentions(cid, chat.get("last_message", ""))
        else:
            chat["name"] = display_names().name(cid, chat.get("name", ""), chat.get("phone", ""))
        if not is_group and manual.get(cid):
            chat["name"] = manual[cid]
        sid = str(row.get("last_sender_id", ""))
        if manual.get(sid):
            chat["last_sender"] = manual[sid]
        chat["last_sender_avatar_url"] = queue_avatars.url(APP_MODULE, sid) if sid else ""
    return chats


def chat_state_snapshot(requested_chat_id: str = "", read_visible: bool = False) -> dict[str, object]:
    requested = STORE.canonical_whatsapp_chat_id(valid_chat_id(requested_chat_id))
    if requested and read_visible:
        mark_whatsapp_read_throttled(requested)
    stored_chats: list[dict[str, object]] = list(STORE.list_saved_whatsapp_chats(100))
    manual_contacts = list(cached_snapshot_value("manual_contacts", 2.5, STORE.list_manual_whatsapp_contacts))
    manual_by_chat: dict[str, dict[str, object]] = {}
    for manual in manual_contacts:
        if not isinstance(manual, dict):
            continue
        manual_chat_id = valid_chat_id(str(manual.get("chat_id", "")))
        phone_digits = re.sub(r"\D", "", str(manual.get("phone", "")))
        if manual_chat_id:
            manual_by_chat[manual_chat_id] = manual
        if phone_digits:
            manual_by_chat[f"{phone_digits}@c.us"] = manual
    for item in stored_chats:
        item_chat_id = str(item.get("id", ""))
        manual = manual_by_chat.get(item_chat_id)
        if manual:
            item["name"] = str(manual.get("name", "")) or item.get("name", "")
            item["manual_contact"] = True
    known_stored_ids = {str(item.get("id", "")) for item in stored_chats}
    stored_contacts = list(cached_snapshot_value("whatsapp_contacts", 2.5, lambda: STORE.list_whatsapp_contacts(100)))
    for contact in stored_contacts:
        chat_id = valid_chat_id(str(contact.get("id", "")))
        if not chat_id or chat_id in known_stored_ids:
            continue
        stored_chats.append(
            {
                "id": chat_id,
                "name": str(contact.get("name", ""))[:100],
                "last_message": str(contact.get("last_message", ""))[:160],
                "timestamp": epoch_time(str(contact.get("created_at", ""))),
                "unread_count": 0,
                "last_from_me": False,
                "manual_contact": bool(contact.get("manual")),
            }
        )
    with CHAT_LOCK:
        if requested:
            CHAT_STATE["requested_chat_id"] = requested
            live_chat_items = CHAT_STATE.get("chats", [])
            if isinstance(live_chat_items, list):
                for item in live_chat_items:
                    if isinstance(item, dict) and item.get("id") == requested:
                        item["unread_count"] = 0
        selected = requested
        messages_by_chat = CHAT_STATE.get("messages", {})
        live_messages: list[dict[str, object]] = []
        if isinstance(messages_by_chat, dict):
            live_messages = list(messages_by_chat.get(selected, []))
        updated_at = str(CHAT_STATE.get("updated_at", ""))
        connection_is_fresh = False
        if updated_at:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                connection_is_fresh = datetime.now(timezone.utc) - updated < timedelta(seconds=45)
            except ValueError:
                pass
        live_chats = list(CHAT_STATE.get("chats", []))
        discovered_contacts = list(CHAT_STATE.get("discovered_contacts", []))
        profiles_snapshot = dict(CHAT_STATE.get("contact_profiles", {})) if isinstance(CHAT_STATE.get("contact_profiles", {}), dict) else {}
        presence_snapshot = dict(CHAT_STATE.get("presence", {})) if isinstance(CHAT_STATE.get("presence", {}), dict) else {}
        connected = bool(CHAT_STATE.get("connected")) and connection_is_fresh

    # WhatsApp contact discovery/profile data is authoritative for the visible name.
    # It repairs old rows that were accidentally renamed to "Система" by our own
    # outgoing auto-replies and also prevents a raw phone number from being shown.
    whatsapp_names: dict[str, str] = {}
    for contact in discovered_contacts:
        if not isinstance(contact, dict):
            continue
        contact_id = valid_chat_id(str(contact.get("chat_id", "")))
        display_name = whatsapp_display_name(contact.get("name", ""))
        if contact_id and display_name:
            whatsapp_names[contact_id] = display_name
    for contact_id, profile_data in profiles_snapshot.items():
        if not isinstance(profile_data, dict):
            continue
        display_name = whatsapp_display_name(profile_data.get("name", ""))
        if display_name:
            whatsapp_names[str(contact_id)] = display_name

    for manual_id, manual in manual_by_chat.items():
        if str(manual.get("name", "")).strip():
            whatsapp_names[manual_id] = str(manual["name"]).strip()

    history_page = STORE.list_saved_whatsapp_messages_page(selected, 50) if selected else {"messages": [], "has_more": False, "cursor": ""}
    stored_messages = list(history_page.get("messages", []))
    merged_messages: dict[str, dict[str, object]] = {}
    for item in stored_messages:
        message_id = str(item.get("id", ""))
        key = message_id or (
            f"{int(item.get('timestamp', 0) or 0)}:"
            f"{int(bool(item.get('from_me')))}:{item.get('body', '')}"
        )
        previous = merged_messages.get(key, {})
        merged = {**previous, **item}
        for preserve in ("media_path", "media_mime", "media_name", "transcript", "mentions", "quoted_message_key", "quoted_body", "quoted_sender", "forwarded", "sender_phone", "sender_id", "reactions"):
            if not merged.get(preserve) and previous.get(preserve):
                merged[preserve] = previous[preserve]
        # БД является авторитетной для удаления/редактирования. Live-снимок
        # коннектора может ещё несколько секунд содержать старую версию сообщения.
        merged["deleted"] = bool(previous.get("deleted") or item.get("deleted"))
        merged["edited"] = bool(previous.get("edited") or item.get("edited"))
        if previous.get("edited"):
            merged["body"] = previous.get("body", "")
        if merged["deleted"]:
            merged["body"] = ""
            merged["media_path"] = ""
            merged["media_url"] = ""
        merged_messages[key] = merged
    raw_messages = sorted(
        merged_messages.values(),
        key=lambda item: int(item.get("timestamp", 0) or 0),
    )[-50:]
    raw_messages = dedupe_whatsapp_messages(raw_messages)
    messages = decorate_chat_messages(selected, raw_messages) if selected else []
    merged_chats: dict[str, dict[str, object]] = {}
    for item in stored_chats:
        chat_id = str(item.get("id", ""))
        if not chat_id:
            continue
        clean_name = whatsapp_names.get(chat_id) or whatsapp_display_name(item.get("name", ""))
        merged_chats[chat_id] = {
            **item,
            "name": clean_name or "Пользователь WhatsApp",
        }
    for item in live_chats:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        chat_id = STORE.canonical_whatsapp_chat_id(str(item["id"]))
        if not chat_id:
            continue
        item = {**item, "id": chat_id}
        previous = merged_chats.get(chat_id, {})
        manual = manual_by_chat.get(chat_id)
        live_name = whatsapp_display_name(item.get("name", ""))
        previous_name = whatsapp_display_name(previous.get("name", ""))
        previous_timestamp = int(previous.get("timestamp", 0) or 0)
        live_timestamp = int(item.get("timestamp", 0) or 0)
        merged = {
            **previous,
            **item,
            # Never show our own label "Система" or a raw phone as a contact name.
            # Prefer the name WhatsApp itself reported for this contact.
            "name": whatsapp_names.get(chat_id) or live_name or previous_name or "Пользователь WhatsApp",
            "manual_contact": bool(manual) or bool(item.get("manual_contact")) or bool(previous.get("manual_contact")),
        }
        # Резервная синхронизация может записать более новое исходящее сообщение
        # прямо в БД, пока live-список ещё содержит старый preview. Не даём
        # старому live-состоянию затереть пометку «Вы:».
        if previous_timestamp > live_timestamp:
            merged["last_message"] = previous.get("last_message", "")
            merged["timestamp"] = previous_timestamp
            merged["last_from_me"] = bool(previous.get("last_from_me"))
        merged["unread_count"] = int(previous.get("unread_count", 0) or 0)
        merged_chats[chat_id] = merged
    # Финальная защита от дублей личных чатов. WhatsApp иногда одновременно
    # отдаёт одного человека как @lid и @c.us. Если обе строки содержат один и
    # тот же подтверждённый номер телефона, показываем одну карточку, сохраняя
    # более свежее превью и максимальный счётчик непрочитанных. Группы никогда
    # не объединяем по имени или другим эвристикам.
    deduped_private: dict[str, dict[str, object]] = {}
    for raw_chat in merged_chats.values():
        chat = dict(raw_chat)
        cid = valid_chat_id(str(chat.get("id", "")))
        if not cid:
            continue
        if cid.endswith("@g.us"):
            identity = f"group:{cid}"
            target_id = cid
        else:
            phone_digits = re.sub(r"\D", "", str(chat.get("phone", "")))
            if not phone_digits and cid.endswith("@c.us"):
                phone_digits = re.sub(r"\D", "", cid.split("@", 1)[0])
            canonical_cid = STORE.canonical_whatsapp_chat_id(cid)
            target_id = f"{phone_digits}@c.us" if phone_digits else canonical_cid
            # Важно: ключ дедупликации тоже строим по каноническому ID.
            # Раньше @lid и его уже известный @c.us могли получить одинаковый
            # target_id, но разные identity, поэтому в UI оставались две карточки.
            identity = f"phone:{phone_digits}" if phone_digits else f"id:{target_id}"
        chat["id"] = target_id
        previous = deduped_private.get(identity)
        if not previous:
            deduped_private[identity] = chat
            continue
        prev_ts = int(previous.get("timestamp", 0) or 0)
        cur_ts = int(chat.get("timestamp", 0) or 0)
        newer, older = (chat, previous) if cur_ts >= prev_ts else (previous, chat)
        merged = {**older, **newer}
        merged["id"] = target_id
        merged["unread_count"] = max(int(previous.get("unread_count", 0) or 0), int(chat.get("unread_count", 0) or 0))
        merged["manual_contact"] = bool(previous.get("manual_contact")) or bool(chat.get("manual_contact"))
        # Не даём пустому/техническому имени затереть нормальное имя контакта.
        prev_name = whatsapp_display_name(previous.get("name", ""))
        cur_name = whatsapp_display_name(chat.get("name", ""))
        merged["name"] = cur_name or prev_name or "Пользователь WhatsApp"
        deduped_private[identity] = merged
    chats = apply_deleted_preview_override(list(deduped_private.values()))
    chats = sorted(
        chats,
        key=lambda item: int(item.get("timestamp", 0) or 0),
        reverse=True,
    )[:100]
    now_presence = datetime.now(timezone.utc)
    def fresh_presence(chat_id: str) -> dict[str, object]:
        raw = presence_snapshot.get(chat_id, {}) if isinstance(presence_snapshot, dict) else {}
        if not isinstance(raw, dict):
            return {"known": False, "online": False}
        updated_raw = str(raw.get("updated_at", "") or "")
        if updated_raw:
            try:
                updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if now_presence - updated > timedelta(seconds=35):
                    return {"known": False, "online": False}
            except ValueError:
                return {"known": False, "online": False}
        return {"known": bool(raw.get("known")), "online": bool(raw.get("online")), "state": str(raw.get("state", "") or "")}
    for chat in chats:
        chat_id = str(chat["id"])
        chat["avatar_url"] = queue_avatars.url(APP_MODULE,chat_id)
        if not chat_id.endswith("@g.us"):
            chat["presence"] = fresh_presence(chat_id)
    manual_mode = STORE.manual_chat_mode(selected) if selected else None
    selected_manual = manual_by_chat.get(selected) if selected else None
    profile = dict(profiles_snapshot.get(selected, {})) if selected else {}
    selected_presence = fresh_presence(selected) if selected else {"known": False, "online": False}
    if selected:
        profile["profile_pic_url"] = queue_avatars.url(APP_MODULE,selected)
        profile["presence"] = selected_presence
    if profile:
        profile["name"] = display_names().name(selected, whatsapp_names.get(selected) or profile.get("name", ""), profile.get("phone", ""))
    # 3.3.105: read-only context for the optional information drawer.
    # This must never affect ticket routing or WhatsApp state. If the helper is
    # unavailable for any reason, the normal chat API stays unchanged.
    ui_context: dict[str, object] = {"active_tickets": []}
    if selected:
        raw_phone = str(profile.get("phone", "") or "").strip()
        if not raw_phone and selected.endswith("@c.us"):
            digits = re.sub(r"\D", "", selected.split("@", 1)[0])
            raw_phone = f"+{digits}" if digits else ""
        try:
            cache_key = f"ui_active_tickets:{selected}:{raw_phone}"
            active_rows = cached_snapshot_value(cache_key, 2.5, lambda: STORE.list_active_user_tickets(selected, raw_phone, 8))
        except Exception:
            active_rows = []
        active_items: list[dict[str, object]] = []
        for raw_row in active_rows:
            try:
                row = dict(raw_row)
            except Exception:
                row = raw_row if isinstance(raw_row, dict) else {}
            try:
                ticket_id = int(row.get("id", 0) or 0)
            except (TypeError, ValueError):
                ticket_id = 0
            if ticket_id <= 0:
                continue
            active_items.append({
                "id": ticket_id,
                "category": str(CATEGORIES.get(str(row.get("category", "")), row.get("category_name") or "Заявка")),
                "category_key": str(row.get("category", "")),
                "status": str(row.get("status", "") or ""),
                "priority": str(row.get("priority", "") or ""),
                "problem": str(row.get("problem", "") or row.get("description", "") or "")[:180],
                "updated_at": str(row.get("updated_at", "") or row.get("created_at", "") or ""),
            })
        ui_context = {"chat_id": selected, "phone": raw_phone, "active_tickets": active_items}
    return {
        "connected": connected,
        "updated_at": updated_at,
        "selected_chat_id": selected,
        "chats": enrich_chat_list(chats),
        "messages": messages,
        "history_has_more": bool(history_page.get("has_more")),
        "history_cursor": str(history_page.get("cursor", "")),
        "manual_mode": manual_mode or {},
        "dialog_review": queue_dialog_flow.review_state(STORE, selected) if selected else {},
        "manual_contact": bool(selected_manual),
        "profile": profile,
        "presence": selected_presence,
        "ui_context": ui_context,
        "forward_targets": forward_targets_snapshot(),
        "conversation_lock": conversation_lock_snapshot(selected),
    }


def group_state_snapshot(requested_group_id: str = "", read_visible: bool = False) -> dict[str, object]:
    requested = valid_group_id(requested_group_id)
    if requested and read_visible:
        mark_whatsapp_read_throttled(requested)
    groups = STORE.list_whatsapp_groups()
    group_preview_rows = [
        {
            "id": str(group.get("chat_id", "")),
            "last_message": str(group.get("last_message", "")),
            "timestamp": int(group.get("last_timestamp", 0) or 0),
            "last_from_me": bool(group.get("last_from_me")),
        }
        for group in groups
    ]
    group_preview_rows = apply_deleted_preview_override(group_preview_rows)
    group_preview_by_id = {str(row.get("id", "")): row for row in group_preview_rows}
    for group in groups:
        override = group_preview_by_id.get(str(group.get("chat_id", "")))
        if override:
            group["last_message"] = str(override.get("last_message", group.get("last_message", "")))
            group["last_timestamp"] = int(override.get("timestamp", group.get("last_timestamp", 0)) or 0)
            group["last_from_me"] = bool(override.get("last_from_me", group.get("last_from_me")))
    selected = requested or (str(groups[0]["chat_id"]) if groups else "")
    with CHAT_LOCK:
        if selected:
            CHAT_STATE["requested_group_id"] = selected
        participants_map = CHAT_STATE.get("group_participants", {})
        participants = (
            list(participants_map.get(selected, []))
            if isinstance(participants_map, dict) and selected
            else []
        )
        presence_snapshot = dict(CHAT_STATE.get("presence", {})) if isinstance(CHAT_STATE.get("presence", {}), dict) else {}
    participant_rows: list[dict[str, object]] = []
    for raw_participant in participants:
        if not isinstance(raw_participant, dict):
            continue
        participant = dict(raw_participant)
        participant["avatar_url"] = ""
        for avatar_id in (str(participant.get("resolved_id", "") or "").strip(), str(participant.get("mention_id", "") or "").strip()):
            if not avatar_id:
                continue
            avatar_url = queue_avatars.url(APP_MODULE, avatar_id)
            if avatar_url:
                participant["avatar_url"] = avatar_url
                break
        presence_value = {"known": False, "online": False}
        for presence_id in (str(participant.get("resolved_id", "") or "").strip(), str(participant.get("mention_id", "") or "").strip()):
            raw_presence = presence_snapshot.get(presence_id, {}) if presence_id else {}
            if not isinstance(raw_presence, dict):
                continue
            updated_raw = str(raw_presence.get("updated_at", "") or "")
            fresh = False
            if updated_raw:
                try:
                    updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    fresh = datetime.now(timezone.utc) - updated < timedelta(seconds=35)
                except ValueError:
                    fresh = False
            if fresh:
                presence_value = {"known": bool(raw_presence.get("known")), "online": bool(raw_presence.get("online")), "state": str(raw_presence.get("state", "") or "")}
                break
        participant["presence"] = presence_value
        participant_rows.append(participant)
    participants = participant_rows
    connection = connector_state_snapshot()
    history_page = STORE.list_saved_whatsapp_messages_page(selected, 50) if selected else {"messages": [], "has_more": False, "cursor": ""}
    raw_messages = dedupe_whatsapp_messages(list(history_page.get("messages", [])))
    return {
        "connected": connection["connected"],
        "selected_chat_id": selected,
        "selected_muted": bool(next((group.get("muted") for group in groups if str(group.get("chat_id", "")) == selected), 0)),
        "chats": enrich_chat_list([
            {
                "id": str(group["chat_id"]),
                "avatar_url": queue_avatars.url(APP_MODULE,str(group["chat_id"])),
                "name": str(group["name"]),
                "last_message": str(group.get("last_message", "")),
                "timestamp": int(group.get("last_timestamp", 0) or 0),
                "unread_count": int(group.get("unread_count", 0) or 0),
                "mention_unread_count": int(group.get("mention_unread_count", 0) or 0),
                "muted": bool(group.get("muted")),
                "favorite": False,
                "last_from_me": bool(group.get("last_from_me")),
                # В списке групп показываем обычный числовой счётчик непрочитанных
                # справа, как в WhatsApp. @упоминание подсвечивается уже внутри чата.
                "mentioned": False,
            }
            for group in groups
        ]),
        "messages": decorate_chat_messages(selected, raw_messages) if selected else [],
        "history_has_more": bool(history_page.get("has_more")),
        "history_cursor": str(history_page.get("cursor", "")),
        "participants": participants,
        "forward_targets": forward_targets_snapshot(),
        "conversation_lock": conversation_lock_snapshot(selected),
    }


def connector_state_snapshot() -> dict[str, object]:
    with CHAT_LOCK:
        updated_at = str(CHAT_STATE.get("updated_at", ""))
        status = str(CHAT_STATE.get("connector_status", "offline"))
        qr_data_url = str(CHAT_STATE.get("qr_data_url", ""))
        fresh = False
        if updated_at:
            try:
                updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                fresh = datetime.now(timezone.utc) - updated < timedelta(seconds=45)
            except ValueError:
                pass
        connected = bool(CHAT_STATE.get("connected")) and fresh
    if not fresh and status != "qr":
        status = "offline"
    return {
        "connected": connected,
        "status": status,
        "updated_at": updated_at,
        "qr_data_url": qr_data_url if status == "qr" else "",
    }


def _decorate_notification_event(event: dict[str, object]) -> dict[str, object]:
    item = dict(event)
    event_id = str(item.get("id", "") or "")
    kind = str(item.get("kind", "") or "")
    chat_id = ""
    if event_id.startswith("contact:"):
        chat_id = event_id[len("contact:"):].rsplit(":", 1)[0]
    elif event_id.startswith("group:"):
        chat_id = event_id[len("group:"):].rsplit(":", 1)[0]
    chat_id = str(item.get("chat_id") or chat_id)
    if not chat_id:
        return item
    if kind == "contact":
        contact_name = display_names().name(chat_id, item.get("source", ""))
        if bool(item.get("reply_to_our_message")):
            item["title"] = f"Ответ от {contact_name}" if contact_name else "Ответ на ваше сообщение"
        else:
            item["title"] = contact_name or str(item.get("title", "Новое сообщение WhatsApp"))

    event_ts = int(item.get("timestamp", 0) or 0)
    row = None
    try:
        with STORE.connection() as connection:
            row = connection.execute(
                """SELECT message_key, sender, sender_id, body, media_path, media_mime, media_name, message_timestamp
                   FROM whatsapp_chat_messages
                   WHERE chat_id=? AND from_me=0 AND deleted=0 AND message_key=?
                   ORDER BY id DESC LIMIT 1""",
                (chat_id, str(item.get("message_key", ""))),
            ).fetchone()
    except Exception:
        row = None

    avatar_candidates: list[str] = []
    if row is not None and kind == "group":
        sender_name = display_names().name(row["sender_id"], row["sender"])
        group_title = str(item.get("source") or "Группа WhatsApp")
        item["title"] = (f"Ответ от {sender_name} · {group_title}" if bool(item.get("reply_to_our_message"))
                         else sender_name + " · " + str(item.get("title", "Группа")))
        avatar_candidates.append(str(row["sender_id"] or "").strip())
    avatar_candidates.append(chat_id)
    for avatar_id in avatar_candidates:
        if not avatar_id:
            continue
        avatar_url = queue_avatars.url(APP_MODULE, avatar_id)
        if avatar_url:
            item["avatar_url"] = avatar_url
            break

    if row is not None:
        mime = str(row["media_mime"] or "").strip().lower()
        media_path = str(row["media_path"] or "").strip()
        message_key = str(row["message_key"] or "").strip()
        if media_path and message_key and mime.startswith("image/"):
            item["media_url"] = f"/api/chat-media?chat_id={quote(chat_id)}&message_id={quote(message_key)}"
            item["media_mime"] = mime
            if not str(item.get("detail", "") or "").strip() or str(item.get("detail", "")).startswith("["):
                item["detail"] = str(row["body"] or row["media_name"] or "Фото")
    return item


def notifications_snapshot() -> dict[str, object]:
    counts = queue_productivity.notification_counts(STORE, STORE.notification_counts())
    base_events = [_decorate_notification_event(item) for item in STORE.notification_events(20)]
    base_events = queue_productivity.filter_notification_events(STORE, base_events)
    connector = connector_state_snapshot()
    reminder_count, reminder_events = 0, []  # QUEUE_1_00_4: reminders removed
    system_events = queue_productivity.system_alerts(STORE, DATA_DIR, connector)
    # 3.3.104: if the live connector is already healthy, immediately retire the
    # persisted reliability alert instead of waiting up to five minutes for the
    # background health loop. This prevents a stale red warning after reconnect.
    if bool(connector.get("connected")):
        try: queue_reliability.resolve_persisted_alert(STORE, "connector")
        except Exception: pass
    reliability_count, reliability_events = queue_reliability.reliability_notification_events(APP_MODULE)
    queue_reliability.reconcile_notification_dismissals(
        STORE, [item.get("id") or item.get("event_id") for item in system_events], ("system:",)
    )
    system_events = queue_reliability.filter_dismissed_notifications(STORE, system_events)
    # 3.3.103: normalise reliability/SLA events so they behave like every other
    # notification in the UI. queue_reliability historically used event_id, while
    # the notification centre expects id/href/source. Keep the original payload too.
    normalized_reliability_events: list[dict[str, object]] = []
    for raw_event in reliability_events:
        event = dict(raw_event)
        event_id = str(event.get("id") or event.get("event_id") or "").strip()
        if event_id:
            event["id"] = event_id
        if not str(event.get("source") or "").strip():
            event["source"] = "SLA" if event_id.startswith("sla:") else "Система"
        if not str(event.get("href") or "").strip():
            if event_id.startswith("sla:"):
                parts = event_id.split(":")
                ticket_id = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
                event["href"] = f"/ticket?id={ticket_id}" if ticket_id else "/admin/system"
            else:
                event["href"] = "/admin/system"
        normalized_reliability_events.append(event)
    reliability_events = normalized_reliability_events
    # Reliability/SLA alerts belong to the same visible "Системные предупреждения"
    # row in the notification center. Keep one canonical counter so the red badge
    # can never disagree with the rows shown in the popover.
    counts["system"] = len(system_events) + int(reliability_count)
    counts.pop("reliability", None)
    total = sum(int(value or 0) for value in counts.values())
    events = [*reliability_events, *system_events, *reminder_events, *base_events]
    return {
        "counts": counts,
        "total": total,
        "group_chat_id": queue_productivity.first_unmuted_chat(STORE, True),
        "contact_chat_id": queue_productivity.first_unmuted_chat(STORE, False),
        "events": events[:20],
    }
