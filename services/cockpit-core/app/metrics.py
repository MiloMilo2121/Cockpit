from __future__ import annotations

from typing import Dict

from app.redis_client import get_redis_client

_METRICS_KEY = "cockpit:metrics:counters"


def increment_metric(name: str, amount: int = 1) -> None:
    client = get_redis_client()
    client.hincrby(_METRICS_KEY, name, amount)


def get_metrics_snapshot() -> Dict[str, int]:
    client = get_redis_client()
    raw = client.hgetall(_METRICS_KEY)
    snapshot: Dict[str, int] = {}
    for key, value in raw.items():
        try:
            snapshot[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return snapshot
