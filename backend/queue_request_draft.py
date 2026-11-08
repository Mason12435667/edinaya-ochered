"""Поля черновика заявки, извлечённые из свободного текста пользователя, и их подписи."""
from __future__ import annotations

import re

from queue_core import normalize_message
from queue_language import tolerant_labels


# Neutral field labels. "post" is the internal key for a department/unit.
POST_LABEL_PATTERN = r"(?:подразделение|отдел|филиал|бөлімше)"
REFERENCE_LABEL_PATTERN = r"(?:номер|№|id|идентификатор|код)"


REQUEST_FIELD_LABELS_RU = {
    "email": "электронную почту",
    "contact_phone": "контактный номер телефона",
    "fio": "ФИО",
    "post": "подразделение",
    "reference": "номер / идентификатор",
    "problem": "описание проблемы",
}


REQUEST_FIELD_LABELS_KZ = {
    "email": "электрондық поштаны",
    "contact_phone": "байланыс телефон нөмірін",
    "fio": "Аты-жөнін",
    "post": "бөлімшені",
    "reference": "нөмір / идентификаторды",
    "problem": "мәселенің сипаттамасын",
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
            r"проблема", r"описание", r"мәселе", r"сипаттама", r"мәселенің\s+сипаттамасы", r"не\s+жұмыс\s+істемейді", r"что\s+не\s+работает", r"что\s+происходит",
            r"что\s+нужно\s+сделать", r"не\s+істеу\s+керек", r"ошибка", r"қате", r"вопрос", r"сұрақ", r"что\s+именно.*",
        ],
    )
    if explicit:
        return explicit[:1800]

    lines: list[str] = []
    reference_prefix = re.compile(
        rf"^\s*{REFERENCE_LABEL_PATTERN}\s*[:№#-]?\s*[A-ZА-Я0-9/-]{{2,}}\s*[,;:—-]?\s*",
        re.IGNORECASE,
    )
    pure_field_prefix = re.compile(
        rf"^\s*(?:фио|должность|{POST_LABEL_PATTERN})\b",
        re.IGNORECASE,
    )
    for line in normalized.splitlines():
        line_plain = line.strip(" .,:;-—")
        line_folded = line_plain.casefold()
        # Не включаем в описание уже распознанные ФИО/подразделение, даже если
        # пользователь написал их без подписи поля (например «Специалист Иванов И»).
        if fio_value:
            fio_folded = fio_value.casefold()
            if line_folded == fio_folded or line_folded.endswith(" " + fio_folded):
                continue
        if post_value:
            post_folded = post_value.casefold()
            if line_folded == post_folded or re.fullmatch(
                rf"{POST_LABEL_PATTERN}\s*[:=-]?\s*{re.escape(post_folded)}",
                line_folded,
            ):
                continue
        # Если номер и описание пришли одной строкой, убираем только сам номер,
        # а хвост строки сохраняем как описание проблемы.
        prefix_match = reference_prefix.match(line)
        # Words such as «не» are not identifiers: preserve «Номер не найден».
        trimmed = (line[prefix_match.end():] if prefix_match and re.search(r"\d", prefix_match.group(0)) else line).strip(" .,:;-—")
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
    fio = _first_named_value(normalized, [r"фио", r"ф\.?и\.?о\.?"])
    if not fio and category == "general":
        # Пользователи часто пишут без названий полей, например:
        # «Специалист Иванов И» или «Иванов И.А.». Роль сотрудника не является
        # частью ФИО, а одна/две буквы после фамилии считаются инициалами.
        fio_line_pattern = re.compile(
            r"^(?:(?:техник|сотрудник|специалист|оператор)\s+)?"
            r"([А-ЯЁ][а-яё]{1,30}\s+(?:[А-ЯЁ][а-яё]{1,30}|[А-ЯЁ](?:\.[А-ЯЁ])?\.?)(?:\s+[А-ЯЁ][а-яё]{1,30})?)$",
            re.IGNORECASE,
        )
        for raw_line in normalized.splitlines():
            candidate = raw_line.strip(" .,:;-—")
            if re.match(rf"^(?:{POST_LABEL_PATTERN}|должность|проблема|описание)\b", candidate, re.IGNORECASE):
                continue
            match = fio_line_pattern.match(candidate)
            if match:
                fio = match.group(1)
                break
        if not fio and not re.match(rf"^\s*(?:{POST_LABEL_PATTERN}|должность|проблема|описание)\b", normalized, re.IGNORECASE):
            m = re.search(
                r"\b([А-ЯЁ][а-яё]{1,30}\s+(?:[А-ЯЁ][а-яё]{1,30}|[А-ЯЁ](?:\.[А-ЯЁ])?\.?)(?:\s+[А-ЯЁ][а-яё]{1,30})?)\b",
                normalized,
            )
            fio = m.group(1) if m else ""
    # A complaint must not be inferred as a person's name merely from 2–3 words.
    if category == "general" and re.search(
        r"(?i)(?:\bне\s+|ошиб|проблем|сбой|завис|неактив|откр|закр|нужно|требуется|мәселе|қате|істем|ашылмай|көрінб)", normalized
    ):
        fio = _first_named_value(normalized, [r"фио", r"ф\.?и\.?о\.?"])
    post = _first_named_value(normalized, [POST_LABEL_PATTERN])
    if not post and category == "general":
        post_match = re.search(
            rf"\b{POST_LABEL_PATTERN}\s*[:=-]?\s*([^,;\n]{{2,80}}?)(?=\s+(?:не|проблем|ошиб|нужно|надо|завис|откр|закр|треб)\b|[,;]|$)",
            normalized,
            re.IGNORECASE,
        )
        post = post_match.group(1).strip() if post_match else ""
    if fio:
        result["fio"] = fio[:120]
    if post:
        result["post"] = post[:120]

    ref = ""
    for match in re.finditer(
        rf"(?<![\w]){REFERENCE_LABEL_PATTERN}\s*[:№#-]?\s*([A-ZА-Я0-9/-]{{3,}})", normalized, re.IGNORECASE
    ):
        value = match.group(1)
        if re.search(r"\d", value):
            ref = value
            break
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


def request_draft_text(category: str, draft: dict[str, str]) -> str:
    parts: list[str] = []
    if draft.get("fio"):
        parts.append(f"ФИО: {draft['fio']}")
    if draft.get("post"):
        parts.append(f"Подразделение: {draft['post']}")
    if draft.get("reference"):
        parts.append(f"Номер / идентификатор: {draft['reference']}")
    dynamic_labels = {
        "email": "Электронная почта",
        "contact_phone": "Контактный телефон",
    }
    for key, label in dynamic_labels.items():
        value = normalize_message(str(draft.get(key, "")))
        if value and not any(line.startswith(f"{label}:") for line in parts):
            parts.append(f"{label}: {value}")
    if draft.get("problem"):
        parts.append(f"Проблема: {draft['problem']}")
    return "\n".join(parts)
