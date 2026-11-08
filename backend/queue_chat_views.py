"""Данные статической страницы групп; сама страница лежит в frontend/pages/groups.html."""
from __future__ import annotations

import json

from queue_admin_pages import avatar_initials
from queue_avatars import AvatarCache
from queue_core import human_time
from ticketing import TicketStore


def _sync_status(store: TicketStore) -> dict[str, object] | None:
    raw = store.get_setting("group_sync_status", "")
    if not raw:
        return None
    try:
        status = json.loads(raw)
        errors = status.get("errors", [])
        return {
            **{key: int(status.get(key, 0) or 0)
               for key in ("groups", "chats_groups", "chats_total", "contacts_groups", "contacts_total")},
            "updated_at": human_time(str(status.get("updated_at", ""))),
            "errors": [str(item) for item in errors] if isinstance(errors, list) else [],
        }
    except (ValueError, TypeError, AttributeError):
        return None


def groups_page_data(show_admin: bool, *, store: TicketStore, avatars: AvatarCache) -> dict[str, object]:
    groups = [{
        "chat_id": str(group["chat_id"]),
        "name": str(group["name"]),
        "initials": avatar_initials(str(group["name"])),
        "avatar_url": avatars.url(str(group["chat_id"])),
        "participant_count": int(group["participant_count"]),
        "updated_at": human_time(str(group["updated_at"])),
    } for group in store.list_whatsapp_groups()]
    admin = None
    if show_admin:
        admin = {
            "sync_status": _sync_status(store),
            "discovered": [{
                "chat_id": str(group["chat_id"]),
                "name": str(group["name"]),
                "participant_count": int(group.get("participant_count", 0) or 0),
            } for group in store.list_discovered_whatsapp_groups()],
        }
    return {"groups": groups, "admin": admin}
