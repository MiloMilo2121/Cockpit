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

## Step 4 (completato)

- Pipeline RAG code-first implementata con strategie di chunking:
  - `recursive`
  - `semantic`
  - `agentic` (via OpenRouter free, con fallback automatico)
- Indicizzazione su Qdrant con metadati (`document_id`, `document_title`, `timestamp`, `source`, `confidence_score`).
- Retrieval ibrido:
  - dense search su vettori
  - scoring sparse per overlap keyword
  - rerank finale via OpenRouter free (quando disponibile)
- Endpoint attivi:
  - `POST /rag/documents/ingest` (job asincrono)
  - `POST /rag/query` (query sincrona)
- Spegnimento progressivo n8n completato nello Step 5.

## Step 5 (completato)

- Servizi n8n rimossi dal `docker-compose.yml` e dal reverse proxy.
- Hardening operativo introdotto:
  - script backup PostgreSQL + Qdrant
  - script healthcheck servizi core
  - runbook operativo in `docs/operations.md`

## Step 6 (completato)

- Servizio `file-watcher` aggiunto in `docker-compose.yml`.
- Monitoraggio ricorsivo cartelle locali con `watchdog`.
- Classificazione file con OpenRouter free (`OPENROUTER_FREE_MODELS`) e fallback euristico.
- Ingest automatico su RAG (`POST /rag/documents/ingest`) con metadati file e categoria.
- Estrazione task dal contenuto e invio opzionale a `POST /webhooks/inbox`.
- Persistenza stato dedup nel volume `file_watcher_state`.
