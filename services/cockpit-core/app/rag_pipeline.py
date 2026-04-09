from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.dead_letter import push_dead_letter
from app.metrics import increment_metric
from app.openrouter_client import OpenRouterError, chat_completion
from app.rag_chunking import chunk_document
from app.rag_embeddings import embed_text, tokenize_text
from app.rag_store import delete_points_by_document_id, search_dense, upsert_points


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_chunk_id(document_id: str, chunk_index: int, chunk_text: str) -> str:
    digest = hashlib.sha1(chunk_text.encode("utf-8")).hexdigest()[:12]
    return f"{document_id}:{chunk_index}:{digest}"


def _string(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    stripped = raw.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    fragment = stripped[start : end + 1]
    try:
        parsed = json.loads(fragment)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict):
        return parsed
    return None


def ingest_document_pipeline(request: dict[str, Any]) -> dict[str, Any]:
    increment_metric("rag_ingest_requests_total")

    title = _string(request.get("title"), "untitled")
    source = _string(request.get("source"), "unknown")
    content = _string(request.get("content"))
    strategy = _string(request.get("chunking_strategy"), "recursive").lower()
    replace_existing_document = bool(request.get("replace_existing_document", False))

    if not content:
        increment_metric("rag_ingest_rejected_total")
        return {"status": "rejected", "reason": "empty_content"}

    document_id = _string(request.get("document_id"))
    if not document_id:
        base = f"{title}|{source}|{content[:200]}"
        document_id = hashlib.sha1(base.encode("utf-8")).hexdigest()

    metadata = request.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    chunks, normalized_strategy = chunk_document(content, strategy)
    if not chunks:
        increment_metric("rag_ingest_rejected_total")
        return {"status": "rejected", "reason": "chunking_no_output", "document_id": document_id}

    ingested_at = _utc_now_iso()
    points: list[dict[str, Any]] = []
    total_chunks = len(chunks)

    for idx, chunk in enumerate(chunks):
        chunk_text = chunk.strip()
        if not chunk_text:
            continue

        chunk_id = _stable_chunk_id(document_id, idx, chunk_text)
        payload = {
            "document_id": document_id,
            "document_title": title,
            "source": source,
            "chunk_index": idx,
            "total_chunks": total_chunks,
            "chunking_strategy": normalized_strategy,
            "timestamp": ingested_at,
            "confidence_score": 1.0,
            "text": chunk_text,
            "metadata": metadata,
        }
        points.append(
            {
                "id": chunk_id,
                "vector": embed_text(chunk_text),
                "payload": payload,
            }
        )

    if not points:
        increment_metric("rag_ingest_rejected_total")
        return {"status": "rejected", "reason": "empty_chunks", "document_id": document_id}

    if replace_existing_document:
        delete_points_by_document_id(document_id)

    upsert_points(points)

    increment_metric("rag_ingest_documents_total")
    increment_metric("rag_ingest_chunks_total", amount=len(points))

    return {
        "status": "indexed",
        "document_id": document_id,
        "document_title": title,
        "source": source,
        "chunking_strategy": normalized_strategy,
        "chunks_indexed": len(points),
        "ingested_at": ingested_at,
    }


def _normalize_dense_scores(candidates: list[dict[str, Any]]) -> None:
    dense_scores = [float(item.get("dense_score", 0.0)) for item in candidates]
    if not dense_scores:
        return

    minimum = min(dense_scores)
    maximum = max(dense_scores)
    span = maximum - minimum

    for item in candidates:
        dense = float(item.get("dense_score", 0.0))
        if span <= 1e-9:
            item["dense_norm"] = 1.0
        else:
            item["dense_norm"] = (dense - minimum) / span


def _sparse_score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0

    candidate_tokens = set(tokenize_text(text))
    if not candidate_tokens:
        return 0.0

    overlap = len(query_tokens.intersection(candidate_tokens))
    return overlap / float(max(len(query_tokens), 1))


def _rerank_with_openrouter(query: str, candidates: list[dict[str, Any]]) -> list[str] | None:
    if not candidates:
        return None

    short_candidates = candidates[: max(int(settings.rag_rerank_candidates), 2)]
    compact = [
        {
            "id": item["id"],
            "text": _string(item.get("text"))[:280],
            "hybrid_score": round(float(item.get("hybrid_score", 0.0)), 6),
        }
        for item in short_candidates
    ]

    system_prompt = (
        "You are a retrieval reranker. "
        "Given a query and candidate chunks, return strict JSON only: "
        "{\"ordered_ids\": [\"id1\", \"id2\", ...]}."
    )
    user_prompt = (
        f"Query:\n{query}\n\n"
        f"Candidates JSON:\n{json.dumps(compact, ensure_ascii=True)}\n\n"
        "Order ids by relevance. Include only ids from candidates."
    )

    raw, _model = chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=700,
    )

    parsed = _extract_json_object(raw)
    if not parsed:
        return None

    ordered_ids = parsed.get("ordered_ids")
    if not isinstance(ordered_ids, list):
        return None

    allowed = {str(item["id"]) for item in short_candidates}
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in ordered_ids:
        candidate_id = str(value)
        if candidate_id not in allowed or candidate_id in seen:
            continue
        seen.add(candidate_id)
        cleaned.append(candidate_id)

    return cleaned or None


