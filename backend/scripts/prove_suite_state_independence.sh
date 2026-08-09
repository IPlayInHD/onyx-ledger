#!/usr/bin/env bash
# Prove the security suite does not depend on a freshly created database.
#
# WHY THIS EXISTS
# `scripts/prove_security_gate.sh` reruns tests/security around twenty-three
# times against ONE database, over twenty-odd minutes. That is not a stress
# test — it is the only honest way to prove a database invariant was removed
# and noticed — but it puts the suite in a state no single run ever sees, and
# Entry 11B4 discovered the suite quietly depended on the state it never saw.
#
# Four tests asserted things that were true only of an empty queue. The queue
# interfaces (`ioe.claim_freshness_events`, `identity.claim_account_lifecycle`)
# take a BOUNDED batch ORDERED oldest-first, so "claim, then expect my own row
# back" is really "assert fewer than fifty claimable rows exist". True on a
# fresh database. False after a few reruns. The failures looked like security
# regressions and were not:
#
#     AssertionError: worker A claimed nothing
#     AssertionError: the worker cannot claim a lifecycle whose account has
#                     been removed, so no phase after account removal could
#                     ever run
#
# Finding that through the gate proof cost half an hour per attempt, because
# the state only builds up near the end of a twenty-three case run. This
# reaches the same condition in four runs.
#
# WHAT IT DOES DIFFERENTLY
# The residue that matters is CLAIMED rows crossing `freshness_claim_timeout()`
# and returning to the queue ahead of anything new. In the gate that happens on
# a ten-minute boundary, continuously, INCLUDING in the seconds between one
# test draining the queue and a later test in the same file reading it. So the
# ageing here runs in the background WHILE pytest runs, not between runs. An
# earlier version of this harness aged between runs, and could not reproduce
# anything: the first test drained the queue every time.
#
#   PGHOST=... PGPORT=... PGSUPER=... ./scripts/prove_suite_state_independence.sh [RUNS]
#
# Not part of release_gate.sh --full. The gate proof already exercises this
# path as a side effect of doing its own job, and paying six more suite runs on
# every release to reach the same conclusion twice is not worth it. Reach for
# this when a gate-proof restore fails in a test the injection never touched —
# that signature means accumulated state, not a security regression.
set -euo pipefail
cd "$(dirname "$0")/.."

RUNS="${1:-6}"
PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
DB="onyx_state_independence"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"
CONN="postgres://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"

# Its own role, for the reason prove_security_gate.sh documents: PostgreSQL
# roles are cluster-wide, so reusing `onyx_test` would break a concurrently
# running run_backend_tests.sh.
SUITE_ROLE=onyx_stateproof
export ONYX_DATABASE_URL="postgresql+asyncpg://${SUITE_ROLE}:test@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="${ONYX_JWT_SECRET:-state-independence-proof-secret-32b}"

AGER_PID=""
stop_ager() { [ -n "$AGER_PID" ] && kill "$AGER_PID" 2>/dev/null || true; AGER_PID=""; }
cleanup() {
  stop_ager
  psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== provisioning a disposable database =="
psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" -c "CREATE DATABASE ${DB};"
DATABASE_URL="$CONN" ./scripts/apply_schema.sh > /dev/null
psql "$CONN" -q -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${SUITE_ROLE}') THEN
    CREATE ROLE ${SUITE_ROLE} LOGIN PASSWORD 'test' IN ROLE onyx_app_rw;
  END IF;
END
\$\$;
GRANT onyx_app_rw TO ${SUITE_ROLE};
SQL

# Age claimed rows past the timeout every few seconds, so recovery lands at
# arbitrary points inside a run rather than only between runs.
start_ager() {
  ( while true; do
      psql "$CONN" -q -c "UPDATE ioe.freshness_outbox
                             SET claimed_at = claimed_at - interval '11 minutes'
                           WHERE claimed_at IS NOT NULL
                             AND claim_state = 'claimed';" >/dev/null 2>&1 || true
      sleep 4
    done ) &
  AGER_PID=$!
}

SUITE=(python -m pytest tests/security -q -p no:cacheprovider)
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; cleanup' EXIT

echo "== ${RUNS} consecutive runs against one database, claims ageing throughout =="
FAILED=0
for run in $(seq 1 "$RUNS"); do
  start_ager
  set +e
  PYTHONPATH=. "${SUITE[@]}" > "$WORK/run" 2>&1
  status=$?
  set -e
  stop_ager

  rows=$(psql "$CONN" -qAt -c "SELECT count(*) FROM ioe.freshness_outbox;")
  summary=$(tail -1 "$WORK/run")
  if [ "$status" -eq 0 ]; then
    echo "  ok    run ${run}: ${summary}  (outbox ${rows})"
  else
    echo "  FAIL  run ${run}: ${summary}  (outbox ${rows})"
    grep -E '^FAILED' "$WORK/run" | sed 's/^/          /'
    FAILED=$((FAILED + 1))
    # Keep going: which run a test starts failing on is the diagnostic, and
    # stopping at the first one hides whether others follow.
  fi
done

echo
if [ "$FAILED" -ne 0 ]; then
  echo "suite state independence: ${FAILED}/${RUNS} runs FAILED"
  echo "A test that passes alone and fails here is asserting something about"
  echo "the state of the database rather than about the code."
  exit 1
fi
echo "suite state independence: ${RUNS}/${RUNS} runs passed"
