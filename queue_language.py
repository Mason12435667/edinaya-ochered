"""Tolerant labels and explicit menu numbers, without guessing identifiers."""
import re

LABEL_TYPOS = {
    'пломда':'пломба', 'пломбв':'пломба', 'пломбыы':'пломбы',
    'первозка':'перевозка', 'первозки':'перевозки', 'перевзока':'перевозка',
    'перевоска':'перевозка', 'перевоски':'перевозки',
    'покет':'пакет', 'покета':'пакета', 'пакеи':'пакет',
    'пробелма':'проблема', 'проблеиа':'проблема', 'проблма':'проблема',
    'номре':'номер', 'номкр':'номер',
    'старыи':'старый', 'старй':'старый', 'новй':'новый', 'новыи':'новый',
    'компнаия':'компания', 'компаниия':'компания', 'странаа':'страна',
}


def parse_menu_number(text):
    # Anchored: a declaration/reference number in a sentence is never a menu choice.
    match = re.fullmatch(r'\s*(?:(?:пункт|номер)\s*)?[№#]?\s*(\d{1,2})\s*[.)!]?\s*',str(text),re.I)
    return str(int(match.group(1))) if match else ''


def tolerant_labels(text):
    # Digits and alphanumeric references, including BIN/vehicle IDs, are untouched.
    return re.sub(r'(?<![\w])[^\W\d_]+(?![\w])',lambda m: LABEL_TYPOS.get(m.group().casefold(),m.group()),str(text),flags=re.U)
