#!/usr/bin/env bash
# Prove each blocking gate rejects a defect it is supposed to catch.
#
# A gate that has only ever been seen passing is an assumption. Each case here
# injects one real defect, asserts the gate exits non-zero, and reverts. Every
# revert is verified: the case fails if the gate does not go green again, so a
# half-reverted injection cannot be left behind.
#
# The schema-drift gate has its own proof (scripts/prove_schema_drift_gate.sh),
# which needs a disposable database; it is invoked from --full there rather than
# duplicated here.
#
#   ./scripts/prove_gates_fail.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Snapshot every file this script will touch. The final check compares against
# THIS, not against git HEAD: the point is that no injection survived the run,
# and the tree may legitimately carry unrelated uncommitted work.
TOUCHED=(
  app/services/ioe/domain/canonical.py
  app/services/tax_engine/core/engine.py
  app/services/tax_engine/core/data.py
  pyproject.toml
  migrations/versions/0043_schema_comment_convergence.py
)
mkdir -p "$WORK/snapshot"
for f in "${TOUCHED[@]}"; do
  mkdir -p "$WORK/snapshot/$(dirname "$f")"
  cp "$f" "$WORK/snapshot/$f"
done

# CPython decides a cached .pyc is still valid by comparing the source's mtime
# (WHOLE SECONDS) and size. Every injection here is a small edit, several are
# byte-for-byte the same length as the original, and inject-run-restore takes
# well under a second — so the restored source can land on the same mtime and
# the same size as the injected one, and the interpreter happily reuses
# bytecode compiled from the DEFECT.
#
# That is how this script reported "gate still failing after revert" while the
# working tree was verifiably byte-identical to its snapshot. The more dangerous
# direction is the same mechanism reversed: a stale GOOD .pyc masking an
# injected defect and the proof reporting that a gate caught something it never
# saw.
#
# So: never write bytecode during the proof, and drop any that exists after each
# restore.
export PYTHONDONTWRITEBYTECODE=1

invalidate_bytecode() {
  find . -name "__pycache__" -type d -prune -not -path "*/.venv/*" \
    -exec rm -rf {} + 2>/dev/null || true
}

# $1 label   $2 file to modify   $3 python snippet injecting the defect   $4 gate command
prove() {
  local label="$1" target="$2" inject="$3"; shift 3
  cp "$target" "$WORK/backup"

  if ! python -c "$inject"; then
    echo "  FAIL  ${label} — could not inject the defect"
    FAIL=$((FAIL + 1)); cp "$WORK/backup" "$target"; return
  fi

  if "$@" > "$WORK/out" 2>&1; then
    echo "  FAIL  ${label} — gate PASSED on an injected defect"
    FAIL=$((FAIL + 1))
  else
    echo "  ok    ${label} — gate rejected it: $(grep -m1 -iE 'error|failed|assert' "$WORK/out" | cut -c1-96)"
    PASS=$((PASS + 1))
  fi

  cp "$WORK/backup" "$target"
  invalidate_bytecode
  if ! "$@" > /dev/null 2>&1; then
    echo "  FAIL  ${label} — gate still failing after revert"
    FAIL=$((FAIL + 1))
  fi
}

echo "== baseline: every gate green before injection =="
ruff check app workers scripts tests > /dev/null && echo "  ok    ruff"
./scripts/check_types.sh > /dev/null && echo "  ok    protected mypy"
./scripts/check_lock.sh > /dev/null && echo "  ok    dependency lock"

echo
echo "== injected defects =="

# ---- lint ------------------------------------------------------------------
# An unused import: trivially real, and exactly what Ruff's F rules exist for.
prove "ruff / lint" app/services/ioe/domain/canonical.py \
  "p='app/services/ioe/domain/canonical.py';t=open(p).read();open(p,'w').write('import os\n'+t)" \
  ruff check app workers scripts tests

# ---- protected typing ------------------------------------------------------
# A genuine type mismatch in a calculation-critical module: a Decimal function
# handed a str. Not a syntax error, not a missing annotation — the kind of
# defect that runs fine until the one input that breaks it.
prove "protected mypy" app/services/tax_engine/core/engine.py \
  "p='app/services/tax_engine/core/engine.py';t=open(p).read();open(p,'w').write(t.replace('def _pos(', 'def _injected_defect(x: int) -> str:\n    return x\n\n\ndef _pos(', 1))" \
  ./scripts/check_types.sh

# ---- unit tests ------------------------------------------------------------
# A wrong number in the reference tax data. The golden-case tests assert exact
# federal amounts, so a changed bracket rate is a deterministic failure and not
# a flake — which is what makes it a fair test of the gate.
prove "test suite" app/services/tax_engine/core/data.py \
  "p='app/services/tax_engine/core/data.py';t=open(p).read();open(p,'w').write(t.replace('Bracket(D(57375), D(\"0.145\"))','Bracket(D(57375), D(\"0.155\"))',1))" \
  python -m pytest tests/unit/test_tax_engine.py -q

# ---- dependency lock -------------------------------------------------------
# A dependency added to the declaration without regenerating the lock — the
# single most likely way the two drift apart in practice.
prove "dependency lock" pyproject.toml \
  "p='pyproject.toml';t=open(p).read();open(p,'w').write(t.replace('    \"defusedxml>=0.7\",','    \"defusedxml>=0.7\",\n    \"tenacity>=8.0\",',1))" \
  ./scripts/check_lock.sh

# ---- quality-gate policy ---------------------------------------------------
# Someone quietly narrows the protected scope to make mypy pass.
prove "protected-scope policy" pyproject.toml \
  "p='pyproject.toml';t=open(p).read();open(p,'w').write(t.replace('protected_scope = [\"app\", \"workers\"]','protected_scope = [\"workers\"]',1))" \
  python -m pytest tests/unit/test_quality_gate_policy.py -q

# ---- migration hygiene -----------------------------------------------------
# A second head: the signature of two branches merged without rebasing.
prove "migration hygiene" migrations/versions/0043_schema_comment_convergence.py \
  "p='migrations/versions/0043_schema_comment_convergence.py';t=open(p).read();open(p,'w').write(t.replace('down_revision = \"0042_freshness_scope_and_reasons\"','down_revision = \"0041_active_calculation_version\"',1))" \
  python -m pytest tests/unit/test_migration_hygiene.py -q

echo
echo "gate failure-injection proof: ${PASS} passed, ${FAIL} failed"
echo
echo "== every touched file must be byte-identical to its pre-run snapshot =="
DIRTY=""
for f in "${TOUCHED[@]}"; do
  cmp -s "$f" "$WORK/snapshot/$f" || DIRTY="${DIRTY}  ${f}\n"
done
if [ -z "$DIRTY" ]; then
  echo "  ok    no injected defect survived"
else
  echo "  FAIL  an injection was not reverted:"
  printf "%b" "$DIRTY"
  FAIL=$((FAIL + 1))
fi

[ "$FAIL" -eq 0 ]
