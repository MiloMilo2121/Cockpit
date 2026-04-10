from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.db import list_google_accounts, list_raw_events_for_user
from app.dead_letter import push_dead_letter
from app.rag_pipeline import query_rag_pipeline

MAX_TOOL_OUTPUT_CHARS = 6000

CRITICAL_KEYWORDS = {
    "urgent",
    "urgente",
    "asap",
    "deadline",
    "scadenza",
    "fattura",
    "invoice",
    "contratto",
    "contract",
    "pagamento",
    "payment",
    "overdue",
    "bloccante",
    "critical",
    "critico",
}


def _string(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n[tool_output_truncated]"


def _safe_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, 1), maximum)


def _local_tz() -> ZoneInfo:
    return ZoneInfo(settings.tz)


def _parse_dt(value: Any, *, default_tz: ZoneInfo) -> datetime | None:
    raw = _string(value)
    if not raw:
        return None

    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            parsed = datetime.combine(datetime.fromisoformat(raw).date(), time.min)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(default_tz)


def _resolve_window(args: dict[str, Any]) -> tuple[str, datetime, datetime]:
    tz = _local_tz()
    now = datetime.now(tz)

    start_arg = _parse_dt(args.get("start") or args.get("from"), default_tz=tz)
    end_arg = _parse_dt(args.get("end") or args.get("to"), default_tz=tz)
    if start_arg and end_arg and end_arg > start_arg:
        return "custom", start_arg, end_arg

    window = _string(args.get("window"), "today").lower().replace(" ", "_")
    today_start = datetime.combine(now.date(), time.min, tzinfo=tz)

    if window in {"tomorrow", "domani"}:
        start = today_start + timedelta(days=1)
        return "tomorrow", start, start + timedelta(days=1)

    if window in {"week", "this_week", "upcoming_week", "settimana"}:
        return "upcoming_week", today_start, today_start + timedelta(days=7)

    if window in {"next_24h", "24h"}:
        return "next_24h", now, now + timedelta(hours=24)

    return "today", today_start, today_start + timedelta(days=1)


def _format_range(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start.date().isoformat()} {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    return f"{start.isoformat(timespec='minutes')} -> {end.isoformat(timespec='minutes')}"


def _event_start_end(payload: dict[str, Any]) -> tuple[datetime | None, datetime | None, bool]:
    tz = _local_tz()
    start_payload = payload.get("start") if isinstance(payload.get("start"), dict) else {}
    end_payload = payload.get("end") if isinstance(payload.get("end"), dict) else {}

    all_day = bool(start_payload.get("date")) and not start_payload.get("dateTime")
    start = _parse_dt(start_payload.get("dateTime") or start_payload.get("date"), default_tz=tz)
    end = _parse_dt(end_payload.get("dateTime") or end_payload.get("date"), default_tz=tz)
    return start, end, all_day


def _overlaps(start: datetime | None, end: datetime | None, window_start: datetime, window_end: datetime) -> bool:
    if start is None:
        return False
    effective_end = end or (start + timedelta(minutes=30))
    return start < window_end and effective_end > window_start


def get_calendar_context(args: dict[str, Any], user_id: str) -> str:
    label, window_start, window_end = _resolve_window(args)
    limit = _safe_limit(args.get("limit"), default=20, maximum=50)

    accounts = list_google_accounts(user_id=user_id)
    if not accounts:
        return f"calendar_context window={label} count=0 | no_google_accounts_for_user={user_id}"

    raw_events = list_raw_events_for_user(
        user_id=user_id,
        providers=["calendar"],
        resource_types=["event"],
        limit=500,
    )

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in raw_events:
        external_id = _string(event.get("external_id"))
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)

        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        start, end, all_day = _event_start_end(payload)
        if not _overlaps(start, end, window_start, window_end):
            continue

        selected.append(
            {
                "start": start,
                "end": end,
                "all_day": all_day,
                "summary": _string(payload.get("summary"), "(untitled event)"),
                "status": _string(payload.get("status"), "confirmed"),
                "location": _string(payload.get("location")),
                "account_email": _string(event.get("account_email")),
            }
        )

    selected.sort(key=lambda row: row["start"] or window_start)
    visible = selected[:limit]

    header = (
        f"calendar_context window={label} range={window_start.isoformat(timespec='minutes')}"
        f"->{window_end.isoformat(timespec='minutes')} count={len(selected)}"
    )
    if not visible:
        return header + " | no_events"

    lines = [header]
    for item in visible:
        start = item["start"]
        end = item["end"] or (start + timedelta(minutes=30) if start else window_start)
        when = "all_day" if item["all_day"] else _format_range(start, end)
        location = f" @ {item['location']}" if item["location"] else ""
        cancelled = " [cancelled]" if item["status"] == "cancelled" else ""
        lines.append(f"- {when}: {item['summary']}{location}{cancelled}")

    if len(selected) > len(visible):
        lines.append(f"- additional_events_hidden={len(selected) - len(visible)}")
    return _truncate("\n".join(lines))


