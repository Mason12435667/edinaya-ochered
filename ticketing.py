from __future__ import annotations

import queue_message_identity
import queue_quote_lookup
import difflib
import json
import re
from queue_language import tolerant_labels
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


STATUSES = {
    "new": "Не тронута",
    "in_progress": "В работе",
    "done": "Сделано",
    "invalid": "Недействительная",
}

CATEGORIES = {
    "bin": "Корректировка БИН",
    "seal": "Навигационная пломба",
    "transport": "Перевозка",
    "package": "Пакет",
    "keden": "Keden",
    "database": "База данных",
    "mobile": "Мобилка",
    "incident": "Нештатная ситуация",
    "general": "Другой запрос",
    "support": "Вопрос в поддержку",
    "telegram_manual": "Telegram вручную",
}

PRIORITIES = {
    "low": "Низкий",
    "normal": "Обычный",
    "high": "Высокий",
    "urgent": "Срочный",
}

CATEGORY_SIGNALS = {
    "seal": ["нп", "пломб", "навигационн"],
    "keden": ["кеден", "keden", "кедена", "кедену", "кедене"],
    "transport": ["перевоз", "груз"],
    "package": ["пакет"],
    "mobile": ["мобил", "мобильное приложение", "мобильная версия"],
    "database": ["база", "базе", "базы", "база данных"],
    "incident": [
        "нештатн",
        "сбой",
        "авари",
        "критич",
        "ошибк",
        "не работает",
        "не открывается",
        "не запускается",
        "не отображается",
        "не выходит",
        "не видит",
        "не получается",
        "недоступ",
        "завис",
        "проблем",
        "сломал",
        "отказ системы",
    ],
}

MENU_CATEGORIES = {
    "1": "seal",
    "2": "transport",
    "3": "bin",
    "4": "keden",
    "5": "package",
    "6": "database",
    "7": "mobile",
    "8": "general",
    "9": "support",
}

REQUEST_MENU = """Это автоматическая система регистрации заявок.

Что сделано неправильно: сначала выберите тему номером от 1 до 9. Вы отправили описание до выбора темы или указали пункт не из списка.

Шаг 1. Сначала выберите свою проблему:

1. Навигационная пломба / НП
2. Перевозка
3. Корректировка БИН
4. Keden
5. Пакеты
6. База данных
7. Мобилка
8. Другая проблема
9. Задать вопрос в поддержку

Отправьте только номер пункта от 1 до 9.
После этого система напишет, какие данные нужны на следующем шаге.
До выбора номера заявка не создаётся"""

MAIN_MENU = """Это автоматическая система регистрации заявок.

Главное меню обращений

Выберите свою проблему:

1. Навигационная пломба / НП
2. Перевозка
3. Корректировка БИН
4. Keden
5. Пакеты
6. База данных
7. Мобилка
8. Другая проблема
9. Задать вопрос в поддержку

Отправьте номер пункта, затем подробно опишите, что именно не работает"""

MENU_CONTEXT = "__menu__"
MENU_GATE_CONTEXT = "__menu_gate__"

BIN_WORD_PATTERN = r"бин(?:ы|ов|а|у|ом|е|ами|ах)?"

