from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.db import (
    delete_sync_cursor,
    get_google_account,
    get_sync_cursor,
    insert_raw_event,
    upsert_external_document,
    upsert_sync_cursor,
)
from app.google_client import GoogleAccountSession, GoogleApiError
from app.metrics import increment_metric
from app.rag_pipeline import ingest_document_pipeline

SUPPORTED_PROVIDERS = {"gmail", "drive", "calendar"}
DIRECT_TEXT_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/javascript",
    "application/sql",
    "application/x-sh",
    "application/x-yaml",
    "application/yaml",
}
GOOGLE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _string(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded.encode("utf-8"))
    except (ValueError, binascii.Error):
        return ""

    for encoding in ("utf-8", "latin-1"):
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _html_to_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return _normalize_whitespace(html.unescape(without_tags))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_google_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _mailbox_q() -> str:
    return "-in:trash -in:spam"


def _gmail_headers(message: dict[str, Any]) -> dict[str, str]:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return {}
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return {}
    normalized: dict[str, str] = {}
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name")).lower()
        value = _string(item.get("value"))
        if name and value:
            normalized[name] = value
    return normalized


def _extract_gmail_body_from_part(part: dict[str, Any], text_parts: list[str], html_parts: list[str]) -> None:
    mime_type = _string(part.get("mimeType")).lower()
    body = part.get("body")
    if isinstance(body, dict):
        encoded = _string(body.get("data"))
        if encoded:
            decoded = _decode_b64url(encoded)
            if mime_type == "text/plain" and decoded:
                text_parts.append(decoded)
            elif mime_type == "text/html" and decoded:
                html_parts.append(decoded)

    parts = part.get("parts")
    if isinstance(parts, list):
        for child in parts:
            if isinstance(child, dict):
                _extract_gmail_body_from_part(child, text_parts, html_parts)


