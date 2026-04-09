from __future__ import annotations

import math
import re
from collections import Counter

from app.config import settings

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)


def tokenize_text(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def embed_text(text: str) -> list[float]:
    dim = max(int(settings.rag_vector_size), 8)
    tokens = tokenize_text(text)
    if not tokens:
        return [0.0] * dim

    vector = [0.0] * dim
    counts = Counter(tokens)

    for token, freq in counts.items():
        idx = hash(token) % dim
        vector[idx] += float(freq)

    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return [0.0] * dim

    return [value / norm for value in vector]
