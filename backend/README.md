# Onyx Ledger — Backend (FastAPI)

Implements [`docs/architecture/backend-architecture.md`](../docs/architecture/backend-architecture.md)
on top of the validated [`backend/db`](./db) schema. Clean Architecture + DDD:
`api → services → domain` (pure) with `database`/`integrations` adapters.

## What's implemented (tested, running)

- **Auth** — register, login, JWT access + rotating refresh with reuse detection (Argon2id).
- **Users/Profile** — account + tax profile.
- **Financials** — tax-year-scoped income & expenses.
- **Tax engine (pure)** — deterministic federal + provincial calculation
  (brackets, credits, dividends, capital gains, CPP/EI, self-employed CPP,
  rental) — matches the validated reference figures.
- **Rules engine (DB-driven)** — sandboxed RPN formula evaluator + boolean
  condition-tree evaluator reading the versioned KB; **no AI, no hardcoded law**.
- **Optimization + Analysis** — ranked, cited recommendations; immutable
  `analysis_run` with input snapshot, line items, reconciliation checks.
- **AI explanation** — RAG service with a guardrail that blocks fabricated
  numbers (never computes tax).
- **Platform** — RLS-binding Unit of Work, correlation-id middleware, RFC-9457
  errors, health/ready probes, Celery worker + Beat schedule.

Scaffolded (ports + routes, returning 501): documents/OCR pipeline, ingestion +
four-eyes publishing, admin RBAC.

## Developer setup

**Python 3.11 only.** One supported runtime, declared in `pyproject.toml`
(`requires-python = "==3.11.*"`) and in `.python-version` (`3.11.15`).
`scripts/check_python.sh` refuses anything else — determinism of the dependency
resolution, of canonical serialization, and of the sealed hashes is measured on
3.11 and nowhere else.

Install **from the lock**, not from the declaration:

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock.txt   # exact, hash-verified
pip install --no-deps -e .                                  # the package itself
```

`--require-hashes` verifies every artifact, including transitive dependencies,
against `requirements-dev.lock.txt`. `--no-deps` stops the editable install
resolving anything behind the lock's back. CI and production use the same two
commands, so all three environments resolve identically.

Changed a dependency in `pyproject.toml`?

```bash
./scripts/lock_dependencies.sh      # regenerate; commit the locks with pyproject.toml
./scripts/check_lock.sh             # what CI runs — fails if they disagree
```

## Run the tests (against real PostgreSQL)

```bash
# start Postgres 16 + pgvector, then:
bash scripts/run_backend_tests.sh          # provisions DB, applies schema, runs pytest
```

`tests/` covers: unit (tax engine golden cases, formula/condition/AI guardrail),
integration (register→profile→income→analysis→recommendations end to end), and
security (RLS cross-user isolation, refresh-token reuse detection).

The suite connects as `onyx_test`, a login member of `onyx_app_rw` — **not** as
a superuser. A superuser bypasses row-level security entirely, so security
assertions run as one prove nothing.

## Quality gates

```bash
./scripts/release_gate.sh --fast    # runtime pin, lock, Ruff, protected mypy   (~1 min)
./scripts/release_gate.sh --full    # + fresh-DB suite, security, migrations, schema drift
```

`--full` mirrors the blocking CI jobs in
`.github/workflows/backend-quality-gate.yml`, so a failure is reproducible
locally. Individually:

| Command | Gate |
|---|---|
| `ruff check app workers scripts tests` | lint |
| `./scripts/check_types.sh` | **protected mypy — zero errors, whole application** |
| `./scripts/check_lock.sh` | dependency lock matches `pyproject.toml` |
| `./scripts/check_migrations.sh` | upgrade head → downgrade base |
| `./scripts/check_schema_drift_on_fresh_db.sh` | schema drift vs. governed policy |
| `./scripts/prove_gates_fail.sh` | proves each gate rejects an injected defect |
| `./scripts/prove_security_gate.sh` | proves the security suite catches a removed RLS invariant |
| `./scripts/prove_clean_install.sh` | proves the lock alone rebuilds a working environment |
| `./scripts/pollution_regression.sh` | release-level: sensitive suites on a database with history |

See
[`docs/architecture/production-engineering-quality-gate.md`](../docs/architecture/production-engineering-quality-gate.md)
and [`docs/architecture/mypy-debt-register.md`](../docs/architecture/mypy-debt-register.md).

## Run locally (Docker)

```bash
cd deploy && docker compose up --build
# API on :8000  (OpenAPI at /api/v1/openapi.json, docs at /docs)
# Postgres applies backend/db/sql on first boot; worker + redis + minio included.
```

## Migrations (Alembic)

The canonical schema lives in `db/sql/*.sql`. Alembic **mirrors** it: a 20-revision
chain (`0001_foundation … 0020_seed_example`), one revision per SQL file in
dependency order, each a thin wrapper that executes its file verbatim. So
`alembic upgrade head` and running the SQL by hand produce an identical database
(verified: the full test suite passes against an Alembic-built DB).

```bash
# build the whole schema from scratch (needs an elevated role: CREATE EXTENSION/ROLE)
ONYX_DATABASE_URL_SYNC="postgresql+psycopg2://onyx_migrator@localhost/onyx" \
  alembic upgrade head

alembic current           # -> 0020_seed_example (head)
alembic history           # the file-by-file chain
alembic downgrade base    # full teardown (the SQL baseline is applied/removed as a unit)

# adopt an EXISTING db that was built by the raw SQL files:
alembic stamp head
```

**Going forward:** change the schema by adding a **new** SQL file + a **new** revision
(never mutate an applied one). `env.py` wires `Base.metadata` with `include_schemas`,
so incremental table changes can use `alembic revision --autogenerate` — but keep
partitioning, RLS, triggers, and the HNSW index as hand-written ops (autogenerate
does not model them).

## Layout

```
app/
  api/v1/{auth,users,financials,tax,analysis,recommendations,documents,admin}
  core/            config, security (jwt/password), logging, exceptions, middleware
  domain/          pure entities + ports (adapter interfaces)
  services/
    tax_engine/core/   PURE deterministic engine + RPN sandbox + condition eval
    tax_engine/        DB-backed rules evaluator + input assembly
    auth/ financial/ optimization/ analysis/ ai/ ...
  database/        async session + Unit of Work (sets RLS GUCs), models, repositories
  integrations/    S3 / LLM / OCR / email adapters (ports impl)
workers/           Celery app + tasks (analysis, maintenance/Beat)
tests/             unit · integration · security
deploy/            Dockerfile · docker-compose.yml · ci/
scripts/           apply_schema.sh · run_backend_tests.sh
```

## Security notes

- Runtime connects as `onyx_app_rw` (never the migrator/superuser). The UoW sets
  `app.user_id`/`app.actor_type` per transaction → PostgreSQL RLS isolates users
  and the audit triggers capture the actor.
- Passwords: Argon2id. Session/reset/verification tokens: stored as SHA-256
  hashes only. Never stored: SIN, government/bank credentials, card numbers.
- Errors are `application/problem+json` (RFC-9457) with a correlation id.
