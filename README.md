# Personal Life Cockpit (Predictive)

Base infrastrutturale self-hosted per un Life Cockpit predittivo con orchestrazione `n8n` in queue mode, RAG su `Qdrant`, messaggistica via `Evolution API`, fallback LLM locale con `Ollama` e layer privacy locale.

## Stack

- `n8n` (web + worker) in queue mode
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
  - `DOMAIN_EVOLUTION`

## Quickstart

1. Copia variabili ambiente:

```bash
cp .env.example .env
```

2. Aggiorna almeno queste variabili in `.env`:

- `LETSENCRYPT_EMAIL`
- `DOMAIN_N8N`
- `DOMAIN_EVOLUTION`
- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `N8N_ENCRYPTION_KEY`
- `N8N_BASIC_AUTH_PASSWORD`
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
docker compose logs -f n8n-web
```

## Endpoint attesi

- n8n: `https://<DOMAIN_N8N>`
- Evolution API: `https://<DOMAIN_EVOLUTION>`
- Privacy node (interno): `http://privacy-node:8100`

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
- Prompt orchestratore: `flows/master_prompt_cognitive_orchestrator.xml`
- Reverse proxy: `infra/caddy/Caddyfile`

## Sicurezza minima

- Non committare mai `.env`
- Ruota periodicamente API keys e password
- Mantieni chiuso tutto su rete `backend` interna Docker
- Espone pubblicamente solo Caddy (80/443)
