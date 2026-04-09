from __future__ import annotations

from typing import Any, Dict

import httpx
from celery import Task

from app.agents import route_to_agent, run_specialist
from app.buffer_store import consume_buffered_events
from app.circuit_breaker import is_open, record_failure, record_success
from app.config import settings
from app.dead_letter import push_dead_letter
from app.metrics import increment_metric


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


def _restore_text(request_id: str, text: str, *, consume: bool = True) -> str:
    response = httpx.post(
        f"{settings.privacy_node_url}/restore",
        json={"request_id": request_id, "text": text, "consume": consume},
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("restored_text", ""))


def _degraded_local_response(message: str) -> str:
    lowered = message.lower()
    if any(keyword in lowered for keyword in ["bug", "errore", "error", "crash", "server", "deploy"]):
        intent = "technical_maintenance"
        risk = "high"
    elif any(keyword in lowered for keyword in ["email", "rispondi", "reply", "messaggio", "whatsapp"]):
        intent = "communication"
        risk = "medium"
    elif any(keyword in lowered for keyword in ["contratto", "documento", "fattura", "report", "analizza"]):
        intent = "knowledge_analysis"
        risk = "medium"
    else:
        intent = "planning"
        risk = "medium"

    return (
        f"INTENT: {intent}\n"
        f"RISK: {risk}\n"
        "NEXT_ACTIONS:\n"
        "1. Conferma il contesto operativo minimo.\n"
        "2. Esegui il primo step a rischio basso entro 15 minuti.\n"
        "3. Registra esito e prossima azione nel cockpit."
    )


def _run_multi_agent_pipeline(message: str) -> Dict[str, str]:
    route = route_to_agent(message)
    specialist = run_specialist(
        agent=route["agent"],
        message=message,
        route_reason=route["reason"],
        priority=route["priority"],
    )
    return {
        "agent": route["agent"],
        "priority": route["priority"],
        "route_reason": route["reason"],
        "router_model": route["router_model"],
        "specialist_model": specialist["specialist_model"],
        "output": specialist["output"],
    }


def _execute_orchestration(event: Dict[str, Any]) -> Dict[str, Any]:
    increment_metric("orchestration_runs_total")

    message = str(event.get("message", "")).strip()
    if not message:
        increment_metric("orchestration_rejected_empty_message_total")
        return {
            "status": "rejected",
            "reason": "empty_message",
        }

    redaction = _redact_text(message)
    redacted_text = str(redaction.get("redacted_text", ""))
    request_id = str(redaction.get("request_id", ""))

    if is_open("openrouter"):
        increment_metric("openrouter_circuit_open_hits_total")
        if settings.allow_local_degraded_mode:
            local_output = _degraded_local_response(redacted_text)
            restored_output = _restore_text(request_id, local_output)
            increment_metric("orchestration_degraded_total")
            return {
                "status": "completed",
                "route_used": "local_degraded_circuit_open",
                "agent": "DEGRADED_LOCAL",
                "priority": "medium",
                "user_id": event.get("user_id"),
                "source": event.get("source"),
                "result": restored_output,
            }

        push_dead_letter(
            stage="orchestration_precheck",
            reason="openrouter_circuit_open",
            payload=event,
            error="circuit_open",
        )
        return {
            "status": "failed",
            "reason": "openrouter_circuit_open",
            "user_id": event.get("user_id"),
            "source": event.get("source"),
        }

    try:
        multi_agent_result = _run_multi_agent_pipeline(redacted_text)
        record_success("openrouter")
        increment_metric("openrouter_success_total")

        restored_output = _restore_text(request_id, multi_agent_result["output"])
        return {
            "status": "completed",
            "route_used": "openrouter_free_multi_agent",
            "agent": multi_agent_result["agent"],
            "priority": multi_agent_result["priority"],
            "route_reason": multi_agent_result["route_reason"],
            "router_model": multi_agent_result["router_model"],
            "specialist_model": multi_agent_result["specialist_model"],
            "user_id": event.get("user_id"),
            "source": event.get("source"),
            "result": restored_output,
        }
    except Exception as exc:  # noqa: BLE001
        failure_info = record_failure("openrouter")
        increment_metric("openrouter_failure_total")
        push_dead_letter(
            stage="openrouter_pipeline",
            reason="openrouter_failure",
            payload=event,
            error=str(exc),
        )

        if settings.allow_local_degraded_mode:
            local_output = _degraded_local_response(redacted_text)
            restored_output = _restore_text(request_id, local_output)
            increment_metric("orchestration_degraded_total")
            return {
                "status": "completed",
                "route_used": "local_degraded_after_openrouter_failure",
                "circuit_opened": bool(failure_info.get("opened")),
                "agent": "DEGRADED_LOCAL",
                "priority": "medium",
                "user_id": event.get("user_id"),
                "source": event.get("source"),
                "result": restored_output,
            }

        return {
            "status": "failed",
            "reason": "openrouter_failure",
            "error": str(exc),
            "user_id": event.get("user_id"),
            "source": event.get("source"),
        }


from app.celery_app import celery_app  # noqa: E402


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.process_ingestion_event")
def process_ingestion_event(self: RetryableTask, event: Dict[str, Any]) -> Dict[str, Any]:
    increment_metric("jobs_direct_total")
    return _execute_orchestration(event)


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.process_buffered_session")
def process_buffered_session(self: RetryableTask, source: str, user_id: str) -> Dict[str, Any]:
    increment_metric("jobs_buffered_total")

    buffered_events = consume_buffered_events(source=source, user_id=user_id)
    if not buffered_events:
        increment_metric("buffer_empty_total")
        return {"status": "noop", "reason": "buffer_empty", "source": source, "user_id": user_id}

    messages = [
        str(item.get("message", "")).strip()
        for item in buffered_events
        if str(item.get("message", "")).strip()
    ]
    if not messages:
        increment_metric("buffer_no_text_total")
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
