# EO_GENERIC_DEAD_UI_V141
"""Tolerant labels and explicit menu numbers, without guessing identifiers."""
import re

LABEL_TYPOS = {
    'пробелма': 'проблема',
    'проблеиа': 'проблема',
    'проблма': 'проблема',
    'номре': 'номер',
    'номкр': 'номер',
}


def parse_menu_number(text):
    # Anchored: a reference number in a sentence is never a menu choice.
    match = re.fullmatch(r'\s*(?:(?:пункт|номер)\s*)?[№#]?\s*(\d{1,2})\s*[.)!]?\s*',str(text),re.I)
    return str(int(match.group(1))) if match else ''


def tolerant_labels(text):
    # Digits and alphanumeric references, including document/account IDs, are untouched.
    return re.sub(r'(?<![\w])[^\W\d_]+(?![\w])',lambda m: LABEL_TYPOS.get(m.group().casefold(),m.group()),str(text),flags=re.U)
