"""Правила для текста входящих сообщений: команды меню, короткие подтверждения, краткое описание заявки."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from queue_core import normalize_message
from ticketing import CATEGORIES


def _extract_value(text: str, patterns: list[str]) -> str:
    for raw_pattern in patterns:
        match = re.search(raw_pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip(" .,;:-")
    return ""


def is_main_menu_command(text: str) -> bool:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    return normalized in {
        "0", "меню", "главное меню", "назад", "сначала", "отмена",
        "вернуться", "вернуться назад", "начать заново", "мню", "менб", "менюу",
        "мәзір", "негізгі мәзір", "артқа", "басынан", "қайта бастау",
    }


def is_acknowledgement(text: str) -> bool:
    normalized = normalize_message(text).casefold()
    normalized = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return bool(
        re.fullmatch(
            r"(?:спасибо(?:\s+большое)?|спс|благодарю|рахмет(?:\s+большое)?|рақмет|"
            r"ок(?:ей)?|понял(?:а)?|хорошо|ясно|принято|готово|түсіндім|жақсы|жарайды)",
            normalized,
        )
    )


def general_category(text: str, forced_category: str = "", *,
                     category_labels: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Choose only an explicitly configured category, never a legacy industry subtype.

    Category identifiers remain valid for reading historical tickets.
    """
    labels = CATEGORIES if category_labels is None else category_labels
    category = forced_category if forced_category in labels else "general"
    return category, str(labels.get(category) or "Общее обращение")


def detect_priority(text: str, category: str) -> str:
    lower = normalize_message(text).casefold()
    if re.search(r"(?:срочн|авари|критич|массов|у\s+всех|простой)", lower):
        return "urgent"
    if category == "incident":
        return "high"
    return "normal"


def build_general_summary(
    text: str,
    forced_category: str = "",
    *, category_labels: Mapping[str, str] | None = None,
) -> tuple[str, str, str]:
    normalized = normalize_message(text)
    category, title = general_category(normalized, forced_category, category_labels=category_labels)

    error_code = _extract_value(
        normalized,
        [r"(?:код\s+ошибки|ошибка)\s*[:№#-]?\s*([A-ZА-Я0-9_-]{3,})"],
    )

    details: list[str] = []
    if error_code:
        details.append(f"Ошибка: {error_code}")

    compact = re.sub(r"\s+", " ", normalized).strip()
    if len(compact) > 220:
        compact = compact[:217].rstrip() + "..."

    if details:
        summary = " · ".join(details)
        if compact and compact.lower() not in summary.lower():
            summary += f"\n{compact}"
    else:
        summary = compact or "Сообщение без текста"

    return category, title, summary


def process_incoming_message(
    sender: str,
    phone: str,
    text: str,
    attachment_name: str = "",
    chat_id: str = "",
    external_id: str = "",
    forced_category: str = "",
    *, category_labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized = normalize_message(text)
    # Явно выбранная пользователем категория всегда имеет приоритет над
    # автоопределением по тексту.
    category, title, summary = build_general_summary(normalized, forced_category, category_labels=category_labels)
    if attachment_name:
        if normalized:
            summary += f"\nВложение: {attachment_name}"
        else:
            summary = f"Получено вложение: {attachment_name}"
    return {
        "created": True,
        "ticket": {
            "source": "whatsapp",
            "sender": sender or "Неизвестный отправитель",
            "phone": phone,
            "chat_id": chat_id,
            "external_id": external_id,
            "category": category,
            "priority": detect_priority(normalized, category),
            "title": title,
            "summary": summary,
            "original_text": normalized,
            "attachment_name": attachment_name,
        },
    }
