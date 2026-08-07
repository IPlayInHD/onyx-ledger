#!/usr/bin/env bash
# Pollution regression: do the sensitive suites still hold on a DIRTY database?
#
# Every other run in this repository starts from an empty database, which hides
# a whole class of defect: a test that silently assumes its own record is the
# only one, or the first one, or the newest one. Production is never empty, and
# a scheduler that only works when there is exactly one eligible target is not a
# scheduler.
#
# So: build a database, fill it with history by running the whole suite once,
# add more tenants and stale reasons on top, then re-run the suites whose
# assertions are most exposed to accumulated state — twice, in two different
# orders, to separate "passes" from "passes when it runs first".
#
# This is a RELEASE-level check, not a per-commit one: it costs roughly three
# full-suite runs. CI runs the fresh-database suite on every push; this runs
# before a release and after any change to freshness, replay, or scheduling.
#
#   PGHOST=... PGPORT=... PGSUPER=... ./scripts/pollution_regression.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
DB="onyx_pollution"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"
CONN="postgres://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"

export ONYX_DATABASE_URL="postgresql+asyncpg://onyx_test:test@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="${ONYX_JWT_SECRET:-pollution-regression-secret-32-bytes-}"

cleanup() { psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# The suites whose correctness depends on selecting the RIGHT row out of many:
# freshness fan-out and relay, the integrity scheduler's bounded target
# selection, replay verification, the Item 3A/3B frozen-input paths, and the
# Entry 9 producer wiring.
SENSITIVE=(
  tests/integration/test_ioe_freshness_events.py
  tests/integration/test_freshness_producer_wiring.py
  tests/integration/test_integrity_scheduler_task.py
  tests/integration/test_integrity_verification.py
  tests/integration/test_golden_replay.py
  tests/integration/test_frozen_snapshot_execution.py
  tests/integration/test_frozen_scenario_baseline.py
  tests/integration/test_scenario_integrity_closeout.py
)

echo "== provisioning =="
psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" -c "CREATE DATABASE ${DB};"
DATABASE_URL="$CONN" ./scripts/apply_schema.sh > /dev/null
psql "$CONN" -q -c "DROP ROLE IF EXISTS onyx_test;" \
  -c "CREATE ROLE onyx_test LOGIN PASSWORD 'test' IN ROLE onyx_app_rw;"

echo "== generating pollution: one full suite run against this database =="
PYTHONPATH=. python -m pytest tests/ -q -p no:cacheprovider > /dev/null 2>&1 \
  || echo "   (the seeding run's own result is not the assertion here)"

echo "== extra pollution: tenants, stale reasons, scheduler cycles =="
PYTHONPATH=. python scripts/seed_pollution.py

echo "== fixture scale now on the database =="
psql "$CONN" -tAc "
  SELECT format('%-34s %s', label, n) FROM (
    SELECT 'identity.user_account'          AS label, count(*) AS n FROM identity.user_account
    UNION ALL SELECT 'analysis.analysis_run',       count(*) FROM analysis.analysis_run
    UNION ALL SELECT 'ioe.optimization_run',        count(*) FROM ioe.optimization_run
    UNION ALL SELECT 'ioe.scenario',                count(*) FROM ioe.scenario
    UNION ALL SELECT 'ioe.strategy_portfolio',      count(*) FROM ioe.strategy_portfolio
    UNION ALL SELECT 'ioe.freshness_outbox',        count(*) FROM ioe.freshness_outbox
    UNION ALL SELECT 'ioe.integrity_check',         count(*) FROM ioe.integrity_check
    UNION ALL SELECT 'distinct stale_reason_code',  count(DISTINCT stale_reason_code) FROM ioe.freshness_outbox
    UNION ALL SELECT 'tax_kb.tax_rule_version',     count(*) FROM tax_kb.tax_rule_version
  ) s;"

STATUS=0

echo
echo "== sensitive suites on the polluted database (declared order) =="
if PYTHONPATH=. python -m pytest "${SENSITIVE[@]}" -q -p no:cacheprovider; then
  echo "   PASS"
else
  echo "   FAIL — a sensitive suite does not hold on a database with history"
  STATUS=1
fi

echo
echo "== same suites, reversed (proves no order dependency between files) =="
REVERSED=()
for (( i=${#SENSITIVE[@]}-1 ; i>=0 ; i-- )); do REVERSED+=("${SENSITIVE[$i]}"); done
if PYTHONPATH=. python -m pytest "${REVERSED[@]}" -q -p no:cacheprovider; then
  echo "   PASS"
else
  echo "   FAIL — result depends on the order the files run in"
  STATUS=1
fi

echo
[ "$STATUS" -eq 0 ] && echo "pollution regression: PASS" || echo "pollution regression: FAIL"
exit "$STATUS"
