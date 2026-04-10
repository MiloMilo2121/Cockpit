from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

import httpx
from celery import Task

from app.agents import AGENTIC_MODEL, run_agentic_loop
from app.buffer_store import consume_buffered_events
from app.circuit_breaker import is_open, record_failure, record_success
from app.config import settings
from app.dead_letter import push_dead_letter
from app.db import list_dead_letter_events_since, list_google_accounts
from app.evolution_client import send_whatsapp_text
from app.google_sync import sync_google_account_pipeline
from app.metrics import increment_metric
from app.rag_pipeline import ingest_document_pipeline
from app.redis_client import get_redis_client


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


def _semantic_cache_key(*, source: str, user_id: str, input_digest: str) -> str:
    digest = hashlib.md5(f"{source}|{user_id}|{input_digest}".encode("utf-8")).hexdigest()
    return f"cockpit:semantic_cache:{digest}"


def _get_cached_agentic_result(*, source: str, user_id: str, input_digest: str) -> Dict[str, Any] | None:
    if not settings.semantic_cache_enabled:
        return None

    try:
        client = get_redis_client()
        raw = client.get(_semantic_cache_key(source=source, user_id=user_id, input_digest=input_digest))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None

    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("output"), str):
        increment_metric("semantic_cache_hits_total")
        return payload
    return None


def _set_cached_agentic_result(*, source: str, user_id: str, input_digest: str, result: Dict[str, str]) -> None:
    if not settings.semantic_cache_enabled:
        return

    payload = {
        "agent": result["agent"],
        "priority": result["priority"],
        "agentic_model": result["agentic_model"],
        "output": result["output"],
    }
    try:
        client = get_redis_client()
        client.set(
            _semantic_cache_key(source=source, user_id=user_id, input_digest=input_digest),
            json.dumps(payload, ensure_ascii=True),
            ex=max(int(settings.semantic_cache_ttl_seconds), 30),
        )
        increment_metric("semantic_cache_sets_total")
    except Exception:  # noqa: BLE001
        return


