from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.qdrant_api_key:
        headers["api-key"] = settings.qdrant_api_key
    return headers


def _collection_url() -> str:
    return f"{settings.qdrant_url}/collections/{settings.rag_collection_name}"


def ensure_rag_collection() -> None:
    headers = _headers()
    url = _collection_url()

    response = httpx.get(url, headers=headers, timeout=10.0)
    if response.status_code == 200:
        return

    body = {
        "vectors": {
            "size": int(settings.rag_vector_size),
            "distance": "Cosine",
        }
    }
    create_response = httpx.put(url, headers=headers, json=body, timeout=20.0)
    if create_response.status_code not in {200, 201}:
        raise RuntimeError(f"qdrant_collection_create_failed:{create_response.status_code}:{create_response.text}")


def upsert_points(points: list[dict[str, Any]]) -> None:
    if not points:
        return

    ensure_rag_collection()
    headers = _headers()
    url = f"{_collection_url()}/points"
    body = {"points": points}

    response = httpx.put(url, headers=headers, json=body, params={"wait": "true"}, timeout=30.0)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"qdrant_upsert_failed:{response.status_code}:{response.text}")


def delete_points_by_document_id(document_id: str) -> None:
    ensure_rag_collection()
    headers = _headers()
    url = f"{_collection_url()}/points/delete"
    body = {
        "filter": {
            "must": [
                {
                    "key": "document_id",
                    "match": {"value": document_id},
                }
            ]
        }
    }

    response = httpx.post(url, headers=headers, json=body, params={"wait": "true"}, timeout=30.0)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"qdrant_delete_failed:{response.status_code}:{response.text}")


def search_dense(*, vector: list[float], limit: int) -> list[dict[str, Any]]:
    ensure_rag_collection()
    headers = _headers()
    url = f"{_collection_url()}/points/search"
    body = {
        "vector": vector,
        "limit": int(limit),
        "with_payload": True,
        "with_vector": False,
    }

    response = httpx.post(url, headers=headers, json=body, timeout=30.0)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"qdrant_search_failed:{response.status_code}:{response.text}")

    payload = response.json()
    raw_results = payload.get("result", [])
    if not isinstance(raw_results, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        normalized.append(item)

    return normalized
