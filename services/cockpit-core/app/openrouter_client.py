from __future__ import annotations

from typing import Any, Iterable

import httpx

from app.config import settings


class OpenRouterError(RuntimeError):
    pass


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


def chat_completion(
    *,
    messages: list[dict[str, str]],
    preferred_models: Iterable[str] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> tuple[str, str]:
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
            content = payload["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{model}:invalid_payload:{exc}")
            continue

        return str(content), model

    raise OpenRouterError("openrouter_models_exhausted: " + " | ".join(errors))
