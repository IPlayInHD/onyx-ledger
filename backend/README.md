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

## Run the tests (against real PostgreSQL)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .[dev]
# start Postgres 16 + pgvector, then:
bash scripts/run_backend_tests.sh          # provisions DB, applies schema, runs pytest
```

`tests/` covers: unit (tax engine golden cases, formula/condition/AI guardrail),
integration (register→profile→income→analysis→recommendations end to end), and
security (RLS cross-user isolation, refresh-token reuse detection).

## Run locally (Docker)

```bash
cd deploy && docker compose up --build
# API on :8000  (OpenAPI at /api/v1/openapi.json, docs at /docs)
# Postgres applies backend/db/sql on first boot; worker + redis + minio included.
```

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
