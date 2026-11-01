from __future__ import annotations

import os
import json
import sqlite3
import queue_backup_bundle
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class RuntimeServices:
    """Runtime-only services extracted from app.py.

    The class deliberately receives STORE/AUTH/database paths from app.py so it
    does not own application initialization and cannot create circular imports.
    """

    def __init__(self, store, auth, database_path: Path, data_dir: Path):
        self.store = store
        self.auth = auth
        self.database_path = Path(database_path)
        self.data_dir = Path(data_dir)
        self.backup_dir = self.data_dir / "scheduled-backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._backup_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: dict[str, object] = {"last_run": "", "last_file": "", "last_error": ""}

    def scheduled_backup_hours(self) -> int:
        raw = self.store.get_setting("scheduled_backup_hours", os.getenv("QUEUE_BACKUP_INTERVAL_HOURS", "24"))
        try:
            return max(1, min(168, int(raw)))
        except (TypeError, ValueError):
            return 24

    def scheduled_backup_keep(self) -> int:
        raw = self.store.get_setting("scheduled_backup_keep", os.getenv("QUEUE_BACKUP_KEEP", "14"))
        try:
            return max(2, min(90, int(raw)))
        except (TypeError, ValueError):
            return 14

    def list_scheduled_backups(self) -> list[Path]:
        try:
            return sorted(
                [path for path in self.backup_dir.glob("tickets-*.db") if path.is_file()],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []

    def create_scheduled_backup(self, reason: str = "scheduled") -> tuple[bool, str]:
        if not self._backup_lock.acquire(blocking=False):
            return False, "Резервная копия уже создаётся"
        try:
            target = queue_backup_bundle.create_bundle(self.database_path, self.data_dir, self.backup_dir)
            for old in self.list_scheduled_backups()[self.scheduled_backup_keep():]:
                try:
                    old.unlink()
                    old.with_suffix(".tar.gz").unlink(missing_ok=True)
                except OSError:
                    pass
            with self._state_lock:
                self._state.update({"last_run": datetime.now(timezone.utc).isoformat(), "last_file": target.name, "last_error": ""})
            try:
                self.auth.record_audit(None, "backup_created", object_type="database", object_id=target.name, details=str(reason or "scheduled"))
            except Exception:
                pass
            return True, target.name
        except Exception as error:
            with self._state_lock:
                self._state.update({"last_run": datetime.now(timezone.utc).isoformat(), "last_error": str(error)[:300]})
            return False, str(error)[:300]
        finally:
            self._backup_lock.release()

    def scheduled_backup_snapshot(self) -> dict[str, object]:
        backups = self.list_scheduled_backups()
        latest = backups[0] if backups else None
        with self._state_lock:
            state = dict(self._state)
        try:
            restore_check = json.loads((self.backup_dir / 'restore-check.json').read_text())
        except (OSError, ValueError):
            restore_check = {}
        return {
            "restore_check": restore_check,
            "hours": self.scheduled_backup_hours(),
            "keep": self.scheduled_backup_keep(),
            "count": len(backups),
            "last_file": latest.name if latest else str(state.get("last_file") or ""),
            "last_mtime": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat() if latest else "",
            "last_error": str(state.get("last_error") or ""),
        }

    def scheduled_backup_worker(self, sleep_seconds: float = 60.0) -> None:
        while True:
            try:
                backups = self.list_scheduled_backups()
                latest_age = None
                if backups:
                    latest_age = max(0.0, time.time() - backups[0].stat().st_mtime)
                interval = self.scheduled_backup_hours() * 3600
                if latest_age is None or latest_age >= interval:
                    self.create_scheduled_backup("schedule")
            except Exception as error:
                with self._state_lock:
                    self._state["last_error"] = str(error)[:300]
            time.sleep(max(1.0, float(sleep_seconds)))

    @staticmethod
    def process_resource_snapshot() -> dict[str, object]:
        result: dict[str, object] = {
            "cpu_count": int(os.cpu_count() or 1),
            "cpu_time": round(time.process_time(), 1),
            "load": "",
            "rss_bytes": 0,
        }
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            result["rss_bytes"] = int(getattr(usage, "ru_maxrss", 0) or 0) * 1024
        except Exception:
            pass
        try:
            if hasattr(os, "getloadavg"):
                load = os.getloadavg()
                result["load"] = f"{load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}"
        except Exception:
            pass
        return result
