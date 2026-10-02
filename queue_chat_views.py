from __future__ import annotations

from typing import Any

def bind(context: dict[str, Any]) -> None:
    protected = {"bind", "Any"}
    for name, value in context.items():
        if name.startswith("__") or name in protected:
            continue
        globals()[name] = value

def render_groups(query: dict[str, list[str]], show_admin: bool = False) -> str:
    initial_group_id = valid_group_id(query.get("chat_id", [""])[0])
    notice = query.get("notice", [""])[0]
    notice_html = f'<div class="notice">{e(notice)}</div>' if notice else ""
    rows = []
    for group in STORE.list_whatsapp_groups():
        chat_id = str(group["chat_id"])
        admin_action = (
            f'''<form method="post" action="/admin/groups" onsubmit="return confirm('Удалить группу из системы?')">
                  <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
                  <input type="hidden" name="action" value="delete">
                  <input type="hidden" name="chat_id" value="{e(chat_id)}">
                  <button class="button ghost" type="submit">Удалить</button>
                </form>'''
            if show_admin
            else ""
        )
        rows.append(
            f"""<tr><td><a class="ticket-title-link avatar-name" href="/groups?chat_id={quote(chat_id)}">{avatar_html(chat_id,str(group['name']))}<strong>{e(group['name'])}</strong></a></td><td>{int(group['participant_count']) or 'Не определено'}</td><td>{e(human_time(str(group['updated_at'])))}</td><td>{admin_action}</td></tr>"""
        )
    table = "".join(rows) or '<tr><td colspan="4" class="empty">Группы пока не добавлены</td></tr>'
    admin_form = ""
    auto_refresh_html = ""
    if show_admin:
        sync_status_html = ""
        raw_sync_status = STORE.get_setting("group_sync_status", "")
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
                    '<div class="notice">'
                    f'Последнее сканирование WhatsApp: найдено групп <strong>{found_count}</strong>. '
                    f'Чаты: {chats_groups}/{chats_total}, контакты: {contacts_groups}/{contacts_total}. '
                    f'{e(updated_at)}{error_html}'
                    '</div>'
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                sync_status_html = ""
        discovered_groups = STORE.list_discovered_whatsapp_groups()
        discovered_rows = []
        for group in discovered_groups:
            group_id = str(group["chat_id"])
            discovered_rows.append(
                f"""<tr>
                  <td><strong>{e(str(group['name']))}</strong></td>
                  <td>{int(group.get('participant_count', 0) or 0) or 'Не определено'}</td>
                  <td>
                    <form method="post" action="/admin/groups">
                      <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
                      <input type="hidden" name="action" value="add_discovered">
                      <input type="hidden" name="chat_id" value="{e(group_id)}">
                      <button class="button primary" type="submit">Добавить</button>
                    </form>
                  </td>
                </tr>"""
            )
        discovered_table = (
            '<div class="table-scroll"><table><thead><tr><th>Группа WhatsApp</th><th>Участники</th><th></th></tr></thead><tbody>'
            + "".join(discovered_rows)
            + '</tbody></table></div>'
            if discovered_rows
            else '<p class="muted">Доступных для добавления групп пока нет. Нажмите «Обновить список из WhatsApp».</p>'
        )
        admin_form = f"""
          <section class="panel manual-panel admin-contact-form">
            <div class="section-heading">
              <div><h2>Добавить группу из WhatsApp</h2><p>Система сама получает группы рабочего аккаунта. ID искать и вводить не нужно.</p></div>
              <form method="post" action="/admin/groups">
                <input type="hidden" name="csrf_token" value="{e(ADMIN_FORM_TOKEN)}">
                <input type="hidden" name="action" value="refresh">
                <button class="button primary" type="submit">Обновить список из WhatsApp</button>
              </form>
            </div>
            {sync_status_html}
            {discovered_table}
          </section>
        """
        if query.get("refresh", [""])[0] == "1":
            auto_refresh_html = '<script>setTimeout(function(){window.location.href="/groups";},7000);</script>'
    content = f"""
      {notice_html}
      {auto_refresh_html}
      <section class="page-heading whatsapp-heading conversation-heading-compact">
        <span class="connection-badge offline" id="whatsapp-connection">Проверка подключения</span>
      </section>
      <section class="chat-shell" data-conversation-page data-state-endpoint="/api/group-state" data-send-endpoint="/group-send" data-media-send-endpoint="/chat-media-send" data-forward-endpoint="/chat-forward" data-initial-chat-id="{e(initial_group_id)}" data-list-empty="Группы пока не добавлены" data-select-title="Выберите группу">
        <aside class="panel chat-sidebar">
          <div class="chat-sidebar-head"><h2>Группы</h2><button class="icon-button" type="button" data-chat-refresh title="Обновить">Обновить</button></div>
          <input type="search" data-chat-search placeholder="Поиск по названию группы">
          <div class="chat-list" data-chat-list><p class="chat-placeholder">Загрузка групп...</p></div>
        </aside>
        <section class="panel chat-main">
          <header class="chat-header">
            <div><small>Группа WhatsApp</small><h2 data-chat-title>Выберите группу</h2></div>
            <div class="chat-header-actions">
              <button class="button compact ghost" type="button" data-group-mute-toggle hidden>🔕 Заглушить</button>
              <details class="participants-details" data-participants-details>
                <summary>Участники <b data-participants-count>0</b></summary>
                <div class="participants-list" data-group-participants><span class="muted">Выберите группу</span></div>
              </details>
            </div>
          </header>
          <div class="chat-messages" data-chat-messages><p class="chat-placeholder">Выберите группу слева, чтобы увидеть сообщения</p></div>
          <div class="forward-toolbar" data-forward-toolbar hidden><strong data-forward-count>0 сообщений выбрано</strong><select data-forward-target aria-label="Куда переслать"></select><button class="button compact" type="button" data-forward-cancel>Отмена</button><button class="button primary compact" type="button" data-forward-send>Переслать</button></div>
          <form class="chat-composer" data-chat-composer>
            <input type="hidden" name="chat_id" data-chat-id>
            <input type="hidden" name="mentions" data-mentions-input>
            <input type="hidden" name="reply_to" data-reply-to>
            <div class="reply-preview" data-reply-preview hidden><div><strong data-reply-sender></strong><span data-reply-body></span></div><button type="button" data-reply-cancel aria-label="Отменить ответ">×</button></div>
            <span class="composer-file-name" data-media-name></span>
            <textarea class="composer-message-input" name="message" rows="2" maxlength="32000" placeholder="Напишите сообщение в группу"></textarea>
            <button class="button primary composer-send" type="submit">Отправить</button>
            <div class="composer-tools"><button class="composer-tool" type="button" data-emoji-toggle title="Смайлы">☺</button><label class="composer-tool composer-attach" title="Прикрепить файл">📎<input type="file" data-media-input multiple accept="image/*,video/*,audio/*,.pdf,.txt,.csv,.docx,.xlsx,.json,.xml,.zip,.rar,.7z" hidden></label></div>
            <div class="emoji-picker" data-emoji-picker hidden></div>
          </form>
        </section>
      </section>
      {admin_form}
      <section class="panel table-panel"><div class="section-heading"><div><h2>Все добавленные группы</h2><p>Добавлять и удалять группы может только администратор</p></div></div><div class="table-scroll"><table><thead><tr><th>Группа</th><th>Участники</th><th>Обновление</th><th></th></tr></thead><tbody>{table}</tbody></table></div></section>
    """
    return layout("Группы WhatsApp", content, "groups", show_admin)

