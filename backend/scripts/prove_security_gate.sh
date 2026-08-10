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

# A role name of its own. PostgreSQL roles are CLUSTER-wide, so reusing
# `onyx_test` here would drop and recreate the role a concurrently running
# run_backend_tests.sh is authenticated as — grants are held by OID, so that
# other run's pooled connections would start failing with "permission denied"
# for reasons that have nothing to do with the code under test.
SUITE_ROLE=onyx_secproof
# The privileged runtimes need their OWN logins here too, for the same reason
# the suite role does. Entry 11B5J made this mandatory rather than optional: the
# account-lifecycle worker keyholes are no longer executable by `onyx_app_rw`,
# so a gate that provisioned only the application identity could not run the
# suite at all — it would report "already failing" and prove nothing.
PRIVACY_ROLE=onyx_secproof_privacy
FRESHNESS_ROLE=onyx_secproof_freshness

export ONYX_DATABASE_URL="postgresql+asyncpg://${SUITE_ROLE}:test@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_PRIVACY_DATABASE_URL="postgresql+asyncpg://${PRIVACY_ROLE}:test@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_FRESHNESS_DATABASE_URL="postgresql+asyncpg://${FRESHNESS_ROLE}:test@/${DB}?host=${PGHOST}&port=${PGPORT}"
export ONYX_JWT_SECRET="${ONYX_JWT_SECRET:-security-gate-proof-secret-32-bytes-x}"

# ONE DESTRUCTIVE RUN AT A TIME.
#
# The proof database name above is a CONSTANT, and until Entry 11B5J nothing
# stopped a second instance of this script from using the same one. Two runs
# overlapped by accident and the damage was not subtle:
#
#   DROP TABLE finance.income_source_y2043   blocked on a relation lock for
#                                            10m56s behind the other run
#   identity.account_lifecycle               one run's claim consumed the
#                                            other's fixture row -> the suite
#                                            reported `assert 'pending' ==
#                                            'claimed'`
#   PD-1 policy present                      reported FAIL -- a security defect
#                                            that did not exist
#   trap cleanup EXIT                        either run's exit would DROP the
#                                            database the other was using
#
# A gate that can invent a defect is worse than no gate, because the first
# response to a red security gate is to go hunting for a hole in the product.
#
# THE LOCK IS TAKEN BEFORE THE EXIT TRAP IS INSTALLED, deliberately: a refused
# run must not reach a cleanup that drops the database belonging to the run
# that holds the lock. `flock` lives on an open descriptor, so the kernel
# releases it however the process dies -- including SIGKILL, which is how both
# of those runs were ended -- so a crashed run cannot wedge the gate.
LOCK="${ONYX_SEC_PROOF_LOCK:-/tmp/onyx_sec_proof.gate.lock}"
exec 9>"$LOCK" || { echo "cannot open the security-gate lock" >&2; exit 70; }
if ! flock -n 9; then
  # A closed reason code and nothing else. This line is printed by a script
  # whose environment is full of connection strings.
  echo "refusing to run: reason=SECURITY_GATE_ALREADY_RUNNING" >&2
  echo "another destructive security-gate run already owns the proof database" >&2
  exit 69
fi

# The database goes; the role stays. Dropping a cluster-wide role on the way out
# would invalidate a concurrent job authenticated as it.
cleanup() { psql "$BASE" -q -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null 2>&1 || true; }
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
  -- Deliberately NOT members of onyx_app_rw: each privileged runtime gets its
  -- capability and nothing else, which is the topology under test.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${PRIVACY_ROLE}') THEN
    CREATE ROLE ${PRIVACY_ROLE} LOGIN PASSWORD 'test' IN ROLE onyx_privacy_worker;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${FRESHNESS_ROLE}') THEN
    CREATE ROLE ${FRESHNESS_ROLE} LOGIN PASSWORD 'test' IN ROLE onyx_freshness_worker;
  END IF;
