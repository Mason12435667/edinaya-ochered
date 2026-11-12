"""Выгрузка переписки чата в TXT, ZIP с вложениями и PDF (через reportlab или собственный писатель PDF)."""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from queue_core import log_suppressed
from queue_workflow import clean, html_escape


def export_messages(store, chat_id: str) -> tuple[str, list[dict[str,Any]]]:
    chat_id = store.canonical_whatsapp_chat_id(clean(chat_id,120))
    with store.connection() as db:
        name_row = db.execute(
            """SELECT COALESCE(NULLIF(c.name,''),NULLIF(g.name,''),NULLIF(p.name,''),?) name
               FROM (SELECT 1) x LEFT JOIN whatsapp_chats c ON c.chat_id=? LEFT JOIN whatsapp_groups g ON g.chat_id=? LEFT JOIN whatsapp_contacts p ON p.chat_id=?""",
            (chat_id, chat_id, chat_id, chat_id),
        ).fetchone()
        rows = db.execute(
            """SELECT * FROM whatsapp_chat_messages WHERE chat_id=? ORDER BY message_timestamp,id""",
            (chat_id,),
        ).fetchall()
    return clean((name_row or {"name": chat_id})["name"],160) or chat_id, [dict(x) for x in rows]


def export_txt(store, chat_id: str) -> tuple[str, bytes]:
    name, rows = export_messages(store, chat_id)
    lines = [f"Единая очередь - история чата: {name}", f"Chat ID: {chat_id}", ""]
    for row in rows:
        ts = int(row.get("message_timestamp",0) or 0)
        when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M:%S") if ts else ""
        who = "Вы" if int(row.get("from_me",0) or 0) else clean(row.get("sender"),120) or "Пользователь"
        flags = []
        if int(row.get("forwarded",0) or 0): flags.append("переслано")
        if int(row.get("edited",0) or 0): flags.append("изменено")
        if int(row.get("deleted",0) or 0): flags.append("удалено")
        text = clean(row.get("body") or row.get("transcript") or row.get("media_name") or "[Вложение]", 32000)
        suffix = f" [{' / '.join(flags)}]" if flags else ""
        lines.append(f"[{when}] {who}{suffix}: {text}")
    return name, ("\n".join(lines) + "\n").encode("utf-8")


def export_zip(store, chat_id: str) -> tuple[str, bytes]:
    name, txt = export_txt(store, chat_id)
    _, rows = export_messages(store, chat_id)
    out = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("history.txt", txt)
        for idx, row in enumerate(rows, 1):
            path = clean(row.get("media_path"), 1000)
            if not path or not Path(path).is_file():
                continue
            raw_name = clean(row.get("media_name"), 200) or Path(path).name or f"media-{idx}"
            safe = re.sub(r"[^\w.()\- а-яА-ЯёЁ]+", "_", raw_name, flags=re.UNICODE).strip("._ ") or f"media-{idx}"
            candidate = safe
            n = 2
            while candidate in used:
                stem, suffix = os.path.splitext(safe)
                candidate = f"{stem}-{n}{suffix}"
                n += 1
            used.add(candidate)
            try:
                zf.write(path, f"attachments/{candidate}")
            except OSError:
                pass
    return name, out.getvalue()


