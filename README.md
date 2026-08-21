# Onyx Ledger

**See your tax position. See what matters. See what changes if you act.**

Onyx Ledger is a Canadian tax intelligence product: it computes a taxpayer's
federal and provincial position from facts they provide, shows what the governed
rules found, and models what a single decision would change — with the
provenance of every figure on screen.

> Educational estimates only. Onyx Ledger does not file returns and is not
> affiliated with or endorsed by the Canada Revenue Agency.

## One authority

Every customer-facing figure comes from the **certified Python/FastAPI backend**
in `backend/`. Its calculations are deterministic, versioned, replayable and
gated; results are sealed and can be re-derived. The frontend displays those
figures and never computes a tax number of its own.

That constraint is the product. Two engines cannot both be right, and nothing
reconciles them.

## Repository layout

```
onyx-ledger/
├── backend/     The certified FastAPI application — THE tax authority
│   ├── app/         api → services → domain, with database/integration adapters
│   ├── workers/     Celery workers and the Beat schedule
│   ├── db/          Validated schema, RLS policies, roles
│   └── scripts/     release_gate.sh and the other gates CI mirrors
├── frontend/    The certified React/TypeScript customer application
│   ├── src/         Screens, the one API client, the design system
│   └── e2e/         Browser journeys against a real backend
├── docs/        Architecture, operations and privacy specifications
└── legacy/      ARCHIVED prototypes — never deployed. See legacy/README.md
```

## Launch scope

**Federal + Ontario.** Those are the jurisdictions with governed published
brackets behind them. Alberta and British Columbia are computed from constants
resident in the engine rather than published knowledge, and Quebec needs QPP and
QPIP handling the engine does not implement — so neither is offered to
customers. The backend owns that list; the frontend asks it rather than keeping
its own.

## Running it

**Backend** — Python 3.11 only, installed from the hash-pinned lock:

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock.txt
pip install --no-deps -e .
PYTHONPATH=. uvicorn app.main:app --reload
```

**Frontend** — Node 20, installed from the lockfile:

```bash
cd frontend
npm ci
npm run dev
```

**Everything at once**, including PostgreSQL, Redis and object storage:

```bash
cd backend/deploy && docker compose up
```

## Gates

Nothing is certified by local tests alone.

```bash
cd backend && ./scripts/release_gate.sh --full   # the full gate (hours)
cd frontend && npm run lint && npm run typecheck && npx vitest run && npm run build
cd frontend && ./scripts/e2e.sh                  # browser journeys, real backend
```

CI runs the backend quality gate and the frontend quality gate on every push,
and both are blocking.

## Deployment

- `docs/operations/production-architecture.md` — the production design
- `NETLIFY.md` — how the static frontend is published

The archived prototype under `legacy/` must never be deployed; a test in
`backend/tests/security/test_legacy_engine_not_deployable.py` enforces it.