def render_whatsapp(query: dict[str, list[str]], show_admin: bool = False) -> str:
    initial_chat_id = query.get("chat_id", [""])[0]
    content = f"""
      <section class="page-heading whatsapp-heading conversation-heading-compact">
        <span class="connection-badge offline" id="whatsapp-connection">Проверка подключения</span>
      </section>
      <section class="chat-shell" data-conversation-page data-whatsapp-page data-state-endpoint="/api/chat-state" data-send-endpoint="/chat-send" data-media-send-endpoint="/chat-media-send" data-forward-endpoint="/chat-forward" data-initial-chat-id="{e(initial_chat_id)}" data-list-empty="Личные чаты пока не загружены" data-select-title="Выберите пользователя">
        <aside class="panel chat-sidebar">
          <div class="chat-sidebar-head">
            <h2>Чаты</h2>
            <button class="icon-button" type="button" data-chat-refresh title="Обновить">Обновить</button>
          </div>
          <input type="search" data-chat-search placeholder="Поиск по имени или номеру">
          <div class="chat-list" data-chat-list><p class="chat-placeholder">Загрузка чатов...</p></div>
        </aside>
        <section class="panel chat-main">
          <header class="chat-header">
            <div><small>Диалог WhatsApp</small><h2 data-chat-title>Выберите пользователя</h2><span class="contact-presence" data-contact-presence hidden></span><span class="manual-mode-state" data-manual-mode-state></span><div class="contact-profile-summary" data-contact-profile-summary hidden></div></div>
            <div class="chat-header-actions">
              <button class="button compact" type="button" data-profile-toggle disabled>Профиль</button>
              <button class="button compact toolbar-overflow-source" type="button" data-manual-mode-toggle disabled>Выключить автоответчик</button>
            </div>
          </header>
          <div class="chat-messages" data-chat-messages>
            <p class="chat-placeholder">Выберите чат слева, чтобы увидеть последние сообщения</p>
          </div>
          <div class="forward-toolbar" data-forward-toolbar hidden><strong data-forward-count>0 сообщений выбрано</strong><select data-forward-target aria-label="Куда переслать"></select><button class="button compact" type="button" data-forward-cancel>Отмена</button><button class="button primary compact" type="button" data-forward-send>Переслать</button></div>
          <form class="chat-composer" data-chat-composer>
            <input type="hidden" name="chat_id" data-chat-id>
            <input type="hidden" name="reply_to" data-reply-to>
            <div class="reply-preview" data-reply-preview hidden><div><strong data-reply-sender></strong><span data-reply-body></span></div><button type="button" data-reply-cancel aria-label="Отменить ответ">×</button></div>
            <span class="composer-file-name" data-media-name></span>
            <textarea class="composer-message-input" name="message" rows="2" maxlength="32000" placeholder="Введите сообщение"></textarea>
            <button class="button primary composer-send" type="submit">Отправить</button>
            <div class="composer-tools"><button class="composer-tool" type="button" data-emoji-toggle title="Смайлы">☺</button><label class="composer-tool composer-attach" title="Прикрепить файл">📎<input type="file" data-media-input multiple accept="image/*,video/*,audio/*,.pdf,.txt,.csv,.docx,.xlsx,.json,.xml,.zip,.rar,.7z" hidden></label></div>
            <div class="emoji-picker" data-emoji-picker hidden></div>
          </form>
        </section>
      </section>
      <p class="muted chat-page-note">Для работы вкладки должны быть запущены программа и WhatsApp-коннектор. Телефон постоянно держать рядом не требуется.</p>
    """
    return layout("WhatsApp", content, "whatsapp", show_admin)
