from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlencode, urlparse

import queue_workflow as qw


def _value(query, key, default=""):
    return query.get(key, [default])[0]


def _csrf(handler, app, form) -> bool:
    if secrets.compare_digest(str(form.get("csrf_token", "")), str(app.ADMIN_FORM_TOKEN)):
        return True
    handler.json_response({"error": "Обновите страницу и попробуйте снова"}, 403)
    return False


def _send_bytes(handler, body: bytes, content_type: str, filename: str):
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.end_headers()
    handler.wfile.write(body)


def _human_bytes(app, value: int) -> str:
    try:
        return app.human_bytes(int(value or 0))
    except Exception:
        size = float(value or 0)
        for unit in ("Б", "КБ", "МБ", "ГБ"):
            if size < 1024 or unit == "ГБ":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} ГБ"


def _recent_page(handler, app):
    rows = qw.recent_chats(app.STORE, app.work_actor(), 30)
    cards = []
    for row in rows:
        chat_id = str(row.get("chat_id") or "")
        group = chat_id.endswith("@g.us")
        href = ("/groups" if group else "/whatsapp") + "?" + urlencode({"chat_id": chat_id})
        unread = int(row.get("unread_count") or 0)
        cards.append(
            f'<a class="workflow-recent-card" href="{app.e(href)}">'
            f'<span class="workflow-recent-icon">{"Г" if group else "Л"}</span>'
            f'<span><strong>{app.e(row.get("name") or chat_id)}</strong><small>{app.e(row.get("last_message") or "Нет текстового превью")}</small></span>'
            f'{f"<b>{unread}</b>" if unread else ""}</a>'
        )
    content = f'''
      <section class="page-heading"><div><p class="eyebrow">WhatsApp</p><h1>Недавние чаты</h1><p>Последние открытые диалоги текущего сотрудника</p></div></section>
      <section class="workflow-recent-grid">{"".join(cards) or '<div class="panel empty">Откройте несколько чатов, и они появятся здесь.</div>'}</section>
    '''
    handler.html_response(app.layout("Недавние чаты", content, "recent", handler.is_admin()))


def _workflow_admin_page(handler, app, query):
    if not handler.require_admin():
        return
    notice = _value(query, "notice")
    settings = qw.retention_settings(app.STORE)
    stats = qw.media_stats(app.STORE)
    required = set(qw.required_close_categories(app.STORE))
    category_items = []
    try:
        category_items = list(app.active_ticket_category_items())
    except Exception:
        category_items = [(k, v) for k, v in getattr(app, "CATEGORIES", {}).items()]
    checks = ''.join(
        f'<label class="workflow-check"><input type="checkbox" name="required_category__{app.e(key)}" value="1" {"checked" if key in required else ""}><span>{app.e(label)}</span></label>'
        for key, label in category_items
    )
    media_cards = ''.join(
        f'<article class="stat-card"><span>{label}</span><strong>{_human_bytes(app, stats[kind]["bytes"])}</strong><small>{stats[kind]["count"]} файлов</small></article>'
        for kind, label in (("photo","Фото"),("video","Видео"),("voice","Голосовые"),("file","Документы"))
    )
    content = f'''
      {f'<div class="notice">{app.e(notice)}</div>' if notice else ''}
      <section class="page-heading"><div><p class="eyebrow">Хранение и процессы</p><h1>Рабочие настройки</h1><p>Сроки хранения, очистка медиа и правила закрытия заявок</p></div></section>
      <section class="workflow-admin-grid">
        <form class="panel workflow-settings-card" method="post" action="/admin/workflow">
          <input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="retention">
          <h2>Срок хранения</h2><p class="muted">Активные заявки и связанные с ними медиа автоматически не удаляются.</p>
          <label>Сообщения, дней<input type="number" name="messages" min="1" max="3650" value="{settings['messages']}"></label>
          <label>Медиа, дней<input type="number" name="media" min="1" max="3650" value="{settings['media']}"></label>
          <label>Закрытые заявки, дней<input type="number" name="tickets" min="1" max="3650" value="{settings['tickets']}"></label>
          <button class="button primary" type="submit">Сохранить сроки</button>
        </form>
        <section class="panel workflow-settings-card"><h2>Медиа на диске</h2><div class="stats-grid workflow-media-stats">{media_cards}</div>
          <form class="workflow-cleanup-form" method="post" action="/admin/workflow">
            <input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="media_cleanup">
            <label>Тип<select name="kind"><option value="all">Все типы</option><option value="photo">Фото</option><option value="video">Видео</option><option value="voice">Голосовые</option><option value="file">Документы</option></select></label>
            <label>Минимальный размер, МБ<input type="number" name="min_mb" min="0" max="10240" step="1" value="0"></label>
            <label>Старше, дней<input type="number" name="older_days" min="0" max="3650" value="0"></label>
            <button class="button danger" type="submit">Очистить выбранное</button>
          </form>
        </section>
      </section>
      <section class="panel workflow-required-card"><h2>Обязательный комментарий при закрытии</h2><p class="muted">Выберите категории, которые нельзя закрыть без служебного комментария.</p>
        <form method="post" action="/admin/workflow"><input type="hidden" name="csrf_token" value="{app.e(app.ADMIN_FORM_TOKEN)}"><input type="hidden" name="action" value="close_categories"><div class="workflow-check-grid">{checks or '<span class="muted">Категории не найдены</span>'}</div><button class="button primary" type="submit">Сохранить правило</button></form>
      </section>
      <section class="panel"><h2>Экспорт истории</h2><p>TXT, PDF и ZIP с вложениями доступны из открытого WhatsApp-чата через кнопку «Экспорт».</p></section>
    '''
    handler.html_response(app.admin_layout("Рабочие настройки", content, "workflow"))


