from __future__ import annotations

from typing import Any

from app.config import settings
from app.db import insert_dead_letter_event
from app.metrics import increment_metric


def push_dead_letter(*, stage: str, reason: str, payload: dict[str, Any], error: str | None = None) -> None:
    if not settings.dead_letter_enabled:
        return
    try:
        insert_dead_letter_event(stage=stage, reason=reason, payload=payload, error=error)
    except Exception:  # noqa: BLE001
        return
    increment_metric("dead_letter_total")
