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

1. `Evolution API` riceve evento WhatsApp e invia webhook a n8n.
2. n8n applica smart buffering su `Redis` (aggregazione messaggi finestra breve).
3. n8n invia testo aggregato a `privacy-node /redact`.
4. Testo redatto va a LLM router (OpenRouter primario, Ollama fallback).
5. Output LLM torna a `privacy-node /restore`.
6. n8n invia risposta finale verso Evolution API.

## 3) Routing LLM ibrido

- Primario: provider cloud con modelli frontier per task complessi.
- Fallback: `Ollama` locale per continuità operativa.
- Politica suggerita:
  - su `429/5xx` => retry con backoff esponenziale + jitter
  - oltre soglia tentativi => fallback locale

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
