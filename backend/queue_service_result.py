"""Transport-neutral result returned by connector application services."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceResult:
    payload: dict[str, object]
    status: int = 200
