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

# A previous run's uvicorn can still hold the port for a moment after its own
# cleanup. Wait for the port to clear, then REFUSE to continue if something
# else owns it: binding would fail, our server would exit, and the health check
# below would be answered by a stranger — producing a green suite that never
# tested the tree we just built.
for _ in $(seq 1 30); do
  if ! curl -fsS --max-time 2 "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1; then break; fi
  echo ">> :$API_PORT still held by an earlier run; waiting"
  sleep 1
done
if curl -fsS --max-time 2 "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1; then
  echo "port $API_PORT is already serving something; refusing to test against it" >&2
  exit 1
fi

echo ">> starting backend on :$API_PORT"
# `exec` matters: without it this subshell stays the parent and $! is the
# SUBSHELL's pid, so cleanup kills the shell and leaves uvicorn orphaned on the
# port — which is exactly how a later run ended up testing against a stale
# server. Replacing the subshell makes $! the real server.
(
  cd "$BACKEND_DIR"
  PYTHONPATH=. \
  ONYX_DATABASE_URL="${ONYX_DATABASE_URL:-postgresql+asyncpg://onyx_test@localhost:5432/onyx_b01}" \
  exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >"$LOG" 2>&1
) &
API_PID=$!

for _ in $(seq 1 60); do
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "backend process exited during startup; last log lines:" >&2
    tail -20 "$LOG" >&2
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done
# Health alone is not proof: it says SOMETHING answers, not that our server
# did. Requiring our own process to still be alive is what closes that gap.
kill -0 "$API_PID" 2>/dev/null || {
  echo "backend process is not running; last log lines:" >&2
  tail -20 "$LOG" >&2
  exit 1
}
curl -fsS "http://127.0.0.1:$API_PORT/healthz" >/dev/null || {
  echo "backend did not become healthy; last log lines:" >&2
  tail -20 "$LOG" >&2
  exit 1
}
echo ">> backend healthy (pid $API_PID)"

cd "$FRONTEND_DIR"
echo ">> building frontend"
npm run build >/dev/null

echo ">> running playwright"
ONYX_E2E_API="http://127.0.0.1:$API_PORT" npx playwright test "$@"
