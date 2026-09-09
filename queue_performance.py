from __future__ import annotations

import contextlib
import os
import resource
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_CONNECTOR: dict[str, Any] = {}
_SQL: dict[str, Any] = {"ok": True, "checked_at": "", "slow": [], "indexes": 0, "optimize": "not-run"}
_MEDIA_SLOTS = max(1, min(4, int(os.getenv("QUEUE_MEDIA_WORKERS", "2") or 2)))
_MEDIA_SEM = threading.BoundedSemaphore(_MEDIA_SLOTS)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextlib.contextmanager
def media_slot(timeout: float = 20.0):
    acquired = _MEDIA_SEM.acquire(timeout=max(0.1, float(timeout)))
    if not acquired:
        raise TimeoutError("Очередь обработки медиа занята. Повторите отправку через несколько секунд")
    try:
        yield
    finally:
        _MEDIA_SEM.release()


def update_connector_telemetry(payload: dict[str, Any]) -> None:
    clean = {
        "rss_mb": max(0.0, float(payload.get("rss_mb", 0) or 0)),
        "node_rss_mb": max(0.0, float(payload.get("node_rss_mb", 0) or 0)),
        "heap_mb": max(0.0, float(payload.get("heap_mb", 0) or 0)),
        "external_mb": max(0.0, float(payload.get("external_mb", 0) or 0)),
        "pending_media": max(0, int(payload.get("pending_media", 0) or 0)),
        "outgoing_probe": max(0, int(payload.get("outgoing_probe", 0) or 0)),
        "fast_sync_busy": bool(payload.get("fast_sync_busy")),
        "pending_media_busy": bool(payload.get("pending_media_busy")),
        "contact_cooldown_ms": max(0, int(payload.get("contact_cooldown_ms", 0) or 0)),
        "recovery_interval_ms": max(0, int(payload.get("recovery_interval_ms", 0) or 0)),
        "ram_restart_mb": max(0, int(payload.get("ram_restart_mb", 0) or 0)),
        "reported_at": utc_now(),
    }
    with _LOCK:
        _CONNECTOR.clear()
        _CONNECTOR.update(clean)


def _server_rss_mb() -> float:
    try:
        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Linux ru_maxrss is KiB, macOS bytes. Production target is Ubuntu.
        return round(value / 1024.0, 1)
    except Exception:
        return 0.0


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def sql_health(database_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    out: dict[str, Any] = {"ok": True, "checked_at": utc_now(), "slow": [], "indexes": 0, "optimize": "ok"}
    try:
        db = sqlite3.connect(str(database_path), timeout=2)
        try:
            db.execute("PRAGMA optimize")
            out["indexes"] = int(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'").fetchone()[0])
            checks = []
            if _table_exists(db, "tickets"):
                checks.append(("tickets/status", "SELECT status, COUNT(*) FROM tickets GROUP BY status"))
            if _table_exists(db, "whatsapp_chat_messages"):
                checks.append(("messages/recent", "SELECT id FROM whatsapp_chat_messages ORDER BY id DESC LIMIT 50"))
            if _table_exists(db, "outbound_messages"):
                checks.append(("outbound/status", "SELECT status, COUNT(*) FROM outbound_messages GROUP BY status"))
            for label, query in checks:
                t0 = time.perf_counter()
                list(db.execute(query))
                ms = round((time.perf_counter() - t0) * 1000.0, 1)
                if ms >= 120:
                    out["slow"].append({"query": label, "ms": ms})
        finally:
            db.close()
    except Exception as exc:
        out["ok"] = False
        out["optimize"] = str(exc)[:220]
    out["total_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    with _LOCK:
        _SQL.clear(); _SQL.update(out)
    return out


def snapshot(app) -> dict[str, Any]:
    with _LOCK:
        connector = dict(_CONNECTOR)
        sql = dict(_SQL)
    voice = {"queued": 0, "running": 0, "cached": 0}
    try:
        states = getattr(app.VOICE_JOBS, "states", {})
        if isinstance(states, dict):
            values = list(states.values())
            voice["queued"] = sum(1 for x in values if isinstance(x, dict) and x.get("status") == "queued")
            voice["running"] = sum(1 for x in values if isinstance(x, dict) and x.get("status") == "running")
            voice["cached"] = sum(1 for x in values if isinstance(x, dict) and x.get("status") == "done")
    except Exception:
        pass
    return {
        "server_rss_mb": _server_rss_mb(),
        "connector": connector,
        "sql": sql,
        "media_workers": _MEDIA_SLOTS,
        "chunk_uploads": True,
        "chunk_size": 512 * 1024,
        "voice_queue": voice,
        "lazy_media_client": True,
    }


def friendly_error(raw: object) -> str:
    text = str(raw or "").strip()
    low = text.casefold()
    rules = (
        (("target closed", "detached frame", "protocol error"), "WhatsApp Web перезапустил внутреннюю страницу. Коннектор восстановит соединение автоматически"),
        (("timeout", "timed out", "истекло время"), "Истекло время ожидания ответа. Повторите действие через несколько секунд"),
        (("database is locked", "database is busy", "база занята"), "База данных занята другой операцией. Повторите действие через несколько секунд"),
        (("413", "too large", "превышает лимит", "слишком большой"), "Файл превышает допустимый размер"),
        (("media", "медиа"), "Не удалось обработать вложение. Текст сообщения и остальные файлы не затронуты"),
        (("whatsapp", "connector", "коннектор"), "WhatsApp временно недоступен. Проверьте индикатор подключения и повторите действие"),
        (("network", "fetch", "connection", "соединен"), "Нет связи с сервером. Проверьте сеть и повторите действие"),
    )
    for needles, message in rules:
        if any(n in low for n in needles):
            return message
    return text or "Неизвестная ошибка. Технические детали сохранены в журнале"


def worker_loop(app) -> None:
    log_path = Path(app.DATA_DIR) / "performance.log"
    while True:
        try:
            result = sql_health(app.DATABASE_PATH)
            if (not result.get("ok")) or result.get("slow"):
                line = f"{utc_now()} sql={result.get('optimize')} slow={result.get('slow')} total_ms={result.get('total_ms')}\n"
                try:
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write(line)
                except OSError:
                    pass
        except Exception:
            pass
        time.sleep(300)
