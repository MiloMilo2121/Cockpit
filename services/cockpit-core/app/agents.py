from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.cockpit_tools import execute_cockpit_tool
from app.config import settings
from app.dead_letter import push_dead_letter
from app.openrouter_client import OpenRouterChatResponse, OpenRouterToolCall, chat_completion_message

AGENTIC_MODEL = "qwen/qwen3-next-80b-a3b-instruct:free"
MAX_TOOL_LOOPS = 4
MAX_AGENTIC_ITERATIONS = MAX_TOOL_LOOPS
MANDATORY_TOOLS = {"get_calendar_context", "search_qdrant_tasks"}
UNSTABLE_OUTPUT = "Sistema instabile sui dati. Azione richiesta: verifica manuale su Cockpit UI"
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_RE = re.compile(r"https?://\S+")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ._-]{7,}\d)(?!\w)")

MASTER_PROMPT = """<system_directive>
  <role>Cockpit Director. Sei l'orchestratore operativo di Marco.</role>
  <rules>
    <rule>NON generare MAI un piano senza prima aver chiamato `get_calendar_context` e `search_qdrant_tasks`.</rule>
    <rule>Usa `query_raw_events` quando email, Drive, rischi, urgenze o scostamenti possono cambiare la priorita.</rule>
    <rule>Nessun preambolo. Nessun saluto. Output finale in formato BLUF.</rule>
    <rule>Se un dato manca, non allucinare: chiedilo a Marco nell'output finale.</rule>
    <rule>Prioritizzazione: Task cognitivi pesanti -> Mattina. Task amministrativi -> Pomeriggio.</rule>
    <rule>Ragiona internamente. Non esporre chain-of-thought, solo decisioni operative verificabili.</rule>
  </rules>
  <output_contract>
    <section>BLUF: una frase secca con decisione principale.</section>
    <section>PIANO: massimo 5 azioni ordinate per impatto e vincoli calendario.</section>
    <section>RISCHI: solo blocchi reali o dati mancanti.</section>
    <section>RICHIESTE_A_MARCO: domande minime quando servono dati non disponibili.</section>
  </output_contract>
</system_directive>"""

COCKPIT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_calendar_context",
            "description": "Estrae gli eventi Google Calendar per la finestra temporale richiesta.",
            "strict": True,
            "parameters": {
                "type": "object",
                "required": ["window", "start", "end", "limit"],
                "properties": {
                    "window": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "next_24h", "upcoming_week"],
                        "description": "Finestra logica: today, tomorrow, next_24h, upcoming_week.",
                    },
                    "start": {
                        "type": ["string", "null"],
                        "description": "Inizio ISO opzionale per una finestra custom.",
                    },
                    "end": {
                        "type": ["string", "null"],
                        "description": "Fine ISO opzionale per una finestra custom.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Numero massimo di eventi compatti da restituire.",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_qdrant_tasks",
            "description": "Estrae i task aperti trovati dal file-watcher interrogando Qdrant.",
            "strict": True,
            "parameters": {
                "type": "object",
                "required": ["query", "limit"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Query sintetica per task, TODO, scadenze o prossime azioni.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Numero massimo di task candidati.",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_raw_events",
            "description": "Verifica email critiche o aggiornamenti Drive non processati.",
            "strict": True,
            "parameters": {
                "type": "object",
                "required": ["provider", "window", "start", "end", "limit"],
                "properties": {
                    "provider": {
                        "type": ["string", "null"],
                        "enum": ["gmail", "drive", "calendar", None],
                        "description": "Provider opzionale: gmail, drive, calendar. Default: gmail+drive.",
                    },
                    "window": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "next_24h", "upcoming_week"],
                        "description": "Finestra logica: today, tomorrow, next_24h, upcoming_week.",
                    },
                    "start": {
                        "type": ["string", "null"],
                        "description": "Inizio ISO opzionale per una finestra custom.",
                    },
                    "end": {
                        "type": ["string", "null"],
                        "description": "Fine ISO opzionale per una finestra custom.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Numero massimo di eventi compatti da restituire.",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
]


class ReflectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    corrected_output: str = Field(default="", max_length=4000)
    reason: str = Field(default="", max_length=600)


def _now_context() -> str:
    now = datetime.now(ZoneInfo(settings.tz))
    return now.isoformat(timespec="minutes")


def _extract_json_object(text: str) -> dict[str, Any] | None:
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

    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _tool_call_payload(tool_call: OpenRouterToolCall) -> dict[str, Any]:
    if tool_call.raw:
        return tool_call.raw
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": json.dumps(tool_call.arguments, ensure_ascii=True),
        },
    }


def _append_assistant_tool_calls(messages: list[dict[str, Any]], response: OpenRouterChatResponse) -> None:
    messages.append(
        {
            "role": "assistant",
            "content": response.content or None,
            "tool_calls": [_tool_call_payload(tool_call) for tool_call in response.tool_calls],
        }
    )


def _append_tool_result(messages: list[dict[str, Any]], tool_call: OpenRouterToolCall, result: str) -> None:
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "content": result,
        }
    )


def _tool_result_valid(result: str) -> bool:
    return not result.startswith(("tool_error", "tool_args_validation_error"))


def _sanitize_tool_result_for_llm(result: str) -> str:
    sanitized = EMAIL_RE.sub("<EMAIL>", result)
    sanitized = URL_RE.sub("<URL>", sanitized)
    sanitized = PHONE_RE.sub("<PHONE>", sanitized)
    return sanitized


