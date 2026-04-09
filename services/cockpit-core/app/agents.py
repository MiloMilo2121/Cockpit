from __future__ import annotations

import json
from typing import Any, Dict

from app.openrouter_client import chat_completion

_ALLOWED_AGENTS = {
    "RAG_ANALYST_AGENT",
    "COMMUNICATION_AGENT",
    "SYSTEM_MAINTENANCE_AGENT",
    "GENERAL_PLANNER_AGENT",
}

_ROUTER_SYSTEM_PROMPT = (
    "You are an intent router for a personal life cockpit. "
    "Choose exactly one agent id from this list: "
    "RAG_ANALYST_AGENT, COMMUNICATION_AGENT, SYSTEM_MAINTENANCE_AGENT, GENERAL_PLANNER_AGENT. "
    "Return only strict JSON with keys: agent, reason, priority. "
    "priority must be low, medium or high."
)

_SPECIALIST_SYSTEM_PROMPTS = {
    "RAG_ANALYST_AGENT": (
        "You are the RAG_ANALYST_AGENT. "
        "Analyze the message for information retrieval needs and produce concise operational guidance."
    ),
    "COMMUNICATION_AGENT": (
        "You are the COMMUNICATION_AGENT. "
        "Draft communication-focused output with clear next reply suggestions."
    ),
    "SYSTEM_MAINTENANCE_AGENT": (
        "You are the SYSTEM_MAINTENANCE_AGENT. "
        "Focus on technical maintenance, reliability and concrete remediation steps."
    ),
    "GENERAL_PLANNER_AGENT": (
        "You are the GENERAL_PLANNER_AGENT. "
        "Provide practical planning output with priority and concrete next actions."
    ),
}


def _extract_json_object(text: str) -> Dict[str, Any] | None:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    fragment = stripped[start : end + 1]
    try:
        parsed = json.loads(fragment)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def _normalize_agent(value: Any) -> str:
    text = str(value or "").strip()
    if text in _ALLOWED_AGENTS:
        return text
    return "GENERAL_PLANNER_AGENT"


def _normalize_priority(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"low", "medium", "high"}:
        return text
    return "medium"


def route_to_agent(message: str) -> Dict[str, str]:
    router_messages = [
        {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]

    raw, model = chat_completion(messages=router_messages, temperature=0.0, max_tokens=200)
    parsed = _extract_json_object(raw) or {}

    return {
        "agent": _normalize_agent(parsed.get("agent")),
        "reason": str(parsed.get("reason") or "intent_router_default"),
        "priority": _normalize_priority(parsed.get("priority")),
        "router_model": model,
    }


def run_specialist(*, agent: str, message: str, route_reason: str, priority: str) -> Dict[str, str]:
    system_prompt = _SPECIALIST_SYSTEM_PROMPTS.get(agent, _SPECIALIST_SYSTEM_PROMPTS["GENERAL_PLANNER_AGENT"])
    user_prompt = (
        "Input message:\n"
        f"{message}\n\n"
        f"Routing reason: {route_reason}\n"
        f"Priority: {priority}\n\n"
        "Return plain text with this exact structure:\n"
        "INTENT: <one line>\n"
        "RISK: <one line>\n"
        "NEXT_ACTIONS:\n"
        "1. <action>\n"
        "2. <action>\n"
        "3. <action>\n"
        "Keep PII placeholder tokens unchanged if present."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    output, model = chat_completion(messages=messages)
    return {
        "agent": agent,
        "specialist_model": model,
        "output": output.strip(),
    }
