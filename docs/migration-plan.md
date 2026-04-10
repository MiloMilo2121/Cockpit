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

- Router multi-agent sostituito da loop ReAct code-first in `agents.py`.
- Qwen via OpenRouter free (`qwen/qwen3-next-80b-a3b-instruct:free`) orchestra tool locali e produce output BLUF.
- Tool matrix implementata:
  - `get_calendar_context`
  - `search_qdrant_tasks`
  - `query_raw_events`
- Strict tool schema + validazione Pydantic degli argomenti tool.
- Hard cap del loop ReAct a 4 turni tool consecutivi.
- Reflection gate JSON sui messaggi WhatsApp proattivi.
- Cache semantica Redis per evitare chiamate OpenRouter duplicate entro 5 minuti.
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

## Step 7 (completato)

- OAuth Google multi-account implementato in `cockpit-core`.
- Tabelle aggiunte:
  - `cockpit_google_oauth_states`
  - `cockpit_google_accounts`
  - `cockpit_sync_cursors`
  - `cockpit_raw_events`
  - `cockpit_external_documents`
- Sync engine incrementale aggiunto:
  - Gmail via `historyId`
  - Drive via `pageToken`
  - Calendar via `syncToken`
- Endpoint aggiunti per auth, exchange, list account, sync manuale, cursori ed eventi recenti.

## Step 8 (completato)

- Frontend `cockpit-ui` aggiunto come servizio separato.
- Reverse proxy Caddy esteso con `DOMAIN_APP` e routing `/api`.
- Endpoint `GET /dashboard/overview` introdotto per alimentare la UI HQ.
- Prima versione della plancia comando top-level implementata.

## Step 9 (completato)

- `cockpit-beat` aggiunto a `docker-compose.yml`.
- Beat schedule attivo:
  - briefing mattutino alle 07:30
  - correzione di meta giornata alle 14:00
  - anomaly scan dead-letter ogni 15 minuti
  - sync Google silenzioso ogni 3 ore
- Task Celery `cockpit.proactive_execution` collegato al loop ReAct.
- Invio WhatsApp tramite Evolution API configurabile con `EVOLUTION_INSTANCE` e `PROACTIVE_WHATSAPP_NUMBER`.
