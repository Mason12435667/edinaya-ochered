from __future__ import annotations

import html
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
ALMATY_TIMEZONE = timezone(timedelta(hours=5), name="Asia/Almaty")


def resolve_data_dir(environment: dict[str, str] | None = None, platform_name: str | None = None, project_root: Path | None = None) -> Path:
    environment = environment if environment is not None else os.environ
    platform_name = platform_name if platform_name is not None else os.name
    project_root = project_root if project_root is not None else ROOT
    configured = str(environment.get("QUEUE_DATA_DIR", "")).strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = str(environment.get("LOCALAPPDATA", "")).strip()
    if platform_name == "nt" and local_app_data:
        return Path(local_app_data) / "QueueLocal"
    if platform_name != "nt":
        xdg_data_home = str(environment.get("XDG_DATA_HOME", "")).strip()
        base = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
        return base / "queue-local"
    return project_root / "data"


def prepare_database_path(data_dir: Path, project_root: Path | None = None) -> Path:
    project_root = project_root if project_root is not None else ROOT
    database_path = data_dir / "tickets.db"
    legacy_path = project_root / "data" / "tickets.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    if database_path.absolute() != legacy_path.absolute() and not database_path.exists() and legacy_path.is_file():
        shutil.copy2(legacy_path, database_path)
    return database_path


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return f"+{digits}" if 8 <= len(digits) <= 15 else ""


def display_phone(phone: str) -> str:
    normalized = normalize_phone(phone)
    digits = normalized.lstrip("+")
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    if len(digits) == 12 and digits.startswith("998"):
        return f"+998 ({digits[3:5]}) {digits[5:8]}-{digits[8:10]}-{digits[10:12]}"
    return normalized or "Номер не определён"


def normalized_phone_digits(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def human_time(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ALMATY_TIMEZONE).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def epoch_time(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return 0


def valid_chat_id(value: str) -> str:
    chat_id = str(value or "").strip()[:120]
    if re.fullmatch(r"[A-Za-z0-9_.:-]+@(c\.us|lid)", chat_id):
        return chat_id
    return ""


def valid_group_id(value: str) -> str:
    chat_id = str(value or "").strip()[:120]
    return chat_id if re.fullmatch(r"[A-Za-z0-9_.:-]+@g\.us", chat_id) else ""


def valid_conversation_id(value: str) -> str:
    return valid_chat_id(value) or valid_group_id(value)


def content_disposition_header(disposition: str, filename: str) -> str:
    mode = "attachment" if str(disposition).lower() == "attachment" else "inline"
    raw = str(filename or "file").replace("\r", " ").replace("\n", " ").strip()[:180]
    if not raw:
        raw = "file"
    path_name = Path(raw)
    suffix = path_name.suffix if path_name.suffix.isascii() else ""
    suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:20]
    ascii_stem = path_name.stem.encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_stem).strip(" ._")
    ascii_base = f"{ascii_stem or 'file'}{suffix}"[:120].replace('"', '_')
    encoded = quote(raw, safe="")
    return f"{mode}; filename=\"{ascii_base}\"; filename*=UTF-8''{encoded}"


def media_extension(mimetype: str, filename: str = "") -> str:
    mime = (mimetype or "").split(";", 1)[0].strip().casefold()
    mapping = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "audio/ogg": ".ogg", "audio/opus": ".opus",
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "video/mp4": ".mp4",
        "application/pdf": ".pdf",
    }
    if mime in mapping:
        return mapping[mime]
    suffix = Path(filename or "").suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return ".bin"


def human_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def parse_iso_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None
