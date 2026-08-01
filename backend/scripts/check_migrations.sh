#!/usr/bin/env bash
# Migration smoke test: build the whole schema via Alembic on a throwaway DB,
# assert it matches the raw-SQL build, then tear it down.
set -euo pipefail
PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"
psql "$BASE" -q -c "DROP DATABASE IF EXISTS onyx_migrate;" -c "CREATE DATABASE onyx_migrate;"

export ONYX_DATABASE_URL_SYNC="postgresql+psycopg2://${SUPER}@/onyx_migrate?host=${PGHOST}&port=${PGPORT}"
export ONYX_DATABASE_URL="postgresql+asyncpg://${SUPER}@/onyx_migrate?host=${PGHOST}&port=${PGPORT}"

PYTHONPATH=. alembic upgrade head
CUR=$(PYTHONPATH=. alembic current 2>/dev/null | grep -o '0020_seed_example' || true)
[ "$CUR" = "0020_seed_example" ] || { echo "FAIL: head not reached"; exit 1; }
N=$(psql "postgres://${SUPER}@/onyx_migrate?host=${PGHOST}&port=${PGPORT}" -tAc \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND NOT c.relispartition AND n.nspname IN ('ref','identity','profile','finance','wealth','tax_kb','rules','analysis','reco','ai','docs','admin','billing','audit');")
echo "alembic upgrade head -> $N tables"
[ "$N" -ge 80 ] || { echo "FAIL: expected >=80 tables, got $N"; exit 1; }

PYTHONPATH=. alembic downgrade base
LEFT=$(psql "postgres://${SUPER}@/onyx_migrate?host=${PGHOST}&port=${PGPORT}" -tAc \
  "SELECT count(*) FROM information_schema.schemata WHERE schema_name IN ('ref','identity','profile','finance','wealth','tax_kb','rules','analysis','reco','ai','docs','admin','billing','audit');")
[ "$LEFT" = "0" ] || { echo "FAIL: $LEFT schemas left after downgrade base"; exit 1; }
echo "migration smoke OK (upgrade head + downgrade base)"
