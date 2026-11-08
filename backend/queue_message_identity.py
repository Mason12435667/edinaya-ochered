"""Exact WhatsApp identity matching. Never infer identity from message text."""
import re

def parts(value):
    value = str(value or "").strip()
    match = re.fullmatch(r"(true|false)_([^_]+)_(.+)", value)
    return (match.group(1), match.group(3)) if match else ("", value)

def same(left, right):
    a, b = str(left or "").strip(), str(right or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    ad, aid = parts(a)
    bd, bid = parts(b)
    return aid == bid and len(aid) >= 8 and (not ad or not bd or ad == bd)

def media_label(item):
    kind = str(item.get("message_type") or item.get("type") or "")
    mime = str(item.get("media_mime") or "")
    name = str(item.get("media_name") or "").strip()
    if kind in ("audio", "ptt") or mime.startswith("audio/"): return "Голосовое сообщение"
    if kind == "image" or mime.startswith("image/"): return "Фото"
    if kind == "video" or mime.startswith("video/"): return "Видео"
    if mime == "application/pdf" or name.lower().endswith(".pdf"): return "PDF: " + (name or "документ")
    if name: return "Файл: " + name
    return "Вложение"