END
\$\$;
GRANT onyx_app_rw TO ${SUITE_ROLE};
GRANT onyx_privacy_worker TO ${PRIVACY_ROLE};
GRANT onyx_freshness_worker TO ${FRESHNESS_ROLE};
SQL

# The suite connects as ${SUITE_ROLE}, a member of onyx_app_rw — NOT as a superuser.
# A superuser bypasses row-level security outright, so these assertions would
# pass against a database with every policy removed. Running them under the
# runtime identity is the whole point.
echo "   suite identity: ${SUITE_ROLE} (member of onyx_app_rw), provisioned by ${SUPER}"

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

# Admission state accumulates across suite runs — every run books leases and
# rate counters in the same database. With fifteen cases the suite runs thirty
# times here, and the global concurrency limiter eventually refuses work that a
# fresh database would admit. Cleared alongside the ledger so each case starts
# from a comparable state; both tables are rebuilt by the suite itself.
SUITE_STATE_RESET="
  DELETE FROM admission.lease;
  DELETE FROM admission.rate_counter;
"

# $1 label   $2 remove the invariant   $3 restore it
prove() {
  local label="$1" remove="$2" restore="$3"
  # Comparable starting state for every case. Without it a case fails or passes
  # according to how many cases ran before it, which is the opposite of a proof.
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$SUITE_STATE_RESET"
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$remove"
  if PYTHONPATH=. "${SUITE[@]}" > "$WORK/out" 2>&1; then
    echo "  FAIL  ${label} — security suite PASSED with the invariant removed"
    FAIL=$((FAIL + 1))
  else
    echo "  ok    ${label} — rejected: $(grep -m1 -E '^(FAILED|E )' "$WORK/out" | cut -c1-92)"
    PASS=$((PASS + 1))
  fi
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$restore"
  # The restore check is a FULL SUITE RUN whose result this gate acts on, so it
  # needs the same comparable starting state the injection run gets. Resetting
  # only before the injection left the restore run inheriting whatever admission
  # leases and rate counters the injection run booked — and the gate then
  # reported "suite still failing after restore" for
  # test_admission_preauth.py::test_a_rejection_still_names_no_scope, which has
  # nothing to do with the invariant under test. Same reasoning as the reset
  # above; it was simply applied to one of the two runs.
  psql "$CONN" -q -v ON_ERROR_STOP=1 -c "$SUITE_STATE_RESET"
  if ! PYTHONPATH=. "${SUITE[@]}" > "$WORK/restore" 2>&1; then
    echo "  FAIL  ${label} — suite still failing after restore:"
    grep -m3 -E '^(FAILED|E  )' "$WORK/restore" | sed 's/^/          /'
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

# ---- PD-1, Entry 11B1 -------------------------------------------------------
# The sixteen tenant-owned child tables. Each case removes one property of the
# remediation and expects the suite to notice; a case that passes means the
# property was never load-bearing.

# The policy itself, on the table holding the frozen calculation inputs.
prove "PD-1 policy present (analysis_input_snapshot)" \
  "DROP POLICY p_self_analysis_input_snapshot ON analysis.analysis_input_snapshot;" \
  "CREATE POLICY p_self_analysis_input_snapshot ON analysis.analysis_input_snapshot
     FOR ALL
     USING (EXISTS (SELECT 1 FROM analysis.analysis_run p
                     WHERE p.id = analysis_input_snapshot.analysis_id
                       AND p.user_id = ref.current_app_user()))
     WITH CHECK (EXISTS (SELECT 1 FROM analysis.analysis_run p
                          WHERE p.id = analysis_input_snapshot.analysis_id
                            AND p.user_id = ref.current_app_user()));"

# RLS switched off on a remediated table: policies still present, no effect.
prove "PD-1 RLS enabled (ai_message)" \
  "ALTER TABLE ai.ai_message DISABLE ROW LEVEL SECURITY;" \
  "ALTER TABLE ai.ai_message ENABLE ROW LEVEL SECURITY;"

# FORCE removed: the owner reads every tenant again.
prove "PD-1 FORCE RLS (docs.extraction_field)" \
  "ALTER TABLE docs.extraction_field NO FORCE ROW LEVEL SECURITY;" \
  "ALTER TABLE docs.extraction_field FORCE ROW LEVEL SECURITY;"

# WITH CHECK weakened to USING-only. Reads stay correct and ownership forgery
# reopens — the case a read-only test suite would never catch.
prove "PD-1 WITH CHECK (billing.invoice)" \
  "DROP POLICY p_self_invoice ON billing.invoice;
   CREATE POLICY p_self_invoice ON billing.invoice
     FOR ALL
     USING (EXISTS (SELECT 1 FROM billing.subscription p
                     WHERE p.id = invoice.subscription_id
                       AND p.user_id = ref.current_app_user()));" \
  "DROP POLICY p_self_invoice ON billing.invoice;
   CREATE POLICY p_self_invoice ON billing.invoice
     FOR ALL
     USING (EXISTS (SELECT 1 FROM billing.subscription p
                     WHERE p.id = invoice.subscription_id
                       AND p.user_id = ref.current_app_user()))
     WITH CHECK (EXISTS (SELECT 1 FROM billing.subscription p
                          WHERE p.id = invoice.subscription_id
                            AND p.user_id = ref.current_app_user()));"

# A runtime privilege the read-only role must never hold.
prove "PD-1 read-only role stays read-only" \
  "GRANT UPDATE ON wealth.asset_valuation TO onyx_app_ro;" \
  "REVOKE UPDATE ON wealth.asset_valuation FROM onyx_app_ro;"

# A tenant-owned table that ships with CRUD and no boundary — PD-1's own shape,
# which is what the default privileges hand a new table.
prove "PD-1 regression guard (new unguarded tenant table)" \
  "CREATE TABLE wealth.pd1_regression_probe (
     id uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
     user_id uuid NOT NULL REFERENCES identity.user_account(id));" \
  "DROP TABLE wealth.pd1_regression_probe;"


