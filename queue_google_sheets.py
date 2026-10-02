from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

DEFAULT_CREDENTIALS = "/etc/edinaya-ochered/google/service-account.json"
DEFAULT_SOURCE_URL = ""
APP_ID_COLUMN = 16  # P
MAX_ROWS = 5001
MAX_COLS = 16
_SPREADSHEET_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_WRITE_LOCK = threading.RLock()


def credentials_path() -> Path:
    raw = str(os.getenv("QUEUE_TRANSIT_GOOGLE_CREDENTIALS", DEFAULT_CREDENTIALS) or DEFAULT_CREDENTIALS).strip()
    return Path(raw)


def source_url() -> str:
    return str(os.getenv("QUEUE_TRANSIT_SOURCE_URL", DEFAULT_SOURCE_URL) or DEFAULT_SOURCE_URL).strip()


def spreadsheet_id(value: str | None = None) -> str:
    raw = str(value or source_url()).strip()
    match = _SPREADSHEET_RE.search(raw)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw):
        return raw
    raise ValueError("Некорректный ID Google Sheets")


def configured() -> bool:
    path = credentials_path()
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(data, dict)
        and data.get("type") == "service_account"
        and data.get("client_email")
        and data.get("private_key")
    )


def _service():
    path = credentials_path()
    if not path.is_file():
        raise RuntimeError(f"Не найден ключ Google Sheets API: {path}")
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError(
            "Не установлены библиотеки Google Sheets API: google-api-python-client/google-auth"
        ) from error
    credentials = Credentials.from_service_account_file(
        str(path),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _api_call(call: Any) -> dict[str, Any]:
    try:
        result = call.execute()
    except Exception as error:
        try:
            from googleapiclient.errors import HttpError
        except ImportError:
            HttpError = ()  # type: ignore[assignment]
        if HttpError and isinstance(error, HttpError):
            status = int(getattr(getattr(error, "resp", None), "status", 0) or 0)
            message = ""
            raw = getattr(error, "content", b"") or b""
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                message = str(payload.get("error", {}).get("message", "") or "")
            except Exception:
                message = raw.decode("utf-8", errors="replace")[:240]
            raise RuntimeError(f"Google Sheets API HTTP {status}: {message[:220]}") from error
        raise RuntimeError(f"Google Sheets API: {str(error)[:240]}") from error
    return result if isinstance(result, dict) else {}


def _sheet_info(service: Any, sheet_id: str, gid: int) -> tuple[str | None, int]:
    meta = _api_call(
        service.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets(properties(sheetId,title,gridProperties(rowCount)))",
        )
    )
    sheets = meta.get("sheets", []) if isinstance(meta, dict) else []
    first_title: str | None = None
    first_rows = 0
    for item in sheets:
        props = item.get("properties", {}) if isinstance(item, dict) else {}
        title = str(props.get("title", "") or "")
        if first_title is None and title:
            first_title = title
            first_rows = int((props.get("gridProperties", {}) or {}).get("rowCount", 0) or 0)
        if int(props.get("sheetId", -1) or -1) == int(gid):
            return title or None, int((props.get("gridProperties", {}) or {}).get("rowCount", 0) or 0)
    return None, first_rows


def _first_sheet_info(service: Any, sheet_id: str) -> tuple[str, int]:
    meta = _api_call(
        service.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets(properties(sheetId,title,gridProperties(rowCount)))",
        )
    )
    sheets = meta.get("sheets", []) if isinstance(meta, dict) else []
    if not sheets:
        raise RuntimeError("В Google Sheets нет листов")
    props = sheets[0].get("properties", {}) if isinstance(sheets[0], dict) else {}
    title = str(props.get("title", "") or "")
    if not title:
        raise RuntimeError("Не удалось определить лист Google Sheets")
    return title, int((props.get("gridProperties", {}) or {}).get("rowCount", 0) or 0)


def _quote_sheet(title: str) -> str:
    return "'" + str(title).replace("'", "''") + "'"


