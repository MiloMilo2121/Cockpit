# Personal Life Cockpit (Predictive)

Base infrastrutturale self-hosted per un Life Cockpit predittivo.  
Attualmente include sia stack `n8n` legacy sia il nuovo runtime code-first `cockpit-core` (`FastAPI + Celery`) per migrazione progressiva.

## Stack

- `n8n` (web + worker) in queue mode
- `cockpit-core` (`FastAPI`) per webhook/API applicative
- `cockpit-worker` (`Celery`) per orchestrazione asincrona e retry/backoff
- `PostgreSQL 16` per stato/credenziali/log
- `Redis 7` come broker/caching
- `Qdrant` per retrieval vettoriale
- `Evolution API v2` per integrazione WhatsApp
- `Ollama` per fallback locale
- `Caddy` per reverse proxy + TLS automatico
- `privacy-node` (FastAPI + Presidio) per redazione PII locale

## Prerequisiti VPS (minimo consigliato)

- Ubuntu `24.04 LTS`
- `2 vCPU`, `4 GB RAM`, `64 GB NVMe`
- Docker Engine + Docker Compose plugin
- DNS già configurato per:
  - `DOMAIN_N8N`
  - `DOMAIN_API`
  - `DOMAIN_EVOLUTION`

## Quickstart

1. Copia variabili ambiente:

```bash
cp .env.example .env
```

2. Aggiorna almeno queste variabili in `.env`:

- `LETSENCRYPT_EMAIL`
- `DOMAIN_N8N`
- `DOMAIN_API`
- `DOMAIN_EVOLUTION`
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `N8N_ENCRYPTION_KEY`
- `N8N_BASIC_AUTH_PASSWORD`
- `OPENROUTER_API_KEY` (obbligatoria per Step 3)
- `OPENROUTER_FREE_MODELS` (lista modelli gratuiti OpenRouter, separati da virgola)
- `SMART_BUFFER_SECONDS` (default 12)
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

- n8n: `https://<DOMAIN_N8N>`
- Cockpit API: `https://<DOMAIN_API>`
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
- Prompt orchestratore: `flows/master_prompt_cognitive_orchestrator.xml`
- Reverse proxy: `infra/caddy/Caddyfile`

## Sicurezza minima

- Non committare mai `.env`
- Ruota periodicamente API keys e password
- Mantieni chiuso tutto su rete `backend` interna Docker
- Espone pubblicamente solo Caddy (80/443)