# PD-9 restores need a cleanup prelude. These cases relax a constraint and then
# let the whole security suite run against the relaxed schema, so the suite can
# write rows the original constraint would have refused — duplicate subjects
# once the primary key is gone, ledger rows whose account was removed while the
# cascade was absent. Restoring the constraint over them fails, which would
# leave the disposable database in a state no later case could use.
#
# Lifecycle rows are not deletable by design, so the cleanup disables that
# trigger for the duration. It runs only inside this proof, on a database the
# script created and drops.
# BOTH no-delete triggers, not one. This cleanup was written in Entry 11B3,
# when identity.account_lifecycle was the only table that refused DELETE. Entry
# 11B5C then added identity.account_lifecycle_phase with
#
#     fk_lifecycle_phase_subject ... REFERENCES identity.account_lifecycle(user_id)
#         ON DELETE CASCADE
#
# and a no-delete trigger of its own — so the DELETE below now cascades into a
# table that refuses to be deleted from, and the whole gate dies mid-run with
#
#     ERROR:  account_lifecycle_phase rows are not deletable
#
# It stayed invisible until Entry 11B5J, because nothing in the suite created a
# phase row for an account this cleanup would remove until the worker
# re-entrancy tests did. Latent since 11B5C; found when the gate first ran far
# enough to reach it.
LEDGER_CLEANUP="${SUITE_STATE_RESET}
  ALTER TABLE identity.account_lifecycle DISABLE TRIGGER trg_account_lifecycle_no_delete;
  ALTER TABLE identity.account_lifecycle_phase DISABLE TRIGGER trg_lifecycle_phase_no_delete;
  DELETE FROM identity.account_lifecycle a
   WHERE a.ctid <> (SELECT min(b.ctid) FROM identity.account_lifecycle b
                     WHERE b.user_id = a.user_id);
  DELETE FROM identity.account_lifecycle a
   WHERE NOT EXISTS (SELECT 1 FROM identity.user_account u WHERE u.id = a.user_id);
  ALTER TABLE identity.account_lifecycle_phase ENABLE TRIGGER trg_lifecycle_phase_no_delete;
  ALTER TABLE identity.account_lifecycle ENABLE TRIGGER trg_account_lifecycle_no_delete;
  -- GUARD ON THE GUARD. Everything after this point runs the security suite and
  -- believes its result. A cleanup that left either protection disabled would
  -- make every later case run against a weaker database than the one being
  -- certified, and nothing downstream would notice.
  DO \$do\$
  BEGIN
    IF EXISTS (SELECT 1 FROM pg_trigger
                WHERE tgname IN ('trg_account_lifecycle_no_delete',
                                 'trg_lifecycle_phase_no_delete')
                  AND NOT tgisinternal
                  AND tgenabled = 'D') THEN
      RAISE EXCEPTION 'ledger cleanup left a no-delete trigger disabled';
    END IF;
  END
  \$do\$;