def read_rows(source: str | None = None, *, gid: int | str = 0, max_rows: int = MAX_ROWS) -> list[list[str]]:
    service = _service()
    sid = spreadsheet_id(source)
    try:
        gid_int = int(gid)
    except (TypeError, ValueError):
        gid_int = 0
    title, _ = _sheet_info(service, sid, gid_int)
    if not title:
        return []
    limit = max(1, min(MAX_ROWS, int(max_rows or MAX_ROWS)))
    end_col = "P"
    result = _api_call(
        service.spreadsheets().values().get(
            spreadsheetId=sid,
            range=f"{_quote_sheet(title)}!A1:{end_col}{limit}",
            majorDimension="ROWS",
            valueRenderOption="FORMATTED_VALUE",
        )
    )
    rows = result.get("values", []) if isinstance(result, dict) else []
    normalized: list[list[str]] = []
    for raw in rows[:limit]:
        if not isinstance(raw, list):
            continue
        normalized.append([str(value if value is not None else "") for value in raw[:MAX_COLS]])
    return normalized


def _row_values(values: dict[str, object], request_id: str) -> list[str]:
    v = values or {}
    return [
        str(v.get("number", "") or ""),
        str(v.get("employee", "") or ""),
        str(v.get("attached_date", "") or ""),
        str(v.get("removed_date", "") or ""),
        str(v.get("vehicle_type", "") or ""),
        str(v.get("vehicle_count", "") or ""),
        str(v.get("plate", "") or ""),
        str(v.get("seal_count", "") or ""),
        str(v.get("seal", "") or ""),
        str(v.get("reason", "") or ""),
        str(v.get("status", "") or ""),
        str(v.get("note", "") or ""),
        str(v.get("transport_status", "") or ""),
        "",
        str(v.get("location", "") or ""),
        "_EO:" + str(request_id or ""),
    ]


def _find_request_row(rows: list[list[str]], request_id: str) -> int:
    wanted = "_EO:" + str(request_id or "")
    if not request_id:
        return 0
    for row_number, row in enumerate(rows, start=1):
        if len(row) >= APP_ID_COLUMN and str(row[APP_ID_COLUMN - 1] or "") == wanted:
            return row_number
    return 0


def clear_record(
    *,
    source: str | None = None,
    gid: int | str = 0,
    row: int = 0,
    request_id: str = "",
    expected_number: str = "",
) -> dict[str, object]:
    """Clear one transport row in Google Sheets without shifting row numbers.

    request_id is preferred because it is stable for app-created rows. Imported
    rows fall back to their known source row. We intentionally clear A:P instead
    of deleting the physical row so source_row references for all following
    records remain stable.
    """
    sid = spreadsheet_id(source)
    try:
        gid_int = int(gid)
    except (TypeError, ValueError):
        gid_int = 0
    supplied_row = int(row or 0)
    request_id = str(request_id or "").strip()
    expected_number = str(expected_number or "").strip()

    with _WRITE_LOCK:
        service = _service()
        title, _max_grid_rows = _sheet_info(service, sid, gid_int)
        if not title:
            title, _max_grid_rows = _first_sheet_info(service, sid)
        quoted = _quote_sheet(title)

        target_row = 0
        if request_id:
            values = _api_call(
                service.spreadsheets().values().get(
                    spreadsheetId=sid,
                    range=f"{quoted}!P1:P{MAX_ROWS}",
                    majorDimension="ROWS",
                    valueRenderOption="FORMATTED_VALUE",
                )
            ).get("values", [])
            wanted = "_EO:" + request_id
            for row_number, raw in enumerate(values, start=1):
                if isinstance(raw, list) and raw and str(raw[0] or "") == wanted:
                    target_row = row_number
                    break

        if target_row <= 0 and supplied_row >= 2:
            target_row = supplied_row

        if target_row < 2:
            return {"ok": True, "deleted": False, "row": 0, "reason": "not_found"}

        # Guard against clearing a stale source_row after a structural change.
        if expected_number:
            current = _api_call(
                service.spreadsheets().values().get(
                    spreadsheetId=sid,
                    range=f"{quoted}!A{target_row}:P{target_row}",
                    majorDimension="ROWS",
                    valueRenderOption="FORMATTED_VALUE",
                )
            ).get("values", [])
            first = current[0] if current and isinstance(current[0], list) else []
            actual_number = str(first[0] or "").strip() if first else ""
            actual_marker = str(first[APP_ID_COLUMN - 1] or "").strip() if len(first) >= APP_ID_COLUMN else ""
            marker_ok = bool(request_id and actual_marker == "_EO:" + request_id)
            if actual_number and actual_number != expected_number and not marker_ok:
                raise RuntimeError(
                    "Строка Google Sheets изменилась; обновите журнал и повторите удаление"
                )

        _api_call(
            service.spreadsheets().values().clear(
                spreadsheetId=sid,
                range=f"{quoted}!A{target_row}:P{target_row}",
                body={},
            )
        )
        return {"ok": True, "deleted": True, "row": target_row}