def _format_tool_observations(tool_observations: list[str]) -> str:
    if not tool_observations:
        return "no_tool_observations"
    compact = "\n\n".join(tool_observations[-8:])
    if len(compact) <= 7000:
        return compact
    return compact[:6900].rstrip() + "\n[reflection_context_truncated]"


def _fallback_from_observations(tool_observations: list[str]) -> str:
    context = _format_tool_observations(tool_observations)
    return (
        "BLUF: limite tool raggiunto; verifica manuale consigliata prima di agire.\n"
        "PIANO:\n"
        "1. Usa solo i dati osservati qui sotto.\n"
        "2. Non assumere task o appuntamenti mancanti.\n"
        "3. Rilancia il briefing dopo sync Google/Qdrant se il contesto e' incompleto.\n"
        "RISCHI: possibile loop o risposta incompleta del modello free.\n"
        f"DATI_ESTRATTI:\n{context}"
    )


def reflect_final_output(draft: str, *, tool_observations: list[str], user_id: str) -> str:
    current_draft = draft.strip()
    observation_context = _format_tool_observations(tool_observations)

    for attempt in range(1, 3):
        messages = [
            {
                "role": "system",
                "content": (
                    "Sei il reflection gate del Cockpit. Validazione obbligatoria prima di WhatsApp. "
                    "Confronta il draft con i dati estratti dai tool. Non aggiungere nuovi fatti. "
                    "Rispondi solo JSON con chiavi: approved, corrected_output, reason."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"user_id={user_id}\n"
                    f"attempt={attempt}\n\n"
                    f"TOOL_OBSERVATIONS:\n{observation_context}\n\n"
                    f"DRAFT:\n{current_draft}\n\n"
                    "Domande di gate: ogni appuntamento citato e' supportato dai tool? "
                    "Le azioni sono fattibili entro 24h o esplicitamente marcate come dato mancante? "
                    "Se no, correggi il draft mantenendo formato BLUF."
                ),
            },
        ]

        try:
            response = chat_completion_message(
                messages=messages,
                preferred_models=[AGENTIC_MODEL],
                temperature=0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            parsed = _extract_json_object(response.content) or {}
            reflection = ReflectionResult.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001
            push_dead_letter(
                stage="agent_reflection",
                reason="reflection_validation_failure",
                payload={"user_id": user_id, "attempt": attempt, "draft": current_draft[:1200]},
                error=str(exc),
            )
            continue

        corrected = reflection.corrected_output.strip()
        if reflection.approved and corrected:
            return corrected
        if reflection.approved:
            return current_draft
        if corrected:
            current_draft = corrected
            continue

    push_dead_letter(
        stage="agent_reflection",
        reason="reflection_gate_failed",
        payload={"user_id": user_id, "draft": draft[:1200], "tool_observations": observation_context[:3000]},
        error="reflection_failed_twice",
    )
    return UNSTABLE_OUTPUT


def run_agentic_loop(instruction: str, user_id: str, is_proactive: bool = False) -> str:
    mode = "proactive" if is_proactive else "reactive"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": MASTER_PROMPT},
        {
            "role": "user",
            "content": (
                f"mode={mode}\n"
                f"user_id={user_id}\n"
                f"now={_now_context()}\n\n"
                f"{instruction.strip()}"
            ),
        },
    ]

    executed_tools: set[str] = set()
    tool_trace: list[dict[str, Any]] = []
    tool_observations: list[str] = []

    for iteration in range(1, MAX_TOOL_LOOPS + 1):
        response = chat_completion_message(
            messages=messages,
            tools=COCKPIT_TOOLS,
            tool_choice="auto",
            preferred_models=[AGENTIC_MODEL],
            temperature=0.0,
            max_tokens=settings.openrouter_max_tokens,
            parallel_tool_calls=False,
        )

        if response.has_tool_calls:
            _append_assistant_tool_calls(messages, response)

            for tool_call in response.tool_calls:
                result = execute_cockpit_tool(tool_call.name, tool_call.arguments, user_id)
                llm_safe_result = _sanitize_tool_result_for_llm(result)
                if _tool_result_valid(result):
                    executed_tools.add(tool_call.name)
                    tool_observations.append(f"{tool_call.name}:\n{llm_safe_result}")
                tool_trace.append(
                    {
                        "iteration": iteration,
                        "tool": tool_call.name,
                        "args": tool_call.arguments,
                        "result_chars": len(llm_safe_result),
                    }
                )
                _append_tool_result(messages, tool_call, llm_safe_result)
            continue

        missing_tools = sorted(MANDATORY_TOOLS.difference(executed_tools))
        if missing_tools:
            messages.append({"role": "assistant", "content": response.content.strip()})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Mandatory context missing. Before final output you must call these tools: "
                        + ", ".join(missing_tools)
                    ),
                }
            )
            continue

        final_output = response.content.strip()
        if is_proactive:
            return reflect_final_output(final_output, tool_observations=tool_observations, user_id=user_id)
        return final_output

    fallback = _fallback_from_observations(tool_observations)
    push_dead_letter(
        stage="agentic_loop",
        reason="max_tool_loops_exceeded",
        payload={
            "instruction": instruction,
            "user_id": user_id,
            "is_proactive": is_proactive,
            "tool_trace": tool_trace,
        },
        error="max_tool_loops_exceeded",
    )
    return fallback
