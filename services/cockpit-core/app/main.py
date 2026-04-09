from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException

from app.celery_app import celery_app
from app.schemas import AcceptedResponse, IngestionEvent, JobStatusResponse
from app.tasks import process_ingestion_event

app = FastAPI(title="cockpit-core-api", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/inbox", response_model=AcceptedResponse, status_code=202)
def ingest_event(event: IngestionEvent) -> AcceptedResponse:
    job = process_ingestion_event.delay(event.model_dump(mode="json"))
    return AcceptedResponse(job_id=job.id)


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
