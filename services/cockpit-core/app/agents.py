from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.cockpit_tools import execute_cockpit_tool
from app.config import settings
from app.dead_letter import push_dead_letter
from app.openrouter_client import OpenRouterChatResponse, OpenRouterToolCall, chat_completion_message

AGENTIC_MODEL = "qwen/qwen3.6-plus:free"
MAX_AGENTIC_ITERATIONS = 5
MANDATORY_TOOLS = {"get_calendar_context", "search_qdrant_tasks"}

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
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {
                        "type": "string",
                        "description": "Finestra logica: today, tomorrow, next_24h, upcoming_week.",
                    },
                    "start": {
                        "type": "string",
                        "description": "Inizio ISO opzionale per una finestra custom.",
                    },
                    "end": {
                        "type": "string",
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
            "parameters": {
                "type": "object",
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
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider opzionale: gmail, drive, calendar. Default: gmail+drive.",
                    },
                    "window": {
                        "type": "string",
                        "description": "Finestra logica: today, tomorrow, next_24h, upcoming_week.",
                    },
                    "start": {
                        "type": "string",
                        "description": "Inizio ISO opzionale per una finestra custom.",
                    },
                    "end": {
                        "type": "string",
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


def _now_context() -> str:
    now = datetime.now(ZoneInfo(settings.tz))
    return now.isoformat(timespec="minutes")


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

    for iteration in range(1, MAX_AGENTIC_ITERATIONS + 1):
        response = chat_completion_message(
            messages=messages,
            tools=COCKPIT_TOOLS,
            tool_choice="auto",
            preferred_models=[AGENTIC_MODEL],
            temperature=0.0,
            max_tokens=settings.openrouter_max_tokens,
        )

        if response.has_tool_calls:
            _append_assistant_tool_calls(messages, response)

            for tool_call in response.tool_calls:
                result = execute_cockpit_tool(tool_call.name, tool_call.arguments, user_id)
                executed_tools.add(tool_call.name)
                tool_trace.append(
                    {
                        "iteration": iteration,
                        "tool": tool_call.name,
                        "args": tool_call.arguments,
                        "result_chars": len(result),
                    }
                )
                _append_tool_result(messages, tool_call, result)
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

        return response.content.strip()

    fallback = "FALLBACK: Agent loop timeout. Richiede intervento manuale."
    push_dead_letter(
        stage="agentic_loop",
        reason="max_iterations_exceeded",
        payload={
            "instruction": instruction,
            "user_id": user_id,
            "is_proactive": is_proactive,
            "tool_trace": tool_trace,
        },
        error=fallback,
    )
    return fallback