"

# ---- PD-9, Entry 11B3 -------------------------------------------------------
# The deletion ledger must outlive the account it records. Each case restores
# one way of losing that and expects the suite to notice.

# The cascade itself. This is the defect: with it back, removing an account
# either destroys the ledger or — because of the no-delete trigger — makes the
# account impossible to remove at all.
#
# NOT VALID because by this point the database already holds ledger rows whose
# accounts were removed by the suite, and a validating constraint cannot be
# added over them. That is not a workaround, it is the migration docstring's
# warning demonstrating itself: once a record has outlived its account, the
# cascade genuinely cannot come back. NOT VALID still arms it for every
# subsequent delete, which is the behaviour under test.
prove "PD-9 no cascade on the deletion ledger" \
  "ALTER TABLE identity.account_lifecycle
     ADD CONSTRAINT account_lifecycle_user_id_fkey
     FOREIGN KEY (user_id) REFERENCES identity.user_account(id)
     ON DELETE CASCADE NOT VALID;" \
  "${LEDGER_CLEANUP}
   ALTER TABLE identity.account_lifecycle
     DROP CONSTRAINT IF EXISTS account_lifecycle_user_id_fkey;"

# The same cascade on the table PD-9's wording names.
prove "PD-9 no cascade on audit.data_deletion_request" \
  "ALTER TABLE audit.data_deletion_request
     ADD CONSTRAINT data_deletion_request_user_id_fkey
     FOREIGN KEY (user_id) REFERENCES identity.user_account(id)
     ON DELETE CASCADE NOT VALID;" \
  "ALTER TABLE audit.data_deletion_request
     DROP CONSTRAINT IF EXISTS data_deletion_request_user_id_fkey;"

# A DELETE grant on the ledger. The record that survives the account must also
# survive the application.
prove "PD-9 ledger DELETE grant" \
  "GRANT DELETE ON identity.account_lifecycle TO onyx_app_rw;" \
  "${LEDGER_CLEANUP}
   REVOKE DELETE ON identity.account_lifecycle FROM onyx_app_rw;"

# The subject's uniqueness. Without the primary key, one account could
# accumulate several logical lifecycles and idempotency would be a claim rather
# than a fact.
# The dependent foreign key has to go with it and come back after. Entry 11B5C
# added `identity.account_lifecycle_phase.fk_lifecycle_phase_subject`, which
# references this primary key's index, and from then on the bare DROP failed
# with "cannot drop constraint ... because other objects depend on it". The
# injection therefore never applied — so the case was measuring nothing, and the
# restore left the schema short of a constraint for every case that followed.
# CASCADE is used deliberately here and the FK is rebuilt explicitly, rather
# than trusting CASCADE's silent removal to be the whole story.
prove "PD-9 durable subject uniqueness" \
  "ALTER TABLE identity.account_lifecycle
     DROP CONSTRAINT account_lifecycle_pkey CASCADE;" \
  "${LEDGER_CLEANUP}
   ALTER TABLE identity.account_lifecycle
     ADD CONSTRAINT account_lifecycle_pkey PRIMARY KEY (user_id);
   ALTER TABLE identity.account_lifecycle_phase
     DROP CONSTRAINT IF EXISTS fk_lifecycle_phase_subject;
   ALTER TABLE identity.account_lifecycle_phase
     ADD CONSTRAINT fk_lifecycle_phase_subject
     FOREIGN KEY (user_id) REFERENCES identity.account_lifecycle(user_id)
     ON DELETE CASCADE;"

