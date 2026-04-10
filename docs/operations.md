# Operations Runbook

## 1) Daily checks

- `make ps`
- `make healthcheck`
- `curl -fsS https://<DOMAIN_API>/ops/metrics`
- `make logs-watcher` (controllo ingest file automatico)
- `make logs-ui`
- `curl -fsS https://<DOMAIN_API>/integrations/google/accounts`

## 2) Backups

### Manual backup

```bash
make backup
```

Artifacts are written to `backups/`:

- `postgres_<db>_<timestamp>.sql.gz`
- `qdrant_storage_<timestamp>.tar.gz`

### Suggested cadence

- PostgreSQL + Qdrant backup every 6 hours.
- Keep at least 7 daily restore points.

## 3) Restore outline

1. Stop stack:

```bash
docker compose down
```

2. Restore PostgreSQL dump into running postgres container (after start):

```bash
gunzip -c backups/postgres_<db>_<timestamp>.sql.gz | docker compose exec -T postgres psql -U <user> -d <db>
```

3. Restore Qdrant storage tarball to `/qdrant/storage` (with service stopped), then restart.

## 4) Incident triage

- API errors: `docker compose logs -f cockpit-api cockpit-worker cockpit-beat`
- File ingest errors: `docker compose logs -f file-watcher`
- Messaging errors: `docker compose logs -f evolution-api`
- Proactive scheduler: `docker compose logs -f cockpit-beat cockpit-worker`
- Dead letter events: `GET /ops/dead-letter?limit=100`
- Circuit breaker status: `GET /ops/metrics`

## 5) File watcher flow (Step 6)

1. Copia o salva file testuali in `data/inbox/`.
2. Verifica log:

```bash
make logs-watcher
```

3. Controlla che `cockpit-api` abbia job RAG in coda:

```bash
docker compose logs -f cockpit-api cockpit-worker cockpit-beat
```

## 6) Google OAuth + sync (Step 7)

1. Configura in `.env`:
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_OAUTH_REDIRECT_URL` (`https://<DOMAIN_API>/google/callback`)

2. Richiedi auth URL:

```bash
curl -X POST https://<DOMAIN_API>/integrations/google/auth-url \
  -H "Content-Type: application/json" \
  -d '{"user_id":"marco","redirect_uri":"https://<DOMAIN_API>/google/callback"}'
```

3. Dopo il redirect Google, scambia il `code`:

```bash
curl -X POST https://<DOMAIN_API>/integrations/google/exchange \
  -H "Content-Type: application/json" \
  -d '{"state":"<STATE>","code":"<CODE>","redirect_uri":"https://<DOMAIN_API>/google/callback"}'
```

Se usi `https://<DOMAIN_API>/google/callback` come redirect URI, il cockpit esegue exchange e bootstrap sync anche direttamente via callback GET.

4. Verifica account e cursori:

```bash
curl -fsS https://<DOMAIN_API>/integrations/google/accounts
curl -fsS https://<DOMAIN_API>/integrations/google/accounts/<ACCOUNT_ID>/cursors
```

5. Lancia sync manuale quando serve:

```bash
curl -X POST https://<DOMAIN_API>/integrations/google/accounts/<ACCOUNT_ID>/sync \
  -H "Content-Type: application/json" \
  -d '{"providers":["gmail","drive","calendar"],"bootstrap":false}'
```

## 7) Security baseline

- Keep `.env` off git.
- Rotate API keys monthly.
- Restrict VPS inbound ports to `22`, `80`, `443`.
- Use SSH keys only, disable password auth.
- Keep Docker and OS patched (`apt update && apt upgrade -y`).

## 8) Cockpit UI HQ (Step 8)

- Configura `DOMAIN_APP` in `.env`.
- Dopo `docker compose up -d --build`, apri:
  - `https://<DOMAIN_APP>`
- Log UI:

```bash
make logs-ui
```
