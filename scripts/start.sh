#!/bin/sh
# Container entrypoint: apply database migrations, then start the API.
# One worker on purpose: system state, caches and the live stream are in-process,
# and duplicate analysis jobs are prevented in-process.
set -e

cd /app/backend 2>/dev/null || cd "$(dirname "$0")/../backend"

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is not set. On Railway, add a PostgreSQL service and reference its DATABASE_URL." >&2
  exit 1
fi

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Applying database migrations..."
  alembic upgrade head
fi

LEVEL=$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips "*" \
  --log-level "$LEVEL"
