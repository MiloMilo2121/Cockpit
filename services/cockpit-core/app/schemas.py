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