def _pdf_unicode_font() -> Path | None:
    for candidate in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _ttf_cmap_for_chars(font_bytes: bytes, chars: str) -> dict[int, int]:
    """Read only the TTF cmap data needed for our PDF fallback."""
    import struct
    if len(font_bytes) < 12:
        return {}
    num_tables = struct.unpack(">H", font_bytes[4:6])[0]
    tables: dict[str, tuple[int, int]] = {}
    offset = 12
    for _ in range(num_tables):
        if offset + 16 > len(font_bytes):
            break
        tag = font_bytes[offset:offset + 4].decode("latin1", "ignore")
        _checksum, table_offset, length = struct.unpack(">III", font_bytes[offset + 4:offset + 16])
        tables[tag] = (table_offset, length)
        offset += 16
    if "cmap" not in tables:
        return {}
    cmap_offset, cmap_length = tables["cmap"]
    data = font_bytes[cmap_offset:cmap_offset + cmap_length]
    if len(data) < 4:
        return {}
    _version, count = struct.unpack(">HH", data[:4])
    records: list[tuple[int, int, int, int]] = []
    for index in range(count):
        pos = 4 + index * 8
        if pos + 8 > len(data):
            break
        platform, encoding, sub_offset = struct.unpack(">HHI", data[pos:pos + 8])
        if sub_offset + 2 <= len(data):
            fmt = struct.unpack(">H", data[sub_offset:sub_offset + 2])[0]
            if fmt in {4, 12}:
                records.append((fmt, platform, encoding, sub_offset))
    records.sort(
        key=lambda item: (
            0 if item[0] == 12 else 1,
            0 if item[1] == 3 else 1 if item[1] == 0 else 2,
            item[2],
        )
    )
    wanted = {ord(ch) for ch in chars}
    result: dict[int, int] = {}
    for fmt, _platform, _encoding, sub_offset in records:
        if fmt == 12 and sub_offset + 16 <= len(data):
            _fmt, _reserved, length, _language, groups_count = struct.unpack(
                ">HHIII", data[sub_offset:sub_offset + 16]
            )
            if sub_offset + length > len(data):
                continue
            groups: list[tuple[int, int, int]] = []
            pos = sub_offset + 16
            for _ in range(groups_count):
                if pos + 12 > sub_offset + length:
                    break
                start, finish, start_gid = struct.unpack(">III", data[pos:pos + 12])
                groups.append((start, finish, start_gid))
                pos += 12
            for codepoint in wanted - result.keys():
                lo, hi = 0, len(groups) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    start, finish, start_gid = groups[mid]
                    if codepoint < start:
                        hi = mid - 1
                    elif codepoint > finish:
                        lo = mid + 1
                    else:
                        result[codepoint] = start_gid + (codepoint - start)
                        break
        elif fmt == 4 and sub_offset + 14 <= len(data):
            _fmt, length, _language, seg_count_x2 = struct.unpack(
                ">HHHH", data[sub_offset:sub_offset + 8]
            )
            if sub_offset + length > len(data):
                continue
            seg_count = seg_count_x2 // 2
            end_offset = sub_offset + 14
            end_codes = struct.unpack(
                ">" + "H" * seg_count,
                data[end_offset:end_offset + 2 * seg_count],
            )
            start_offset = end_offset + 2 * seg_count + 2
            start_codes = struct.unpack(
                ">" + "H" * seg_count,
                data[start_offset:start_offset + 2 * seg_count],
            )
            delta_offset = start_offset + 2 * seg_count
            deltas = struct.unpack(
                ">" + "h" * seg_count,
                data[delta_offset:delta_offset + 2 * seg_count],
            )
            range_offset_pos = delta_offset + 2 * seg_count
            range_offsets = struct.unpack(
                ">" + "H" * seg_count,
                data[range_offset_pos:range_offset_pos + 2 * seg_count],
            )
            for codepoint in wanted - result.keys():
                if codepoint > 0xFFFF:
                    continue
                for idx, (start, finish) in enumerate(zip(start_codes, end_codes)):
                    if start <= codepoint <= finish:
                        range_offset = range_offsets[idx]
                        if range_offset == 0:
                            gid = (codepoint + deltas[idx]) & 0xFFFF
                        else:
                            glyph_pos = (
                                range_offset_pos
                                + 2 * idx
                                + range_offset
                                + 2 * (codepoint - start)
                            )
                            if glyph_pos + 2 > sub_offset + length:
                                gid = 0
                            else:
                                glyph = struct.unpack(
                                    ">H", data[glyph_pos:glyph_pos + 2]
                                )[0]
                                gid = ((glyph + deltas[idx]) & 0xFFFF) if glyph else 0
                        if gid:
                            result[codepoint] = gid
                        break
        if wanted.issubset(result.keys()):
            break
    return result


