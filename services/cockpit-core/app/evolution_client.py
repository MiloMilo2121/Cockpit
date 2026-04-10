from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


def send_whatsapp_text(text: str) -> dict[str, Any]:
    if not settings.proactive_notify_whatsapp_enabled:
        return {"status": "skipped", "reason": "whatsapp_notifications_disabled"}

    if not settings.evolution_api_key:
        return {"status": "skipped", "reason": "evolution_api_key_not_set"}
    if not settings.evolution_instance:
        return {"status": "skipped", "reason": "evolution_instance_not_set"}
    if not settings.proactive_whatsapp_number:
        return {"status": "skipped", "reason": "proactive_whatsapp_number_not_set"}

    url = f"{settings.evolution_api_url.rstrip('/')}/message/sendText/{settings.evolution_instance}"
    response = httpx.post(
        url,
        headers={"apikey": settings.evolution_api_key, "Content-Type": "application/json"},
        json={"number": settings.proactive_whatsapp_number, "text": text},
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"evolution_send_text_failed:{response.status_code}:{response.text[:500]}")

    payload: dict[str, Any] = {}
    try:
        raw = response.json()
        if isinstance(raw, dict):
            payload = raw
    except ValueError:
        payload = {}

    return {
        "status": "sent",
        "status_code": response.status_code,
        "response": payload,
    }
