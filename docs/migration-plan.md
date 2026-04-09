# Migrazione n8n -> Code-First (sequenziale)

## Step 1 (completato)

- Introduzione `cockpit-core` (`FastAPI`) e `cockpit-worker` (`Celery`).
- Endpoint base:
  - `POST /webhooks/inbox`
  - `GET /jobs/{job_id}`
- Routing LLM iniziale con fallback OpenRouter -> Ollama.
- Redazione PII locale integrata tramite `privacy-node`.

## Step 2 (completato)

- Smart Buffering implementato in Redis per eventi `source=whatsapp`.
- Task Celery differita (`process_buffered_session`) con aggregazione messaggi in finestra temporale.
- Dedup idempotente su PostgreSQL (`source + source_message_id`) con mappatura al `job_id`.
- Loop prevention applicata su payload self-message (`fromMe=true` / `direction=outbound`).

## Step 3 (completato)

- Router multi-agent code-first implementato (`RAG_ANALYST_AGENT`, `COMMUNICATION_AGENT`, `SYSTEM_MAINTENANCE_AGENT`, `GENERAL_PLANNER_AGENT`).
- Esecuzione specialistica via OpenRouter con modelli `:free` configurabili da env.
- Circuit breaker su integrazione OpenRouter con soglia errori e finestra di apertura.
- Dead-letter queue persistita su PostgreSQL (`cockpit_dead_letter_events`).
- Metriche operative esposte via endpoint `/ops/metrics` e dead-letter via `/ops/dead-letter`.

## Step 4

- Migrare pipeline RAG completa (chunking, indexing, retrieval, rerank).
- Spegnimento progressivo dei workflow n8n sostituiti.

## Step 5

- Rimozione finale servizi n8n dal compose.
- Hardening operativo (backup, alerting, dashboards, runbook incidenti).
