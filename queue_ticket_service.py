"""Canonical ticket replies and transactional status changes.

Dependencies are supplied by app.py; no application globals are injected here.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
import queue_user_locale
import queue_workflow
from queue_user_locale import tr
from queue_core import normalize_phone
from ticketing import TicketStore, normalize_message


@dataclass(frozen=True)
class TicketDependencies:
    store: TicketStore
    statuses: dict
    categories: dict
    menu_context: str
    close_reason_labels_kz: dict
    contact_language: Callable[[str, str], str]
    auto_reply_enabled: Callable[[], bool]
    main_menu: Callable[[], str]
    menu_label: Callable[[str, str], str]
    status_label: Callable[[str, str], str]


class TicketService:
    def __init__(self, dependencies: TicketDependencies):
        self.deps = dependencies

    def ticket_accepted_reply(self, ticket_id: int) -> str:
        ticket = self.deps.store.get_ticket(ticket_id) or {}
        key = str(ticket.get("category", ""))
        category = self.deps.menu_label(key, self.deps.categories.get(key, "")) if key else ""
        category_line = tr(f"Категория: {category}\n", f"Санат: {category}\n") if category else ""
        return tr(
            f"✅ Заявка №{ticket_id} принята и передана в работу!\n{category_line}"
            "Ответ специалиста поступит в этот чат. Дополнительную информацию, фото или документы можно отправлять прямо сюда.\n"
            "(Для регистрации новой заявки отправьте 0)",
            f"✅ №{ticket_id} өтініміңіз қабылданып, жұмысқа берілді!\n{category_line}"
            "Маманның жауабы осы чатқа келеді. Қосымша ақпаратты, фото немесе құжаттарды осында жібере аласыз.\n"
            "(Жаңа өтінім жасау үшін 0 жіберіңіз)",
        )

    def active_tickets_reply(self, tickets: list[dict[str, object]]) -> str:
        if not tickets:
            return tr(
                "У вас сейчас нет активных заявок. (0 — главное меню)",
                "Қазіргі уақытта белсенді өтінімдеріңіз жоқ. (0 — негізгі мәзір)",
            )
        rows: list[str] = []
        for ticket in tickets:
            ticket_id = int(ticket.get("id", 0) or 0)
            category_key = str(ticket.get("category", ""))
            status_key = str(ticket.get("status", ""))
            category = self.deps.menu_label(category_key, self.deps.categories.get(category_key, category_key))
            status = self.deps.status_label(status_key, self.deps.statuses.get(status_key, status_key))
            summary = normalize_message(str(ticket.get("summary", "") or ticket.get("original_text", ""))).replace("\n", " ")
            if len(summary) > 100:
                summary = summary[:97].rstrip() + "..."
            suffix = f" — {summary}" if summary else ""
            rows.append(f"№{ticket_id} · {category} · {status}{suffix}")
        body = "\n".join(rows)
        return tr(
            f"Ваши активные заявки:\n{body}\n"
            "Отправьте номер заявки из списка, чтобы прикрепить к ней новые фото или текст.\n"
            "(0 — главное меню)",
            f"Сіздің белсенді өтінімдеріңіз:\n{body}\n"
            "Жаңа фото немесе мәтін тіркеу үшін тізімнен өтінім нөмірін жіберіңіз.\n"
            "(0 — негізгі мәзір)",
        )

    def active_ticket_selected_reply(self, ticket: dict[str, object]) -> str:
        ticket_id = int(ticket.get("id", 0) or 0)
        return tr(
            f"Выбрана заявка №{ticket_id}. Отправьте новый текст, фото или документ — он будет прикреплён к этой заявке.\n(0 — главное меню)",
            f"№{ticket_id} өтінім таңдалды. Жаңа мәтін, фото немесе құжат жіберіңіз — ол осы өтінімге тіркеледі.\n(0 — негізгі мәзір)",
        )

    def support_question_accepted_reply(self, ticket_id: int) -> str:
        return tr(
            f"Вопрос №{ticket_id} передан в службу поддержки. Ответ сотрудника придёт в этот чат.\n\n"
            "Дополнительные сообщения, фотографии и файлы можно отправлять сюда, они будут добавлены к этому вопросу.\n"
            "Чтобы задать другой вопрос, отправьте 0 или слово «меню».",
            f"№{ticket_id} сұрақ қолдау қызметіне жіберілді. Қызметкердің жауабы осы чатқа келеді.\n\n"
            "Қосымша хабарламаларды, фотоларды және файлдарды осы жерге жібере аласыз, олар осы сұраққа тіркеледі.\n"
            "Басқа сұрақ қою үшін 0 немесе «мәзір» сөзін жіберіңіз.",
        )

    def status_reply_text(
        self,
        status: str,
        ticket_id: int,
        close_reason: str = "",
        close_comment: str = "",
    ) -> str:
        """Return the exact user-facing close message used by WhatsApp."""
        reason_key = normalize_message(str(close_reason or "")).strip()
        reason_label = queue_workflow.ALLOWED_CLOSE_REASONS.get(reason_key, "") or reason_key
        comment_text = normalize_message(str(close_comment or "")).strip()
        if status == "done":
            comment_value = comment_text or "—"
            return tr(
                f"🟢 Заявка №{ticket_id} выполнена!\nКомментарий: {comment_value}\n(Нужна помощь по другому вопросу? Отправьте 0)",
                f"🟢 №{ticket_id} өтінім орындалды!\nПікір: {comment_value}\n(Басқа мәселе бойынша көмек қажет пе? 0 жіберіңіз)",
            )
        if status == "invalid":
            if queue_user_locale.is_kz() and reason_label:
                reason_label = self.deps.close_reason_labels_kz.get(reason_label, reason_label)
            reason_value = reason_label or tr("Не указана", "Көрсетілмеген")
            comment_value = comment_text or "—"
            return tr(
                f"🔴 Заявка №{ticket_id} закрыта.\nПричина: {reason_value}\nКомментарий: {comment_value}\n(0 — главное меню)",
                f"🔴 №{ticket_id} өтінім жабылды.\nСебеп: {reason_value}\nПікір: {comment_value}\n(0 — негізгі мәзір)",
            )
        return ""

    def _status_operation(
        self,
        ticket_id: int,
        status: str,
        actor: str,
        assigned_to: str,
        allow_reopen: bool = False,
        close_reason: str = "",
        close_comment: str = "",
    ) -> tuple[bool, int]:
        before = self.deps.store.get_ticket(ticket_id)
        if not before or status not in self.deps.statuses:
            return False, 0
        previous_status = str(before.get("status", ""))
        if previous_status in {"done", "invalid"} and status != previous_status and not allow_reopen:
            return False, 0
        if previous_status == status:
            return True, 0
        updated = self.deps.store.update_status(ticket_id, status, actor, assigned_to)
        if not updated or before.get("source") != "whatsapp":
            return updated, 0

        previous_language = queue_user_locale.current_language()
        ticket_language = self.deps.contact_language(
            str(before.get("chat_id", "")), str(before.get("phone", ""))
        ) or queue_user_locale.LANG_RU
        queue_user_locale.set_language(ticket_language)
        body = ""
        try:
            body = self.status_reply_text(
                status, ticket_id, close_reason=close_reason, close_comment=close_comment
            )
        finally:
            queue_user_locale.set_language(previous_language)
            contact_key = str(before.get("chat_id", "")) or normalize_phone(str(before.get("phone", "")))
            self.deps.store.set_conversation_context(contact_key, self.deps.menu_context, ticket_id, True)
        message_id = self.deps.store.queue_outbound_message(ticket_id, body, "Система") if body and self.deps.auto_reply_enabled() else 0
        return updated, message_id

    def update_status(
        self, ticket_id: int, status: str, actor: str, assigned_to: str,
        allow_reopen: bool = False, close_reason: str = "", close_comment: str = "",
    ) -> tuple[bool, int]:
        store = self.deps.store
        with store.atomic_inbound():
            before = store.get_ticket(ticket_id)
            result = self._status_operation(ticket_id, status, actor, assigned_to,
                                            allow_reopen, close_reason, close_comment)
            updated, message_id = result
            if updated and before and before['status'] != status:
                queue_workflow.record_close_meta(store, ticket_id, status, close_reason, close_comment, actor)
                if status in {'done', 'invalid'}:
                    reason = '' if message_id else (
                        'Автоответы отключены' if not self.deps.auto_reply_enabled()
                        else 'Нет адреса WhatsApp или заявка создана вручную'
                    )
                    with store.connection() as db:
                        db.execute(
                            'INSERT INTO ticket_notifications(ticket_id,status,message_id,reason) VALUES(?,?,?,?)',
                            (ticket_id, status, message_id, reason),
                        )
        return result
