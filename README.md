# Personal Life Cockpit (Predictive)

Base infrastrutturale self-hosted per un Life Cockpit predittivo.  
Runtime completamente code-first: `cockpit-core` (`FastAPI + Celery`) con pipeline multi-agent, RAG e resilienza operativa.

## Stack

- `cockpit-core` (`FastAPI`) per webhook/API applicative
- `cockpit-worker` (`Celery`) per orchestrazione asincrona e retry/backoff
- `cockpit-ui` (`React + Vite`) come plancia comando top-level
- `PostgreSQL 16` per stato/credenziali/log
- `Redis 7` come broker/caching
- `Qdrant` per retrieval vettoriale
- `Evolution API v2` per integrazione WhatsApp
- `Google OAuth + Gmail + Drive + Calendar` per memoria multi-account
- `Ollama` per fallback locale
- `file-watcher` per ingest automatico file locali -> RAG + task extraction
- `Caddy` per reverse proxy + TLS automatico
- `privacy-node` (FastAPI + Presidio) per redazione PII locale

## Prerequisiti VPS (minimo consigliato)

- Ubuntu `24.04 LTS`
- `2 vCPU`, `4 GB RAM`, `64 GB NVMe`
- Docker Engine + Docker Compose plugin
- DNS già configurato per:
  - `DOMAIN_API`
  - `DOMAIN_APP`
  - `DOMAIN_EVOLUTION`

## Quickstart

1. Copia variabili ambiente:

```bash
cp .env.example .env
```

2. Aggiorna almeno queste variabili in `.env`:

- `LETSENCRYPT_EMAIL`
- `DOMAIN_API`
- `DOMAIN_APP`
- `DOMAIN_EVOLUTION`
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `OPENROUTER_API_KEY` (obbligatoria per Step 3)
- `OPENROUTER_FREE_MODELS` (lista modelli gratuiti OpenRouter, separati da virgola)
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_OAUTH_REDIRECT_URL` (es. `https://<DOMAIN_API>/google/callback`)
- `SMART_BUFFER_SECONDS` (default 12)
- `FILE_WATCHER_ALLOWED_EXTENSIONS` (estensioni indicizzate in Step 6)
- `RAG_COLLECTION_NAME` (default `life_cockpit_memory`)
- `RAG_VECTOR_SIZE` (default `384`)
- `QDRANT_API_KEY`
- `EVOLUTION_API_KEY`
- `PRIVACY_SALT`

3. Avvia stack:

```bash
docker compose up -d --build
```

4. Verifica:

```bash
docker compose ps
docker compose logs -f cockpit-api cockpit-worker
```

## Endpoint attesi

- Cockpit API: `https://<DOMAIN_API>`
- Cockpit UI: `https://<DOMAIN_APP>`
- Evolution API: `https://<DOMAIN_EVOLUTION>`
- Privacy node (interno): `http://privacy-node:8100`

## Test rapido cockpit-core

1. Invia evento al webhook:

```bash
curl -X POST https://<DOMAIN_API>/webhooks/inbox \
  -H "Content-Type: application/json" \
  -d '{
    "source": "web",
    "user_id": "user-1",
    "message": "Domani voglio pianificare 3 task ad alta priorità",
    "metadata": {}
  }'
```

Esempio WhatsApp con dedup id:

```bash
curl -X POST https://<DOMAIN_API>/webhooks/inbox \
  -H "Content-Type: application/json" \
  -d '{
    "source": "whatsapp",
    "user_id": "393331112223",
    "message": "parte 1 del messaggio",
    "metadata": {
      "message_id": "wamid.HBgLMzkzMzMxMTEyMjIzFQIAEhgg...",
      "fromMe": false
    }
  }'
```

2. Controlla stato job:

```bash
curl https://<DOMAIN_API>/jobs/<JOB_ID>
```

## Step 2 attivo (buffer + dedup + loop prevention)

- Smart buffering per `source=whatsapp` su Redis.
- Dedup idempotente su PostgreSQL (`source + source_message_id`).
- Loop prevention: eventi `fromMe=true` o `direction=outbound` vengono ignorati.
- Risposte webhook possibili:
  - `processing` con `job_id`
  - `duplicate` con `job_id` precedente (se disponibile)
  - `ignored` per self-message

## Step 3 attivo (multi-agent + resilienza)

- Router multi-agent: `RAG_ANALYST_AGENT`, `COMMUNICATION_AGENT`, `SYSTEM_MAINTENANCE_AGENT`, `GENERAL_PLANNER_AGENT`.
- Solo modelli OpenRouter gratuiti (`:free`) tramite `OPENROUTER_FREE_MODELS`.
- Circuit breaker su OpenRouter con variabili:
  - `CIRCUIT_BREAKER_FAILURE_THRESHOLD`
  - `CIRCUIT_BREAKER_OPEN_SECONDS`
- Dead-letter queue persistita su PostgreSQL (`cockpit_dead_letter_events`).
- Endpoint operativi:
  - `GET /ops/metrics`
  - `GET /ops/dead-letter?limit=50`

## Step 4 attivo (RAG completo)

