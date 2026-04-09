#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "Missing $ROOT_DIR/.env"
  exit 1
fi

# shellcheck disable=SC1091
. "$ROOT_DIR/.env"

DOMAIN_API=${DOMAIN_API:-}
DOMAIN_EVOLUTION=${DOMAIN_EVOLUTION:-}

if [ -z "$DOMAIN_API" ] || [ -z "$DOMAIN_EVOLUTION" ]; then
  echo "DOMAIN_API and DOMAIN_EVOLUTION must be set in .env"
  exit 1
fi

echo "Checking cockpit API..."
curl -fsS "https://${DOMAIN_API}/health" >/dev/null

echo "Checking ops metrics..."
curl -fsS "https://${DOMAIN_API}/ops/metrics" >/dev/null

echo "Checking Evolution API endpoint..."
curl -fsS "https://${DOMAIN_EVOLUTION}/" >/dev/null

echo "Healthcheck passed"