def _export_pdf_native(name: str, rows: list[dict[str, Any]]) -> bytes:
    """Self-contained Unicode PDF writer using an Ubuntu system TTF font."""
    import struct
    import textwrap

    font_path = _pdf_unicode_font()
    if not font_path:
        raise RuntimeError("PDF-экспорт: на сервере не найден Unicode-шрифт")
    font_bytes = font_path.read_bytes()

    raw_lines: list[str] = []
    for row in rows:
        ts = int(row.get("message_timestamp", 0) or 0)
        when = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone()
            .strftime("%d.%m.%Y %H:%M:%S")
            if ts
            else ""
        )
        who = (
            "Вы"
            if int(row.get("from_me", 0) or 0)
            else clean(row.get("sender"), 120) or "Пользователь"
        )
        flags: list[str] = []
        if int(row.get("forwarded", 0) or 0):
            flags.append("переслано")
        if int(row.get("edited", 0) or 0):
            flags.append("изменено")
        if int(row.get("deleted", 0) or 0):
            flags.append("удалено")
        text = clean(
            row.get("body")
            or row.get("transcript")
            or row.get("media_name")
            or "[Вложение]",
            32000,
        )
        prefix = " · ".join(
            part for part in (when, who, ", ".join(flags)) if part
        )
        logical = f"[{prefix}] {text}" if prefix else text
        for chunk in logical.splitlines() or [""]:
            raw_lines.extend(
                textwrap.wrap(
                    chunk,
                    width=92,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or [""]
            )
        raw_lines.append("")

    title = f"Единая очередь · история чата: {name}"
    cmap = _ttf_cmap_for_chars(font_bytes, title + "".join(raw_lines) + "?")
    fallback_gid = cmap.get(ord("?"), 0)
    used: dict[int, int] = {}

    def encode_line(value: str) -> str:
        payload = bytearray()
        for char in value:
            codepoint = ord(char)
            gid = cmap.get(codepoint, fallback_gid)
            if gid:
                used[gid] = codepoint
            payload.extend(struct.pack(">H", gid))
        return payload.hex().upper()

    title_hex = encode_line(title)
    per_page = 57
    pages = [
        raw_lines[index:index + per_page]
        for index in range(0, len(raw_lines), per_page)
    ] or [[]]
    encoded_pages = [[encode_line(line) for line in page] for page in pages]

    mappings: list[tuple[str, str]] = []
    for gid, codepoint in sorted(used.items()):
        src = f"<{gid:04X}>"
        if codepoint <= 0xFFFF:
            dst = f"<{codepoint:04X}>"
        else:
            value = codepoint - 0x10000
            high = 0xD800 + (value >> 10)
            low = 0xDC00 + (value & 0x3FF)
            dst = f"<{high:04X}{low:04X}>"
        mappings.append((src, dst))

    cmap_lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /QueueToUnicode def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for index in range(0, len(mappings), 100):
        chunk = mappings[index:index + 100]
        cmap_lines.append(f"{len(chunk)} beginbfchar")
        cmap_lines.extend(f"{src} {dst}" for src, dst in chunk)
        cmap_lines.append("endbfchar")
    cmap_lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
        ]
    )
    to_unicode = ("\n".join(cmap_lines) + "\n").encode("ascii")

    objects: list[bytes] = []

    def add_object(content: bytes) -> int:
        objects.append(content)
        return len(objects)

    catalog_id = add_object(b"")
    pages_id = add_object(b"")
    font_file_id = add_object(
        f"<< /Length {len(font_bytes)} /Length1 {len(font_bytes)} >>\nstream\n".encode()
        + font_bytes
        + b"\nendstream"
    )
    descriptor_id = add_object(
        (
            "<< /Type /FontDescriptor /FontName /QueueUnicode /Flags 32 "
            "/FontBBox [-1021 -463 1794 1232] /ItalicAngle 0 "
            "/Ascent 928 /Descent -236 /CapHeight 729 /StemV 80 "
            f"/FontFile2 {font_file_id} 0 R >>"
        ).encode()
    )
    cid_font_id = add_object(
        (
            "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /QueueUnicode "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/FontDescriptor {descriptor_id} 0 R /DW 600 /CIDToGIDMap /Identity >>"
        ).encode()
    )
    unicode_id = add_object(
        f"<< /Length {len(to_unicode)} >>\nstream\n".encode()
        + to_unicode
        + b"endstream"
    )
    type0_id = add_object(
        (
            "<< /Type /Font /Subtype /Type0 /BaseFont /QueueUnicode "
            "/Encoding /Identity-H "
            f"/DescendantFonts [{cid_font_id} 0 R] /ToUnicode {unicode_id} 0 R >>"
        ).encode()
    )

    page_ids: list[int] = []
    for lines in encoded_pages:
        commands = [
            "BT",
            "/F1 13 Tf",
            "38 806 Td",
            f"<{title_hex}> Tj",
            "0 -24 Td",
            "/F1 9 Tf",
            "12 TL",
        ]
        for line_hex in lines:
            commands.extend([f"<{line_hex}> Tj", "T*"])
        commands.append("ET")
        stream = ("\n".join(commands) + "\n").encode("ascii")
        content_id = add_object(
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"endstream"
        )
        page_id = add_object(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {type0_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        page_ids.append(page_id)

    objects[catalog_id - 1] = (
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    )
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] "
        f"/Count {len(page_ids)} >>"
    ).encode()

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def export_pdf(store, chat_id: str) -> tuple[str, bytes]:
    name, rows = export_messages(store, chat_id)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    except Exception:
        log_suppressed("queue_chat_export.export_pdf")
        return name, _export_pdf_native(name, rows)

    font_name = "Helvetica"
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).is_file():
            try:
                pdfmetrics.registerFont(TTFont("QueueUnicode", path))
                font_name = "QueueUnicode"
                break
            except Exception:
                log_suppressed("queue_chat_export.export_pdf")

    if font_name == "Helvetica" and any(ord(ch) > 255 for ch in name):
        return name, _export_pdf_native(name, rows)

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        rightMargin=34,
        leftMargin=34,
        topMargin=34,
        bottomMargin=34,
        title=f"История чата {name}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "QueueTitle",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=16,
        leading=20,
    )
    body_style = ParagraphStyle(
        "QueueBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        alignment=TA_LEFT,
    )
    story = [
        Paragraph(
            f"Единая очередь - история чата: {html_escape(name)}",
            title_style,
        ),
        Spacer(1, 12),
    ]
    for row in rows:
        ts = int(row.get("message_timestamp", 0) or 0)
        when = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone()
            .strftime("%d.%m.%Y %H:%M:%S")
            if ts
            else ""
        )
        who = (
            "Вы"
            if int(row.get("from_me", 0) or 0)
            else clean(row.get("sender"), 120) or "Пользователь"
        )
        text = clean(
            row.get("body")
            or row.get("transcript")
            or row.get("media_name")
            or "[Вложение]",
            32000,
        )
        story.append(
            Paragraph(
                f"<b>{html_escape(when)} · {html_escape(who)}</b><br/>"
                f"{html_escape(text).replace(chr(10), '<br/>')}",
                body_style,
            )
        )
        story.append(Spacer(1, 7))
    doc.build(story)
    return name, out.getvalue()