def write_record(
    *,
    source: str | None = None,
    gid: int | str = 0,
    action: str = "append",
    row: int = 0,
    request_id: str,
    values: dict[str, object],
) -> dict[str, object]:
    if not request_id:
        raise RuntimeError("request_id is required")
    action = str(action or "append").lower()
    if action not in {"append", "update"}:
        raise RuntimeError("Неподдерживаемое действие Google Sheets")
    sid = spreadsheet_id(source)
    try:
        gid_int = int(gid)
    except (TypeError, ValueError):
        gid_int = 0

    with _WRITE_LOCK:
        service = _service()
        title, max_grid_rows = _sheet_info(service, sid, gid_int)
        if not title:
            title, max_grid_rows = _first_sheet_info(service, sid)
        quoted = _quote_sheet(title)
        current = _api_call(
            service.spreadsheets().values().get(
                spreadsheetId=sid,
                range=f"{quoted}!A1:P{MAX_ROWS}",
                majorDimension="ROWS",
                valueRenderOption="FORMATTED_VALUE",
            )
        ).get("values", [])
        rows = [list(item) for item in current if isinstance(item, list)]
        target_row = _find_request_row(rows, request_id)
        supplied_row = int(row or 0)
        is_new = not target_row and action == "append"
        if not target_row:
            if action == "update" and supplied_row >= 2:
                target_row = supplied_row
            else:
                target_row = max(2, len(rows) + 1)

        if target_row > max_grid_rows:
            _api_call(
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sid,
                    body={"requests": [{"appendDimension": {"sheetId": gid_int, "dimension": "ROWS", "length": target_row - max_grid_rows}}]},
                )
            )

        _api_call(
            service.spreadsheets().values().update(
                spreadsheetId=sid,
                range=f"{quoted}!A{target_row}:P{target_row}",
                valueInputOption="RAW",
                body={"values": [_row_values(values, request_id)]},
            )
        )

        # Оформление и скрытие P не должны превращать успешную запись данных в ошибку.
        requests: list[dict[str, object]] = []
        if is_new and target_row > 2:
            requests.append({
                "copyPaste": {
                    "source": {"sheetId": gid_int, "startRowIndex": target_row - 2, "endRowIndex": target_row - 1, "startColumnIndex": 0, "endColumnIndex": 15},
                    "destination": {"sheetId": gid_int, "startRowIndex": target_row - 1, "endRowIndex": target_row, "startColumnIndex": 0, "endColumnIndex": 15},
                    "pasteType": "PASTE_FORMAT",
                    "pasteOrientation": "NORMAL",
                }
            })
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": gid_int, "dimension": "COLUMNS", "startIndex": APP_ID_COLUMN - 1, "endIndex": APP_ID_COLUMN},
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        })
        try:
            _api_call(service.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}))
        except Exception:
            pass

        return {"ok": True, "row": target_row, "action": action, "request_id": request_id}
