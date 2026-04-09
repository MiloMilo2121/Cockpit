#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ ! -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  echo "Created $ROOT_DIR/.env from template"
fi

echo "Review and update secrets in $ROOT_DIR/.env before starting containers."
echo "Then run: docker compose up -d --build"
