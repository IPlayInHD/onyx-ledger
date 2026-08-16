#!/usr/bin/env bash
# onyx-entry preflight: report dynamic repository state. READ ONLY.
#
# Nothing here mutates git, the database, or any file. No commits, no pushes,
# no schema application. It exists so the skill never hardcodes a SHA, a
# migration head, or a schema version that will be wrong next week.
#
#   .claude/skills/onyx-entry/scripts/preflight.sh
set -uo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "not a git repository"; exit 1; }
cd "$ROOT"

echo "=== git ==="
echo "branch          $(git rev-parse --abbrev-ref HEAD)"
echo "HEAD            $(git rev-parse HEAD)"
UP=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) \
  && echo "upstream        ${UP} $(git rev-parse '@{u}')" \
  || echo "upstream        (none configured)"
DIRTY=$(git status --porcelain)
if [ -z "$DIRTY" ]; then
  echo "working tree    clean"
else
  echo "working tree    DIRTY"
  printf '%s\n' "$DIRTY" | sed 's/^/                /'
fi
echo "last commit     $(git log --oneline -1)"

echo
echo "=== schema / migrations ==="
# Highest-numbered raw SQL file: db/sql is the authoritative schema.
if [ -d backend/db/sql ]; then
  echo "latest db/sql   $(ls backend/db/sql/*.sql 2>/dev/null | sort | tail -1 | xargs -r basename)"
fi
# Alembic revisions read from the files, so this needs no database. The real
# head is whatever no other revision names as down_revision; `alembic heads`
# is the authority when a database is available.
if [ -d backend/migrations/versions ]; then
  echo "revision files  $(ls backend/migrations/versions/*.py 2>/dev/null | wc -l)"
  echo "latest revision $(ls backend/migrations/versions/*.py 2>/dev/null \
        | sort | tail -1 | xargs -r basename)"
  echo "                (authoritative head: PYTHONPATH=. alembic heads, needs a DB)"
fi

echo
echo "=== governed version authority ==="
# Read the values from the module rather than restating them anywhere.
python3 - <<'PY' 2>/dev/null || echo "  (could not import; read app/services/ioe/domain/scenario.py directly)"
import pathlib, re, sys
p = pathlib.Path("backend/app/services/ioe/domain/scenario.py")
if not p.exists():
    sys.exit(1)
src = p.read_text()
for name in ("CURRENT_SCENARIO_RESULT_SCHEMA_VERSION",
             "SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS",
             "DERIVED_STATE_BEARING_VERSIONS"):
    m = re.search(rf"^{name}\s*[:=].*?(?=\n[A-Z_#]|\n\n)", src, re.S | re.M)
    if m:
        print("  " + " ".join(m.group(0).split()))
PY

echo
echo "=== gate + CI entry points ==="
for f in backend/scripts/release_gate.sh backend/scripts/run_backend_tests.sh \
         backend/scripts/check_types.sh backend/scripts/prove_security_gate.sh \
         backend/scripts/prove_suite_state_independence.sh \
         .github/workflows/backend-quality-gate.yml; do
  [ -f "$f" ] && echo "  present  $f" || echo "  MISSING  $f"
done

echo
echo "=== test inventory (files, not counts) ==="
for d in backend/tests/unit backend/tests/integration backend/tests/security \
         backend/tests/privacy backend/tests/golden; do
  [ -d "$d" ] && echo "  $(printf '%-28s' "$d") $(find "$d" -name 'test_*.py' | wc -l) files"
done

echo
echo "Reminder: test counts, CI run ids and gate timings are discovered per run."
