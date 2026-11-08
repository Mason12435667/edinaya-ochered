from __future__ import annotations

import threading
import time

_CONDITION = threading.Condition()
_REVISION = 1
_OUTBOUND_REVISION = 1
_LAST_KIND = "startup"


def revision() -> int:
    with _CONDITION:
        return int(_REVISION)


def outbound_revision() -> int:
    with _CONDITION:
        return int(_OUTBOUND_REVISION)


def notify(kind: str = "state") -> int:
    global _REVISION, _LAST_KIND
    with _CONDITION:
        _REVISION += 1
        _LAST_KIND = str(kind or "state")[:40]
        _CONDITION.notify_all()
        return int(_REVISION)


def notify_outbound() -> int:
    global _OUTBOUND_REVISION
    with _CONDITION:
        _OUTBOUND_REVISION += 1
        _CONDITION.notify_all()
        return int(_OUTBOUND_REVISION)


def wait_for_change(since: int, timeout: float = 25.0) -> dict[str, object]:
    safe_since = max(0, int(since or 0))
    safe_timeout = max(0.0, min(float(timeout or 0.0), 25.0))
    deadline = time.monotonic() + safe_timeout
    with _CONDITION:
        while _REVISION <= safe_since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _CONDITION.wait(remaining)
        return {
            "revision": int(_REVISION),
            "changed": bool(_REVISION > safe_since),
            "kind": str(_LAST_KIND),
        }


def wait_for_outbound(since: int, timeout: float = 18.0) -> int:
    safe_since = max(0, int(since or 0))
    safe_timeout = max(0.0, min(float(timeout or 0.0), 20.0))
    deadline = time.monotonic() + safe_timeout
    with _CONDITION:
        while _OUTBOUND_REVISION <= safe_since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _CONDITION.wait(remaining)
        return int(_OUTBOUND_REVISION)
