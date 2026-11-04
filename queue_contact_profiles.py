"""Contact language/profile persistence with an explicit store dependency."""
from __future__ import annotations
import hashlib
import json
import re
import queue_user_locale
from queue_core import log_suppressed, normalize_phone, valid_chat_id
from ticketing import TicketStore, normalize_message

LANGUAGE_SETTING_PREFIX = "whatsapp_user_language:"
USER_PROFILE_SETTING_PREFIX = "whatsapp_user_profile:"
USER_PROFILE_CONTEXT = "__user_profile__"


class ContactProfiles:
    def __init__(self, store: TicketStore):
        self.store = store

    def _language_identities(self, chat_id: str = "", phone: str = "") -> list[str]:
        """Return every stable identity we know for one WhatsApp user.

        WhatsApp can alternate between phone-backed @c.us and LID identifiers.
        Persisting the language under both aliases prevents a previously selected
        language from appearing to flip when the connector reports another alias.
        """
        identities: list[str] = []
        phone_digits = re.sub(r"\D", "", normalize_phone(phone))
        if len(phone_digits) >= 10:
            identities.append(f"phone:{phone_digits}")
        clean_chat = valid_chat_id(chat_id)
        if clean_chat:
            try:
                clean_chat = self.store.canonical_whatsapp_chat_id(clean_chat) or clean_chat
            except Exception:
                log_suppressed("queue_contact_profiles.identities")
            identities.append(f"chat:{clean_chat}")
        result: list[str] = []
        for identity in identities:
            if identity and identity not in result:
                result.append(identity)
        return result

    def _language_identity(self, chat_id: str = "", phone: str = "") -> str:
        identities = self._language_identities(chat_id, phone)
        return identities[0] if identities else ""

    def _language_setting_key(self, identity: str) -> str:
        if not identity:
            return ""
        digest = hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()
        return f"{LANGUAGE_SETTING_PREFIX}{digest}"

    def get_user_language(self, identity: str) -> str:
        key = self._language_setting_key(identity)
        if not key:
            return ""
        value = self.store.get_setting(key, "").strip().casefold()
        return value if value in {queue_user_locale.LANG_RU, queue_user_locale.LANG_KZ} else ""

    def set_user_language(self, identity: str, language: str) -> str:
        key = self._language_setting_key(identity)
        lang = queue_user_locale.normalize_language(language)
        if key:
            self.store.set_setting(key, lang)
        return lang

    def clear_user_language(self, identity: str) -> None:
        key = self._language_setting_key(identity)
        if key:
            self.store.set_setting(key, "")

    def get_contact_language(self, chat_id: str = "", phone: str = "") -> str:
        """Resolve language across phone/chat aliases and heal stale conflicts."""
        identities = self._language_identities(chat_id, phone)
        values = [(identity, self.get_user_language(identity)) for identity in identities]
        selected = next((value for _identity, value in values if value), "")
        if selected:
            for identity, value in values:
                if value != selected:
                    self.set_user_language(identity, selected)
        return selected

    def set_contact_language(self, chat_id: str, phone: str, language: str) -> str:
        lang = queue_user_locale.normalize_language(language)
        identities = self._language_identities(chat_id, phone)
        if not identities:
            return lang
        for identity in identities:
            self.set_user_language(identity, lang)
        return lang

    def clear_contact_language(self, chat_id: str = "", phone: str = "") -> None:
        for identity in self._language_identities(chat_id, phone):
            self.clear_user_language(identity)

    def _profile_setting_key(self, identity: str) -> str:
        if not identity:
            return ""
        digest = hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()
        return f"{USER_PROFILE_SETTING_PREFIX}{digest}"

    def _clean_user_profile_value(self, value: object) -> str:
        clean = normalize_message(str(value or "")).strip(" \t\r\n,;.")[:120]
        return clean if len(clean) >= 2 else ""

    def get_user_profile(self, identity: str) -> dict[str, str]:
        key = self._profile_setting_key(identity)
        if not key:
            return {}
        raw = self.store.get_setting(key, "")
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            return {}
        post_name = self._clean_user_profile_value(payload.get("post", ""))
        position = self._clean_user_profile_value(payload.get("position", ""))
        return {"post": post_name, "position": position} if post_name and position else {}

    def set_user_profile(self, identity: str, post_name: str, position: str) -> dict[str, str]:
        key = self._profile_setting_key(identity)
        profile = {
            "post": self._clean_user_profile_value(post_name),
            "position": self._clean_user_profile_value(position),
        }
        if key and profile["post"] and profile["position"]:
            self.store.set_setting(key, json.dumps(profile, ensure_ascii=False, separators=(",", ":")))
            return profile
        return {}

    def get_contact_profile(self, chat_id: str = "", phone: str = "") -> dict[str, str]:
        """Resolve saved post/position across phone/chat aliases and heal them."""
        identities = self._language_identities(chat_id, phone)
        values = [(identity, self.get_user_profile(identity)) for identity in identities]
        selected = next((profile for _identity, profile in values if profile), {})
        if selected:
            for identity, profile in values:
                if profile != selected:
                    self.set_user_profile(identity, selected["post"], selected["position"])
        return dict(selected)

    def set_contact_profile(self, chat_id: str, phone: str, post_name: str, position: str) -> dict[str, str]:
        post_value = self._clean_user_profile_value(post_name)
        position_value = self._clean_user_profile_value(position)
        if not post_value or not position_value:
            return {}
        profile = {"post": post_value, "position": position_value}
        for identity in self._language_identities(chat_id, phone):
            self.set_user_profile(identity, post_value, position_value)
        return profile

    def profile_prompt_text(self, error: bool = False) -> str:
        if queue_user_locale.is_kz():
            prefix = "Екі жолды да көрсетіңіз.\n\n" if error else ""
            return (
                prefix
                + "Өтініммен жұмысты бастамас бұрын, бөлімшеңіз бен лауазымыңызды бір хабарламамен жазыңыз:\n"
                "Бөлімше: [бөлімше атауы]\n"
                "Лауазым: [лауазымыңыз]\n\n"
                "Бұл мәліметтер сақталады, келесі өтінімдерде қайта енгізудің қажеті жоқ."
            )
        prefix = "Укажите оба поля.\n\n" if error else ""
        return (
            prefix
            + "Перед началом работы с заявками укажите одним сообщением своё подразделение и должность:\n"
            "Подразделение: [название подразделения]\n"
            "Должность: [ваша должность]\n\n"
            "Эти данные сохранятся, и в следующих заявках вводить их повторно не потребуется."
        )

    def parse_contact_profile(self, text: str) -> dict[str, str]:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return {}
        post_name = ""
        position = ""
        unlabeled: list[str] = []
        for source_line in raw.split("\n"):
            line = source_line.strip().lstrip("•*-—– ").strip()
            if not line:
                continue
            match = re.match(r"(?i)^(?:подразделение|отдел|филиал|бөлімше)\s*[:=\-–—]\s*(.+)$", line)
            if match:
                post_name = self._clean_user_profile_value(match.group(1))
                continue
            match = re.match(r"(?i)^(?:должность|лауазым)\s*[:=\-–—]\s*(.+)$", line)
            if match:
                position = self._clean_user_profile_value(match.group(1))
                continue
            unlabeled.append(line)
        if not post_name and not position and len(unlabeled) == 2:
            post_name = self._clean_user_profile_value(unlabeled[0])
            position = self._clean_user_profile_value(unlabeled[1])
        if not post_name or not position:
            return {}
        # A bare identifier is not an occupation, even in a two-line reply.
        if not re.search(r"[^\W\d_]{3,}", position, re.UNICODE):
            return {}
        if any(re.search(r"\d", token) for token in position.split()) and not re.search(r"[^\W\d_]{3,}", " ".join(t for t in position.split() if not re.search(r"\d", t)), re.UNICODE):
            return {}
        return {"post": post_name, "position": position}

    def apply_contact_profile_to_ticket(self, ticket: dict[str, object], profile: dict[str, str]) -> None:
        post_name = self._clean_user_profile_value(profile.get("post", ""))
        position = self._clean_user_profile_value(profile.get("position", ""))
        if not post_name or not position:
            return
        profile_summary = f"Подразделение: {post_name} · Должность: {position}"
        current_summary = normalize_message(str(ticket.get("summary", "") or ""))
        if current_summary and profile_summary not in current_summary:
            ticket["summary"] = f"{profile_summary} · {current_summary}"
        elif not current_summary:
            ticket["summary"] = profile_summary
        current_original = str(ticket.get("original_text", "") or "").strip()
        profile_block = f"Подразделение: {post_name}\nДолжность: {position}"
        if profile_block not in current_original:
            ticket["original_text"] = f"{profile_block}\n\n{current_original}".strip()


    def parse_flexible(self, text: str) -> dict[str, str] | None:
        """Parse department/position from natural WhatsApp wording without guessing blindly.

        The internal key stays "post" for storage compatibility; for users it is
        the department / unit (подразделение / бөлімше).
        """
        # Keep the old strict parser as the first/authoritative path.
        parsed = self.parse_contact_profile(text)
        if parsed:
            return parsed

        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not raw:
            return None

        lines: list[str] = []
        for raw_line in raw.split("\n"):
            value = normalize_message(raw_line)
            if value:
                lines.append(value)
        if not lines:
            value = normalize_message(raw)
            if value:
                lines = [value]
        if not lines or len(lines) > 6:
            return None

        def clean(value: str, *, cut_extra_colon: bool = False) -> str:
            result = normalize_message(value).strip(" \t:-—;,")
            if cut_extra_colon and ":" in result:
                # Example: "Начальник смены :Иванов И" -> "Начальник смены".
                result = normalize_message(result.split(":", 1)[0]).strip(" \t:-—;,")
            return result

        def valid_post(value: str) -> bool:
            value = clean(value)
            if not (2 <= len(value) <= 100):
                return False
            if re.fullmatch(r"[\d\s+()./-]+", value):
                return False
            if re.search(r"(?i)\b(?:должность|лауазым)\b", value):
                return False
            return True

        def valid_position(value: str) -> bool:
            value = clean(value, cut_extra_colon=True)
            if not (2 <= len(value) <= 120):
                return False
            if re.fullmatch(r"[\d\s+()./-]+", value):
                return False
            if re.search(r"(?i)\b(?:подразделение|отдел|филиал|бөлімше)\b", value):
                return False
            return True

        post = ""
        position = ""
        post_line_index = -1
        position_line_index = -1
        explicit_post = False
        explicit_position = False

        for index, line in enumerate(lines):
            # "Подразделение: Бухгалтерия" / "Отдел продаж" / "Бөлімше: ..."
            match = re.match(r"(?i)^\s*(?:подразделение|отдел|филиал|бөлімше)\s*[:\-—]?\s*(.+?)\s*$", line)
            if match and not post:
                candidate = clean(match.group(1))
                if valid_post(candidate):
                    post = candidate
                    post_line_index = index
                    explicit_post = True
                    continue

            # "Бухгалтерия отдел" / "Центральный филиал"
            match = re.match(r"(?i)^\s*(.+?)\s+(?:подразделение|отдел|филиал|бөлімше)\s*[:\-—]?\s*$", line)
            if match and not post:
                candidate = clean(match.group(1))
                if valid_post(candidate):
                    post = candidate
                    post_line_index = index
                    explicit_post = True
                    continue

            # "Должность: Начальник смены" / "Лауазым Начальник смены"
            match = re.match(r"(?i)^\s*(?:должность|лауазым)\s*[:\-—]?\s*(.+?)\s*$", line)
            if match and not position:
                candidate = clean(match.group(1), cut_extra_colon=True)
                if valid_position(candidate):
                    position = candidate
                    position_line_index = index
                    explicit_position = True
                    continue

            # Also allow suffix notation: "Начальник смены должность".
            match = re.match(r"(?i)^\s*(.+?)\s+(?:должность|лауазым)\s*[:\-—]?\s*$", line)
            if match and not position:
                candidate = clean(match.group(1), cut_extra_colon=True)
                if valid_position(candidate):
                    position = candidate
                    position_line_index = index
                    explicit_position = True

        # A common human answer is:
        #   "Отдел продаж"
        #   "Начальник смены"
        # The explicit department marker makes the second line safe enough to
        # treat as position, but only when there are exactly two meaningful lines.
        if post and not position and explicit_post and len(lines) == 2:
            other_index = 1 - post_line_index if post_line_index in {0, 1} else -1
            if other_index >= 0:
                candidate = clean(lines[other_index], cut_extra_colon=True)
                if (
                    valid_position(candidate)
                    and not re.search(r"(?i)\b(?:подразделение|отдел|филиал|бөлімше|должность|лауазым)\b", candidate)
                ):
                    position = candidate

        # Symmetric form:
        #   "Должность: Начальник смены"
        #   "Бухгалтерия"
        # is accepted only because the first line explicitly declares position.
        if position and not post and explicit_position and len(lines) == 2:
            other_index = 1 - position_line_index if position_line_index in {0, 1} else -1
            if other_index >= 0:
                candidate = clean(lines[other_index])
                if (
                    valid_post(candidate)
                    and not re.search(r"(?i)\b(?:подразделение|отдел|филиал|бөлімше|должность|лауазым)\b", candidate)
                ):
                    post = candidate

        if not post or not position:
            return None

        return {
            "post": post[:100],
            "position": position[:120],
        }
