from datetime import datetime
from typing import Any, Dict, Literal

from pydantic import BaseModel, Field


class IngestionEvent(BaseModel):
    source: Literal["whatsapp", "web", "cron", "system"]
    user_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=datetime.utcnow)


class AcceptedResponse(BaseModel):
    status: Literal["processing", "duplicate", "ignored"] = "processing"
    job_id: str | None = None
    reason: str | None = None


class JobStatusResponse(BaseModel):
    status: str
    state: str
    result: Dict[str, Any] | None = None
    error: str | None = None


class RagIngestRequest(BaseModel):
    document_id: str | None = None
    title: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    chunking_strategy: Literal["recursive", "semantic", "agentic"] = "semantic"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RagQueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=6, ge=1, le=50)
    rerank: bool = True


class RagQueryResponse(BaseModel):
    status: str
    query: str
    retrieval: Dict[str, Any] = Field(default_factory=dict)
    results: list[Dict[str, Any]] = Field(default_factory=list)