- Ingest documenti asincrono: `POST /rag/documents/ingest`
- Query RAG sincrona: `POST /rag/query`
- Chunking supportato:
  - `recursive`
  - `semantic`
  - `agentic` (OpenRouter free, fallback automatico su semantic)
- Retrieval ibrido:
  - dense (Qdrant)
  - sparse keyword overlap
  - rerank finale OpenRouter free

## Step 5 completato (decommission n8n + hardening)

- `n8n` rimosso da `docker-compose.yml`.
- Reverse proxy Caddy allineato solo a:
  - `DOMAIN_API`
  - `DOMAIN_EVOLUTION`
- Hardening operativo introdotto:
  - `make backup` (`scripts/backup.sh`)
  - `make healthcheck` (`scripts/healthcheck.sh`)
  - runbook operativo in `docs/operations.md`

## Step 6 completato (watchdog file ingestion + auto-categorizzazione)

- Nuovo servizio `file-watcher` in `docker-compose.yml`.
- Monitoraggio cartella locale `data/inbox` (ricorsivo) con dedup su fingerprint persistita.
- Classificazione file con modelli OpenRouter `:free` (fallback euristico locale).
- Ingest automatico su RAG (`POST /rag/documents/ingest`) con metadati:
  - `path`, `fingerprint`, `category`, `priority`, `tasks`.
- Opzionale push su `POST /webhooks/inbox` dei task estratti dal file.
- Copia file processati in `data/processed`.

Esempio rapido:

```bash
echo "- TODO: pagare F24 entro venerdi" > data/inbox/finanza_oggi.md
make logs-watcher
```

## Step 7 completato (Google multi-account sync foundation)

- OAuth Google multi-account con endpoint:
  - `POST /integrations/google/auth-url`
  - `POST /integrations/google/exchange`
- Persistenza canonica in PostgreSQL:
  - account Google
  - stati OAuth
  - cursori di sync incrementale
  - raw events
  - documenti esterni normalizzati
- Sync manuale/bootstrapped via Celery:
  - Gmail (`historyId`)
  - Drive (`pageToken`)
  - Calendar (`syncToken`)
- Endpoint operativi:
  - `GET /integrations/google/accounts`
  - `POST /integrations/google/accounts/{account_id}/sync`
  - `GET /integrations/google/accounts/{account_id}/cursors`
  - `GET /integrations/google/accounts/{account_id}/events`

Esempio auth URL:

```bash
curl -X POST https://<DOMAIN_API>/integrations/google/auth-url \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "marco",
    "redirect_uri": "https://<DOMAIN_API>/google/callback"
  }'
```

Esempio sync manuale:

```bash
curl -X POST https://<DOMAIN_API>/integrations/google/accounts/1/sync \
  -H "Content-Type: application/json" \
  -d '{
    "providers": ["gmail", "drive", "calendar"],
    "bootstrap": false
  }'
```

## Step 8 completato (Cockpit UI HQ)

- Nuovo servizio `cockpit-ui` in [docker-compose.yml](/Users/marcomilanello/Documents/cockpit/docker-compose.yml).
- Reverse proxy Caddy su `DOMAIN_APP` con:
  - UI su `/`
  - backend proxato su `/api`
- Nuovo endpoint aggregato `GET /dashboard/overview` per alimentare la plancia.
- UI stile command-center con:
  - posture sistema
  - contatori memoria/segnali/account
  - operational feed
  - source mesh
  - next build priorities

Esempio ingest:

```bash
curl -X POST https://<DOMAIN_API>/rag/documents/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Piano Q2",
    "source": "notion",
    "content": "Testo lungo del documento...",
    "chunking_strategy": "semantic",
    "metadata": {"workspace": "life-cockpit"}
  }'
```

Esempio query:

```bash
curl -X POST https://<DOMAIN_API>/rag/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Quali sono le priorità operative del Q2?",
    "top_k": 5,
    "rerank": true
  }'
```

## Pattern operativi consigliati

- Smart Buffering WhatsApp: usa Redis per aggregare frammenti in finestra temporale (es. 8-20s).
- Loop prevention: marca ogni messaggio inviato dal bot con `message_id` persistito su PostgreSQL.
- Retry intelligente: backoff esponenziale con jitter per 429/5xx.
- Circuit breaker: apri circuito su errori ripetuti per evitare saturazione integrazioni esterne.
- PII redaction: passa sempre dal `privacy-node` prima di chiamate a LLM cloud.

## Note su Evolution API

Le variabili ambiente di Evolution API possono cambiare tra release minor. La base in `docker-compose.yml` è pronta per v2 ma va sempre validata sulla release specifica usata in produzione.

## Documentazione progetto

- Architettura: `docs/architecture.md`
- Piano migrazione: `docs/migration-plan.md`
- Operatività: `docs/operations.md`
- Prompt orchestratore: `flows/master_prompt_cognitive_orchestrator.xml`
- Reverse proxy: `infra/caddy/Caddyfile`

## Sicurezza minima

- Non committare mai `.env`
- Ruota periodicamente API keys e password
- Mantieni chiuso tutto su rete `backend` interna Docker
- Espone pubblicamente solo Caddy (80/443)
