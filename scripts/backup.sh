#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BACKUP_DIR="$ROOT_DIR/backups"
TS=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "Missing $ROOT_DIR/.env"
  exit 1
fi

# Load environment needed for pg_dump credentials.
# shellcheck disable=SC1091
. "$ROOT_DIR/.env"

POSTGRES_DB=${POSTGRES_DB:-lifecockpit}
POSTGRES_USER=${POSTGRES_USER:-lifecockpit}

PG_OUT="$BACKUP_DIR/postgres_${POSTGRES_DB}_${TS}.sql.gz"
QDRANT_OUT="$BACKUP_DIR/qdrant_storage_${TS}.tar.gz"

if command -v docker >/dev/null 2>&1; then
  DOCKER_BIN=docker
elif [ -x /usr/local/bin/docker ]; then
  DOCKER_BIN=/usr/local/bin/docker
elif [ -x /usr/bin/docker ]; then
  DOCKER_BIN=/usr/bin/docker
else
  echo "docker binary not found"
  exit 1
fi

echo "Creating PostgreSQL dump -> $PG_OUT"
cd "$ROOT_DIR"
"$DOCKER_BIN" compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip -9 > "$PG_OUT"

echo "Creating Qdrant storage archive -> $QDRANT_OUT"
"$DOCKER_BIN" compose exec -T qdrant sh -c "tar -czf - -C /qdrant/storage ." > "$QDRANT_OUT"

echo "Backup completed"
ls -lh "$PG_OUT" "$QDRANT_OUT"
