#!/usr/bin/env bash
# Prove the schema-drift gate rejects real defects.
#
# A gate nobody has watched fail is a gate nobody knows works. Each case below
# injects ONE defect into a DISPOSABLE database at head, asserts the checker
# exits non-zero and names the object, then reverts and asserts the checker is
# clean again. Nothing here touches a database anyone else uses, and the
# database is dropped at the end regardless of outcome.
#
#   PGHOST=... PGPORT=... PGSUPER=... ./scripts/prove_schema_drift_gate.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PGHOST="${PGHOST:-/tmp}"; PGPORT="${PGPORT:-54329}"; SUPER="${PGSUPER:-onyx_super}"
DB="onyx_drift_proof"
BASE="postgres://${SUPER}@/postgres?host=${PGHOST}&port=${PGPORT}"
CONN="postgres://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"

export ONYX_DATABASE_URL_SYNC="postgresql+psycopg2://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_DATABASE_URL="postgresql+asyncpg://${SUPER}@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="${ONYX_JWT_SECRET:-schema-drift-proof-secret-32-bytes-x}"

cleanup() { psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null 2>&1 || true; }
trap cleanup EXIT

psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" -c "CREATE DATABASE ${DB};"
PYTHONPATH=. alembic upgrade head >/dev/null
echo "== baseline =="
PYTHONPATH=. python scripts/check_schema_drift.py | tail -1

PASS=0; FAIL=0

# $1 label   $2 inject SQL   $3 revert SQL   $4 substring the failure must name
prove() {
  local label="$1" inject="$2" revert="$3" expect="$4" out=""
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$inject"
  if out=$(PYTHONPATH=. python scripts/check_schema_drift.py 2>&1); then
    echo "  FAIL  ${label} — gate PASSED on an injected defect"
    FAIL=$((FAIL + 1))
  elif ! grep -q "$expect" <<<"$out"; then
    echo "  FAIL  ${label} — gate failed but never named '${expect}'"
    FAIL=$((FAIL + 1))
  else
    echo "  ok    ${label} — rejected: $(grep -m1 'FAIL ' <<<"$out" | sed 's/^ *//')"
    PASS=$((PASS + 1))
  fi
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$revert"
  if ! PYTHONPATH=. python scripts/check_schema_drift.py >/dev/null 2>&1; then
    echo "  FAIL  ${label} — gate still dirty after revert"
    FAIL=$((FAIL + 1))
  fi
}

echo "== injected defects =="

# A column the models do not know about. The most dangerous kind of drift: code
# reads and writes a table it does not fully describe.
prove "unexpected column" \
  "ALTER TABLE ioe.scenario ADD COLUMN drift_probe_col text;" \
  "ALTER TABLE ioe.scenario DROP COLUMN drift_probe_col;" \
  "remove_column"

# A column the models DO know about, missing from the database.
# Reverted with its comment: re-adding a bare column would leave a DIFFERENT
# divergence behind and the next case would inherit it.
prove "missing column" \
  "ALTER TABLE ioe.scenario DROP COLUMN note;" \
  "ALTER TABLE ioe.scenario ADD COLUMN note text; COMMENT ON COLUMN ioe.scenario.note IS 'User-supplied note. Excluded from both hashes, exactly as label is.';" \
  "add_column"

# A type change on a governed column. The TYPE_AFFINITY policy entry records the
# type PAIR, so a different pair is not covered by it.
prove "changed type" \
  "ALTER TABLE ioe.scenario ALTER COLUMN label TYPE varchar(64);" \
  "ALTER TABLE ioe.scenario ALTER COLUMN label TYPE text;" \
  "modify_type"

# A type change on a column that IS governed, to a DIFFERENT pair.
#
# The case above covers an ungoverned column, which is the easy half. This is
# the half TYPE_AFFINITY actually claims: "the recorded type pair is part of
# this entry's identity, so a change to a DIFFERENT type is not covered by it".
# It was not true. `key` is kind|schema|table|object and the pair lived only in
# `detail`, which was never compared — so a policy entry blessed every future
# type change on its column. Entry 11B5 proved it by re-typing a governed
# `deleted_at` to `String` and watching the gate report zero drift.
#
# `ai.ai_conversation.deleted_at` is governed as TIMESTAMP->DateTime. Making it
# a DATE keeps the key and changes the pair, which is exactly the case that used
# to slip through.
prove "changed type on a GOVERNED column" \
  "ALTER TABLE ai.ai_conversation ALTER COLUMN deleted_at TYPE date;" \
  "ALTER TABLE ai.ai_conversation ALTER COLUMN deleted_at TYPE timestamptz;" \
  "modify_type"

# Nullability is not globally suppressed: relaxing NOT NULL is caught.
prove "unexpected nullable change" \
  "ALTER TABLE ioe.scenario ALTER COLUMN workflow_status DROP NOT NULL;" \
  "ALTER TABLE ioe.scenario ALTER COLUMN workflow_status SET NOT NULL;" \
  "modify_nullable"

# A raw-SQL index is invisible to the ORM metadata, so its disappearance
# produces no new difference at all — only the silence of its policy entry. That
# silence is what the gate treats as a failure.
prove "missing index" \
  "DROP INDEX ioe.ix_ioe_scenario_user;" \
  "CREATE INDEX ix_ioe_scenario_user ON ioe.scenario USING btree (user_id, created_at DESC);" \
  "vanished"

# CHECK comparison is not globally suppressed either.
prove "unexpected CHECK" \
  "ALTER TABLE ioe.scenario ADD CONSTRAINT ck_drift_probe CHECK (tax_year > 0);" \
  "ALTER TABLE ioe.scenario DROP CONSTRAINT ck_drift_probe;" \
  "remove_constraint"

echo "== governed divergences still pass =="
if PYTHONPATH=. python scripts/check_schema_drift.py | grep -q "unexpected schema drift: 0"; then
  echo "  ok    raw-SQL-owned CHECKs/indexes/FKs/types remain governed, not failures"
  PASS=$((PASS + 1))
else
  echo "  FAIL  clean schema reported drift"
  FAIL=$((FAIL + 1))
fi

echo
echo "schema drift gate proof: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