BIN_TEMPLATE = """Вы выбрали: Корректировка БИН.

Можно написать данные обычным текстом и в любом порядке. Строгий шаблон не нужен.
Нужно указать:
• старый БИН
• новый БИН
• номер АТС / ТС
• название компании
• страну

Можно отправлять данные в нескольких сообщениях. Уже полученные поля система запомнит и спросит только то, чего не хватает.

Пример: старый БИН 123456789012, новый 210987654321, АТС 12345, компания Ромашка, Казахстан.

Чтобы вернуться к выбору проблемы, отправьте 0, «меню», «назад» или «сначала»."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def epoch_from_iso(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, TypeError, OSError):
        return 0


def normalize_message(text: str) -> str:
    """Remove invisible chat characters while preserving readable line breaks."""
    number_sign_placeholder = "\uf000"
    text = (text or "").replace("№", number_sign_placeholder)
    text = unicodedata.normalize("NFKC", text).replace(number_sign_placeholder, "№")
    for char in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
        text = text.replace(char, "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _clean_numbered_value(value: str) -> str:
    # Remove only an actual list marker such as "1." or "2)".
    # A plain numeric line can be the BIN itself and must stay intact.
    value = re.sub(r"^\s*\d{1,2}\s*[.)-]\s*", "", value)
    return value.strip(" :-\t")


def _find_labeled_value(lines: list[str], labels: list[str]) -> str:
    label_pattern = "|".join(labels)
    pattern = re.compile(
        rf"^\s*(?:\d+\s*[.)-]?\s*)?(?:{label_pattern})(?:\s*[:=\-]\s*|\s+)(.+?)\s*$",
        re.IGNORECASE,
    )
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return ""


def _ordered_bin_values(text: str) -> list[str]:
    """Return values from a BIN request even when users omit every field name."""
    values: list[str] = []
    labels = re.compile(
        rf"^(?:стар(?:ый|ого)(?:\s+{BIN_WORD_PATTERN})?|{BIN_WORD_PATTERN}\s+старый|"
        rf"нов(?:ый|ого)(?:\s+{BIN_WORD_PATTERN})?|{BIN_WORD_PATTERN}\s+новый|"
        r"номер\s+(?:атс|тс)|атс|тс|"
        r"название\s+компании|наименование\s+компании|компания|"
        r"страна(?:\s+регистрации)?)"
        r"(?:\s*[:=\-]\s*|\s+)",
        re.IGNORECASE,
    )
    for raw_line in normalize_message(text).splitlines():
        candidate = _clean_numbered_value(raw_line)
        if re.fullmatch(
            rf"(?:корректиров\w*\s+)?{BIN_WORD_PATTERN}[!.,:\s]*",
            candidate,
            re.IGNORECASE,
        ):
            continue
        candidate = labels.sub("", candidate, count=1).strip(" :-\t")
        if candidate:
            values.append(candidate)
    return values


def _looks_like_unlabeled_bin(text: str) -> bool:
    values = _ordered_bin_values(text)
    if len(values) != 5:
        return False
    old_bin, new_bin, ats, company, country = values
    old_ok = bool(
        5 <= len(old_bin) <= 18
        and re.fullmatch(r"[A-ZА-Я0-9/-]+", old_bin, re.IGNORECASE)
        and re.search(r"\d", old_bin)
    )
    new_ok = bool(
        5 <= len(new_bin) <= 18
        and re.fullmatch(r"[A-ZА-Я0-9/-]+", new_bin, re.IGNORECASE)
        and re.search(r"\d", new_bin)
    )
    ats_ok = bool(
        3 <= len(ats) <= 18
        and re.search(r"\d", ats)
        and re.fullmatch(r"[A-ZА-Я0-9/-]+", ats, re.IGNORECASE)
    )
    company_ok = bool(len(company) >= 3 and re.search(r"[A-ZА-Я]", company, re.IGNORECASE))
    country_ok = bool(
        2 <= len(country) <= 40
        and re.fullmatch(r"[A-ZА-ЯЁ .'-]+", country, re.IGNORECASE)
    )
    return old_ok and new_ok and ats_ok and company_ok and country_ok


def is_bin_request(text: str) -> bool:
    normalized = normalize_message(text).lower()
    return bool(
        re.search(rf"(?<![\wа-яё]){BIN_WORD_PATTERN}(?![\wа-яё])", normalized)
        or (re.search(r"\bстар(?:ый|ого)\b", normalized) and re.search(r"\bнов(?:ый|ого)\b", normalized))
        or _looks_like_unlabeled_bin(normalized)
    )


def parse_bin_request(text: str) -> tuple[dict[str, str], list[str]]:
    normalized = normalize_message(text)
    lines = normalized.splitlines()

    values = {
        "bin_old": _find_labeled_value(
            lines,
            [
                rf"стар(?:ый|ого)(?:\s+{BIN_WORD_PATTERN})?",
                rf"{BIN_WORD_PATTERN}\s+старый",
            ],
        ),
        "bin_new": _find_labeled_value(
            lines,
            [
                rf"нов(?:ый|ого)(?:\s+{BIN_WORD_PATTERN})?",
                rf"{BIN_WORD_PATTERN}\s+новый",
            ],
        ),
        "ats_number": _find_labeled_value(
            lines,
            [r"номер\s+атс", r"атс", r"номер\s+тс", r"тс"],
        ),
        "company": _find_labeled_value(
            lines,
            [r"название\s+компании", r"компания", r"наименование\s+компании"],
        ),
        "country": _find_labeled_value(
            lines,
            [r"страна(?:\s+регистрации)?"],
        ),
    }

    # If labels are omitted, use the established five-field order.
    ordered = _ordered_bin_values(normalized)
    if len(ordered) == 5:
        keys = ["bin_old", "bin_new", "ats_number", "company", "country"]
        for key, candidate in zip(keys, ordered):
            if not values[key]:
                values[key] = candidate

    labels = {
        "bin_old": "Старый БИН",
        "bin_new": "Новый БИН",
        "ats_number": "Номер АТС",
        "company": "Название компании",
        "country": "Страна",
    }
    missing = [labels[key] for key, value in values.items() if not value]
    return values, missing


def build_missing_bin_reply(missing: list[str]) -> str:
    missing_block = ", ".join(missing)
    return (
        f"Почти готово. Не хватает: {missing_block}.\n"
        "Отправьте только недостающие данные, уже полученные поля повторять не нужно."
    )


REQUEST_FIELD_LABELS = {
    "bin_old": "старый БИН",
    "bin_new": "новый БИН",
    "ats_number": "номер АТС / ТС",
    "company": "название компании",
    "country": "страну",
    "fio": "ФИО",
    "post": "пост",
    "reference": "номер / идентификатор",
    "problem": "описание проблемы",
}

REQUEST_REQUIRED_FIELDS = {
    "bin": ["bin_old", "bin_new", "ats_number", "company", "country"],
    "seal": ["reference", "problem"],
    "transport": ["reference", "problem"],
    "keden": ["reference", "problem"],
    "package": ["reference", "problem"],
    "database": ["problem"],
    "mobile": ["problem"],
    "incident": ["problem"],
    "general": ["fio", "post", "problem"],
    "support": ["problem"],
}


def _first_named_value(text: str, labels: list[str]) -> str:
    lines = normalize_message(text).splitlines()
    pattern = "|".join(labels)
    for line in lines:
        match = re.match(rf"^\s*(?:{pattern})\s*[:=\-]?\s*(.+?)\s*$", line, re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip(" .,:;-\t")
    return ""


def _free_problem_text(text: str, category: str, known_values: dict[str, str]) -> str:
    normalized = normalize_message(text)
    if not normalized:
        return ""
    fio_value = normalize_message(str(known_values.get("fio", "")))
    post_value = normalize_message(str(known_values.get("post", "")))
    if category == "general":
        plain = normalized.strip(" .,:;-")
        if (fio_value and plain.casefold() == fio_value.casefold()) or (post_value and plain.casefold() == post_value.casefold()):
            return ""
        if re.fullmatch(r"[А-ЯЁ][а-яё]{1,30}\s+[А-ЯЁ][а-яё]{1,30}(?:\s+[А-ЯЁ][а-яё]{1,30})?", plain):
            return ""
    explicit = _first_named_value(
        normalized,
        [
            r"проблема", r"описание", r"что\s+не\s+работает", r"что\s+происходит",
            r"что\s+нужно\s+сделать", r"ошибка", r"вопрос", r"что\s+именно.*",
        ],
    )
    if explicit:
        return explicit[:1800]

    lines: list[str] = []
    reference_prefix = re.compile(
        r"^\s*(?:номер\s+)?(?:нп|пломб(?:а|ы)?|перевозк(?:а|и)|тс|атс|тд|"
        r"декларац(?:ия|ии)|пакет(?:а)?)\s*[:№#-]?\s*[A-ZА-Я0-9/-]{2,}\s*[,;:—-]?\s*",
        re.IGNORECASE,
    )
    pure_field_prefix = re.compile(
        r"^\s*(?:фио|пост|тп|таможенный\s+пост|стар(?:ый|ого)\s+бин|нов(?:ый|ого)\s+бин|бин\s+(?:старый|новый)|"
        r"компания|название\s+компании|страна)\b",
        re.IGNORECASE,
    )
    for line in normalized.splitlines():
        line_plain = line.strip(" .,:;-—")
        line_folded = line_plain.casefold()
        # Не включаем в описание уже распознанные ФИО/пост, даже если пользователь
        # написал их без подписи поля (например «Техник Адилбеков Ж» / «ТП Алмалы»).
        if fio_value:
            fio_folded = fio_value.casefold()
            if line_folded == fio_folded or line_folded.endswith(" " + fio_folded):
                continue
        if post_value:
            post_folded = post_value.casefold()
            if line_folded == post_folded or re.fullmatch(
                rf"(?:пост|тп|таможенный\s+пост)\s*[:=-]?\s*{re.escape(post_folded)}",
                line_folded,
            ):
                continue
        # Если номер и описание пришли одной строкой, убираем только сам номер,
        # а хвост строки сохраняем как описание проблемы.
        trimmed = reference_prefix.sub("", line, count=1).strip(" .,:;-—")
        if trimmed != line.strip(" .,:;-—"):
            if trimmed:
                lines.append(trimmed)
            continue
        if pure_field_prefix.search(line):
            continue
        lines.append(line)
    remainder = " ".join(lines).strip()
    if not remainder or re.fullmatch(r"[A-ZА-Я0-9/+_.#№-]{2,}", remainder, re.IGNORECASE):
        return ""
    words = re.findall(r"[A-ZА-ЯЁ0-9]+", remainder, re.IGNORECASE)
    if category == "support" or len(words) >= 2 or re.search(
        r"(?:не\s+\w+|ошиб|сбой|завис|откр|закр|видит|выходит|отображ|работ|замен|помен|нужн|треб)",
        remainder.casefold(),
    ):
        return remainder[:1800]
    return ""


def extract_request_draft(text: str, category: str) -> dict[str, str]:
    normalized = tolerant_labels(normalize_message(text))
    result: dict[str, str] = {}
    if not normalized:
        return result
    if category == "bin":
        values, _ = parse_bin_request(normalized)
        result.update({key: value for key, value in values.items() if value})
        # Свободная строка вида «старый ... новый ... АТС ... компания ... Казахстан»
        # может не пройти построчный parser. Добираем значения из одной строки.
        patterns = {
            "bin_old": [r"стар(?:ый|ого)(?:\s+бин)?\s*[:№#-]?\s*([A-ZА-Я0-9/-]{5,18})"],
            "bin_new": [r"нов(?:ый|ого)(?:\s+бин)?\s*[:№#-]?\s*([A-ZА-Я0-9/-]{5,18})"],
            "ats_number": [r"(?:номер\s+)?(?:атс|тс)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{3,18})"],
            "company": [r"(?:компания|название\s+компании)\s*[:=-]?\s*([^,;\n]{2,80})"],
            "country": [r"страна\s*[:=-]?\s*([^,;\n]{2,40})"],
        }
        for key, pats in patterns.items():
            value = _extract_value(normalized, pats)
            if value:
                result[key] = value
        if not result.get("country"):
            country_match = re.search(r"(?:^|[,;]\s*)(Казахстан|Узбекистан|Россия|Кыргызстан|Киргизия|Таджикистан)\s*[.!]?$", normalized, re.IGNORECASE)
            if country_match:
                result["country"] = country_match.group(1)
        return result

    fio = _first_named_value(normalized, [r"фио", r"ф\.?и\.?о\.?"])
    if not fio and category == "general":
        # Пользователи часто пишут без названий полей, например:
        # «Техник Адилбеков Ж» или «Адилбеков Ж.А.». Роль сотрудника не является
        # частью ФИО, а одна/две буквы после фамилии считаются инициалами.
        fio_line_pattern = re.compile(
            r"^(?:(?:техник|инспектор|сотрудник|специалист|оператор)\s+)?"
            r"([А-ЯЁ][а-яё]{1,30}\s+(?:[А-ЯЁ][а-яё]{1,30}|[А-ЯЁ](?:\.[А-ЯЁ])?\.?)(?:\s+[А-ЯЁ][а-яё]{1,30})?)$",
            re.IGNORECASE,
        )
        for raw_line in normalized.splitlines():
            candidate = raw_line.strip(" .,:;-—")
            if re.match(r"^(?:пост|тп|проблема|описание)\b", candidate, re.IGNORECASE):
                continue
            match = fio_line_pattern.match(candidate)
            if match:
                fio = match.group(1)
                break
        if not fio and not re.match(r"^\s*(?:пост|тп|проблема|описание)\b", normalized, re.IGNORECASE):
            m = re.search(
                r"\b([А-ЯЁ][а-яё]{1,30}\s+(?:[А-ЯЁ][а-яё]{1,30}|[А-ЯЁ](?:\.[А-ЯЁ])?\.?)(?:\s+[А-ЯЁ][а-яё]{1,30})?)\b",
                normalized,
            )
            fio = m.group(1) if m else ""
    post = _first_named_value(normalized, [r"пост", r"название\s+поста", r"тп", r"таможенный\s+пост"])
    if not post and category == "general":
        post_match = re.search(
            r"\bпост\s*[:=-]?\s*([^,;\n]{2,80}?)(?=\s+(?:не|проблем|ошиб|нужно|надо|завис|откр|закр|треб)\b|[,;]|$)",
            normalized,
            re.IGNORECASE,
        )
        post = post_match.group(1).strip() if post_match else ""
    if fio:
        result["fio"] = fio[:120]
    if post:
        result["post"] = post[:120]

    reference_patterns = {
        "seal": [
            r"(?:номер\s+)?(?:нп|пломб(?:а|ы)?)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{3,})",
            r"(?:перевозк(?:а|и)|тд)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{3,})",
        ],
        "transport": [r"(?:номер\s+)?(?:перевозк(?:а|и)|тс|атс|тд)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{2,})"],
        "keden": [r"(?:номер\s+)?(?:перевозк(?:а|и)|тд)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{2,})"],
        "package": [r"(?:номер\s+)?(?:пакет(?:а)?|перевозк(?:а|и)|тд)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{2,})"],
        "database": [r"(?:номер\s+)?(?:перевозк(?:а|и)|тд|декларац(?:ия|ии))\s*[:№#-]?\s*([A-ZА-Я0-9/-]{2,})"],
    }
    ref = ""
    for pattern in reference_patterns.get(category, []):
        for match in re.finditer(r"(?<![\w])" + pattern, normalized, re.IGNORECASE):
            value = match.group(1)
            if re.search(r"\d", value):
                ref = value
                break
        if ref:
            break
    if not ref and category in {"seal", "transport", "keden", "package"}:
        # Если пользователь прислал отдельным сообщением только номер, принимаем его как reference.
        only = normalized.strip(" .,:;#№")
        if re.fullmatch(r"[A-ZА-Я0-9/-]{3,}", only, re.IGNORECASE) and re.search(r"\d", only):
            ref = only
    if ref:
        result["reference"] = ref[:120]

    problem = _free_problem_text(normalized, category, result)
    if problem:
        result["problem"] = problem
    return result


def merge_request_draft(existing: dict[str, str] | None, incoming: dict[str, str]) -> dict[str, str]:
    merged = {str(k): normalize_message(str(v)) for k, v in (existing or {}).items() if str(v).strip()}
    for key, value in incoming.items():
        clean = normalize_message(str(value))
        if clean:
            merged[key] = clean
    return merged


def missing_request_fields(category: str, draft: dict[str, str]) -> list[str]:
    required = REQUEST_REQUIRED_FIELDS.get(category, ["problem"])
    return [key for key in required if not normalize_message(str(draft.get(key, "")))]


def apply_expected_request_answer(
    text: str,
    category: str,
    existing: dict[str, str] | None,
    parsed: dict[str, str],
) -> dict[str, str]:
    """Use a short unlabeled reply as the answer to the one field we just asked for.

    This is what makes the multi-message flow feel conversational: after the
    system asks only for a post/company/reference, the user can simply reply
    «Алмалы», «ТОО Транзит» or «123456» without repeating the field name.
    """
    current = {str(k): normalize_message(str(v)) for k, v in (existing or {}).items() if str(v).strip()}
    required = REQUEST_REQUIRED_FIELDS.get(category, ["problem"])
    missing_before = [key for key in required if not current.get(key)]
    if len(missing_before) != 1:
        return parsed
    expected = missing_before[0]
    if normalize_message(str(parsed.get(expected, ""))):
        return parsed

    clean = normalize_message(text).strip(" \t\r\n.,;:—")
    if not clean or clean in {"0", "1"} or len(clean) > 1800:
        return parsed

    candidate = ""
    if expected in {"bin_old", "bin_new", "ats_number", "reference"}:
        token = re.sub(
            r"^(?:стар(?:ый|ого)(?:\s+бин)?|нов(?:ый|ого)(?:\s+бин)?|номер(?:\s+(?:атс|тс|нп|пломбы|перевозки|тд|пакета))?|атс|тс|нп|пломба|перевозка|тд|пакет)\s*[:№#=-]?\s*",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        if re.fullmatch(r"[A-ZА-ЯЁ0-9/+_.-]{2,40}", token, re.IGNORECASE) and re.search(r"\d", token):
            candidate = token
    elif expected == "fio":
        stripped = re.sub(
            r"^(?:фио\s*[:=-]?|(?:техник|инспектор|сотрудник|специалист|оператор)\s+)",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        if re.fullmatch(
            r"[А-ЯЁ][а-яё]{1,30}\s+(?:[А-ЯЁ][а-яё]{1,30}|[А-ЯЁ](?:\.[А-ЯЁ])?\.?)(?:\s+[А-ЯЁ][а-яё]{1,30})?",
            stripped,
        ):
            candidate = stripped
    elif expected in {"post", "company", "country"}:
        stripped = re.sub(
            r"^(?:пост|тп|таможенный\s+пост|компания|название\s+компании|страна)\s*[:=-]?\s*",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        if 1 <= len(stripped.split()) <= 12 and 1 < len(stripped) <= 160:
            candidate = stripped
    elif expected == "problem":
        if len(clean) >= 2:
            candidate = clean

    if not candidate:
        return parsed
    enriched = dict(parsed)
    enriched[expected] = candidate[:1800]
    return enriched


def build_missing_request_reply(category: str, missing_keys: list[str]) -> str:
    labels = [REQUEST_FIELD_LABELS.get(key, key) for key in missing_keys]
    if not labels:
        return ""
    if len(labels) == 1:
        question = f"Укажите, пожалуйста, {labels[0]}."
    else:
        question = "Не хватает только: " + ", ".join(labels) + "."
    return (
        f"{question}\n"
        "Остальные данные уже запомнил, повторять их не нужно. Можно ответить обычным текстом."
    )


def request_draft_text(category: str, draft: dict[str, str]) -> str:
    if category == "bin":
        return "\n".join(
            [
                f"Старый БИН: {draft.get('bin_old', '')}",
                f"Новый БИН: {draft.get('bin_new', '')}",
                f"Номер АТС: {draft.get('ats_number', '')}",
                f"Название компании: {draft.get('company', '')}",
                f"Страна: {draft.get('country', '')}",
            ]
        )
    parts: list[str] = []
    if draft.get("fio"):
        parts.append(f"ФИО: {draft['fio']}")
    if draft.get("post"):
        parts.append(f"Пост: {draft['post']}")
    if draft.get("reference"):
        reference_label = {
            "seal": "Номер НП / пломбы",
            "transport": "Номер перевозки / ТС / ТД",
            "keden": "Номер перевозки / ТД",
            "package": "Номер пакета / перевозки / ТД",
            "database": "Номер перевозки / ТД / декларации",
        }.get(category, "Номер")
        parts.append(f"{reference_label}: {draft['reference']}")
    if draft.get("problem"):
        parts.append(f"Проблема: {draft['problem']}")
    return "\n".join(parts)


def _extract_value(text: str, patterns: list[str]) -> str:
    for raw_pattern in patterns:
        match = re.search(raw_pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip(" .,;:-")
    return ""


def _signal_matches(text: str, signal: str) -> bool:
    signal = normalize_message(signal).casefold().strip()
    if not signal:
        return False
    if " " in signal or len(signal) > 4:
        return signal in text
    return bool(re.search(rf"(?<![\wа-яё]){re.escape(signal)}(?![\wа-яё])", text))


def identify_category(text: str) -> str:
    lower = normalize_message(text).casefold()
    for category in (
        "seal",
        "keden",
        "package",
        "mobile",
        "database",
        "transport",
        "incident",
    ):
        if any(_signal_matches(lower, signal) for signal in CATEGORY_SIGNALS.get(category, [])):
            return category
    return ""


def is_actionable_message(text: str) -> bool:
    normalized = normalize_message(text)
    return is_bin_request(normalized) or bool(identify_category(normalized))


def menu_category(text: str) -> str:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    match = re.fullmatch(r"(?:пункт\s*)?([1-9])", normalized)
    return MENU_CATEGORIES.get(match.group(1), "") if match else ""


def is_main_menu_command(text: str) -> bool:
    normalized = normalize_message(text).casefold().strip(" .,!?:;-")
    return normalized in {
        "0", "меню", "главное меню", "назад", "сначала", "отмена",
        "вернуться", "вернуться назад", "начать заново", "мню", "менб", "менюу",
    }


def is_support_trigger_message(text: str) -> bool:
    """Return True only when a free-form message looks like a support request.

    Ordinary conversation must remain ordinary conversation. The menu is shown
    only for explicit request/problem signals, while numeric choices and an
    already selected category are handled by the conversation state separately.
    """
    normalized = normalize_message(text).casefold()
    if not normalized:
        return False
    if is_main_menu_command(normalized) or re.fullmatch(r"(?:пункт\s*)?\d{1,2}", normalized.strip(" .,!?:;-")):
        return True
    # Само упоминание темы («мобилка», «перевозка», «НП») ещё не означает
    # заявку. Это важно для живой переписки вроде «где скачать приложение?».
    # Автоматика включается при явном признаке запроса, ошибки или действия.
    if is_bin_request(normalized):
        return True
    return bool(
        re.search(
            r"(?:\bзаявк\w*|\bобращен\w*|\bзапрос\w*|\bтехподдерж\w*|"
            r"\bпомог(?:ите|и)?\b|\bнужна\s+помощь|"
            r"\bнадо\s+(?:исправ|откр|закр|помен|смен|замен|добав|удал|подключ)|"
            r"\bнужно\s+(?:исправ|откр|закр|помен|смен|замен|добав|удал|подключ)|"
            r"\b(?:исправ|откр|закр|помен|смен|замен|добав|удал|подключ)\w*.{0,40}"
            r"(?:\bнп\b|пломб|перевоз|пакет|кеден|keden|баз|мобил|аккаунт)|"
            r"\bкорректиров\w*|\bаккаунт\w*.{0,40}(?:смен|замен|помен)|"
            r"\bне\s+(?:работает|открывается|выходит|видит|отображается|получается)|"
            r"\bошиб\w*|\bсбой\w*|\bпроблем\w*)",
            normalized,
        )
    )


def has_request_details(text: str, category: str) -> bool:
    normalized = normalize_message(text).casefold()
    if not normalized:
        return False
    if category == "bin":
        _, missing = parse_bin_request(normalized)
        return len(missing) < 5
    category_patterns = {
        "seal": r"\bнп\b|пломб\w*|навигационн\w*",
        "transport": r"перевоз\w*|груз\w*",
        "keden": r"кеден\w*|keden\w*",
        "package": r"пакет\w*",
        "database": r"\bбаз(?:а|е|ы|у|ой)?\b|база\s+данных",
        "mobile": r"мобил\w*|мобильн\w*\s+приложен\w*",
        "incident": r"нештатн\w*|сбой\w*|авари\w*|ошибк\w*|проблем\w*",
    }
    without_topic = re.sub(category_patterns.get(category, r"$^"), " ", normalized)
    words = re.findall(r"[a-zа-яё0-9/-]+", without_topic)
    filler = {
        "привет",
        "здравствуйте",
        "добрый",
        "день",
        "вечер",
        "утро",
        "заявка",
        "запрос",
        "проблема",
        "помогите",
        "помоги",
        "нужно",
        "надо",
        "по",
        "с",
        "со",
        "у",
        "меня",
        "там",
        "пожалуйста",
    }
    meaningful = [word for word in words if word not in filler]
    issue_phrase = bool(
        re.search(
            r"(?:не\s+\w+|ошибк|сбой|завис|пропал|сломан|откр|закр|"
            r"видит|выходит|отображ|работает|номер|тд|декларац)",
            normalized,
        )
    )
    has_reference = any(re.search(r"\d{3,}", word) for word in meaningful)
    if category == "general":
        return len(meaningful) >= 2 or issue_phrase
    return has_reference or issue_phrase or len(meaningful) >= 2


def request_detail_issues(text: str, category: str) -> list[str]:
    normalized = normalize_message(text)
    lower = normalized.casefold()
    if not normalized:
        return ["Сообщение пустое"]

    problem_described = bool(
        re.search(
            r"(?:\bне\s+\w+|проблем\w*|ошиб\w*|сбой\w*|завис\w*|"
            r"откр\w*|закр\w*|видит\w*|выходит\w*|отображ\w*|"
            r"работа\w*|пропал\w*|отсутств\w*|добав\w*|измен\w*|"
            r"коррект\w*|сломан\w*|требуется\w*)",
            lower,
        )
    )
    has_identifier = bool(
        re.search(r"\d{3,}", lower)
        or re.search(r"\b[a-zа-яё]{1,5}[-/]?\d{2,}[a-zа-яё0-9/-]*\b", lower)
    )

    issues: list[str] = []
    if category == "general":
        has_fio = bool(
            re.search(r"\bфио\b\s*[:\-]?\s*\S+\s+\S+", lower)
            or re.search(r"\b[А-ЯЁ][а-яё]{1,}\s+[А-ЯЁ][а-яё]{1,}\b", normalized)
        )
        has_post = bool(re.search(r"\bпост\b\s*[:\-]?\s*[a-zа-яё0-9]", lower))
        if not has_fio:
            issues.append("Не указано ФИО")
        if not has_post:
            issues.append("Не указан пост")
        if not problem_described:
            issues.append("Не описано, что именно не работает")
        return issues
    if category == "support":
        return []

    identifier_labels = {
        "seal": "Не указан номер НП или пломбы",
        "transport": "Не указан номер перевозки, ТС или ТД",
        "keden": "Не указан номер перевозки, которую не видит Keden",
        "package": "Не указан номер пакета, перевозки или ТД",
    }
    if category in identifier_labels and not has_identifier:
        issues.append(identifier_labels[category])
    if not problem_described:
        issues.append("Не описано, что нужно сделать или что именно не работает")
    return issues


def request_detail_error_reply(text: str, category: str) -> str:
    issues = request_detail_issues(text, category)
    issue_block = "\n".join(f"• {issue}" for issue in issues)
    return (
        f"Заявку пока нельзя создать. В теме «{CATEGORIES.get(category, category)}» не хватает данных:\n"
        f"{issue_block}\n\n"
        "Ниже пример, на который можно ориентироваться. Дополните, пожалуйста, недостающие данные. "
        "Их можно прислать несколькими сообщениями.\n\n"
        f"{category_prompt(category)}"
    )


def category_prompt(category: str) -> str:
    if category == "bin":
        return BIN_TEMPLATE
    label = CATEGORIES.get(category, "Другая проблема")
    required = {
        "seal": "номер НП / пломбы и что нужно сделать",
        "transport": "номер перевозки, ТС или ТД и описание проблемы",
        "keden": "номер перевозки / ТД и что именно Keden не видит или не принимает",
        "package": "номер пакета, перевозки или ТД и описание проблемы",
        "database": "что именно не отображается или работает неправильно; номер можно добавить, если он есть",
        "mobile": "что вы делаете и что происходит / какой текст ошибки",
        "general": "ФИО, пост и описание проблемы",
        "support": "ваш вопрос",
    }.get(category, "описание проблемы")
    examples = {
        "seal": "Например: НП 123456 не отображается в базе, нужно открыть",
        "transport": "Например: перевозка 123456, статус завис и не меняется",
        "keden": "Например: перевозка 123456, Keden её не видит",
        "package": "Например: пакет 123456 повреждён, нужно заменить",
        "database": "Например: в базе не выходит ТД 123456",
        "mobile": "Например: в разделе проверки нажимаю «Открыть», появляется ошибка 500",
        "general": "Например: Иванов Иван, пост Алмалы, не выходит перевозка в базе",
        "support": "Напишите вопрос обычным сообщением",
    }.get(category, "")
    return (
        f"Вы выбрали: {label}.\n\n"
        "Строгий шаблон не нужен. Напишите обычным текстом, в любом порядке. "
        "Можно отправить данные в нескольких сообщениях.\n"
        f"Нужно: {required}.\n"
        + (f"{examples}.\n" if examples else "")
        + "Если чего-то не хватит, система запомнит уже полученные данные и спросит только недостающее.\n"
        "Фото или скриншот можно приложить отдельно.\n\n"
        "Чтобы вернуться в меню, отправьте 0."
    )


def is_acknowledgement(text: str) -> bool:
    # Короткие благодарности/подтверждения не должны случайно создавать новую
    # заявку. Убираем эмодзи, квадраты замены и прочую пунктуацию, чтобы
    # «Рахмет 🙏», «Спасибо 👍» и похожие ответы распознавались стабильно.
    normalized = normalize_message(text).casefold()
    normalized = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return bool(
        re.fullmatch(
            r"(?:спасибо(?:\s+большое)?|спс|благодарю|рахмет(?:\s+большое)?|"
            r"ок(?:ей)?|понял(?:а)?|хорошо|ясно|принято|готово)",
            normalized,
        )
    )


def general_category(
    text: str,
    forced_category: str = "",
) -> tuple[str, str]:
    lower = text.casefold()
    matched_category = forced_category if forced_category in CATEGORIES else identify_category(lower)
    if matched_category == "seal":
        if re.search(r"\b(?:откр\w*|разблокир\w*)\b", lower):
            return "seal", "Открытие НП"
        if re.search(r"\b(?:закр\w*|заблокир\w*)\b", lower):
            return "seal", "Закрытие НП"
        database_problem = bool(
            "в базе" in lower
            and re.search(r"(?:не\s+(?:выходит|видно|отображается|появляется)|нет|пропал\w*)", lower)
        )
        if database_problem:
            return "seal", "НП не отображается в базе"
        return "seal", "Запрос по навигационной пломбе"

    if matched_category == "keden":
        not_received = bool(
            re.search(
                r"(?:не\s+(?:видит|получил|получает|отображает|выходит|приходит)|"
                r"нет\s+(?:у\s+них|в\s+кедене)|не\s+передал\w*)",
                lower,
            )
        )
        if not_received and "перевоз" in lower:
            return "keden", "Keden не видит перевозку"
        if not_received:
            return "keden", "Keden не видит данные"
        return "keden", "Запрос по обмену с Keden"

    if matched_category == "package":
        if re.search(r"(?:поврежд|порван|испорчен|слом)", lower):
            return "package", "Повреждение пакета"
        if re.search(r"\b(?:откр\w*|разблокир\w*)\b", lower):
            return "package", "Открытие пакета"
        if re.search(r"\b(?:закр\w*|заблокир\w*)\b", lower):
            return "package", "Закрытие пакета"
        if re.search(r"(?:не\s+(?:выходит|видно|отображается|появляется)|нет\s+(?:в\s+)?базе)", lower):
            return "package", "Пакет не отображается"
        return "package", "Запрос по пакету"

    if matched_category == "mobile":
        if re.search(r"(?:не\s+(?:входит|открывается|работает|запускается)|ошибк|завис|недоступ)", lower):
            return "mobile", "Проблема в мобилке"
        if re.search(r"(?:обнов|верси)", lower):
            return "mobile", "Обновление мобилки"
        return "mobile", "Запрос по мобилке"

    if matched_category == "transport":
        if re.search(r"\b(?:откр\w*|разблокир\w*)\b", lower):
            return "transport", "Открытие перевозки"
        if re.search(r"\b(?:закр\w*|заверш\w*|заблокир\w*)\b", lower):
            return "transport", "Закрытие перевозки"
        if re.search(r"(?:завис|статус.{0,20}не\s+(?:меняется|обновляется))", lower):
            return "transport", "Перевозка зависла"
        if re.search(r"(?:не\s+(?:выходит|видно|отображается|появляется)|нет\s+(?:в\s+)?базе)", lower):
            return "transport", "Перевозка не отображается"
        return "transport", "Запрос по перевозке"

    if matched_category == "database":
        if re.search(r"(?:не\s+(?:выходит|видно|отображается|появляется|находит)|нет\s+данных|пропал)", lower):
            return "database", "Данные не отображаются в базе"
        if re.search(r"(?:добав|внес|исправ|обнов|корректир)", lower):
            return "database", "Изменение данных в базе"
        if re.search(r"(?:не\s+работает|ошибк|завис|недоступ)", lower):
            return "database", "Ошибка базы данных"
        return "database", "Запрос по базе данных"

    if matched_category == "incident":
        if re.search(r"(?:авари|критич|массов|у\s+всех|полностью\s+не\s+работает)", lower):
            return "incident", "Критическая нештатная ситуация"
        return "incident", "Нештатная ситуация"

    if matched_category == "support":
        return "support", "Вопрос в поддержку"

    if matched_category:
        return matched_category, CATEGORIES.get(matched_category, "Новый запрос")

    return "general", "Новый запрос"


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
) -> tuple[str, str, str]:
    normalized = normalize_message(text)
    category, title = general_category(normalized, forced_category)

    transport = _extract_value(
        normalized,
        [r"(?:номер\s+перевозки|перевозк(?:а|и|е|у|ой)?)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{2,})"],
    )
    vehicle = _extract_value(
        normalized,
        [r"(?:номер\s+атс|атс|номер\s+тс|тс)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{2,})"],
    )
    seal = _extract_value(
        normalized,
        [
            r"(?:номер\s+(?:нп|пломбы)|нп|пломба)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{5,})",
            r"(?:открыть|открытие)\s+(?:нп|пломбу)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{5,})",
        ],
    )
    declaration = _extract_value(
        normalized,
        [r"(?:номер\s+тд|тд|декларация)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{4,})"],
    )
    package = _extract_value(
        normalized,
        [r"(?:номер\s+пакета|пакет(?:\s+документов)?)\s*[:№#-]?\s*([A-ZА-Я0-9/-]{3,})"],
    )
    error_code = _extract_value(
        normalized,
        [r"(?:код\s+ошибки|ошибка)\s*[:№#-]?\s*([A-ZА-Я0-9_-]{3,})"],
    )

    details: list[str] = []
    if transport:
        details.append(f"Перевозка: {transport}")
    if vehicle:
        details.append(f"АТС: {vehicle}")
    if seal:
        details.append(f"НП: {seal}")
    if declaration:
        details.append(f"ТД: {declaration}")
    if package:
        details.append(f"Пакет: {package}")
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
) -> dict[str, Any]:
    normalized = normalize_message(text)
    # Явно выбранная пользователем категория всегда имеет приоритет над
    # автоопределением по тексту. Например, вопрос в поддержку может содержать
    # слово «БИН», но это не должно внезапно запускать шаблон корректировки БИН.
    if forced_category == "bin" or (not forced_category and is_bin_request(normalized)):
        values, missing = parse_bin_request(normalized)
        if missing:
            return {
                "created": False,
                "reply": build_missing_bin_reply(missing),
                "missing": missing,
            }

        summary = (
            f"Корректировка БИН {values['bin_old']} → {values['bin_new']}\n"
            f"АТС: {values['ats_number']} · {values['company']} · {values['country']}"
        )
        return {
            "created": True,
            "ticket": {
                "source": "whatsapp",
                "sender": sender or "Неизвестный отправитель",
                "phone": phone,
                "chat_id": chat_id,
                "external_id": external_id,
                "category": "bin",
                "title": "Корректировка БИН",
                "summary": summary,
                "original_text": normalized,
                "attachment_name": attachment_name,
                **values,
            },
        }

    category, title, summary = build_general_summary(normalized, forced_category)
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


class TicketStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.create_function("FOLD", 1, lambda value: normalize_message(str(value or "")).casefold())
        connection.create_function("DIGITS", 1, lambda value: re.sub(r"\D", "", str(value or "")))
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA cache_size = -32768")
        connection.execute("PRAGMA mmap_size = 134217728")
        connection.execute("PRAGMA wal_autocheckpoint = 1000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS outbound_requests (
                    request_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, actor TEXT NOT NULL,
                    message_id INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    original_text TEXT NOT NULL DEFAULT '',
                    attachment_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    assigned_to TEXT NOT NULL DEFAULT '',
                    bin_old TEXT NOT NULL DEFAULT '',
                    bin_new TEXT NOT NULL DEFAULT '',
                    ats_number TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL DEFAULT '',
                    first_response_at TEXT NOT NULL DEFAULT '',
                    shift_handoff INTEGER NOT NULL DEFAULT 0,
                    handoff_from TEXT NOT NULL DEFAULT '',
                    handoff_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS error_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    attachment_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    admin_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL DEFAULT 'action',
                    actor TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '',
                    object_type TEXT NOT NULL DEFAULT '',
                    object_id TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '',
                    level TEXT NOT NULL DEFAULT 'info',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inbound_messages (
                    external_id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS template_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    missing_fields TEXT NOT NULL,
                    reply_body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    provider_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS outbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    claimed_at TEXT NOT NULL DEFAULT '',
                    mentions_json TEXT NOT NULL DEFAULT '[]',
                    reply_to_key TEXT NOT NULL DEFAULT '',
                    media_path TEXT NOT NULL DEFAULT '',
                    media_mime TEXT NOT NULL DEFAULT '',
                    media_name TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 4,
                    next_attempt_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL DEFAULT '',
                    failed_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
                );

                CREATE TABLE IF NOT EXISTS conversation_contexts (
                    contact_key TEXT PRIMARY KEY,
                    pending_category TEXT NOT NULL DEFAULT '',
                    active_ticket_id INTEGER NOT NULL DEFAULT 0,
                    force_new INTEGER NOT NULL DEFAULT 0,
                    draft_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_chats (
                    chat_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    last_timestamp INTEGER NOT NULL DEFAULT 0,
                    unread_count INTEGER NOT NULL DEFAULT 0,
                    mention_unread_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_chat_aliases (
                    alias_id TEXT PRIMARY KEY,
                    canonical_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_whatsapp_chat_aliases_canonical
                    ON whatsapp_chat_aliases(canonical_id);

                CREATE TABLE IF NOT EXISTS whatsapp_chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    from_me INTEGER NOT NULL DEFAULT 0,
                    body TEXT NOT NULL DEFAULT '',
                    message_type TEXT NOT NULL DEFAULT 'chat',
                    message_timestamp INTEGER NOT NULL DEFAULT 0,
                    ack INTEGER NOT NULL DEFAULT 0,
                    media_path TEXT NOT NULL DEFAULT '',
                    media_mime TEXT NOT NULL DEFAULT '',
                    media_name TEXT NOT NULL DEFAULT '',
                    transcript TEXT NOT NULL DEFAULT '',
                    ticket_id INTEGER NOT NULL DEFAULT 0,
                    mentions_json TEXT NOT NULL DEFAULT '[]',
                    notify INTEGER NOT NULL DEFAULT 0,
                    quoted_message_key TEXT NOT NULL DEFAULT '',
                    quoted_body TEXT NOT NULL DEFAULT '',
                    quoted_sender TEXT NOT NULL DEFAULT '',
                    forwarded INTEGER NOT NULL DEFAULT 0,
                    edited INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    reactions_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_key)
                );

                CREATE TABLE IF NOT EXISTS whatsapp_contacts (
                    chat_id TEXT PRIMARY KEY,
                    phone TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_groups (
                    chat_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    participant_count INTEGER NOT NULL DEFAULT 0,
                    added_by_admin INTEGER NOT NULL DEFAULT 0,
                    muted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_call_permissions (
                    contact_key TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_manual_chat_modes (
                    chat_id TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auto_reply_cooldowns (
                    contact_key TEXT NOT NULL,
                    reply_kind TEXT NOT NULL,
                    last_sent_at TEXT NOT NULL,
                    PRIMARY KEY(contact_key, reply_kind)
                );

                CREATE TABLE IF NOT EXISTS whatsapp_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_key TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    claimed_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_whatsapp_chat_messages_chat_time
                ON whatsapp_chat_messages(chat_id, message_timestamp, id);
                CREATE INDEX IF NOT EXISTS idx_tickets_status_updated
                ON tickets(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_error_reports_status_created
                ON error_reports(status, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_error_reports_chat_created
                ON error_reports(chat_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_tickets_category_priority
                ON tickets(category, priority, updated_at);
                CREATE INDEX IF NOT EXISTS idx_outbound_status_created
                ON outbound_messages(status, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_whatsapp_actions_status_created
                ON whatsapp_actions(status, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_whatsapp_chats_last_timestamp
                ON whatsapp_chats(last_timestamp DESC, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_whatsapp_groups_enabled_name
                ON whatsapp_groups(added_by_admin, name);
                CREATE INDEX IF NOT EXISTS idx_audit_log_created
                ON audit_log(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_log_level
                ON audit_log(level, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tickets_assignee_status
                ON tickets(assigned_to, status, shift_handoff, priority, id DESC);

                """
            )
            existing_ticket_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tickets)").fetchall()
            }
            for name, definition in {
                "chat_id": "TEXT NOT NULL DEFAULT ''",
                "external_id": "TEXT NOT NULL DEFAULT ''",
                "priority": "TEXT NOT NULL DEFAULT 'normal'",
                "shift_handoff": "INTEGER NOT NULL DEFAULT 0",
                "handoff_from": "TEXT NOT NULL DEFAULT ''",
                "handoff_at": "TEXT NOT NULL DEFAULT ''",
                "first_response_at": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing_ticket_columns:
                    connection.execute(f"ALTER TABLE tickets ADD COLUMN {name} {definition}")
            connection.execute(
                "UPDATE tickets SET first_response_at = updated_at WHERE first_response_at = '' AND status <> 'new'"
            )
            existing_context_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(conversation_contexts)"
                ).fetchall()
            }
            if "force_new" not in existing_context_columns:
                connection.execute(
                    "ALTER TABLE conversation_contexts "
                    "ADD COLUMN force_new INTEGER NOT NULL DEFAULT 0"
                )
            if "draft_json" not in existing_context_columns:
                connection.execute(
                    "ALTER TABLE conversation_contexts "
                    "ADD COLUMN draft_json TEXT NOT NULL DEFAULT '{}'"
                )
            existing_chat_message_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(whatsapp_chat_messages)"
                ).fetchall()
            }
            if "ack" not in existing_chat_message_columns:
                connection.execute(
                    "ALTER TABLE whatsapp_chat_messages "
                    "ADD COLUMN ack INTEGER NOT NULL DEFAULT 0"
                )
            if "sender" not in existing_chat_message_columns:
                connection.execute(
                    "ALTER TABLE whatsapp_chat_messages "
                    "ADD COLUMN sender TEXT NOT NULL DEFAULT ''"
                )
            for name, definition in {
                "sender_phone": "TEXT NOT NULL DEFAULT ''",
                "sender_id": "TEXT NOT NULL DEFAULT ''",
                "media_path": "TEXT NOT NULL DEFAULT ''",
                "media_mime": "TEXT NOT NULL DEFAULT ''",
                "media_name": "TEXT NOT NULL DEFAULT ''",
                "transcript": "TEXT NOT NULL DEFAULT ''",
                "ticket_id": "INTEGER NOT NULL DEFAULT 0",
                "mentions_json": "TEXT NOT NULL DEFAULT '[]'",
                "notify": "INTEGER NOT NULL DEFAULT 0",
                "quoted_message_key": "TEXT NOT NULL DEFAULT ''",
                "quoted_body": "TEXT NOT NULL DEFAULT ''",
                "quoted_sender": "TEXT NOT NULL DEFAULT ''",
                "forwarded": "INTEGER NOT NULL DEFAULT 0",
                "edited": "INTEGER NOT NULL DEFAULT 0",
                "edit_timestamp": "INTEGER NOT NULL DEFAULT 0",
                "deleted": "INTEGER NOT NULL DEFAULT 0",
                "reactions_json": "TEXT NOT NULL DEFAULT '{}'",
            }.items():
                if name not in existing_chat_message_columns:
                    connection.execute(
                        f"ALTER TABLE whatsapp_chat_messages ADD COLUMN {name} {definition}"
                    )
            existing_group_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(whatsapp_groups)").fetchall()
            }
            if "added_by_admin" not in existing_group_columns:
                connection.execute(
                    "ALTER TABLE whatsapp_groups "
                    "ADD COLUMN added_by_admin INTEGER NOT NULL DEFAULT 0"
                )
            if "muted" not in existing_group_columns:
                connection.execute(
                    "ALTER TABLE whatsapp_groups "
                    "ADD COLUMN muted INTEGER NOT NULL DEFAULT 0"
                )
            existing_chat_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(whatsapp_chats)").fetchall()
            }
            if "mention_unread_count" not in existing_chat_columns:
                connection.execute(
                    "ALTER TABLE whatsapp_chats "
                    "ADD COLUMN mention_unread_count INTEGER NOT NULL DEFAULT 0"
                )
            existing_outbound_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(outbound_messages)").fetchall()
            }
            if "mentions_json" not in existing_outbound_columns:
                connection.execute(
                    "ALTER TABLE outbound_messages ADD COLUMN mentions_json TEXT NOT NULL DEFAULT '[]'"
                )
            for name, definition in {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "max_attempts": "INTEGER NOT NULL DEFAULT 4",
                "next_attempt_at": "TEXT NOT NULL DEFAULT ''",
                "last_error": "TEXT NOT NULL DEFAULT ''",
                "provider_id": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
                "sent_at": "TEXT NOT NULL DEFAULT ''",
                "failed_at": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing_outbound_columns:
                    connection.execute(f"ALTER TABLE outbound_messages ADD COLUMN {name} {definition}")
            connection.execute(
                "UPDATE outbound_messages SET updated_at = created_at WHERE updated_at = ''"
            )
            existing_action_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(whatsapp_actions)").fetchall()
            }
            if "actor" not in existing_action_columns:
                connection.execute(
                    "ALTER TABLE whatsapp_actions ADD COLUMN actor TEXT NOT NULL DEFAULT ''"
                )
            if "dispatch_started_at" not in existing_outbound_columns:
                connection.execute("ALTER TABLE outbound_messages ADD COLUMN dispatch_started_at TEXT NOT NULL DEFAULT ''")
                connection.execute(
                    "UPDATE outbound_messages SET status='uncertain', claimed_at='', next_attempt_at='', "
                    "last_error='Отправка старой версии могла состояться. Проверьте переписку.' WHERE status='processing'"
                )
            # Legacy builds may have sent these already. Do not replay on boot.
            connection.execute(
                "UPDATE outbound_messages SET status='uncertain', claimed_at='', next_attempt_at='', "
                "last_error='Отправка могла состояться. Автоповтор остановлен, проверьте переписку.' "
                "WHERE status IN ('pending','failed') AND last_error LIKE '%WhatsApp не подтвердил отправку%'"
            )
            for name, definition in {
                "reply_to_key": "TEXT NOT NULL DEFAULT ''",
                "media_path": "TEXT NOT NULL DEFAULT ''",
                "media_mime": "TEXT NOT NULL DEFAULT ''",
                "media_name": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in existing_outbound_columns:
                    connection.execute(f"ALTER TABLE outbound_messages ADD COLUMN {name} {definition}")
            # В базу исходящие сообщения попадают только после успешного
            # client.sendMessage, поэтому старое значение 0 означает
            # «отправлено», а не «ещё отправляется».
            connection.execute(
                "UPDATE whatsapp_chat_messages SET ack = 1 "
                "WHERE from_me = 1 AND ack < 1"
            )
            self._merge_whatsapp_lid_aliases(connection)
            # Let SQLite refresh planner statistics for the newly created indexes.
            connection.execute("PRAGMA optimize")

    def _canonical_whatsapp_chat_id(self, connection: sqlite3.Connection, chat_id: str) -> str:
        clean = normalize_message(chat_id)[:120]
        if not clean or clean.endswith("@g.us") or clean.endswith("@c.us"):
            return clean
        row = connection.execute(
            "SELECT canonical_id FROM whatsapp_chat_aliases WHERE alias_id=? LIMIT 1",
            (clean,),
        ).fetchone()
        canonical = normalize_message(str(row["canonical_id"] if row else ""))[:120]
        return canonical or clean

    def canonical_whatsapp_chat_id(self, chat_id: str) -> str:
        """Return the persistent canonical private-chat id used by the UI."""
        with self.connection() as connection:
            return self._canonical_whatsapp_chat_id(connection, chat_id)

    def remember_whatsapp_chat_alias(self, alias_id: str, canonical_id: str) -> bool:
        alias = normalize_message(alias_id)[:120]
        canonical = normalize_message(canonical_id)[:120]
        if not alias.endswith("@lid") or not canonical.endswith("@c.us") or alias == canonical:
            return False
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alias_id) DO UPDATE SET
                    canonical_id=excluded.canonical_id,
                    updated_at=excluded.updated_at
                """,
                (alias, canonical, utc_now()),
            )
            self._merge_whatsapp_lid_aliases(connection)
        return True

    def _merge_whatsapp_lid_aliases(self, connection: sqlite3.Connection) -> None:
        """Persist and merge private @lid aliases into phone based @c.us chats.

        The alias mapping must survive the first merge. Otherwise a later
        WhatsApp sync can recreate the same contact as a second chat after the
        ticket row has already been canonicalised.
        """
        now = utc_now()
        sources = connection.execute(
            """
            SELECT chat_id, phone FROM tickets
             WHERE source='whatsapp' AND chat_id LIKE '%@lid' AND phone<>''
            UNION ALL
            SELECT chat_id, phone FROM whatsapp_contacts
             WHERE chat_id LIKE '%@lid' AND phone<>''
            UNION ALL
            SELECT chat_id, sender_phone AS phone FROM whatsapp_chat_messages
             WHERE chat_id LIKE '%@lid' AND from_me=0 AND sender_phone<>''
            """
        ).fetchall()
        for row in sources:
            alias = normalize_message(str(row["chat_id"] or ""))[:120]
            digits = re.sub(r"\D", "", str(row["phone"] or ""))[:24]
            if alias.endswith("@lid") and digits:
                connection.execute(
                    """
                    INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(alias_id) DO UPDATE SET
                        canonical_id=excluded.canonical_id,
                        updated_at=excluded.updated_at
                    """,
                    (alias, f"{digits}@c.us", now),
                )

        # Дополнительное восстановление алиасов из уже сохранённой истории.
        # В новых версиях WhatsApp один личный чат может некоторое время
        # приходить как @lid, а затем как @c.us. Для входящего личного сообщения
        # sender_id часто уже содержит настоящий @c.us, даже когда chat_id ещё @lid.
        sender_rows = connection.execute(
            """
            SELECT chat_id, sender_id
              FROM whatsapp_chat_messages
             WHERE chat_id LIKE '%@lid'
               AND from_me=0
               AND sender_id LIKE '%@c.us'
             ORDER BY id DESC
             LIMIT 2000
            """
        ).fetchall()
        for row in sender_rows:
            alias = normalize_message(str(row["chat_id"] or ""))[:120]
            canonical = normalize_message(str(row["sender_id"] or ""))[:120]
            if alias.endswith("@lid") and canonical.endswith("@c.us") and alias != canonical:
                connection.execute(
                    """
                    INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(alias_id) DO UPDATE SET
                        canonical_id=excluded.canonical_id,
                        updated_at=excluded.updated_at
                    """,
                    (alias, canonical, now),
                )

        # Если sender_phone/sender_id в старой истории не заполнялись, одинаковый
        # provider message id остаётся безопасным признаком того, что @lid и @c.us
        # являются двумя представлениями одного чата. Message stanza у WhatsApp
        # глобально уникален, поэтому по имени/тексту сообщения здесь не гадаем.
        recent_rows = connection.execute(
            """
            SELECT chat_id, message_key
              FROM whatsapp_chat_messages
             WHERE chat_id LIKE '%@lid' OR chat_id LIKE '%@c.us'
             ORDER BY id DESC
             LIMIT 4000
            """
        ).fetchall()
        stanza_chats: dict[str, set[str]] = {}
        for row in recent_rows:
            chat = normalize_message(str(row["chat_id"] or ""))[:120]
            key = normalize_message(str(row["message_key"] or ""))[:160]
            if not chat or not key:
                continue
            _, stanza = queue_message_identity.parts(key)
            if len(stanza) < 8:
                continue
            stanza_chats.setdefault(stanza, set()).add(chat)
        alias_candidates: dict[str, set[str]] = {}
        for chats in stanza_chats.values():
            lids = {chat for chat in chats if chat.endswith("@lid")}
            phones = {chat for chat in chats if chat.endswith("@c.us")}
            if len(phones) != 1:
                continue
            canonical = next(iter(phones))
            for alias in lids:
                alias_candidates.setdefault(alias, set()).add(canonical)
        for alias, candidates in alias_candidates.items():
            # Не выбираем наугад, если один @lid каким-то образом пересёкся
            # сразу с несколькими телефонными чатами.
            if len(candidates) != 1:
                continue
            canonical = next(iter(candidates))
            connection.execute(
                """
                INSERT INTO whatsapp_chat_aliases(alias_id, canonical_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(alias_id) DO UPDATE SET
                    canonical_id=excluded.canonical_id,
                    updated_at=excluded.updated_at
                """,
                (alias, canonical, now),
            )

        aliases = connection.execute(
            "SELECT alias_id, canonical_id FROM whatsapp_chat_aliases WHERE alias_id<>canonical_id"
        ).fetchall()
        for row in aliases:
            alias = normalize_message(str(row["alias_id"] or ""))[:120]
            canonical = normalize_message(str(row["canonical_id"] or ""))[:120]
            if not alias.endswith("@lid") or not canonical.endswith("@c.us"):
                continue

            # Move history without rebuilding rows, so media, quotes, sender ids,
            # reactions and transcripts are never lost. If both aliases already
            # contain the same provider message, merge only the richer fields.
            message_rows = connection.execute(
                "SELECT * FROM whatsapp_chat_messages WHERE chat_id=? ORDER BY id",
                (alias,),
            ).fetchall()
            for message in message_rows:
                message_key = str(message["message_key"] or "")
                _, stanza = queue_message_identity.parts(message_key)
                candidates = connection.execute(
                    "SELECT * FROM whatsapp_chat_messages WHERE chat_id=? AND (message_key=? OR substr(message_key, -?)=?) ORDER BY id",
                    (canonical, message_key, max(1, len(stanza)), stanza),
                ).fetchall()
                matches = [candidate for candidate in candidates if queue_message_identity.same(candidate["message_key"], message_key)]
                existing = matches[0] if len(matches) == 1 else None
                if not existing:
                    connection.execute(
                        "UPDATE whatsapp_chat_messages SET chat_id=? WHERE id=?",
                        (canonical, message["id"]),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE whatsapp_chat_messages SET
                        ack=MAX(ack, ?),
                        sender=CASE WHEN sender='' THEN ? ELSE sender END,
                        sender_phone=CASE WHEN sender_phone='' THEN ? ELSE sender_phone END,
                        sender_id=CASE WHEN sender_id='' THEN ? ELSE sender_id END,
                        body=CASE WHEN body='' THEN ? ELSE body END,
                        message_type=CASE WHEN message_type IN ('','chat') AND ? NOT IN ('','chat') THEN ? ELSE message_type END,
                        message_timestamp=MAX(message_timestamp, ?),
                        media_path=CASE WHEN media_path='' THEN ? ELSE media_path END,
                        media_mime=CASE WHEN media_mime='' THEN ? ELSE media_mime END,
                        media_name=CASE WHEN media_name='' THEN ? ELSE media_name END,
                        transcript=CASE WHEN transcript='' THEN ? ELSE transcript END,
                        ticket_id=CASE WHEN ticket_id=0 THEN ? ELSE ticket_id END,
                        mentions_json=CASE WHEN mentions_json IN ('','[]') THEN ? ELSE mentions_json END,
                        quoted_message_key=CASE WHEN quoted_message_key='' THEN ? ELSE quoted_message_key END,
                        quoted_body=CASE WHEN quoted_body='' THEN ? ELSE quoted_body END,
                        quoted_sender=CASE WHEN quoted_sender='' THEN ? ELSE quoted_sender END,
                        forwarded=MAX(forwarded, ?), edited=MAX(edited, ?), deleted=MAX(deleted, ?),
                        reactions_json=CASE WHEN reactions_json IN ('','{}') THEN ? ELSE reactions_json END,
                        edit_timestamp=MAX(edit_timestamp, ?)
                    WHERE id=?
                    """,
                    (
                        int(message["ack"] or 0), str(message["sender"] or ""),
                        str(message["sender_phone"] or ""), str(message["sender_id"] or ""),
                        str(message["body"] or ""), str(message["message_type"] or ""),
                        str(message["message_type"] or ""), int(message["message_timestamp"] or 0),
                        str(message["media_path"] or ""), str(message["media_mime"] or ""),
                        str(message["media_name"] or ""), str(message["transcript"] or ""),
                        int(message["ticket_id"] or 0), str(message["mentions_json"] or "[]"),
                        str(message["quoted_message_key"] or ""), str(message["quoted_body"] or ""),
                        str(message["quoted_sender"] or ""), int(message["forwarded"] or 0),
                        int(message["edited"] or 0), int(message["deleted"] or 0),
                        str(message["reactions_json"] or "{}"), int(message["edit_timestamp"] or 0),
                        existing["id"],
                    ),
                )
                connection.execute("DELETE FROM whatsapp_chat_messages WHERE id=?", (message["id"],))

            alias_chat = connection.execute("SELECT * FROM whatsapp_chats WHERE chat_id=?", (alias,)).fetchone()
            canonical_chat = connection.execute("SELECT * FROM whatsapp_chats WHERE chat_id=?", (canonical,)).fetchone()
            if alias_chat and canonical_chat:
                alias_ts = int(alias_chat["last_timestamp"] or 0)
                canonical_ts = int(canonical_chat["last_timestamp"] or 0)
                best = alias_chat if alias_ts >= canonical_ts else canonical_chat
                alias_name = str(alias_chat["name"] or "").strip()
                canonical_name = str(canonical_chat["name"] or "").strip()
                canonical_digits = canonical.split("@", 1)[0]
                name = alias_name if alias_name and (not canonical_name or canonical_name == canonical_digits) else canonical_name
                connection.execute(
                    """
                    UPDATE whatsapp_chats SET name=?, last_message=?, last_timestamp=?, unread_count=?, updated_at=?
                    WHERE chat_id=?
                    """,
                    (name, str(best["last_message"] or ""), max(alias_ts, canonical_ts),
                     max(int(alias_chat["unread_count"] or 0), int(canonical_chat["unread_count"] or 0)),
                     str(best["updated_at"] or now), canonical),
                )
                connection.execute("DELETE FROM whatsapp_chats WHERE chat_id=?", (alias,))
            elif alias_chat:
                connection.execute("UPDATE whatsapp_chats SET chat_id=? WHERE chat_id=?", (canonical, alias))

            alias_contact = connection.execute("SELECT * FROM whatsapp_contacts WHERE chat_id=?", (alias,)).fetchone()
            canonical_contact = connection.execute("SELECT * FROM whatsapp_contacts WHERE chat_id=?", (canonical,)).fetchone()
            if alias_contact and not canonical_contact:
                connection.execute("UPDATE whatsapp_contacts SET chat_id=? WHERE chat_id=?", (canonical, alias))
            elif alias_contact:
                if not str(canonical_contact["name"] or "").strip() and str(alias_contact["name"] or "").strip():
                    connection.execute("UPDATE whatsapp_contacts SET name=? WHERE chat_id=?", (alias_contact["name"], canonical))
                connection.execute("DELETE FROM whatsapp_contacts WHERE chat_id=?", (alias,))

            alias_context = connection.execute("SELECT * FROM conversation_contexts WHERE contact_key=?", (alias,)).fetchone()
            canonical_context = connection.execute("SELECT * FROM conversation_contexts WHERE contact_key=?", (canonical,)).fetchone()
            if alias_context and not canonical_context:
                connection.execute("UPDATE conversation_contexts SET contact_key=? WHERE contact_key=?", (canonical, alias))
            elif alias_context:
                if str(alias_context["updated_at"] or "") > str(canonical_context["updated_at"] or ""):
                    connection.execute(
                        "UPDATE conversation_contexts SET pending_category=?, active_ticket_id=?, force_new=?, draft_json=?, updated_at=? WHERE contact_key=?",
                        (alias_context["pending_category"], alias_context["active_ticket_id"], alias_context["force_new"],
                         alias_context["draft_json"], alias_context["updated_at"], canonical),
                    )
                connection.execute("DELETE FROM conversation_contexts WHERE contact_key=?", (alias,))

            alias_permission = connection.execute("SELECT expires_at FROM whatsapp_call_permissions WHERE contact_key=?", (alias,)).fetchone()
            canonical_permission = connection.execute("SELECT expires_at FROM whatsapp_call_permissions WHERE contact_key=?", (canonical,)).fetchone()
            if alias_permission and not canonical_permission:
                connection.execute("UPDATE whatsapp_call_permissions SET contact_key=? WHERE contact_key=?", (canonical, alias))
            elif alias_permission:
                if str(alias_permission["expires_at"] or "") > str(canonical_permission["expires_at"] or ""):
                    connection.execute("UPDATE whatsapp_call_permissions SET expires_at=? WHERE contact_key=?", (alias_permission["expires_at"], canonical))
                connection.execute("DELETE FROM whatsapp_call_permissions WHERE contact_key=?", (alias,))

            connection.execute("UPDATE tickets SET chat_id=? WHERE chat_id=?", (canonical, alias))
            connection.execute("UPDATE outbound_messages SET chat_id=? WHERE chat_id=?", (canonical, alias))

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        event_type: str,
        actor: str,
        action: str,
        object_type: str = "",
        object_id: str | int = "",
        details: str = "",
        level: str = "info",
        created_at: str = "",
    ) -> None:
        safe_level = level if level in {"info", "warning", "error"} else "info"
        connection.execute(
            """INSERT INTO audit_log
               (event_type, actor, action, object_type, object_id, details, level, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalize_message(event_type)[:60] or "action",
                normalize_message(actor)[:100],
                normalize_message(action)[:300],
                normalize_message(object_type)[:60],
                normalize_message(str(object_id))[:120],
                normalize_message(details)[:1600],
                safe_level,
                created_at or utc_now(),
            ),
        )

    def add_audit(
        self,
        event_type: str,
        actor: str,
        action: str,
        object_type: str = "",
        object_id: str | int = "",
        details: str = "",
        level: str = "info",
    ) -> None:
        with self.connection() as connection:
            self._audit(connection, event_type, actor, action, object_type, object_id, details, level)

    def list_audit_entries(self, limit: int = 200, level: str = "", query: str = "") -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if level in {"info", "warning", "error"}:
            conditions.append("level = ?")
            params.append(level)
        clean_query = normalize_message(query).casefold()
        if clean_query:
            conditions.append("(FOLD(actor) LIKE ? OR FOLD(action) LIKE ? OR FOLD(details) LIKE ? OR FOLD(object_id) LIKE ?)")
            term = f"%{clean_query}%"
            params.extend([term, term, term, term])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(max(1, min(int(limit), 1000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_ticket(self, payload: dict[str, Any]) -> int:
        now = utc_now()
        fields = {
            "source": payload.get("source", "whatsapp"),
            "sender": payload.get("sender", "Неизвестный отправитель"),
            "phone": payload.get("phone", ""),
            "chat_id": payload.get("chat_id", ""),
            "external_id": payload.get("external_id", ""),
            "category": payload.get("category", "general"),
            "priority": payload.get("priority", "normal")
            if payload.get("priority", "normal") in PRIORITIES
            else "normal",
            "title": payload.get("title", "Новый запрос"),
            "summary": payload.get("summary", ""),
            "original_text": payload.get("original_text", ""),
            "attachment_name": payload.get("attachment_name", ""),
            "status": payload.get("status", "new"),
            "assigned_to": payload.get("assigned_to", ""),
            "bin_old": payload.get("bin_old", ""),
            "bin_new": payload.get("bin_new", ""),
            "ats_number": payload.get("ats_number", ""),
            "company": payload.get("company", ""),
            "country": payload.get("country", ""),
            "created_at": now,
            "updated_at": now,
            "closed_at": now if payload.get("status") in {"done", "invalid"} else "",
            "first_response_at": now if payload.get("status") not in {None, "", "new"} else "",
            "shift_handoff": 1 if payload.get("shift_handoff") else 0,
            "handoff_from": payload.get("handoff_from", ""),
            "handoff_at": payload.get("handoff_at", ""),
        }
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with self.connection() as connection:
            cursor = connection.execute(
                f"INSERT INTO tickets ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
            ticket_id = int(cursor.lastrowid)
            actor = str(payload.get("assigned_to", "") or "Система")
            connection.execute(
                "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, "Заявка создана", actor, now),
            )
            self._audit(
                connection, "ticket", actor, "Заявка создана", "ticket", ticket_id,
                f"{fields['title']} · {fields['sender']} · {fields['source']}", created_at=now,
            )
        return ticket_id

    def create_error_report(
        self, *, sender: str, phone: str = "", chat_id: str = "", external_id: str = "",
        description: str, attachment_name: str = ""
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        sender = normalize_message(sender)[:160] or "Неизвестный отправитель"
        phone = normalize_message(phone)[:40]
        chat_id = normalize_message(chat_id)[:120]
        external_id = normalize_message(external_id)[:160]
        description = normalize_message(description)[:32000]
        attachment_name = normalize_message(attachment_name)[:240]
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO error_reports
                    (sender, phone, chat_id, external_id, description, attachment_name, status, admin_note, created_at, updated_at, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, 'new', '', ?, ?, '')
                """,
                (sender, phone, chat_id, external_id, description, attachment_name, now, now),
            )
            report_id = int(cursor.lastrowid)
            self._audit(
                connection, "error_report", sender, "Репорт об ошибке создан",
                "error_report", report_id, description[:300], created_at=now,
            )
        return report_id

    def get_error_report(self, report_id: int) -> dict[str, Any] | None:
        try:
            report_id = int(report_id)
        except (TypeError, ValueError):
            return None
        if report_id <= 0:
            return None
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM error_reports WHERE id = ?", (report_id,)).fetchone()
        return dict(row) if row else None

    def list_error_reports(self, status: str = "", query: str = "", limit: int = 300) -> list[dict[str, Any]]:
        valid_statuses = {"new", "in_progress", "resolved", "rejected"}
        clauses: list[str] = []
        params: list[Any] = []
        if status in valid_statuses:
            clauses.append("status = ?")
            params.append(status)
        search = normalize_message(query).strip()
        if search:
            folded = f"%{search.casefold()}%"
            digits = re.sub(r"\D", "", search)
            search_clauses = [
                "FOLD(sender) LIKE ?", "FOLD(description) LIKE ?",
                "FOLD(attachment_name) LIKE ?", "CAST(id AS TEXT) LIKE ?",
            ]
            params.extend([folded, folded, folded, f"%{search}%"])
            if digits:
                search_clauses.append("DIGITS(phone) LIKE ?")
                params.append(f"%{digits}%")
            clauses.append("(" + " OR ".join(search_clauses) + ")")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(1000, int(limit or 300))))
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM error_reports{where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def error_report_counts(self) -> dict[str, int]:
        result = {"new": 0, "in_progress": 0, "resolved": 0, "rejected": 0}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM error_reports GROUP BY status"
            ).fetchall()
        for row in rows:
            result[str(row["status"])] = int(row["count"] or 0)
        return result

    def update_error_report(self, report_id: int, status: str, admin_note: str, actor: str = "") -> bool:
        valid_statuses = {"new", "in_progress", "resolved", "rejected"}
        if status not in valid_statuses:
            status = "new"
        try:
            report_id = int(report_id)
        except (TypeError, ValueError):
            return False
        if report_id <= 0:
            return False
        now = datetime.now(timezone.utc).isoformat()
        resolved_at = now if status in {"resolved", "rejected"} else ""
        note = normalize_message(admin_note)[:3000]
        actor = normalize_message(actor)[:160] or "Администратор"
        with self.connection() as connection:
            current = connection.execute("SELECT id FROM error_reports WHERE id = ?", (report_id,)).fetchone()
            if not current:
                return False
            connection.execute(
                "UPDATE error_reports SET status = ?, admin_note = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
                (status, note, now, resolved_at, report_id),
            )
            self._audit(
                connection, "error_report", actor, f"Репорт: {status}",
                "error_report", report_id, note[:300], created_at=now,
            )
        return True

    def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def list_tickets(
        self,
        status: str = "",
        category: str = "",
        priority: str = "",
        query: str = "",
        limit: int = 0,
        offset: int = 0,
        view: str = "",
        employee: str = "",
    ) -> list[dict[str, Any]]:
        where, params = self._ticket_filter(status, category, priority, query, view, employee)
        pagination = ""
        if limit > 0:
            pagination = " LIMIT ? OFFSET ?"
            params.extend([str(limit), str(max(0, offset))])
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM tickets{where} ORDER BY shift_handoff DESC, id DESC{pagination}",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_whatsapp_contacts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            manual_rows = connection.execute(
                """
                SELECT chat_id, phone, name, created_at
                FROM whatsapp_contacts
                ORDER BY name COLLATE NOCASE, created_at DESC
                """
            ).fetchall()
            rows = connection.execute(
                """
                SELECT chat_id, phone, sender, summary, created_at
                FROM tickets
                WHERE source = 'whatsapp' AND (chat_id <> '' OR phone <> '')
                ORDER BY id DESC
                LIMIT 500
                """
            ).fetchall()
        contacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in manual_rows:
            chat_id = str(row["chat_id"] or "")
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            contacts.append(
                {
                    "id": chat_id,
                    "name": str(row["name"] or row["phone"] or "Пользователь WhatsApp"),
                    "phone": str(row["phone"] or ""),
                    "last_message": "Добавлен администратором",
                    "created_at": str(row["created_at"] or ""),
                    "manual": True,
                }
            )
            if len(contacts) >= max(1, limit):
                return contacts
        for row in rows:
            chat_id = str(row["chat_id"] or "")
            if not chat_id:
                digits = re.sub(r"\D", "", str(row["phone"] or ""))
                chat_id = f"{digits}@c.us" if digits else ""
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            contacts.append(
                {
                    "id": chat_id,
                    "name": str(row["sender"] or "Пользователь WhatsApp"),
                    "phone": str(row["phone"] or ""),
                    "last_message": str(row["summary"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "manual": False,
                }
            )
            if len(contacts) >= max(1, limit):
                break
        return contacts

    def save_whatsapp_contact(self, name: str, phone: str, chat_id: str) -> bool:
        clean_name = normalize_message(name)[:100]
        clean_phone = normalize_message(phone)[:30]
        clean_chat_id = normalize_message(chat_id)[:120]
        if not clean_name or not clean_phone or not clean_chat_id.endswith("@c.us"):
            return False
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO whatsapp_contacts (chat_id, phone, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    phone = excluded.phone,
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (clean_chat_id, clean_phone, clean_name, now, now),
            )
        return True

    def delete_whatsapp_contact(self, chat_id: str) -> bool:
        clean_chat_id = normalize_message(chat_id)[:120]
        if not clean_chat_id:
            return False
        with self.connection() as connection:
            row = connection.execute(
                "SELECT phone FROM whatsapp_contacts WHERE chat_id = ? LIMIT 1",
                (clean_chat_id,),
            ).fetchone()
            phone_digits = re.sub(r"\D", "", str(row["phone"] if row else ""))[:24]
            if phone_digits:
                # Удаляем и возможный старый дубль того же номера. Это важно
                # после объединения @lid/@c.us: один оставшийся alias не должен
                # продолжать считать пользователя ручным контактом.
                cursor = connection.execute(
                    """
                    DELETE FROM whatsapp_contacts
                    WHERE chat_id = ?
                       OR chat_id = ?
                       OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') = ?
                    """,
                    (clean_chat_id, f"{phone_digits}@c.us", phone_digits),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM whatsapp_contacts WHERE chat_id = ?",
                    (clean_chat_id,),
                )

            # Контакт после удаления должен сразу стать обычным пользователем.
            # Чистим старые одноразовые разрешения на звонки и контекст для
            # точного chat_id и канонического номерного @c.us alias.
            cleanup_keys = {clean_chat_id}
            if phone_digits:
                cleanup_keys.add(f"{phone_digits}@c.us")
            for cleanup_key in cleanup_keys:
                if not cleanup_key:
                    continue
                connection.execute(
                    "DELETE FROM whatsapp_call_permissions WHERE contact_key = ?",
                    (cleanup_key,),
                )
                connection.execute(
                    "DELETE FROM conversation_contexts WHERE contact_key = ?",
                    (cleanup_key,),
                )
        return cursor.rowcount > 0

    def list_manual_whatsapp_contacts(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, phone, name, created_at, updated_at
                FROM whatsapp_contacts
                ORDER BY name COLLATE NOCASE, created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def manual_whatsapp_contact(
        self,
        chat_id: str = "",
        phone: str = "",
    ) -> dict[str, Any] | None:
        clean_chat_id = normalize_message(chat_id)[:120]
        phone_digits = re.sub(r"\D", "", normalize_message(phone))[:24]
        chat_digits = clean_chat_id.split("@", 1)[0] if clean_chat_id.endswith("@c.us") else ""
        candidates = [digits for digits in (phone_digits, chat_digits) if digits]
        with self.connection() as connection:
            if clean_chat_id:
                row = connection.execute(
                    """
                    SELECT chat_id, phone, name, created_at, updated_at
                    FROM whatsapp_contacts
                    WHERE chat_id = ?
                    LIMIT 1
                    """,
                    (clean_chat_id,),
                ).fetchone()
                if row:
                    return dict(row)
            for digits in candidates:
                row = connection.execute(
                    """
                    SELECT chat_id, phone, name, created_at, updated_at
                    FROM whatsapp_contacts
                    WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') = ?
                       OR chat_id = ?
                    LIMIT 1
                    """,
                    (digits, f"{digits}@c.us"),
                ).fetchone()
                if row:
                    return dict(row)
        return None

    def is_manual_whatsapp_contact(self, chat_id: str = "", phone: str = "") -> bool:
        return self.manual_whatsapp_contact(chat_id, phone) is not None

    def replace_whatsapp_groups(self, groups: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute("DELETE FROM whatsapp_groups WHERE added_by_admin IN (0, 2)")
            for group in groups:
                chat_id = normalize_message(str(group.get("id", "")))[:120]
                if not chat_id.endswith("@g.us"):
                    continue
                try:
                    participants = max(0, int(group.get("participant_count", 0) or 0))
                except (TypeError, ValueError):
                    participants = 0
                connection.execute(
                    """
                    INSERT INTO whatsapp_groups
                        (chat_id, name, participant_count, added_by_admin, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        name = excluded.name,
                        participant_count = excluded.participant_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_id,
                        normalize_message(str(group.get("name", "")))[:100]
                        or "Группа WhatsApp",
                        participants,
                        now,
                    ),
                )

    def save_whatsapp_group(self, name: str, chat_id: str) -> dict[str, Any] | None:
        clean_name = normalize_message(name)[:100]
        clean_chat_id = normalize_message(chat_id)[:120]
        if not clean_name or not re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", clean_chat_id):
            return None
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO whatsapp_groups
                    (chat_id, name, participant_count, added_by_admin, updated_at)
                VALUES (?, ?, 0, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    name = excluded.name,
                    added_by_admin = 1,
                    updated_at = excluded.updated_at
                """,
                (clean_chat_id, clean_name, now),
            )
        return {"chat_id": clean_chat_id, "name": clean_name}

    def set_whatsapp_group_muted(self, chat_id: str, muted: bool) -> bool:
        clean_chat_id = normalize_message(chat_id)[:120]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", clean_chat_id):
            return False
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE whatsapp_groups SET muted = ?, updated_at = ? WHERE chat_id = ? AND added_by_admin = 1",
                (1 if muted else 0, utc_now(), clean_chat_id),
            )
            if muted:
                # Старое упоминание не должно всплыть после последующего размьюта.
                connection.execute(
                    "UPDATE whatsapp_chats SET mention_unread_count = 0 WHERE chat_id = ?",
                    (clean_chat_id,),
                )
        return cursor.rowcount > 0

    def delete_whatsapp_group(self, chat_id: str) -> bool:
        clean_chat_id = normalize_message(chat_id)[:120]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", clean_chat_id):
            return False
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE whatsapp_groups SET added_by_admin = 2, updated_at = ? WHERE chat_id = ?",
                (utc_now(), clean_chat_id),
            )
        return cursor.rowcount > 0

    def list_whatsapp_groups(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT g.chat_id, g.name, g.participant_count, g.added_by_admin, g.muted, g.updated_at,
                       COALESCE(c.last_message, '') AS last_message,
                       COALESCE(c.last_timestamp, 0) AS last_timestamp,
                       COALESCE(c.unread_count, 0) AS unread_count,
                       COALESCE(c.mention_unread_count, 0) AS mention_unread_count,
                       COALESCE((
                           SELECT m.from_me
                           FROM whatsapp_chat_messages AS m
                           WHERE m.chat_id = g.chat_id AND m.deleted = 0
                           ORDER BY m.message_timestamp DESC, m.id DESC
                           LIMIT 1
                       ), 0) AS last_from_me
                FROM whatsapp_groups AS g
                LEFT JOIN whatsapp_chats AS c ON c.chat_id = g.chat_id
                WHERE g.added_by_admin = 1
                ORDER BY c.last_timestamp DESC, g.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_discovered_whatsapp_groups(self) -> list[dict[str, Any]]:
        """Groups visible to WhatsApp but not currently enabled in the system."""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, name, participant_count, added_by_admin, updated_at
                FROM whatsapp_groups
                WHERE added_by_admin = 0
                ORDER BY name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def allow_whatsapp_call(self, contact_key: str, minutes: int = 30) -> bool:
        clean_key = normalize_message(contact_key)[:120]
        if not clean_key:
            return False
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=max(1, min(120, minutes)))
        ).replace(microsecond=0).isoformat()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO whatsapp_call_permissions (contact_key, expires_at)
                VALUES (?, ?)
                ON CONFLICT(contact_key) DO UPDATE SET expires_at = excluded.expires_at
                """,
                (clean_key, expires_at),
            )
        return True

    def consume_whatsapp_call_permission(self, contact_key: str) -> bool:
        clean_key = normalize_message(contact_key)[:120]
        if not clean_key:
            return False
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT expires_at FROM whatsapp_call_permissions WHERE contact_key = ?",
                (clean_key,),
            ).fetchone()
            connection.execute(
                "DELETE FROM whatsapp_call_permissions WHERE contact_key = ?",
                (clean_key,),
            )
        return bool(row and str(row["expires_at"]) >= now)

    def upsert_whatsapp_chats(self, chats: list[dict[str, Any]]) -> None:
        if not chats:
            return
        now = utc_now()
        with self.connection() as connection:
            for chat in chats:
                chat_id = normalize_message(str(chat.get("id", "")))[:120]
                chat_id = self._canonical_whatsapp_chat_id(connection, chat_id)
                if not chat_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO whatsapp_chats
                        (chat_id, name, last_message, last_timestamp, unread_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE whatsapp_chats.name END,
                        last_message = CASE
                            WHEN excluded.last_timestamp >= whatsapp_chats.last_timestamp
                            THEN excluded.last_message ELSE whatsapp_chats.last_message END,
                        last_timestamp = MAX(whatsapp_chats.last_timestamp, excluded.last_timestamp),
                        unread_count = whatsapp_chats.unread_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_id,
                        normalize_message(str(chat.get("name", "")))[:100],
                        normalize_message(str(chat.get("last_message", "")))[:160],
                        max(0, int(chat.get("timestamp", 0) or 0)),
                        0,
                        now,
                    ),
                )
            self._merge_whatsapp_lid_aliases(connection)

    def save_whatsapp_chat_messages(
        self,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id or not messages:
            return
        now = utc_now()
        with self.connection() as connection:
            chat_id = self._canonical_whatsapp_chat_id(connection, chat_id)
            for item in messages:
                body = normalize_message(str(item.get("body", "")))[:32000]
                timestamp = max(0, int(item.get("timestamp", 0) or 0))
                from_me = bool(item.get("from_me"))
                ack = max(0, min(4, int(item.get("ack", 0) or 0)))
                message_key = normalize_message(str(item.get("id", "")))[:160]
                if not message_key:
                    continue  # No stable provider ID: wait for the next valid observation.
                _, stanza = queue_message_identity.parts(message_key)
                if len(stanza) >= 8:
                    variants = connection.execute("SELECT message_key FROM whatsapp_chat_messages WHERE chat_id=? AND (message_key=? OR substr(message_key, -?)=?)", (chat_id, stanza, len(stanza), stanza)).fetchall()
                    variants = [row["message_key"] for row in variants if queue_message_identity.same(row["message_key"], message_key)]
                    if len(variants) == 1: message_key = variants[0]
                sender = normalize_message(str(item.get("sender", "")))[:100]
                sender_phone = normalize_message(str(item.get("sender_phone", "")))[:50]
                sender_id = normalize_message(str(item.get("sender_id", "")))[:120]
                contact_key = sender_id if chat_id.endswith("@g.us") else chat_id
                manual = connection.execute("SELECT name FROM whatsapp_contacts WHERE chat_id=?", (contact_key,)).fetchone()
                if manual and not from_me and manual["name"]:
                    sender = manual["name"]
                # Provider IDs, not matching text, identify a message. Repeated
                # identical messages can be intentional and must stay separate.
                media_path = str(item.get("media_path", ""))[:500]
                media_mime = normalize_message(str(item.get("media_mime", "")))[:120]
                media_name = normalize_message(str(item.get("media_name", "")))[:180]
                transcript = normalize_message(str(item.get("transcript", "")))[:8000]
                try:
                    ticket_id = max(0, int(item.get("ticket_id", 0) or 0))
                except (ValueError, TypeError):
                    ticket_id = 0
                raw_mentions = item.get("mentions", [])
                if not isinstance(raw_mentions, list):
                    raw_mentions = []
                mentions_json = json.dumps(raw_mentions[:100], ensure_ascii=False)
                # WhatsApp can render a group-wide @all mention without returning
                # it through the ordinary mention metadata. Treat a standalone
                # inbound @all token as a real notification mention as well.
                all_mention = bool(
                    chat_id.endswith("@g.us")
                    and not from_me
                    and re.search(r"(?<!\w)@all(?!\w)", body, re.IGNORECASE)
                )
                notify = 1 if item.get("notify") or all_mention else 0
                quoted_message_key = normalize_message(str(item.get("quoted_message_key", "")))[:160]
                quoted_body = normalize_message(str(item.get("quoted_body", "")))[:1200]
                quoted_sender = normalize_message(str(item.get("quoted_sender", "")))[:100]
                forwarded = 1 if item.get("forwarded") else 0
                edited = 1 if item.get("edited") else 0
                edit_timestamp = max(0, int(item.get("edit_timestamp", 0) or 0))
                deleted = 1 if item.get("deleted") else 0
                raw_reactions = item.get("reactions", {})
                reaction_map: dict[str, str] = {}
                if isinstance(raw_reactions, dict):
                    for reactor, emoji in list(raw_reactions.items())[:200]:
                        reactor_key = normalize_message(str(reactor))[:160]
                        reaction_emoji = str(emoji or "")[:32]
                        if reactor_key and reaction_emoji:
                            reaction_map[reactor_key] = reaction_emoji
                elif isinstance(raw_reactions, list):
                    for reaction in raw_reactions[:200]:
                        if not isinstance(reaction, dict):
                            continue
                        reactor_key = normalize_message(str(reaction.get("sender_id", "")))[:160]
                        if reaction.get("from_me"):
                            reactor_key = "__me__"
                        reaction_emoji = str(reaction.get("emoji", "") or "")[:32]
                        if reactor_key and reaction_emoji:
                            reaction_map[reactor_key] = reaction_emoji
                reactions_json = json.dumps(reaction_map, ensure_ascii=False, separators=(",", ":"))
                exists = connection.execute(
                    "SELECT 1 FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                    (chat_id, message_key),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO whatsapp_chat_messages
                        (message_key, chat_id, from_me, sender, sender_phone, sender_id, body, message_type,
                         message_timestamp, ack, media_path, media_mime, media_name,
                         transcript, ticket_id, mentions_json, notify, quoted_message_key, quoted_body,
                         quoted_sender, forwarded, edited, deleted, reactions_json, created_at, edit_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_key) DO UPDATE SET
                        ack = MAX(whatsapp_chat_messages.ack, excluded.ack),
                        sender = CASE WHEN whatsapp_chat_messages.from_me=1 AND whatsapp_chat_messages.sender='Система' THEN whatsapp_chat_messages.sender WHEN excluded.sender <> '' THEN excluded.sender ELSE whatsapp_chat_messages.sender END,
                        sender_phone = CASE WHEN excluded.sender_phone <> '' THEN excluded.sender_phone ELSE whatsapp_chat_messages.sender_phone END,
                        sender_id = CASE WHEN excluded.sender_id <> '' THEN excluded.sender_id ELSE whatsapp_chat_messages.sender_id END,
                        body = CASE WHEN whatsapp_chat_messages.deleted=1 THEN '' WHEN whatsapp_chat_messages.edited=1 AND (excluded.edited=0 OR excluded.edit_timestamp <= whatsapp_chat_messages.edit_timestamp) AND excluded.deleted=0 THEN whatsapp_chat_messages.body WHEN excluded.body <> '' OR excluded.deleted=1 OR excluded.edited=1 THEN excluded.body ELSE whatsapp_chat_messages.body END,
                        message_type = CASE WHEN excluded.message_type <> '' THEN excluded.message_type ELSE whatsapp_chat_messages.message_type END,
                        media_path = CASE WHEN excluded.media_path <> '' THEN excluded.media_path ELSE whatsapp_chat_messages.media_path END,
                        media_mime = CASE WHEN excluded.media_mime <> '' THEN excluded.media_mime ELSE whatsapp_chat_messages.media_mime END,
                        media_name = CASE WHEN excluded.media_name <> '' THEN excluded.media_name ELSE whatsapp_chat_messages.media_name END,
                        transcript = CASE WHEN excluded.transcript <> '' THEN excluded.transcript ELSE whatsapp_chat_messages.transcript END,
                        ticket_id = CASE WHEN excluded.ticket_id > 0 THEN excluded.ticket_id ELSE whatsapp_chat_messages.ticket_id END,
                        mentions_json = CASE WHEN excluded.mentions_json <> '[]' THEN excluded.mentions_json ELSE whatsapp_chat_messages.mentions_json END,
                        notify = MAX(whatsapp_chat_messages.notify, excluded.notify),
                        quoted_message_key = CASE WHEN excluded.quoted_message_key <> '' THEN excluded.quoted_message_key ELSE whatsapp_chat_messages.quoted_message_key END,
                        quoted_body = CASE WHEN excluded.quoted_body <> '' THEN excluded.quoted_body ELSE whatsapp_chat_messages.quoted_body END,
                        quoted_sender = CASE WHEN excluded.quoted_sender <> '' THEN excluded.quoted_sender ELSE whatsapp_chat_messages.quoted_sender END,
                        forwarded = MAX(whatsapp_chat_messages.forwarded, excluded.forwarded),
                        edited = MAX(whatsapp_chat_messages.edited, excluded.edited),
                        edit_timestamp = MAX(whatsapp_chat_messages.edit_timestamp, excluded.edit_timestamp),
                        deleted = MAX(whatsapp_chat_messages.deleted, excluded.deleted),
                        reactions_json = CASE WHEN excluded.reactions_json <> '{}' THEN excluded.reactions_json ELSE whatsapp_chat_messages.reactions_json END
                    """,
                    (
                        message_key, chat_id, int(from_me), sender, sender_phone, sender_id, body,
                        normalize_message(str(item.get("type", "chat")))[:40] or "chat",
                        timestamp, ack, media_path, media_mime, media_name, transcript, ticket_id,
                        mentions_json, notify, quoted_message_key, quoted_body, quoted_sender, forwarded,
                        edited, deleted, reactions_json, now, edit_timestamp,
                    ),
                )
                latest_visible = connection.execute(
                    """SELECT body, media_name, message_timestamp
                         FROM whatsapp_chat_messages
                        WHERE chat_id = ? AND deleted = 0
                        ORDER BY message_timestamp DESC, id DESC LIMIT 1""",
                    (chat_id,),
                ).fetchone()
                if latest_visible:
                    preview = latest_visible["body"] or latest_visible["media_name"] or "[Вложение]"
                    preview_timestamp = int(latest_visible["message_timestamp"] or 0)
                else:
                    preview, preview_timestamp = "", 0
                connection.execute(
                    """
                    INSERT INTO whatsapp_chats
                        (chat_id, name, last_message, last_timestamp, unread_count, updated_at)
                    VALUES (?, '', ?, ?, 0, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        last_message = excluded.last_message,
                        last_timestamp = excluded.last_timestamp,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, str(preview)[:160], preview_timestamp, now),
                )
                should_increment = False
                mention_increment = False
                if chat_id.endswith("@g.us"):
                    # Число справа у группы считает все входящие, как в WhatsApp.
                    # Отдельный счётчик упоминаний нужен только для колокольчика/
                    # системных уведомлений и растёт лишь при реальном @упоминании нас.
                    should_increment = (not from_me and not exists)
                    mention_increment = bool(should_increment and notify)
                elif not from_me and not exists:
                    manual_row = connection.execute(
                        "SELECT 1 FROM whatsapp_contacts WHERE chat_id = ? LIMIT 1",
                        (chat_id,),
                    ).fetchone()
                    should_increment = manual_row is not None
                if should_increment:
                    connection.execute(
                        "UPDATE whatsapp_chats SET unread_count = unread_count + 1 WHERE chat_id = ?",
                        (chat_id,),
                    )
                if mention_increment:
                    connection.execute(
                        "UPDATE whatsapp_chats SET mention_unread_count = mention_unread_count + 1 WHERE chat_id = ?",
                        (chat_id,),
                    )
            self._merge_whatsapp_lid_aliases(connection)

    def update_whatsapp_message_ack(
        self,
        chat_id: str,
        message_key: str,
        ack: int,
    ) -> bool:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        safe_ack = max(0, min(4, int(ack)))
        if not chat_id or not message_key:
            return False
        original = self.resolve_whatsapp_message(chat_id, message_key)
        if original: message_key = original["message_key"]
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE whatsapp_chat_messages
                SET ack = MAX(ack, ?)
                WHERE chat_id = ? AND message_key = ?
                """,
                (safe_ack, chat_id, message_key),
            )
            if cursor.rowcount == 0:
                # WhatsApp может прислать подтверждение под @c.us, а входящий
                # чат ранее сохранить под @lid. ID самого сообщения совпадает.
                cursor = connection.execute(
                    """
                    UPDATE whatsapp_chat_messages
                    SET ack = MAX(ack, ?)
                    WHERE message_key = ?
                    """,
                    (safe_ack, message_key),
                )
            return cursor.rowcount > 0

    def update_whatsapp_group_message_identities(
        self,
        chat_id: str,
        identities: list[dict[str, Any]],
    ) -> int:
        """Update sender metadata only for group messages that already exist.

        This is intentionally update-only: the connector may inspect older loaded
        WhatsApp models to recover an author name/number, but must never import
        historical messages that were not already saved by the system.
        """
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id.endswith("@g.us") or not identities:
            return 0
        generic = {"", "участник", "участник группы", "пользователь whatsapp"}
        updated = 0
        with self.connection() as connection:
            for item in identities[:100]:
                message_key = normalize_message(str(item.get("id", "")))[:160]
                if not message_key:
                    continue
                row = connection.execute(
                    "SELECT sender, sender_phone, sender_id FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                    (chat_id, message_key),
                ).fetchone()
                if not row:
                    continue
                old_sender = normalize_message(str(row["sender"] or ""))[:100]
                old_phone = normalize_message(str(row["sender_phone"] or ""))[:50]
                old_sender_id = normalize_message(str(row["sender_id"] or ""))[:120]
                new_sender = normalize_message(str(item.get("sender", "")))[:100]
                new_phone = normalize_message(str(item.get("sender_phone", "")))[:50]
                new_sender_id = normalize_message(str(item.get("sender_id", "")))[:120]

                # Never downgrade an already useful display name to a generic
                # placeholder or a raw number. Upgrade generic/number-only names
                # as soon as WhatsApp exposes a real pushname/contact name.
                old_folded = old_sender.casefold()
                new_folded = new_sender.casefold()
                old_is_generic = old_folded in generic or bool(re.fullmatch(r"[+\d\s().-]+", old_sender or ""))
                new_is_useful = bool(new_sender) and new_folded not in generic and not bool(re.fullmatch(r"[+\d\s().-]+", new_sender))
                sender_value = new_sender if (new_is_useful and old_is_generic) else old_sender
                phone_value = new_phone or old_phone
                sender_id_value = new_sender_id or old_sender_id
                if sender_value == old_sender and phone_value == old_phone and sender_id_value == old_sender_id:
                    continue
                connection.execute(
                    """
                    UPDATE whatsapp_chat_messages
                    SET sender = ?, sender_phone = ?, sender_id = ?
                    WHERE chat_id = ? AND message_key = ?
                    """,
                    (sender_value, phone_value, sender_id_value, chat_id, message_key),
                )
                updated += 1
        return updated

    def mark_whatsapp_chat_read(self, chat_id: str) -> None:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id:
            return
        with self.connection() as connection:
            connection.execute(
                "UPDATE whatsapp_chats SET unread_count = 0, mention_unread_count = 0 "
                "WHERE chat_id = ? AND (unread_count <> 0 OR mention_unread_count <> 0)",
                (chat_id,),
            )

    def list_saved_whatsapp_chats(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.chat_id, c.name, c.last_message, c.last_timestamp, c.unread_count,
                       COALESCE((
                           SELECT m.from_me
                           FROM whatsapp_chat_messages AS m
                           WHERE m.chat_id = c.chat_id AND m.deleted = 0
                           ORDER BY m.message_timestamp DESC, m.id DESC
                           LIMIT 1
                       ), 0) AS last_from_me
                FROM whatsapp_chats AS c
                WHERE c.chat_id NOT LIKE '%@g.us'
                ORDER BY last_timestamp DESC, updated_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [
            {
                "id": row["chat_id"],
                "name": row["name"] or str(row["chat_id"]).split("@")[0],
                "last_message": row["last_message"],
                "timestamp": int(row["last_timestamp"] or 0),
                "unread_count": int(row["unread_count"] or 0),
                "last_from_me": bool(row["last_from_me"]),
            }
            for row in rows
        ]

    @staticmethod
    def _reaction_summary(raw: object) -> list[dict[str, Any]]:
        try:
            values = json.loads(str(raw or "{}")) if not isinstance(raw, dict) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        counts: dict[str, int] = {}
        my_reaction = str(values.get("__me__", "") or "")
        for emoji in values.values():
            value = str(emoji or "")[:32]
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        return [
            {"emoji": emoji, "count": count, "me": emoji == my_reaction}
            for emoji, count in counts.items()
        ]

    def update_whatsapp_message_reaction(
        self, chat_id: str, message_key: str, sender_id: str, emoji: str, *, from_me: bool = False
    ) -> bool:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        sender_key = "__me__" if from_me else normalize_message(sender_id)[:160]
        reaction = str(emoji or "")[:32]
        if not chat_id or not message_key or not sender_key:
            return False
        with self.connection() as connection:
            row = connection.execute(
                "SELECT reactions_json FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                (chat_id, message_key),
            ).fetchone()
            if not row:
                return False
            try:
                values = json.loads(str(row["reactions_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                values = {}
            if not isinstance(values, dict):
                values = {}
            if reaction:
                values[sender_key] = reaction
            else:
                values.pop(sender_key, None)
            cursor = connection.execute(
                "UPDATE whatsapp_chat_messages SET reactions_json = ? WHERE chat_id = ? AND message_key = ?",
                (json.dumps(values, ensure_ascii=False, separators=(",", ":")), chat_id, message_key),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _chat_message_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            mentions = json.loads(str(row["mentions_json"] or "[]"))
            if not isinstance(mentions, list):
                mentions = []
        except (TypeError, ValueError, json.JSONDecodeError):
            mentions = []
        return {
            "id": row["message_key"],
            "history_row_id": int(row["id"] or 0),
            "from_me": bool(row["from_me"]),
            "sender": row["sender"],
            "sender_phone": row["sender_phone"],
            "sender_id": row["sender_id"],
            "body": row["body"],
            "type": row["message_type"],
            "timestamp": int(row["message_timestamp"] or 0),
            "ack": int(row["ack"] or 0),
            "media_path": row["media_path"],
            "media_mime": row["media_mime"],
            "media_name": row["media_name"],
            "transcript": row["transcript"],
            "ticket_id": int(row["ticket_id"] or 0),
            "mentions": mentions,
            "quoted_message_key": row["quoted_message_key"],
            "quoted_body": row["quoted_body"],
            "quoted_sender": row["quoted_sender"],
            "forwarded": bool(row["forwarded"]),
            "edited": bool(row["edited"]),
            "edit_timestamp": int(row["edit_timestamp"] or 0),
            "deleted": bool(row["deleted"]),
            "reactions": TicketStore._reaction_summary(row["reactions_json"] if "reactions_json" in row.keys() else "{}"),
        }

    def list_saved_whatsapp_messages_page(
        self, chat_id: str, limit: int = 50, cursor: str = ""
    ) -> dict[str, Any]:
        chat_id = normalize_message(chat_id)[:120]
        safe_limit = max(1, min(int(limit), 100))
        conditions = ["chat_id = ?"]
        params: list[Any] = [chat_id]
        if cursor:
            try:
                ts_text, row_text = cursor.split(":", 1)
                before_ts, before_row = int(ts_text), int(row_text)
            except (ValueError, TypeError):
                before_ts = before_row = 0
            if before_row > 0:
                conditions.append("(message_timestamp < ? OR (message_timestamp = ? AND id < ?))")
                params.extend([before_ts, before_ts, before_row])
        params.append(safe_limit + 1)
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT id, message_key, from_me, sender, sender_phone, sender_id, body, message_type, message_timestamp, ack,
                           media_path, media_mime, media_name, transcript, ticket_id, mentions_json,
                           quoted_message_key, quoted_body, quoted_sender, forwarded, edited, deleted, edit_timestamp, reactions_json
                    FROM whatsapp_chat_messages
                    WHERE {' AND '.join(conditions)}
                    ORDER BY message_timestamp DESC, id DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        has_more = len(rows) > safe_limit
        rows = rows[:safe_limit]
        next_cursor = ""
        if rows:
            oldest = rows[-1]
            next_cursor = f"{int(oldest['message_timestamp'] or 0)}:{int(oldest['id'])}"
        messages = [self._chat_message_row(row) for row in reversed(rows)]
        return {"messages": messages, "has_more": has_more, "cursor": next_cursor}

    def list_saved_whatsapp_messages(
        self,
        chat_id: str,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        return list(self.list_saved_whatsapp_messages_page(chat_id, limit).get("messages", []))

    def link_whatsapp_message_to_ticket(self, chat_id: str, message_key: str, ticket_id: int) -> bool:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        try:
            safe_ticket_id = max(0, int(ticket_id))
        except (TypeError, ValueError):
            safe_ticket_id = 0
        if not chat_id or not message_key or not safe_ticket_id:
            return False
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE whatsapp_chat_messages SET ticket_id = ? WHERE chat_id = ? AND message_key = ?",
                (safe_ticket_id, chat_id, message_key),
            )
            return cursor.rowcount > 0

    def link_recent_whatsapp_images_to_ticket(
        self,
        chat_id: str,
        ticket_id: int,
        before_timestamp: int = 0,
        lookback_seconds: int = 600,
        limit: int = 12,
    ) -> list[str]:
        """Attach recent unassigned inbound images sent before ticket creation.

        This is intentionally limited to private chats. Group chats can contain
        several senders and must never donate another participant's image to a
        ticket. Only locally preserved images with ticket_id=0 are considered.
        """
        clean_chat_id = normalize_message(chat_id)[:120]
        try:
            safe_ticket_id = max(0, int(ticket_id))
        except (TypeError, ValueError):
            safe_ticket_id = 0
        if not clean_chat_id or not safe_ticket_id or clean_chat_id.endswith("@g.us"):
            return []
        try:
            safe_before = max(0, int(before_timestamp or 0))
        except (TypeError, ValueError):
            safe_before = 0
        try:
            safe_lookback = max(60, min(3600, int(lookback_seconds or 600)))
        except (TypeError, ValueError):
            safe_lookback = 600
        try:
            safe_limit = max(1, min(24, int(limit or 12)))
        except (TypeError, ValueError):
            safe_limit = 12

        with self.connection() as connection:
            canonical = self._canonical_whatsapp_chat_id(connection, clean_chat_id)
            params: list[Any] = [canonical]
            time_clause = ""
            if safe_before > 0:
                cutoff = max(0, safe_before - safe_lookback)
                time_clause = "AND message_timestamp BETWEEN ? AND ?"
                params.extend([cutoff, safe_before])
            else:
                # A missing provider timestamp is unusual, but should not make the
                # feature unusable. In that case use the local receive time only.
                cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=safe_lookback)).isoformat()
                time_clause = "AND created_at >= ?"
                params.append(cutoff_iso)
            params.append(safe_limit)
            rows = connection.execute(
                f"""
                SELECT id, message_key
                FROM whatsapp_chat_messages
                WHERE chat_id = ?
                  AND from_me = 0
                  AND ticket_id = 0
                  AND deleted = 0
                  AND media_path <> ''
                  AND (media_mime LIKE 'image/%' OR message_type = 'image')
                  {time_clause}
                ORDER BY message_timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            if not rows:
                return []
            row_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in row_ids)
            connection.execute(
                f"UPDATE whatsapp_chat_messages SET ticket_id = ? "
                f"WHERE ticket_id = 0 AND id IN ({placeholders})",
                [safe_ticket_id, *row_ids],
            )
            # Return chronological provider keys for diagnostics/tests.
            return [str(row["message_key"]) for row in reversed(rows)]

    def list_ticket_whatsapp_media(
        self, ticket_id: int, chat_id: str = "", external_id: str = "", limit: int = 24
    ) -> list[dict[str, Any]]:
        try:
            safe_ticket_id = max(0, int(ticket_id))
        except (TypeError, ValueError):
            safe_ticket_id = 0
        chat_id = normalize_message(chat_id)[:120]
        external_id = normalize_message(external_id)[:160]
        if not safe_ticket_id and not external_id:
            return []
        clauses = []
        params: list[Any] = []
        if safe_ticket_id:
            clauses.append("ticket_id = ?")
            params.append(safe_ticket_id)
        if chat_id and external_id:
            clauses.append("(chat_id = ? AND message_key = ?)")
            params.extend([chat_id, external_id])
        where = " OR ".join(clauses) or "0"
        params.append(max(1, min(100, int(limit))))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT message_key, chat_id, from_me, sender, body, message_type, message_timestamp,
                       media_path, media_mime, media_name, transcript, ticket_id, deleted
                FROM whatsapp_chat_messages
                WHERE ({where}) AND media_path <> '' AND deleted = 0
                ORDER BY message_timestamp ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_whatsapp_message(self, chat_id: str, message_key: str):
        return queue_quote_lookup.resolve(self, chat_id, message_key)

    def chat_activity(self):
        """One indexed lookup per chat, including groups. No per-message SQL."""
        with self.connection() as connection:
            rows = connection.execute("""
                SELECT m.*, n.from_me AS relevant_from_me, n.message_timestamp AS waiting_since
                FROM whatsapp_chats c JOIN whatsapp_chat_messages m ON m.id=(SELECT id FROM whatsapp_chat_messages
                         WHERE chat_id=c.chat_id AND deleted=0 ORDER BY message_timestamp DESC, id DESC LIMIT 1)
                LEFT JOIN whatsapp_chat_messages n ON n.id=(SELECT id FROM whatsapp_chat_messages
                    WHERE chat_id=c.chat_id AND deleted=0 AND NOT (from_me=1 AND sender='Система')
                    ORDER BY message_timestamp DESC, id DESC LIMIT 1)
            """).fetchall()
        return {row["chat_id"]: {
            "last_sender": row["sender"], "last_sender_id": row["sender_id"],
            "last_from_me": bool(row["from_me"]), "last_message": row["body"] or queue_message_identity.media_label(dict(row)),
            "last_ack": int(row["ack"] or 0),
            "timestamp": row["message_timestamp"],
            "needs_reply": row["relevant_from_me"] == 0, "waiting_since": row["waiting_since"] or 0,
        } for row in rows}

    def get_whatsapp_message(self, chat_id: str, message_key: str) -> dict[str, Any] | None:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        if not chat_id or not message_key:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                (chat_id, message_key),
            ).fetchone()
        return dict(row) if row else None

    def update_whatsapp_message_content(
        self, chat_id: str, message_key: str, body: str, *, edited: bool = False, deleted: bool = False
    ) -> bool:
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        body = normalize_message(body)[:4000]
        if not chat_id or not message_key:
            return False
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE whatsapp_chat_messages
                   SET body = ?, edited = MAX(edited, ?), deleted = MAX(deleted, ?)
                   WHERE chat_id = ? AND message_key = ?""",
                (body, int(bool(edited)), int(bool(deleted)), chat_id, message_key),
            )
            return cursor.rowcount > 0

    def count_tickets(
        self,
        status: str = "",
        category: str = "",
        priority: str = "",
        query: str = "",
        view: str = "",
        employee: str = "",
    ) -> int:
        where, params = self._ticket_filter(status, category, priority, query, view, employee)
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS amount FROM tickets{where}",
                params,
            ).fetchone()
        return int(row["amount"])

    @staticmethod
    def _ticket_filter(
        status: str,
        category: str,
        priority: str,
        query: str,
        view: str = "",
        employee: str = "",
    ) -> tuple[str, list[str]]:
        conditions: list[str] = []
        params: list[str] = []
        if status in STATUSES:
            conditions.append("status = ?")
            params.append(status)
        if category in CATEGORIES:
            conditions.append("category = ?")
            params.append(category)
        if priority in PRIORITIES:
            conditions.append("priority = ?")
            params.append(priority)
        if view == "mine" and employee:
            conditions.extend(["assigned_to = ?", "status IN ('new','in_progress')"] )
            params.append(employee)
        elif view == "handoff":
            conditions.extend(["shift_handoff = 1", "status IN ('new','in_progress')"] )
        elif view == "open":
            conditions.append("status IN ('new','in_progress')")
        elif view == "urgent":
            conditions.extend(["status IN ('new','in_progress')", "priority = 'urgent'"] )
        clean_query = normalize_message(query).casefold()
        query_digits = re.sub(r"\D", "", clean_query)
        phone_only = bool(len(query_digits) >= 7 and re.fullmatch(r"[+\d\s().-]+", clean_query))
        if phone_only:
            canonical_digits = ("7" + query_digits[1:]) if len(query_digits) == 11 and query_digits.startswith("8") else query_digits
            # Длинная цифровая строка может быть не только телефоном, но и БИН/АТС/ТД.
            # Поэтому форматированный телефон ищем по phone/chat_id, а те же цифры
            # одновременно проверяем в рабочих идентификаторах и тексте заявки.
            conditions.append(
                "(DIGITS(phone) LIKE ? OR DIGITS(chat_id) LIKE ? OR DIGITS(bin_old) LIKE ? "
                "OR DIGITS(bin_new) LIKE ? OR DIGITS(ats_number) LIKE ? OR FOLD(original_text) LIKE ? "
                "OR FOLD(summary) LIKE ?)"
            )
            params.extend([
                f"%{canonical_digits}%", f"%{canonical_digits}%", f"%{query_digits}%",
                f"%{query_digits}%", f"%{query_digits}%", f"%{query_digits}%", f"%{query_digits}%",
            ])
            tokens: list[str] = []
        else:
            tokens = [token for token in re.split(r"\s+", clean_query) if token][:8]
        category_aliases = {
            key: normalize_message(label).casefold() for key, label in CATEGORIES.items()
        }
        searchable = [
            "CAST(id AS TEXT)", "source", "sender", "phone", "chat_id", "category",
            "title", "summary", "original_text", "ats_number", "bin_old", "bin_new",
            "company", "country",
        ]
        for token in tokens:
            token_conditions = [f"FOLD({field}) LIKE ?" for field in searchable]
            token_params = [f"%{token}%"] * len(searchable)
            digits = re.sub(r"\D", "", token)
            if digits:
                canonical_digits = ("7" + digits[1:]) if len(digits) == 11 and digits.startswith("8") else digits
                token_conditions.extend(["DIGITS(phone) LIKE ?", "DIGITS(chat_id) LIKE ?"])
                token_params.extend([f"%{canonical_digits}%", f"%{canonical_digits}%"])
            matching_categories = [
                key for key, label in category_aliases.items()
                if token in label or label in token
            ]
            if matching_categories:
                token_conditions.append("category IN (" + ",".join("?" for _ in matching_categories) + ")")
                token_params.extend(matching_categories)
            conditions.append("(" + " OR ".join(token_conditions) + ")")
            params.extend(token_params)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

    def counts(self) -> dict[str, int]:
        result = {status: 0 for status in STATUSES}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS amount FROM tickets GROUP BY status"
            ).fetchall()
        for row in rows:
            result[row["status"]] = row["amount"]
        result["all"] = sum(result.values())
        return result

    def first_unread_whatsapp_group(self) -> str:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT c.chat_id
                FROM whatsapp_chats AS c
                INNER JOIN whatsapp_groups AS g ON g.chat_id = c.chat_id
                WHERE g.added_by_admin = 1 AND c.unread_count > 0
                ORDER BY c.last_timestamp DESC, c.updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["chat_id"]) if row else ""

    def first_unread_whatsapp_contact(self) -> str:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT c.chat_id
                FROM whatsapp_chats AS c
                INNER JOIN whatsapp_contacts AS m ON m.chat_id = c.chat_id
                WHERE c.unread_count > 0
                  AND c.chat_id NOT LIKE '%@g.us'
                  AND c.chat_id NOT LIKE '%@broadcast'
                  AND c.chat_id NOT LIKE '%@newsletter'
                ORDER BY c.last_timestamp DESC, c.updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["chat_id"]) if row else ""

    def first_unread_manual_whatsapp_contact(self) -> str:
        # Only contacts explicitly added by an administrator generate alerts.
        return self.first_unread_whatsapp_contact()

    def notification_counts(self) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'new' AND category <> 'support' THEN 1 ELSE 0 END) AS tickets,
                    SUM(CASE WHEN status = 'new' AND category = 'support' THEN 1 ELSE 0 END) AS support
                FROM tickets
                """
            ).fetchone()
            groups = connection.execute(
                """
                SELECT COALESCE(SUM(c.mention_unread_count), 0) AS amount
                FROM whatsapp_chats AS c
                INNER JOIN whatsapp_groups AS g ON g.chat_id = c.chat_id
                WHERE g.added_by_admin = 1 AND COALESCE(g.muted, 0) = 0
                """
            ).fetchone()
            contacts = connection.execute(
                """
                SELECT COALESCE(SUM(c.unread_count), 0) AS amount
                FROM whatsapp_chats AS c
                INNER JOIN whatsapp_contacts AS m ON m.chat_id = c.chat_id
                WHERE c.chat_id NOT LIKE '%@g.us'
                  AND c.chat_id NOT LIKE '%@broadcast'
                  AND c.chat_id NOT LIKE '%@newsletter'
                """
            ).fetchone()
        return {
            "tickets": int(rows["tickets"] or 0),
            "support": int(rows["support"] or 0),
            "groups": int(groups["amount"] or 0),
            "contacts": int(contacts["amount"] or 0),
        }

    def notification_events(self, limit: int = 12) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self.connection() as connection:
            tickets = connection.execute(
                """SELECT id, sender, phone, category, title, created_at
                   FROM tickets WHERE status = 'new' ORDER BY id DESC LIMIT ?""",
                (max(1, min(limit, 20)),),
            ).fetchall()
            for row in tickets:
                category = str(row["category"] or "")
                events.append({
                    "id": f"ticket:{int(row['id'])}",
                    "kind": "support" if category == "support" else "ticket",
                    "title": "Новый вопрос в поддержку" if category == "support" else f"Новая заявка №{int(row['id'])}",
                    "detail": str(row["title"] or "Заявка"),
                    "source": " · ".join(part for part in [str(row["sender"] or ""), str(row["phone"] or "")] if part),
                    "href": f"/ticket?id={int(row['id'])}",
                    "timestamp": epoch_from_iso(str(row["created_at"] or "")),
                })
            contacts = connection.execute(
                """SELECT c.chat_id, m.name, w.body AS last_message, w.message_timestamp AS last_timestamp, m.phone, w.message_key
                   FROM whatsapp_chats AS c
                   INNER JOIN whatsapp_contacts AS m ON m.chat_id = c.chat_id
                   JOIN whatsapp_chat_messages w ON w.id=(SELECT id FROM whatsapp_chat_messages WHERE chat_id=c.chat_id AND from_me=0 AND deleted=0 ORDER BY message_timestamp DESC,id DESC LIMIT 1)
                   WHERE c.unread_count > 0
                     AND c.chat_id NOT LIKE '%@g.us'
                     AND c.chat_id NOT LIKE '%@broadcast'
                     AND c.chat_id NOT LIKE '%@newsletter'
                   ORDER BY c.last_timestamp DESC LIMIT 6"""
            ).fetchall()
            for row in contacts:
                chat_id = str(row["chat_id"] or "")
                fallback_phone = chat_id.split("@")[0] if "@" in chat_id else chat_id
                source_name = str(row["name"] or "").strip()
                if source_name.casefold() in {"система", "system", "рабочий whatsapp"}:
                    source_name = ""
                source_phone = str(row["phone"] or "").strip() or (f"+{fallback_phone}" if fallback_phone.isdigit() else "")
                events.append({
                    "id": f"contact:{chat_id}:{row['message_key']}",
                    "chat_id": chat_id, "message_key": row["message_key"],
                    "kind": "contact",
                    "title": "Новое сообщение WhatsApp",
                    "detail": str(row["last_message"] or "Новое сообщение"),
                    "source": " · ".join(part for part in [source_name, source_phone] if part) or "WhatsApp",
                    "href": f"/whatsapp?chat_id={chat_id}",
                    "timestamp": int(row["last_timestamp"] or 0),
                })
            groups = connection.execute(
                """SELECT c.chat_id, g.name, w.body AS last_message, w.message_timestamp AS last_timestamp, w.message_key
                   FROM whatsapp_chats AS c
                   INNER JOIN whatsapp_groups AS g ON g.chat_id = c.chat_id
                   JOIN whatsapp_chat_messages w ON w.id=(
                       SELECT id FROM whatsapp_chat_messages
                       WHERE chat_id=c.chat_id AND from_me=0 AND deleted=0 AND notify=1
                       ORDER BY message_timestamp DESC,id DESC LIMIT 1
                   )
                   WHERE g.added_by_admin = 1
                     AND COALESCE(g.muted, 0) = 0
                     AND c.mention_unread_count > 0
                   ORDER BY w.message_timestamp DESC LIMIT 6"""
            ).fetchall()
            for row in groups:
                events.append({
                    "id": f"group:{row['chat_id']}:{row['message_key']}",
                    "chat_id": row["chat_id"], "message_key": row["message_key"],
                    "kind": "group",
                    "title": "Упоминание в группе",
                    "detail": str(row["last_message"] or "Вас упомянули"),
                    "source": str(row["name"] or "Группа WhatsApp"),
                    "href": f"/groups?chat_id={row['chat_id']}",
                    "timestamp": int(row["last_timestamp"] or 0),
                })
        events.sort(key=lambda item: int(item.get("timestamp", 0) or 0), reverse=True)
        return events[:max(1, min(limit, 20))]

    def delete_ticket(self, ticket_id: int, actor: str = "Администратор") -> bool:
        if ticket_id <= 0:
            return False
        with self.connection() as connection:
            row = connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not row:
                return False
            # Это только внутреннее удаление. Никакое исходящее сообщение не
            # создаётся. Неотправленные ответы этой заявки удаляем из очереди.
            connection.execute("DELETE FROM outbound_messages WHERE ticket_id = ?", (ticket_id,))
            connection.execute("DELETE FROM events WHERE ticket_id = ?", (ticket_id,))
            connection.execute("UPDATE whatsapp_chat_messages SET ticket_id = 0 WHERE ticket_id = ?", (ticket_id,))
            connection.execute(
                "UPDATE conversation_contexts SET active_ticket_id = 0, force_new = 0, draft_json = '{}' WHERE active_ticket_id = ?",
                (ticket_id,),
            )
            connection.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            self._audit(connection, "ticket", normalize_message(actor)[:100] or "Администратор", "Заявка удалена", "ticket", ticket_id, level="warning")
        return True

    def dashboard_version(self) -> str:
        with self.connection() as connection:
            tickets = connection.execute(
                "SELECT COUNT(*) AS amount, COALESCE(MAX(id), 0) AS last_id FROM tickets"
            ).fetchone()
            events = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS last_id FROM events"
            ).fetchone()
        return f"{tickets['amount']}:{tickets['last_id']}:{events['last_id']}"

    def shift_summary(self, employee: str) -> dict[str, Any]:
        employee = normalize_message(employee)[:100]
        with self.connection() as connection:
            row = connection.execute(
                """SELECT
                    SUM(CASE WHEN assigned_to=? AND status IN ('new','in_progress') THEN 1 ELSE 0 END) AS mine,
                    SUM(CASE WHEN shift_handoff=1 AND status IN ('new','in_progress') THEN 1 ELSE 0 END) AS handoff,
                    SUM(CASE WHEN status IN ('new','in_progress') THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN priority='urgent' AND status IN ('new','in_progress') THEN 1 ELSE 0 END) AS urgent
                    FROM tickets""",
                (employee,),
            ).fetchone()
        return {
            "mine": int(row["mine"] or 0),
            "handoff": int(row["handoff"] or 0),
            "open": int(row["open_count"] or 0),
            "urgent": int(row["urgent"] or 0),
            "previous_employee": self.get_setting("previous_shift_employee", ""),
        }

    def switch_shift(self, previous_employee: str, new_employee: str) -> int:
        previous_employee = normalize_message(previous_employee)[:100]
        new_employee = normalize_message(new_employee)[:100]
        if not previous_employee or not new_employee or previous_employee == new_employee:
            return 0
        now = utc_now()
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM tickets WHERE assigned_to=? AND status IN ('new','in_progress') AND shift_handoff=0",
                (previous_employee,),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE tickets SET shift_handoff=1, handoff_from=?, handoff_at=?, updated_at=? WHERE id IN ({placeholders})",
                    [previous_employee, now, now, *ids],
                )
                connection.executemany(
                    "INSERT INTO events(ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    [(ticket_id, "Автоматически передано следующей смене", previous_employee, now) for ticket_id in ids],
                )
            self._audit(
                connection, "shift", new_employee, "Смена сотрудника", "shift", new_employee,
                f"Предыдущая смена: {previous_employee}; автоматически передано заявок: {len(ids)}", created_at=now,
            )
        self.set_setting("previous_shift_employee", previous_employee)
        return len(ids)

    def set_ticket_handoff(self, ticket_id: int, enabled: bool, actor: str) -> bool:
        now = utc_now()
        with self.connection() as connection:
            ticket = connection.execute("SELECT id, status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if not ticket or str(ticket["status"]) in {"done", "invalid"}:
                return False
            if enabled:
                cursor = connection.execute(
                    "UPDATE tickets SET shift_handoff = 1, handoff_from = ?, handoff_at = ?, updated_at = ? WHERE id = ?",
                    (actor, now, now, ticket_id),
                )
                action = "Передано следующей смене"
            else:
                cursor = connection.execute(
                    "UPDATE tickets SET shift_handoff = 0, handoff_from = '', handoff_at = '', assigned_to = ?, updated_at = ? WHERE id = ?",
                    (actor, now, ticket_id),
                )
                action = "Принято со следующей смены"
            if cursor.rowcount:
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (ticket_id, action, actor, now),
                )
                self._audit(connection, "shift", actor, action, "ticket", ticket_id, created_at=now)
                return True
        return False

    def find_similar_open_ticket(self, payload: dict[str, Any], hours: int = 72) -> dict[str, Any] | None:
        """Find a duplicate ticket from the same requester.

        Two protections are used:
        1. an exact repeat of the same data is blocked for 30 minutes even if the
           previous ticket was already closed, which stops button/message spam;
        2. an open ticket with the same or very similar data is treated as the
           same request for up to seven days.
        Different identifiers/data are not blocked merely because the same user
        created several tickets.
        """
        chat_id = normalize_message(str(payload.get("chat_id", "")))[:120]
        phone = re.sub(r"\D", "", str(payload.get("phone", "")))
        category = str(payload.get("category", ""))
        has_identity = bool(chat_id or phone)
        now_dt = datetime.now(timezone.utc)
        active_cutoff = (now_dt - timedelta(hours=max(168, max(1, hours)))).replace(microsecond=0).isoformat()
        exact_cutoff = (now_dt - timedelta(minutes=30)).replace(microsecond=0).isoformat()

        identity_sql: list[str] = []
        identity_params: list[Any] = []
        if chat_id:
            identity_sql.append("chat_id = ?")
            identity_params.append(chat_id)
        if phone:
            identity_sql.append("REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(phone, '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?")
            identity_params.append(f"%{phone[-10:]}")

        def fingerprint(value: str) -> str:
            value = normalize_message(value).casefold()
            value = re.sub(r"[^a-zа-яё0-9]+", " ", value)
            stop = {"заявка", "запрос", "проблема", "пожалуйста", "нужно", "надо", "номер"}
            tokens = [token for token in value.split() if token not in stop]
            return " ".join(tokens)

        def ticket_text(row: dict[str, Any]) -> str:
            return fingerprint(" ".join([
                str(row.get("summary", "")), str(row.get("original_text", "")),
            ]))

        def structured_values(row: dict[str, Any]) -> tuple[str, ...]:
            keys = ("bin_old", "bin_new", "ats_number", "company", "country")
            return tuple(fingerprint(str(row.get(key, ""))) for key in keys)

        new_text = ticket_text(payload)
        new_structured = structured_values(payload)
        new_numbers = set(re.findall(r"\d{4,}", new_text))

        # Exact recent repeats: include closed tickets too. This is deliberately a
        # short window so a genuinely recurring problem can be submitted later.
        recent_conditions = ["created_at >= ?"]
        recent_params: list[Any] = [exact_cutoff]
        if identity_sql:
            recent_conditions.append("(" + " OR ".join(identity_sql) + ")")
            recent_params.extend(identity_params)
        elif category:
            recent_conditions.append("category = ?")
            recent_params.append(category)
        with self.connection() as connection:
            recent_rows = connection.execute(
                f"SELECT * FROM tickets WHERE {' AND '.join(recent_conditions)} ORDER BY id DESC LIMIT 50",
                recent_params,
            ).fetchall()
        for row in recent_rows:
            existing = dict(row)
            if str(existing.get("status", "")) == "invalid":
                continue
            old_text = ticket_text(existing)
            old_structured = structured_values(existing)
            # Same normalized text, or exactly the same populated structured data.
            if new_text and old_text and new_text == old_text:
                return existing
            populated_pairs = [(a, b) for a, b in zip(new_structured, old_structured) if a and b]
            if populated_pairs and len(populated_pairs) >= 2 and all(a == b for a, b in populated_pairs):
                return existing
            # Same set of meaningful identifiers is an exact-data repeat even when
            # the user changes a few words around it.
            old_numbers = set(re.findall(r"\d{4,}", old_text))
            if new_numbers and old_numbers and new_numbers == old_numbers and len(new_numbers) >= 2:
                return existing

        # Open duplicate protection: same requester + similar data for up to 7 days.
        conditions = ["status IN ('new','in_progress')", "created_at >= ?"]
        params: list[Any] = [active_cutoff]
        if identity_sql:
            conditions.append("(" + " OR ".join(identity_sql) + ")")
            params.extend(identity_params)
        elif category:
            conditions.append("category = ?")
            params.append(category)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM tickets WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT 60",
                params,
            ).fetchall()

        new_tokens = set(new_text.split())
        for row in rows:
            existing = dict(row)
            if category == "bin":
                keys = ("bin_old", "bin_new", "ats_number")
                if all(str(payload.get(key, "")).strip() and str(payload.get(key, "")).strip().casefold() == str(existing.get(key, "")).strip().casefold() for key in keys):
                    return existing
            old_text = ticket_text(existing)
            if not new_text or not old_text:
                continue
            if new_text == old_text:
                return existing
            ratio = difflib.SequenceMatcher(None, new_text, old_text).ratio()
            old_tokens = set(old_text.split())
            union = new_tokens | old_tokens
            jaccard = len(new_tokens & old_tokens) / len(union) if union else 0.0
            old_numbers = set(re.findall(r"\d{4,}", old_text))
            common_numbers = new_numbers & old_numbers
            identifier_match = bool(common_numbers)
            # Two or more identical long identifiers from the same requester are a
            # strong duplicate signal even if the wording around them is different.
            strong_identifier_match = len(common_numbers) >= 2
            if has_identity:
                matched = (
                    ratio >= 0.78 or jaccard >= 0.64 or
                    (identifier_match and jaccard >= 0.38) or strong_identifier_match
                )
            else:
                matched = (
                    (identifier_match and jaccard >= 0.55) or
                    (ratio >= 0.92 and jaccard >= 0.78)
                )
            if matched:
                return existing
        return None

    def enable_manual_chat_mode(self, chat_id: str, actor: str, minutes: int = 30) -> bool:
        """Выключить автоответчик явным действием сотрудника.

        Параметр ``minutes`` оставлен только для совместимости со старым кодом.
        Начиная с 3.3.25 состояние не истекает по таймеру: автоответчик остаётся
        выключенным, пока сотрудник сам не включит его обратно.
        """
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id or chat_id.endswith("@g.us"):
            return False
        now = datetime.now(timezone.utc).replace(microsecond=0)
        # Существующая таблица требует expires_at. Используем далёкую дату как
        # постоянное состояние, чтобы не менять схему БД при обновлении.
        expires = "9999-12-31T23:59:59+00:00"
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO whatsapp_manual_chat_modes(chat_id, expires_at, actor, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET expires_at = excluded.expires_at, actor = excluded.actor, updated_at = excluded.updated_at""",
                (chat_id, expires, actor, now.isoformat()),
            )
            self._audit(connection, "auto_reply", actor, "Автоответчик выключен сотрудником", "chat", chat_id, created_at=now.isoformat())
        return True

    def disable_manual_chat_mode(self, chat_id: str, actor: str = "") -> bool:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id:
            return False
        with self.connection() as connection:
            cursor = connection.execute("DELETE FROM whatsapp_manual_chat_modes WHERE chat_id = ?", (chat_id,))
            if cursor.rowcount:
                self._audit(connection, "auto_reply", actor or "Сотрудник", "Автоответчик включен сотрудником", "chat", chat_id)
            return cursor.rowcount > 0

    def manual_chat_mode(self, chat_id: str) -> dict[str, Any] | None:
        chat_id = normalize_message(chat_id)[:120]
        if not chat_id:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT chat_id, expires_at, actor FROM whatsapp_manual_chat_modes WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def allow_auto_reply(self, contact_key: str, reply_kind: str, seconds: int = 45) -> bool:
        contact_key = normalize_message(contact_key)[:120]
        reply_kind = normalize_message(reply_kind)[:60]
        if not contact_key or not reply_kind:
            return True
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        cutoff = (now_dt - timedelta(seconds=max(1, seconds))).isoformat()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT last_sent_at FROM auto_reply_cooldowns WHERE contact_key = ? AND reply_kind = ?",
                (contact_key, reply_kind),
            ).fetchone()
            if row and str(row["last_sent_at"]) > cutoff:
                return False
            connection.execute(
                """INSERT INTO auto_reply_cooldowns(contact_key, reply_kind, last_sent_at) VALUES (?, ?, ?)
                   ON CONFLICT(contact_key, reply_kind) DO UPDATE SET last_sent_at = excluded.last_sent_at""",
                (contact_key, reply_kind, now),
            )
        return True

    def update_status(
        self,
        ticket_id: int,
        status: str,
        actor: str,
        assigned_to: str | None = None,
    ) -> bool:
        if status not in STATUSES:
            return False
        now = utc_now()
        closed_at = now if status in {"done", "invalid"} else ""
        assignee = assigned_to if assigned_to is not None else actor
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT status, assigned_to, created_at, first_response_at FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE tickets
                SET status = ?, assigned_to = ?, updated_at = ?, closed_at = ?,
                    first_response_at = CASE WHEN first_response_at='' AND ? <> 'new' THEN ? ELSE first_response_at END,
                    shift_handoff = CASE WHEN ? IN ('in_progress','done','invalid') THEN 0 ELSE shift_handoff END,
                    handoff_from = CASE WHEN ? IN ('in_progress','done','invalid') THEN '' ELSE handoff_from END,
                    handoff_at = CASE WHEN ? IN ('in_progress','done','invalid') THEN '' ELSE handoff_at END
                WHERE id = ?
                """,
                (status, assignee, now, closed_at, status, now, status, status, status, ticket_id),
            )
            if cursor.rowcount:
                action = f"Статус изменён: {STATUSES[status]}"
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (ticket_id, action, actor, now),
                )
                before = str(previous["status"] if previous else "")
                details = f"{STATUSES.get(before, before) or '—'} → {STATUSES[status]}; исполнитель: {assignee}"
                self._audit(connection, "status", actor, action, "ticket", ticket_id, details, created_at=now)
                return True
        return False

    def update_priority(self, ticket_id: int, priority: str, actor: str) -> bool:
        if priority not in PRIORITIES:
            return False
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE tickets SET priority = ?, updated_at = ? WHERE id = ?",
                (priority, now, ticket_id),
            )
            if cursor.rowcount:
                action = f"Приоритет изменён: {PRIORITIES[priority]}"
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (ticket_id, action, actor, now),
                )
                self._audit(connection, "priority", actor, action, "ticket", ticket_id, created_at=now)
                return True
        return False

    def update_classification(
        self,
        ticket_id: int,
        category: str,
        priority: str,
        actor: str,
    ) -> bool:
        if category not in CATEGORIES or priority not in PRIORITIES:
            return False
        now = utc_now()
        with self.connection() as connection:
            current = connection.execute(
                "SELECT category, priority FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if not current:
                return False
            connection.execute(
                "UPDATE tickets SET category = ?, priority = ?, updated_at = ? WHERE id = ?",
                (category, priority, now, ticket_id),
            )
            changes: list[str] = []
            if current["category"] != category:
                changes.append(f"категория: {CATEGORIES[category]}")
            if current["priority"] != priority:
                changes.append(f"приоритет: {PRIORITIES[priority]}")
            if changes:
                action = "Изменена классификация: " + ", ".join(changes)
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (ticket_id, action, actor, now),
                )
                self._audit(connection, "classification", actor, action, "ticket", ticket_id, created_at=now)
            return True

    def ticket_events(self, ticket_id: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY id DESC",
                (ticket_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_conversation_context(self, contact_key: str) -> dict[str, Any] | None:
        if not contact_key:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_contexts WHERE contact_key = ?",
                (contact_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_conversation_draft(self, contact_key: str) -> dict[str, str]:
        context = self.get_conversation_context(contact_key)
        if not context:
            return {}
        try:
            raw = json.loads(str(context.get("draft_json", "{}") or "{}"))
        except (ValueError, TypeError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): normalize_message(str(value)) for key, value in raw.items() if str(value).strip()}

    def set_conversation_draft(self, contact_key: str, draft: dict[str, str]) -> None:
        if not contact_key:
            return
        clean = {str(key): normalize_message(str(value))[:1800] for key, value in draft.items() if str(value).strip()}
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_contexts WHERE contact_key = ?", (contact_key,)
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE conversation_contexts SET draft_json = ?, updated_at = ? WHERE contact_key = ?",
                    (json.dumps(clean, ensure_ascii=False), now, contact_key),
                )
            else:
                connection.execute(
                    "INSERT INTO conversation_contexts(contact_key, pending_category, active_ticket_id, force_new, draft_json, updated_at) VALUES (?, '', 0, 0, ?, ?)",
                    (contact_key, json.dumps(clean, ensure_ascii=False), now),
                )

    def clear_conversation_draft(self, contact_key: str) -> None:
        if not contact_key:
            return
        with self.connection() as connection:
            connection.execute(
                "UPDATE conversation_contexts SET draft_json = '{}', updated_at = ? WHERE contact_key = ?",
                (utc_now(), contact_key),
            )

    def set_conversation_context(
        self,
        contact_key: str,
        pending_category: str = "",
        active_ticket_id: int = 0,
        force_new: bool = False,
    ) -> None:
        if not contact_key:
            return
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO conversation_contexts
                    (contact_key, pending_category, active_ticket_id, force_new, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(contact_key) DO UPDATE SET
                    pending_category = excluded.pending_category,
                    active_ticket_id = excluded.active_ticket_id,
                    force_new = excluded.force_new,
                    updated_at = excluded.updated_at
                """,
                (
                    contact_key,
                    pending_category,
                    active_ticket_id,
                    int(force_new),
                    utc_now(),
                ),
            )

    def list_active_user_tickets(
        self, chat_id: str = "", phone: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return only this WhatsApp user's currently active tickets.

        Matching prefers the canonical chat id and also accepts the same normalized
        phone number, so an @lid/@c.us alias does not hide or duplicate a ticket.
        """
        safe_limit = min(max(1, int(limit or 10)), 30)
        phone_digits = re.sub(r"\D", "", normalize_message(phone))[:24]
        if len(phone_digits) == 11 and phone_digits.startswith("8"):
            phone_digits = "7" + phone_digits[1:]
        elif len(phone_digits) == 10:
            phone_digits = "7" + phone_digits
        with self.connection() as connection:
            canonical = self._canonical_whatsapp_chat_id(connection, chat_id) if chat_id else ""
            rows = connection.execute(
                """
                SELECT * FROM tickets
                 WHERE source='whatsapp' AND status IN ('new','in_progress')
                 ORDER BY updated_at DESC, id DESC
                 LIMIT 200
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            seen: set[int] = set()
            for row in rows:
                data = dict(row)
                ticket_id = int(data.get("id", 0) or 0)
                if not ticket_id or ticket_id in seen:
                    continue
                row_chat = normalize_message(str(data.get("chat_id", "")))[:120]
                row_canonical = self._canonical_whatsapp_chat_id(connection, row_chat) if row_chat else ""
                row_digits = re.sub(r"\D", "", normalize_message(str(data.get("phone", ""))))[:24]
                if len(row_digits) == 11 and row_digits.startswith("8"):
                    row_digits = "7" + row_digits[1:]
                elif len(row_digits) == 10:
                    row_digits = "7" + row_digits
                same_chat = bool(canonical and row_canonical and canonical == row_canonical)
                same_phone = bool(phone_digits and row_digits and phone_digits == row_digits)
                if not (same_chat or same_phone):
                    continue
                seen.add(ticket_id)
                result.append(data)
                if len(result) >= safe_limit:
                    break
        return result

    def active_context_ticket(self, contact_key: str) -> dict[str, Any] | None:
        context = self.get_conversation_context(contact_key)
        if not context or not context["active_ticket_id"]:
            return None
        ticket = self.get_ticket(int(context["active_ticket_id"]))
        if not ticket or ticket["status"] not in {"new", "in_progress"}:
            return None
        return ticket

    def note_context_followup(
        self, ticket_id: int, body: str = "", attachment_name: str = ""
    ) -> bool:
        now = utc_now()
        clean_body = normalize_message(body)[:700]
        clean_attachment = normalize_message(attachment_name)[:240]
        details: list[str] = []
        if clean_body:
            details.append(clean_body)
        if clean_attachment:
            details.append(f"Вложение: {clean_attachment}")
        action = "Получено уточнение от пользователя"
        if details:
            action += ": " + " | ".join(details)
        action = action[:1000]
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE tickets SET updated_at = ? WHERE id = ? AND status IN ('new','in_progress')",
                (now, ticket_id),
            )
            if not cursor.rowcount:
                return False
            connection.execute(
                "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, action, "WhatsApp", now),
            )
            return True

    def queue_outbound_message(self, ticket_id: int, body: str, actor: str) -> int:
        body = normalize_message(body)[:32000]
        if not body:
            return 0
        now = utc_now()
        with self.connection() as connection:
            ticket = connection.execute(
                "SELECT source, chat_id, phone FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if not ticket or ticket["source"] != "whatsapp":
                return 0
            if not ticket["chat_id"] and not ticket["phone"]:
                return 0
            cursor = connection.execute(
                """
                INSERT INTO outbound_messages
                    (ticket_id, chat_id, phone, body, actor, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    ticket["chat_id"],
                    ticket["phone"],
                    body,
                    actor,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, "Ответ поставлен в очередь отправки", actor, now),
            )
            message_id = int(cursor.lastrowid)
            self._audit(connection, "message", actor, "Сообщение поставлено в очередь", "ticket", ticket_id, f"Очередь #{message_id}", created_at=now)
            return message_id

    def queue_direct_message(
        self, chat_id: str, body: str, actor: str, mentions: list[str] | None = None,
        reply_to_key: str = "", media_path: str = "", media_mime: str = "", media_name: str = "",
        request_id: str = "",
    ) -> int:
        body = normalize_message(body)[:32000]
        chat_id = normalize_message(chat_id)[:120]
        reply_to_key = normalize_message(reply_to_key)[:160]
        media_path = str(media_path or "")[:500]
        media_mime = normalize_message(media_mime)[:120]
        media_name = normalize_message(media_name)[:180]
        if not chat_id or (not body and not media_path):
            return 0
        safe_mentions = []
        for value in mentions or []:
            mention = normalize_message(str(value))[:120]
            if mention and mention not in safe_mentions:
                safe_mentions.append(mention)
        request_id = str(request_id or "")[:120]
        now = utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if request_id:
                existing = connection.execute("SELECT * FROM outbound_requests WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    return int(existing["message_id"]) if existing["chat_id"] == chat_id and existing["actor"] == actor else 0
            cursor = connection.execute(
                """
                INSERT INTO outbound_messages
                    (ticket_id, chat_id, phone, body, actor, created_at, updated_at, mentions_json,
                     reply_to_key, media_path, media_mime, media_name)
                VALUES (0, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, body, actor, now, now, json.dumps(safe_mentions, ensure_ascii=False),
                 reply_to_key, media_path, media_mime, media_name),
            )
            message_id = int(cursor.lastrowid)
            if request_id:
                connection.execute("INSERT INTO outbound_requests VALUES (?, ?, ?, ?, ?)", (request_id, chat_id, actor, message_id, now))
            detail = (body[:180] if body else media_name or "Вложение")
            self._audit(connection, "message", actor, "Сообщение поставлено в очередь", "chat", chat_id, f"#{message_id}: {detail}", created_at=now)
            return message_id

    def queue_whatsapp_action(
        self, action_type: str, chat_id: str, message_key: str, body: str = "", actor: str = ""
    ) -> int:
        if action_type not in {"edit", "delete", "forward", "react"}:
            return 0
        chat_id = normalize_message(chat_id)[:120]
        message_key = normalize_message(message_key)[:160]
        body = normalize_message(body)[:32000]
        actor = normalize_message(actor)[:100] or "Сотрудник"
        if not chat_id or not message_key or (action_type in {"edit", "forward"} and not body):
            return 0
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO whatsapp_actions(action_type, chat_id, message_key, body, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (action_type, chat_id, message_key, body, actor, now),
            )
            action_id = int(cursor.lastrowid)
            label = {"edit": "Редактирование сообщения поставлено в очередь", "delete": "Удаление сообщения поставлено в очередь", "forward": "Пересылка сообщения поставлена в очередь", "react": "Реакция на сообщение поставлена в очередь"}[action_type]
            self._audit(connection, "message_action", actor, label, "chat", chat_id, f"message={message_key}; action=#{action_id}", created_at=now)
            return action_id

    def claim_whatsapp_action(self) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        stale_cutoff = (now_dt - timedelta(seconds=60)).isoformat()
        cleanup_cutoff = (now_dt - timedelta(days=1)).isoformat()
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM whatsapp_actions WHERE status IN ('sent','failed') AND created_at < ?",
                (cleanup_cutoff,),
            )
            connection.execute(
                "UPDATE whatsapp_actions SET status='failed', claimed_at='', error='Нет подтверждения действия. Проверьте переписку перед повтором.' WHERE status='processing' AND claimed_at<>'' AND claimed_at < ?",
                (stale_cutoff,),
            )
            row = connection.execute(
                "SELECT * FROM whatsapp_actions WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                "UPDATE whatsapp_actions SET status = 'processing', claimed_at = ? WHERE id = ? AND status = 'pending'",
                (now, row["id"]),
            )
            if not cursor.rowcount:
                return None
        return dict(row)

    def whatsapp_action_status(self, action_id: int) -> dict[str, Any] | None:
        if action_id <= 0:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, action_type, chat_id, message_key, status, error, created_at, claimed_at FROM whatsapp_actions WHERE id = ?",
                (action_id,),
            ).fetchone()
        return dict(row) if row else None

    def complete_whatsapp_action(self, action_id: int, sent: bool, error: str = "") -> dict[str, Any] | None:
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM whatsapp_actions WHERE id = ?", (action_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            if data["status"] == "sent":
                return data
            safe_error = normalize_message(error)[:500]
            connection.execute(
                "UPDATE whatsapp_actions SET status=?, error=?, claimed_at='' WHERE id=?",
                ("sent" if sent else "failed", "" if sent else safe_error, action_id),
            )
            action_type = str(data.get("action_type", ""))
            actor = str(data.get("actor", "") or "Сотрудник")
            if sent:
                if action_type == "edit":
                    connection.execute(
                        "UPDATE whatsapp_chat_messages SET body = ?, edited = 1, edit_timestamp=? WHERE chat_id = ? AND message_key = ?",
                        (data["body"], int(datetime.now(timezone.utc).timestamp()*1000), data["chat_id"], data["message_key"]),
                    )
                elif action_type == "delete":
                    target = connection.execute(
                        """SELECT body, media_name, message_type, message_timestamp
                             FROM whatsapp_chat_messages
                            WHERE chat_id = ? AND message_key = ?""",
                        (data["chat_id"], data["message_key"]),
                    ).fetchone()
                    connection.execute(
                        "UPDATE whatsapp_chat_messages SET body = '', media_path = '', media_mime = '', media_name = '', deleted = 1 WHERE chat_id = ? AND message_key = ?",
                        (data["chat_id"], data["message_key"]),
                    )
                elif action_type == "react":
                    reaction_row = connection.execute(
                        "SELECT reactions_json FROM whatsapp_chat_messages WHERE chat_id = ? AND message_key = ?",
                        (data["chat_id"], data["message_key"]),
                    ).fetchone()
                    if reaction_row:
                        try:
                            reaction_values = json.loads(str(reaction_row["reactions_json"] or "{}"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            reaction_values = {}
                        if not isinstance(reaction_values, dict):
                            reaction_values = {}
                        reaction_emoji = str(data.get("body", "") or "")[:32]
                        if reaction_emoji:
                            reaction_values["__me__"] = reaction_emoji
                        else:
                            reaction_values.pop("__me__", None)
                        connection.execute(
                            "UPDATE whatsapp_chat_messages SET reactions_json = ? WHERE chat_id = ? AND message_key = ?",
                            (json.dumps(reaction_values, ensure_ascii=False, separators=(",", ":")), data["chat_id"], data["message_key"]),
                        )
                latest = connection.execute(
                    """SELECT body, media_name, message_timestamp
                       FROM whatsapp_chat_messages
                       WHERE chat_id = ? AND deleted = 0
                       ORDER BY message_timestamp DESC, id DESC LIMIT 1""",
                    (data["chat_id"],),
                ).fetchone()
                if latest:
                    preview = latest["body"] or latest["media_name"] or "[Вложение]"
                    preview_timestamp = int(latest["message_timestamp"] or 0)
                else:
                    preview, preview_timestamp = "", 0
                connection.execute(
                    "UPDATE whatsapp_chats SET last_message = ?, last_timestamp = ?, updated_at = ? WHERE chat_id = ?",
                    (str(preview)[:160], preview_timestamp, now, data["chat_id"]),
                )
                label = {"edit": "Сообщение изменено в WhatsApp", "delete": "Сообщение удалено в WhatsApp", "forward": "Сообщение переслано в WhatsApp", "react": "Реакция отправлена в WhatsApp"}.get(action_type, "Действие с сообщением выполнено")
                self._audit(connection, "message_action", actor, label, "chat", data["chat_id"], f"message={data['message_key']}", created_at=now)
            else:
                safe_error = safe_error or "WhatsApp не выполнил действие"
                connection.execute("UPDATE whatsapp_actions SET error=? WHERE id=?", (safe_error, action_id))
                label = {"edit": "Не удалось изменить сообщение", "delete": "Не удалось удалить сообщение", "forward": "Не удалось переслать сообщение", "react": "Не удалось отправить реакцию"}.get(action_type, "Ошибка действия с сообщением")
                self._audit(connection, "message_action", actor, label, "chat", data["chat_id"], safe_error, level="error", created_at=now)
                data["error"] = safe_error
            data["status"] = "sent" if sent else "failed"
            if sent:
                data["error"] = ""
            return data

    def claim_outbound_message(self, created_after: str = "") -> dict[str, Any] | None:
        now = utc_now()
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).replace(microsecond=0).isoformat()
        with self.connection() as connection:
            connection.execute(
                "UPDATE outbound_messages SET status='uncertain', updated_at=?, "
                "last_error='Связь прервалась во время отправки. Проверьте переписку; автоповтор остановлен.' "
                "WHERE status='sending' AND claimed_at<>'' AND claimed_at < ?",
                (now, stale_cutoff),
            )
            # Если коннектор взял сообщение и умер до результата, запись не теряется.
            connection.execute(
                """
                UPDATE outbound_messages
                SET status='pending', claimed_at='', next_attempt_at='',
                    last_error=CASE WHEN last_error='' THEN 'Повтор после зависшей отправки' ELSE last_error END,
                    updated_at=?
                WHERE status='processing' AND claimed_at <> '' AND claimed_at < ?
                """,
                (now, stale_cutoff),
            )
            # Очередь постоянная: после перезапуска коннектора старые pending
            # сообщения не теряются и продолжают отправляться.
            row = connection.execute(
                """
                SELECT * FROM outbound_messages
                WHERE status='pending'
                  AND attempt_count < max_attempts
                  AND (next_attempt_at='' OR next_attempt_at <= ?)
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                """
                UPDATE outbound_messages
                SET status='processing', claimed_at=?, attempt_count=attempt_count+1,
                    updated_at=?
                WHERE id=? AND status='pending'
                """,
                (now, now, row["id"]),
            )
            if not cursor.rowcount:
                return None
            row = connection.execute(
                "SELECT * FROM outbound_messages WHERE id=?", (row["id"],)
            ).fetchone()
        return dict(row) if row else None

    def touch_outbound_message(self, message_id: int) -> bool:
        with self.connection() as connection:
            return bool(connection.execute(
                "UPDATE outbound_messages SET claimed_at=? WHERE id=? AND status IN ('processing','sending')",
                (utc_now(), message_id),
            ).rowcount)

    def begin_outbound_send(self, message_id: int) -> bool:
        with self.connection() as connection:
            return bool(connection.execute(
                "UPDATE outbound_messages SET status='sending', claimed_at=?, updated_at=?, dispatch_started_at=? "
                "WHERE id=? AND status='processing'",
                (utc_now(), utc_now(), utc_now(), message_id),
            ).rowcount)

    def mark_outbound_uncertain(self, message_id: int, error: str = '') -> dict[str, Any] | None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE outbound_messages SET status='uncertain', last_error=?, updated_at=?, "
                "claimed_at='', next_attempt_at='' WHERE id=? AND status IN ('processing','sending','uncertain')",
                (normalize_message(error)[:500] or 'Нет подтверждения WhatsApp. Проверьте переписку.', utc_now(), message_id),
            )
            row = connection.execute('SELECT * FROM outbound_messages WHERE id=?', (message_id,)).fetchone()
            return dict(row) if row else None

    def complete_outbound_message(
        self,
        message_id: int,
        sent: bool,
        error: str = "",
        provider_id: str = "",
    ) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM outbound_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if not row:
                return None
            if row["status"] == "sent":
                return dict(row)
            if not sent and row["status"] in {"sending", "uncertain"}:
                connection.execute(
                    "UPDATE outbound_messages SET status='uncertain', claimed_at='', next_attempt_at='', "
                    "last_error=?, updated_at=? WHERE id=?",
                    ('Отправка могла состояться. Автоповтор остановлен. ' + normalize_message(error)[:350], now, message_id),
                )
                return dict(connection.execute('SELECT * FROM outbound_messages WHERE id=?', (message_id,)).fetchone())
            media_path = str(row["media_path"] or "")
            attempts = int(row["attempt_count"] or 0)
            max_attempts = max(1, int(row["max_attempts"] or 4))
            if sent:
                connection.execute(
                    """
                    UPDATE outbound_messages
                    SET status='sent', claimed_at='', next_attempt_at='', last_error='',
                        provider_id=?, sent_at=?, failed_at='', updated_at=?, media_path=''
                    WHERE id=?
                    """,
                    (normalize_message(provider_id)[:180], now, now, message_id),
                )
                # SLA считается до первой реальной реакции сотрудника. Успешно
                # доставленный ответ по заявке останавливает таймер даже если
                # сотрудник ещё не успел вручную изменить статус заявки.
                if int(row["ticket_id"] or 0) > 0:
                    connection.execute(
                        """
                        UPDATE tickets
                           SET first_response_at = CASE WHEN first_response_at='' THEN ? ELSE first_response_at END,
                               updated_at = ?
                         WHERE id = ?
                        """,
                        (now, now, int(row["ticket_id"])),
                    )
                if media_path:
                    try:
                        Path(media_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                action = "Ответ отправлен пользователю"
            else:
                safe_error = normalize_message(error)[:500]
                if attempts < max_attempts:
                    delays = (5, 15, 45, 120)
                    delay = delays[min(max(0, attempts - 1), len(delays) - 1)]
                    next_at = (now_dt + timedelta(seconds=delay)).isoformat()
                    connection.execute(
                        """
                        UPDATE outbound_messages
                        SET status='pending', claimed_at='', next_attempt_at=?,
                            last_error=?, updated_at=?
                        WHERE id=?
                        """,
                        (next_at, safe_error, now, message_id),
                    )
                    action = f"Ошибка отправки, повтор {attempts + 1}/{max_attempts} через {delay} сек."
                    if safe_error:
                        action += f" {safe_error[:160]}"
                else:
                    connection.execute(
                        """
                        UPDATE outbound_messages
                        SET status='failed', claimed_at='', next_attempt_at='',
                            last_error=?, failed_at=?, updated_at=?, media_path=''
                        WHERE id=?
                        """,
                        (safe_error, now, now, message_id),
                    )
                    if media_path:
                        try:
                            Path(media_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                    action = "Не удалось отправить ответ после повторных попыток"
                    if safe_error:
                        action += f": {safe_error[:160]}"
            level = "info" if sent else ("warning" if attempts < max_attempts else "error")
            self._audit(
                connection, "message", str(row["actor"] or "Система"), action,
                "ticket" if row["ticket_id"] else "chat", row["ticket_id"] or row["chat_id"],
                normalize_message(error)[:500], level=level, created_at=now,
            )
            if row["ticket_id"]:
                connection.execute(
                    "INSERT INTO events (ticket_id, action, actor, created_at) VALUES (?, ?, ?, ?)",
                    (row["ticket_id"], action, row["actor"], now),
                )
            current = connection.execute(
                "SELECT * FROM outbound_messages WHERE id=?", (message_id,)
            ).fetchone()
            return dict(current) if current else None

    def outbound_queue_snapshot(self, limit: int = 30) -> dict[str, Any]:
        with self.connection() as connection:
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM outbound_messages GROUP BY status"
                ).fetchall()
            }
            rows = connection.execute(
                """
                SELECT id, ticket_id, chat_id, phone, actor, status, created_at, updated_at,
                       attempt_count, max_attempts, next_attempt_at, last_error, provider_id,
                       sent_at, failed_at, CASE WHEN media_path<>'' THEN 1 ELSE 0 END AS has_media
                FROM outbound_messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return {"counts": counts, "rows": [dict(row) for row in rows]}

    def retry_failed_outbound(self) -> int:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbound_messages
                SET status='pending', attempt_count=0, claimed_at='', next_attempt_at='',
                    failed_at='', updated_at=?,
                    last_error=CASE WHEN last_error='' THEN 'Повтор запрошен администратором' ELSE last_error END
                WHERE status='failed'
                """,
                (now,),
            )
            return int(cursor.rowcount or 0)

    def database_health(self) -> dict[str, Any]:
        try:
            with self.connection() as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
                result = str(row[0] if row else "unknown")
                tables = int(connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0])
            return {"ok": result.lower() == "ok", "result": result, "tables": tables}
        except Exception as exc:
            return {"ok": False, "result": normalize_message(str(exc))[:300], "tables": 0}

    def update_whatsapp_contact_display_name(
        self, chat_id: str, name: str, phone: str = ""
    ) -> int:
        clean_chat_id = normalize_message(chat_id)[:120]
        clean_name = normalize_message(name)[:100].strip()
        if not clean_name or clean_name.casefold() in {"система", "system", "рабочий whatsapp"}:
            return 0
        digits_name = re.sub(r"\D", "", clean_name)
        if digits_name and len(digits_name) >= 8 and not re.search(r"[A-Za-zА-Яа-яЁё]", clean_name):
            return 0
        phone_digits = re.sub(r"\D", "", normalize_message(phone))[:24]
        candidates = {clean_chat_id} if clean_chat_id else set()
        if phone_digits:
            if len(phone_digits) == 11 and phone_digits.startswith("8"):
                phone_digits = "7" + phone_digits[1:]
            candidates.add(f"{phone_digits}@c.us")
        candidates.discard("")
        if not candidates:
            return 0
        now = utc_now()
        changed = 0
        with self.connection() as connection:
            for candidate in candidates:
                manual = connection.execute("SELECT name FROM whatsapp_contacts WHERE chat_id=?", (candidate,)).fetchone()
                if manual and manual["name"]: clean_name = manual["name"]
                cursor = connection.execute(
                    "UPDATE whatsapp_chats SET name=?, updated_at=? WHERE chat_id=?",
                    (clean_name, now, candidate),
                )
                changed += int(cursor.rowcount or 0)
                connection.execute(
                    "UPDATE tickets SET sender=?, updated_at=? WHERE source='whatsapp' AND chat_id=?",
                    (clean_name, now, candidate),
                )
        return changed

    def cleanup_old_data(self, retention_days: int = 20) -> dict[str, Any]:
        days = max(1, min(3650, int(retention_days)))
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff_dt.replace(microsecond=0).isoformat()
        cutoff_epoch = int(cutoff_dt.timestamp())
        media_paths: list[str] = []
        deleted_messages = 0
        deleted_tickets = 0
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT media_path FROM whatsapp_chat_messages WHERE message_timestamp > 0 AND message_timestamp < ? AND media_path <> '' AND ticket_id NOT IN (SELECT id FROM tickets WHERE status NOT IN ('done','invalid'))",
                (cutoff_epoch,),
            ).fetchall()
            media_paths = [str(row["media_path"] or "") for row in rows if row["media_path"]]
            cursor = connection.execute(
                "DELETE FROM whatsapp_chat_messages WHERE message_timestamp > 0 AND message_timestamp < ? AND ticket_id NOT IN (SELECT id FROM tickets WHERE status NOT IN ('done','invalid'))",
                (cutoff_epoch,),
            )
            deleted_messages = int(cursor.rowcount or 0)
            # Active work is never removed just because it is old. Only completed/invalid tickets
            # are eligible for the automatic 20-day retention policy.
            old_ticket_rows = connection.execute(
                "SELECT id FROM tickets WHERE status IN ('done','invalid') AND updated_at < ?",
                (cutoff_iso,),
            ).fetchall()
            ticket_ids = [int(row["id"]) for row in old_ticket_rows]
            for ticket_id in ticket_ids:
                connection.execute("DELETE FROM events WHERE ticket_id = ?", (ticket_id,))
                connection.execute("UPDATE whatsapp_chat_messages SET ticket_id = 0 WHERE ticket_id = ?", (ticket_id,))
                connection.execute("DELETE FROM outbound_messages WHERE ticket_id = ?", (ticket_id,))
                connection.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            deleted_tickets = len(ticket_ids)
            connection.execute("DELETE FROM inbound_messages WHERE received_at < ?", (cutoff_iso,))
            connection.execute("DELETE FROM template_errors WHERE created_at < ?", (cutoff_iso,))
            connection.execute("DELETE FROM whatsapp_actions WHERE created_at < ?", (cutoff_iso,))
            connection.execute("DELETE FROM outbound_messages WHERE created_at < ?", (cutoff_iso,))
            # Rebuild chat preview after message retention.
            chats = connection.execute("SELECT chat_id FROM whatsapp_chats").fetchall()
            for chat in chats:
                cid = str(chat["chat_id"] or "")
                latest = connection.execute(
                    "SELECT body, media_name, message_timestamp FROM whatsapp_chat_messages WHERE chat_id = ? AND deleted = 0 ORDER BY message_timestamp DESC, id DESC LIMIT 1",
                    (cid,),
                ).fetchone()
                if latest:
                    preview = latest["body"] or latest["media_name"] or "[Вложение]"
                    connection.execute(
                        "UPDATE whatsapp_chats SET last_message=?, last_timestamp=?, updated_at=? WHERE chat_id=?",
                        (str(preview)[:160], int(latest["message_timestamp"] or 0), utc_now(), cid),
                    )
                else:
                    connection.execute("UPDATE whatsapp_chats SET last_message='', last_timestamp=0, unread_count=0, updated_at=? WHERE chat_id=?", (utc_now(), cid))
            connection.execute("PRAGMA optimize")
        return {"media_paths": media_paths, "messages": deleted_messages, "tickets": deleted_tickets, "cutoff": cutoff_iso}

    def claim_inbound_message(self, external_id: str, sender: str) -> bool:
        if not external_id:
            return True
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO inbound_messages (external_id, sender, received_at) VALUES (?, ?, ?)",
                (external_id, sender, utc_now()),
            )
            return bool(cursor.rowcount)

    def add_template_error(
        self,
        sender: str,
        phone: str,
        missing_fields: list[str],
        reply_body: str,
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO template_errors
                    (sender, phone, missing_fields, reply_body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sender, phone, ", ".join(missing_fields), reply_body, utc_now()),
            )
            return int(cursor.lastrowid)

    def update_template_error_delivery(
        self,
        error_id: int,
        delivery_status: str,
        provider_id: str = "",
        error: str = "",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE template_errors
                SET delivery_status = ?, provider_id = ?, error = ?
                WHERE id = ?
                """,
                (delivery_status, provider_id, error, error_id),
            )

    def list_template_errors(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM template_errors ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]
