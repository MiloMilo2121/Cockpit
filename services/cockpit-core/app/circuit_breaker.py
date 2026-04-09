from __future__ import annotations

import time
from typing import Dict

from app.config import settings
from app.redis_client import get_redis_client


def _open_key(name: str) -> str:
    return f"cockpit:cb:{name}:open_until"


def _failure_key(name: str) -> str:
    return f"cockpit:cb:{name}:failures"


def is_open(name: str) -> bool:
    client = get_redis_client()
    raw = client.get(_open_key(name))
    if not raw:
        return False

    try:
        open_until = int(raw)
    except ValueError:
        client.delete(_open_key(name))
        return False

    remaining = open_until - int(time.time())
    if remaining <= 0:
        client.delete(_open_key(name))
        return False
    return True


def record_failure(name: str) -> Dict[str, int | bool]:
    client = get_redis_client()
    failures = int(client.incr(_failure_key(name)))
    client.expire(_failure_key(name), max(settings.circuit_breaker_open_seconds * 3, 300))

    opened = False
    if failures >= settings.circuit_breaker_failure_threshold:
        open_until = int(time.time()) + settings.circuit_breaker_open_seconds
        client.set(_open_key(name), str(open_until), ex=settings.circuit_breaker_open_seconds)
        client.delete(_failure_key(name))
        opened = True

    return {"failures": failures, "opened": opened}


def record_success(name: str) -> None:
    client = get_redis_client()
    client.delete(_open_key(name))
    client.delete(_failure_key(name))


def get_state(name: str) -> Dict[str, int | str | bool]:
    client = get_redis_client()
    raw = client.get(_open_key(name))
    if not raw:
        failure_raw = client.get(_failure_key(name))
        failures = int(failure_raw) if failure_raw and str(failure_raw).isdigit() else 0
        return {"integration": name, "state": "closed", "is_open": False, "failures": failures, "open_for_seconds": 0}

    try:
        open_until = int(raw)
    except ValueError:
        client.delete(_open_key(name))
        return {"integration": name, "state": "closed", "is_open": False, "failures": 0, "open_for_seconds": 0}

    remaining = max(open_until - int(time.time()), 0)
    if remaining == 0:
        client.delete(_open_key(name))
        return {"integration": name, "state": "closed", "is_open": False, "failures": 0, "open_for_seconds": 0}

    return {
        "integration": name,
        "state": "open",
        "is_open": True,
        "failures": settings.circuit_breaker_failure_threshold,
        "open_for_seconds": remaining,
    }