def _ticket_details(app, ticket_id: int):
    ticket = app.STORE.get_ticket(ticket_id)
    if not ticket:
        return None
    return {
        "ticket": ticket,
        "notes": qw.internal_notes(app.STORE, ticket_id),
        "related": qw.related_tickets(app.STORE, ticket_id),
        "close": qw.close_meta(app.STORE, ticket_id),
        "comment_required": qw.close_comment_required(app.STORE, str(ticket.get("category") or "")),
        "employees": list(getattr(app, "EMPLOYEES", [])),
        "close_reasons": qw.ALLOWED_CLOSE_REASONS,
    }


def get(handler, app):
    parsed = urlparse(handler.path); query = parse_qs(parsed.query); path = parsed.path
    if path == "/recent":
        _recent_page(handler, app)
    elif path == "/admin/workflow":
        _workflow_admin_page(handler, app, query)
    elif path == "/api/workflow-state":
        chat_id = _value(query, "chat_id")[:120]
        client_id = _value(query, "client_id")[:120]
        handler.json_response({
            "presence": qw.presence_state(app.STORE, chat_id, client_id),
            "tickets": qw.open_tickets_for_chat(app.STORE, chat_id, False, 20),
            "unread": qw.unread_counts(app.STORE),
            "conflict": qw.chat_conflict_state(app.STORE, chat_id),
        })
    elif path == "/api/workflow-unread-counts":
        handler.json_response(qw.unread_counts(app.STORE))
    elif path == "/api/workflow-next-unread":
        scope = _value(query, "scope")
        group = True if scope == "group" else False if scope == "personal" else None
        chat_id = qw.next_unread_chat(app.STORE, group)
        handler.json_response({"chat_id": chat_id, "href": (("/groups" if chat_id.endswith("@g.us") else "/whatsapp") + "?" + urlencode({"chat_id": chat_id})) if chat_id else ""})
    elif path == "/api/workflow-chat-history":
        handler.json_response({"events": qw.chat_history(app.STORE, _value(query, "chat_id"), 120)})
    elif path == "/api/workflow-summary":
        handler.json_response(qw.conversation_summary(app.STORE, _value(query, "chat_id")))
    elif path == "/api/workflow-tickets":
        handler.json_response({"tickets": qw.open_tickets_for_chat(app.STORE, _value(query, "chat_id"), _value(query,"all") in {"1","true"}, 100)})
    elif path == "/api/workflow-ticket-details":
        try: ticket_id = int(_value(query, "ticket_id", "0") or 0)
        except ValueError: ticket_id = 0
        data = _ticket_details(app, ticket_id)
        handler.json_response(data or {"error":"Заявка не найдена"}, 200 if data else 404)
    elif path == "/api/workflow-handover":
        handler.json_response(qw.handover_summary(app.STORE, app.work_actor()))
    elif path == "/api/workflow-export":
        chat_id = _value(query, "chat_id")[:120]
        fmt = _value(query, "format", "txt").lower()
        try:
            if fmt == "zip":
                _, body = qw.export_zip(app.STORE, chat_id); _send_bytes(handler, body, "application/zip", "queue-chat.zip")
            elif fmt == "pdf":
                _, body = qw.export_pdf(app.STORE, chat_id); _send_bytes(handler, body, "application/pdf", "queue-chat.pdf")
            else:
                _, body = qw.export_txt(app.STORE, chat_id); _send_bytes(handler, body, "text/plain; charset=utf-8", "queue-chat.txt")
        except Exception as exc:
            handler.json_response({"error": str(exc)[:300]}, 500)
    elif path == "/api/workflow-media-stats":
        if not handler.require_admin(): return True
        handler.json_response({"stats": qw.media_stats(app.STORE), "retention": qw.retention_settings(app.STORE)})
    else:
        return False
    return True


