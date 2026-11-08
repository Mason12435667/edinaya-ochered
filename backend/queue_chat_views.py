from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import quote

from queue_admin_pages import avatar_html
from queue_avatars import AvatarCache
from queue_core import e, human_time, valid_group_id
from ticketing import TicketStore
from queue_templates import render_template


def render_groups(query: dict[str, list[str]], show_admin: bool = False, *, store: TicketStore,
                  avatars: AvatarCache, csrf_token: Callable[[], str], layout: Callable[..., str]) -> str:
    initial_group_id = valid_group_id(query.get("chat_id", [""])[0])
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows = []
    for group in store.list_whatsapp_groups():
        chat_id = str(group["chat_id"])
        admin_action = (
            render_template(
                "chat/group_delete_form.html",
                csrf_token=e(csrf_token()),
                chat_id=e(chat_id),
            )
            if show_admin
            else ""
        )
        rows.append(
            f"""<tr><td><a class="ticket-title-link avatar-name" href="/groups?chat_id={quote(chat_id)}">{avatar_html(avatars,chat_id,str(group['name']))}<strong>{e(group['name'])}</strong></a></td><td>{int(group['participant_count']) or 'Не определено'}</td><td>{e(human_time(str(group['updated_at'])))}</td><td>{admin_action}</td></tr>"""
        )
    table = "".join(rows) or '<tr><td colspan="4" class="empty">Группы пока не добавлены</td></tr>'
    admin_form = ""
    auto_refresh_html = ""
    if show_admin:
        sync_status_html = ""
        raw_sync_status = store.get_setting("group_sync_status", "")
        if raw_sync_status:
            try:
                sync_status = json.loads(raw_sync_status)
                found_count = int(sync_status.get("groups", 0) or 0)
                chats_groups = int(sync_status.get("chats_groups", 0) or 0)
                chats_total = int(sync_status.get("chats_total", 0) or 0)
                contacts_groups = int(sync_status.get("contacts_groups", 0) or 0)
                contacts_total = int(sync_status.get("contacts_total", 0) or 0)
                updated_at = human_time(str(sync_status.get("updated_at", "")))
                errors = sync_status.get("errors", [])
                error_html = ""
                if isinstance(errors, list) and errors:
                    error_html = f'<small class="muted">Часть способов поиска вернула ошибку: {e(" | ".join(str(item) for item in errors))}</small>'
                sync_status_html = (
                    render_template(
                        "chat/group_sync_status.html",
                        found_count=found_count,
                        chats_groups=chats_groups,
                        chats_total=chats_total,
                        contacts_groups=contacts_groups,
                        contacts_total=contacts_total,
                        updated_at=e(updated_at),
                        error_html=error_html,
                    )
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                sync_status_html = ""
        discovered_groups = store.list_discovered_whatsapp_groups()
        discovered_rows = []
        for group in discovered_groups:
            group_id = str(group["chat_id"])
            discovered_rows.append(
                render_template(
                    "chat/discovered_group_row.html",
                    name=e(str(group['name'])),
                    participant_count=int(group.get('participant_count', 0) or 0) or 'Не определено',
                    csrf_token=e(csrf_token()),
                    group_id=e(group_id),
                )
            )
        discovered_table = (
            '<div class="table-scroll"><table><thead><tr><th>Группа WhatsApp</th><th>Участники</th><th></th></tr></thead><tbody>'
            + "".join(discovered_rows)
            + '</tbody></table></div>'
            if discovered_rows
            else '<p class="muted">Доступных для добавления групп пока нет. Нажмите «Обновить список из WhatsApp».</p>'
        )
        admin_form = render_template(
            "chat/group_add_form.html",
            csrf_token=e(csrf_token()),
            sync_status_html=sync_status_html,
            discovered_table=discovered_table,
        )
        if query.get("refresh", [""])[0] == "1":
            auto_refresh_html = '<script>setTimeout(function(){window.location.href="/groups";},7000);</script>'
    content = render_template(
        "chat/groups.html",
        notice_html=notice_html,
        auto_refresh_html=auto_refresh_html,
        initial_group_id=e(initial_group_id),
        admin_form=admin_form,
        table=table,
    )
    return layout("Группы WhatsApp", content, "groups", show_admin)

def render_whatsapp(query: dict[str, list[str]], show_admin: bool = False, *, layout: Callable[..., str]) -> str:
    initial_chat_id = query.get("chat_id", [""])[0]
    content = render_template(
        "chat/whatsapp.html",
        initial_chat_id=e(initial_chat_id),
    )
    return layout("WhatsApp", content, "whatsapp", show_admin)
