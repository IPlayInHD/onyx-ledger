#!/usr/bin/env bash
# The release gate: every blocking check, in one command, in the order that
# fails cheapest first.
#
#   ./scripts/release_gate.sh --fast    lock, lint, types, schema drift  (~1 min, no DB build)
#   ./scripts/release_gate.sh --full    everything, including a freshly provisioned database
#
# --full is the same set of gates CI blocks on, so a developer can find out here
# rather than in review. Nothing is tolerated with `|| true`: `set -e` plus the
# explicit `run` wrapper means the first failing command decides the exit code,
# and the summary at the end reports which one it was.
#
# Environment (defaults suit a local PostgreSQL 16 with pgvector):
#   PGHOST PGPORT PGSUPER   how to reach the server and as whom to provision
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:---fast}"
export PGHOST="${PGHOST:-/tmp}" PGPORT="${PGPORT:-54329}" PGSUPER="${PGSUPER:-onyx_super}"

FAILED=""
run() {
  local label="$1"; shift
  echo
  echo "─── ${label} ───"
  local start; start=$(date +%s)
  if "$@"; then
    echo "    ✓ ${label}  ($(($(date +%s) - start))s)"
  else
    echo "    ✗ ${label}  ($(($(date +%s) - start))s)"
    FAILED="${FAILED}${label}\n"
    return 1
  fi
}

# ---------------------------------------------------------------- fast gates --
# Cheap, no database. A developer runs these constantly.
run "python runtime"        ./scripts/check_python.sh
run "dependency lock"       ./scripts/check_lock.sh
run "ruff"                  ruff check app workers scripts tests
run "protected mypy"        ./scripts/check_types.sh

if [ "$MODE" = "--fast" ]; then
  echo
  echo "release gate (--fast): PASS"
  echo "Run --full before releasing: it adds the fresh-database suite, the"
  echo "security invariants, migration smoke, and the schema-drift gate."
  exit 0
fi

if [ "$MODE" != "--full" ]; then
  echo "usage: $0 [--fast|--full]" >&2
  exit 64
fi

# ---------------------------------------------------------------- full gates --
# Everything below builds or requires a database. Order matters: migrations are
# proved before anything is asked to run against a migrated database.
run "clean install proof"   ./scripts/prove_clean_install.sh
run "gate failure proof"    ./scripts/prove_gates_fail.sh
run "migration smoke"       ./scripts/check_migrations.sh
run "revision hygiene"      python -m pytest tests/unit/test_migration_hygiene.py -q
run "schema drift"          ./scripts/check_schema_drift_on_fresh_db.sh
run "schema drift proof"    ./scripts/prove_schema_drift_gate.sh
run "admission control"     ./scripts/run_backend_tests.sh \
                              tests/integration/test_admission_control.py \
                              tests/integration/test_admission_api.py \
                              tests/integration/test_admission_auth.py \
                              tests/integration/test_admission_document_bounds.py \
                              tests/integration/test_admission_failure_modes.py \
                              tests/security/test_admission_isolation.py \
                              tests/security/test_admission_preauth.py \
                              tests/unit/test_admission_wiring.py -q
run "security invariants"   ./scripts/run_backend_tests.sh tests/security -q
run "security gate proof"   ./scripts/prove_security_gate.sh
run "determinism"           ./scripts/run_backend_tests.sh \
                              tests/integration/test_integrity_api_and_determinism.py \
                              tests/integration/test_golden_replay.py -q
run "full suite (fresh DB)" ./scripts/run_backend_tests.sh

echo
if [ -n "$FAILED" ]; then
  echo "release gate (--full): FAIL"
  printf "  failed: %b" "$FAILED"
  exit 1
fi
echo "release gate (--full): PASS"