# The worker requiring a live account again. Restoring the join is the shape of
# a future change that quietly breaks every phase after account removal.
prove "PD-9 worker does not need a live account" \
  "CREATE OR REPLACE FUNCTION identity.account_deletion_state(p_user_id uuid)
     RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
     SET search_path = identity, pg_catalog
     AS \$fn\$ SELECT l.state FROM identity.account_lifecycle l
                JOIN identity.user_account u ON u.id = l.user_id
               WHERE l.user_id = p_user_id \$fn\$;" \
  "CREATE OR REPLACE FUNCTION identity.account_deletion_state(p_user_id uuid)
     RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
     SET search_path = identity, pg_catalog
     AS \$fn\$ SELECT state FROM identity.account_lifecycle
               WHERE user_id = p_user_id \$fn\$;"

# The creation-time integrity that replaced the foreign key.
prove "PD-9 subject must exist at creation" \
  "DROP TRIGGER trg_account_lifecycle_subject_exists
     ON identity.account_lifecycle;" \
  "CREATE TRIGGER trg_account_lifecycle_subject_exists
     BEFORE INSERT ON identity.account_lifecycle
     FOR EACH ROW EXECUTE FUNCTION identity.require_lifecycle_subject_exists();"

# Entry 11B5J. The application role holding EXECUTE on the account-lifecycle
# worker keyholes — the defect this gate did not previously look for, because
# PD-16 was a role MEMBERSHIP and these were direct grants.
#
# The injection ASSERTS ITS OWN EFFECT before the suite runs: if the GRANT did
# not actually produce the unsafe condition, the case raises here instead of
# quietly reporting a pass. That is what stops it going the way the PD-9
# primary-key case went, where a later foreign key silently made the injection
# a no-op and the case measured nothing for months.
prove "11B5J lifecycle worker keyholes reachable from the app role" \
  "GRANT EXECUTE ON FUNCTION identity.claim_account_lifecycle(integer, text)
     TO onyx_app_rw;
   GRANT EXECUTE ON FUNCTION identity.advance_account_lifecycle(uuid, uuid, text, text)
     TO onyx_app_rw;
   GRANT EXECUTE ON FUNCTION identity.fail_account_lifecycle(uuid, uuid, text, text)
     TO onyx_app_rw;
   DO \$do\$
   BEGIN
     IF NOT has_function_privilege(
              'onyx_app_rw',
              'identity.claim_account_lifecycle(integer,text)', 'EXECUTE') THEN
       RAISE EXCEPTION 'injection did not take effect: the unsafe condition '
                       'this case exists to detect was never created';
     END IF;
   END
   \$do\$;" \
  "REVOKE EXECUTE ON FUNCTION identity.claim_account_lifecycle(integer, text)
     FROM onyx_app_rw;
   REVOKE EXECUTE ON FUNCTION identity.advance_account_lifecycle(uuid, uuid, text, text)
     FROM onyx_app_rw;
   REVOKE EXECUTE ON FUNCTION identity.fail_account_lifecycle(uuid, uuid, text, text)
     FROM onyx_app_rw;
   DO \$do\$
   BEGIN
     IF has_function_privilege(
          'onyx_app_rw',
          'identity.claim_account_lifecycle(integer,text)', 'EXECUTE') THEN
       RAISE EXCEPTION 'cleanup failed: the app role still holds the lifecycle '
                       'worker capability';
     END IF;
   END
   \$do\$;"

echo
echo "security gate proof: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
