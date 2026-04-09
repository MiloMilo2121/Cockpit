# Architettura tecnica del Life Cockpit

## 1) Topologia

- Reverse proxy: `Caddy` (TLS automatico)
- Runtime code-first: `cockpit-core` (`FastAPI`) + `cockpit-worker` (`Celery`)
- Frontend command center: `cockpit-ui` (`React + Vite`)
- Persistenza: `PostgreSQL`
- Messaggistica interna: `Redis`
- Memoria semantica: `Qdrant`
- Canale WhatsApp: `Evolution API`
- Connettori Google: `Gmail`, `Drive`, `Calendar` via OAuth multi-account
- Fallback LLM locale: `Ollama`
- Ingest file locale: `file-watcher` (watchdog + classificazione + push RAG)
- Privacy layer: `privacy-node` (redazione/restore PII)

Tutti i servizi sono su rete Docker interna `backend`; verso Internet è esposto solo `caddy`.

## 2) Flusso dati raccomandato (messaggistica)

1. `Evolution API` riceve evento WhatsApp e invia webhook a `cockpit-api`.
2. `cockpit-api` applica dedup (`source + source_message_id`) su PostgreSQL.
3. Eventi WhatsApp vengono bufferizzati su Redis e aggregati da `cockpit-worker`.
4. `cockpit-worker` invia testo aggregato a `privacy-node /redact`.
5. Testo redatto va a LLM router (OpenRouter primario, Ollama fallback).
6. Output LLM torna a `privacy-node /restore`.
7. Il risultato viene reso disponibile via endpoint `/jobs/{job_id}`.

## 3) Routing LLM ibrido

- Primario: OpenRouter con modelli gratuiti (`:free`) configurati in `OPENROUTER_FREE_MODELS`.
- Multi-agent in due fasi:
  - Router intent -> selezione agente specialistico.
  - Specialista -> output operativo strutturato.
- Resilienza:
  - retry con backoff esponenziale + jitter
  - circuit breaker su OpenRouter
  - fallback locale degradato (se abilitato)
  - dead-letter queue persistita su PostgreSQL

## 4) Affidabilità e resilienza

- Scomporre i flussi lunghi in task/moduli indipendenti nel worker Celery.
- Instradare gli errori critici verso dead-letter con classificazione severità.
- Circuit breaker per integrazioni instabili:
  - stato su Redis (`closed/open/half-open`)
  - apertura su errori consecutivi oltre soglia
- Dead-letter queue per payload malformati o non processabili.

## 5) Sicurezza zero-trust

- Redazione PII locale obbligatoria prima di chiamate cloud LLM.
- Logging applicativo con payload minimizzati e redatti.
- Credenziali solo in `.env` e mai in repository.
- Accesso API solo via Caddy con TLS.

## 6) RAG e qualità retrieval

- Ingest:
  - endpoint `POST /rag/documents/ingest` (async)
  - chunking selezionabile: `recursive`, `semantic`, `agentic`
- Metadati per chunk:
  - `document_id`, `document_title`, `timestamp`, `source`, `confidence_score`
- Retrieval:
  - dense search su Qdrant
  - sparse score su overlap keyword
  - fusione ibrida (`RAG_DENSE_WEIGHT`, `RAG_SPARSE_WEIGHT`)
  - rerank OpenRouter free su top candidati
- Query:
  - endpoint `POST /rag/query` (sync) con `top_k` e `rerank`

## 7) Scheduling predittivo

Formula base priorità:

`P = E (Easiness) * I (Importance) * U (Urgency)`

Regole pratiche:

- Task cognitivamente pesanti nelle fasce ad alta energia.
- Task amministrativi in batch nelle fasce a bassa energia.
- Inserire buffer temporale 15-20% tra blocchi.
- Alert burnout se hard constraints superano capacità oraria.

## 8) Operatività

- Bootstrap: `cp .env.example .env && docker compose up -d --build`
- Audit rapido: `docker compose ps`
- Diagnosi: `docker compose logs -f <service>`

## 9) File ingestion continuo (Step 6)

- Il servizio `file-watcher` osserva `data/inbox` e indicizza file testuali in RAG.
- Dedup: fingerprint `sha1` persistita in `/state/file_state.json`.
- Arricchimento:
  - categorizzazione (`finance|health|legal|work|personal|operations|learning|uncategorized`)
  - priorità (`low|medium|high`)
  - estrazione task azionabili
- Sink:
  - ingest asincrono su `POST /rag/documents/ingest`
  - opzionale evento operativo su `POST /webhooks/inbox`

## 10) Google sync canonico (Step 7)

- `OAuth state` persistito su PostgreSQL.
- `Google accounts` multi-account per stesso `user_id` logico.
- `Raw events` immutabili per audit e provenance.
- `Sync cursors` separati per provider:
  - Gmail: `history_id`
  - Drive: `page_token`
  - Calendar: `sync_token:<calendar_id>`
- `External documents` normalizzati e reindicizzati in RAG con replace per `document_id`.
- Sync eseguita via task Celery, non nel thread HTTP.

## 11) UI command center (Step 8)

- `cockpit-ui` espone una plancia top-level su `DOMAIN_APP`.
- Browser e backend condividono host UI tramite proxy Caddy:
  - `/` -> `cockpit-ui`
  - `/api/*` -> `cockpit-api`
- Endpoint aggregato `GET /dashboard/overview` usato dalla UI per evitare fan-out eccessivo lato browser.
