#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODELS_URL = "https://openrouter.ai/api/v1/models"
REQUIRED_AGENT_PARAMS = {"max_tokens", "response_format", "structured_outputs", "temperature", "tool_choice", "tools"}
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


def _env_value(source: str, name: str) -> str:
    for line in source.splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{name}_not_found")


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


def main() -> int:
    agents_source = _read("services/cockpit-core/app/agents.py")
    config_source = _read("services/cockpit-core/app/config.py")
    env_source = _read(".env.example")
    watcher_source = _read("services/file-watcher/app/main.py")

    agentic_model = _string_assignment(agents_source, "AGENTIC_MODEL")
    configured_models = {
        agentic_model,
        _env_value(env_source, "OPENROUTER_MODEL"),
        *_env_value(env_source, "OPENROUTER_FREE_MODELS").split(","),
        *_quoted_model_ids(config_source),
        *_quoted_model_ids(watcher_source),
    }
    configured_models = {model.strip() for model in configured_models if model.strip()}

    catalog = _load_openrouter_models()
    failures: list[str] = []

    for model_id in sorted(configured_models):
        model = catalog.get(model_id)
        if not model:
            failures.append(f"{model_id}: missing_from_openrouter_catalog")
            continue
        if not model_id.endswith(":free"):
            failures.append(f"{model_id}: model_id_must_end_with_:free")
        if not _is_zero_price(model):
            failures.append(f"{model_id}: pricing_is_not_free:{model.get('pricing')}")

    agent_model = catalog.get(agentic_model)
    if agent_model:
        supported_params = set(agent_model.get("supported_parameters") or [])
        missing_params = sorted(REQUIRED_AGENT_PARAMS.difference(supported_params))
        if missing_params:
            failures.append(f"{agentic_model}: missing_supported_parameters:{','.join(missing_params)}")

    if failures:
        print("openrouter model check: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("openrouter model check: OK")
    print(f"agentic_model={agentic_model}")
    print("configured_models=" + ",".join(sorted(configured_models)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
