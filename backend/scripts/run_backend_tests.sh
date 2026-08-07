#!/usr/bin/env bash
# Provision a throwaway PostgreSQL test DB, apply the schema, and run the suite.
# Assumes a running PostgreSQL 16 with pgvector reachable via $PGHOST/$PGPORT.
set -euo pipefail
PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"
CONN="postgres://${SUPER}@/onyx_test?host=${PGHOST}&port=${PGPORT}"

psql "$BASE" -q -c "DROP DATABASE IF EXISTS onyx_test;" -c "CREATE DATABASE onyx_test;"
DATABASE_URL="$CONN" ./scripts/apply_schema.sh
# Created if absent, never dropped. PostgreSQL roles are CLUSTER-wide, so
# DROP/CREATE here reached outside this database: a concurrently running job
# authenticated as `onyx_test` kept its connections, but grants are held by role
# OID, and the recreated role has a new one — so that job's next statement
# failed with "permission denied for table ..." for reasons that had nothing to
# do with the code under test. Idempotent creation removes the hazard entirely.
psql "$CONN" -q -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_test') THEN
    CREATE ROLE onyx_test LOGIN PASSWORD 'test' IN ROLE onyx_app_rw;
  END IF;
END
$$;
GRANT onyx_app_rw TO onyx_test;
SQL

export ONYX_DATABASE_URL="postgresql+asyncpg://onyx_test:test@/onyx_test?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="test-secret-at-least-32-bytes-long-000"

# Arguments REPLACE the default target rather than adding to it. `tests/ $@`
# meant `run_backend_tests.sh tests/security` ran the whole suite and reported
# the time as if it had run one directory — so a targeted CI stage silently
# duplicated the full-suite job. Bare flags (-q, -x) still apply to the default.
TARGETS=()
for arg in "$@"; do
  [[ "$arg" != -* ]] && TARGETS+=("$arg")
done
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=(tests/)

PYTHONPATH=. python -m pytest "${TARGETS[@]}" -q "$@"
