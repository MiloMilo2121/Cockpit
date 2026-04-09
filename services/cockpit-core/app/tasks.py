from __future__ import annotations

from typing import Any, Dict

import httpx
from celery import Task

from app.buffer_store import consume_buffered_events
from app.celery_app import celery_app
from app.config import settings


class RetryableTask(Task):
    autoretry_for = (httpx.TimeoutException, httpx.NetworkError)
    retry_backoff = True
    retry_backoff_max = 60
    retry_jitter = True
    max_retries = 5


def _redact_text(text: str) -> Dict[str, Any]:
    response = httpx.post(
        f"{settings.privacy_node_url}/redact",
        json={"text": text, "language": "en"},
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def _restore_text(request_id: str, text: str) -> str:
    response = httpx.post(
        f"{settings.privacy_node_url}/restore",
        json={"request_id": request_id, "text": text, "consume": True},
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("restored_text", ""))


def _call_openrouter(prompt: str) -> str:
    if not settings.openrouter_api_key:
        raise RuntimeError("openrouter_api_key_not_set")

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.openrouter_model,
        "messages": [
            {
                "role": "system",
                "content": "You are the orchestration core. Return concise operational output.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.2,
    }

    response = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=body,
        timeout=45.0,
    )
    response.raise_for_status()
    payload = response.json()

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("invalid_openrouter_response") from exc


def _call_ollama(prompt: str) -> str:
    response = httpx.post(
        f"{settings.ollama_base_url}/api/generate",
        json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
        timeout=45.0,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("response", ""))


def _execute_orchestration(event: Dict[str, Any]) -> Dict[str, Any]:
    message = str(event.get("message", "")).strip()
    if not message:
        return {
            "status": "rejected",
            "reason": "empty_message",
        }

    redaction = _redact_text(message)
    redacted_text = str(redaction.get("redacted_text", ""))
    request_id = str(redaction.get("request_id", ""))

    prompt = (
        "Classify intent, priority and next action for this user message. "
        "Return compact plain text. Message: "
        f"{redacted_text}"
    )

    route_used = "openrouter"
    try:
        llm_output = _call_openrouter(prompt)
    except Exception:  # noqa: BLE001
        route_used = "ollama"
        llm_output = _call_ollama(prompt)

    restored_output = _restore_text(request_id, llm_output)

    return {
        "status": "completed",
        "route_used": route_used,
        "user_id": event.get("user_id"),
        "source": event.get("source"),
        "result": restored_output,
    }


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.process_ingestion_event")
def process_ingestion_event(self: RetryableTask, event: Dict[str, Any]) -> Dict[str, Any]:
    return _execute_orchestration(event)


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.process_buffered_session")
def process_buffered_session(self: RetryableTask, source: str, user_id: str) -> Dict[str, Any]:
    buffered_events = consume_buffered_events(source=source, user_id=user_id)
    if not buffered_events:
        return {"status": "noop", "reason": "buffer_empty", "source": source, "user_id": user_id}

    messages = [
        str(item.get("message", "")).strip()
        for item in buffered_events
        if str(item.get("message", "")).strip()
    ]
    if not messages:
        return {"status": "noop", "reason": "buffer_no_text", "source": source, "user_id": user_id}

    source_ids = [
        str(item.get("source_message_id", "")).strip()
        for item in buffered_events
        if str(item.get("source_message_id", "")).strip()
    ]

    aggregated_event: Dict[str, Any] = {
        "source": source,
        "user_id": user_id,
        "message": "\n".join(messages),
        "metadata": {
            "buffered_count": len(messages),
            "source_message_ids": source_ids,
        },
    }
    return _execute_orchestration(aggregated_event)
