from __future__ import annotations

import secrets
from typing import Any

from app.config import settings
from app.db import consume_google_oauth_state, create_google_oauth_state, upsert_google_account
from app.google_client import (
    GoogleAuthError,
    build_google_auth_url,
    exchange_google_code,
    fetch_google_userinfo,
    token_expiry_from_payload,
)


def prepare_google_auth_url(
    *,
    user_id: str,
    redirect_uri: str | None,
    scopes: list[str] | None,
) -> dict[str, Any]:
    resolved_redirect_uri = (redirect_uri or settings.google_oauth_redirect_url).strip()
    if not resolved_redirect_uri:
        raise GoogleAuthError("google_redirect_uri_not_configured")

    requested_scopes = scopes or settings.google_scopes
    if not requested_scopes:
        raise GoogleAuthError("google_scopes_not_configured")

    state = secrets.token_urlsafe(24)
    create_google_oauth_state(
        state=state,
        user_id=user_id,
        scopes=requested_scopes,
        redirect_uri=resolved_redirect_uri,
    )
    auth_url = build_google_auth_url(state=state, redirect_uri=resolved_redirect_uri, scopes=requested_scopes)
    return {
        "state": state,
        "auth_url": auth_url,
        "scopes": requested_scopes,
    }


def exchange_google_auth_code(
    *,
    state: str,
    code: str,
    redirect_uri: str | None,
) -> dict[str, Any]:
    oauth_state = consume_google_oauth_state(state)
    if not oauth_state:
        raise GoogleAuthError("google_oauth_state_invalid_or_consumed")

    resolved_redirect_uri = (redirect_uri or oauth_state.get("redirect_uri") or settings.google_oauth_redirect_url).strip()
    if not resolved_redirect_uri:
        raise GoogleAuthError("google_redirect_uri_not_configured")

    token_payload = exchange_google_code(code=code, redirect_uri=resolved_redirect_uri)
    access_token = str(token_payload.get("access_token", "")).strip()
    if not access_token:
        raise GoogleAuthError("google_access_token_missing_after_exchange")

    userinfo = fetch_google_userinfo(access_token)
    google_email = str(userinfo.get("email", "")).strip()
    if not google_email:
        raise GoogleAuthError("google_email_missing_from_userinfo")

    scopes_raw = str(token_payload.get("scope", "")).strip()
    scopes = [item for item in scopes_raw.split(" ") if item] or oauth_state.get("scopes", [])

    account = upsert_google_account(
        user_id=str(oauth_state["user_id"]),
        google_email=google_email,
        google_subject=None if userinfo.get("sub") is None else str(userinfo.get("sub")),
        display_name=None if userinfo.get("name") is None else str(userinfo.get("name")),
        access_token=access_token,
        refresh_token=None if token_payload.get("refresh_token") is None else str(token_payload.get("refresh_token")),
        token_type=None if token_payload.get("token_type") is None else str(token_payload.get("token_type")),
        token_expiry=token_expiry_from_payload(token_payload),
        scopes=scopes,
    )
    return account
