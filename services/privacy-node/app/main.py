import os
import re
import time
import uuid
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from presidio_analyzer import AnalyzerEngine, RecognizerResult

app = FastAPI(title="life-cockpit-privacy-node", version="0.1.0")
analyzer = AnalyzerEngine()

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))

# In-memory, short-lived mapping for placeholder restoration.
# For horizontal scaling use Redis.
cache: Dict[str, Dict[str, object]] = {}


class RedactRequest(BaseModel):
    text: str = Field(..., min_length=1)
    language: str = "en"
    entities: Optional[List[str]] = None


class RestoreRequest(BaseModel):
    request_id: str
    text: str
    consume: bool = True


def _cleanup_cache() -> None:
    now = time.time()
    expired = [key for key, value in cache.items() if float(value["expires_at"]) <= now]
    for key in expired:
        del cache[key]


def _normalize_entity(entity_type: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", entity_type.upper()).strip("_")
    return normalized or "ENTITY"


def _select_non_overlapping(results: List[RecognizerResult]) -> List[RecognizerResult]:
    # Deterministic ordering: earliest first, then highest score, then longest span.
    ordered = sorted(results, key=lambda r: (r.start, -r.score, -(r.end - r.start)))
    accepted: List[RecognizerResult] = []
    last_end = 0

    for result in ordered:
        if result.start < last_end:
            continue
        accepted.append(result)
        last_end = result.end

    return accepted


def _redact_text(text: str, results: List[RecognizerResult]) -> tuple[str, Dict[str, str], List[Dict[str, object]]]:
    counters: Dict[str, int] = {}
    mapping: Dict[str, str] = {}
    spans: List[Dict[str, object]] = []

    chunks: List[str] = []
    cursor = 0

    for item in results:
        entity = _normalize_entity(item.entity_type)
        counters[entity] = counters.get(entity, 0) + 1
        token = f"<{entity}_{counters[entity]}>"

        original = text[item.start : item.end]
        chunks.append(text[cursor : item.start])
        chunks.append(token)

        mapping[token] = original
        spans.append(
            {
                "start": item.start,
                "end": item.end,
                "entity_type": entity,
                "score": item.score,
                "token": token,
            }
        )

        cursor = item.end

    chunks.append(text[cursor:])
    return "".join(chunks), mapping, spans


def _restore_text(text: str, mapping: Dict[str, str]) -> str:
    restored = text
    # Replace longer placeholders first to avoid accidental partial replacement.
    for token in sorted(mapping.keys(), key=len, reverse=True):
        restored = restored.replace(token, mapping[token])
    return restored


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/redact")
def redact(payload: RedactRequest) -> Dict[str, object]:
    _cleanup_cache()

    try:
        results = analyzer.analyze(
            text=payload.text,
            language=payload.language,
            entities=payload.entities,
            return_decision_process=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"PII analysis failed: {exc}") from exc

    filtered = _select_non_overlapping(results)
    redacted_text, mapping, spans = _redact_text(payload.text, filtered)

    request_id = str(uuid.uuid4())
    cache[request_id] = {
        "expires_at": time.time() + CACHE_TTL_SECONDS,
        "mapping": mapping,
    }

    return {
        "request_id": request_id,
        "redacted_text": redacted_text,
        "entities_detected": len(spans),
        "spans": spans,
        "expires_in_seconds": CACHE_TTL_SECONDS,
    }


@app.post("/restore")
def restore(payload: RestoreRequest) -> Dict[str, str]:
    _cleanup_cache()

    state = cache.get(payload.request_id)
    if not state:
        raise HTTPException(status_code=404, detail="request_id not found or expired")

    mapping = state["mapping"]
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=500, detail="corrupted mapping state")

    restored_text = _restore_text(payload.text, mapping)

    if payload.consume:
        del cache[payload.request_id]

    return {
        "request_id": payload.request_id,
        "restored_text": restored_text,
    }
