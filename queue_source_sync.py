from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import posixpath
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import queue_transit
import queue_google_sheets

CACHE_DIR = Path(os.getenv("QUEUE_SOURCE_CACHE_DIR", "/var/lib/edinaya-ochered/source-cache"))
SYNC_SECONDS = max(30, min(1800, int(os.getenv("QUEUE_SOURCE_SYNC_SECONDS", "60") or "60")))
FETCH_TIMEOUT = max(5.0, min(30.0, float(os.getenv("QUEUE_SOURCE_FETCH_TIMEOUT", "15") or "15")))
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_XLSX_BYTES = 64 * 1024 * 1024
INSTRUCTION_MEDIA_DIR = CACHE_DIR / "instruction-media"

TRANSIT_SOURCE_URL_DEFAULT = ""
TRANSIT_MAIN_GID = os.getenv("QUEUE_TRANSIT_SOURCE_GID", "0").strip() or "0"
TRANSIT_POINTS_GID = os.getenv("QUEUE_TRANSIT_POINTS_GID", "0").strip() or "0"

_LOCK = threading.RLock()
_BG_GUARD = threading.Lock()
_BG_RUNNING: set[str] = set()
_SPREADSHEET_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _display_now() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Almaty")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CACHE_DIR.chmod(0o750)
    except OSError:
        pass

def _ensure_instruction_media_dir() -> None:
    _ensure_cache_dir()
    INSTRUCTION_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        INSTRUCTION_MEDIA_DIR.chmod(0o750)
    except OSError:
        pass


def instruction_media_path(name: str) -> Path | None:
    safe = Path(str(name or "")).name
    if not safe or safe != str(name or ""):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", safe):
        return None
    path = INSTRUCTION_MEDIA_DIR / safe
    return path if path.is_file() else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _ensure_cache_dir()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o640)
    except OSError:
        pass
    tmp.replace(path)


def _spreadsheet_id(source_url: str) -> str:
    match = _SPREADSHEET_RE.search(str(source_url or ""))
    if not match:
        raise ValueError("Источник должен быть ссылкой Google Sheets")
    return match.group(1)


def _source_gid(source_url: str, fallback: str = "0") -> str:
    parsed = urllib.parse.urlparse(str(source_url or ""))
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    raw = (query.get("gid") or fragment.get("gid") or [fallback])[0]
    return raw if str(raw).isdigit() else fallback


def _export_url(source_url: str, gid: str | None = None) -> str:
    spreadsheet_id = _spreadsheet_id(source_url)
    sheet_gid = str(gid or _source_gid(source_url, "0"))
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={sheet_gid}"


def _fetch_xlsx(source_url: str) -> bytes:
    spreadsheet_id = _spreadsheet_id(source_url)
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=xlsx"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Edinaya-Ochered/1.00.6.79",
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.2",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        payload = response.read(MAX_XLSX_BYTES + 1)
        content_type = str(response.headers.get("Content-Type", "") or "").casefold()
    if len(payload) > MAX_XLSX_BYTES:
        raise ValueError("XLSX-источник слишком большой")
    if not payload or not payload.startswith(b"PK"):
        raise ValueError("Google Sheets не вернул XLSX")
    if "text/html" in content_type:
        raise PermissionError("Google Sheets вернул HTML вместо XLSX")
    return payload


