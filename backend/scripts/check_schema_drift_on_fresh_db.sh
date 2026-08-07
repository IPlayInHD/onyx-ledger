#!/usr/bin/env bash
# Run the schema-drift gate against a database built from scratch by Alembic.
#
# Deliberately not run against a developer's working database: the question is
# whether the MIGRATION CHAIN produces the schema the code believes in, and a
# database somebody has been experimenting on cannot answer that.
set -euo pipefail
cd "$(dirname "$0")/.."

PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
DB="onyx_drift"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"

export ONYX_DATABASE_URL_SYNC="postgresql+psycopg2://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_DATABASE_URL="postgresql+asyncpg://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="${ONYX_JWT_SECRET:-schema-drift-gate-secret-32-bytes-xx}"

cleanup() { psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null 2>&1 || true; }
trap cleanup EXIT

psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" -c "CREATE DATABASE ${DB};"
PYTHONPATH=. alembic upgrade head > /dev/null
PYTHONPATH=. python scripts/check_schema_drift.py
