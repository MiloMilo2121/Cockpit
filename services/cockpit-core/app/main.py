from __future__ import annotations

from uuid import uuid4

from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException

from app.buffer_store import (
    append_buffered_event,
    clear_buffer_job,
    get_buffer_job_id,
    try_claim_buffer_job,
)
from app.celery_app import celery_app
from app.circuit_breaker import get_state as get_circuit_breaker_state
from app.config import settings
from app.dead_letter import push_dead_letter
from app.db import (
    ensure_schema,
    find_job_id,
    list_recent_dead_letter_events,
    map_job_to_message,
    register_message_event,
)
from app.event_utils import extract_source_message_id, self_message_reason
from app.metrics import get_metrics_snapshot, increment_metric
from app.rag_pipeline import query_rag_pipeline
from app.rag_store import ensure_rag_collection
from app.schemas import (
    AcceptedResponse,
    IngestionEvent,
    JobStatusResponse,
    RagIngestRequest,
    RagQueryRequest,
    RagQueryResponse,
)
from app.tasks import process_buffered_session, process_ingestion_event, rag_ingest_document as rag_ingest_document_task

app = FastAPI(title="cockpit-core-api", version="0.3.0")


@app.on_event("startup")
def startup() -> None:
    ensure_schema()
    try:
        ensure_rag_collection()
    except Exception:  # noqa: BLE001
        # Do not fail API startup if Qdrant is temporarily unavailable.
        pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _schedule_buffered_job(*, source: str, user_id: str) -> tuple[str, bool]:
    candidate_job_id = str(uuid4())
    claimed = try_claim_buffer_job(source=source, user_id=user_id, job_id=candidate_job_id)

    if claimed:
        try:
            process_buffered_session.apply_async(
                args=[source, user_id],
                countdown=settings.smart_buffer_seconds,
                task_id=candidate_job_id,
            )
        except Exception:  # noqa: BLE001
            clear_buffer_job(source=source, user_id=user_id)
            raise
        return candidate_job_id, True

    existing_job = get_buffer_job_id(source=source, user_id=user_id)
    if existing_job:
        return existing_job, False

    # Rare race fallback when a claimed key expires before read.
    fallback_job_id = str(uuid4())
    claimed_fallback = try_claim_buffer_job(source=source, user_id=user_id, job_id=fallback_job_id)
    if claimed_fallback:
        try:
            process_buffered_session.apply_async(
                args=[source, user_id],
                countdown=settings.smart_buffer_seconds,
                task_id=fallback_job_id,
            )
        except Exception:  # noqa: BLE001
            clear_buffer_job(source=source, user_id=user_id)
            raise
        return fallback_job_id, True

    existing_job = get_buffer_job_id(source=source, user_id=user_id)
    if existing_job:
        return existing_job, False

    raise HTTPException(status_code=500, detail="buffer_scheduler_unavailable")


@app.post("/webhooks/inbox", response_model=AcceptedResponse, status_code=202)
def ingest_event(event: IngestionEvent) -> AcceptedResponse:
    increment_metric("ingestion_requests_total")

    source_message_id = extract_source_message_id(event)
    payload = event.model_dump(mode="json")
    payload["source_message_id"] = source_message_id

    inserted = register_message_event(
        source=event.source,
        source_message_id=source_message_id,
        user_id=event.user_id,
        payload=payload,
    )
    if not inserted:
        increment_metric("ingestion_duplicates_total")
        existing_job = find_job_id(source=event.source, source_message_id=source_message_id)
        return AcceptedResponse(status="duplicate", job_id=existing_job, reason="duplicate_message")

    blocked_reason = self_message_reason(event)
    if blocked_reason:
        increment_metric("ingestion_ignored_total")
        return AcceptedResponse(status="ignored", reason=blocked_reason)

    if settings.smart_buffering_enabled and event.source == "whatsapp":
        append_buffered_event(source=event.source, user_id=event.user_id, event=payload)
        job_id, first_schedule = _schedule_buffered_job(source=event.source, user_id=event.user_id)
        map_job_to_message(source=event.source, source_message_id=source_message_id, job_id=job_id)
        increment_metric("ingestion_buffered_total")
        return AcceptedResponse(
            status="processing",
            job_id=job_id,
            reason="buffer_scheduled" if first_schedule else "buffer_appended",
        )

    job = process_ingestion_event.delay(payload)
    map_job_to_message(source=event.source, source_message_id=source_message_id, job_id=job.id)
    increment_metric("ingestion_direct_dispatch_total")
    return AcceptedResponse(status="processing", job_id=job.id, reason="direct_dispatch")


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str) -> JobStatusResponse:
    result = AsyncResult(job_id, app=celery_app)
    state = result.state

    if state == "PENDING":
        return JobStatusResponse(status="processing", state=state)

    if state in {"RECEIVED", "STARTED", "RETRY"}:
        return JobStatusResponse(status="processing", state=state)

    if state == "SUCCESS":
        task_result = result.result if isinstance(result.result, dict) else {"result": result.result}
        return JobStatusResponse(status="completed", state=state, result=task_result)

    if state == "FAILURE":
        return JobStatusResponse(status="failed", state=state, error=str(result.result))

    raise HTTPException(status_code=500, detail=f"Unexpected task state: {state}")


@app.post("/rag/documents/ingest", response_model=AcceptedResponse, status_code=202)
def rag_ingest_document(request: RagIngestRequest) -> AcceptedResponse:
    increment_metric("rag_ingest_endpoint_hits_total")
    job = rag_ingest_document_task.delay(request.model_dump(mode="json"))
    return AcceptedResponse(status="processing", job_id=job.id, reason="rag_ingest_queued")


@app.post("/rag/query", response_model=RagQueryResponse)
def rag_query(request: RagQueryRequest) -> RagQueryResponse:
    top_k = request.top_k or int(settings.rag_default_top_k)

    try:
        payload = query_rag_pipeline(query=request.query, top_k=top_k, rerank=request.rerank)
    except Exception as exc:  # noqa: BLE001
        push_dead_letter(
            stage="rag_query_endpoint",
            reason="rag_query_failure",
            payload=request.model_dump(mode="json"),
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="rag_query_failure") from exc

    status = str(payload.get("status", "ok"))
    if status == "rejected":
        raise HTTPException(status_code=400, detail=str(payload.get("reason", "invalid_query")))

    return RagQueryResponse(
        status=status,
        query=str(payload.get("query", request.query)),
        retrieval=payload.get("retrieval", {}) if isinstance(payload.get("retrieval"), dict) else {},
        results=payload.get("results", []) if isinstance(payload.get("results"), list) else [],
    )


@app.get("/ops/metrics")
def ops_metrics() -> dict[str, object]:
    return {
        "metrics": get_metrics_snapshot(),
        "circuit_breakers": {
            "openrouter": get_circuit_breaker_state("openrouter"),
        },
    }


@app.get("/ops/dead-letter")
def ops_dead_letter(limit: int = 50) -> dict[str, object]:
    events = list_recent_dead_letter_events(limit=limit)
    return {
        "count": len(events),
        "events": events,
    }
