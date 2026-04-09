from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.config import settings


def _connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=True)


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _google_account_from_row(row: tuple[Any, ...], *, include_tokens: bool = False) -> dict[str, Any]:
    payload = {
        "id": int(row[0]),
        "user_id": str(row[1]),
        "provider": str(row[2]),
        "google_email": str(row[3]),
        "google_subject": None if row[4] is None else str(row[4]),
        "display_name": None if row[5] is None else str(row[5]),
        "token_type": None if row[8] is None else str(row[8]),
        "token_expiry": _iso_or_none(row[9]),
        "scopes": row[10] if isinstance(row[10], list) else [],
        "status": str(row[11]),
        "created_at": _iso_or_none(row[12]),
        "updated_at": _iso_or_none(row[13]),
        "has_refresh_token": bool(row[7]),
    }
    if include_tokens:
        payload["access_token"] = str(row[6])
        payload["refresh_token"] = None if row[7] is None else str(row[7])
    return payload


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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_dead_letter_events (
                id BIGSERIAL PRIMARY KEY,
                stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload JSONB NOT NULL,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_google_oauth_states (
                state TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                scopes JSONB NOT NULL,
                redirect_uri TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                consumed_at TIMESTAMPTZ
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_google_accounts (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'google',
                google_email TEXT NOT NULL,
                google_subject TEXT,
                display_name TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                token_type TEXT,
                token_expiry TIMESTAMPTZ,
                scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (provider, user_id, google_email)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cockpit_google_accounts_user_id
            ON cockpit_google_accounts (user_id);
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_sync_cursors (
                account_id BIGINT NOT NULL REFERENCES cockpit_google_accounts(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                cursor_key TEXT NOT NULL,
                cursor_value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (account_id, provider, cursor_key)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_raw_events (
                event_uid TEXT PRIMARY KEY,
                account_id BIGINT NOT NULL REFERENCES cockpit_google_accounts(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source_cursor TEXT NOT NULL DEFAULT '',
                payload JSONB NOT NULL,
                occurred_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cockpit_external_documents (
                account_id BIGINT NOT NULL REFERENCES cockpit_google_accounts(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                external_document_id TEXT NOT NULL,
                title TEXT NOT NULL,
                mime_type TEXT,
                content TEXT NOT NULL DEFAULT '',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (account_id, provider, external_document_id)
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


def insert_dead_letter_event(
    *,
    stage: str,
    reason: str,
    payload: dict[str, Any],
    error: str | None = None,
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_dead_letter_events (stage, reason, payload, error)
            VALUES (%s, %s, %s, %s);
            """,
            (stage, reason, Jsonb(payload), error),
        )


def list_recent_dead_letter_events(limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = min(max(limit, 1), 200)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, stage, reason, payload, error, created_at
            FROM cockpit_dead_letter_events
            ORDER BY id DESC
            LIMIT %s;
            """,
            (safe_limit,),
        )
        rows = cur.fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "id": int(row[0]),
                "stage": str(row[1]),
                "reason": str(row[2]),
                "payload": row[3] if isinstance(row[3], dict) else {},
                "error": None if row[4] is None else str(row[4]),
                "created_at": _iso_or_none(row[5]),
            }
        )
    return result


def list_recent_message_events(limit: int = 20) -> list[dict[str, Any]]:
    safe_limit = min(max(limit, 1), 200)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, source_message_id, user_id, payload, received_at
            FROM cockpit_message_events
            ORDER BY received_at DESC
            LIMIT %s;
            """,
            (safe_limit,),
        )
        rows = cur.fetchall()

    return [
        {
            "source": str(row[0]),
            "source_message_id": str(row[1]),
            "user_id": str(row[2]),
            "payload": row[3] if isinstance(row[3], dict) else {},
            "received_at": _iso_or_none(row[4]),
        }
        for row in rows
    ]


def create_google_oauth_state(
    *,
    state: str,
    user_id: str,
    scopes: list[str],
    redirect_uri: str | None,
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_google_oauth_states (state, user_id, scopes, redirect_uri)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (state) DO UPDATE
            SET user_id = EXCLUDED.user_id,
                scopes = EXCLUDED.scopes,
                redirect_uri = EXCLUDED.redirect_uri,
                created_at = NOW(),
                consumed_at = NULL;
            """,
            (state, user_id, Jsonb(scopes), redirect_uri),
        )


def consume_google_oauth_state(state: str) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE cockpit_google_oauth_states
            SET consumed_at = NOW()
            WHERE state = %s AND consumed_at IS NULL
            RETURNING state, user_id, scopes, redirect_uri, created_at;
            """,
            (state,),
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "state": str(row[0]),
        "user_id": str(row[1]),
        "scopes": row[2] if isinstance(row[2], list) else [],
        "redirect_uri": None if row[3] is None else str(row[3]),
        "created_at": _iso_or_none(row[4]),
    }


def upsert_google_account(
    *,
    user_id: str,
    google_email: str,
    google_subject: str | None,
    display_name: str | None,
    access_token: str,
    refresh_token: str | None,
    token_type: str | None,
    token_expiry: datetime | None,
    scopes: list[str],
) -> dict[str, Any]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_google_accounts (
                user_id,
                provider,
                google_email,
                google_subject,
                display_name,
                access_token,
                refresh_token,
                token_type,
                token_expiry,
                scopes,
                status,
                updated_at
            )
            VALUES (%s, 'google', %s, %s, %s, %s, %s, %s, %s, %s, 'active', NOW())
            ON CONFLICT (provider, user_id, google_email)
            DO UPDATE SET
                google_subject = EXCLUDED.google_subject,
                display_name = EXCLUDED.display_name,
                access_token = EXCLUDED.access_token,
                refresh_token = CASE
                    WHEN EXCLUDED.refresh_token IS NULL OR EXCLUDED.refresh_token = ''
                    THEN cockpit_google_accounts.refresh_token
                    ELSE EXCLUDED.refresh_token
                END,
                token_type = EXCLUDED.token_type,
                token_expiry = EXCLUDED.token_expiry,
                scopes = EXCLUDED.scopes,
                status = 'active',
                updated_at = NOW()
            RETURNING
                id,
                user_id,
                provider,
                google_email,
                google_subject,
                display_name,
                access_token,
                refresh_token,
                token_type,
                token_expiry,
                scopes,
                status,
                created_at,
                updated_at;
            """,
            (
                user_id,
                google_email,
                google_subject,
                display_name,
                access_token,
                refresh_token,
                token_type,
                token_expiry,
                Jsonb(scopes),
            ),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("google_account_upsert_failed")
    return _google_account_from_row(row, include_tokens=True)


def update_google_account_tokens(
    *,
    account_id: int,
    access_token: str,
    refresh_token: str | None,
    token_type: str | None,
    token_expiry: datetime | None,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE cockpit_google_accounts
            SET access_token = %s,
                refresh_token = CASE
                    WHEN %s IS NULL OR %s = ''
                    THEN refresh_token
                    ELSE %s
                END,
                token_type = %s,
                token_expiry = %s,
                scopes = CASE
                    WHEN %s IS NULL THEN scopes
                    ELSE %s
                END,
                updated_at = NOW()
            WHERE id = %s
            RETURNING
                id,
                user_id,
                provider,
                google_email,
                google_subject,
                display_name,
                access_token,
                refresh_token,
                token_type,
                token_expiry,
                scopes,
                status,
                created_at,
                updated_at;
            """,
            (
                access_token,
                refresh_token,
                refresh_token,
                refresh_token,
                token_type,
                token_expiry,
                None if scopes is None else Jsonb(scopes),
                None if scopes is None else Jsonb(scopes),
                int(account_id),
            ),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("google_account_update_failed")
    return _google_account_from_row(row, include_tokens=True)


def get_google_account(account_id: int, *, include_tokens: bool = False) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
                user_id,
                provider,
                google_email,
                google_subject,
                display_name,
                access_token,
                refresh_token,
                token_type,
                token_expiry,
                scopes,
                status,
                created_at,
                updated_at
            FROM cockpit_google_accounts
            WHERE id = %s;
            """,
            (int(account_id),),
        )
        row = cur.fetchone()

    if not row:
        return None
    return _google_account_from_row(row, include_tokens=include_tokens)


def list_google_accounts(*, user_id: str | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT
            id,
            user_id,
            provider,
            google_email,
            google_subject,
            display_name,
            access_token,
            refresh_token,
            token_type,
            token_expiry,
            scopes,
            status,
            created_at,
            updated_at
        FROM cockpit_google_accounts
    """
    params: tuple[Any, ...] = ()
    if user_id:
        query += " WHERE user_id = %s"
        params = (user_id,)
    query += " ORDER BY id DESC"

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    return [_google_account_from_row(row, include_tokens=False) for row in rows]


def upsert_sync_cursor(*, account_id: int, provider: str, cursor_key: str, cursor_value: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_sync_cursors (account_id, provider, cursor_key, cursor_value)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, provider, cursor_key)
            DO UPDATE SET cursor_value = EXCLUDED.cursor_value, updated_at = NOW();
            """,
            (int(account_id), provider, cursor_key, cursor_value),
        )


def delete_sync_cursor(*, account_id: int, provider: str, cursor_key: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM cockpit_sync_cursors
            WHERE account_id = %s AND provider = %s AND cursor_key = %s;
            """,
            (int(account_id), provider, cursor_key),
        )


def get_sync_cursor(*, account_id: int, provider: str, cursor_key: str) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT provider, cursor_key, cursor_value, updated_at
            FROM cockpit_sync_cursors
            WHERE account_id = %s AND provider = %s AND cursor_key = %s;
            """,
            (int(account_id), provider, cursor_key),
        )
        row = cur.fetchone()

    if not row:
        return None
    return {
        "provider": str(row[0]),
        "cursor_key": str(row[1]),
        "cursor_value": str(row[2]),
        "updated_at": _iso_or_none(row[3]),
    }


def list_sync_cursors(*, account_id: int) -> list[dict[str, Any]]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT provider, cursor_key, cursor_value, updated_at
            FROM cockpit_sync_cursors
            WHERE account_id = %s
            ORDER BY provider, cursor_key;
            """,
            (int(account_id),),
        )
        rows = cur.fetchall()

    return [
        {
            "provider": str(row[0]),
            "cursor_key": str(row[1]),
            "cursor_value": str(row[2]),
            "updated_at": _iso_or_none(row[3]),
        }
        for row in rows
    ]


def insert_raw_event(
    *,
    event_uid: str,
    account_id: int,
    provider: str,
    resource_type: str,
    external_id: str,
    event_type: str,
    source_cursor: str,
    payload: dict[str, Any],
    occurred_at: datetime | None,
) -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_raw_events (
                event_uid,
                account_id,
                provider,
                resource_type,
                external_id,
                event_type,
                source_cursor,
                payload,
                occurred_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_uid) DO NOTHING
            RETURNING event_uid;
            """,
            (
                event_uid,
                int(account_id),
                provider,
                resource_type,
                external_id,
                event_type,
                source_cursor,
                Jsonb(payload),
                occurred_at,
            ),
        )
        return cur.fetchone() is not None


def list_recent_raw_events(*, account_id: int, limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = min(max(limit, 1), 200)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_uid, provider, resource_type, external_id, event_type, source_cursor, occurred_at, created_at, payload
            FROM cockpit_raw_events
            WHERE account_id = %s
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (int(account_id), safe_limit),
        )
        rows = cur.fetchall()

    return [
        {
            "event_uid": str(row[0]),
            "provider": str(row[1]),
            "resource_type": str(row[2]),
            "external_id": str(row[3]),
            "event_type": str(row[4]),
            "source_cursor": str(row[5]),
            "occurred_at": _iso_or_none(row[6]),
            "created_at": _iso_or_none(row[7]),
            "payload": row[8] if isinstance(row[8], dict) else {},
        }
        for row in rows
    ]


def list_recent_raw_events_global(limit: int = 50) -> list[dict[str, Any]]:
    safe_limit = min(max(limit, 1), 200)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_uid, account_id, provider, resource_type, external_id, event_type, source_cursor, occurred_at, created_at, payload
            FROM cockpit_raw_events
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (safe_limit,),
        )
        rows = cur.fetchall()

    return [
        {
            "event_uid": str(row[0]),
            "account_id": int(row[1]),
            "provider": str(row[2]),
            "resource_type": str(row[3]),
            "external_id": str(row[4]),
            "event_type": str(row[5]),
            "source_cursor": str(row[6]),
            "occurred_at": _iso_or_none(row[7]),
            "created_at": _iso_or_none(row[8]),
            "payload": row[9] if isinstance(row[9], dict) else {},
        }
        for row in rows
    ]


def upsert_external_document(
    *,
    account_id: int,
    provider: str,
    external_document_id: str,
    title: str,
    mime_type: str | None,
    content: str,
    metadata: dict[str, Any],
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cockpit_external_documents (
                account_id,
                provider,
                external_document_id,
                title,
                mime_type,
                content,
                metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, provider, external_document_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                mime_type = EXCLUDED.mime_type,
                content = EXCLUDED.content,
                metadata = EXCLUDED.metadata,
                updated_at = NOW();
            """,
            (
                int(account_id),
                provider,
                external_document_id,
                title,
                mime_type,
                content,
                Jsonb(metadata),
            ),
        )


def get_dashboard_counts() -> dict[str, int]:
    queries = {
        "message_events": "SELECT COUNT(*) FROM cockpit_message_events;",
        "dead_letter_events": "SELECT COUNT(*) FROM cockpit_dead_letter_events;",
        "google_accounts": "SELECT COUNT(*) FROM cockpit_google_accounts;",
        "raw_events": "SELECT COUNT(*) FROM cockpit_raw_events;",
        "external_documents": "SELECT COUNT(*) FROM cockpit_external_documents;",
    }
    results: dict[str, int] = {}
    with _connect() as conn, conn.cursor() as cur:
        for key, query in queries.items():
            cur.execute(query)
            row = cur.fetchone()
            results[key] = int(row[0]) if row else 0
    return results
