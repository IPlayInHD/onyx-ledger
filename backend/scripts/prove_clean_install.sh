#!/usr/bin/env bash
# Prove the lock alone reconstructs a working environment.
#
# A lock that has only ever been used to top up an already-working virtualenv
# proves nothing. This builds an EMPTY interpreter environment, installs from the
# lock with hash verification and nothing else, imports the application, reports
# the resolved versions, and runs a bounded smoke test.
#
# No credentials are read or printed. The smoke test is deliberately
# database-free so this can run anywhere.
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/check_python.sh

VENV="$(mktemp -d)/clean"
trap 'rm -rf "$(dirname "$VENV")"' EXIT

echo "== building an empty environment =="
python -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

echo "== installing from requirements-dev.lock.txt ONLY =="
# --require-hashes makes pip verify every artifact against the committed hash and
# refuse anything the lock does not pin — including transitive dependencies.
# pyproject.toml is not consulted for versions; -e . adds the package itself
# with --no-deps so it cannot pull an unlocked resolution in behind the lock.
"$VENV/bin/pip" install --quiet --require-hashes -r requirements-dev.lock.txt
"$VENV/bin/pip" install --quiet --no-deps -e .

echo "== resolved versions =="
"$VENV/bin/python" - <<'EOF'
import importlib.metadata as md
import sys

print(f"{'python':22s} {sys.version.split()[0]}")
for dist in ("sqlalchemy", "alembic", "fastapi", "pydantic", "pydantic-settings",
             "asyncpg", "psycopg2-binary", "celery", "redis", "pgvector",
             "structlog", "mypy", "ruff", "pytest", "pytest-asyncio"):
    print(f"{dist:22s} {md.version(dist)}")
EOF

echo "== importing the application =="
ONYX_JWT_SECRET="clean-install-proof-secret-32-bytes-x" \
ONYX_DATABASE_URL="postgresql+asyncpg://unused@/unused" \
PYTHONPATH=. "$VENV/bin/python" - <<'EOF'
from app.main import create_app
from app.services.tax_engine.core.engine import TaxInput, compute
from app.services.ioe.domain import canonical as c

app = create_app()
routes = sum(1 for _ in app.routes)
print(f"FastAPI app built with {routes} routes")
print(f"canonical serialization version: {c.CANONICAL_SERIALIZATION_VERSION}")

# A bounded smoke test: the pure engine is deterministic and needs no database,
# so a wrong resolution of Decimal handling or a broken import shows up here.
result = compute(TaxInput(province="ON", year=2025, employment_income=c.Decimal("80000")))
assert result.total_payable > 0, "engine produced no payable"
print(f"engine smoke: ON/2025 on 80000 employment income -> {result.total_payable}")
EOF

echo "== celery app loads (workers import cleanly) =="
ONYX_JWT_SECRET="clean-install-proof-secret-32-bytes-x" \
ONYX_DATABASE_URL="postgresql+asyncpg://unused@/unused" \
PYTHONPATH=. "$VENV/bin/python" -c "
from workers.celery_app import celery_app
print('celery app:', celery_app.main)
"

echo
echo "clean-environment install from lock: OK"
