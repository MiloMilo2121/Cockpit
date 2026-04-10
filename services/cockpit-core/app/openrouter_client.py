from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from app.config import settings


class OpenRouterError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenRouterToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OpenRouterChatResponse:
    content: str
    model: str
    tool_calls: list[OpenRouterToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def _candidate_models(preferred_models: Iterable[str] | None = None) -> list[str]:
    models: list[str] = []
    if preferred_models:
        models.extend([item.strip() for item in preferred_models if item and item.strip()])
    models.extend(settings.openrouter_models)

    deduped: list[str] = []
    seen: set[str] = set()
    for model in models:
        if not model.endswith(":free"):
            continue
        if model in seen:
            continue
        seen.add(model)
        deduped.append(model)
    return deduped


def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return {}

    text = raw_arguments.strip()
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}

    if isinstance(parsed, dict):
        return parsed
    return {}


def _normalize_tool_calls(raw_tool_calls: Any) -> list[OpenRouterToolCall]:
    if not isinstance(raw_tool_calls, list):
        return []

    normalized: list[OpenRouterToolCall] = []
    for index, item in enumerate(raw_tool_calls):
        if not isinstance(item, dict):
            continue

        function = item.get("function")
        if not isinstance(function, dict):
            continue

        name = str(function.get("name") or "").strip()
        if not name:
            continue

        call_id = str(item.get("id") or f"tool_call_{index}")
        normalized.append(
            OpenRouterToolCall(
                id=call_id,
                name=name,
                arguments=_parse_tool_arguments(function.get("arguments")),
                raw=item,
            )
        )

    return normalized


def chat_completion_message(
    *,
    messages: list[dict[str, Any]],
    preferred_models: Iterable[str] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> OpenRouterChatResponse:
    if not settings.openrouter_api_key:
        raise OpenRouterError("openrouter_api_key_not_set")

    models = _candidate_models(preferred_models)
    if not models:
        raise OpenRouterError("no_openrouter_free_models_configured")

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }

    errors: list[str] = []
    for model in models:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": settings.openrouter_temperature if temperature is None else temperature,
            "max_tokens": settings.openrouter_max_tokens if max_tokens is None else max_tokens,
        }
        if tools is not None:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice

        try:
            response = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=float(settings.openrouter_timeout_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{model}:request_error:{exc}")
            continue

        if response.status_code in {429, 500, 502, 503, 504}:
            errors.append(f"{model}:http_{response.status_code}")
            continue

        if response.status_code >= 400:
            errors.append(f"{model}:http_{response.status_code}")
            continue

        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message_not_object")
            content = message.get("content") or ""
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{model}:invalid_payload:{exc}")
            continue

        return OpenRouterChatResponse(
            content=str(content),
            model=model,
            tool_calls=_normalize_tool_calls(message.get("tool_calls")),
            raw_message=message,
        )

    raise OpenRouterError("openrouter_models_exhausted: " + " | ".join(errors))


def chat_completion(
    *,
    messages: list[dict[str, Any]],
    preferred_models: Iterable[str] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> tuple[str, str]:
    response = chat_completion_message(
        messages=messages,
        preferred_models=preferred_models,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        tool_choice=tool_choice,
    )
    return response.content, response.model