def _rels_path(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _resolve_part(base_part: str, target: str) -> str:
    if str(target or "").startswith("/"):
        return str(target).lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _xlsx_relationships(zf: zipfile.ZipFile, part: str) -> dict[str, str]:
    rel_path = _rels_path(part)
    if rel_path not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read(rel_path))
    result: dict[str, str] = {}
    for node in list(root):
        rid = str(node.attrib.get("Id", "") or "")
        target = str(node.attrib.get("Target", "") or "")
        if rid and target and not target.startswith(("http://", "https://")):
            result[rid] = _resolve_part(part, target)
    return result


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(path))
    values: list[str] = []
    for si in root.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
        values.append("".join(node.text or "" for node in si.iter() if node.tag.endswith("}t")))
    return values


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = str(cell.attrib.get("t", "") or "")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
    value_node = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def _xlsx_sheet_parts(zf: zipfile.ZipFile) -> list[str]:
    workbook = "xl/workbook.xml"
    if workbook not in zf.namelist():
        return []
    rels = _xlsx_relationships(zf, workbook)
    root = ET.fromstring(zf.read(workbook))
    parts: list[str] = []
    rel_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sheet in root.iter():
        if not sheet.tag.endswith("}sheet"):
            continue
        rid = str(sheet.attrib.get(rel_attr, "") or "")
        target = rels.get(rid, "")
        if target and target in zf.namelist():
            parts.append(target)
    return parts


def _xlsx_first_row(zf: zipfile.ZipFile, sheet_part: str, shared: list[str]) -> list[str]:
    root = ET.fromstring(zf.read(sheet_part))
    first_row = None
    for row in root.iter():
        if row.tag.endswith("}row"):
            first_row = row
            break
    if first_row is None:
        return []
    values: dict[int, str] = {}
    for cell in list(first_row):
        if not cell.tag.endswith("}c"):
            continue
        ref = str(cell.attrib.get("r", "") or "")
        match = re.match(r"([A-Z]+)", ref.upper())
        if not match:
            continue
        col = 0
        for char in match.group(1):
            col = col * 26 + (ord(char) - 64)
        values[col - 1] = _xlsx_cell_value(cell, shared).strip()
    if not values:
        return []
    return [values.get(index, "") for index in range(max(values) + 1)]


def _select_xlsx_sheet(zf: zipfile.ZipFile, csv_header: list[str]) -> str | None:
    parts = _xlsx_sheet_parts(zf)
    if not parts:
        return None
    shared = _xlsx_shared_strings(zf)
    wanted = [str(value or "").strip().casefold() for value in csv_header[:50]]
    best_part = parts[0]
    best_score = -1
    for part in parts:
        current = [str(value or "").strip().casefold() for value in _xlsx_first_row(zf, part, shared)[:50]]
        score = sum(1 for index, value in enumerate(wanted) if value and index < len(current) and current[index] == value)
        if score > best_score:
            best_part, best_score = part, score
    return best_part


def _safe_image_ext(path: str, payload: bytes) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".bin"


def _save_instruction_image(payload: bytes, source_path: str) -> str | None:
    if not payload or len(payload) > 20 * 1024 * 1024:
        return None
    ext = _safe_image_ext(source_path, payload)
    if ext == ".bin":
        return None
    _ensure_instruction_media_dir()
    name = hashlib.sha256(payload).hexdigest()[:24] + ext
    path = INSTRUCTION_MEDIA_DIR / name
    if not path.exists():
        path.write_bytes(payload)
        try:
            path.chmod(0o640)
        except OSError:
            pass
    return name