def query_rag_pipeline(*, query: str, top_k: int, rerank: bool) -> dict[str, Any]:
    increment_metric("rag_query_requests_total")

    normalized_query = query.strip()
    if not normalized_query:
        return {"status": "rejected", "reason": "empty_query", "results": []}

    query_vector = embed_text(normalized_query)
    dense_limit = max(int(top_k) * 4, int(settings.rag_query_candidates), 4)

    dense_results = search_dense(vector=query_vector, limit=dense_limit)
    if not dense_results:
        increment_metric("rag_query_empty_total")
        return {
            "status": "knowledge_gap",
            "query": normalized_query,
            "results": [],
            "retrieval": {"dense_candidates": 0, "reranked": False},
        }

    query_tokens = set(tokenize_text(normalized_query))

    candidates: list[dict[str, Any]] = []
    for item in dense_results:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        text = _string(payload.get("text"))
        candidate_id = _string(item.get("id"))
        if not text or not candidate_id:
            continue

        dense_score = float(item.get("score", 0.0))
        sparse = _sparse_score(query_tokens, text)

        candidates.append(
            {
                "id": candidate_id,
                "dense_score": dense_score,
                "sparse_score": sparse,
                "text": text,
                "payload": payload,
            }
        )

    if not candidates:
        increment_metric("rag_query_empty_total")
        return {
            "status": "knowledge_gap",
            "query": normalized_query,
            "results": [],
            "retrieval": {"dense_candidates": 0, "reranked": False},
        }

    _normalize_dense_scores(candidates)
    dense_weight = float(settings.rag_dense_weight)
    sparse_weight = float(settings.rag_sparse_weight)

    for item in candidates:
        dense_norm = float(item.get("dense_norm", 0.0))
        sparse_score = float(item.get("sparse_score", 0.0))
        item["hybrid_score"] = dense_weight * dense_norm + sparse_weight * sparse_score

    candidates.sort(key=lambda row: float(row.get("hybrid_score", 0.0)), reverse=True)

    reranked = False
    if rerank and len(candidates) > 1:
        try:
            ordered_ids = _rerank_with_openrouter(normalized_query, candidates)
            if ordered_ids:
                reranked = True
                rank_map = {cid: idx for idx, cid in enumerate(ordered_ids)}
                max_rank = len(ordered_ids)

                for item in candidates:
                    cid = str(item["id"])
                    if cid in rank_map:
                        item["rerank_bonus"] = (max_rank - rank_map[cid]) / float(max_rank)
                    else:
                        item["rerank_bonus"] = 0.0
                    item["final_score"] = float(item["hybrid_score"]) + 0.15 * float(item["rerank_bonus"])

                candidates.sort(key=lambda row: float(row.get("final_score", row.get("hybrid_score", 0.0))), reverse=True)
        except OpenRouterError as exc:
            push_dead_letter(
                stage="rag_rerank",
                reason="openrouter_rerank_failure",
                payload={"query": normalized_query, "candidates": len(candidates)},
                error=str(exc),
            )

    final_top_k = max(int(top_k), 1)
    selected = candidates[:final_top_k]

    results: list[dict[str, Any]] = []
    for item in selected:
        payload = item["payload"] if isinstance(item["payload"], dict) else {}
        results.append(
            {
                "id": str(item["id"]),
                "score": round(float(item.get("final_score", item.get("hybrid_score", 0.0))), 6),
                "dense_score": round(float(item.get("dense_score", 0.0)), 6),
                "sparse_score": round(float(item.get("sparse_score", 0.0)), 6),
                "text": _string(item.get("text")),
                "document_id": _string(payload.get("document_id")),
                "document_title": _string(payload.get("document_title")),
                "source": _string(payload.get("source")),
                "chunk_index": int(payload.get("chunk_index", 0)),
                "timestamp": _string(payload.get("timestamp")),
                "confidence_score": float(payload.get("confidence_score", 1.0)),
                "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            }
        )

    increment_metric("rag_query_success_total")

    return {
        "status": "ok",
        "query": normalized_query,
        "results": results,
        "retrieval": {
            "dense_candidates": len(candidates),
            "reranked": reranked,
            "top_k": final_top_k,
        },
    }
