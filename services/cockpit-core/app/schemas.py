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
    replace_existing_document: bool = False
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


class GoogleAuthUrlRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    redirect_uri: str | None = None
    scopes: list[str] | None = None


class GoogleAuthUrlResponse(BaseModel):
    status: Literal["ready"] = "ready"
    state: str
    auth_url: str
    scopes: list[str] = Field(default_factory=list)


class GoogleOAuthExchangeRequest(BaseModel):
    state: str = Field(..., min_length=8)
    code: str = Field(..., min_length=8)
    redirect_uri: str | None = None


class GoogleAccountResponse(BaseModel):
    id: int
    user_id: str
    provider: str
    google_email: str
    google_subject: str | None = None
    display_name: str | None = None
    scopes: list[str] = Field(default_factory=list)
    status: str
    token_expiry: str | None = None
    has_refresh_token: bool = False
    created_at: str
    updated_at: str


class GoogleOAuthExchangeResponse(BaseModel):
    status: Literal["processing"] = "processing"
    account: GoogleAccountResponse
    job_id: str | None = None
    reason: str | None = None


class GoogleAccountsResponse(BaseModel):
    count: int
    accounts: list[GoogleAccountResponse] = Field(default_factory=list)


class GoogleManualSyncRequest(BaseModel):
    providers: list[Literal["gmail", "drive", "calendar"]] = Field(
        default_factory=lambda: ["gmail", "drive", "calendar"]
    )
    bootstrap: bool = False


class GoogleSyncCursorResponse(BaseModel):
    provider: str
    cursor_key: str
    cursor_value: str
    updated_at: str


class GoogleRawEventResponse(BaseModel):
    event_uid: str
    provider: str
    resource_type: str
    external_id: str
    event_type: str
    source_cursor: str
    occurred_at: str | None = None
    created_at: str
    payload: Dict[str, Any] = Field(default_factory=dict)
