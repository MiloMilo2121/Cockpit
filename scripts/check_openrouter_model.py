#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODELS_URL = "https://openrouter.ai/api/v1/models"
REQUIRED_AGENT_PARAMS = {"max_tokens", "response_format", "structured_outputs", "temperature", "tool_choice", "tools"}
REQUIRED_REASONING_PARAMS = {"reasoning"}
MODEL_PROVIDERS = {
    "anthropic",
    "arcee-ai",
    "deepseek",
    "google",
    "meta-llama",
    "mistralai",
    "moonshotai",
    "nvidia",
    "openai",
    "openrouter",
    "qwen",
    "x-ai",
    "z-ai",
}


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _string_assignment(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if name in targets and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise RuntimeError(f"{name}_not_found")


def _optional_string_assignment(source: str, name: str) -> str | None:
    try:
        return _string_assignment(source, name)
    except RuntimeError:
        return None


def _env_value(source: str, name: str) -> str:
    override = os.getenv(name)
    if override is not None:
        return override.strip()
    for line in source.splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{name}_not_found")


def _env_bool(source: str, name: str) -> bool:
    raw = _env_value(source, name).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _csv(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def _quoted_model_ids(source: str) -> set[str]:
    candidates = re.findall(r"['\"]([a-z0-9_.-]+/[a-z0-9_.:-]+)['\"]", source, flags=re.IGNORECASE)
    return {candidate for candidate in candidates if candidate.split("/", 1)[0].lower() in MODEL_PROVIDERS}


def _load_openrouter_models() -> dict[str, dict[str, Any]]:
    with urllib.request.urlopen(MODELS_URL, timeout=20) as response:
        payload = json.load(response)
    models = payload.get("data")
    if not isinstance(models, list):
        raise RuntimeError("openrouter_models_payload_invalid")
    return {str(model.get("id")): model for model in models if isinstance(model, dict) and model.get("id")}


def _is_zero_price(model: dict[str, Any]) -> bool:
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return False
    return str(pricing.get("prompt")) in {"0", "0.0", "0.000000"} and str(pricing.get("completion")) in {
        "0",
        "0.0",
        "0.000000",
    }


def _pricing(model: dict[str, Any]) -> dict[str, Any]:
    pricing = model.get("pricing")
    return pricing if isinstance(pricing, dict) else {}


def _check_model_exists(
    *,
    model_id: str,
    catalog: dict[str, dict[str, Any]],
    failures: list[str],
) -> dict[str, Any] | None:
    model = catalog.get(model_id)
    if not model:
        failures.append(f"{model_id}: missing_from_openrouter_catalog")
        return None
    return model


def _check_supported_parameters(
    *,
    model_id: str,
    model: dict[str, Any],
    required: set[str],
    failures: list[str],
) -> None:
    supported_params = set(model.get("supported_parameters") or [])
    missing_params = sorted(required.difference(supported_params))
    if missing_params:
        failures.append(f"{model_id}: missing_supported_parameters:{','.join(missing_params)}")


def main() -> int:
    agents_source = _read("services/cockpit-core/app/agents.py")
    config_source = _read("services/cockpit-core/app/config.py")
    env_source = _read(".env.example")
    watcher_source = _read("services/file-watcher/app/main.py")

    allow_paid = _env_bool(env_source, "OPENROUTER_ALLOW_PAID_MODELS")

    easy_models = {
        _env_value(env_source, "OPENROUTER_MODEL"),
        *(_csv(_env_value(env_source, "OPENROUTER_FREE_MODELS"))),
        *(_csv(_env_value(env_source, "OPENROUTER_EASY_MODELS"))),
        *(_csv(_optional_string_assignment(config_source, "openrouter_easy_models") or "")),
        *(_csv(_optional_string_assignment(agents_source, "AGENTIC_EASY_MODEL") or "")),
        *_quoted_model_ids(watcher_source),
    }
    medium_models = {
        *(_csv(_env_value(env_source, "OPENROUTER_MEDIUM_MODELS"))),
        *(_csv(_optional_string_assignment(config_source, "openrouter_medium_models") or "")),
        *(_csv(_optional_string_assignment(agents_source, "AGENTIC_MEDIUM_MODEL") or "")),
    }
    hard_models = {
        *(_csv(_env_value(env_source, "OPENROUTER_HARD_MODELS"))),
        *(_csv(_optional_string_assignment(config_source, "openrouter_hard_models") or "")),
        *(_csv(_optional_string_assignment(agents_source, "AGENTIC_HARD_MODEL") or "")),
    }

    easy_models = {model.strip() for model in easy_models if model.strip()}
    medium_models = {model.strip() for model in medium_models if model.strip()}
    hard_models = {model.strip() for model in hard_models if model.strip()}
    configured_models = easy_models | medium_models | hard_models

    catalog = _load_openrouter_models()
    failures: list[str] = []
    guarded_paid: list[str] = []
    enabled_paid: list[str] = []

    for model_id in sorted(easy_models):
        model = _check_model_exists(model_id=model_id, catalog=catalog, failures=failures)
        if not model:
            continue
        if not model_id.endswith(":free"):
            failures.append(f"{model_id}: easy_model_must_end_with_:free")
        if not _is_zero_price(model):
            failures.append(f"{model_id}: easy_model_pricing_is_not_free:{_pricing(model)}")
        _check_supported_parameters(
            model_id=model_id,
            model=model,
            required=REQUIRED_AGENT_PARAMS,
            failures=failures,
        )

    for tier_name, model_ids in (("medium", medium_models), ("hard", hard_models)):
        for model_id in sorted(model_ids):
            model = _check_model_exists(model_id=model_id, catalog=catalog, failures=failures)
            if not model:
                continue
            _check_supported_parameters(
                model_id=model_id,
                model=model,
                required=REQUIRED_AGENT_PARAMS | REQUIRED_REASONING_PARAMS,
                failures=failures,
            )
            is_free = model_id.endswith(":free") and _is_zero_price(model)
            if is_free:
                continue
            if allow_paid:
                enabled_paid.append(f"{tier_name}:{model_id}:{_pricing(model)}")
            else:
                guarded_paid.append(f"{tier_name}:{model_id}:{_pricing(model)}")

    if failures:
        print("openrouter model check: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("openrouter model check: OK")
    print("easy_models=" + ",".join(sorted(easy_models)))
    print("medium_models=" + ",".join(sorted(medium_models)))
    print("hard_models=" + ",".join(sorted(hard_models)))
    print(f"allow_paid_models={str(allow_paid).lower()}")
    if guarded_paid:
        print("paid_tier_models_guarded=" + " | ".join(guarded_paid))
    if enabled_paid:
        print("paid_tier_models_enabled=" + " | ".join(enabled_paid))
    print("configured_models=" + ",".join(sorted(configured_models)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
