from __future__ import annotations

import hashlib
from typing import Any

from app.config import settings
from app.schemas import IngestionEvent


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def extract_source_message_id(event: IngestionEvent) -> str:
    metadata = event.metadata or {}

    candidates: list[Any] = [
        metadata.get("message_id"),
        metadata.get("id"),
        metadata.get("event_id"),
        metadata.get("wamid"),
        metadata.get("messageId"),
    ]

    key_data = metadata.get("key")
    if isinstance(key_data, dict):
        candidates.append(key_data.get("id"))

    for candidate in candidates:
        normalized = _string_or_none(candidate)
        if normalized:
            return normalized

    base = f"{event.source}|{event.user_id}|{event.message}|{event.received_at.isoformat()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def self_message_reason(event: IngestionEvent) -> str | None:
    if not settings.loop_block_from_me:
        return None

    metadata = event.metadata or {}

    for key in ("from_me", "fromMe", "is_from_me", "is_bot", "bot_message", "self"):
        value = metadata.get(key)
        if value is True:
            return "self_message"

    direction = _string_or_none(metadata.get("direction"))
    if direction and direction.lower() in {"out", "outbound", "sent"}:
        return "self_message"

    return None
