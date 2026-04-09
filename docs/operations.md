# Operations Runbook

## 1) Daily checks

- `make ps`
- `make healthcheck`
- `curl -fsS https://<DOMAIN_API>/ops/metrics`

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

- API errors: `docker compose logs -f cockpit-api cockpit-worker`
- Messaging errors: `docker compose logs -f evolution-api`
- Dead letter events: `GET /ops/dead-letter?limit=100`
- Circuit breaker status: `GET /ops/metrics`

## 5) Security baseline

- Keep `.env` off git.
- Rotate API keys monthly.
- Restrict VPS inbound ports to `22`, `80`, `443`.
- Use SSH keys only, disable password auth.
- Keep Docker and OS patched (`apt update && apt upgrade -y`).
