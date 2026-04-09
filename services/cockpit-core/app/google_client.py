from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.config import settings
from app.db import update_google_account_tokens

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleAuthError(RuntimeError):
    pass


class GoogleApiError(RuntimeError):
    pass


def _required_google_oauth() -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAuthError("google_oauth_credentials_not_configured")


def build_google_auth_url(*, state: str, redirect_uri: str, scopes: list[str]) -> str:
    _required_google_oauth()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_google_code(*, code: str, redirect_uri: str) -> dict[str, Any]:
    _required_google_oauth()
    response = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=float(settings.google_sync_http_timeout_seconds),
    )
    if response.status_code >= 400:
        raise GoogleAuthError(f"google_token_exchange_failed:{response.status_code}:{response.text[:280]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise GoogleAuthError("google_token_exchange_invalid_payload")
    return payload


def fetch_google_userinfo(access_token: str) -> dict[str, Any]:
    response = httpx.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=float(settings.google_sync_http_timeout_seconds),
    )
    if response.status_code >= 400:
        raise GoogleAuthError(f"google_userinfo_failed:{response.status_code}:{response.text[:280]}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise GoogleAuthError("google_userinfo_invalid_payload")
    return payload


def token_expiry_from_payload(payload: dict[str, Any]) -> datetime | None:
    expires_in = payload.get("expires_in")
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class GoogleAccountSession:
    def __init__(self, account: dict[str, Any]) -> None:
        self.account = dict(account)
        self._client = httpx.Client(timeout=float(settings.google_sync_http_timeout_seconds))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoogleAccountSession":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _access_token(self) -> str:
        token = str(self.account.get("access_token", "")).strip()
        if not token:
            self._refresh_access_token()
            token = str(self.account.get("access_token", "")).strip()
        if not token:
            raise GoogleAuthError("google_access_token_missing")
        return token

    def _token_is_expiring(self) -> bool:
        raw = self.account.get("token_expiry")
        if not raw:
            return False
        try:
            expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry <= datetime.now(timezone.utc) + timedelta(seconds=90)

    def _refresh_access_token(self) -> None:
        _required_google_oauth()
        refresh_token = str(self.account.get("refresh_token", "") or "").strip()
        if not refresh_token:
            raise GoogleAuthError("google_refresh_token_missing")

        response = self._client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            raise GoogleAuthError(f"google_refresh_failed:{response.status_code}:{response.text[:280]}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleAuthError("google_refresh_invalid_payload")

        updated = update_google_account_tokens(
            account_id=int(self.account["id"]),
            access_token=str(payload.get("access_token", "")),
            refresh_token=refresh_token,
            token_type=None if payload.get("token_type") is None else str(payload.get("token_type")),
            token_expiry=token_expiry_from_payload(payload),
            scopes=None,
        )
        self.account.update(updated)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> httpx.Response:
        if self._token_is_expiring():
            self._refresh_access_token()

        headers = {"Authorization": f"Bearer {self._access_token()}"}
        response = self._client.request(method, url, params=params, json=json, headers=headers)

        if response.status_code == 401 and retry_auth:
            self._refresh_access_token()
            return self.request(method, url, params=params, json=json, retry_auth=False)

        if response.status_code >= 400:
            raise GoogleApiError(f"google_api_failed:{response.status_code}:{url}:{response.text[:280]}")
        return response

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.request(method, url, params=params, json=json)
        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleApiError(f"google_api_invalid_payload:{url}")
        return payload

    def request_text(self, method: str, url: str, *, params: dict[str, Any] | None = None) -> str:
        response = self.request(method, url, params=params)
        return response.text

    def gmail_get_profile(self) -> dict[str, Any]:
        return self.request_json("GET", f"{GMAIL_API_BASE}/users/me/profile")

    def gmail_list_messages(self, *, page_token: str | None = None, max_results: int = 100, q: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        if q:
            params["q"] = q
        return self.request_json("GET", f"{GMAIL_API_BASE}/users/me/messages", params=params)

    def gmail_get_message(self, message_id: str) -> dict[str, Any]:
        return self.request_json(
            "GET",
            f"{GMAIL_API_BASE}/users/me/messages/{quote(message_id, safe='')}",
            params={"format": "full"},
        )

    def gmail_list_history(self, *, start_history_id: str, page_token: str | None = None, max_results: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "maxResults": max_results,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
        }
        if page_token:
            params["pageToken"] = page_token
        return self.request_json("GET", f"{GMAIL_API_BASE}/users/me/history", params=params)

    def drive_get_start_page_token(self) -> dict[str, Any]:
        return self.request_json(
            "GET",
            f"{DRIVE_API_BASE}/changes/startPageToken",
            params={"supportsAllDrives": "true"},
        )

    def drive_list_changes(self, *, page_token: str) -> dict[str, Any]:
        return self.request_json(
            "GET",
            f"{DRIVE_API_BASE}/changes",
            params={
                "pageToken": page_token,
                "pageSize": max(int(settings.google_drive_bootstrap_page_size), 25),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": (
                    "nextPageToken,newStartPageToken,"
                    "changes(fileId,removed,time,file(id,name,mimeType,modifiedTime,size,webViewLink,trashed))"
                ),
            },
        )

    def drive_list_files(self, *, page_token: str | None = None, page_size: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageSize": page_size or int(settings.google_drive_bootstrap_page_size),
            "orderBy": "modifiedTime desc",
            "q": "trashed=false",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": (
                "nextPageToken,"
                "files(id,name,mimeType,modifiedTime,size,webViewLink,createdTime,trashed)"
            ),
        }
        if page_token:
            params["pageToken"] = page_token
        return self.request_json("GET", f"{DRIVE_API_BASE}/files", params=params)

    def drive_download_file(self, file_id: str) -> str:
        return self.request_text(
            "GET",
            f"{DRIVE_API_BASE}/files/{quote(file_id, safe='')}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )

    def drive_export_file(self, file_id: str, mime_type: str) -> str:
        return self.request_text(
            "GET",
            f"{DRIVE_API_BASE}/files/{quote(file_id, safe='')}/export",
            params={"mimeType": mime_type},
        )

    def calendar_list_calendars(self) -> dict[str, Any]:
        return self.request_json(
            "GET",
            f"{CALENDAR_API_BASE}/users/me/calendarList",
            params={"maxResults": 250},
        )

    def calendar_list_events(
        self,
        *,
        calendar_id: str,
        sync_token: str | None = None,
        page_token: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "showDeleted": "true",
            "singleEvents": "true",
            "maxResults": int(settings.google_calendar_bootstrap_page_size),
        }
        if sync_token:
            params["syncToken"] = sync_token
        else:
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max
            params["orderBy"] = "updated"
        if page_token:
            params["pageToken"] = page_token
        return self.request_json(
            "GET",
            f"{CALENDAR_API_BASE}/calendars/{quote(calendar_id, safe='')}/events",
            params=params,
        )
