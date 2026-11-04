# EO_GENERIC_REGEX_FIX_V1381
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from ticketing import (
    REQUEST_FIELD_LABELS_RU,
    REQUEST_FIELD_LABELS_KZ,
    POST_LABEL_PATTERN,
    REFERENCE_LABEL_PATTERN,
    extract_request_draft,
    merge_request_draft,
    normalize_message,
    request_draft_text,
    utc_now,
)
from queue_user_locale import tr, is_kz

FIELD_LABELS_RU: dict[str, str] = {**REQUEST_FIELD_LABELS_RU}
FIELD_LABELS_KZ: dict[str, str] = {**REQUEST_FIELD_LABELS_KZ}
# Internal/admin history keeps the Russian label map. Ordinary-user replies use
# _field_label(), which follows the language selected for the current request.
FIELD_LABELS: dict[str, str] = FIELD_LABELS_RU
ALLOWED_FIELD_NAMES = tuple(FIELD_LABELS_RU)


def _field_label(key: str) -> str:
    return (FIELD_LABELS_KZ if is_kz() else FIELD_LABELS_RU).get(key, key)
NUMERIC_FIELDS = {"reference", "contact_phone"}
SKIP_WORDS = {
    "нет",
    "не знаю",
    "неизвестно",
    "нет номера",
    "номера нет",
    "пропустить",
    "пропусти",
    "білмеймін",
    "жоқ",
    "өткізіп жіберу",
    "өткізу",
}
LOW_INFORMATION_PROBLEMS = {
    "да", "нет", "ок", "окей", "ошибка", "сбой", "проблема", "не работает",
    "не выходит", "не могу", "помогите", "помоги",
    "иә", "жоқ", "жарайды", "қате", "мәселе", "жұмыс істемейді", "көмектесіңіз",
    "привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер",
    "спасибо", "рахмет", "сәлем", "сәлеметсіз бе", "қайырлы күн",
}
CORRECTION_RE = re.compile(
    r"(?:ошиб(?:ся|лась)|не\s+этот|номер\s+другой|нет\s*,?\s*ошиб(?:ся|лась)|не\s+тот\s+номер|неправильн\w*|"
    r"правильн\w*|верн\w*|вместо\b|замени(?:ть)?\b|поменяй\b|дұрыс\w*|қате\w*|орнына\b|ауыстыр\w*)",
    re.IGNORECASE,
)