def _extract_xlsx_drawing_images(payload: bytes, csv_header: list[str]) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        sheet_part = _select_xlsx_sheet(zf, csv_header)
        if not sheet_part:
            return []
        sheet_rels = _xlsx_relationships(zf, sheet_part)
        drawing_parts = [path for path in sheet_rels.values() if "/drawings/" in path and path in zf.namelist()]
        rel_embed = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
        for drawing_part in drawing_parts:
            drawing_rels = _xlsx_relationships(zf, drawing_part)
            root = ET.fromstring(zf.read(drawing_part))
            for anchor in list(root):
                if not (anchor.tag.endswith("}oneCellAnchor") or anchor.tag.endswith("}twoCellAnchor")):
                    continue
                start = next((node for node in list(anchor) if node.tag.endswith("}from")), None)
                if start is None:
                    continue
                row_node = next((node for node in list(start) if node.tag.endswith("}row")), None)
                col_node = next((node for node in list(start) if node.tag.endswith("}col")), None)
                try:
                    row = int(row_node.text or "0") if row_node is not None else 0
                    col = int(col_node.text or "0") if col_node is not None else 0
                except ValueError:
                    continue
                alt = "Скриншот"
                for node in anchor.iter():
                    if node.tag.endswith("}cNvPr"):
                        alt = str(node.attrib.get("descr") or node.attrib.get("name") or alt).strip()[:160] or alt
                    if not node.tag.endswith("}blip"):
                        continue
                    rid = str(node.attrib.get(rel_embed, "") or "")
                    media_part = drawing_rels.get(rid, "")
                    if not media_part or media_part not in zf.namelist():
                        continue
                    image_name = _save_instruction_image(zf.read(media_part), media_part)
                    if image_name:
                        images.append({"row": row, "col": col, "name": image_name, "alt": alt})
        # Best effort for modern in-cell images that expose media but not a drawing anchor.
        # We only auto-place media that can be mapped to a cell through worksheet vm metadata.
        # Unanchored media is deliberately ignored to avoid attaching an image to the wrong item.
    return images


def _fetch_instruction_images(source_url: str, rows: list[list[str]]) -> tuple[list[dict[str, Any]], str]:
    try:
        payload = _fetch_xlsx(source_url)
        return _extract_xlsx_drawing_images(payload, rows[0] if rows else []), ""
    except Exception as error:
        return [], f"{type(error).__name__}: {str(error)[:180]}"

def _fetch_transit_rows(source_url: str, *, gid: str | None = None) -> list[list[str]]:
    errors: list[str] = []
    try:
        return queue_google_sheets.read_rows(
            source_url,
            gid=str(gid or TRANSIT_MAIN_GID),
            max_rows=5001,
        )
    except Exception as error:
        errors.append(f"SheetsAPI {type(error).__name__}: {str(error)[:160]}")
    try:
        return _fetch_csv(source_url, gid=gid)
    except Exception as error:
        errors.append(f"PublicCSV {type(error).__name__}: {str(error)[:160]}")
    raise RuntimeError("; ".join(errors[-2:]) or "Не удалось получить журнал НП")


def _fetch_csv(source_url: str, *, gid: str | None = None) -> list[list[str]]:
    spreadsheet_id = _spreadsheet_id(source_url)
    sheet_gid = str(gid or _source_gid(source_url, "0"))
    candidates = [
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={sheet_gid}",
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tqx=out:csv&gid={sheet_gid}",
    ]
    errors: list[str] = []
    for url in candidates:
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Edinaya-Ochered/1.00.6.45",
                    "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.1",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                payload = response.read(MAX_SOURCE_BYTES + 1)
                content_type = str(response.headers.get("Content-Type", "") or "").casefold()
            if len(payload) > MAX_SOURCE_BYTES:
                raise ValueError("Источник слишком большой")
            if not payload:
                raise ValueError("Источник вернул пустой файл")
            head = payload[:700].lstrip().lower()
            if b"<html" in head or b"<!doctype" in head or "text/html" in content_type:
                raise PermissionError("Google Sheets вернул HTML вместо CSV; проверьте доступ к исходнику")
            text = payload.decode("utf-8-sig", errors="replace")
            rows = list(csv.reader(io.StringIO(text)))
            if not rows:
                raise ValueError("В источнике нет строк")
            return rows
        except Exception as error:
            errors.append(f"{type(error).__name__}: {str(error)[:180]}")
    raise RuntimeError("; ".join(errors[-2:]) or "Не удалось получить CSV из Google Sheets")


def _slug(value: str, fallback: str) -> str:
    raw = str(value or "").casefold().strip()
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"[^a-z0-9а-яё_-]+", "", raw)
    raw = raw.strip("-")[:80]
    return raw or fallback


