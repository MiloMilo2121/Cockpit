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
    get_dashboard_counts,
    get_google_account,
    list_google_accounts,
    list_recent_message_events,
    list_recent_raw_events,
    list_recent_raw_events_global,
    list_sync_cursors,
    list_recent_dead_letter_events,
    map_job_to_message,
    register_message_event,
)
from app.event_utils import extract_source_message_id, self_message_reason
from app.google_auth import exchange_google_auth_code, prepare_google_auth_url
from app.google_client import GoogleAuthError
from app.metrics import get_metrics_snapshot, increment_metric
from app.rag_pipeline import query_rag_pipeline
from app.rag_store import ensure_rag_collection
from app.schemas import (
    AcceptedResponse,
    GoogleAccountResponse,
    GoogleAccountsResponse,
    GoogleAuthUrlRequest,
    GoogleAuthUrlResponse,
    GoogleManualSyncRequest,
    GoogleOAuthExchangeRequest,
    GoogleOAuthExchangeResponse,
    GoogleRawEventResponse,
    GoogleSyncCursorResponse,
    IngestionEvent,
    JobStatusResponse,
    RagIngestRequest,
    RagQueryRequest,
    RagQueryResponse,
)
from app.tasks import (
    process_buffered_session,
    process_ingestion_event,
    rag_ingest_document as rag_ingest_document_task,
    sync_google_account as sync_google_account_task,
)

app = FastAPI(title="cockpit-core-api", version="0.4.0")


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


@app.get("/dashboard/overview")
def dashboard_overview(limit: int = 12) -> dict[str, object]:
    safe_limit = min(max(limit, 4), 30)
    counts = get_dashboard_counts()
    metrics = get_metrics_snapshot()
    dead_letters = list_recent_dead_letter_events(limit=safe_limit)
    raw_events = list_recent_raw_events_global(limit=safe_limit)
    message_events = list_recent_message_events(limit=safe_limit)
    accounts = list_google_accounts()
    circuit_state = get_circuit_breaker_state("openrouter")

    command_feed: list[dict[str, object]] = []

    for item in raw_events:
        command_feed.append(
            {
                "kind": "external_event",
                "headline": f"{item['provider']}::{item['resource_type']}::{item['event_type']}",
                "subline": item["external_id"],
                "timestamp": item["created_at"],
                "severity": "high" if str(item["event_type"]).endswith("removed") else "normal",
            }
        )

    for item in message_events:
        payload = item["payload"] if isinstance(item.get("payload"), dict) else {}
        command_feed.append(
            {
                "kind": "message_event",
                "headline": f"{item['source']} incoming",
                "subline": str(payload.get("message", ""))[:140],
                "timestamp": item["received_at"],
                "severity": "normal",
            }
        )

    for item in dead_letters:
        command_feed.append(
            {
                "kind": "dead_letter",
                "headline": f"{item['stage']}::{item['reason']}",
                "subline": item["error"] or "no_error_text",
                "timestamp": item["created_at"],
                "severity": "critical",
            }
        )

    command_feed.sort(key=lambda row: str(row.get("timestamp", "")), reverse=True)

    posture = "nominal"
    if dead_letters:
        posture = "attention"
    if str(circuit_state.get("state", "closed")) == "open":
        posture = "degraded"

    return {
        "posture": posture,
        "counts": counts,
        "metrics": metrics,
        "circuit_breakers": {"openrouter": circuit_state},
        "accounts": accounts,
        "command_feed": command_feed[:safe_limit],
    }


@app.post("/integrations/google/auth-url", response_model=GoogleAuthUrlResponse)
def google_auth_url(request: GoogleAuthUrlRequest) -> GoogleAuthUrlResponse:
    try:
        payload = prepare_google_auth_url(
            user_id=request.user_id,
            redirect_uri=request.redirect_uri,
            scopes=request.scopes,
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return GoogleAuthUrlResponse(
        state=str(payload["state"]),
        auth_url=str(payload["auth_url"]),
        scopes=payload.get("scopes", []) if isinstance(payload.get("scopes"), list) else [],
    )


@app.post("/integrations/google/exchange", response_model=GoogleOAuthExchangeResponse)
def google_exchange(request: GoogleOAuthExchangeRequest) -> GoogleOAuthExchangeResponse:
    try:
        account = exchange_google_auth_code(
            state=request.state,
            code=request.code,
            redirect_uri=request.redirect_uri,
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = sync_google_account_task.delay(int(account["id"]), ["gmail", "drive", "calendar"], True)
    response_account = GoogleAccountResponse(**{key: value for key, value in account.items() if key != "access_token" and key != "refresh_token"})
    return GoogleOAuthExchangeResponse(
        account=response_account,
        job_id=job.id,
        reason="google_bootstrap_sync_queued",
    )


@app.get("/google/callback", response_model=GoogleOAuthExchangeResponse)
def google_callback(state: str, code: str) -> GoogleOAuthExchangeResponse:
    try:
        account = exchange_google_auth_code(
            state=state,
            code=code,
            redirect_uri=None,
        )
    except GoogleAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = sync_google_account_task.delay(int(account["id"]), ["gmail", "drive", "calendar"], True)
    response_account = GoogleAccountResponse(**{key: value for key, value in account.items() if key != "access_token" and key != "refresh_token"})
    return GoogleOAuthExchangeResponse(
        account=response_account,
        job_id=job.id,
        reason="google_bootstrap_sync_queued",
    )


@app.get("/integrations/google/accounts", response_model=GoogleAccountsResponse)
def google_accounts(user_id: str | None = None) -> GoogleAccountsResponse:
    accounts = list_google_accounts(user_id=user_id)
    return GoogleAccountsResponse(
        count=len(accounts),
        accounts=[GoogleAccountResponse(**account) for account in accounts],
    )


@app.post("/integrations/google/accounts/{account_id}/sync", response_model=AcceptedResponse, status_code=202)
def google_sync(account_id: int, request: GoogleManualSyncRequest) -> AcceptedResponse:
    account = get_google_account(account_id, include_tokens=False)
    if not account:
        raise HTTPException(status_code=404, detail="google_account_not_found")

    job = sync_google_account_task.delay(account_id, request.providers, request.bootstrap)
    return AcceptedResponse(status="processing", job_id=job.id, reason="google_sync_queued")


@app.get("/integrations/google/accounts/{account_id}/cursors", response_model=list[GoogleSyncCursorResponse])
def google_cursors(account_id: int) -> list[GoogleSyncCursorResponse]:
    account = get_google_account(account_id, include_tokens=False)
    if not account:
        raise HTTPException(status_code=404, detail="google_account_not_found")

    cursors = list_sync_cursors(account_id=account_id)
    return [GoogleSyncCursorResponse(**cursor) for cursor in cursors]


@app.get("/integrations/google/accounts/{account_id}/events", response_model=list[GoogleRawEventResponse])
def google_events(account_id: int, limit: int = 50) -> list[GoogleRawEventResponse]:
    account = get_google_account(account_id, include_tokens=False)
    if not account:
        raise HTTPException(status_code=404, detail="google_account_not_found")

    events = list_recent_raw_events(account_id=account_id, limit=limit)
    return [GoogleRawEventResponse(**event) for event in events]
