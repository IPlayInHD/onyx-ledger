#!/usr/bin/env bash
# Provision a throwaway PostgreSQL test DB, apply the schema, and run the suite.
# Assumes a running PostgreSQL 16 with pgvector reachable via $PGHOST/$PGPORT.
set -euo pipefail
PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"
CONN="postgres://${SUPER}@/onyx_test?host=${PGHOST}&port=${PGPORT}"

psql "$BASE" -q -c "DROP DATABASE IF EXISTS onyx_test;" -c "CREATE DATABASE onyx_test;"
DATABASE_URL="$CONN" ./scripts/apply_schema.sh
psql "$CONN" -q -c "DROP ROLE IF EXISTS onyx_test;" \
  -c "CREATE ROLE onyx_test LOGIN PASSWORD 'test' IN ROLE onyx_app_rw;"

export ONYX_DATABASE_URL="postgresql+asyncpg://onyx_test:test@/onyx_test?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="test-secret-at-least-32-bytes-long-000"
PYTHONPATH=. python -m pytest tests/ -q "$@"
