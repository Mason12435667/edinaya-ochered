"""Pure bot prompts and dialog identifiers."""
import queue_user_locale
from queue_user_locale import tr
from ticketing import normalize_message

MENU_ACTION_TICKET = "ticket"

MENU_ACTION_INSTRUCTION = "instruction"

MENU_ACTIONS = {MENU_ACTION_TICKET, MENU_ACTION_INSTRUCTION}

SUPPORT_MODE_CONTEXT = "__support_mode__"

ERROR_REPORT_CONTEXT = "__error_report__"

ACTIVE_TICKETS_CONTEXT = "__active_tickets__"

ACTIVE_TICKET_FOLLOWUP_CONTEXT = "__active_ticket_followup__"

def error_report_prompt() -> str:
    return tr(
        "Вы выбрали категорию «Репорт об ошибке».\n\n"
        "Опишите, пожалуйста, последовательность действий и возникшую ошибку. При необходимости приложите скриншот, фото, видео или файл.\n\n"
        "Сообщение будет сохранено отдельно от обычных заявок и доступно сотрудникам в разделе «Ошибки».\n"
        "Для возврата в главное меню отправьте 0.",
        "Сіз «Қате туралы хабарлама» санатын таңдадыңыз.\n\n"
        "Орындаған әрекеттеріңізді және пайда болған қатені сипаттаңыз. Қажет болса, скриншот, фото, видео немесе файл тіркеңіз.\n\n"
        "Хабарлама қарапайым өтінімдерден бөлек сақталады және қызметкерлерге «Қателер» бөлімінде қолжетімді болады.\n"
        "Негізгі мәзірге оралу үшін 0 жіберіңіз.",
    )

def is_active_tickets_command(text: str) -> bool:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    return normalized in {
        "мои заявки", "мои активные заявки", "активные заявки",
        "показать заявки", "покажи заявки", "статус заявок",
        "менің өтінімдерім", "менің белсенді өтінімдерім", "белсенді өтінімдер",
    }

V3_FIELD_LABELS_RU = {
    "reference": "номер / идентификатор",
    "problem": "описание проблемы",
    "email": "email/логин пользователя",
}

V3_FIELD_LABELS_KZ = {
    "reference": "нөмір / идентификатор",
    "problem": "мәселенің сипаттамасы",
    "email": "пайдаланушының email/логині",
}

def language_selection_text_v3() -> str:
    return (
        "Здравствуйте! / Сәлеметсіз бе!\n"
        "Выберите язык обслуживания / Қызмет көрсету тілін таңдаңыз (1-2):\n"
        "1️⃣ — Русский\n"
        "2️⃣ — Қазақша\n\n"
        "После выбора языка укажите своё подразделение и должность / "
        "Тілді таңдағаннан кейін бөлімшеңіз бен лауазымыңызды көрсетіңіз."
    )

def active_tickets_menu_number() -> int:
    return 8

def v3_missing_fields_reply(missing_fields) -> str:
    fields = [str(item or "").strip() for item in (missing_fields or []) if str(item or "").strip()]
    labels_map = V3_FIELD_LABELS_KZ if queue_user_locale.is_kz() else V3_FIELD_LABELS_RU
    labels = [labels_map.get(field, field) for field in fields]
    joined = ", ".join(labels) if labels else tr("недостающие данные", "жетіспейтін мәліметтер")
    return tr(
        f"Пожалуйста, уточните: {joined} (остальные данные уже зафиксированы).",
        f"Нақтылап жіберіңізші: {joined} (қалған мәліметтер сақталды).",
    )

def v3_duplicate_reply(ticket_id: int) -> str:
    return tr(
        f"⚠️ Заявка по данному вопросу уже в работе (№{ticket_id}). Мы добавили ваше сообщение к ней, повторную заявку создавать не требуется.",
        f"⚠️ Бұл мәселе бойынша өтінім қазір жұмыс барысында (№{ticket_id}). Хабарламаңыз оған қосылды, жаңа өтінім жасаудың қажеті жоқ.",
    )