def _post_impl(handler, app):
    path = urlparse(handler.path).path
    known = {
        "/api/workflow-recent-open", "/api/workflow-presence", "/api/workflow-internal-note",
        "/api/workflow-ticket-quick", "/api/workflow-transfer", "/api/workflow-ticket-merge",
        "/api/workflow-ticket-split", "/api/workflow-bulk", "/admin/workflow",
    }
    if path not in known:
        return False
    if path == "/admin/workflow" and not handler.require_admin():
        return True
    form = handler.read_form()
    if not _csrf(handler, app, form):
        return True
    try:
        if path == "/api/workflow-recent-open":
            qw.record_recent(app.STORE, form.get("chat_id", ""), app.work_actor())
            handler.json_response({"updated": True})
        elif path == "/api/workflow-presence":
            qw.presence_update(app.STORE, form.get("chat_id", ""), form.get("client_id", ""), app.work_actor(), form.get("state", "viewing"))
            handler.json_response({"updated": True, "presence": qw.presence_state(app.STORE, form.get("chat_id", ""), form.get("client_id", ""))})
        elif path == "/api/workflow-internal-note":
            ticket_id = int(form.get("ticket_id", "0") or 0)
            actor = app.work_actor() if hasattr(app, "work_actor") else app.work_actor()
            note_id = qw.add_internal_note(app.STORE, ticket_id, actor, form.get("note", ""))
            handler.json_response({"created": True, "id": note_id, "ticket_id": ticket_id, "actor": actor})
        elif path == "/api/workflow-ticket-quick":
            ticket_id = int(form.get("ticket_id", "0") or 0)
            ticket = app.STORE.get_ticket(ticket_id)
            if not ticket:
                raise ValueError("Заявка не найдена")
            actor = app.work_actor()
            priority = form.get("priority", "")
            employee = form.get("employee", "")
            status = form.get("status", "")
            reason = form.get("reason", "")
            comment = form.get("comment", "")
            changed = False
            if priority:
                changed = bool(app.STORE.update_priority(ticket_id, priority, actor)) or changed
            if employee and employee != str(ticket.get("assigned_to", "") or ""):
                changed = qw.transfer_ticket(app.STORE, ticket_id, employee, reason, actor) or changed
            if status and status != str(ticket.get("status", "") or ""):
                valid, why = qw.validate_close(app.STORE, ticket_id, status, reason, comment)
                if not valid:
                    raise ValueError(why)
                assignee = employee or str(ticket.get("assigned_to", "") or "") or actor
                updated, _ = app.update_ticket_status(
                    ticket_id, status, actor, assignee,
                    allow_reopen=handler.is_admin(), close_reason=reason, close_comment=comment,
                )
                if not updated:
                    raise ValueError("Не удалось изменить статус")
                changed = True
            handler.json_response({"updated": changed, "ticket": app.STORE.get_ticket(ticket_id)})
        elif path == "/api/workflow-transfer":
            ok = qw.transfer_ticket(app.STORE, int(form.get("ticket_id", "0") or 0), form.get("employee", ""), form.get("reason", ""), app.work_actor())
            handler.json_response({"updated": ok}, 200 if ok else 404)
        elif path == "/api/workflow-ticket-merge":
            ok = qw.merge_tickets(app.STORE, int(form.get("source_id", "0") or 0), int(form.get("target_id", "0") or 0), app.work_actor(), form.get("note", ""))
            handler.json_response({"updated": ok})
        elif path == "/api/workflow-ticket-split":
            new_id = qw.split_ticket(app.STORE, int(form.get("source_id", "0") or 0), app.work_actor(), form.get("title", ""), form.get("summary", ""), form.get("category", ""), form.get("priority", ""))
            handler.json_response({"created": True, "ticket_id": new_id, "href": f"/ticket?id={new_id}"})
        elif path == "/api/workflow-bulk":
            try:
                ids = sorted({int(x) for x in json.loads(form.get("ticket_ids", "[]")) if int(x) > 0})[:200]
            except Exception:
                ids = []
            actor = app.work_actor(); updated = failed = 0
            status = form.get("status", ""); priority = form.get("priority", ""); employee = form.get("employee", "")
            reason = form.get("reason", ""); comment = form.get("comment", "")
            for ticket_id in ids:
                try:
                    ticket = app.STORE.get_ticket(ticket_id)
                    if not ticket: raise ValueError("not found")
                    changed = False
                    if priority: changed = bool(app.STORE.update_priority(ticket_id, priority, actor)) or changed
                    if employee and employee != str(ticket.get("assigned_to", "") or ""): changed = qw.transfer_ticket(app.STORE, ticket_id, employee, reason, actor) or changed
                    if status and status != str(ticket.get("status", "") or ""):
                        valid, why = qw.validate_close(app.STORE, ticket_id, status, reason, comment)
                        if not valid: raise ValueError(why)
                        assignee = employee or str(ticket.get("assigned_to", "") or "") or actor
                        ok, _ = app.update_ticket_status(
                            ticket_id, status, actor, assignee,
                            allow_reopen=handler.is_admin(), close_reason=reason, close_comment=comment,
                        )
                        if not ok: raise ValueError("status")
                        changed = True
                    updated += int(changed)
                except Exception:
                    failed += 1
            handler.json_response({"selected":len(ids),"updated":updated,"failed":failed})
        elif path == "/admin/workflow":
            action = form.get("action", "")
            if action == "retention":
                settings = qw.save_retention_settings(app.STORE, int(form.get("messages","20") or 20), int(form.get("media","20") or 20), int(form.get("tickets","20") or 20))
                notice = f"Сроки сохранены: сообщения {settings['messages']} дн., медиа {settings['media']} дн., заявки {settings['tickets']} дн."
            elif action == "media_cleanup":
                result = qw.manual_media_cleanup(app.STORE, form.get("kind","all"), int(float(form.get("min_mb","0") or 0) * 1024 * 1024), int(form.get("older_days","0") or 0))
                notice = f"Очищено файлов: {result['files']}, освобождено: {_human_bytes(app, result['bytes'])}"
            elif action == "close_categories":
                values = [key.split("required_category__",1)[1] for key,value in form.items() if key.startswith("required_category__") and value in {"1","on","true"}]
                qw.set_close_comment_categories(app.STORE, values)
                notice = "Правило обязательного комментария сохранено"
            else:
                notice = "Неизвестное действие"
            handler.redirect("/admin/workflow?notice=" + quote(notice))
    except Exception as exc:
        if path == "/admin/workflow":
            handler.redirect("/admin/workflow?notice=" + quote("Ошибка: " + str(exc)[:220]))
        else:
            handler.json_response({"error": str(exc)[:300]}, 400)
    return True


