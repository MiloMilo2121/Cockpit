from __future__ import annotations

import json
import re

from app.config import settings
from app.openrouter_client import OpenRouterError, chat_completion
from app.rag_embeddings import embed_text

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    raw = _SENTENCE_SPLIT_RE.split(text.strip())
    return [sentence.strip() for sentence in raw if sentence.strip()]


def recursive_chunk_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []

    chunk_size = max(int(settings.rag_chunk_size_chars), 200)
    overlap = max(int(settings.rag_chunk_overlap_chars), 0)

    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))

        if end < len(text):
            boundary = text.rfind("\n", start, end)
            if boundary == -1:
                boundary = text.rfind(" ", start, end)
            if boundary != -1 and boundary > start + int(chunk_size * 0.55):
                end = boundary

        candidate = text[start:end].strip()
        if candidate:
            chunks.append(candidate)

        if end >= len(text):
            break

        start = max(end - overlap, start + 1)

    return chunks


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))


def semantic_chunk_text(text: str) -> list[str]:
    sentences = _split_sentences(text)
    if len(sentences) <= 3:
        return recursive_chunk_text(text)

    threshold = float(settings.rag_semantic_similarity_threshold)

    chunks: list[str] = []
    current_sentences: list[str] = [sentences[0]]
    current_vector = embed_text(sentences[0])

    for sentence in sentences[1:]:
        sentence_vector = embed_text(sentence)
        similarity = _cosine_similarity(current_vector, sentence_vector)

        current_text = " ".join(current_sentences)
        if similarity < threshold or len(current_text) >= int(settings.rag_chunk_size_chars):
            chunks.append(current_text.strip())
            current_sentences = [sentence]
            current_vector = sentence_vector
            continue

        current_sentences.append(sentence)
        current_vector = embed_text(" ".join(current_sentences))

    final_text = " ".join(current_sentences).strip()
    if final_text:
        chunks.append(final_text)

    # Safety net in case semantic chunks are still too large.
    flattened: list[str] = []
    for chunk in chunks:
        if len(chunk) <= int(settings.rag_chunk_size_chars) * 2:
            flattened.append(chunk)
        else:
            flattened.extend(recursive_chunk_text(chunk))

    return [chunk for chunk in flattened if chunk.strip()]


def _extract_string_array(raw: str) -> list[str] | None:
    stripped = raw.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            values = [str(item).strip() for item in parsed if str(item).strip()]
            if values:
                return values
    except json.JSONDecodeError:
        pass

    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None

    fragment = stripped[start : end + 1]
    try:
        parsed = json.loads(fragment)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, list):
        return None

    values = [str(item).strip() for item in parsed if str(item).strip()]
    return values or None


def agentic_chunk_text(text: str) -> list[str]:
    trimmed = text.strip()
    if not trimmed:
        return []

    max_chars = max(int(settings.rag_agentic_chunk_max_chars), 2000)
    limited = trimmed[:max_chars]

    system_prompt = (
        "You are a chunking engine. "
        "Split the input into semantically coherent chunks for retrieval. "
        "Return ONLY a JSON array of chunk strings. No markdown, no extra keys."
    )
    user_prompt = (
        "Input text:\n"
        f"{limited}\n\n"
        f"Target chunk size around {settings.rag_chunk_size_chars} characters with overlap-aware semantics."
    )

    try:
        raw, _model = chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=1200,
        )
    except OpenRouterError:
        return semantic_chunk_text(trimmed)

    parsed = _extract_string_array(raw)
    if not parsed:
        return semantic_chunk_text(trimmed)

    cleaned = [chunk.strip() for chunk in parsed if chunk.strip()]
    if not cleaned:
        return semantic_chunk_text(trimmed)

    return cleaned


def chunk_document(text: str, strategy: str) -> tuple[list[str], str]:
    normalized = strategy.strip().lower()

    if normalized == "agentic":
        chunks = agentic_chunk_text(text)
    elif normalized == "semantic":
        chunks = semantic_chunk_text(text)
    else:
        normalized = "recursive"
        chunks = recursive_chunk_text(text)

    non_empty = [chunk.strip() for chunk in chunks if chunk.strip()]
    return non_empty, normalized