def _gmail_message_text(message: dict[str, Any]) -> str:
    headers = _gmail_headers(message)
    text_parts: list[str] = []
    html_parts: list[str] = []

    payload = message.get("payload")
    if isinstance(payload, dict):
        _extract_gmail_body_from_part(payload, text_parts, html_parts)

    body = "\n\n".join(part.strip() for part in text_parts if part.strip())
    if not body and html_parts:
        body = "\n\n".join(_html_to_text(part) for part in html_parts if part.strip())
    if not body:
        body = _string(message.get("snippet"))

    lines = [
        f"Subject: {_string(headers.get('subject'), '(no subject)')}",
        f"From: {_string(headers.get('from'))}",
        f"To: {_string(headers.get('to'))}",
        f"Date: {_string(headers.get('date'))}",
        "",
        body,
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def _gmail_message_metadata(account: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    headers = _gmail_headers(message)
    return {
        "account_id": int(account["id"]),
        "account_email": _string(account.get("google_email")),
        "thread_id": _string(message.get("threadId")),
        "history_id": _string(message.get("historyId")),
        "internal_date": _string(message.get("internalDate")),
        "label_ids": message.get("labelIds") if isinstance(message.get("labelIds"), list) else [],
        "headers": headers,
    }


def _calendar_event_text(calendar_name: str, event: dict[str, Any]) -> str:
    attendees = event.get("attendees")
    attendee_lines: list[str] = []
    if isinstance(attendees, list):
        for attendee in attendees[:20]:
            if not isinstance(attendee, dict):
                continue
            attendee_lines.append(_string(attendee.get("email")) or _string(attendee.get("displayName")))

    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    lines = [
        f"Calendar: {calendar_name}",
        f"Title: {_string(event.get('summary'), '(untitled event)')}",
        f"Status: {_string(event.get('status'))}",
        f"Start: {_string(start.get('dateTime') or start.get('date'))}",
        f"End: {_string(end.get('dateTime') or end.get('date'))}",
        f"Location: {_string(event.get('location'))}",
        "",
        _string(event.get("description")),
    ]
    if attendee_lines:
        lines.extend(["", "Attendees:", *attendee_lines])
    return "\n".join(line for line in lines if line is not None).strip()


def _drive_file_text(session: GoogleAccountSession, file_data: dict[str, Any]) -> str:
    mime_type = _string(file_data.get("mimeType"))
    file_size = int(file_data.get("size") or 0)
    file_id = _string(file_data.get("id"))

    content = ""
    if mime_type in GOOGLE_EXPORT_MIME_TYPES:
        content = session.drive_export_file(file_id, GOOGLE_EXPORT_MIME_TYPES[mime_type])
    elif mime_type.startswith("text/") or mime_type in DIRECT_TEXT_MIME_TYPES:
        if file_size <= int(settings.google_drive_download_max_bytes) or file_size == 0:
            content = session.drive_download_file(file_id)

    if not content:
        content = (
            f"Drive file metadata\n"
            f"Name: {_string(file_data.get('name'))}\n"
            f"Mime-Type: {mime_type}\n"
            f"Modified: {_string(file_data.get('modifiedTime'))}\n"
            f"WebViewLink: {_string(file_data.get('webViewLink'))}"
        )

    return content.strip()


def _ingest_external_document(
    *,
    account: dict[str, Any],
    provider: str,
    external_document_id: str,
    title: str,
    mime_type: str | None,
    content: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    upsert_external_document(
        account_id=int(account["id"]),
        provider=provider,
        external_document_id=external_document_id,
        title=title,
        mime_type=mime_type,
        content=content,
        metadata=metadata,
    )
    return ingest_document_pipeline(
        {
            "document_id": f"google:{provider}:{int(account['id'])}:{external_document_id}",
            "title": title,
            "source": f"google_{provider}",
            "content": content,
            "chunking_strategy": "semantic",
            "replace_existing_document": True,
            "metadata": metadata,
        }
    )


def _persist_gmail_message(account: dict[str, Any], message: dict[str, Any], *, event_type: str, source_cursor: str) -> bool:
    message_id = _string(message.get("id"))
    if not message_id:
        return False

    history_id = _string(message.get("historyId"))
    occurred_at = _parse_google_datetime(message.get("internalDate"))
    payload = {
        "id": message_id,
        "threadId": _string(message.get("threadId")),
        "historyId": history_id,
        "labelIds": message.get("labelIds") if isinstance(message.get("labelIds"), list) else [],
        "snippet": _string(message.get("snippet")),
        "headers": _gmail_headers(message),
    }
    event_uid = f"gmail:{int(account['id'])}:{message_id}:{history_id or source_cursor or event_type}"
    inserted = insert_raw_event(
        event_uid=event_uid,
        account_id=int(account["id"]),
        provider="gmail",
        resource_type="message",
        external_id=message_id,
        event_type=event_type,
        source_cursor=source_cursor,
        payload=payload,
        occurred_at=occurred_at,
    )

    content = _gmail_message_text(message)
    metadata = _gmail_message_metadata(account, message)
    _ingest_external_document(
        account=account,
        provider="gmail",
        external_document_id=message_id,
        title=_string(metadata["headers"].get("subject"), "(no subject)"),
        mime_type="message/rfc822",
        content=content,
        metadata=metadata,
    )
    return inserted


def _persist_drive_file(
    account: dict[str, Any],
    session: GoogleAccountSession,
    file_data: dict[str, Any],
    *,
    event_type: str,
    source_cursor: str,
) -> bool:
    file_id = _string(file_data.get("id"))
    if not file_id:
        return False

    occurred_at = _parse_google_datetime(file_data.get("modifiedTime"))
    payload = {
        "id": file_id,
        "name": _string(file_data.get("name")),
        "mimeType": _string(file_data.get("mimeType")),
        "modifiedTime": _string(file_data.get("modifiedTime")),
        "size": _string(file_data.get("size")),
        "webViewLink": _string(file_data.get("webViewLink")),
        "trashed": bool(file_data.get("trashed", False)),
    }
    version_marker = _string(file_data.get("modifiedTime") or source_cursor or event_type)
    event_uid = f"drive:{int(account['id'])}:{file_id}:{version_marker}"
    inserted = insert_raw_event(
        event_uid=event_uid,
        account_id=int(account["id"]),
        provider="drive",
        resource_type="file",
        external_id=file_id,
        event_type=event_type,
        source_cursor=source_cursor,
        payload=payload,
        occurred_at=occurred_at,
    )

    content = _drive_file_text(session, file_data)
    metadata = {
        "account_id": int(account["id"]),
        "account_email": _string(account.get("google_email")),
        "mime_type": _string(file_data.get("mimeType")),
        "modified_time": _string(file_data.get("modifiedTime")),
        "size": _string(file_data.get("size")),
        "web_view_link": _string(file_data.get("webViewLink")),
    }
    _ingest_external_document(
        account=account,
        provider="drive",
        external_document_id=file_id,
        title=_string(file_data.get("name"), file_id),
        mime_type=_string(file_data.get("mimeType")) or None,
        content=content,
        metadata=metadata,
    )
    return inserted


def _persist_calendar_event(
    account: dict[str, Any],
    calendar_name: str,
    calendar_id: str,
    event: dict[str, Any],
    *,
    event_type: str,
    source_cursor: str,
) -> bool:
    event_id = _string(event.get("id"))
    if not event_id:
        return False

    occurred_at = _parse_google_datetime(event.get("updated"))
    payload = {
        "id": event_id,
        "calendarId": calendar_id,
        "summary": _string(event.get("summary")),
        "status": _string(event.get("status")),
        "start": event.get("start") if isinstance(event.get("start"), dict) else {},
        "end": event.get("end") if isinstance(event.get("end"), dict) else {},
        "location": _string(event.get("location")),
        "htmlLink": _string(event.get("htmlLink")),
    }
    version_marker = _string(event.get("updated") or source_cursor or event_type)
    event_uid = f"calendar:{int(account['id'])}:{calendar_id}:{event_id}:{version_marker}"
    inserted = insert_raw_event(
        event_uid=event_uid,
        account_id=int(account["id"]),
        provider="calendar",
        resource_type="event",
        external_id=f"{calendar_id}:{event_id}",
        event_type=event_type,
        source_cursor=source_cursor,
        payload=payload,
        occurred_at=occurred_at,
    )

    content = _calendar_event_text(calendar_name, event)
    metadata = {
        "account_id": int(account["id"]),
        "account_email": _string(account.get("google_email")),
        "calendar_id": calendar_id,
        "calendar_name": calendar_name,
        "status": _string(event.get("status")),
        "updated": _string(event.get("updated")),
        "html_link": _string(event.get("htmlLink")),
    }
    _ingest_external_document(
        account=account,
        provider="calendar",
        external_document_id=f"{calendar_id}:{event_id}",
        title=_string(event.get("summary"), "(untitled event)"),
        mime_type="text/calendar",
        content=content,
        metadata=metadata,
    )
    return inserted


def _sync_gmail(account: dict[str, Any], session: GoogleAccountSession, bootstrap: bool) -> dict[str, Any]:
    increment_metric("google_sync_gmail_runs_total")
    history_cursor = get_sync_cursor(account_id=int(account["id"]), provider="gmail", cursor_key="history_id")
    bootstrap_page = get_sync_cursor(account_id=int(account["id"]), provider="gmail", cursor_key="bootstrap_page_token")

    if bootstrap or not history_cursor:
        response = session.gmail_list_messages(
            page_token=None if bootstrap_page is None else bootstrap_page["cursor_value"],
            max_results=int(settings.google_gmail_bootstrap_max_results),
            q=_mailbox_q(),
        )
        raw_messages = response.get("messages")
        messages = raw_messages if isinstance(raw_messages, list) else []
        inserted = 0
        for item in messages:
            if not isinstance(item, dict):
                continue
            message_id = _string(item.get("id"))
            if not message_id:
                continue
            full_message = session.gmail_get_message(message_id)
            if _persist_gmail_message(
                account,
                full_message,
                event_type="bootstrap_message",
                source_cursor="" if bootstrap_page is None else bootstrap_page["cursor_value"],
            ):
                inserted += 1

        next_page_token = _string(response.get("nextPageToken"))
        if next_page_token:
            upsert_sync_cursor(
                account_id=int(account["id"]),
                provider="gmail",
                cursor_key="bootstrap_page_token",
                cursor_value=next_page_token,
            )
        else:
            delete_sync_cursor(account_id=int(account["id"]), provider="gmail", cursor_key="bootstrap_page_token")
            profile = session.gmail_get_profile()
            history_id = _string(profile.get("historyId"))
            if history_id:
                upsert_sync_cursor(
                    account_id=int(account["id"]),
                    provider="gmail",
                    cursor_key="history_id",
                    cursor_value=history_id,
                )

        return {
            "mode": "bootstrap",
            "messages_seen": len(messages),
            "events_inserted": inserted,
            "next_page_token": next_page_token or None,
        }

    history_value = _string(history_cursor.get("cursor_value"))
    try:
        response = session.gmail_list_history(
            start_history_id=history_value,
            max_results=int(settings.google_gmail_history_page_size),
        )
    except GoogleApiError as exc:
        if "404" in str(exc):
            delete_sync_cursor(account_id=int(account["id"]), provider="gmail", cursor_key="history_id")
            return _sync_gmail(account, session, bootstrap=True)
        raise

    history_items = response.get("history")
    items = history_items if isinstance(history_items, list) else []
    message_ids: set[str] = set()
    inserted_events = 0

    for history_item in items:
        if not isinstance(history_item, dict):
            continue
        history_record_id = _string(history_item.get("id"))
        if insert_raw_event(
            event_uid=f"gmail-history:{int(account['id'])}:{history_record_id}",
            account_id=int(account["id"]),
            provider="gmail",
            resource_type="history",
            external_id=history_record_id,
            event_type="history_delta",
            source_cursor=history_value,
            payload=history_item,
            occurred_at=_utc_now(),
        ):
            inserted_events += 1

        for key in ("messages", "messagesAdded"):
            values = history_item.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, dict):
                    message = value.get("message") if key == "messagesAdded" else value
                    if isinstance(message, dict):
                        message_id = _string(message.get("id"))
                        if message_id:
                            message_ids.add(message_id)

    for message_id in sorted(message_ids):
        full_message = session.gmail_get_message(message_id)
        if _persist_gmail_message(
            account,
            full_message,
            event_type="incremental_message",
            source_cursor=history_value,
        ):
            inserted_events += 1

    new_history_id = _string(response.get("historyId"))
    if new_history_id:
        upsert_sync_cursor(
            account_id=int(account["id"]),
            provider="gmail",
            cursor_key="history_id",
            cursor_value=new_history_id,
        )

    return {
        "mode": "incremental",
        "history_records": len(items),
        "message_ids_touched": len(message_ids),
        "events_inserted": inserted_events,
        "history_id": new_history_id or history_value,
    }


def _sync_drive(account: dict[str, Any], session: GoogleAccountSession, bootstrap: bool) -> dict[str, Any]:
    increment_metric("google_sync_drive_runs_total")
    drive_cursor = get_sync_cursor(account_id=int(account["id"]), provider="drive", cursor_key="page_token")
    bootstrap_page = get_sync_cursor(account_id=int(account["id"]), provider="drive", cursor_key="bootstrap_page_token")

    if bootstrap or not drive_cursor:
        response = session.drive_list_files(
            page_token=None if bootstrap_page is None else bootstrap_page["cursor_value"],
            page_size=int(settings.google_drive_bootstrap_page_size),
        )
        raw_files = response.get("files")
        files = raw_files if isinstance(raw_files, list) else []
        inserted = 0
        for item in files:
            if isinstance(item, dict) and _persist_drive_file(
                account,
                session,
                item,
                event_type="bootstrap_file",
                source_cursor="" if bootstrap_page is None else bootstrap_page["cursor_value"],
            ):
                inserted += 1

        next_page_token = _string(response.get("nextPageToken"))
        if next_page_token:
            upsert_sync_cursor(
                account_id=int(account["id"]),
                provider="drive",
                cursor_key="bootstrap_page_token",
                cursor_value=next_page_token,
            )
        else:
            delete_sync_cursor(account_id=int(account["id"]), provider="drive", cursor_key="bootstrap_page_token")
            page_token_payload = session.drive_get_start_page_token()
            start_page_token = _string(page_token_payload.get("startPageToken"))
            if start_page_token:
                upsert_sync_cursor(
                    account_id=int(account["id"]),
                    provider="drive",
                    cursor_key="page_token",
                    cursor_value=start_page_token,
                )

        return {
            "mode": "bootstrap",
            "files_seen": len(files),
            "events_inserted": inserted,
            "next_page_token": next_page_token or None,
        }

    try:
        response = session.drive_list_changes(page_token=drive_cursor["cursor_value"])
    except GoogleApiError as exc:
        if "410" in str(exc) or "404" in str(exc):
            delete_sync_cursor(account_id=int(account["id"]), provider="drive", cursor_key="page_token")
            return _sync_drive(account, session, bootstrap=True)
        raise
    raw_changes = response.get("changes")
    changes = raw_changes if isinstance(raw_changes, list) else []
    inserted = 0

    for change in changes:
        if not isinstance(change, dict):
            continue
        file_id = _string(change.get("fileId"))
        change_time = _string(change.get("time"))
        removed = bool(change.get("removed", False))
        payload = {
            "fileId": file_id,
            "time": change_time,
            "removed": removed,
            "file": change.get("file") if isinstance(change.get("file"), dict) else {},
        }
        if insert_raw_event(
            event_uid=f"drive-change:{int(account['id'])}:{file_id}:{change_time or drive_cursor['cursor_value']}",
            account_id=int(account["id"]),
            provider="drive",
            resource_type="change",
            external_id=file_id,
            event_type="change_removed" if removed else "change_updated",
            source_cursor=drive_cursor["cursor_value"],
            payload=payload,
            occurred_at=_parse_google_datetime(change.get("time")),
        ):
            inserted += 1

        file_data = change.get("file")
        if removed or not isinstance(file_data, dict):
            continue
        if _persist_drive_file(
            account,
            session,
            file_data,
            event_type="incremental_file",
            source_cursor=drive_cursor["cursor_value"],
        ):
            inserted += 1

    next_page_token = _string(response.get("nextPageToken"))
    new_start_page_token = _string(response.get("newStartPageToken"))
    if next_page_token:
        upsert_sync_cursor(
            account_id=int(account["id"]),
            provider="drive",
            cursor_key="page_token",
            cursor_value=next_page_token,
        )
    elif new_start_page_token:
        upsert_sync_cursor(
            account_id=int(account["id"]),
            provider="drive",
            cursor_key="page_token",
            cursor_value=new_start_page_token,
        )

    return {
        "mode": "incremental",
        "changes_seen": len(changes),
        "events_inserted": inserted,
        "page_token": next_page_token or new_start_page_token or drive_cursor["cursor_value"],
    }


def _sync_calendar_feed(
    account: dict[str, Any],
    session: GoogleAccountSession,
    *,
    calendar_id: str,
    calendar_name: str,
    bootstrap: bool,
) -> dict[str, Any]:
    cursor_key = f"sync_token:{calendar_id}"
    sync_cursor = get_sync_cursor(account_id=int(account["id"]), provider="calendar", cursor_key=cursor_key)
    inserted = 0
    events_seen = 0

    if bootstrap or not sync_cursor:
        page_token: str | None = None
        next_sync_token = ""
        window_start = (_utc_now() - timedelta(days=int(settings.google_calendar_bootstrap_past_days))).isoformat()
        window_end = (_utc_now() + timedelta(days=int(settings.google_calendar_bootstrap_future_days))).isoformat()

        while True:
            response = session.calendar_list_events(
                calendar_id=calendar_id,
                page_token=page_token,
                time_min=window_start,
                time_max=window_end,
            )
            raw_items = response.get("items")
            items = raw_items if isinstance(raw_items, list) else []
            events_seen += len(items)

            for item in items:
                if isinstance(item, dict) and _persist_calendar_event(
                    account,
                    calendar_name,
                    calendar_id,
                    item,
                    event_type="bootstrap_event",
                    source_cursor=page_token or "",
                ):
                    inserted += 1

            page_token = _string(response.get("nextPageToken")) or None
            next_sync_token = _string(response.get("nextSyncToken"))
            if not page_token:
                break

        if next_sync_token:
            upsert_sync_cursor(
                account_id=int(account["id"]),
                provider="calendar",
                cursor_key=cursor_key,
                cursor_value=next_sync_token,
            )

        return {
            "mode": "bootstrap",
            "calendar_id": calendar_id,
            "calendar_name": calendar_name,
            "events_seen": events_seen,
            "events_inserted": inserted,
        }

    page_token = None
    next_sync_token = _string(sync_cursor["cursor_value"])
    try:
        while True:
            response = session.calendar_list_events(
                calendar_id=calendar_id,
                sync_token=sync_cursor["cursor_value"],
                page_token=page_token,
            )
            raw_items = response.get("items")
            items = raw_items if isinstance(raw_items, list) else []
            events_seen += len(items)

            for item in items:
                if isinstance(item, dict) and _persist_calendar_event(
                    account,
                    calendar_name,
                    calendar_id,
                    item,
                    event_type="incremental_event",
                    source_cursor=sync_cursor["cursor_value"],
                ):
                    inserted += 1

            page_token = _string(response.get("nextPageToken")) or None
            next_sync_token = _string(response.get("nextSyncToken")) or next_sync_token
            if not page_token:
                break
    except GoogleApiError as exc:
        if "410" in str(exc):
            delete_sync_cursor(account_id=int(account["id"]), provider="calendar", cursor_key=cursor_key)
            return _sync_calendar_feed(account, session, calendar_id=calendar_id, calendar_name=calendar_name, bootstrap=True)
        raise

    if next_sync_token:
        upsert_sync_cursor(
            account_id=int(account["id"]),
            provider="calendar",
            cursor_key=cursor_key,
            cursor_value=next_sync_token,
        )

    return {
        "mode": "incremental",
        "calendar_id": calendar_id,
        "calendar_name": calendar_name,
        "events_seen": events_seen,
        "events_inserted": inserted,
    }


def _sync_calendar(account: dict[str, Any], session: GoogleAccountSession, bootstrap: bool) -> dict[str, Any]:
    increment_metric("google_sync_calendar_runs_total")
    response = session.calendar_list_calendars()
    raw_items = response.get("items")
    calendars = raw_items if isinstance(raw_items, list) else []

    selected = [
        item
        for item in calendars
        if isinstance(item, dict)
        and _string(item.get("accessRole")) != "none"
        and item.get("selected", True) is not False
    ]

    results: list[dict[str, Any]] = []
    for calendar in selected:
        calendar_id = _string(calendar.get("id"))
        if not calendar_id:
            continue
        calendar_name = _string(calendar.get("summary"), calendar_id)
        results.append(
            _sync_calendar_feed(
                account,
                session,
                calendar_id=calendar_id,
                calendar_name=calendar_name,
                bootstrap=bootstrap,
            )
        )

    return {
        "mode": "bootstrap" if bootstrap else "incremental",
        "calendars_seen": len(selected),
        "calendars": results,
    }


def sync_google_account_pipeline(
    *,
    account_id: int,
    providers: list[str] | None = None,
    bootstrap: bool = False,
) -> dict[str, Any]:
    account = get_google_account(int(account_id), include_tokens=True)
    if not account:
        return {
            "status": "failed",
            "reason": "google_account_not_found",
            "account_id": int(account_id),
        }

    requested = [item for item in (providers or ["gmail", "drive", "calendar"]) if item in SUPPORTED_PROVIDERS]
    if not requested:
        requested = ["gmail", "drive", "calendar"]

    results: dict[str, Any] = {}
    with GoogleAccountSession(account) as session:
        if "gmail" in requested:
            results["gmail"] = _sync_gmail(account, session, bootstrap)
        if "drive" in requested:
            results["drive"] = _sync_drive(account, session, bootstrap)
        if "calendar" in requested:
            results["calendar"] = _sync_calendar(account, session, bootstrap)

    increment_metric("google_sync_runs_total")
    return {
        "status": "completed",
        "account_id": int(account_id),
        "providers": requested,
        "bootstrap": bool(bootstrap),
        "results": results,
        "synced_at": _utc_now().isoformat(),
        "sync_digest": hashlib.sha1(
            f"{account_id}:{','.join(requested)}:{int(bool(bootstrap))}:{_utc_now().isoformat()}".encode("utf-8")
        ).hexdigest()[:16],
    }