def post(handler, app):
    # Delay the response until the whole mutation has committed.
    guarded = {"/api/workflow-ticket-quick", "/api/workflow-transfer",
               "/api/workflow-ticket-merge", "/api/workflow-ticket-split", "/api/workflow-bulk"}
    path = urlparse(handler.path).path
    if path not in guarded:
        return _post_impl(handler, app)
    form = handler.read_form()
    if not _csrf(handler, app, form):
        return True
    from queue_operations import Conflict
    read_form, respond = handler.read_form, handler.json_response
    replies = []
    try:
        revisions = json.loads(form.get("revisions", "{}"))
        if not isinstance(revisions, dict): raise ValueError("Некорректные версии заявок")
        if path.endswith("-bulk"):
            ids = sorted({int(x) for x in json.loads(form.get("ticket_ids", "[]"))})
            if not ids or len(ids) > 200: raise ValueError("Выберите от 1 до 200 заявок")
        elif path.endswith("-merge"):
            ids = [int(form.get("source_id", 0)), int(form.get("target_id", 0))]
        else:
            ids = [int(form.get("source_id") or form.get("ticket_id") or 0)]
        with app.STORE.atomic_inbound():
            for tid in ids:
                ticket = app.STORE.get_ticket(tid)
                if not ticket: raise ValueError("Заявка не найдена")
                if int(revisions.get(str(tid), -1)) != ticket["revision"]:
                    raise Conflict("Заявку уже изменили. Обновите страницу и проверьте актуальные данные")
            handler.read_form = lambda: form
            handler.json_response = lambda data, status=200: replies.append((data, status))
            _post_impl(handler, app)
            if not replies: raise RuntimeError("No operation response")
            data, status = replies[-1]
            if status >= 400 or data.get("failed"):
                raise ValueError(data.get("error") or "Не все заявки доступны для изменения. Изменения отменены для всей группы")
        app.queue_realtime.notify('ticket')
        app.queue_realtime.notify_outbound()
    except Conflict as error:
        data, status = {"error": str(error)}, 409
    except (ValueError, TypeError) as error:
        data, status = {"error": str(error)}, 400
    except Exception:
        data, status = {"error": "Изменения не сохранены. Повторите позже"}, 503
    finally:
        handler.read_form, handler.json_response = read_form, respond
    respond(data, status)
    return True