def _parse_instruction(rows: list[list[str]], source_url: str, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    header = rows[0] if rows else []
    image_list = images or []
    sections: list[dict[str, Any]] = []
    for column, raw_title in enumerate(header[:50]):
        title = str(raw_title or "").strip()
        if not title or title in {"*", "№", "#"}:
            continue
        positioned: list[tuple[int, str]] = []
        for row_index, row in enumerate(rows[1:1001], start=1):
            value = str(row[column] if column < len(row) else "").strip()
            if value:
                positioned.append((row_index, value[:12000]))
            if len(positioned) >= 150:
                break
        if not positioned:
            continue
        items: list[str] = []
        for item_index, (row_index, value) in enumerate(positioned):
            next_row = positioned[item_index + 1][0] if item_index + 1 < len(positioned) else 1000000
            attached = [
                image for image in image_list
                if int(image.get("col", -1)) == column and row_index <= int(image.get("row", -1)) < next_row
            ]
            if attached:
                markers: list[str] = []
                for image_number, image in enumerate(attached, start=1):
                    name = str(image.get("name", "") or "").strip()
                    alt = str(image.get("alt", "") or "").strip() or f"Скриншот к пункту {item_index + 1}"
                    alt = re.sub(r"[\]\|]+", " ", alt).strip()[:160]
                    if name:
                        markers.append(f"[image:/instruction-media/{name}|{alt}]")
                if markers:
                    value = value + "\n" + "\n".join(markers)
            items.append(value[:14000])
        sections.append(
            {
                "id": _slug(title, f"section-{len(sections) + 1}"),
                "title": title[:160],
                "items": items,
            }
        )
    if not sections:
        raise ValueError("В основном листе инструкции не найдены разделы")
    return {
        "title": "Инструкция",
        "source_name": "Обязанности-инструкция",
        "source_url": source_url,
        "snapshot_date": _display_now(),
        "sections": sections,
        "media_count": len(image_list),
    }


def instruction_data(source_url: str, fallback_path: str | Path, *, force: bool = False) -> dict[str, Any]:
    cache_path = CACHE_DIR / "instruction_live.json"
    fallback_path = Path(fallback_path)
    now = time.time()
    with _LOCK:
        cached = _read_json(cache_path)
        last_attempt = float((cached or {}).get("_sync_epoch", 0) or 0)
        due = force or not cached or (now - last_attempt >= SYNC_SECONDS)
        if due:
            try:
                rows = _fetch_csv(source_url)
                instruction_images, image_error = _fetch_instruction_images(source_url, rows)
                data = _parse_instruction(rows, source_url, instruction_images)
                data["_sync_epoch"] = now
                media_note = f" · изображений: {len(instruction_images)}" if instruction_images else ""
                data["_sync"] = {
                    "state": "live",
                    "last_sync": _display_now(),
                    "message": "Данные получены из исходной Google-таблицы" + media_note,
                    "error": image_error if image_error and not instruction_images else "",
                }
                _write_json(cache_path, data)
                return data
            except Exception as error:
                error_text = f"{type(error).__name__}: {str(error)}"[:500]
                if cached:
                    cached["_sync_epoch"] = now
                    cached["_sync"] = {
                        "state": "cached",
                        "last_sync": str((cached.get("_sync") or {}).get("last_sync", "") or cached.get("snapshot_date", "")),
                        "message": "Источник временно недоступен, показана последняя синхронизированная копия",
                        "error": error_text,
                    }
                    _write_json(cache_path, cached)
                    return cached
                fallback = _read_json(fallback_path) or {"title": "Инструкция", "sections": []}
                fallback["_sync_epoch"] = now
                fallback["_sync"] = {
                    "state": "local",
                    "last_sync": str(fallback.get("snapshot_date", "") or ""),
                    "message": "Не удалось получить свежую инструкцию из Google Sheets",
                    "error": error_text,
                }
                try:
                    _write_json(CACHE_DIR / "instruction_sync_state.json", {"_sync_epoch": now, "_sync": fallback["_sync"]})
                except OSError:
                    pass
                return fallback
        if cached:
            sync = cached.get("_sync") if isinstance(cached.get("_sync"), dict) else {}
            if sync.get("state") == "cached" and now - last_attempt < SYNC_SECONDS:
                return cached
            cached["_sync"] = {
                "state": "live",
                "last_sync": str(sync.get("last_sync", "") or cached.get("snapshot_date", "")),
                "message": "Автосинхронизация включена",
                "error": "",
            }
            return cached

        fallback = _read_json(fallback_path) or {"title": "Инструкция", "sections": []}
        fallback["_sync_epoch"] = now
        fallback["_sync"] = {
            "state": "local",
            "last_sync": str(fallback.get("snapshot_date", "") or ""),
            "message": "Используется локальная копия инструкции",
            "error": "Кэш ещё не создан; нажмите «Обновить сейчас»",
        }
        return fallback


def transit_source_url() -> str:
    return os.getenv("QUEUE_TRANSIT_SOURCE_URL", TRANSIT_SOURCE_URL_DEFAULT).strip() or TRANSIT_SOURCE_URL_DEFAULT


def _normalize_header(value: object) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", str(value or "").casefold()).strip()


def _find_column(headers: list[str], *needles: str) -> int | None:
    normalized = [_normalize_header(item) for item in headers]
    for needle in needles:
        token = _normalize_header(needle)
        for index, header in enumerate(normalized):
            if token and token in header:
                return index
    return None


def _source_status(raw: str) -> str:
    text = str(raw or "").strip()
    key = text.casefold()
    if not key:
        return ""
    if "успеш" in key or "штат" in key:
        return "Снято штатно"
    if "срез" in key:
        return "Срезано"
    if "bluetooth" in key or "блют" in key:
        return "Снять через Bluetooth"
    if "нужно снять" in key or "надо снять" in key:
        return "Нужно снять"
    if "отлож" in key:
        return "Отложено"
    if "трос" in key:
        return "Повреждение троса"
    if key in {"нет", "нету"} or "нету" in key:
        return "Нету"
    return text[:80]


def _parse_transit(rows: list[list[str]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    headers = rows[0]
    columns = {
        "number": _find_column(headers, "*", "№"),
        "employee": _find_column(headers, "ф.и.о", "исполнителя"),
        "attached_date": _find_column(headers, "дата навеш"),
        "removed_date": _find_column(headers, "дата снятия"),
        "vehicle_type": _find_column(headers, "вид тс"),
        "vehicle_count": _find_column(headers, "кол-во тс", "количество тс"),
        "plate": _find_column(headers, "номер грнз", "грнз"),
        "seal_count": _find_column(headers, "кол-во нп", "количество нп"),
        "seal": _find_column(headers, "номер пломбы", "нп"),
        "reason": _find_column(headers, "причина снятия"),
        "status_raw": _find_column(headers, "статус"),
        "note": _find_column(headers, "примечание"),
        "transport_status": _find_column(headers, "статус перевозки"),
        "location": _find_column(headers, "место снятие нп", "место снятия нп"),
    }

    def cell(row: list[str], field: str) -> str:
        index = columns.get(field)
        return str(row[index] if index is not None and index < len(row) else "").strip()

    records: list[dict[str, Any]] = []
    for sheet_row, row in enumerate(rows[1:5001], start=2):
        values = {
            "row": sheet_row,
            "number": cell(row, "number") or str(sheet_row - 1),
            "employee": cell(row, "employee"),
            "attached_date": cell(row, "attached_date"),
            "removed_date": cell(row, "removed_date"),
            "vehicle_type": cell(row, "vehicle_type"),
            "vehicle_count": cell(row, "vehicle_count"),
            "plate": cell(row, "plate"),
            "seal_count": cell(row, "seal_count"),
            "seal": cell(row, "seal"),
            "reason": cell(row, "reason"),
            "status_raw": cell(row, "status_raw"),
            "note": cell(row, "note"),
            "transport_status": cell(row, "transport_status"),
            "location": cell(row, "location"),
        }
        values["status"] = _source_status(values["status_raw"])
        if not any(values.get(key) for key in ("employee", "plate", "seal", "status_raw", "note", "location")):
            continue
        records.append(values)
    if not records:
        raise ValueError("В журнале НП не найдены записи")
    return records


def _parse_point_changes(rows: list[list[str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for row in rows[1:101]:
        before = str(row[0] if len(row) > 0 else "").strip()
        after = str(row[1] if len(row) > 1 else "").strip()
        if before and after:
            result.append({"before": before[:12000], "after": after[:12000]})
    return result[:20]


def _transit_state_path() -> Path:
    return CACHE_DIR / "transit_sync_state.json"


def sync_transit_source(store: Any, *, force: bool = False) -> dict[str, Any]:
    source_url = transit_source_url()
    state_path = _transit_state_path()
    points_path = CACHE_DIR / "transit_points.json"
    now = time.time()
    with _LOCK:
        previous = _read_json(state_path) or {}
        last_attempt = float(previous.get("last_attempt_epoch", 0) or 0)
        if not force and previous and now - last_attempt < SYNC_SECONDS:
            return previous
        try:
            writeback = queue_transit.flush_pending_writebacks(store, limit=25)
            rows = _fetch_transit_rows(source_url, gid=TRANSIT_MAIN_GID)
            records = _parse_transit(rows)
            result = queue_transit.sync_source_records(store, records, actor="Синхронизация Google Sheets")
            point_error = ""
            try:
                point_rows = _fetch_transit_rows(source_url, gid=TRANSIT_POINTS_GID)
                points = _parse_point_changes(point_rows)
                if points:
                    _write_json(points_path, {"points": points, "updated_at": _now_iso()})
            except Exception as error:
                point_error = str(error)[:180]
            state = {
                "state": "live",
                "last_attempt_epoch": now,
                "last_sync": _display_now(),
                "source_url": source_url,
                "rows": int(result.get("rows", len(records)) or 0),
                "inserted": int(result.get("inserted", 0) or 0),
                "updated": int(result.get("updated", 0) or 0),
                "hidden": int(result.get("hidden", 0) or 0),
                "writeback_configured": bool(writeback.get("configured")),
                "writeback_synced": int(writeback.get("synced", 0) or 0),
                "writeback_pending": int(writeback.get("pending", 0) or 0),
                "writeback_errors": int(writeback.get("errors", 0) or 0),
                "message": (
                    "Журнал НП синхронизирован напрямую через Google Sheets API"
                    if bool(writeback.get("configured"))
                    else "Журнал НП читается из Google Sheets; Google Sheets API ожидает настройки"
                ),
                "error": point_error,
            }
            _write_json(state_path, state)
            return state
        except Exception as error:
            writeback = locals().get("writeback", {}) if isinstance(locals().get("writeback", {}), dict) else {}
            state = {
                **previous,
                "state": "cached" if previous.get("last_sync") else "local",
                "last_attempt_epoch": now,
                "source_url": source_url,
                "writeback_configured": bool(writeback.get("configured")),
                "writeback_synced": int(writeback.get("synced", 0) or 0),
                "writeback_pending": int(writeback.get("pending", 0) or 0),
                "writeback_errors": int(writeback.get("errors", 0) or 0),
                "message": "Источник НП временно недоступен; рабочие записи в SQLite сохранены",
                "error": str(error)[:240],
            }
            _write_json(state_path, state)
            return state



def _background_running(key: str) -> bool:
    with _BG_GUARD:
        return key in _BG_RUNNING


def _start_background(key: str, target: Any) -> bool:
    with _BG_GUARD:
        if key in _BG_RUNNING:
            return False
        _BG_RUNNING.add(key)

    def runner() -> None:
        try:
            target()
        finally:
            with _BG_GUARD:
                _BG_RUNNING.discard(key)

    thread = threading.Thread(target=runner, name=f"queue-source-{key}", daemon=True)
    thread.start()
    return True


def instruction_snapshot(source_url: str, fallback_path: str | Path) -> dict[str, Any]:
    """Return the latest local snapshot without performing network I/O.

    The instruction page must stay fast even when Google Sheets is slow or
    unavailable. Network refreshes are started separately in a daemon thread.
    """
    cache_path = CACHE_DIR / "instruction_live.json"
    cached = _read_json(cache_path)
    running = _background_running("instruction")
    if cached:
        result = dict(cached)
        sync = result.get("_sync") if isinstance(result.get("_sync"), dict) else {}
        if running:
            result["_sync"] = {
                **sync,
                "state": "refreshing",
                "message": "Проверяем изменения в исходной Google-таблице в фоне",
                "error": "",
            }
        return result

    state_payload = _read_json(CACHE_DIR / "instruction_sync_state.json") or {}
    fallback = _read_json(Path(fallback_path)) or {"title": "Инструкция", "sections": []}
    result = dict(fallback)
    result.setdefault("source_url", source_url)
    state_sync = state_payload.get("_sync") if isinstance(state_payload.get("_sync"), dict) else {}
    if running:
        result["_sync"] = {
            **state_sync,
            "state": "refreshing",
            "message": "Получаем актуальную инструкцию из Google Sheets в фоне",
            "error": "",
        }
    else:
        result["_sync"] = state_sync or {
            "state": "local",
            "last_sync": str(result.get("snapshot_date", "") or ""),
            "message": "Используется локальная копия инструкции",
            "error": "Кэш ещё не создан",
        }
    return result


def schedule_instruction_sync(source_url: str, fallback_path: str | Path, *, force: bool = False) -> bool:
    cache_path = CACHE_DIR / "instruction_live.json"
    cached = _read_json(cache_path) or {}
    last_attempt = float(cached.get("_sync_epoch", 0) or 0)
    if not force and cached and time.time() - last_attempt < SYNC_SECONDS:
        return False
    return _start_background(
        "instruction",
        lambda: instruction_data(source_url, fallback_path, force=True),
    )


def transit_sync_state() -> dict[str, Any]:
    state = _read_json(_transit_state_path()) or {
        "state": "local",
        "last_attempt_epoch": 0,
        "last_sync": "",
        "message": "Используются локальные данные журнала НП",
        "error": "Кэш синхронизации ещё не создан",
    }
    if _background_running("transit"):
        state = {
            **state,
            "state": "refreshing",
            "message": "Проверяем изменения журнала НП в Google Sheets в фоне",
            "error": "",
        }
    return state


def schedule_transit_sync(store: Any, *, force: bool = False) -> bool:
    previous = _read_json(_transit_state_path()) or {}
    last_attempt = float(previous.get("last_attempt_epoch", 0) or 0)
    if not force and previous and time.time() - last_attempt < SYNC_SECONDS:
        return False
    return _start_background(
        "transit",
        lambda: sync_transit_source(store, force=True),
    )

def transit_point_changes(fallback: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    payload = _read_json(CACHE_DIR / "transit_points.json") or {}
    points = payload.get("points", [])
    if isinstance(points, list) and points:
        result: list[dict[str, str]] = []
        for item in points[:20]:
            if not isinstance(item, dict):
                continue
            before = str(item.get("before", "") or "").strip()
            after = str(item.get("after", "") or "").strip()
            if before and after:
                result.append({"before": before, "after": after})
        if result:
            return result
    result = []
    for item in (fallback or [])[:20]:
        if isinstance(item, dict):
            before = str(item.get("before", "") or "").strip()
            after = str(item.get("after", "") or "").strip()
            if before and after:
                result.append({"before": before, "after": after})
    return result
