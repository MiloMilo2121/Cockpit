from __future__ import annotations

import redis

from app.config import settings


def get_redis_client(db: int = 0) -> redis.Redis:
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password or None,
        db=db,
        decode_responses=True,
    )