def search_qdrant_tasks(args: dict[str, Any], user_id: str) -> str:
    query = _string(args.get("query"), "task aperti TODO next action file watcher priorita")
    limit = _safe_limit(args.get("limit"), default=8, maximum=20)

    payload = query_rag_pipeline(query=query, top_k=limit, rerank=False)
    results = payload.get("results") if isinstance(payload.get("results"), list) else []

    lines = [f"qdrant_task_context query={query!r} count={len(results)} user_id={user_id}"]
    if not results:
        lines.append("no_task_candidates_found")
        return "\n".join(lines)

    emitted = 0
    for result in results:
        if not isinstance(result, dict):
            continue

        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        tasks = metadata.get("tasks") if isinstance(metadata.get("tasks"), list) else []
        title = _string(result.get("document_title"), "untitled")
        source = _string(result.get("source"), "unknown")
        priority = _string(metadata.get("priority"), "medium")
        category = _string(metadata.get("category"), "uncategorized")

        if tasks:
            for task in tasks[:4]:
                task_text = _string(task)
                if not task_text:
                    continue
                lines.append(f"- {priority} | {category} | {title} | {task_text}")
                emitted += 1
                if emitted >= limit:
                    return _truncate("\n".join(lines))
            continue

        snippet = _string(result.get("text"))[:220].replace("\n", " ")
        if snippet:
            lines.append(f"- candidate | {source} | {title} | {snippet}")
            emitted += 1
            if emitted >= limit:
                return _truncate("\n".join(lines))

    if emitted == 0:
        lines.append("no_explicit_tasks_in_retrieved_chunks")
    return _truncate("\n".join(lines))


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    tz = _local_tz()
    return _parse_dt(event.get("occurred_at"), default_tz=tz) or _parse_dt(event.get("created_at"), default_tz=tz)


def _event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    provider = _string(event.get("provider"))

    if provider == "gmail":
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        subject = _string(headers.get("subject"), "(no subject)")
        sender = _string(headers.get("from"))
        snippet = _string(payload.get("snippet"))
        return f"gmail | {subject} | from={sender} | {snippet}"

    if provider == "drive":
        file_payload = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        name = _string(payload.get("name") or file_payload.get("name") or payload.get("fileId"), "(unnamed file)")
        modified = _string(payload.get("modifiedTime") or payload.get("time"))
        return f"drive | {event.get('event_type')} | {name} | modified={modified}"

    return f"{provider} | {event.get('resource_type')} | {event.get('event_type')} | {event.get('external_id')}"


def _is_critical(text: str, event: dict[str, Any]) -> bool:
    lowered = text.lower()
    if any(keyword in lowered for keyword in CRITICAL_KEYWORDS):
        return True
    event_type = _string(event.get("event_type"))
    return event_type.endswith("removed")


def query_raw_events(args: dict[str, Any], user_id: str) -> str:
    label, window_start, window_end = _resolve_window(args)
    limit = _safe_limit(args.get("limit"), default=12, maximum=50)
    provider = _string(args.get("provider")).lower()
    providers = [provider] if provider in {"gmail", "drive", "calendar"} else ["gmail", "drive"]

    raw_events = list_raw_events_for_user(
        user_id=user_id,
        providers=providers,
        limit=200,
    )

    selected: list[dict[str, Any]] = []
    for event in raw_events:
        timestamp = _event_timestamp(event)
        if timestamp and not (window_start <= timestamp < window_end):
            continue
        text = _event_text(event)
        selected.append(
            {
                "timestamp": timestamp,
                "text": text,
                "critical": _is_critical(text, event),
            }
        )

    selected.sort(key=lambda row: (bool(row["critical"]), row["timestamp"] or window_start), reverse=True)
    visible = selected[:limit]
    critical_count = sum(1 for item in selected if item["critical"])

    header = (
        f"raw_events_context window={label} providers={','.join(providers)}"
        f" count={len(selected)} critical={critical_count} processing_state=not_tracked"
    )
    if not visible:
        return header + " | no_recent_events"

    lines = [header]
    for item in visible:
        ts = item["timestamp"].isoformat(timespec="minutes") if item["timestamp"] else "unknown_time"
        marker = "critical" if item["critical"] else "normal"
        lines.append(f"- {marker} | {ts} | {item['text'][:320]}")

    if len(selected) > len(visible):
        lines.append(f"- additional_events_hidden={len(selected) - len(visible)}")
    return _truncate("\n".join(lines))


ToolHandler = Callable[[dict[str, Any], str], str]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "get_calendar_context": get_calendar_context,
    "search_qdrant_tasks": search_qdrant_tasks,
    "query_raw_events": query_raw_events,
}


def execute_cockpit_tool(name: str, args: dict[str, Any] | None, user_id: str) -> str:
    handler = TOOL_HANDLERS.get(name)
    safe_args = args if isinstance(args, dict) else {}
    if handler is None:
        return f"tool_error name={name} reason=unknown_tool"

    try:
        return _truncate(handler(safe_args, user_id))
    except Exception as exc:  # noqa: BLE001
        push_dead_letter(
            stage="agent_tool",
            reason="tool_failure",
            payload={"tool": name, "args": safe_args, "user_id": user_id},
            error=str(exc),
        )
        return f"tool_error name={name} error={type(exc).__name__}:{exc}"
