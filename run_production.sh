#!/usr/bin/env bash
# Production-style server: multiple workers + long timeout (Koyeb / VPS / Docker image CMD).
# Set REDIS_URL for session sharing across workers.
set -euo pipefail
cd "$(dirname "$0")"
# Koyeb and most platforms set PORT; API_PORT is this project's own name for it.
export API_PORT="${PORT:-${API_PORT:-8000}}"
export API_RELOAD="${API_RELOAD:-false}"
if [[ -n "${WEB_CONCURRENCY:-}" ]]; then
  WORKERS="$WEB_CONCURRENCY"
elif [[ -n "${REDIS_URL:-}" ]]; then
  WORKERS=4
else
  # In-memory sessions live in one process; more workers would lose students' sessions.
  echo "REDIS_URL not set: running 1 worker (set REDIS_URL to run 4)" >&2
  WORKERS=1
fi
exec gunicorn main:app \
  -k uvicorn.workers.UvicornWorker \
  -w "$WORKERS" \
  --bind "0.0.0.0:${API_PORT}" \
  --timeout 120 \
  --graceful-timeout 30 \
  --access-logfile - \
  --error-logfile -
