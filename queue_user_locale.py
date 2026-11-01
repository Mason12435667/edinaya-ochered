from __future__ import annotations

import re
import threading

LANG_RU = "ru"
LANG_KZ = "kz"
SUPPORTED_LANGUAGES = {LANG_RU, LANG_KZ}
_local = threading.local()


def normalize_language(value: str, default: str = LANG_RU) -> str:
    value = str(value or "").strip().casefold()
    if value in {"ru", "rus", "рус", "русский", "орыс", "орысша"}:
        return LANG_RU
    if value in {"kz", "kk", "kaz", "қаз", "қазақ", "қазақша", "каз", "казах", "казахский"}:
        return LANG_KZ
    return default if default in SUPPORTED_LANGUAGES else LANG_RU


def set_language(value: str) -> str:
    lang = normalize_language(value)
    _local.lang = lang
    return lang


def current_language(default: str = LANG_RU) -> str:
    lang = getattr(_local, "lang", "")
    return lang if lang in SUPPORTED_LANGUAGES else normalize_language(default)


def is_kz() -> bool:
    return current_language() == LANG_KZ


def tr(ru: str, kz: str) -> str:
    return kz if is_kz() else ru


def choose(ru_value, kz_value):
    return kz_value if is_kz() else ru_value


def language_selection_text() -> str:
    return (
        "Выберите язык / Тілді таңдаңыз\n\n"
        "1. Русский\n"
        "2. Қазақша\n\n"
        "Отправьте 1 или 2 / 1 немесе 2 санын жіберіңіз"
    )


def parse_language_choice(text: str) -> str:
    value = re.sub(r"[\s.,!?:;()\[\]{}]+", " ", str(text or "").casefold()).strip()
    if value in {"1", "русский", "рус", "ru", "rus", "орысша", "орыс"}:
        return LANG_RU
    if value in {"2", "қазақша", "қазақ", "қаз", "kz", "kk", "kaz", "казахский", "казах", "каз"}:
        return LANG_KZ
    return ""


def is_language_change_command(text: str) -> bool:
    value = re.sub(r"[\s.,!?:;()\[\]{}]+", " ", str(text or "").casefold()).strip()
    return value in {
        "язык", "сменить язык", "изменить язык", "выбрать язык", "language", "change language",
        "тіл", "тілді өзгерту", "тіл таңдау", "тілді таңдау",
    }


USER_MENU_LABELS_KZ = {'general': 'Жалпы өтініш', 'support': 'Қолдау қызметіне сұрақ', 'incident': 'Мәселе туралы хабарлау', 'database': 'Жүйеге қолжетімділік', 'mobile': 'Мобильді қосымша'}

USER_STATUS_LABELS_KZ = {
    "new": "Жаңа",
    "in_progress": "Жұмыста",
    "done": "Орындалды",
    "invalid": "Жарамсыз",
}

USER_CLOSE_REASON_LABELS_KZ = {
    "Решено": "Шешілді",
    "Дубль": "Қайталанған өтінім",
    "Недействительно": "Жарамсыз",
    "Ошибка пользователя": "Пайдаланушы қатесі",
    "Передано": "Берілді",
    "Другое": "Басқа",
}

USER_CATEGORY_PROMPTS_KZ = {'general': 'Мәселені немесе сұрақты сипаттаңыз. Қажет болса, скриншот немесе файл тіркеңіз.\n0 — негізгі мәзірге оралу'}


def user_menu_label(category: str, fallback: str = "") -> str:
    return USER_MENU_LABELS_KZ.get(str(category), fallback or str(category)) if is_kz() else (fallback or str(category))


def user_status_label(status: str, fallback: str = "") -> str:
    return USER_STATUS_LABELS_KZ.get(str(status), fallback or str(status)) if is_kz() else (fallback or str(status))


def close_reason_label(reason: str) -> str:
    return USER_CLOSE_REASON_LABELS_KZ.get(str(reason), str(reason)) if is_kz() else str(reason)
