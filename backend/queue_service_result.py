"""Результат сервисов для коннектора, не привязанный к способу передачи."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceResult:
    payload: dict[str, object]
    status: int = 200
