from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.config import settings


def _connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=True)


def ensure_schema() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_message_events (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                payload JSONB NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (source, source_message_id)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_message_jobs (
                source TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (source, source_message_id)
            );
            """
        )


def register_message_event(
    *,
    source: str,
    source_message_id: str,
    user_id: str,
    payload: dict[str, Any],
) -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_message_events (source, source_message_id, user_id, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source, source_message_id) DO NOTHING
            RETURNING id;
            """,
            (source, source_message_id, user_id, Jsonb(payload)),
        )
        return cur.fetchone() is not None


def map_job_to_message(*, source: str, source_message_id: str, job_id: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_message_jobs (source, source_message_id, job_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (source, source_message_id)
            DO UPDATE SET job_id = EXCLUDED.job_id;
            """,
            (source, source_message_id, job_id),
        )


def find_job_id(*, source: str, source_message_id: str) -> str | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id
            FROM cockpit_message_jobs
            WHERE source = %s AND source_message_id = %s;
            """,
            (source, source_message_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return str(row[0])
