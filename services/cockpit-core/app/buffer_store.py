from __future__ import annotations

import json
from typing import Any

import redis

from app.config import settings


def _redis() -> redis.Redis:
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        db=0,
        decode_responses=True,
    )


def _buffer_key(source: str, user_id: str) -> str:
    return f"cockpit:buffer:{source}:{user_id}"


def _job_key(source: str, user_id: str) -> str:
    return f"cockpit:buffer-job:{source}:{user_id}"


def append_buffered_event(*, source: str, user_id: str, event: dict[str, Any]) -> None:
    client = _redis()
    key = _buffer_key(source, user_id)
    client.rpush(key, json.dumps(event))
    client.expire(key, settings.smart_buffer_ttl_seconds)


def try_claim_buffer_job(*, source: str, user_id: str, job_id: str) -> bool:
    client = _redis()
    key = _job_key(source, user_id)
    return bool(client.set(key, job_id, nx=True, ex=settings.smart_buffer_ttl_seconds))


def get_buffer_job_id(*, source: str, user_id: str) -> str | None:
    client = _redis()
    key = _job_key(source, user_id)
    value = client.get(key)
    if not value:
        return None
    return str(value)


def clear_buffer_job(*, source: str, user_id: str) -> None:
    client = _redis()
    client.delete(_job_key(source, user_id))


def consume_buffered_events(*, source: str, user_id: str) -> list[dict[str, Any]]:
    client = _redis()
    list_key = _buffer_key(source, user_id)
    job_key = _job_key(source, user_id)

    pipe = client.pipeline(transaction=True)
    pipe.lrange(list_key, 0, -1)
    pipe.delete(list_key)
    pipe.delete(job_key)
    results = pipe.execute()

    raw_events = results[0] if results else []
    events: list[dict[str, Any]] = []
    for raw in raw_events:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                events.append(parsed)
        except json.JSONDecodeError:
            continue
    return events
