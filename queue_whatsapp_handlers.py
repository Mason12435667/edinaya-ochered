from __future__ import annotations

from typing import Any
import queue_dialog_flow
from http import HTTPStatus

APP_MODULE: Any = None

def bind(context: dict[str, Any], app_module: Any) -> None:
    global APP_MODULE
    APP_MODULE = app_module
    protected = {"bind", "APP_MODULE", "Any", "WhatsAppHandlerMixin"}
    for name, value in context.items():
        if name.startswith("__") or name in protected:
            continue
        globals()[name] = value


class WhatsAppHandlerMixin:
    def _inbound_json_response(self, payload, status=HTTPStatus.OK):
        # Serialize before committing; only send the response after COMMIT.
        json.dumps(payload, ensure_ascii=False)
        self._inbound_result = (payload, status)

    def _ticket_category_is_selectable(self, category: str) -> bool:
        category = str(category or "").strip()
        if not category:
            return False
        # Public v3 item 9 is intentionally not stored in legacy request_menu_json.
        # It starts an automated KEDEN check and only creates a ticket on an issue.
        if category == "check_td":
            return True
        options = enabled_menu_options()
        # The company-name form is a real ticket subcategory under the enabled BIN menu item.
        if category == str(BIN_COMPANY_NAME_CATEGORY):
            return any(str(option.get("key", "")) in {"bin", str(BIN_COMPANY_NAME_CATEGORY)} and bool(option.get("enabled", True)) for option in options)
        for option in options:
            if not bool(option.get("enabled", True)):
                continue
            if str(option.get("key", "")) != category:
                continue
            return configured_category_action(category) == MENU_ACTION_TICKET
        return False

    def _category_required_reply(self) -> dict[str, object]:
        return {
            "created": False,
            "awaiting_category": True,
            "main_menu": True,
            "force_menu": True,
            "reply": main_menu_text(error=True),
            "menu_options": menu_payload(),
        }

    # EO_FLEXIBLE_CONTACT_PROFILE_V23_20261001
    @staticmethod
    def _parse_contact_profile_flexible(text: str) -> dict[str, str] | None:
        """Parse post/position from natural WhatsApp wording without guessing blindly."""
        # Keep the old strict parser as the first/authoritative path.
        parsed = parse_contact_profile(text)
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
                # Example: "Начальник смены :Сактап Мади" -> "Начальник смены".
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
            if re.search(r"(?i)\b(?:пост|бекет)\b", value):
                return False
            return True

        post = ""
        position = ""
        post_line_index = -1
        position_line_index = -1
        explicit_post = False
        explicit_position = False

        for index, line in enumerate(lines):
            # "Пост: Темир баба" / "Пост Темир баба" / "Бекет: ..."
            match = re.match(r"(?i)^\s*(?:пост|бекет)\s*[:\-—]?\s*(.+?)\s*$", line)
            if match and not post:
                candidate = clean(match.group(1))
                if valid_post(candidate):
                    post = candidate
                    post_line_index = index
                    explicit_post = True
                    continue

            # "Темир баба пост" / "Темир баба бекет"
            match = re.match(r"(?i)^\s*(.+?)\s+(?:пост|бекет)\s*[:\-—]?\s*$", line)
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
        #   "Темир баба пост"
        #   "Начальник смены"
        # The explicit "пост/бекет" marker makes the second line safe enough to
        # treat as position, but only when there are exactly two meaningful lines.
        if post and not position and explicit_post and len(lines) == 2:
            other_index = 1 - post_line_index if post_line_index in {0, 1} else -1
            if other_index >= 0:
                candidate = clean(lines[other_index], cut_extra_colon=True)
                if (
                    valid_position(candidate)
                    and not re.search(r"(?i)\b(?:пост|бекет|должность|лауазым)\b", candidate)
                ):
                    position = candidate

        # Symmetric form:
        #   "Должность: Начальник смены"
        #   "Темир баба"
        # is accepted only because the first line explicitly declares position.
        if position and not post and explicit_position and len(lines) == 2:
            other_index = 1 - position_line_index if position_line_index in {0, 1} else -1
            if other_index >= 0:
                candidate = clean(lines[other_index])
                if (
                    valid_post(candidate)
                    and not re.search(r"(?i)\b(?:пост|бекет|должность|лауазым)\b", candidate)
                ):
                    post = candidate

        if not post or not position:
            return None

        return {
            "post": post[:100],
            "position": position[:120],
        }

    # EO_SMART_USER_INPUT_20260930
    # WhatsApp users rarely answer in a perfectly machine-shaped format.
    # Accept menu replies such as:
    #   "2. Перевозка"
    #   "2) Перевозка\n123456\nне завершается"
    #   "Проблема с перевозкой: 123456, не завершается"
    # while keeping bare long numbers from being mistaken for menu choices.
    def _smart_menu_input(self, text: str, context_mode: str) -> tuple[str, str]:
        if context_mode != MENU_CONTEXT:
            return "", ""
        raw = normalize_message(text)
        if not raw:
            return "", ""

        exact = parse_menu_number(raw)
        if exact:
            return exact, ""

        patterns = (
            r"^\s*(?:пункт|категория|санат)?\s*№?\s*([1-9])\s*[\.\)\]:\-—]+\s*(.*)$",
            r"^\s*([1-9])\s*\n+\s*(.*)$",
        )
        for pattern in patterns:
            match = re.match(pattern, raw, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1), normalize_message(match.group(2))

        # "2 перевозка ..." is accepted only when the word after the digit
        # matches the selected menu item. This avoids treating a reference
        # beginning with a digit as a category.
        match = re.match(r"^\s*([1-9])\s+(.+)$", raw, re.IGNORECASE | re.DOTALL)
        if match:
            number, remainder = match.group(1), normalize_message(match.group(2))
            category = configured_menu_category(number)
            if category and self._category_text_matches(category, remainder):
                return number, remainder

        return "", ""

    @staticmethod
    def _category_text_matches(category: str, text: str) -> bool:
        value = normalize_message(text).casefold()
        rules = {
            "seal": (r"\b(?:нп|пломб\w*|навигац\w*)\b",),
            "transport": (r"\b(?:перевоз\w*|транспорт\w*|тс\b)\b",),
            "bin_company_name": (r"\bбин\b", r"назван\w*\s+компан"),
            "keden": (r"\bкеден\b",),
            "database": (r"\b(?:доступ\w*|роль\w*|блокиров\w*|логин\w*)\b",),
            "mobile": (r"\b(?:мобил\w*|приложен\w*)\b",),
            "general": (r"\b(?:вопрос\w*|друг\w*\s+проблем\w*|ошибк\w*)\b",),
            "check_td": (r"\b(?:провер\w*\s+тд|пакет\w*\s+кеден)\b",),
        }
        if category == str(BIN_COMPANY_NAME_CATEGORY):
            category = "bin_company_name"
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in rules.get(category, ()))

    def _category_from_text(self, text: str, context_mode: str) -> tuple[str, str]:
        """Return (category, detail_text) only while the main menu is open."""
        if context_mode != MENU_CONTEXT:
            return "", ""
        raw = normalize_message(text)
        if not raw:
            return "", ""
        value = raw.casefold()

        # Specific service must be checked before generic KEDEN.
        if re.search(r"\b(?:провер\w*\s+тд|пакет\w*\s+кеден|провер\w*\s+пакет)\b", value):
            return "check_td", raw
        if re.search(r"\b(?:нп|пломб\w*|навигац\w*)\b", value):
            return "seal", raw
        if re.search(r"\b(?:перевоз\w*|оформлен\w*\s+перевоз\w*)\b", value):
            return "transport", raw
        if re.search(r"\bбин\b|назван\w*\s+компан", value):
            return str(BIN_COMPANY_NAME_CATEGORY), raw
        if re.search(r"\bкеден\b", value):
            return "keden", raw
        if re.search(r"\b(?:доступ\w*|выдат\w*\s+роль|снят\w*\s+блокиров|заблокиров\w*)\b", value):
            return "database", raw
        if re.search(r"\b(?:мобил\w*\s+приложен\w*|приложен\w*\s+transit)\b", value):
            return "mobile", raw
        if re.search(r"\b(?:вопрос\w*|другая\s+проблема|сообщить\s+об\s+ошибке)\b", value):
            return "general", raw
        return "", ""

    @staticmethod
    def _strip_category_label(category: str, text: str) -> str:
        raw = normalize_message(text)
        if not raw:
            return ""
        patterns = {
            "seal": r"^\s*(?:проблема\s+с\s+)?(?:навигационной\s+)?(?:пломбой|пломба|нп)\s*[:\-—]*\s*",
            "transport": r"^\s*(?:проблема\s+с\s+)?(?:оформлением\s+)?перевозк\w*\s*[:\-—]*\s*",
            "keden": r"^\s*(?:проблемы?\s+с\s+)?кеден\w*\s*[:\-—]*\s*",
            "database": r"^\s*(?:доступ\s+к\s+)?(?:ис\s+)?transit\s*[:\-—]*\s*",
            "mobile": r"^\s*(?:мобильное\s+)?приложение(?:\s+transit)?\s*[:\-—]*\s*",
            "general": r"^\s*(?:другая\s+проблема|вопрос|ошибка)\s*[:\-—]*\s*",
            "check_td": r"^\s*(?:проверить|проверка)?\s*(?:тд|пакеты?\s+кеден)\s*[:\-—]*\s*",
        }
        if category == str(BIN_COMPANY_NAME_CATEGORY):
            pattern = r"^\s*(?:корректировка\s+)?бин\s*[:\-—]*\s*"
        else:
            pattern = patterns.get(category, "")
        if pattern:
            return normalize_message(re.sub(pattern, "", raw, count=1, flags=re.IGNORECASE))
        return raw

    @staticmethod
    def _all_information_confirmed(text: str) -> bool:
        value = normalize_message(text).casefold().strip(" .,!?:;—-")
        compact = re.sub(r"\s+", " ", value)
        phrases = {
            "вся инфа", "вся информация", "это вся инфа", "это вся информация",
            "все данные", "это все данные", "это всё", "это все",
            "всё что есть", "все что есть", "больше данных нет",
            "данных больше нет", "другой информации нет", "больше информации нет",
            "больше ничего нет", "это вся имеющаяся информация",
            # EO_MULTIMESSAGE_DRAFT_20261001
            # Explicit draft finalization. A ticket is never created merely
            # because the parser already has enough fields.
            "готово", "готов", "создать заявку", "создай заявку",
            "отправить заявку", "отправь заявку", "завершить заявку",
            "бар ақпарат осы", "осы барлық ақпарат", "басқа ақпарат жоқ",
            "басқа мәлімет жоқ", "бар мәлімет осы",
            "дайын", "өтінімді құру", "өтінімді жіберу",
        }
        return compact in phrases

    def _remove_all_information_phrase(self, text: str) -> str:
        raw = normalize_message(text)
        if not raw:
            return ""
        lines = [line for line in raw.splitlines() if not self._all_information_confirmed(line)]
        cleaned = normalize_message("\n".join(lines))
        # collect_request may concatenate a short confirmation to the end of
        # the accumulated request without a line break.
        tails = (
            "это вся информация", "вся информация", "вся инфа", "это всё", "это все",
            "все данные", "больше данных нет", "данных больше нет",
            "больше информации нет", "больше ничего нет",
            "готово", "готов", "создать заявку", "создай заявку",
            "отправить заявку", "отправь заявку", "завершить заявку",
            "дайын", "өтінімді құру", "өтінімді жіберу",
        )
        lowered = cleaned.casefold().rstrip(" .,!?:;—-")
        for tail in tails:
            if lowered.endswith(tail):
                cleaned = normalize_message(cleaned[: len(cleaned) - len(tail)].rstrip(" .,!?:;—-"))
                break
        return cleaned

    @staticmethod
    def _looks_like_reference_only(text: str) -> bool:
        value = normalize_message(text).strip()
        if not value or len(value) > 80:
            return False
        return bool(re.fullmatch(r"[A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9_./\\-]{3,}", value))

    def handle_api_message(self) -> None:
        payload = self.read_authorized_json(18_000_000)
        if payload is None:
            return
        self._inbound_result = None
        language = queue_user_locale.current_language()
        try:
            with STORE.atomic_inbound():
                self._process_api_message(payload)
                if self._inbound_result is None:
                    raise RuntimeError("Inbound response missing")
        except Exception as error:
            self.log_error("Inbound transaction rolled back: %s", type(error).__name__)
            self.json_response({"error": "inbound_processing_failed", "retryable": True},
                               HTTPStatus.SERVICE_UNAVAILABLE)
            return
        finally:
            queue_user_locale.set_language(language)
        result, status = self._inbound_result
        self.json_response(result, status)

    def _process_api_message(self, payload) -> None:
        key = valid_chat_id(str(payload.get("chat_id", ""))) or normalize_phone(str(payload.get("phone", "")))
        text = normalize_message(str(payload.get("text", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        state = queue_dialog_flow.load(STORE, key)
        command = text.casefold().strip()

        # EO_GLOBAL_MENU_DIGITS_20260930
        # Exact menu digits are commands, not free-form text.
        # 0 always means "main menu".
        # 1..9 always select the corresponding ENABLED configured category,
        # regardless of the previous dialog/category context.
        # Long numbers (e.g. 39855502/010926/9017998) are NOT affected.
        global_menu_digit = parse_menu_number(text)
        global_menu_category = configured_menu_category(global_menu_digit) if global_menu_digit else ""
        global_menu_command = bool(
            command == "0"
            or (global_menu_digit and global_menu_category)
        )
        if global_menu_command:
            # EO_STRICT_CATEGORY_SELECTION_20260930
            # A ticket category is armed only by a separate, explicit numeric
            # menu choice. Text written before that choice is never recycled as
            # ticket details.
            state.pop("topic", None)
            state.pop("topic_attempts", None)
            state.pop("buffer", None)
            if command == "0":
                state.pop("strict_category", None)
                state.pop("strict_category_message", None)
            elif global_menu_digit and global_menu_category:
                state["strict_category"] = str(global_menu_category)
                state["strict_category_message"] = str(payload.get("external_id", "") or "")
            queue_dialog_flow.save(STORE, key, state)

            # Force the core into the main-menu namespace so a digit never gets
            # swallowed by BIN/active-ticket/follow-up submenus.
            active_for_menu = STORE.active_context_ticket(key)
            active_for_menu_id = int(active_for_menu["id"]) if active_for_menu else 0
            queue_contextual_tickets.reset_context(STORE, key, "")
            STORE.set_conversation_context(
                key,
                MENU_CONTEXT,
                active_for_menu_id,
                bool(active_for_menu),
            )
            if global_menu_digit and global_menu_category:
                payload = {**payload, "menu_choice": global_menu_digit}

        eligible = bool(key and get_contact_language(key, phone) and get_contact_profile(key, phone)
                        and not STORE.manual_whatsapp_contact(key, phone) and not STORE.manual_chat_mode(key)
                        and not queue_productivity.auto_reply_blocked(STORE, key))
        context = STORE.get_conversation_context(key) or {}
        active = STORE.active_context_ticket(key)
        intent = state.get("topic") if eligible else None
        if eligible and (intent or (active and not pending_context_category(context)
                and str(context.get("pending_category", "")) in {"", ACTIVE_TICKET_FOLLOWUP_CONTEXT}
                and queue_dialog_flow.other_topic(text, str(active.get("category", ""))))):
            mid = str(payload.get("external_id", ""))
            if mid and not STORE.claim_inbound_message(mid,key):
                self._inbound_json_response({"created":False,"duplicate":True});return
            if intent and command in {"дополнение", "қосымша"}:
                state.pop("topic",None);state.pop("topic_attempts",None);queue_dialog_flow.save(STORE,key,state)
                self._process_api_message_core({**payload, **intent, "chat_id":key, "phone":phone}, replay=True)
                return
            if intent and command in {"новая", "жаңа", "0"}:
                state.pop("topic",None);state.pop("topic_attempts",None);queue_dialog_flow.save(STORE,key,state)
                queue_dialog_flow.remember(STORE,key,intent)
                self._process_api_message_core({**payload,"text":"0"}, replay=True)
                return
            if command in {"0","00","отмена","бас тарту"}:
                state.pop("topic",None);state.pop("topic_attempts",None);queue_dialog_flow.save(STORE,key,state)
                self._process_api_message_core(payload,replay=True);return
            if not intent:
                state['topic'] = {k:payload.get(k,'') for k in ('text','attachment_name','external_id','media_mime')}
            state['topic_attempts'] = int(state.get('topic_attempts',0)) + 1
            queue_dialog_flow.save(STORE,key,state)
            if state['topic_attempts'] >= 3:
                queue_dialog_flow.review(STORE,key,'Не удалось определить: новая проблема или дополнение')
                self._inbound_json_response({"created":False,"needs_review":True,"reply":tr("Передаю диалог сотруднику для разбора.","Диалогты маманға жіберемін.")});return
            self._inbound_json_response({"created":False,"reply":queue_dialog_flow.topic_prompt()});return
        if command in {"0","отмена","бас тарту"}:
            state.pop('buffer',None);state.pop('topic',None);state.pop('topic_attempts',None)
            queue_dialog_flow.save(STORE,key,state)
        self._process_api_message_core(payload)
        result, status = self._inbound_result
        if result.get('duplicate') or result.get('silent') or result.get('manual_contact'): return
        pre_menu = any(result.get(k) for k in ('language_required','profile_required','awaiting_category','menu_gate','menu_reminder'))
        if pre_menu and command not in {'0','00','отмена','бас тарту'} and not (re.search(r'(?im)^(?:пост|бекет)\s*:', text) and re.search(r'(?im)^(?:должность|лауазым)\s*:', text)):
            if result.get('awaiting_category') or result.get('menu_gate') or result.get('menu_reminder'):
                # Strict category mode: messages before a category choice stay
                # visible in the chat but are never reused to create a ticket.
                flow_state = queue_dialog_flow.load(STORE, key)
                flow_state.pop('buffer', None)
                queue_dialog_flow.save(STORE, key, flow_state)
            else:
                queue_dialog_flow.remember(STORE, key, payload)

        if result.get('awaiting_details') and result.get('contextual'):
            # EO_MULTIMESSAGE_DRAFT_20261001
            # A draft may consist of any number of messages. Do not send it to
            # manual review merely because the user needed more than two turns.
            pass

    def _process_api_message_core(self, payload, replay=False) -> None:
        external_id = str(payload.get("external_id", ""))
        phone = normalize_phone(str(payload.get("phone", "")))
        sender = str(payload.get("sender", "")).strip() or "Неизвестный отправитель"
        text = normalize_message(str(payload.get("text", "")))
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        contact_key = chat_id or phone
        media_mime = normalize_message(str(payload.get("media_mime", "")))[:120]
        media_name = normalize_message(str(payload.get("media_name", "") or payload.get("attachment_name", "")))[:180]
        media_path = ""
        media_base64 = str(payload.get("media_base64", "") or "")
        if media_base64 and chat_id and external_id:
            media_path = save_media_payload(chat_id, external_id, media_base64, media_mime, media_name)
        if payload.get("media_receipt"):
            media_path = queue_inbound_media.resolve(MEDIA_DIR, chat_id, external_id, payload["media_receipt"]) or media_path
        if not replay and external_id and not STORE.claim_inbound_message(external_id, contact_key):
            self._inbound_json_response({"created": False, "duplicate": True})
            return

        # Дополнительная страховка для медиа: если live-sync коннектора не успел
        # сохранить фото/голосовое, API обработки заявки всё равно закрепит файл
        # за тем же сообщением в истории чата. Upsert по message id не создаёт дубль.
        if chat_id and external_id and (media_path or media_name):
            try:
                message_timestamp = max(0, int(payload.get("message_timestamp", 0) or 0))
            except (ValueError, TypeError):
                message_timestamp = 0
            STORE.save_whatsapp_chat_messages(
                chat_id,
                [
                    {
                        "id": external_id,
                        "from_me": False,
                        "sender": sender,
                        "body": text or ("[Вложение]" if media_name else ""),
                        "type": normalize_message(str(payload.get("message_type", "chat")))[:40] or "chat",
                        "timestamp": message_timestamp,
                        "ack": 0,
                        "media_path": media_path,
                        "media_mime": media_mime,
                        "media_name": media_name,
                    }
                ],
            )

        # Контакты из админки остаются полноценной личной перепиской, но без
        # заявок, меню, антиспам-ответов и других автоответов. Важно сохранять
        # входящее сообщение именно здесь: прежний ранний return в коннекторе
        # мог приводить к тому, что сообщение не попадало в панель.
        manual_contact = STORE.manual_whatsapp_contact(chat_id, phone)
        if manual_contact:
            try:
                message_timestamp = max(0, int(payload.get("message_timestamp", 0) or 0))
            except (ValueError, TypeError):
                message_timestamp = 0
            STORE.save_whatsapp_chat_messages(
                chat_id,
                [
                    {
                        "id": external_id,
                        "from_me": False,
                        "sender": str(manual_contact.get("name", "")) or sender,
                        "body": text or ("[Вложение]" if payload.get("attachment_name") else ""),
                        "type": normalize_message(str(payload.get("message_type", "chat")))[:40] or "chat",
                        "timestamp": message_timestamp,
                        "ack": 0,
                        "media_path": media_path,
                        "media_mime": media_mime,
                        "media_name": media_name,
                    }
                ],
            )
            self._inbound_json_response(
                {
                    "created": False,
                    "manual_contact": True,
                    "ignored": True,
                    "reason": "Контакт администратора",
                    "name": str(manual_contact.get("name", "")),
                }
            )
            return

        # Если сотрудник явно выключил автоответчик для этого пользователя,
        # автоматика молчит. Состояние меняется только кнопкой сотрудника и не
        # зависит от отправки сообщений вручную или системных автоответов.
        manual_mode = STORE.manual_chat_mode(chat_id) if chat_id else None
        if manual_mode:
            # EO_GLOBAL_MENU_DIGITS_20260930:
            # A bot-generated review pause must never trap the user. An exact
            # menu digit immediately returns control to the bot. A pause set by
            # a real employee remains authoritative and is NOT bypassed.
            menu_digit_now = parse_menu_number(text)
            menu_category_now = configured_menu_category(menu_digit_now) if menu_digit_now else ""
            menu_command_now = bool(
                normalize_message(text).strip() == "0"
                or (menu_digit_now and menu_category_now)
            )
            manual_actor = normalize_message(str(manual_mode.get("actor", "")))
            automatic_review_pause = manual_actor.casefold() == "бот: нужен разбор".casefold()
            if menu_command_now and automatic_review_pause and chat_id:
                STORE.disable_manual_chat_mode(chat_id, "Бот: команда меню")
                manual_mode = None
                print(
                    f"Автоматическая пауза снята командой меню {normalize_message(text).strip()}: {chat_id}",
                    flush=True,
                )

        if manual_mode:
            self._inbound_json_response(
                {
                    "created": False,
                    "ignored": True,
                    "silent": True,
                    "reason": "Ручной диалог",
                    "manual_mode": manual_mode,
                }
            )
            return

        if chat_id and queue_productivity.auto_reply_blocked(STORE, chat_id):
            self._inbound_json_response({
                "created": False,
                "ignored": True,
                "silent": True,
                "reason": "Контакт исключён из автоответов",
            })
            return

        # 1.00.6.12: ordinary users must choose Russian or Kazakh before any
        # greeting, category menu or ticket parsing. The choice persists for the
        # user and can be reopened later with «язык» / «тіл».
        language_identity = _language_identity(chat_id, phone)
        selected_language = get_contact_language(chat_id, phone)
        if selected_language:
            queue_user_locale.set_language(selected_language)

        if selected_language and (normalize_message(text).strip() == "00" or queue_user_locale.is_language_change_command(text)):
            clear_contact_language(chat_id, phone)
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, "")
            STORE.set_conversation_context(contact_key, "", 0, False)
            queue_user_locale.set_language(queue_user_locale.LANG_RU)
            self._inbound_json_response({
                "created": False,
                "language_required": True,
                "reply": language_selection_text_v3(),
            })
            return

        if not selected_language:
            language_choice = queue_user_locale.parse_language_choice(text)
            if not language_choice:
                queue_user_locale.set_language(queue_user_locale.LANG_RU)
                self._inbound_json_response({
                    "created": False,
                    "language_required": True,
                    "reply": language_selection_text_v3(),
                })
                return

            selected_language = set_contact_language(chat_id, phone, language_choice)
            queue_user_locale.set_language(selected_language)
            existing_active_ticket = STORE.active_context_ticket(contact_key)
            existing_active_ticket_id = int(existing_active_ticket["id"]) if existing_active_ticket else 0
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, "")
            contact_profile = get_contact_profile(chat_id, phone)
            if not contact_profile:
                STORE.set_conversation_context(
                    contact_key, USER_PROFILE_CONTEXT, existing_active_ticket_id, bool(existing_active_ticket)
                )
                self._inbound_json_response({
                    "created": False,
                    "language_selected": selected_language,
                    "profile_required": True,
                    "reply": profile_prompt_text(),
                })
                return
            STORE.set_conversation_context(
                contact_key, MENU_CONTEXT, existing_active_ticket_id, bool(existing_active_ticket)
            )
            self._inbound_json_response({
                "created": False,
                "language_selected": selected_language,
                "awaiting_category": True,
                "main_menu": True,
                "force_menu": True,
                "reply": main_menu_text(),
                "menu_options": menu_payload(),
            })
            return

        contact_profile = get_contact_profile(chat_id, phone)
        if not contact_profile:
            parsed_profile = self._parse_contact_profile_flexible(text)
            active_before_profile = STORE.active_context_ticket(contact_key)
            active_before_profile_id = int(active_before_profile["id"]) if active_before_profile else 0
            if not parsed_profile:
                STORE.set_conversation_context(
                    contact_key, USER_PROFILE_CONTEXT, active_before_profile_id, bool(active_before_profile)
                )
                self._inbound_json_response({
                    "created": False,
                    "profile_required": True,
                    "reply": profile_prompt_text(error=True),
                })
                return
            contact_profile = set_contact_profile(
                chat_id, phone, parsed_profile["post"], parsed_profile["position"]
            )
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, "")
            STORE.set_conversation_context(
                contact_key, MENU_CONTEXT, active_before_profile_id, bool(active_before_profile)
            )
            self._inbound_json_response({
                "created": False,
                "profile_saved": True,
                "post": contact_profile.get("post", ""),
                "position": contact_profile.get("position", ""),
                "main_menu": True,
                "force_menu": True,
                "reply": main_menu_text(),
                "menu_options": menu_payload(),
            })
            return

        context = STORE.get_conversation_context(contact_key)
        active_ticket = STORE.active_context_ticket(contact_key)
        active_ticket_id = int(active_ticket["id"]) if active_ticket else 0
        context_mode = str(context.get("pending_category", "")) if context else ""
        pending_category = pending_context_category(context)
        force_new_requested = bool(context and context.get("force_new"))
        menu_choice = normalize_message(str(payload.get("menu_choice", "")))
        typed_choice = parse_menu_number(text)

        # Strict category binding:
        # - only an exact configured menu digit can choose a ticket category;
        # - category names/keywords are NOT inferred from free-form text;
        # - "2. Перевозка ..." in one message is NOT accepted as selection+details.
        explicit_menu_choice = menu_choice if re.fullmatch(r"[1-9]", menu_choice) else ""
        typed_category_choice = typed_choice if context_mode == MENU_CONTEXT else ""
        chosen_category = configured_menu_category(explicit_menu_choice or typed_category_choice)
        inline_category_details = ""
        selected_from_menu = bool(
            chosen_category and (explicit_menu_choice or typed_category_choice)
        )

        # v3 merges questions and bug reports into item 7 (general). The old
        # separate error-report mode remains only for already-open legacy sessions.
        error_report_choice = False
        active_tickets_choice = (
            (context_mode == MENU_CONTEXT and typed_choice == str(active_tickets_menu_number()))
            or is_active_tickets_command(text)
        )
        attachment_present = bool(
            media_path or media_name or normalize_message(str(payload.get("attachment_name", "")))
        )
        # A free-form message or attachment must never create a ticket without
        # the user's explicit category selection. Keep the legacy flag false so
        # old branches below continue routing the conversation through the menu.
        direct_free_request = False

        if is_main_menu_command(text) or text.casefold().strip() in {"отмена", "бас тарту"}:
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, "")
            STORE.set_conversation_context(
                contact_key,
                MENU_CONTEXT,
                active_ticket_id,
                bool(active_ticket),
            )
            self._inbound_json_response(
                {
                    "created": False,
                    "awaiting_category": True,
                    "main_menu": True,
                    "force_menu": True,
                    "reply": main_menu_text(),
                    "menu_options": menu_payload(),
                }
            )
            return


        # Старые сессии могли остаться на MENU_GATE_CONTEXT. После появления
        # выбора языка дополнительный шаг с цифрой 1 больше не нужен: сразу
        # переводим такую сессию в основное меню.
        if context_mode == MENU_GATE_CONTEXT and not direct_free_request:
            STORE.set_conversation_context(
                contact_key, MENU_CONTEXT, active_ticket_id, bool(active_ticket)
            )
            self._inbound_json_response({
                "created": False,
                "awaiting_category": True,
                "main_menu": True,
                "force_menu": True,
                "reply": main_menu_text(),
                "menu_options": menu_payload(),
            })
            return

        if context_mode == BIN_MENU_CONTEXT:
            if is_main_menu_command(text):
                STORE.clear_conversation_draft(contact_key)
                queue_contextual_tickets.reset_context(STORE, contact_key, "")
                STORE.set_conversation_context(contact_key, MENU_CONTEXT, active_ticket_id, bool(active_ticket))
                self._inbound_json_response({
                    "created": False,
                    "awaiting_category": True,
                    "main_menu": True,
                    "force_menu": True,
                    "reply": main_menu_text(),
                    "menu_options": menu_payload(),
                })
                return
            normalized_bin_choice = normalize_message(text).casefold()
            if typed_choice == "1" or (("назван" in normalized_bin_choice or "атау" in normalized_bin_choice) and "компан" in normalized_bin_choice):
                STORE.clear_conversation_draft(contact_key)
                queue_contextual_tickets.reset_context(STORE, contact_key, BIN_COMPANY_NAME_CATEGORY)
                STORE.set_conversation_context(
                    contact_key, BIN_COMPANY_NAME_CATEGORY, active_ticket_id,
                    force_new_requested or bool(active_ticket),
                )
                self._inbound_json_response({
                    "created": False,
                    "awaiting_details": True,
                    "category": BIN_COMPANY_NAME_CATEGORY,
                    "reply": configured_category_prompt(BIN_COMPANY_NAME_CATEGORY),
                })
                return
            if typed_choice == "2" or normalized_bin_choice:
                # После показа инструкции сообщение само предлагает отправить 1
                # для изменения названия компании. Поэтому сохраняем BIN_MENU_CONTEXT:
                # следующая «1» должна открыть форму изменения названия, а не
                # восприниматься как пункт №1 главного меню (навигационная пломба).
                STORE.clear_conversation_draft(contact_key)
                queue_contextual_tickets.reset_context(STORE, contact_key, "")
                STORE.set_conversation_context(
                    contact_key, BIN_MENU_CONTEXT, active_ticket_id,
                    force_new_requested or bool(active_ticket),
                )
                self._inbound_json_response({
                    "created": False,
                    "instruction_only": True,
                    "awaiting_bin_action": True,
                    "category": "bin",
                    "reply": bin_instruction_text(),
                })
                return
            self._inbound_json_response({
                "created": False,
                "awaiting_bin_action": True,
                "reply": tr(
                    "Пожалуйста, выберите действие: отправьте 1 для изменения названия компании или 2 для получения инструкции.",
                    "Әрекетті таңдаңыз: компания атауын өзгерту үшін 1, нұсқаулық алу үшін 2 жіберіңіз.",
                ),
            })
            return

        if context_mode == ACTIVE_TICKETS_CONTEXT:
            active_rows = STORE.list_active_user_tickets(chat_id, phone, 10)
            try:
                selected_index = int(typed_choice or "0") - 1
            except (TypeError, ValueError):
                selected_index = -1
            if 0 <= selected_index < len(active_rows):
                selected_ticket = active_rows[selected_index]
                selected_id = int(selected_ticket.get("id", 0) or 0)
                STORE.set_conversation_context(
                    contact_key, ACTIVE_TICKET_FOLLOWUP_CONTEXT, selected_id, False
                )
                self._inbound_json_response({
                    "created": False,
                    "active_ticket_selected": True,
                    "ticket_id": selected_id,
                    "reply": active_ticket_selected_reply(selected_ticket),
                })
            else:
                self._inbound_json_response({
                    "created": False,
                    "active_tickets": True,
                    "reply": active_tickets_reply(active_rows),
                })
            return

        if context_mode == ACTIVE_TICKET_FOLLOWUP_CONTEXT:
            # Пользователь явно выбрал одну из своих активных заявок. Любой текст
            # или вложение теперь считается дополнением именно к ней до команды 0.
            if not active_ticket:
                active_rows = STORE.list_active_user_tickets(chat_id, phone, 10)
                STORE.set_conversation_context(contact_key, ACTIVE_TICKETS_CONTEXT, 0, False)
                self._inbound_json_response({
                    "created": False,
                    "active_tickets": True,
                    "reply": (
                        tr("Выбранная заявка уже закрыта или больше не активна.\n\n", "Таңдалған өтінім жабылған немесе енді белсенді емес.\n\n")
                        + active_tickets_reply(active_rows)
                    ),
                })
                return
            attachment_name = normalize_message(str(payload.get("attachment_name", "")))[:240]
            STORE.note_context_followup(active_ticket_id, text, attachment_name)
            queue_contextual_tickets.record_followup(
                STORE, contact_key, active_ticket_id, chat_id=chat_id,
                message_key=external_id, text=text, media_name=attachment_name,
            )
            if chat_id and external_id:
                STORE.link_whatsapp_message_to_ticket(chat_id, external_id, active_ticket_id)
            self._inbound_json_response({
                "created": False,
                "linked": True,
                "ticket_id": active_ticket_id,
                "reply": tr(
                    f"Дополнительная информация добавлена к заявке №{active_ticket_id}.",
                    f"Қосымша ақпарат №{active_ticket_id} өтінімге қосылды.",
                ),
            })
            return

        if active_tickets_choice:
            active_rows = STORE.list_active_user_tickets(chat_id, phone, 10)
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(contact_key, ACTIVE_TICKETS_CONTEXT, active_ticket_id, False)
            self._inbound_json_response({
                "created": False,
                "active_tickets": True,
                "reply": active_tickets_reply(active_rows),
            })
            return

        # EO_DRAFT_FINISH_PRIORITY_V13_20261001
        # "Готово/готово/Дайын" is a draft command, not a casual acknowledgement.
        # _all_information_confirmed() case-folds the text, so capitalization does
        # not matter. Connector also passes draft_finish for the fast path.
        draft_finish_command = bool(payload.get("draft_finish")) or self._all_information_confirmed(text)
        if is_acknowledgement(text) and not draft_finish_command:
            # «Спасибо», «рахмет», «ок», «понял» и похожие ответы не должны
            # превращаться в заявку даже если до этого остался выбранный пункт меню.
            self._inbound_json_response(
                {
                    "created": False,
                    "ignored": True,
                    "silent": True,
                    "reason": "Короткий ответ без новой заявки",
                }
            )
            return


        if context_mode == ERROR_REPORT_CONTEXT:
            description = text or (
                f"Вложение: {media_name or str(payload.get('attachment_name', ''))}"
                if (media_name or payload.get("attachment_name")) else ""
            )
            if not description:
                self._inbound_json_response({
                    "created": False,
                    "error_report": True,
                    "awaiting_details": True,
                    "reply": tr(
                        "Опишите ошибку текстом или приложите файл / скриншот.",
                        "Қатені мәтінмен сипаттаңыз немесе файл / скриншот тіркеңіз.",
                    ),
                })
                return
            report_id = STORE.create_error_report(
                sender=sender,
                phone=phone,
                chat_id=chat_id,
                external_id=external_id,
                description=description,
                attachment_name=media_name or normalize_message(str(payload.get("attachment_name", "")))[:240],
            )
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(contact_key, MENU_CONTEXT, active_ticket_id, bool(active_ticket))
            self._inbound_json_response({
                "created": False,
                "error_report_created": True,
                "error_report_id": report_id,
                "reply": tr(
                    f"Репорт об ошибке №{report_id} сохранён. Он находится отдельно от заявок.\n"
                    "Сотрудники смогут посмотреть его в разделе «Ошибки».\n\n"
                    "Чтобы открыть меню снова, отправьте 0.",
                    f"№{report_id} қате туралы хабарлама сақталды. Ол өтінімдерден бөлек орналасқан.\n"
                    "Қызметкерлер оны «Қателер» бөлімінен көре алады.\n\n"
                    "Мәзірді қайта ашу үшін 0 жіберіңіз.",
                ),
            })
            return

        if error_report_choice:
            STORE.clear_conversation_draft(contact_key)
            STORE.set_conversation_context(contact_key, ERROR_REPORT_CONTEXT, active_ticket_id, bool(active_ticket))
            self._inbound_json_response({
                "created": False,
                "error_report": True,
                "awaiting_details": True,
                "reply": error_report_prompt(),
            })
            return

        if context_mode == SUPPORT_MODE_CONTEXT:
            # В старых сессиях мог сохраниться выбор способа связи. После
            # 3.3.10 звонки для обычных пользователей больше не разрешаются:
            # любой выбор ведёт в текстовую поддержку.
            STORE.set_conversation_context(
                contact_key,
                "support",
                active_ticket_id,
                force_new_requested or bool(active_ticket),
            )
            self._inbound_json_response(
                {
                    "created": False,
                    "awaiting_details": True,
                    "category": "support",
                    "reply": configured_category_prompt("support"),
                }
            )
            return

        if (
            active_ticket
            and chosen_category == "package"
            and not context_mode
            and not selected_from_menu
        ):
            STORE.set_conversation_context(
                contact_key,
                MENU_CONTEXT,
                active_ticket_id,
                True,
            )
            self._inbound_json_response(
                {
                    "created": False,
                    "awaiting_category": True,
                    "main_menu": True,
                    "force_new": True,
                    "reply": main_menu_text(),
                    "menu_options": menu_payload(),
                }
            )
            return

        if chosen_category:
            strict_state = queue_dialog_flow.load(STORE, contact_key)
            strict_state.pop("buffer", None)
            strict_state["strict_category"] = str(chosen_category)
            strict_state["strict_category_message"] = str(external_id or "")
            queue_dialog_flow.save(STORE, contact_key, strict_state)

            if chosen_category == "bin":
                STORE.clear_conversation_draft(contact_key)
                queue_contextual_tickets.reset_context(STORE, contact_key, "")
                STORE.set_conversation_context(
                    contact_key, BIN_MENU_CONTEXT, active_ticket_id,
                    force_new_requested or bool(active_ticket),
                )
                # В варианте меню со скриншотов БИН отмечен как «только инструкция».
                # Но сама инструкция предлагает отправить 1 для изменения названия
                # компании. Поэтому оставляем специальный BIN-контекст и показываем
                # инструкцию с вариантами 1/0. В старой конфигурации сохраняем
                # привычное подменю 1/2.
                bin_is_instruction = configured_category_action(chosen_category) == MENU_ACTION_INSTRUCTION
                self._inbound_json_response({
                    "created": False,
                    "awaiting_bin_action": True,
                    "instruction_only": bin_is_instruction,
                    "category": "bin",
                    "reply": bin_instruction_text() if bin_is_instruction else bin_submenu_text(),
                })
                return
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, chosen_category)
            if configured_category_action(chosen_category) == MENU_ACTION_INSTRUCTION:
                STORE.set_conversation_context(
                    contact_key,
                    MENU_CONTEXT,
                    active_ticket_id,
                    bool(active_ticket),
                )
                self._inbound_json_response(
                    {
                        "created": False,
                        "instruction_only": True,
                        "category": chosen_category,
                        "reply": configured_category_prompt(chosen_category),
                    }
                )
                return
            if chosen_category == "support":
                STORE.set_conversation_context(
                    contact_key,
                    "support",
                    active_ticket_id,
                    force_new_requested or bool(active_ticket),
                )
                self._inbound_json_response(
                    {
                        "created": False,
                        "awaiting_details": True,
                        "category": "support",
                        "reply": configured_category_prompt("support"),
                    }
                )
                return
            STORE.set_conversation_context(
                contact_key,
                chosen_category,
                active_ticket_id,
                force_new_requested or bool(active_ticket),
            )

            # Strict mode intentionally does NOT process ticket details from
            # the same message as the category choice. The next user message is
            # the first allowed details message.

            self._inbound_json_response(
                {
                    "created": False,
                    "awaiting_details": True,
                    "category": chosen_category,
                    "draft_collecting": True,
                    "reply": (
                        configured_category_prompt(chosen_category)
                        + "\n\n"
                        + tr(
                            "Можно отправить данные несколькими сообщениями. "
                            "Я сохраню их в одной заявке. Когда закончите — напишите «Готово».",
                            "Деректерді бірнеше хабарламамен жібере аласыз. "
                            "Оларды бір өтінімге жинаймын. Аяқтағанда «Дайын» деп жазыңыз.",
                        )
                    ),
                }
            )
            return

        if context_mode == MENU_CONTEXT and not chosen_category and not direct_free_request:
            # Keep the menu open and require a valid category number before collecting details.
            response = self._category_required_reply()
            response["menu_reminder"] = True
            self._inbound_json_response(response)
            return

        # A finish command must never fall into the "silent active ticket"
        # branch while an unfinished strict draft still exists. Recover the
        # selected category from the strict marker if the conversation context
        # was temporarily lost/reset by another background event.
        if draft_finish_command and not pending_category and not chosen_category:
            finish_state = queue_dialog_flow.load(STORE, contact_key)
            remembered_category = str(finish_state.get("strict_category", "") or "").strip()
            remembered_fragments = queue_contextual_tickets.request_fragments(STORE, contact_key)
            if self._ticket_category_is_selectable(remembered_category) and remembered_fragments:
                STORE.set_conversation_context(
                    contact_key,
                    remembered_category,
                    active_ticket_id,
                    True,
                )
                pending_category = remembered_category
                context_mode = remembered_category

        # После создания заявки обычные уточнения по ней принимаются молча:
        # система не засыпает человека меню на каждую следующую фразу. Новая
        # заявка начинается только после явной команды 0/«меню» или выбора темы.
        if active_ticket and not pending_category and not chosen_category and not direct_free_request:
            attachment_name = normalize_message(str(payload.get("attachment_name", "")))[:240]
            STORE.note_context_followup(active_ticket_id, text, attachment_name)
            queue_contextual_tickets.record_followup(
                STORE, contact_key, active_ticket_id, chat_id=chat_id,
                message_key=external_id, text=text, media_name=attachment_name,
            )
            if chat_id and external_id:
                STORE.link_whatsapp_message_to_ticket(chat_id, external_id, active_ticket_id)
            self._inbound_json_response(
                {
                    "created": False,
                    "linked": True,
                    "silent": True,
                    "ticket_id": active_ticket_id,
                    "reason": "Уточнение к активной заявке",
                }
            )
            return

        forced_category = pending_category
        strict_state = queue_dialog_flow.load(STORE, contact_key)
        strict_selected_category = str(strict_state.get("strict_category", "") or "").strip()

        if forced_category and strict_selected_category != str(forced_category):
            # Old/stale/inferred contexts are not trusted. Force a fresh explicit
            # menu selection before any ticket details can be collected.
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, "")
            strict_state.pop("strict_category", None)
            strict_state.pop("strict_category_message", None)
            strict_state.pop("buffer", None)
            queue_dialog_flow.save(STORE, contact_key, strict_state)
            STORE.set_conversation_context(
                contact_key, MENU_CONTEXT, active_ticket_id,
                force_new_requested or bool(active_ticket),
            )
            response = self._category_required_reply()
            response["strict_category_required"] = True
            self._inbound_json_response(response)
            return

        if not self._ticket_category_is_selectable(forced_category):
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, "")
            STORE.set_conversation_context(
                contact_key, MENU_CONTEXT, active_ticket_id,
                force_new_requested or bool(active_ticket),
            )
            self._inbound_json_response(self._category_required_reply())
            return
        combined_request_text = text
        contextual_decision = None
        audio_direct_request = bool(
            forced_category
            and forced_category != BIN_COMPANY_NAME_CATEGORY
            and not text
            and media_mime.startswith("audio/")
        )
        # EO_MULTIMESSAGE_DRAFT_20261001
        draft_finalized = False
        if forced_category and not direct_free_request:
            collect_text = text
            # A single identifier on a follow-up message is common in WhatsApp.
            if (
                forced_category in {"seal", "transport", "keden", "check_td"}
                and self._looks_like_reference_only(collect_text)
            ):
                collect_text = f"Номер: {collect_text}"

            finish_requested = bool(payload.get("draft_finish")) or self._all_information_confirmed(text)
            contextual_decision = queue_contextual_tickets.collect_request(
                STORE, contact_key, forced_category, collect_text,
                attachment_name=normalize_message(str(payload.get("attachment_name", "")))[:240],
                message_key=external_id, chat_id=chat_id, active_ticket_id=0,
                required_fields_override=configured_category_required_fields(forced_category),
            )

            # KEDEN package check is a service action, not a normal ticket draft:
            # as soon as its reference is available it can be queued as before.
            if forced_category != "check_td":
                raw_fragments = queue_contextual_tickets.request_fragments(STORE, contact_key)
                draft_fragments = [
                    normalize_message(str(fragment))
                    for fragment in raw_fragments
                    if normalize_message(str(fragment))
                    and not self._all_information_confirmed(str(fragment))
                ]
                accumulated = normalize_message("\n".join(draft_fragments))
                if not accumulated:
                    accumulated = self._remove_all_information_phrase(
                        str(contextual_decision.get("request_text") or "")
                    )

                if not finish_requested:
                    # Even if all required fields are already present, keep the
                    # request as a draft until the user explicitly says "Готово".
                    missing = contextual_decision.get("missing", [])
                    if contextual_decision.get("ready"):
                        reply = tr(
                            "✅ Данные сохранены. Можете отправить ещё информацию, фото или документы. "
                            "Когда закончите — напишите «Готово».",
                            "✅ Деректер сақталды. Қосымша ақпарат, фото немесе құжат жібере аласыз. "
                            "Аяқтағанда «Дайын» деп жазыңыз.",
                        )
                    else:
                        missing_reply = (
                            contextual_decision.get("reply")
                            or v3_missing_fields_reply(missing)
                        )
                        reply = tr(
                            "✅ Данные сохранены.",
                            "✅ Деректер сақталды.",
                        )
                        if missing_reply:
                            reply += "\n" + str(missing_reply)
                        reply += "\n" + tr(
                            "Можно продолжать отправлять данные отдельными сообщениями. "
                            "Когда закончите — «Готово».",
                            "Деректерді бөлек хабарламалармен жібере беріңіз. "
                            "Аяқтағанда — «Дайын».",
                        )

                    self._inbound_json_response(
                        {
                            "created": False,
                            "awaiting_details": True,
                            "draft_collecting": True,
                            "draft_ready": bool(contextual_decision.get("ready")),
                            "category": forced_category,
                            "missing_fields": missing,
                            "contextual": True,
                            "confidence": contextual_decision.get("confidence", {}),
                            "clarify": bool(contextual_decision.get("clarify")),
                            "reply": reply,
                        }
                    )
                    return

                # "Готово / Вся инфа / Дайын" is the only normal-ticket commit.
                # If there is still some information in the draft, respect the
                # user's explicit confirmation and create one ticket from all
                # accumulated messages.
                if not accumulated:
                    self._inbound_json_response(
                        {
                            "created": False,
                            "awaiting_details": True,
                            "draft_collecting": True,
                            "category": forced_category,
                            "reply": tr(
                                "Сначала отправьте данные по обращению, затем напишите «Готово».",
                                "Алдымен өтінім бойынша деректерді жіберіңіз, содан кейін «Дайын» деп жазыңыз.",
                            ),
                        }
                    )
                    return

                contextual_decision = {
                    **contextual_decision,
                    "ready": True,
                    "missing": [],
                    "clarify": False,
                    "request_text": accumulated,
                    "user_confirmed_all_info": True,
                }
                combined_request_text = accumulated
                draft_finalized = True
            else:
                # Preserve the existing check_td behavior.
                if not contextual_decision.get("ready"):
                    self._inbound_json_response(
                        {
                            "created": False,
                            "awaiting_details": True,
                            "category": forced_category,
                            "missing_fields": contextual_decision.get("missing", []),
                            "contextual": True,
                            "confidence": contextual_decision.get("confidence", {}),
                            "clarify": bool(contextual_decision.get("clarify")),
                            "reply": contextual_decision.get("reply") or v3_missing_fields_reply(contextual_decision.get("missing", [])),
                        }
                    )
                    return
                combined_request_text = str(contextual_decision.get("request_text") or text)

        if not forced_category:
            # Первое сообщение обычного пользователя только предлагает открыть меню.
            # Контакты из админки отсекаются выше и автоответов не получают.
            STORE.set_conversation_context(
                contact_key, MENU_GATE_CONTEXT, active_ticket_id, bool(active_ticket)
            )
            self._inbound_json_response(
                {
                    "created": False,
                    "menu_gate": True,
                    "force_menu": True,
                    "reply": start_menu_prompt(),
                }
            )
            return

        # 1.00.6.139: «Проверить ТД» is an asynchronous service action, not a
        # ticket by itself. The specialist ticket is created only when KEDEN
        # returns an error / bad set_shipment status, or when automation itself
        # fails and manual verification is required.
        if forced_category == "check_td":
            reference = queue_keden_checks.extract_reference(combined_request_text)
            if not reference:
                self._inbound_json_response({
                    "created": False,
                    "awaiting_details": True,
                    "category": "check_td",
                    "missing_fields": ["reference"],
                    "reply": tr(
                        "Укажите номер ТД или перевозки для проверки пакетов Кеден.\n*(0 — главное меню)*",
                        "КЕДЕН пакеттерін тексеру үшін ТД немесе тасымалдау нөмірін көрсетіңіз.\n*(0 — негізгі мәзір)*",
                    ),
                })
                return
            job = queue_keden_checks.enqueue(
                STORE,
                chat_id=chat_id, phone=phone, sender=sender, external_id=external_id,
                reference=reference, source_text=combined_request_text,
                language=get_contact_language(chat_id, phone) or "ru",
            )
            STORE.clear_conversation_draft(contact_key)
            queue_contextual_tickets.reset_context(STORE, contact_key, "")
            strict_state = queue_dialog_flow.load(STORE, contact_key)
            strict_state.pop("strict_category", None)
            strict_state.pop("strict_category_message", None)
            strict_state.pop("buffer", None)
            queue_dialog_flow.save(STORE, contact_key, strict_state)
            STORE.set_conversation_context(
                contact_key, MENU_CONTEXT, active_ticket_id, bool(active_ticket)
            )
            self._inbound_json_response({
                "created": False,
                "keden_check_queued": True,
                "keden_job_id": int(job.get("id", 0) or 0),
                "reference": reference,
                "reply": tr(
                    f"Проверяю ТД {reference} в Пакетах Кеден. Результат пришлю сюда автоматически.",
                    f"{reference} ТД бойынша КЕДЕН пакеттерін тексеріп жатырмын. Нәтижені осы чатқа автоматты түрде жіберемін.",
                ),
            })
            return

        result = process_incoming_message(
            sender,
            phone,
            combined_request_text,
            str(payload.get("attachment_name", "")),
            chat_id,
            external_id,
            forced_category,
        )
        if result["created"]:
            if not self._ticket_category_is_selectable(forced_category):
                STORE.set_conversation_context(
                    contact_key, MENU_CONTEXT, active_ticket_id,
                    force_new_requested or bool(active_ticket),
                )
                self._inbound_json_response(self._category_required_reply())
                return
            fragments = queue_contextual_tickets.request_fragments(STORE, contact_key)
            if fragments and not draft_finalized:
                result["ticket"]["original_text"] = str(result["ticket"].get("original_text", "")) + "\n\nСообщения пользователя:\n" + "\n".join(fragments)
            result["ticket"]["assigned_to"] = active_employee()
            apply_contact_profile_to_ticket(result["ticket"], contact_profile)
            similar = STORE.find_similar_open_ticket(result["ticket"], hours=72)
            if similar:
                similar_id = int(similar.get("id", 0) or 0)
                queue_contextual_tickets.commit_ticket(
                    STORE, contact_key, similar_id, forced_category,
                    message_key=external_id, source="WhatsApp · дубль",
                )
                if chat_id and external_id:
                    STORE.link_whatsapp_message_to_ticket(chat_id, external_id, similar_id)
                STORE.clear_conversation_draft(contact_key)
                strict_state = queue_dialog_flow.load(STORE, contact_key)
                strict_state.pop("strict_category", None)
                strict_state.pop("strict_category_message", None)
                strict_state.pop("buffer", None)
                queue_dialog_flow.save(STORE, contact_key, strict_state)
                STORE.set_conversation_context(
                    contact_key, MENU_CONTEXT, similar_id, False
                )
                self._inbound_json_response(
                    {
                        "created": False,
                        "duplicate_ticket": True,
                        "ticket_id": similar_id,
                        "reply": v3_duplicate_reply(similar_id),
                    }
                )
                return
            ticket_id = STORE.create_ticket(result["ticket"])
            queue_contextual_tickets.commit_ticket(
                STORE, contact_key, ticket_id, forced_category,
                message_key=external_id, source="WhatsApp",
            )
            STORE.clear_conversation_draft(contact_key)
            strict_state = queue_dialog_flow.load(STORE, contact_key)
            strict_state.pop("strict_category", None)
            strict_state.pop("strict_category_message", None)
            strict_state.pop("buffer", None)
            queue_dialog_flow.save(STORE, contact_key, strict_state)
            STORE.set_conversation_context(contact_key, "", ticket_id)
            if chat_id and external_id:
                STORE.link_whatsapp_message_to_ticket(chat_id, external_id, ticket_id)
            # Пользователи иногда отправляют скриншот до финального текста заявки.
            # С 1.00.6.5 автоматически привязываются только изображения, которые
            # уже попали в контекст ТЕКУЩЕЙ заявки после выбора её категории.
            # Просто недавние изображения из этого чата больше не подтягиваются.
            try:
                ticket_message_timestamp = max(0, int(payload.get("message_timestamp", 0) or 0))
            except (TypeError, ValueError):
                ticket_message_timestamp = 0
            pre_ticket_images = STORE.link_recent_whatsapp_images_to_ticket(
                chat_id,
                ticket_id,
                before_timestamp=ticket_message_timestamp,
                lookback_seconds=PRE_TICKET_IMAGE_LOOKBACK_SECONDS,
                limit=5,
            ) if chat_id else []
            self._inbound_json_response(
                {
                    "created": True,
                    "ticket_id": ticket_id,
                    "pre_ticket_images": len(pre_ticket_images),
                    "category": result["ticket"]["category"],
                    "title": result["ticket"]["title"],
                    "reply": (
                        support_question_accepted_reply(ticket_id)
                        if result["ticket"]["category"] == "support"
                        else ticket_accepted_reply(ticket_id)
                    ),
                },
                HTTPStatus.CREATED,
            )
            return
        STORE.set_conversation_context(
            contact_key,
            "bin",
            active_ticket_id,
            force_new_requested and pending_category == "bin",
        )
        error_id = STORE.add_template_error(sender, phone, result["missing"], result["reply"])
        self._inbound_json_response(
            {
                "created": False,
                "template_error_id": error_id,
                "missing": result["missing"],
                "reply": result["reply"],
            }
        )

    def handle_template_error_delivery(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            error_id = int(payload.get("template_error_id", 0))
        except (ValueError, TypeError):
            error_id = 0
        status = str(payload.get("status", ""))
        if error_id <= 0 or status not in {"sent", "failed"}:
            self.json_response({"error": "invalid_delivery"}, HTTPStatus.BAD_REQUEST)
            return
        STORE.update_template_error_delivery(
            error_id,
            status,
            str(payload.get("provider_id", "")),
            str(payload.get("error", ""))[:500],
        )
        self.json_response({"updated": True})

    def handle_outbound_claim(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            created_after_epoch = int(payload.get("created_after", 0))
        except (ValueError, TypeError):
            created_after_epoch = 0
        created_after = ""
        if 0 < created_after_epoch <= int(datetime.now(timezone.utc).timestamp()) + 60:
            created_after = datetime.fromtimestamp(
                created_after_epoch,
                timezone.utc,
            ).replace(microsecond=0).isoformat()
        try:
            wait_ms = max(0, min(20000, int(payload.get("wait_ms", 0) or 0)))
        except (ValueError, TypeError):
            wait_ms = 0
        outbound_revision = queue_realtime.outbound_revision()
        message = STORE.claim_outbound_message(created_after)
        if not message and wait_ms:
            queue_realtime.wait_for_outbound(outbound_revision, wait_ms / 1000.0)
            message = STORE.claim_outbound_message(created_after)
        if not message:
            self.json_response({"message": None})
            return
        # Preserve the original upload before WhatsApp can consume it.
        if message.get("media_path"):
            try:
                preserved = queue_uploads.preserve_sent_attachment(APP_MODULE, message["id"])
                if not preserved: raise OSError("Не удалось сохранить локальную копию вложения")
            except OSError as error:
                STORE.complete_outbound_message(message["id"], False, str(error))
                self.json_response({"message": None, "error": str(error)[:180]})
                return
        try:
            mentions = json.loads(str(message.get("mentions_json", "[]") or "[]"))
            if not isinstance(mentions, list):
                mentions = []
        except (TypeError, ValueError, json.JSONDecodeError):
            mentions = []
        reply_key = normalize_message(str(message.get("reply_to_key", "")))[:160]
        reply_message = STORE.get_whatsapp_message(valid_conversation_id(str(message.get("chat_id", ""))), reply_key) if reply_key else None
        reply_preview_body = ""
        reply_preview_sender = ""
        if reply_message:
            reply_preview_body = normalize_message(str(reply_message.get("body", "") or reply_message.get("media_name", "") or "Вложение"))[:1200]
            reply_preview_sender = "Вы" if bool(reply_message.get("from_me")) else normalize_message(str(reply_message.get("sender", "") or "Пользователь"))[:100]
        self.json_response(
            {
                "message": {
                    "id": message["id"],
                    "ticket_id": message["ticket_id"],
                    "chat_id": message["chat_id"],
                    "phone": normalize_phone(message["phone"]),
                    "body": message["body"],
                    "mentions": mentions[:100],
                    "reply_to_key": message.get("reply_to_key", ""),
                    "reply_preview_body": reply_preview_body,
                    "reply_preview_sender": reply_preview_sender,
                    "media_path": message.get("media_path", ""),
                    "media_mime": message.get("media_mime", ""),
                    "media_name": message.get("media_name", ""),
                }
            }
        )

    def handle_outbound_result(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            message_id = int(payload.get("message_id", 0))
        except (ValueError, TypeError):
            message_id = 0
        sent = payload.get("status") == "sent"
        if message_id <= 0 or payload.get("status") not in {"sent", "failed", "uncertain"}:
            self.json_response({"error": "invalid_result"}, HTTPStatus.BAD_REQUEST)
            return
        provider_id = normalize_message(str(payload.get("provider_id", "")))[:180]
        if payload.get('status') == 'uncertain':
            updated = STORE.mark_outbound_uncertain(message_id, str(payload.get('error', '')))
            self.json_response({'updated': bool(updated), 'queue_status': str((updated or {}).get('status', ''))})
            return
        sent_media = queue_uploads.preserve_sent_attachment(APP_MODULE,message_id) if sent else {}
        updated = STORE.complete_outbound_message(
            message_id,
            sent,
            str(payload.get("error", ""))[:500],
            provider_id,
        )
        # Сразу сохраняем успешное исходящее сообщение в истории. Это делает
        # ответ с цитатой видимым в нашей системе даже если whatsapp-web.js
        # вернул sentMessage без заполненного hasQuotedMsg и live-sync догонит позже.
        if sent and updated and provider_id:
            chat_id = valid_conversation_id(str(updated.get("chat_id", "")))
            if chat_id:
                reply_key = normalize_message(str(payload.get("reply_to_key", "") or updated.get("reply_to_key", "")))[:160]
                quoted = STORE.get_whatsapp_message(chat_id, reply_key) if reply_key else None
                quoted_body = normalize_message(str(payload.get("quoted_body", "")))[:1200]
                quoted_sender = normalize_message(str(payload.get("quoted_sender", "")))[:100]
                if quoted:
                    if not quoted_body:
                        quoted_body = normalize_message(str(quoted.get("body", "") or quoted.get("media_name", "") or "Вложение"))[:1200]
                    if not quoted_sender:
                        quoted_sender = "Вы" if bool(quoted.get("from_me")) else normalize_message(str(quoted.get("sender", "") or "Пользователь"))[:100]
                if reply_key and not quoted_body:
                    quoted_body = "Сообщение"
                if reply_key and not quoted_sender:
                    quoted_sender = "Сообщение"
                STORE.save_whatsapp_chat_messages(chat_id, [{
                    "id": provider_id,
                    "from_me": True,
                    "sender": normalize_message(str(updated.get("actor", "") or "Вы"))[:100],
                    "body": normalize_message(str(updated.get("body", "")))[:32000],
                    "type": "chat",
                    "timestamp": int(datetime.now(timezone.utc).timestamp()),
                    "ack": 1,
                    "quoted_message_key": reply_key,
                    "quoted_body": quoted_body,
                    "quoted_sender": quoted_sender,
                    **sent_media,
                }])
        if updated:
            queue_realtime.notify("chat")
        self.json_response({"updated": bool(updated), "queue_status": str((updated or {}).get("status", ""))})

    def handle_chat_list_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        cleaned: list[dict[str, object]] = []
        raw_chats = payload.get("chats")
        if isinstance(raw_chats, list):
            for item in raw_chats[:100]:
                if not isinstance(item, dict):
                    continue
                chat_id = valid_chat_id(str(item.get("id", "")))
                if not chat_id:
                    continue
                # QUEUE_3_3_95_CHAT_LIST_CANONICAL
                chat_id = STORE.canonical_whatsapp_chat_id(chat_id)
                try:
                    timestamp = int(item.get("timestamp", 0))
                    unread = max(0, int(item.get("unread_count", 0)))
                except (ValueError, TypeError):
                    timestamp, unread = 0, 0
                cleaned.append(
                    {
                        "id": chat_id,
                        "name": normalize_message(str(item.get("name", "")))[:100]
                        or chat_id.split("@")[0],
                        "last_message": normalize_message(
                            str(item.get("last_message", ""))
                        )[:160],
                        "timestamp": timestamp,
                        "unread_count": unread,
                        "last_from_me": bool(item.get("last_from_me")),
                    }
                )
        # QUEUE_3_3_95_CHAT_LIST_DEDUPE
        deduped_chats: dict[str, dict[str, object]] = {}
        for row in cleaned:
            cid = str(row.get("id", ""))
            if not cid:
                continue
            previous = deduped_chats.get(cid)
            if previous is None:
                deduped_chats[cid] = row
                continue
            prev_ts = int(previous.get("timestamp", 0) or 0)
            row_ts = int(row.get("timestamp", 0) or 0)
            newer, older = (row, previous) if row_ts >= prev_ts else (previous, row)
            merged = dict(older)
            merged.update(newer)
            merged["unread_count"] = max(int(previous.get("unread_count", 0) or 0), int(row.get("unread_count", 0) or 0))
            # Preserve a useful name if the newer technical row has only digits.
            newer_name = str(merged.get("name", "") or "").strip()
            older_name = str(older.get("name", "") or "").strip()
            if (not newer_name or newer_name.replace("+", "").replace(" ", "").isdigit()) and older_name:
                merged["name"] = older_name
            deduped_chats[cid] = merged
        cleaned = list(deduped_chats.values())

        with CHAT_LOCK:
            CHAT_STATE["connected"] = bool(payload.get("connected", True))
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
            if cleaned or isinstance(raw_chats, list):
                CHAT_STATE["chats"] = cleaned
        STORE.upsert_whatsapp_chats(cleaned)
        if isinstance(raw_chats, list):
            queue_realtime.notify("chat-list")
        self.json_response({"updated": True, "chats": len(cleaned)})

    def handle_presence_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        rows = payload.get("items", [])
        if not isinstance(rows, list):
            rows = []
        cleaned: dict[str, dict[str, object]] = {}
        now = datetime.now(timezone.utc).isoformat()
        for row in rows[:80]:
            if not isinstance(row, dict):
                continue
            chat_id = valid_chat_id(str(row.get("chat_id", "") or ""))
            if not chat_id or chat_id.endswith("@g.us"):
                continue
            cleaned[chat_id] = {
                "known": bool(row.get("known")),
                "online": bool(row.get("online")),
                "state": str(row.get("state", "") or "")[:32],
                "updated_at": now,
            }
        with CHAT_LOCK:
            presence_map = CHAT_STATE.get("presence")
            if not isinstance(presence_map, dict):
                presence_map = {}
                CHAT_STATE["presence"] = presence_map
            presence_map.update(cleaned)
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "items": len(cleaned)})

    def handle_group_participants_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_group_id(str(payload.get("chat_id", "")))
        raw = payload.get("participants", [])
        if not chat_id or not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, object]] = []
        for item in raw[:300]:
            if not isinstance(item, dict):
                continue
            mention_id = valid_chat_id(str(item.get("mention_id", "")))
            if not mention_id:
                continue
            resolved_id = valid_chat_id(str(item.get("resolved_id", "")))
            cleaned.append({
                "mention_id": mention_id,
                "resolved_id": resolved_id,
                "name": normalize_message(str(item.get("name", "")))[:100] or mention_id.split("@")[0],
                "phone": normalize_phone(str(item.get("phone", ""))),
                "is_admin": bool(item.get("is_admin")),
                "is_me": bool(item.get("is_me")),
            })
        with CHAT_LOCK:
            participants_map = CHAT_STATE.get("group_participants")
            if not isinstance(participants_map, dict):
                participants_map = {}
                CHAT_STATE["group_participants"] = participants_map
            participants_map[chat_id] = cleaned
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "participants": len(cleaned)})

    def handle_group_message_identities_sync(self) -> None:
        payload = self.read_authorized_json(2_000_000)
        if payload is None:
            return
        chat_id = valid_group_id(str(payload.get("chat_id", "")))
        raw = payload.get("identities", [])
        if not chat_id or not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, str]] = []
        generic = {"", "участник", "участник группы", "пользователь whatsapp"}
        for item in raw[:100]:
            if not isinstance(item, dict):
                continue
            message_id = normalize_message(str(item.get("id", "")))[:160]
            if not message_id:
                continue
            sender = normalize_message(str(item.get("sender", "")))[:100]
            sender_phone = normalize_phone(str(item.get("sender_phone", "")))
            sender_id = valid_chat_id(str(item.get("sender_id", "")))
            cleaned.append({
                "id": message_id,
                "sender": sender,
                "sender_phone": sender_phone,
                "sender_id": sender_id,
            })
        updated = STORE.update_whatsapp_group_message_identities(chat_id, cleaned)

        # Apply the same enrichment to the in-memory copy so the open group
        # changes immediately, without requiring a page reload or DB roundtrip.
        by_id = {item["id"]: item for item in cleaned}
        with CHAT_LOCK:
            messages_map = CHAT_STATE.get("messages", {})
            messages = messages_map.get(chat_id, []) if isinstance(messages_map, dict) else []
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    patch = by_id.get(str(message.get("id", "")))
                    if not patch:
                        continue
                    current_sender = normalize_message(str(message.get("sender", "")))[:100]
                    current_folded = current_sender.casefold()
                    new_sender = patch.get("sender", "")
                    new_folded = new_sender.casefold()
                    current_generic = current_folded in generic or bool(re.fullmatch(r"[+\d\s().-]+", current_sender or ""))
                    new_useful = bool(new_sender) and new_folded not in generic and not bool(re.fullmatch(r"[+\d\s().-]+", new_sender))
                    if current_generic and new_useful:
                        message["sender"] = new_sender
                    if patch.get("sender_phone"):
                        message["sender_phone"] = patch["sender_phone"]
                    if patch.get("sender_id"):
                        message["sender_id"] = patch["sender_id"]
                CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True, "messages": updated})

    # EO_LIVE_CHAT_ALIAS_COLLAPSE_V26_20261001
    @staticmethod
    def _collapse_live_whatsapp_aliases_v26(
        live_rows: object,
        authoritative_names: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        """Canonicalize @lid rows already present in CHAT_STATE and merge duplicates."""
        if not isinstance(live_rows, list):
            return [], 0

        authoritative_names = authoritative_names or {}
        merged: dict[str, dict[str, object]] = {}
        removed = 0

        def useful_name(value: object) -> bool:
            text = str(value or "").strip()
            if not text:
                return False
            folded = text.casefold()
            if folded in {
                "пользователь whatsapp",
                "неизвестный отправитель",
                "direct",
                "рабочий whatsapp",
            }:
                return False
            compact = re.sub(r"[\s+().-]+", "", text)
            return not compact.isdigit()

        for raw_row in live_rows:
            if not isinstance(raw_row, dict):
                continue
            raw_id = valid_chat_id(str(raw_row.get("id", "")))
            if not raw_id:
                continue

            canonical_id = STORE.canonical_whatsapp_chat_id(raw_id) or raw_id
            row = dict(raw_row)
            row["id"] = canonical_id

            known_name = str(authoritative_names.get(canonical_id, "") or "").strip()
            if known_name:
                row["name"] = known_name

            previous = merged.get(canonical_id)
            if previous is None:
                merged[canonical_id] = row
                if canonical_id != raw_id:
                    removed += 1
                continue

            removed += 1
            try:
                previous_ts = int(previous.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                previous_ts = 0
            try:
                row_ts = int(row.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                row_ts = 0

            newer, older = (row, previous) if row_ts >= previous_ts else (previous, row)
            combined = dict(older)
            combined.update(newer)
            combined["id"] = canonical_id

            # Preserve the best contact name even if the newer row is a
            # technical phone/placeholder row generated by a system reply.
            preferred_name = str(authoritative_names.get(canonical_id, "") or "").strip()
            if preferred_name:
                combined["name"] = preferred_name
            else:
                newer_name = str(newer.get("name", "") or "").strip()
                older_name = str(older.get("name", "") or "").strip()
                if not useful_name(newer_name) and useful_name(older_name):
                    combined["name"] = older_name

            for counter in ("unread_count", "mention_unread_count"):
                try:
                    combined[counter] = max(
                        int(previous.get(counter, 0) or 0),
                        int(row.get(counter, 0) or 0),
                    )
                except (TypeError, ValueError):
                    pass

            # Boolean UI metadata should survive either side.
            for flag in ("favorite", "muted", "needs_reply"):
                if flag in previous or flag in row:
                    combined[flag] = bool(previous.get(flag)) or bool(row.get(flag))

            # Preserve a useful avatar/profile field when only one alias had it.
            for field in (
                "avatar_url",
                "profile_pic_url",
                "phone",
                "last_sender",
                "last_sender_avatar_url",
            ):
                if not combined.get(field):
                    combined[field] = previous.get(field) or row.get(field) or ""

            merged[canonical_id] = combined

        rows = list(merged.values())
        rows.sort(key=lambda item: int(item.get("timestamp", 0) or 0), reverse=True)
        return rows, removed

    def handle_contact_list_sync(self) -> None:
        payload = self.read_authorized_json(4_000_000)
        if payload is None:
            return
        raw = payload.get("contacts", [])
        if not isinstance(raw, list):
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        cleaned = []
        for item in raw[:1000]:
            if not isinstance(item, dict):
                continue
            chat_id = valid_chat_id(str(item.get("chat_id", "")))
            phone = normalize_phone(str(item.get("phone", "")))
            if not chat_id and phone:
                chat_id = f"{phone.lstrip('+')}@c.us"
            if not chat_id:
                continue
            raw_id = valid_chat_id(str(item.get("raw_id", "")))
            cleaned.append({"chat_id": chat_id, "raw_id": raw_id, "phone": phone, "name": normalize_message(str(item.get("name", "")))[:100], "saved": bool(item.get("saved"))})
            if raw_id.endswith("@lid") and chat_id.endswith("@c.us") and raw_id != chat_id:
                STORE.remember_whatsapp_chat_alias(raw_id, chat_id)
        authoritative_names: dict[str, str] = {}
        for item in cleaned:
            display_name = whatsapp_display_name(item.get("name", ""))
            chat_key = valid_chat_id(str(item.get("chat_id", "")))
            phone = normalize_phone(str(item.get("phone", "")))
            if display_name and chat_key:
                authoritative_names[chat_key] = display_name
                STORE.update_whatsapp_contact_display_name(chat_key, display_name, phone)
            if display_name and phone:
                authoritative_names[f"{phone.lstrip('+')}@c.us"] = display_name
        collapsed_count_v26 = 0
        with CHAT_LOCK:
            CHAT_STATE["discovered_contacts"] = cleaned
            live = CHAT_STATE.get("chats", [])
            collapsed_live_v26, collapsed_count_v26 = self._collapse_live_whatsapp_aliases_v26(
                live,
                authoritative_names,
            )
            CHAT_STATE["chats"] = collapsed_live_v26
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        if collapsed_count_v26:
            queue_realtime.notify("chat-list")
            print(
                f"WhatsApp contact sync merged duplicate live chats: {collapsed_count_v26}",
                flush=True,
            )
        self.json_response({
            "updated": True,
            "contacts": len(cleaned),
            "collapsed_live_chats": collapsed_count_v26,
        })

    def handle_contact_profile_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        if not chat_id:
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        chat_id = STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        profile = {"chat_id": chat_id, "name": normalize_message(str(payload.get("name", "")))[:100], "phone": normalize_phone(str(payload.get("phone", ""))), "about": normalize_message(str(payload.get("about", "")))[:500], "profile_pic_url": str(payload.get("profile_pic_url", ""))[:2000], "is_business": bool(payload.get("is_business"))}
        display_name = whatsapp_display_name(profile.get("name", ""))
        if display_name:
            STORE.update_whatsapp_contact_display_name(chat_id, display_name, str(profile.get("phone", "")))
        with CHAT_LOCK:
            profiles = CHAT_STATE.get("contact_profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                CHAT_STATE["contact_profiles"] = profiles
            profiles[chat_id] = profile
            if display_name:
                live = CHAT_STATE.get("chats", [])
                if isinstance(live, list):
                    for chat in live:
                        if isinstance(chat, dict) and valid_chat_id(str(chat.get("id", ""))) == chat_id:
                            chat["name"] = display_name
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_group_refresh_check(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        self.json_response({"request": STORE.get_setting("group_refresh_request", "")})

    def handle_group_list_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        cleaned: list[dict[str, object]] = []
        raw_groups = payload.get("groups", [])
        if not isinstance(raw_groups, list):
            self.json_response({"error": "invalid_groups"}, HTTPStatus.BAD_REQUEST)
            return
        for item in raw_groups[:500]:
            if not isinstance(item, dict):
                continue
            chat_id = valid_group_id(str(item.get("id", "")))
            if not chat_id:
                continue
            try:
                participant_count = max(0, int(item.get("participant_count", 0) or 0))
            except (TypeError, ValueError):
                participant_count = 0
            cleaned.append(
                {
                    "id": chat_id,
                    "name": normalize_message(str(item.get("name", "")))[:100]
                    or "Группа WhatsApp",
                    "participant_count": participant_count,
                }
            )
        diagnostics = payload.get("diagnostics", {})
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        status = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "groups": len(cleaned),
            "chats_ok": bool(diagnostics.get("chats_ok")),
            "chats_total": int(diagnostics.get("chats_total", 0) or 0),
            "chats_groups": int(diagnostics.get("chats_groups", 0) or 0),
            "contacts_ok": bool(diagnostics.get("contacts_ok")),
            "contacts_total": int(diagnostics.get("contacts_total", 0) or 0),
            "contacts_groups": int(diagnostics.get("contacts_groups", 0) or 0),
            "enriched": int(diagnostics.get("enriched", 0) or 0),
            "errors": [str(item)[:240] for item in diagnostics.get("errors", [])[:4]]
            if isinstance(diagnostics.get("errors", []), list)
            else [],
        }
        STORE.set_setting("group_sync_status", json.dumps(status, ensure_ascii=False))
        STORE.replace_whatsapp_groups(cleaned)
        self.json_response({"updated": True, "groups": len(cleaned)})

    def handle_connector_state(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        status = normalize_message(str(payload.get("status", "offline"))).casefold()
        if status not in {"offline", "connecting", "qr", "ready"}:
            self.json_response({"error": "invalid_state"}, HTTPStatus.BAD_REQUEST)
            return
        qr_data_url = str(payload.get("qr_data_url", ""))
        if qr_data_url and (
            not qr_data_url.startswith("data:image/png;base64,")
            or len(qr_data_url) > 1_000_000
        ):
            self.json_response({"error": "invalid_qr"}, HTTPStatus.BAD_REQUEST)
            return
        with CHAT_LOCK:
            CHAT_STATE["connector_status"] = status
            CHAT_STATE["connected"] = status == "ready"
            CHAT_STATE["qr_data_url"] = qr_data_url if status == "qr" else ""
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.json_response({"updated": True})

    def handle_contact_policy(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        canonical_chat_id = STORE.canonical_whatsapp_chat_id(chat_id) or chat_id
        contact = STORE.manual_whatsapp_contact(canonical_chat_id, phone)
        # EO_BOT_HARD_RESET_20260930:
        # The connector consumes this generation token on the next inbound
        # message and clears only this user's in-memory bot caches.
        reset_token = STORE.get_setting(f"bot_hard_reset_v1:{canonical_chat_id}", "")
        if not reset_token and chat_id and chat_id != canonical_chat_id:
            reset_token = STORE.get_setting(f"bot_hard_reset_v1:{chat_id}", "")
        self.json_response(
            {
                "manual_contact": bool(contact),
                "name": str((contact or {}).get("name", "")),
                "allow_calls": bool(contact),
                "bot_reset_token": str(reset_token or ""),
            }
        )

    def handle_call_permission(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_chat_id(str(payload.get("chat_id", "")))
        phone = normalize_phone(str(payload.get("phone", "")))
        language = get_contact_language(chat_id, phone)
        if STORE.is_manual_whatsapp_contact(chat_id, phone):
            self.json_response({"allowed": True, "manual_contact": True, "language": language})
            return
        # Обычным пользователям звонки всегда запрещены. Старое одноразовое
        # разрешение (если осталось от версии с пунктом «Позвонить») просто
        # поглощаем и не используем.
        if chat_id:
            STORE.consume_whatsapp_call_permission(chat_id)
        self.json_response({"allowed": False, "manual_contact": False, "language": language})

    def handle_chat_messages_sync(self) -> None:
        payload = self.read_authorized_json(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        source_chat_id = valid_conversation_id(str(payload.get("source_chat_id", "")))
        # QUEUE_3_3_95_ALIAS_HINT
        if source_chat_id.endswith("@lid") and chat_id.endswith("@c.us") and source_chat_id != chat_id:
            try:
                STORE.remember_whatsapp_chat_alias(source_chat_id, chat_id)
                # EO_LIVE_CHAT_ALIAS_COLLAPSE_V26_20261001
                collapsed_count_v26 = 0
                with CHAT_LOCK:
                    collapsed_v26, collapsed_count_v26 = self._collapse_live_whatsapp_aliases_v26(
                        CHAT_STATE.get("chats", [])
                    )
                    if collapsed_count_v26:
                        CHAT_STATE["chats"] = collapsed_v26
                        CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
                if collapsed_count_v26:
                    queue_realtime.notify("chat-list")
                    print(
                        f"WhatsApp live duplicate merged: {source_chat_id} -> {chat_id}; "
                        f"removed={collapsed_count_v26}",
                        flush=True,
                    )
            except Exception as exc:
                print(f"WhatsApp alias merge warning: {source_chat_id} -> {chat_id}: {exc}", flush=True)
        if chat_id:
            chat_id = STORE.canonical_whatsapp_chat_id(chat_id)
        raw_messages = payload.get("messages", [])
        if not chat_id or not isinstance(raw_messages, list):
            self.json_response({"error": "invalid_chat"}, HTTPStatus.BAD_REQUEST)
            return
        cleaned: list[dict[str, object]] = []
        local_media_recovered: list[str] = []
        deleted_payloads: list[tuple[str, str, int, bool]] = []
        for item in raw_messages[-80:]:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = int(item.get("timestamp", 0))
                ack = max(0, min(4, int(item.get("ack", 0) or 0)))
            except (ValueError, TypeError):
                timestamp, ack = 0, 0
            message_id = normalize_message(str(item.get("id", "")))[:160]
            is_deleted = bool(item.get("deleted"))
            if is_deleted and message_id:
                old_media_path = ""
                try:
                    stored_message = STORE.get_whatsapp_message(chat_id, message_id)
                    if stored_message:
                        old_media_path = str(stored_message.get("media_path", "") or "")
                except Exception:
                    old_media_path = ""
                deleted_payloads.append((message_id, old_media_path, timestamp, bool(item.get("from_me"))))
            try:
                outbound_message_id = max(0, int(item.get("outbound_message_id", 0) or 0))
            except (ValueError, TypeError):
                outbound_message_id = 0
            media_mime = normalize_message(str(item.get("media_mime", "")))[:120]
            media_name = normalize_message(str(item.get("media_name", "")))[:180]
            media_path = ""
            media_base64 = str(item.get("media_base64", "") or "")
            if media_base64 and message_id and not is_deleted:
                media_path = save_media_payload(
                    chat_id, message_id, media_base64, media_mime, media_name
                )
            if item.get("media_receipt") and not is_deleted:
                media_path = queue_inbound_media.resolve(MEDIA_DIR, chat_id, message_id, item["media_receipt"]) or media_path
            # Strongest path for our own UI sends: the connector supplies the
            # exact outbound queue id after WhatsApp has accepted the message.
            # preserve_sent_attachment is idempotent and returns the already
            # preserved /media/outbound-N.bin even after the temporary upload
            # was deleted from the outbound queue.
            if bool(item.get("from_me")) and outbound_message_id > 0 and not media_path and not is_deleted:
                preserved_by_queue = queue_uploads.preserve_sent_attachment(APP_MODULE, outbound_message_id)
                if preserved_by_queue:
                    media_path = str(preserved_by_queue.get("media_path", ""))
                    media_mime = media_mime or str(preserved_by_queue.get("media_mime", ""))
                    media_name = media_name or str(preserved_by_queue.get("media_name", ""))
            # A message sent from this web UI may be echoed by WhatsApp as a
            # caption-only observation.  Reattach the copy preserved when the
            # outbound queue completed, so our own system shows the same media
            # that is visible in WhatsApp.
            if bool(item.get("from_me")) and message_id and not media_path and not is_deleted:
                preserved_media = preserved_system_outgoing_media(
                    message_id,
                    str(item.get("body", "")),
                    str(item.get("quoted_message_key", "")),
                    str(item.get("type", "")),
                )
                if preserved_media:
                    media_path = str(preserved_media.get("media_path", ""))
                    media_mime = media_mime or str(preserved_media.get("media_mime", ""))
                    media_name = media_name or str(preserved_media.get("media_name", ""))
            if bool(item.get("from_me")) and message_id and not media_path and not is_deleted:
                recovered_media = reconcile_uncertain_system_outgoing_media(
                    chat_id,
                    message_id,
                    timestamp,
                    str(item.get("body", "")),
                    str(item.get("type", "")),
                    str(item.get("quoted_message_key", "")),
                )
                if not recovered_media:
                    recovered_media = queue_uploads.recover_clipboard_attachment(
                        APP_MODULE,
                        chat_id,
                        message_id,
                        timestamp,
                        str(item.get("body", "")),
                        str(item.get("type", "")),
                    )
                if recovered_media:
                    media_path = str(recovered_media.get("media_path", ""))
                    media_mime = media_mime or str(recovered_media.get("media_mime", ""))
                    media_name = media_name or str(recovered_media.get("media_name", ""))
                    local_media_recovered.append(message_id)
            raw_mentions = item.get("mentions", [])
            if not isinstance(raw_mentions, list):
                raw_mentions = []
            cleaned.append(
                {
                    "id": message_id,
                    "from_me": bool(item.get("from_me")),
                    "sender": normalize_message(str(item.get("sender", "")))[:100],
                    "sender_phone": normalize_phone(str(item.get("sender_phone", ""))),
                    "sender_id": valid_chat_id(str(item.get("sender_id", ""))),
                    "body": normalize_message(str(item.get("body", "")))[:32000],
                    "type": normalize_message(str(item.get("type", "chat")))[:40],
                    "timestamp": timestamp,
                    "ack": ack,
                    "notify": bool(item.get("notify")),
                    "media_path": media_path,
                    "media_mime": media_mime,
                    "media_name": media_name,
                    "transcript": normalize_message(str(item.get("transcript", "")))[:12000] if media_mime.startswith("audio/") else "",
                    "mentions": raw_mentions[:100],
                    "quoted_message_key": normalize_message(str(item.get("quoted_message_key", "")))[:160],
                    "quoted_body": normalize_message(str(item.get("quoted_body", "")))[:1200],
                    "quoted_sender": normalize_message(str(item.get("quoted_sender", "")))[:100],
                    "forwarded": bool(item.get("forwarded")),
                    "edited": bool(item.get("edited")),
                    "edit_timestamp": max(0, int(item.get("edit_timestamp", 0) or 0)),
                    "deleted": is_deleted,
                    "reactions": item.get("reactions", {}) if isinstance(item.get("reactions", {}), (dict, list)) else {},
                }
            )
        # 3.3.117: reconcile the revoke event with the row that is already
        # visible in our history before touching live state or persisting a tombstone.
        matched_deleted_keys: set[str] = set()
        for deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me in deleted_payloads:
            resolved_key, matched_count = scrub_deleted_whatsapp_message(
                chat_id, deleted_message_id, deleted_media_path, deleted_timestamp, deleted_from_me
            )
            if matched_count > 0 and resolved_key:
                matched_deleted_keys.add(resolved_key)
                for cleaned_item in cleaned:
                    if bool(cleaned_item.get("deleted")) and same_whatsapp_message_identity(
                        str(cleaned_item.get("id", "")), deleted_message_id
                    ):
                        cleaned_item["id"] = resolved_key

        with CHAT_LOCK:
            messages_by_chat = CHAT_STATE.get("messages")
            if not isinstance(messages_by_chat, dict):
                messages_by_chat = {}
                CHAT_STATE["messages"] = messages_by_chat
            if payload.get("append"):
                combined = [
                    *list(messages_by_chat.get(chat_id, [])),
                    *cleaned,
                ]
                unique: dict[str, dict[str, object]] = {}
                for item in combined:
                    message_id = str(item.get("id", ""))
                    key = message_id or (
                        f"{int(item.get('timestamp', 0) or 0)}:"
                        f"{int(bool(item.get('from_me')))}:{item.get('body', '')}"
                    )
                    unique[key] = item
                messages_by_chat[chat_id] = sorted(
                    unique.values(),
                    key=lambda item: int(item.get("timestamp", 0) or 0),
                )[-80:]
            else:
                messages_by_chat[chat_id] = cleaned
            if len(messages_by_chat) > 20:
                for old_chat_id in list(messages_by_chat)[:-20]:
                    messages_by_chat.pop(old_chat_id, None)
            CHAT_STATE["connected"] = True
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Only live append events are persisted. Historical messages from the
        # linked WhatsApp account are deliberately never imported.
        if payload.get("append"):
            # If the original DB row was found, it was already scrubbed in place.
            # Do not insert a second "Сообщение удалено" row under a different
            # @lid/@c.us alias. Only persist a tombstone when no original exists.
            persist_items = [
                item for item in cleaned
                if not bool(item.get("deleted")) or str(item.get("id", "")) not in matched_deleted_keys
            ]
            if persist_items:
                STORE.save_whatsapp_chat_messages(chat_id, persist_items)
            workflow_items = [item for item in cleaned if not bool(item.get("deleted"))]
            try:
                if workflow_items:
                    queue_workflow.auto_link_open_ticket(STORE, chat_id, workflow_items)
            except Exception as exc:
                print(f"Автопривязка сообщения к заявке пропущена: {exc}", flush=True)
            # 1.00.6.6: голосовые сохраняются и воспроизводятся как обычные медиа.
            # Задания на нейросетевую расшифровку больше не создаются.
            # Важно: исходящее сообщение, в том числе системный автоответ, не
            # должно автоматически выключать автоответчик. Состояние меняет
            # только сотрудник через /chat-mode. Старое поле
            # enable_manual_mode намеренно игнорируется для совместимости с
            # коннектором предыдущей версии.
        if payload.get("append") and cleaned:
            queue_realtime.notify("chat")
        self.json_response({
            "updated": True,
            "messages": len(cleaned),
            "local_media_recovered": list(dict.fromkeys(local_media_recovered)),
        })

    def handle_chat_message_ack(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        message_id = normalize_message(str(payload.get("message_id", "")))[:160]
        try:
            ack = max(0, min(4, int(payload.get("ack", 0))))
        except (ValueError, TypeError):
            ack = -1
        if not chat_id or not message_id or ack < 0:
            self.json_response({"error": "invalid_ack"}, HTTPStatus.BAD_REQUEST)
            return
        updated = STORE.update_whatsapp_message_ack(chat_id, message_id, ack)
        with CHAT_LOCK:
            messages_by_chat = CHAT_STATE.get("messages", {})
            if isinstance(messages_by_chat, dict):
                for messages in messages_by_chat.values():
                    if not isinstance(messages, list):
                        continue
                    for item in messages:
                        if isinstance(item, dict) and item.get("id") == message_id:
                            item["ack"] = max(int(item.get("ack", 0) or 0), ack)
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        if updated:
            queue_realtime.notify("chat")
        self.json_response({"updated": updated, "ack": ack})

    def handle_chat_reaction_sync(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        message_id = normalize_message(str(payload.get("message_id", "")))[:160]
        sender_id = normalize_message(str(payload.get("sender_id", "")))[:160]
        emoji = str(payload.get("emoji", "") or "")[:32]
        from_me = bool(payload.get("from_me"))
        if not chat_id or not message_id or (not from_me and not sender_id):
            self.json_response({"updated": False, "error": "invalid_reaction"}, HTTPStatus.BAD_REQUEST)
            return
        updated = STORE.update_whatsapp_message_reaction(
            chat_id, message_id, sender_id or "__me__", emoji, from_me=from_me
        )
        message = STORE.get_whatsapp_message(chat_id, message_id) if updated else None
        reactions = STORE._reaction_summary((message or {}).get("reactions_json", "{}"))
        with CHAT_LOCK:
            messages_by_chat = CHAT_STATE.get("messages", {})
            if isinstance(messages_by_chat, dict):
                live = messages_by_chat.get(chat_id, [])
                if isinstance(live, list):
                    for item in live:
                        if isinstance(item, dict) and str(item.get("id", "")) == message_id:
                            item["reactions"] = reactions
                            break
            CHAT_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        if updated:
            queue_realtime.notify("chat")
        self.json_response({"updated": updated, "reactions": reactions})

    def handle_chat_history(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        cursor = normalize_message(query.get("cursor", [""])[0])[:80]
        if not chat_id:
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Не выбран чат"}, HTTPStatus.BAD_REQUEST)
            return
        page = STORE.list_saved_whatsapp_messages_page(chat_id, 50, cursor)
        messages = decorate_chat_messages(chat_id, dedupe_whatsapp_messages(list(page.get("messages", []))))
        self.json_response({
            "messages": messages,
            "has_more": bool(page.get("has_more")),
            "cursor": str(page.get("cursor", "")),
        })

    def handle_message_window(self, query: dict[str, list[str]]) -> None:
        """Return a history page containing the message referenced by a quote.

        WhatsApp Web does not always expose the same identifier for a quoted
        message that whatsapp-web.js exposes for the original message.  Some
        builds return only stanzaId.  Therefore we first try the exact id and,
        if that fails, resolve the original by quoted text/sender immediately
        before the reply.
        """
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        requested_id = normalize_message(query.get("message_id", [""])[0])[:160]
        quoted_body = normalize_message(query.get("quoted_body", [""])[0])[:1200]
        quoted_sender = normalize_message(query.get("quoted_sender", [""])[0])[:100]
        try:
            before_ts = max(0, int(query.get("before_ts", ["0"])[0] or 0))
        except (TypeError, ValueError):
            before_ts = 0
        if not chat_id or not (requested_id or quoted_body):
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Не выбран чат или сообщение"}, HTTPStatus.BAD_REQUEST)
            return

        target = STORE.resolve_whatsapp_message(chat_id, requested_id)
        resolved_id = str(target["message_key"]) if target else ""
        found_page = None

        if not target or bool(target.get("deleted")):
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Сообщение недоступно"}, HTTPStatus.NOT_FOUND)
            return

        if found_page is not None:
            page = found_page
            raw_messages = list(page.get("messages", []))
        else:
            try:
                target_ts = int(target.get("message_timestamp") or target.get("timestamp") or 0)
                target_row = int(target.get("id") or target.get("history_row_id") or 0)
            except (TypeError, ValueError):
                target_ts = target_row = 0
            cursor_before_target = f"{target_ts}:{target_row + 1}" if target_row > 0 else ""
            page = STORE.list_saved_whatsapp_messages_page(chat_id, 100, cursor_before_target)
            raw_messages = list(page.get("messages", []))
            if not any(str(item.get("id", "")) == resolved_id for item in raw_messages):
                raw_messages = []
                cursor = ""
                for _ in range(50):
                    candidate = STORE.list_saved_whatsapp_messages_page(chat_id, 100, cursor)
                    batch = list(candidate.get("messages", []))
                    if any(str(item.get("id", "")) == resolved_id for item in batch):
                        raw_messages = batch
                        page = candidate
                        break
                    if not candidate.get("has_more") or not candidate.get("cursor"):
                        break
                    cursor = str(candidate.get("cursor", ""))
        if not raw_messages:
            self.json_response({"messages": [], "has_more": False, "cursor": "", "error": "Сообщение не найдено в истории"}, HTTPStatus.NOT_FOUND)
            return
        messages = decorate_chat_messages(chat_id, dedupe_whatsapp_messages(raw_messages))
        self.json_response({
            "messages": messages,
            "has_more": bool(page.get("has_more")),
            "cursor": str(page.get("cursor", "")),
            "target_id": resolved_id,
        })

    def handle_chat_media(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        message_id = normalize_message(query.get("message_id", [""])[0])[:160]
        message = STORE.get_whatsapp_message(chat_id, message_id) if chat_id and message_id else None
        if not message or bool(message.get("deleted")):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        media_path = str(message.get("media_path", "") or "")
        if not media_path:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            path = Path(media_path).resolve()
            media_root = MEDIA_DIR.resolve()
            if media_root not in path.parents or not path.is_file():
                raise ValueError
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = str(message.get("media_mime", "") or "application/octet-stream")
        download = query.get("download", [""])[0] == "1"
        media_name = str(message.get("media_name", "") or "Вложение")
        # Active documents must not execute in the application's origin.
        mime = queue_uploads.safe_mime(content_type)
        previewable = mime.startswith(('audio/','video/')) or mime in {
            'image/jpeg','image/png','image/webp','image/gif','application/pdf','text/plain'
        }
        disposition = content_disposition_header("attachment" if download or not previewable else "inline", media_name)
        queue_uploads.serve_range(self, path, content_type, disposition)

    def handle_chat_media_send(self) -> None:
        payload = self.read_json_body(18_000_000)
        if payload is None:
            return
        chat_id = valid_conversation_id(str(payload.get("chat_id", "")))
        if not self.claim_conversation_write(chat_id):
            return
        body = normalize_message(str(payload.get("message", "")))[:1000]
        reply_to = normalize_message(str(payload.get("reply_to", "")))[:160]
        mimetype = normalize_message(str(payload.get("mimetype", "")))[:120]
        filename = normalize_message(str(payload.get("filename", "")))[:180] or "Вложение"
        media_base64 = str(payload.get("media_base64", "") or "")
        if not chat_id or not media_base64:
            self.json_response({"queued": False, "error": "Не выбран чат или файл"}, HTTPStatus.BAD_REQUEST)
            return
        valid_upload, upload_error = queue_reliability.upload_validation(filename, mimetype, len(media_base64), MAX_MEDIA_BYTES)
        if not valid_upload:
            self.json_response({"queued": False, "error": upload_error}, HTTPStatus.BAD_REQUEST)
            return
        media_path = save_outbound_media(media_base64, mimetype, filename)
        if not media_path:
            self.json_response({"queued": False, "error": queue_performance.friendly_error(f"Не удалось сохранить вложение. Проверьте файл и лимит {MAX_MEDIA_BYTES // (1024*1024)} МБ")}, HTTPStatus.BAD_REQUEST)
            return
        mentions = payload.get("mentions", [])
        if not isinstance(mentions, list):
            mentions = []
        message_id = STORE.queue_direct_message(
            chat_id, body, work_actor(), [str(x) for x in mentions[:100]],
            reply_to_key=reply_to, media_path=media_path, media_mime=mimetype, media_name=filename,
        )
        if not message_id:
            try: Path(media_path).unlink()
            except OSError: pass
        else:
            queue_realtime.notify_outbound()
        self.json_response({"queued": bool(message_id), "message_id": message_id}, HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST)

    def handle_chat_media_send_raw(self, query: dict[str, list[str]]) -> None:
        chat_id = valid_conversation_id(query.get("chat_id", [""])[0])
        if not self.claim_conversation_write(chat_id):
            return
        body = normalize_message(query.get("message", [""])[0])[:1000]
        reply_to = normalize_message(query.get("reply_to", [""])[0])[:160]
        mimetype = normalize_message(query.get("mimetype", [""])[0])[:120] or normalize_message(self.headers.get("Content-Type", ""))[:120]
        filename = normalize_message(query.get("filename", [""])[0])[:180] or "Вложение"
        raw_mentions = query.get("mentions", [""])[0]
        mentions = [
            item.strip() for item in raw_mentions.split(",")
            if valid_conversation_id(item.strip()) or valid_group_id(item.strip())
        ][:100]
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if not chat_id or content_length <= 0:
            self.json_response({"queued": False, "error": "Не выбран чат или файл"}, HTTPStatus.BAD_REQUEST)
            return
        if content_length > MAX_MEDIA_BYTES:
            self.json_response(
                {"queued": False, "error": f"Файл превышает лимит {MAX_MEDIA_BYTES // (1024*1024)} МБ"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        # Existing validation expects the encoded payload size. Keep the same
        # safety check while streaming the actual bytes directly to disk.
        encoded_length = ((content_length + 2) // 3) * 4
        valid_upload, upload_error = queue_reliability.upload_validation(filename, mimetype, encoded_length, MAX_MEDIA_BYTES)
        if not valid_upload:
            self.json_response({"queued": False, "error": upload_error}, HTTPStatus.BAD_REQUEST)
            return
        media_path = save_outbound_media_stream(self, content_length, mimetype, filename)
        if not media_path:
            self.json_response({"queued": False, "error": "Не удалось сохранить вложение"}, HTTPStatus.BAD_REQUEST)
            return
        message_id = STORE.queue_direct_message(
            chat_id, body, work_actor(), mentions,
            reply_to_key=reply_to, media_path=media_path, media_mime=mimetype, media_name=filename,
        )
        if not message_id:
            try:
                Path(media_path).unlink()
            except OSError:
                pass
        else:
            queue_realtime.notify_outbound()
        self.json_response(
            {"queued": bool(message_id), "message_id": message_id},
            HTTPStatus.OK if message_id else HTTPStatus.BAD_REQUEST,
        )

    def handle_chat_forward(self) -> None:
        payload = self.read_json_body(250_000)
        if payload is None:
            return
        source_chat = valid_conversation_id(str(payload.get("source_chat_id", "")))
        target_chat = valid_conversation_id(str(payload.get("target_chat_id", "")))
        if target_chat and not self.claim_conversation_write(target_chat):
            return
        raw_ids = payload.get("message_ids", [])
        if not source_chat or not target_chat or not isinstance(raw_ids, list):
            self.json_response({"queued": False, "error": "Некорректные данные пересылки"}, HTTPStatus.BAD_REQUEST)
            return
        ids: list[str] = []
        for value in raw_ids[:20]:
            mid = normalize_message(str(value))[:160]
            if mid and mid not in ids and STORE.get_whatsapp_message(source_chat, mid):
                ids.append(mid)
        queued = []
        action_ids: list[int] = []
        action_message_ids: list[str] = []
        blocked_mentions = 0
        for mid in ids:
            message = STORE.get_whatsapp_message(source_chat, mid) or {}
            if mention_only_message(message):
                blocked_mentions += 1
                continue
            payload = json.dumps(
                {
                    "target_chat_id": target_chat,
                    "body": str(message.get("body", "") or ""),
                    "timestamp": int(message.get("message_timestamp", 0) or 0),
                    "media_name": str(message.get("media_name", "") or ""),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            action_id = STORE.queue_whatsapp_action("forward", source_chat, mid, payload, work_actor())
            if action_id:
                queued.append(action_id)
                action_ids.append(int(action_id))
                action_message_ids.append(mid)
        if queued:
            queue_realtime.notify_outbound()
        if not queued and blocked_mentions:
            self.json_response(
                {"queued": False, "count": 0, "blocked_count": blocked_mentions, "error": "Нельзя пересылать сообщение, состоящее только из тега пользователя"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        self.json_response(
            {
                "queued": bool(queued), "count": len(queued), "blocked_count": blocked_mentions,
                "action_ids": action_ids, "action_message_ids": action_message_ids,
            },
            HTTPStatus.OK if queued else HTTPStatus.BAD_REQUEST,
        )

    def handle_whatsapp_action_claim(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        action = STORE.claim_whatsapp_action()
        self.json_response({"action": action})

    def handle_whatsapp_action_result(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        try:
            action_id = int(payload.get("action_id", 0) or 0)
        except (ValueError, TypeError):
            action_id = 0
        status = str(payload.get("status", ""))
        if action_id <= 0 or status not in {"sent", "failed"}:
            self.json_response({"updated": False}, HTTPStatus.BAD_REQUEST)
            return
        action = STORE.complete_whatsapp_action(
            action_id, status == "sent", str(payload.get("error", ""))[:500]
        )
        if action and status == "sent":
            chat_id = str(action.get("chat_id", ""))
            message_key = str(action.get("message_key", ""))
            action_type = str(action.get("action_type", ""))
            with CHAT_LOCK:
                messages_by_chat = CHAT_STATE.get("messages", {})
                if isinstance(messages_by_chat, dict):
                    live = messages_by_chat.get(chat_id, [])
                    if isinstance(live, list):
                        for item in live:
                            if not isinstance(item, dict) or str(item.get("id", "")) != message_key:
                                continue
                            if action_type == "delete":
                                item["deleted"] = True
                                item["body"] = ""
                                item["media_path"] = ""
                                item["media_url"] = ""
                            elif action_type == "edit":
                                item["body"] = str(action.get("body", ""))
                                item["edited"] = True
                            elif action_type == "react":
                                stored = STORE.get_whatsapp_message(chat_id, message_key) or {}
                                item["reactions"] = STORE._reaction_summary(stored.get("reactions_json", "{}"))
                            break
        if action:
            queue_realtime.notify("chat")
        self.json_response({
            "updated": bool(action),
            "sent": status == "sent",
            "error": str((action or {}).get("error", "")),
        })

    def handle_chat_control(self) -> None:
        payload = self.read_authorized_json()
        if payload is None:
            return
        with CHAT_LOCK:
            requested = str(CHAT_STATE.get("requested_chat_id", ""))
            requested_group = str(CHAT_STATE.get("requested_group_id", ""))
        unresolved_group_message_ids: list[str] = []
        if requested_group.endswith("@g.us"):
            generic = {"", "участник", "участник группы", "пользователь whatsapp", "direct"}
            try:
                saved = STORE.list_saved_whatsapp_messages(requested_group, limit=80)
                for item in reversed(saved):
                    if bool(item.get("from_me")):
                        continue
                    sender = str(item.get("sender", "") or "").strip().casefold()
                    phone = str(item.get("sender_phone", "") or "").strip()
                    sender_id = str(item.get("sender_id", "") or "").strip()
                    if sender in generic or not phone or not sender_id:
                        message_id = str(item.get("id", "") or "").strip()
                        if message_id:
                            unresolved_group_message_ids.append(message_id)
                    if len(unresolved_group_message_ids) >= 30:
                        break
            except Exception:
                unresolved_group_message_ids = []
        quote_probe_ids: list[str] = []
        quote_chat_id = requested_group if requested_group.endswith("@g.us") else requested
        if quote_chat_id:
            try:
                for item in reversed(STORE.list_saved_whatsapp_messages(quote_chat_id, limit=30)):
                    if bool(item.get("deleted")):
                        continue
                    message_id = str(item.get("id", "") or "").strip()
                    if message_id and message_id not in quote_probe_ids:
                        quote_probe_ids.append(message_id)
                    if len(quote_probe_ids) >= 20:
                        break
            except Exception:
                quote_probe_ids = []
        presence_ids: list[str] = []
        if requested and not requested.endswith("@g.us"):
            presence_ids.append(requested)
        if requested_group.endswith("@g.us"):
            with CHAT_LOCK:
                participants_map = CHAT_STATE.get("group_participants", {})
                current_participants = list(participants_map.get(requested_group, [])) if isinstance(participants_map, dict) else []
            for participant in current_participants[:16]:
                if not isinstance(participant, dict) or participant.get("is_me"):
                    continue
                candidate = valid_chat_id(str(participant.get("resolved_id", "") or participant.get("mention_id", "") or ""))
                if candidate and not candidate.endswith("@g.us") and candidate not in presence_ids:
                    presence_ids.append(candidate)
        self.json_response({
            "read_chat_ids": [key for key, at in list(READ_MARKED_AT.items()) if time.monotonic() - at < 5],
            "requested_chat_id": requested,
            "requested_group_id": requested_group,
            "presence_ids": presence_ids[:16],
            "unresolved_group_message_ids": unresolved_group_message_ids,
            "quote_probe_chat_id": quote_chat_id,
            "quote_probe_message_ids": quote_probe_ids,
        })