# Рабочий минимум. ФИО/подразделение полезны, но не должны блокировать работу заявки.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "database": ["problem"],
    "mobile": ["problem"],
    "incident": ["problem"],
    "general": ["problem"],
    "support": ["problem"],
}
OPTIONAL_FIELDS: dict[str, list[str]] = {
    "database": ["reference", "fio", "post"],
    "mobile": ["reference", "fio", "post"],
    "incident": ["reference", "fio", "post"],
    "general": ["fio", "post", "reference"],
    "support": ["fio", "post", "reference"],
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def initialize(store: Any) -> None:
    with store.connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS contextual_conversation_state (
                contact_key TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                field_meta_json TEXT NOT NULL DEFAULT '{}',
                skipped_json TEXT NOT NULL DEFAULT '[]',
                last_question_field TEXT NOT NULL DEFAULT '',
                last_question_text TEXT NOT NULL DEFAULT '',
                repeat_count INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'idle',
                last_message_key TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS contextual_message_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT '',
                contact_key TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                message_key TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL DEFAULT '',
                attachment_name TEXT NOT NULL DEFAULT '',
                parsed_json TEXT NOT NULL DEFAULT '{}',
                confidence_json TEXT NOT NULL DEFAULT '{}',
                unresolved_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_context_analysis_contact
                ON contextual_message_analysis(contact_key, id DESC);
            CREATE TABLE IF NOT EXISTS ticket_context_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT '',
                ticket_id INTEGER NOT NULL DEFAULT 0,
                contact_key TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                field_name TEXT NOT NULL DEFAULT '',
                old_value TEXT NOT NULL DEFAULT '',
                new_value TEXT NOT NULL DEFAULT '',
                message_key TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                media_name TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ticket_context_history_ticket
                ON ticket_context_history(ticket_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ticket_context_history_session
                ON ticket_context_history(session_id, id ASC);
            CREATE TABLE IF NOT EXISTS ticket_context_fields (
                ticket_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                source_message_key TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(ticket_id, field_name)
            );
            """
        )


@dataclass
class ParseResult:
    fields: dict[str, str]
    confidence: dict[str, float]
    unresolved: list[dict[str, Any]]
    correction: bool = False


class MessageParser:
    """Детерминированно извлекает все пригодные поля из одного сообщения."""

    @staticmethod
    def _generic_fields(text: str) -> dict[str, str]:
        clean = normalize_message(text)
        result: dict[str, str] = {}
        if not clean:
            return result
        post_match = re.search(
            rf"(?:^|\n|\b){POST_LABEL_PATTERN}\s*[:=-]?\s*"
            r"([^,;\n]{2,80}?)(?=\s+(?:не|ошиб|проблем|сбой|нужн|надо|откр|закр|видит|выходит|работ)\b|[,;\n]|$)",
            clean,
            re.IGNORECASE,
        )
        if post_match:
            result["post"] = post_match.group(1).strip(" .,:;-")[:120]
        fio_match = re.search(
            r"(?:^|\n)\s*(?:фио\s*[:=-]?\s*)?"
            r"([А-ЯЁ][а-яё]{1,30}\s+(?:[А-ЯЁ][а-яё]{1,30}|[А-ЯЁ](?:\.[А-ЯЁ])?\.?)"
            r"(?:\s+[А-ЯЁ][а-яё]{1,30})?)\s*(?:$|\n)",
            clean,
        )
        if fio_match and not re.match(rf"^{POST_LABEL_PATTERN}\b", fio_match.group(1), re.IGNORECASE):
            result["fio"] = fio_match.group(1)[:120]

        email_match = re.search(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", clean, re.IGNORECASE)
        if email_match:
            result["email"] = email_match.group(1)[:180]

        phone_match = re.search(
            r"(?:телефон|номер\s+телефона|контактный\s+номер|мобильный\s+номер|телефон\s+нөмірі|байланыс\s+телефон\s+нөмірі)\s*[:=+-]?\s*(\+?[0-9][0-9 ()-]{6,20}[0-9])",
            clean, re.IGNORECASE,
        )
        if phone_match:
            result["contact_phone"] = phone_match.group(1).strip()[:40]

        return result

    @staticmethod
    def _labeled_confidence(text: str, field: str) -> float:
        labels = {
            "fio": r"фио",
            "post": POST_LABEL_PATTERN,
            "reference": REFERENCE_LABEL_PATTERN,
            "problem": r"(?:проблема|описание|ошибка|мәселе|сипаттама|қате|не\s+жұмыс\s+істемейді|что\s+не\s+работает|что\s+нужно)",
        }
        pattern = labels.get(field)
        return 0.995 if pattern and re.search(pattern, text, re.IGNORECASE) else 0.90

    @classmethod
    def parse(
        cls,
        text: str,
        category: str,
        existing: dict[str, str],
        *,
        last_question_field: str = "",
        last_changed_field: str = "",
        attachment_name: str = "",
    ) -> ParseResult:
        clean = normalize_message(text)
        fields = dict(extract_request_draft(clean, category)) if clean else {}
        # extract_request_draft historically parsed FIO/post only for general.
        fields.update({k: v for k, v in cls._generic_fields(clean).items() if v})
        confidence = {key: cls._labeled_confidence(clean, key) for key in fields}
        unresolved: list[dict[str, Any]] = []
        correction = bool(CORRECTION_RE.search(clean))

        stripped = clean.strip(" \t\r\n.,;:—-#№")
        single_token = bool(stripped) and bool(re.fullmatch(r"[A-ZА-ЯЁ0-9/+_.-]{1,80}", stripped, re.IGNORECASE))
        has_digit = bool(re.search(r"\d", stripped))

        # Последний заданный вопрос разрешает короткие неоднозначные ответы.
        expected = last_question_field if last_question_field in ALLOWED_FIELD_NAMES else ""
        if expected and expected not in fields and stripped:
            if expected == "email":
                email_match = re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", stripped, re.IGNORECASE)
                if email_match:
                    fields[expected] = email_match.group(0)[:180]
                    confidence[expected] = 0.995
            elif expected == "contact_phone":
                phone_digits = re.sub(r"\D", "", stripped)
                if 7 <= len(phone_digits) <= 15:
                    fields[expected] = stripped[:40]
                    confidence[expected] = 0.98
            elif expected in NUMERIC_FIELDS and single_token and has_digit:
                fields[expected] = stripped[:120]
                confidence[expected] = 0.98
            elif expected in {"post", "fio"} and len(stripped) <= 160:
                fields[expected] = stripped[:160]
                confidence[expected] = 0.96
            elif expected == "problem" and len(stripped) <= 1800:
                fields[expected] = stripped[:1800]
                confidence[expected] = 0.94

        # Коррекция без повторения подписи поля: «нет, ошибся, правильно 897...».
        if correction and clean:
            correction_token_match = re.findall(r"[A-ZА-ЯЁ0-9/+_.-]*\d[A-ZА-ЯЁ0-9/+_.-]*", clean, re.IGNORECASE)
            target = expected or (last_changed_field if last_changed_field in ALLOWED_FIELD_NAMES else "")
            if target in NUMERIC_FIELDS and correction_token_match:
                value = correction_token_match[-1].strip(".,;:")
                if value:
                    fields[target] = value[:120]
                    confidence[target] = 0.99
                    # Фраза коррекции номера сама по себе не является новым описанием проблемы.
                    if "problem" in fields and not re.search(r"(?:проблема|описание|что\s+не\s+работает)\s*[:=-]", clean, re.IGNORECASE):
                        fields.pop("problem", None)
                        confidence.pop("problem", None)

        # Файл сам по себе является полезным уточнением, но не заменяет описание
        # там, где текст проблемы обязателен. Для поддержки разрешаем файл как запрос.
        if attachment_name and not fields.get("problem") and category == "support" and not clean:
            fields["problem"] = f"Вложение: {normalize_message(attachment_name)[:240]}"
            confidence["problem"] = 0.90

        # Слишком бедное описание не должно само открыть новую заявку.
        problem = normalize_message(fields.get("problem", "")).strip(" .,:;—-")
        words_only = re.sub(r"\S*\d\S*|\S+@\S+|https?://\S+", " ", problem)
        words_only = re.sub(r"(?i)\b(?:номер|id|email|почта)\b", " ", words_only).strip(" .,:;—-")
        contextual_problem = bool(existing.get("reference") or fields.get("reference")) and problem.casefold() in {"не работает", "не выходит", "жұмыс істемейді"}
        if fields.get("reference") and len(re.sub(r"\D", "", fields["reference"])) < 3:
            fields.pop("reference", None)
        if problem and (not re.search(r"[^\W\d_]{3,}", words_only, re.UNICODE)
                        or (problem.casefold() in LOW_INFORMATION_PROBLEMS and not contextual_problem)):
            fields.pop("problem", None)
            confidence.pop("problem", None)
        elif problem and len(re.findall(r"[A-ZА-ЯЁ0-9]+", problem, re.IGNORECASE)) < 2 and len(problem) < 12:
            fields.pop("problem", None)
            confidence.pop("problem", None)

        return ParseResult(fields, confidence, unresolved, correction)


class ContextEngine:
    """Объединяет сообщения в один черновик и хранит источник каждого поля."""

    @staticmethod
    def _state(store: Any, contact_key: str) -> dict[str, Any]:
        with store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM contextual_conversation_state WHERE contact_key = ?",
                (contact_key,),
            ).fetchone()
        if not row:
            return {}
        result = dict(row)
        result["field_meta"] = _load_json(result.get("field_meta_json"), {})
        result["skipped"] = _load_json(result.get("skipped_json"), [])
        return result

    @staticmethod
    def _write_state(store: Any, contact_key: str, state: dict[str, Any]) -> None:
        with store.connection() as connection:
            connection.execute(
                """
                INSERT INTO contextual_conversation_state
                    (contact_key, category, session_id, field_meta_json, skipped_json,
                     last_question_field, last_question_text, repeat_count, state,
                     last_message_key, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(contact_key) DO UPDATE SET
                    category=excluded.category, session_id=excluded.session_id,
                    field_meta_json=excluded.field_meta_json, skipped_json=excluded.skipped_json,
                    last_question_field=excluded.last_question_field,
                    last_question_text=excluded.last_question_text,
                    repeat_count=excluded.repeat_count, state=excluded.state,
                    last_message_key=excluded.last_message_key, updated_at=excluded.updated_at
                """,
                (
                    contact_key, state.get("category", ""), state.get("session_id", ""),
                    _json(state.get("field_meta", {})), _json(state.get("skipped", [])),
                    state.get("last_question_field", ""), state.get("last_question_text", ""),
                    int(state.get("repeat_count", 0) or 0), state.get("state", "idle"),
                    state.get("last_message_key", ""), utc_now(),
                ),
            )

    @classmethod
    def reset(cls, store: Any, contact_key: str, category: str = "") -> str:
        if not contact_key:
            return ""
        session_id = uuid.uuid4().hex
        cls._write_state(store, contact_key, {
            "category": category,
            "session_id": session_id,
            "field_meta": {},
            "skipped": [],
            "last_question_field": "",
            "last_question_text": "",
            "repeat_count": 0,
            "state": "collecting" if category else "idle",
            "last_message_key": "",
        })
        return session_id

    @staticmethod
    def _record_history(
        store: Any, *, session_id: str, ticket_id: int, contact_key: str,
        chat_id: str, event_type: str, field_name: str = "", old_value: str = "",
        new_value: str = "", message_key: str = "", text: str = "", media_name: str = "",
        confidence: float = 0.0, source: str = "WhatsApp",
    ) -> None:
        with store.connection() as connection:
            connection.execute(
                """
                INSERT INTO ticket_context_history
                    (session_id,ticket_id,contact_key,chat_id,event_type,field_name,old_value,new_value,
                     message_key,text,media_name,confidence,source,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id, int(ticket_id or 0), contact_key, chat_id, event_type, field_name,
                    normalize_message(old_value)[:1800], normalize_message(new_value)[:1800],
                    normalize_message(message_key)[:180], normalize_message(text)[:32000],
                    normalize_message(media_name)[:240], float(confidence or 0), source[:80], utc_now(),
                ),
            )

    @classmethod
    def collect(
        cls, store: Any, contact_key: str, category: str, text: str, *,
        attachment_name: str = "", message_key: str = "", chat_id: str = "",
        active_ticket_id: int = 0, required_override: list[str] | None = None,
    ) -> dict[str, Any]:
        state = cls._state(store, contact_key)
        if not state or state.get("category") != category or not state.get("session_id"):
            cls.reset(store, contact_key, category)
            state = cls._state(store, contact_key)
        session_id = str(state.get("session_id", ""))
        existing = store.get_conversation_draft(contact_key)
        field_meta = state.get("field_meta") if isinstance(state.get("field_meta"), dict) else {}
        last_changed_field = str(field_meta.get("_last_changed_field", ""))
        clean = normalize_message(text)
        normalized_skip = clean.casefold().strip(" .,!?:;—-")

        # «нет / не знаю / пропустить» относится именно к последнему вопросу.
        expected = str(state.get("last_question_field", ""))
        if normalized_skip in SKIP_WORDS and expected:
            required = required_fields(category, required_override)
            if expected not in required:
                skipped = list(state.get("skipped") or [])
                if expected not in skipped:
                    skipped.append(expected)
                state["skipped"] = skipped
                state["repeat_count"] = 0
                state["last_message_key"] = message_key
                cls._record_history(
                    store, session_id=session_id, ticket_id=active_ticket_id, contact_key=contact_key,
                    chat_id=chat_id, event_type="field_skipped", field_name=expected,
                    message_key=message_key, text=clean, media_name=attachment_name, source="WhatsApp",
                )
                state["last_question_field"] = ""
                state["last_question_text"] = ""
                cls._write_state(store, contact_key, state)
            else:
                state["repeat_count"] = int(state.get("repeat_count", 0) or 0) + 1
                cls._write_state(store, contact_key, state)
                label = _field_label(expected)
                return {
                    "ready": False, "draft": existing, "missing": [expected], "confidence": {},
                    "reply": tr(
                        f"Для регистрации заявки необходимо указать «{label}». Если сведения сейчас недоступны, вернитесь в меню командой 0 и выберите другую категорию либо направьте данные позднее.",
                        f"Өтінімді тіркеу үшін «{label}» көрсету қажет. Егер мәлімет қазір қолжетімсіз болса, 0 командасымен мәзірге оралып, басқа санатты таңдаңыз немесе ақпаратты кейінірек жіберіңіз.",
                    ),
                    "contextual": True,
                }

        parsed = MessageParser.parse(
            clean, category, existing,
            last_question_field=expected,
            last_changed_field=last_changed_field,
            attachment_name=attachment_name,
        )
        if parsed.unresolved:
            cls._record_history(store, session_id=session_id,ticket_id=0,contact_key=contact_key,chat_id=chat_id,
                event_type="message_received",message_key=message_key,text=normalize_message(text),media_name=attachment_name)
            detail = parsed.unresolved[0]
            if detail.get("field"):
                state["last_question_field"] = detail["field"]
            with store.connection() as connection:
                connection.execute(
                    """INSERT INTO contextual_message_analysis
                       (session_id,contact_key,chat_id,message_key,category,raw_text,attachment_name,parsed_json,confidence_json,unresolved_json,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, contact_key, chat_id, message_key, category, clean, attachment_name,
                     _json(parsed.fields), _json(parsed.confidence), _json(parsed.unresolved), utc_now()),
                )
            question = str(parsed.unresolved[0].get("question") or tr("Уточните значение, пожалуйста.", "Сұрақтың мәнін нақтылап жіберіңіз."))
            state["repeat_count"] = int(state.get("repeat_count", 0) or 0) + 1
            state["last_question_text"] = question
            state["last_message_key"] = message_key
            cls._write_state(store, contact_key, state)
            return {
                "ready": False, "draft": existing, "missing": missing_required(category, existing, required_override),
                "confidence": parsed.confidence, "reply": question, "clarify": True,
                "contextual": True,
            }

        if parsed.fields.get("problem") and existing.get("problem") and not parsed.correction:
            old_problem = normalize_message(existing["problem"])
            new_problem = normalize_message(parsed.fields["problem"])
            if new_problem not in old_problem.splitlines():
                parsed.fields["problem"] = old_problem + "\n" + new_problem
            else:
                parsed.fields["problem"] = old_problem
        merged = merge_request_draft(existing, parsed.fields)
        changed_fields: list[str] = []
        for key, new_value in parsed.fields.items():
            old_value = normalize_message(existing.get(key, ""))
            new_value = normalize_message(new_value)
            if not new_value or old_value == new_value:
                continue
            changed_fields.append(key)
            confidence = float(parsed.confidence.get(key, 0.90))
            field_meta[key] = {
                "confidence": confidence,
                "source": "WhatsApp",
                "message_key": message_key,
                "updated_at": utc_now(),
            }
            field_meta["_last_changed_field"] = key
            cls._record_history(
                store, session_id=session_id, ticket_id=active_ticket_id, contact_key=contact_key,
                chat_id=chat_id, event_type="field_corrected" if old_value else "field_found",
                field_name=key, old_value=old_value, new_value=new_value,
                message_key=message_key, text=clean, media_name=attachment_name,
                confidence=confidence, source="WhatsApp",
            )
        store.set_conversation_draft(contact_key, merged)

        with store.connection() as connection:
            connection.execute(
                """INSERT INTO contextual_message_analysis
                   (session_id,contact_key,chat_id,message_key,category,raw_text,attachment_name,parsed_json,confidence_json,unresolved_json,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, contact_key, chat_id, message_key, category, clean, attachment_name,
                 _json(parsed.fields), _json(parsed.confidence), "[]", utc_now()),
            )

        if clean or attachment_name:
            cls._record_history(
                store, session_id=session_id, ticket_id=active_ticket_id, contact_key=contact_key,
                chat_id=chat_id, event_type="message_received", message_key=message_key,
                text=clean, media_name=attachment_name, source="WhatsApp",
            )

        missing = missing_required(category, merged, required_override)
        state["field_meta"] = field_meta
        state["last_message_key"] = message_key
        state["state"] = "ready" if not missing else "collecting"
        if changed_fields:
            state["repeat_count"] = 0
        else:
            state["repeat_count"] = int(state.get("repeat_count", 0) or 0) + 1

        if not missing:
            state["last_question_field"] = ""
            state["last_question_text"] = ""
            cls._write_state(store, contact_key, state)
            return {
                "ready": True,
                "draft": merged,
                "missing": [],
                "confidence": {key: float(meta.get("confidence", 0)) for key, meta in field_meta.items() if isinstance(meta, dict)},
                "request_text": request_draft_text(category, merged) or clean,
                "contextual": True,
            }

        next_field = missing[0]
        state["last_question_field"] = next_field
        labels = [_field_label(key) for key in missing]
        saved_labels = [_field_label(key) for key in changed_fields]
        if int(state.get("repeat_count", 0) or 0) >= 2:
            prefix = tr("Полученные сведения сохранены. ", "Алынған мәліметтер сақталды. ") if changed_fields else ""
            reply = prefix + tr(
                "Для продолжения регистрации необходимо указать: " + ", ".join(labels) + ".",
                "Тіркеуді жалғастыру үшін мыналарды көрсету қажет: " + ", ".join(labels) + ".",
            )
        elif len(labels) == 1:
            reply = tr(
                f"Для продолжения регистрации укажите, пожалуйста, {labels[0]}.",
                f"Тіркеуді жалғастыру үшін {labels[0]} көрсетіңіз.",
            )
            if saved_labels:
                reply = tr(f"Получены сведения: {', '.join(saved_labels)}. ", f"Мәліметтер алынды: {', '.join(saved_labels)}. ") + reply
        else:
            reply = tr(
                "Для регистрации обращения необходимо дополнительно указать: " + ", ".join(labels) + ".",
                "Өтінімді тіркеу үшін қосымша мыналарды көрсету қажет: " + ", ".join(labels) + ".",
            )
            if saved_labels:
                reply = tr(f"Получены сведения: {', '.join(saved_labels)}. ", f"Мәліметтер алынды: {', '.join(saved_labels)}. ") + reply
        state["last_question_text"] = reply
        cls._write_state(store, contact_key, state)
        return {
            "ready": False,
            "draft": merged,
            "missing": missing,
            "confidence": {key: float(meta.get("confidence", 0)) for key, meta in field_meta.items() if isinstance(meta, dict)},
            "reply": reply,
            "contextual": True,
        }


class TicketEngine:
    """Принимает решение о создании/дополнении и связывает историю с заявкой."""

    @staticmethod
    def commit(store: Any, contact_key: str, ticket_id: int, category: str, *, message_key: str = "", source: str = "WhatsApp") -> None:
        if not ticket_id:
            return
        state = ContextEngine._state(store, contact_key)
        session_id = str(state.get("session_id", ""))
        draft = store.get_conversation_draft(contact_key)
        field_meta = state.get("field_meta") if isinstance(state.get("field_meta"), dict) else {}
        now = utc_now()
        with store.connection() as connection:
            if session_id:
                connection.execute(
                    "UPDATE ticket_context_history SET ticket_id = ? WHERE session_id = ? AND ticket_id = 0",
                    (ticket_id, session_id),
                )
            for field_name, value in draft.items():
                if field_name not in ALLOWED_FIELD_NAMES or not normalize_message(value):
                    continue
                meta = field_meta.get(field_name, {}) if isinstance(field_meta.get(field_name), dict) else {}
                connection.execute(
                    """INSERT INTO ticket_context_fields(ticket_id,field_name,value,confidence,source,source_message_key,updated_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(ticket_id,field_name) DO UPDATE SET
                         value=excluded.value, confidence=excluded.confidence, source=excluded.source,
                         source_message_key=excluded.source_message_key, updated_at=excluded.updated_at""",
                    (ticket_id, field_name, value, float(meta.get("confidence", 0.9) or 0.9),
                     str(meta.get("source", source)), str(meta.get("message_key", message_key)), now),
                )
        ContextEngine._record_history(
            store, session_id=session_id, ticket_id=ticket_id, contact_key=contact_key,
            chat_id="", event_type="ticket_created", message_key=message_key,
            text=f"Контекст обращения закреплён за заявкой #{ticket_id}", source=source,
        )
        if session_id:
            with store.connection() as connection:
                messages = connection.execute(
                    "SELECT DISTINCT chat_id,message_key FROM ticket_context_history WHERE session_id=? AND event_type='message_received' AND message_key<>''",
                    (session_id,),
                ).fetchall()
            for message in messages:
                if message["chat_id"]:
                    store.link_whatsapp_message_to_ticket(message["chat_id"], message["message_key"], ticket_id)
        state["state"] = "active"
        state["last_question_field"] = ""
        state["last_question_text"] = ""
        ContextEngine._write_state(store, contact_key, state)

    @staticmethod
    def record_followup(
        store: Any, contact_key: str, ticket_id: int, *, chat_id: str = "", message_key: str = "",
        text: str = "", media_name: str = "",
    ) -> None:
        ticket = store.get_ticket(ticket_id)
        if not ticket:
            return
        category = str(ticket.get("category", "general"))
        state = ContextEngine._state(store, contact_key)
        session_id = str(state.get("session_id", "")) or uuid.uuid4().hex
        existing = {item["field_name"]: item["value"] for item in ticket_fields(store, ticket_id)}
        parsed = MessageParser.parse(
            text, category, existing,
            last_question_field=str(state.get("last_question_field", "")),
            last_changed_field=str((state.get("field_meta") or {}).get("_last_changed_field", "")),
            attachment_name=media_name,
        )
        if parsed.fields.get("problem") and existing.get("problem") and not parsed.correction:
            new_problem = normalize_message(parsed.fields["problem"])
            old_problem = normalize_message(existing["problem"])
            parsed.fields["problem"] = old_problem if new_problem in old_problem.splitlines() else old_problem + "\n" + new_problem
        for key, value in parsed.fields.items():
            confidence = float(parsed.confidence.get(key, 0.9))
            if confidence < 0.80 or key not in ALLOWED_FIELD_NAMES:
                continue
            old = normalize_message(existing.get(key, ""))
            if value != old:
                _upsert_ticket_field(store, ticket_id, key, value, confidence, "WhatsApp follow-up", message_key)
                ContextEngine._record_history(
                    store, session_id=session_id, ticket_id=ticket_id, contact_key=contact_key,
                    chat_id=chat_id, event_type="followup_field_corrected" if old else "followup_field_found",
                    field_name=key, old_value=old, new_value=value, message_key=message_key,
                    text=text, media_name=media_name, confidence=confidence, source="WhatsApp follow-up",
                )
        addition = normalize_message(text)
        if media_name:
            addition += "\nВложение: " + normalize_message(media_name)
        if addition.strip():
            with store.connection() as connection:
                connection.execute(
                    "UPDATE tickets SET original_text=COALESCE(original_text,'') || ?, updated_at=? WHERE id=?",
                    ("\n\nДополнение пользователя:\n" + addition.strip(), utc_now(), ticket_id),
                )
        ContextEngine._record_history(
            store, session_id=session_id, ticket_id=ticket_id, contact_key=contact_key,
            chat_id=chat_id, event_type="followup", message_key=message_key,
            text=text, media_name=media_name, source="WhatsApp follow-up",
        )


PROMPT_REQUIRED_FIELD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b(?:электронн\w*\s+почт\w*|почт\w*|электрондық\s+пошта\w*|e-?mail|email)\b", re.IGNORECASE)),
    ("contact_phone", re.compile(r"\b(?:контактн\w*\s+номер|номер\s+телефона|телефон|мобильн\w*\s+номер|телефон\s+нөмірі|байланыс\s+телефон\s+нөмірі)\b", re.IGNORECASE)),
    ("fio", re.compile(r"\b(?:фио|ф\.\s*и\.\s*о\.)\b", re.IGNORECASE)),
    ("post", re.compile(r"\b(?:подразделени\w*|отдел\w*|филиал\w*|бөлімше\w*)\b", re.IGNORECASE)),
    ("reference", re.compile(r"\b(?:номер\s+(?:заявки|заказа|договора|документа)|идентификатор\w*|\bID\b)", re.IGNORECASE)),
    ("problem", re.compile(r"\b(?:описание\s+проблемы|описать\s+проблему|мәселенің\s+сипаттамасы|мәселені\s+сипатта|не\s+жұмыс\s+істемейді|қате\s+мәтіні|сұрағыңызды|что\s+не\s+работает|текст\s+ошибки|описание\s+ошибки|ваш\s+вопрос)\b", re.IGNORECASE)),
)


def required_fields(category: str, override: list[str] | None = None) -> list[str]:
    if override:
        return [field for field in override if field in ALLOWED_FIELD_NAMES]
    return list(REQUIRED_FIELDS.get(category, ["problem"]))


def missing_required(category: str, draft: dict[str, str], override: list[str] | None = None) -> list[str]:
    return [key for key in required_fields(category, override) if not normalize_message(str(draft.get(key, "")))]


def reset_context(store: Any, contact_key: str, category: str = "") -> str:
    return ContextEngine.reset(store, contact_key, category)


def collect_request(
    store: Any, contact_key: str, category: str, text: str, *, attachment_name: str = "",
    message_key: str = "", chat_id: str = "", active_ticket_id: int = 0,
    required_fields_override: list[str] | None = None,
) -> dict[str, Any]:
    return ContextEngine.collect(
        store, contact_key, category, text, attachment_name=attachment_name,
        message_key=message_key, chat_id=chat_id, active_ticket_id=active_ticket_id,
        required_override=required_fields_override,
    )


def commit_ticket(store: Any, contact_key: str, ticket_id: int, category: str, *, message_key: str = "", source: str = "WhatsApp") -> None:
    TicketEngine.commit(store, contact_key, ticket_id, category, message_key=message_key, source=source)


def record_followup(
    store: Any, contact_key: str, ticket_id: int, *, chat_id: str = "", message_key: str = "",
    text: str = "", media_name: str = "",
) -> None:
    TicketEngine.record_followup(
        store, contact_key, ticket_id, chat_id=chat_id, message_key=message_key,
        text=text, media_name=media_name,
    )


def _upsert_ticket_field(store: Any, ticket_id: int, field_name: str, value: str, confidence: float, source: str, message_key: str) -> None:
    if field_name not in ALLOWED_FIELD_NAMES or not normalize_message(value):
        return
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO ticket_context_fields(ticket_id,field_name,value,confidence,source,source_message_key,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(ticket_id,field_name) DO UPDATE SET
                 value=excluded.value, confidence=excluded.confidence, source=excluded.source,
                 source_message_key=excluded.source_message_key, updated_at=excluded.updated_at""",
            (ticket_id, field_name, normalize_message(value)[:1800], float(confidence or 0), source[:80], message_key[:180], utc_now()),
        )


def ticket_fields(store: Any, ticket_id: int) -> list[dict[str, Any]]:
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM ticket_context_fields WHERE ticket_id = ? ORDER BY field_name",
            (int(ticket_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def ticket_history(store: Any, ticket_id: int, limit: int = 200) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 200), 500))
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM ticket_context_history WHERE ticket_id = ? ORDER BY id DESC LIMIT ?",
            (int(ticket_id), safe_limit),
        ).fetchall()
    return [dict(row) for row in rows]


def apply_history_item(
    store: Any, ticket_id: int, history_id: int, field_name: str, actor: str, mode: str = "field",
) -> tuple[bool, str]:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM ticket_context_history WHERE id = ? AND ticket_id = ?",
            (int(history_id), int(ticket_id)),
        ).fetchone()
    if not row:
        return False, "Элемент истории не найден"
    item = dict(row)
    if mode == "media":
        chat_id = normalize_message(item.get("chat_id", ""))
        message_key = normalize_message(item.get("message_key", ""))
        if not chat_id or not message_key:
            return False, "У этого элемента нет WhatsApp-медиа для привязки"
        linked = store.link_whatsapp_message_to_ticket(chat_id, message_key, ticket_id)
        if linked:
            ContextEngine._record_history(
                store, session_id=str(item.get("session_id", "")), ticket_id=ticket_id,
                contact_key=str(item.get("contact_key", "")), chat_id=chat_id,
                event_type="history_media_linked", message_key=message_key,
                media_name=str(item.get("media_name", "")), source=f"Сотрудник: {actor}",
            )
            with store.connection() as connection:
                connection.execute(
                    "INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)",
                    (ticket_id, "Медиа из истории привязано к заявке", actor, utc_now()),
                )
            return True, "Медиа привязано к заявке"
        return False, "Не удалось привязать медиа: сообщение не найдено"

    if field_name not in ALLOWED_FIELD_NAMES:
        return False, "Некорректное поле заявки"
    value = normalize_message(item.get("new_value", "") or item.get("text", "") or item.get("media_name", ""))
    if not value:
        return False, "В выбранной записи нет значения для переноса"
    existing = {entry["field_name"]: entry["value"] for entry in ticket_fields(store, ticket_id)}
    old_value = normalize_message(existing.get(field_name, ""))
    confidence = float(item.get("confidence", 0) or 0) or 0.95
    _upsert_ticket_field(store, ticket_id, field_name, value, confidence, f"Сотрудник: {actor}", str(item.get("message_key", "")))
    ContextEngine._record_history(
        store, session_id=str(item.get("session_id", "")), ticket_id=ticket_id,
        contact_key=str(item.get("contact_key", "")), chat_id=str(item.get("chat_id", "")),
        event_type="history_applied_to_field", field_name=field_name, old_value=old_value,
        new_value=value, message_key=str(item.get("message_key", "")), text=str(item.get("text", "")),
        media_name=str(item.get("media_name", "")), confidence=confidence, source=f"Сотрудник: {actor}",
    )
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO events(ticket_id,action,actor,created_at) VALUES(?,?,?,?)",
            (ticket_id, f"Из истории обновлено поле: {FIELD_LABELS.get(field_name, field_name)}", actor, utc_now()),
        )
    return True, f"Поле «{FIELD_LABELS.get(field_name, field_name)}» обновлено из истории"


def request_fragments(store, contact_key):
    state = ContextEngine._state(store, contact_key)
    session_id = str(state.get("session_id", ""))
    if not session_id:
        return []
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT text, media_name FROM ticket_context_history WHERE session_id=? AND event_type='message_received' ORDER BY id",
            (session_id,),
        ).fetchall()
    fragments = []
    for row in rows:
        value = str(row["text"] or "")
        if row["media_name"]:
            value += "\nВложение: " + str(row["media_name"])
        if value.strip():
            fragments.append(value.strip())
    return fragments
