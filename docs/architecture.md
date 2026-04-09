# Architettura tecnica del Life Cockpit

## 1) Topologia

- Reverse proxy: `Caddy` (TLS automatico)
- Runtime code-first: `cockpit-core` (`FastAPI`) + `cockpit-worker` (`Celery`)
- Orchestrazione: `n8n` in queue mode (`n8n-web` + `n8n-worker`)
- Persistenza: `PostgreSQL`
- Messaggistica interna: `Redis`
- Memoria semantica: `Qdrant`
- Canale WhatsApp: `Evolution API`
- Fallback LLM locale: `Ollama`
- Privacy layer: `privacy-node` (redazione/restore PII)

Tutti i servizi sono su rete Docker interna `backend`; verso Internet è esposto solo `caddy`.

Nota migrazione: `n8n` resta attivo come fallback legacy finché i workflow critici non vengono portati in `cockpit-core`.

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

- Scomporre i flussi lunghi in sub-workflow (`Execute Sub-Workflow`).
- Implementare un workflow globale di errore (`Error Trigger`) con classificazione severità.
- Circuit breaker per integrazioni instabili:
  - stato su Redis (`closed/open/half-open`)
  - apertura su errori consecutivi oltre soglia
- Dead-letter queue per payload malformati o non processabili.

## 5) Sicurezza zero-trust

- Redazione PII locale obbligatoria prima di chiamate cloud LLM.
- Logging n8n con payload minimizzati e redatti.
- Credenziali solo in `.env` e mai in repository.
- Accesso UI n8n con basic auth + TLS.

## 6) RAG e qualità retrieval

- Strategia consigliata:
  - Ingest rapidi: chunking ricorsivo
  - Documenti densi: semantic/agentic chunking
- Ogni chunk deve includere metadati minimi:
  - `document_id`, `document_title`, `timestamp`, `source`, `confidence_score`
- Retrieval consigliato:
  - dense + sparse (ibrido)
  - reranking finale prima del prompt generation

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
