# Migrazione n8n -> Code-First (sequenziale)

## Step 1 (completato)

- Introduzione `cockpit-core` (`FastAPI`) e `cockpit-worker` (`Celery`).
- Endpoint base:
  - `POST /webhooks/inbox`
  - `GET /jobs/{job_id}`
- Routing LLM iniziale con fallback OpenRouter -> Ollama.
- Redazione PII locale integrata tramite `privacy-node`.

## Step 2

- Migrare Smart Buffering (attualmente previsto in n8n) in Redis direttamente nel worker.
- Gestione dedup/loop prevention con `message_id` su PostgreSQL.

## Step 3

- Implementare orchestrazione multi-agent code-first (router intent -> sub-moduli).
- Strato di policy (retry, circuit breaker, dead-letter queue) con metriche.

## Step 4

- Migrare pipeline RAG completa (chunking, indexing, retrieval, rerank).
- Spegnimento progressivo dei workflow n8n sostituiti.

## Step 5

- Rimozione finale servizi n8n dal compose.
- Hardening operativo (backup, alerting, dashboards, runbook incidenti).
