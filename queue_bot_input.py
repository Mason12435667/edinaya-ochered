"""Pure parsing of explicit draft-completion commands."""
import re
from queue_core import normalize_message


def all_information_confirmed(text: str) -> bool:
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

def remove_all_information_phrase(text: str) -> str:
    raw = normalize_message(text)
    if not raw:
        return ""
    lines = [line for line in raw.splitlines() if not all_information_confirmed(line)]
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
