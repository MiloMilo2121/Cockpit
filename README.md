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
- `OPENROUTER_API_KEY` (opzionale ma consigliata)
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

2. Controlla stato job:

```bash
curl https://<DOMAIN_API>/jobs/<JOB_ID>
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
- Prompt orchestratore: `flows/master_prompt_cognitive_orchestrator.xml`
- Reverse proxy: `infra/caddy/Caddyfile`

## Sicurezza minima

- Non committare mai `.env`
- Ruota periodicamente API keys e password
- Mantieni chiuso tutto su rete `backend` interna Docker
- Espone pubblicamente solo Caddy (80/443)
