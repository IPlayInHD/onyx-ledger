#!/usr/bin/env bash
# Run the browser E2E suite against a REAL backend.
#
# The point of these journeys is that the figures on screen are the figures the
# certified engine produced. Mocking the API would test the mock, so this boots
# the actual FastAPI service, builds the frontend, and drives Chromium against
# both.
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$(cd "$FRONTEND_DIR/../backend" && pwd)"
API_PORT="${ONYX_E2E_API_PORT:-8099}"
LOG="${ONYX_E2E_API_LOG:-/tmp/onyx_e2e_api.log}"

cleanup() {
  if [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo ">> starting backend on :$API_PORT"
(
  cd "$BACKEND_DIR"
  PYTHONPATH=. \
  ONYX_DATABASE_URL="${ONYX_DATABASE_URL:-postgresql+asyncpg://onyx_test@localhost:5432/onyx_b01}" \
  python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >"$LOG" 2>&1
) &
API_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS "http://127.0.0.1:$API_PORT/healthz" >/dev/null || {
  echo "backend did not become healthy; last log lines:" >&2
  tail -20 "$LOG" >&2
  exit 1
}
echo ">> backend healthy"

cd "$FRONTEND_DIR"
echo ">> building frontend"
npm run build >/dev/null

echo ">> running playwright"
ONYX_E2E_API="http://127.0.0.1:$API_PORT" npx playwright test "$@"
