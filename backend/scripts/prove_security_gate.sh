#!/usr/bin/env bash
# Prove the security gate rejects a removed database invariant.
#
# The other gates can be proved by editing a source file. These cannot: RLS,
# FORCE RLS, definer-function ownership and PUBLIC EXECUTE revocation live in
# PostgreSQL, so the only honest proof is to remove one from a real database and
# watch the suite fail.
#
# Everything happens on a database this script creates and drops. Each case
# reverts and re-runs, so a case fails if the suite does not go green again —
# a half-removed invariant cannot be left behind.
#
#   PGHOST=... PGPORT=... PGSUPER=... ./scripts/prove_security_gate.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
DB="onyx_sec_proof"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"
CONN="postgres://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"

export ONYX_DATABASE_URL="postgresql+asyncpg://onyx_test:test@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="${ONYX_JWT_SECRET:-security-gate-proof-secret-32-bytes-x}"

cleanup() { psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== provisioning a disposable database =="
psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" -c "CREATE DATABASE ${DB};"
DATABASE_URL="$CONN" ./scripts/apply_schema.sh > /dev/null
psql "$CONN" -q -c "DROP ROLE IF EXISTS onyx_test;" \
  -c "CREATE ROLE onyx_test LOGIN PASSWORD 'test' IN ROLE onyx_app_rw;"

# The suite connects as onyx_test, a member of onyx_app_rw — NOT as a superuser.
# A superuser bypasses row-level security outright, so these assertions would
# pass against a database with every policy removed. Running them under the
# runtime identity is the whole point.
echo "   suite identity: onyx_test (member of onyx_app_rw), provisioned by ${SUPER}"

SUITE=(python -m pytest tests/security -q -p no:cacheprovider)

echo "== baseline =="
if PYTHONPATH=. "${SUITE[@]}" > /dev/null 2>&1; then
  echo "  ok    security suite green before injection"
else
  echo "  FAIL  security suite already failing; cannot prove anything"
  exit 1
fi

PASS=0; FAIL=0
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"; cleanup' EXIT

# $1 label   $2 remove the invariant   $3 restore it
prove() {
  local label="$1" remove="$2" restore="$3"
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$remove"
  if PYTHONPATH=. "${SUITE[@]}" > "$WORK/out" 2>&1; then
    echo "  FAIL  ${label} — security suite PASSED with the invariant removed"
    FAIL=$((FAIL + 1))
  else
    echo "  ok    ${label} — rejected: $(grep -m1 -E '^(FAILED|E )' "$WORK/out" | cut -c1-92)"
    PASS=$((PASS + 1))
  fi
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$restore"
  if ! PYTHONPATH=. "${SUITE[@]}" > /dev/null 2>&1; then
    echo "  FAIL  ${label} — suite still failing after restore"
    FAIL=$((FAIL + 1))
  fi
}

echo "== removed invariants =="

# FORCE ROW LEVEL SECURITY is what makes policies apply to the TABLE OWNER too.
# Without it a table looks protected — policies present, RLS enabled — while the
# owning role reads every tenant's rows.
prove "FORCE ROW LEVEL SECURITY" \
  "ALTER TABLE profile.tax_profile NO FORCE ROW LEVEL SECURITY;" \
  "ALTER TABLE profile.tax_profile FORCE ROW LEVEL SECURITY;"

# Row-level security disabled outright on a tenant table.
prove "ENABLE ROW LEVEL SECURITY" \
  "ALTER TABLE profile.tax_profile DISABLE ROW LEVEL SECURITY;" \
  "ALTER TABLE profile.tax_profile ENABLE ROW LEVEL SECURITY;"

# A SECURITY DEFINER function executable by PUBLIC is a privilege-escalation
# path: any role could invoke the keyhole the workers are supposed to own.
prove "PUBLIC EXECUTE revocation" \
  "GRANT EXECUTE ON FUNCTION ioe.claim_freshness_events(integer, text) TO PUBLIC;" \
  "REVOKE EXECUTE ON FUNCTION ioe.claim_freshness_events(integer, text) FROM PUBLIC;"

echo
echo "security gate proof: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