def _run_agentic_pipeline(message: str, *, user_id: str, is_proactive: bool = False) -> Dict[str, str]:
    output = run_agentic_loop(
        instruction=message,
        user_id=user_id,
        is_proactive=is_proactive,
    )
    return {
        "agent": "REACT_COCKPIT_DIRECTOR",
        "priority": "high" if is_proactive else "medium",
        "agentic_model": AGENTIC_MODEL,
        "output": output,
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
    user_id = str(event.get("user_id") or settings.proactive_default_user_id)
    source = str(event.get("source") or "unknown")
    input_digest = hashlib.md5(message.encode("utf-8")).hexdigest()

    cached_result = _get_cached_agentic_result(source=source, user_id=user_id, input_digest=input_digest)
    if cached_result:
        restored_output = _restore_text(request_id, str(cached_result["output"]))
        return {
            "status": "completed",
            "route_used": "semantic_cache",
            "agent": str(cached_result.get("agent", "REACT_COCKPIT_DIRECTOR")),
            "priority": str(cached_result.get("priority", "medium")),
            "agentic_model": str(cached_result.get("agentic_model", AGENTIC_MODEL)),
            "user_id": event.get("user_id"),
            "source": event.get("source"),
            "result": restored_output,
        }

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
        agentic_result = _run_agentic_pipeline(redacted_text, user_id=user_id)
        restored_output = _restore_text(request_id, agentic_result["output"])
        record_success("openrouter")
        increment_metric("openrouter_success_total")
        _set_cached_agentic_result(
            source=source,
            user_id=user_id,
            input_digest=input_digest,
            result=agentic_result,
        )

        return {
            "status": "completed",
            "route_used": "openrouter_free_react_loop",
            "agent": agentic_result["agent"],
            "priority": agentic_result["priority"],
            "agentic_model": agentic_result["agentic_model"],
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


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.proactive_execution")
def proactive_execution(
    self: RetryableTask,
    instruction: str,
    user_id: str | None = None,
) -> Dict[str, Any]:
    increment_metric("proactive_execution_runs_total")
    target_user_id = str(user_id or settings.proactive_default_user_id).strip()
    redaction = _redact_text(instruction)
    redacted_instruction = str(redaction.get("redacted_text", ""))
    request_id = str(redaction.get("request_id", ""))

    if is_open("openrouter"):
        increment_metric("openrouter_circuit_open_hits_total")
        push_dead_letter(
            stage="proactive_execution_precheck",
            reason="openrouter_circuit_open",
            payload={"instruction": instruction, "user_id": target_user_id},
            error="circuit_open",
        )
        return {
            "status": "failed",
            "reason": "openrouter_circuit_open",
            "user_id": target_user_id,
        }

    try:
        result = _run_agentic_pipeline(
            redacted_instruction,
            user_id=target_user_id,
            is_proactive=True,
        )
        restored_output = _restore_text(request_id, result["output"])
        record_success("openrouter")
        increment_metric("openrouter_success_total")
    except Exception as exc:  # noqa: BLE001
        failure_info = record_failure("openrouter")
        increment_metric("openrouter_failure_total")
        push_dead_letter(
            stage="proactive_execution",
            reason="openrouter_failure",
            payload={"instruction": instruction, "user_id": target_user_id},
            error=str(exc),
        )
        return {
            "status": "failed",
            "reason": "openrouter_failure",
            "error": str(exc),
            "circuit_opened": bool(failure_info.get("opened")),
            "user_id": target_user_id,
        }

    notification: Dict[str, Any]
    try:
        notification = send_whatsapp_text(restored_output)
    except Exception as exc:  # noqa: BLE001
        push_dead_letter(
            stage="proactive_notification",
            reason="evolution_send_failure",
            payload={"instruction": redacted_instruction, "user_id": target_user_id, "output": result["output"]},
            error=str(exc),
        )
        notification = {"status": "failed", "reason": "evolution_send_failure", "error": str(exc)}

    return {
        "status": "completed",
        "route_used": "openrouter_free_react_loop",
        "agent": result["agent"],
        "priority": result["priority"],
        "agentic_model": result["agentic_model"],
        "user_id": target_user_id,
        "source": "cron",
        "result": restored_output,
        "notification": notification,
    }


def _is_critical_dead_letter(event: Dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            str(event.get("stage") or ""),
            str(event.get("reason") or ""),
            str(event.get("error") or ""),
        ]
    ).lower()
    critical_terms = ("openrouter", "timeout", "network", "circuit", "evolution", "google_sync")
    return any(term in haystack for term in critical_terms)


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.dead_letter_anomaly_scan")
def dead_letter_anomaly_scan(self: RetryableTask) -> Dict[str, Any]:
    increment_metric("dead_letter_anomaly_scan_runs_total")
    window_minutes = max(int(settings.dead_letter_anomaly_window_minutes), 1)
    threshold = max(int(settings.dead_letter_anomaly_threshold), 1)
    events = list_dead_letter_events_since(minutes=window_minutes, limit=100)
    critical_events = [event for event in events if _is_critical_dead_letter(event)]

    if len(critical_events) <= threshold:
        return {
            "status": "noop",
            "reason": "below_threshold",
            "window_minutes": window_minutes,
            "critical_count": len(critical_events),
        }

    client = get_redis_client()
    cooldown_key = "cockpit:alerts:dead_letter_anomaly"
    acquired = client.set(
        cooldown_key,
        "1",
        ex=max(int(settings.dead_letter_alert_cooldown_seconds), 60),
        nx=True,
    )
    if not acquired:
        return {
            "status": "suppressed",
            "reason": "cooldown_active",
            "window_minutes": window_minutes,
            "critical_count": len(critical_events),
        }

    sample = critical_events[:5]
    lines = [
        "BLUF: anomalia critica nel Cockpit.",
        f"FINESTRA: ultimi {window_minutes} minuti.",
        f"ERRORI_CRITICI: {len(critical_events)}.",
        "CAMPIONE:",
    ]
    for event in sample:
        lines.append(f"- {event['stage']}::{event['reason']} | {str(event.get('error') or '')[:160]}")

    try:
        notification = send_whatsapp_text("\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        push_dead_letter(
            stage="dead_letter_anomaly_scan",
            reason="alert_send_failure",
            payload={"critical_count": len(critical_events), "window_minutes": window_minutes},
            error=str(exc),
        )
        notification = {"status": "failed", "reason": "alert_send_failure", "error": str(exc)}
    increment_metric("dead_letter_anomaly_alerts_total")
    return {
        "status": "alerted",
        "window_minutes": window_minutes,
        "critical_count": len(critical_events),
        "notification": notification,
    }


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.rag_ingest_document")
def rag_ingest_document(self: RetryableTask, request: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return ingest_document_pipeline(request)
    except Exception as exc:  # noqa: BLE001
        push_dead_letter(
            stage="rag_ingest_task",
            reason="rag_ingest_failure",
            payload=request if isinstance(request, dict) else {"raw": str(request)},
            error=str(exc),
        )
        increment_metric("rag_ingest_failure_total")
        return {
            "status": "failed",
            "reason": "rag_ingest_failure",
            "error": str(exc),
        }


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.sync_google_account")
def sync_google_account(
    self: RetryableTask,
    account_id: int,
    providers: list[str] | None = None,
    bootstrap: bool = False,
) -> Dict[str, Any]:
    try:
        return sync_google_account_pipeline(
            account_id=int(account_id),
            providers=providers,
            bootstrap=bootstrap,
        )
    except Exception as exc:  # noqa: BLE001
        push_dead_letter(
            stage="google_sync_task",
            reason="google_sync_failure",
            payload={
                "account_id": int(account_id),
                "providers": providers or ["gmail", "drive", "calendar"],
                "bootstrap": bool(bootstrap),
            },
            error=str(exc),
        )
        increment_metric("google_sync_failure_total")
        return {
            "status": "failed",
            "reason": "google_sync_failure",
            "error": str(exc),
            "account_id": int(account_id),
        }


@celery_app.task(bind=True, base=RetryableTask, name="cockpit.sync_all_google_accounts")
def sync_all_google_accounts(self: RetryableTask) -> Dict[str, Any]:
    increment_metric("google_sync_all_runs_total")
    accounts = [account for account in list_google_accounts() if str(account.get("status")) == "active"]
    dispatched: list[int] = []

    for account in accounts:
        account_id = int(account["id"])
        sync_google_account.delay(account_id, ["gmail", "drive", "calendar"], False)
        dispatched.append(account_id)

    return {
        "status": "dispatched",
        "accounts": dispatched,
        "count": len(dispatched),
    }
