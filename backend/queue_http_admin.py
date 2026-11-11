"""Маршруты админки: страницы и их данные, сотрудники, контакты, группы, категории, система."""
from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import quote

import queue_reliability
import queue_admin_views
import queue_workflow
from queue_core import display_phone, normalize_message, normalize_phone, valid_chat_id, valid_group_id
from queue_store_conversations import MENU_CONTEXT
from queue_work_context import active_employee, assignment_names, work_actor
from ticketing import STATUSES


class AdminRoutes:
    def _get_admin_home(self, query) -> None:
        self.page_response("admin")

    def _get_admin_users(self, query) -> None:
        self.page_response("admin")

    def _get_admin_employees(self, query) -> None:
        self.page_response("admin")

    def _get_admin_manual(self, query) -> None:
        self.page_response("admin")

    def _get_admin_menu(self, query) -> None:
        self.page_response("admin")

    def _get_admin_categories(self, query) -> None:
        self.page_response("admin")

    def _get_admin_errors(self, query) -> None:
        self.page_response("admin")

    def _get_admin_error(self, query) -> None:
        self.page_response("admin")

    def _get_admin_contacts(self, query) -> None:
        self.page_response("admin")

    def _get_admin_audit(self, query) -> None:
        self.page_response("admin")

    def _get_admin_manager(self, query) -> None:
        self.page_response("admin")

    def _get_admin_api_keys(self, query) -> None:
        self.page_response("admin")

    def _get_admin_tickets_export(self, query) -> None:
        rows = self.deps.final_ticket_filter(query)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["ID","Статус","Категория","Приоритет","Сотрудник","Создано","Обновлено","Описание"])
        for item in rows:
            writer.writerow([
                int(item.get("id",0) or 0),
                str(item.get("status") or ""),
                str(item.get("category") or item.get("title") or ""),
                str(item.get("priority") or ""),
                str(item.get("assigned_to") or ""),
                str(item.get("created_at") or ""),
                str(item.get("updated_at") or ""),
                str(item.get("description") or item.get("problem") or ""),
            ])
        self.csv_bytes_response(("﻿" + stream.getvalue()).encode("utf-8"), "tickets.csv")

    def _get_admin_system(self, query) -> None:
        self.page_response("admin")

    def _get_api_admin_contact_search(self, query) -> None:
        search_raw = query.get("q", [""])[0]
        self.json_response({"rows": self.admin_pages.discovered_contacts(search_raw, 80)})

    def _get_api_admin_page(self, query) -> None:
        page = query.get("page", [""])[0]
        providers = {
            "users": self.deps.admin_users_data,
            "analytics": self.productivity_routes.analytics_data,
            "backups": self.productivity_routes.backups_data,
            "reliability": self.reliability_routes.page_data,
            "workflow": self.workflow_routes.admin_data,
            "shift": self.feature_routes.shift_data,
            "api-keys": lambda: self.admin_pages.api_keys_data(int((self.effective_user() or {}).get("id", 0) or 0)),
        }
        data = providers[page]() if page in providers else self.admin_pages.page_data(page, query)
        if data is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.json_response({**data, "tabs": [{"key": key, "href": href, "label": label} for key, href, label in queue_admin_views.ADMIN_TABS]})

    def _get_admin_groups(self, query) -> None:
        self.page_response("groups")

    def _get_to_admin_home(self, query) -> None:
        self.redirect("/admin/")

    def _post_admin_api_keys(self, parsed) -> None:
        # POST /admin/api-keys
        if not self.require_admin(): return
        form=self.read_form()
        if form is None: return
        action=str(form.get("action") or "").strip().lower(); actor=self.effective_user() or {}
        if action == "create":
            ok,message,token=self.deps.auth.create_api_key(actor,form.get("name",""),"read")
            if ok: self.admin_pages.remember_new_api_key(int(actor.get("id",0) or 0),token)
            self.redirect("/admin/api-keys?notice="+quote(message)); return
        if action == "revoke":
            try: key_id=int(form.get("key_id",0) or 0)
            except ValueError: key_id=0
            ok,message=self.deps.auth.revoke_api_key(actor,key_id); self.redirect("/admin/api-keys?notice="+quote(message)); return
        self.redirect("/admin/api-keys?notice="+quote("Неизвестное действие"))

    def _post_api_system_warning_action(self, parsed) -> None:
        # POST /api/system-warning-action
        payload = self.read_json_body(120_000)
        if payload is None:
            return
        action = str(payload.get("action") or "").strip().lower()
        event_id = str(payload.get("event_id") or "").strip()
        try:
            if action == "dismiss":
                if not queue_reliability.dismiss_notification(self.deps.store, event_id):
                    self.json_response({"error":"invalid_warning_id"}, HTTPStatus.BAD_REQUEST)
                    return
                self.json_response({"ok":True,"dismissed":1})
                return
            if action == "dismiss_all":
                snapshot = self.chat_views.notifications_snapshot()
                ids = [str(item.get("id") or item.get("event_id") or "") for item in snapshot.get("events",[]) if isinstance(item,dict)]
                count = queue_reliability.dismiss_notifications(self.deps.store, ids)
                self.json_response({"ok":True,"dismissed":count})
                return
            if action in {"check","check_all"}:
                self.deps.reliability.refresh_active_alerts()
                self.json_response({"ok":True,"checked":True})
                return
        except Exception as error:
            self.json_response({"error":str(error)[:180]}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.json_response({"error":"invalid_action"}, HTTPStatus.BAD_REQUEST)

    def _post_admin_users(self, parsed, form) -> None:
        # POST /admin/users
        if not self.require_admin():
            return
        action = form.get("action", "create")
        actor_user = self.effective_user() or {}
        object_id = str(form.get("user_id", "") or "")
        if action == "create":
            temporary = form.get("temporary_password", "") == "1"
            ok, notice, created_id = self.deps.auth.create_user(
                form.get("username", ""), form.get("display_name", ""), form.get("password", ""),
                role=form.get("role", "employee"), must_change_password=temporary,
            )
            object_id = str(created_id or "")
        elif action == "toggle":
            ok, notice = self.deps.auth.set_enabled(self.form_int(form, "user_id"), form.get("enabled", "0") == "1")
        elif action == "role":
            ok, notice = self.deps.auth.set_role(self.form_int(form, "user_id"), form.get("role", "employee"))
        elif action == "password":
            ok, notice = self.deps.auth.set_password(self.form_int(form, "user_id"), form.get("password", ""), must_change_password=form.get("temporary_password", "") == "1")
        elif action == "theme":
            theme = str(form.get("theme", "") or "").strip().lower()
            ok, notice = self.deps.auth.set_user_preference(self.form_int(form, "user_id"), "theme", theme)
            if ok:
                notice = "Тема пользователя сохранена"
        elif action == "employee_link":
            employee_name = form.get("employee_name", "")
            if employee_name and employee_name not in self.deps.employees:
                ok, notice = False, "Сотрудник не найден в списке"
            else:
                ok, notice = self.deps.auth.set_employee_link(self.form_int(form, "user_id"), employee_name)
        elif action == "delete":
            target_user_id = self.form_int(form, "user_id")
            target_user = self.deps.auth.get_user(target_user_id) if target_user_id > 0 else None
            actor_user_id = int(actor_user.get("id", 0) or 0)
            if actor_user_id > 0 and target_user_id == actor_user_id:
                ok, notice = False, "Нельзя удалить собственную учётную запись"
            elif not target_user:
                ok, notice = False, "Пользователь не найден"
            else:
                target_name = str(target_user.get("username") or target_user.get("display_name") or target_user_id)
                ok, delete_notice = self.deps.auth.delete_user(target_user_id)
                notice = (
                    f"Пользователь @{target_name} удалён. Его активные сессии завершены"
                    if ok else delete_notice
                )
        elif action == "revoke_session":
            ok = self.deps.auth.revoke_session_hash(form.get("session_hash", ""))
            notice = "Сессия завершена" if ok else "Сессия уже завершена или не найдена"
            object_id = str(form.get("session_hash", ""))[:12]
        else:
            ok, notice = False, "Неизвестное действие"
        if ok:
            self.deps.auth.record_audit(actor_user, f"user_{action}", object_type="auth", object_id=object_id, details=notice)
        prefix = "" if ok else "Ошибка: "
        self.redirect(f"/admin/users?notice={quote(prefix + notice)}")

    def _post_admin_employees(self, parsed, form) -> None:
        # POST /admin/employees
        if not self.require_admin():
            return
        try:
            count = int(form.get("employee_count", "0"))
        except ValueError:
            count = 0
        if count < 1 or count > self.deps.max_employees:
            self.redirect(f"/admin/employees?notice={quote('Некорректное количество сотрудников')}")
            return
        names = [
            normalize_message(form.get(f"employee_{index}", ""))[:60].strip()
            for index in range(1, count + 1)
        ]
        if any(not name for name in names):
            self.redirect(f"/admin/employees?count={count}&notice={quote('Все имена должны быть заполнены')}")
            return
        if len({name.casefold() for name in names}) != len(names):
            self.redirect(f"/admin/employees?count={count}&notice={quote('Имена сотрудников должны отличаться')}")
            return
        old_names = list(self.deps.employees)
        old_active = active_employee()
        self.deps.store.set_setting("employees_json", json.dumps(names, ensure_ascii=False))
        self.deps.employees[:] = names
        if old_active in old_names:
            old_index = old_names.index(old_active)
            if old_index < len(names):
                self.deps.store.set_setting("active_employee", names[old_index])
            else:
                self.deps.store.set_setting("active_employee", names[0])
        elif old_active not in names:
            self.deps.store.set_setting("active_employee", names[0])
        self.redirect(f"/admin/employees?notice={quote('Список сотрудников сохранён')}")

    def _post_admin_ticket_delete(self, parsed, form) -> None:
        # POST /admin/ticket-delete
        if not self.require_admin():
            return
        ticket_id = self.form_int(form, "ticket_id")
        deleted = self.deps.store.delete_ticket(ticket_id, f"Администратор · {work_actor()}")
        notice = (
            f"Заявка №{ticket_id} удалена только из системы. Сообщение пользователю не отправлялось"
            if deleted else "Заявка не найдена"
        )
        self.redirect(f"/?notice={quote(notice)}")

    def _post_admin_contacts(self, parsed, form) -> None:
        # POST /admin/contacts
        if not self.require_admin():
            return
        action = form.get("action", "save")
        if action == "delete":
            contact_chat_id = valid_chat_id(form.get("chat_id", ""))
            existing_contact = self.deps.store.manual_whatsapp_contact(contact_chat_id, "")
            deleted = self.deps.store.delete_whatsapp_contact(contact_chat_id)
            if deleted:
                # После удаления из исключений следующий текст снова должен
                # запускать обычную автоматику, а не продолжать старый контекст
                # заявки, существовавший до добавления пользователя в контакты.
                reset_keys = {contact_chat_id}
                if existing_contact:
                    contact_phone = normalize_phone(str(existing_contact.get("phone", "")))
                    if contact_phone:
                        reset_keys.add(f"{contact_phone.lstrip('+')}@c.us")
                for reset_key in reset_keys:
                    if reset_key:
                        self.deps.store.set_conversation_context(reset_key, MENU_CONTEXT, 0, False)
            notice = "Контакт удалён. Автообработка для него снова включена" if deleted else "Контакт не найден"
            self.redirect(f"/admin/contacts?notice={quote(notice)}")
            return
        name = normalize_message(form.get("name", ""))[:100]
        phone = normalize_phone(form.get("phone", ""))
        chat_id = f"{phone.lstrip('+')}@c.us" if phone else ""
        existing = self.deps.store.manual_whatsapp_contact(chat_id, phone) if phone else None
        if existing:
            notice = f"Номер {display_phone(phone)} уже добавлен как {existing.get('name') or 'контакт'}"
            self.redirect(f"/admin/contacts?notice={quote(notice)}")
            return
        saved = self.deps.store.save_whatsapp_contact(name, phone, chat_id)
        notice = "Контакт добавлен" if saved else "Проверьте имя и мобильный номер"
        self.redirect(f"/admin/contacts?notice={quote(notice)}")

    def _post_admin_manual(self, parsed, form) -> None:
        # POST /admin/manual
        if not self.require_admin():
            return
        employee = form.get("employee", "")
        if employee not in assignment_names():
            employee = work_actor()
        category = form.get("category", "general")
        if category not in self.deps.categories:
            category = "general"
        status = form.get("status", "new")
        if status not in STATUSES:
            status = "new"
        ticket_id = self.deps.store.create_ticket(
            {
                "source": "admin_manual",
                "sender": employee,
                "category": category,
                "priority": form.get("priority", "normal"),
                "title": normalize_message(form.get("title", ""))[:160] or "Ручная заявка",
                "summary": normalize_message(form.get("summary", ""))[:2000],
                "original_text": normalize_message(form.get("summary", ""))[:2000],
                "status": status,
                "assigned_to": employee,
            }
        )
        self.redirect(f"/ticket?id={ticket_id}")

    def _post_admin_system(self, parsed, form) -> None:
        # POST /admin/system
        if not self.require_admin():
            return
        action = form.get("action", "")
        if action == "cleanup":
            result = queue_workflow.cleanup_retention(self.deps.store)
            notice = f"Очистка завершена: сообщений {result.get('messages',0)}, медиа {result.get('media_rows',0)}, заявок {result.get('tickets',0)}"
        elif action == "retry_failed":
            count = self.deps.store.retry_failed_outbound()
            notice = f"На повторную отправку возвращено: {count}"
        elif action == "backup_now":
            ok, info = self.deps.create_scheduled_backup("manual_admin")
            notice = f"Резервная копия создана: {info}" if ok else f"Ошибка резервной копии: {info}"
        elif action == "backup_settings":
            try:
                hours=max(1,min(168,int(form.get("backup_hours","24"))))
                keep=max(2,min(90,int(form.get("backup_keep","14"))))
                self.deps.store.set_setting("scheduled_backup_hours",str(hours)); self.deps.store.set_setting("scheduled_backup_keep",str(keep))
                notice=f"Расписание сохранено: каждые {hours} ч., хранить {keep} копий"
            except ValueError:
                notice="Некорректные параметры расписания"
        else:
            notice = "Неизвестное действие"
        self.deps.auth.record_audit(self.effective_user() or {}, f"system_{action}", object_type="system", details=notice)
        try:
            with self.deps.monitor_log_path.open("a", encoding="utf-8") as log:
                log.write(f"{datetime.now(timezone.utc).isoformat()} ADMIN {notice}\n")
        except OSError:
            pass
        self.redirect(f"/admin/system?notice={quote(notice)}")

    def _post_admin_groups(self, parsed, form) -> None:
        # POST /admin/groups
        if not self.require_admin():
            return
        action = form.get("action", "save")
        if action == "refresh":
            self.deps.store.set_setting("group_refresh_request", secrets.token_urlsafe(18))
            notice = "Запрос отправлен QR-коннектору. Список обновится примерно через 5–7 секунд"
            self.redirect(f"/groups?notice={quote(notice)}&refresh=1")
            return
        if action == "delete":
            deleted = self.deps.store.delete_whatsapp_group(valid_group_id(form.get("chat_id", "")))
            notice = "Группа удалена" if deleted else "Группа не найдена"
        elif action == "add_discovered":
            chat_id = valid_group_id(form.get("chat_id", ""))
            discovered = next(
                (item for item in self.deps.store.list_discovered_whatsapp_groups() if str(item.get("chat_id", "")) == chat_id),
                None,
            )
            saved = self.deps.store.save_whatsapp_group(str(discovered.get("name", "")), chat_id) if discovered else None
            notice = "Группа добавлена" if saved else "Группа не найдена в списке WhatsApp"
        else:
            saved = self.deps.store.save_whatsapp_group(
                form.get("name", ""),
                valid_group_id(form.get("chat_id", "")),
            )
            notice = "Группа добавлена" if saved else "Укажите название и корректный WhatsApp ID группы"
        self.redirect(f"/groups?notice={quote(notice)}")

    def _post_admin_errors(self, parsed, form) -> None:
        # POST /admin/errors
        if not self.require_admin():
            return
        report_id = self.form_int(form, "report_id")
        status = form.get("status", "new")
        admin_note = normalize_message(form.get("admin_note", ""))[:3000]
        updated = self.deps.store.update_error_report(report_id, status, admin_note, f"Администратор · {work_actor()}")
        notice = "Репорт обновлён" if updated else "Репорт не найден"
        self.redirect(f"/admin/error?id={report_id}&notice={quote(notice)}")

    def _post_admin_categories(self, parsed, form) -> None:
        if not self.require_admin():
            return
        try:
            self.deps.menu_config.update_from_form(form)
        except ValueError as error:
            self.redirect(f"/admin/categories?notice={quote(str(error))}")
            return
        self.redirect(f"/admin/categories?notice={quote('Сохранено. Меню бота обновлено')}")

    def _post_active_employee(self, parsed, form) -> None:
        # POST /active-employee
        self.json_response({"updated": False, "error": "Ручной выбор смены отключён. Сотрудник становится на смену автоматически после входа."}, HTTPStatus.GONE)
