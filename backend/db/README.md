# Onyx Ledger — Database (PostgreSQL)

Production schema for the architecture in
[`docs/architecture/database-architecture.md`](../../docs/architecture/database-architecture.md).
14 schemas (13 DDD bounded contexts + shared `ref`), ~70 tables, UUIDv7 keys,
tax-year/time partitioning, an append-only audit trail, Row-Level Security, and
a fully data-driven tax rules engine.

## Layout

```
backend/db/
  sql/
    00_extensions_roles.sql   extensions, schemas, domains, UUIDv7, roles
    01_ref.sql                reference/lookup tables
    02_identity.sql           accounts, credentials, sessions, tokens, MFA
    03_profile.sql            tax profile, dependents, spouse, prefs, privacy
    04_finance.sql            income & expenses (LIST-partitioned by tax_year)
    05_wealth.sql             assets, valuations, registered accts, liabilities
    06_tax_kb.sql             versioned tax law, brackets, limits, benefits
    07_rules.sql              fact catalog, condition tree, formulas, outcomes
    08_analysis.sql           immutable analyses, line items, checks, assumptions
    09_reco.sql               recommendations + lifecycle events
    10_ai.sql                 conversations, messages, citations, embeddings
    11_docs.sql               object-store doc refs, OCR extractions, links
    12_admin.sql              admin users, RBAC, KB change-request governance
    13_billing.sql            plans, subscriptions, invoices, payment tokens
    14_audit.sql              immutable audit log, consent, PIPEDA requests
    15_triggers.sql           updated_at + generic audit triggers
    16_rls_grants.sql         RLS policies + least-privilege grants
    17_indexes.sql            GIN / BRIN / trigram / pgvector HNSW
    90_seed_reference.sql     reference data (idempotent)
    91_seed_example_rule.sql  worked example: Medical Expense Credit 2025
  README.md
  DATA_DICTIONARY.md
```

## Migration order (Alembic)

Each SQL file maps to one Alembic revision, applied in filename order. The
`down_revision` chain mirrors the numeric prefix.

| Revision | File | Notes |
|----------|------|-------|
| 0001_foundation | 00 | needs superuser (CREATE EXTENSION/ROLE) |
| 0002_ref | 01 | |
| 0003_identity | 02 | |
| 0004_profile | 03 | |
| 0005_finance | 04 | creates initial year partitions |
| 0006_wealth | 05 | |
| 0007_tax_kb | 06 | `formula_id` FKs deferred |
| 0008_rules | 07 | back-fills deferred FKs into tax_kb |
| 0009_analysis | 08 | |
| 0010_reco | 09 | |
| 0011_ai | 10 | `reviewed_by_admin_id` FK deferred |
| 0012_docs | 11 | composite FKs into partitioned finance tables |
| 0013_admin | 12 | back-fills `ai_explanation` reviewer FK |
| 0014_billing | 13 | |
| 0015_audit | 14 | creates initial month partitions |
| 0016_triggers | 15 | run AFTER all tables exist |
| 0017_rls_grants | 16 | |
| 0018_indexes | 17 | build HNSW/GIN CONCURRENTLY in prod |
| 0019_seed_reference | 90 | data migration (idempotent) |
| 0020_seed_example | 91 | data migration (demo rule) |

> Ordering rule: `ref → identity → profile → finance → wealth → tax_kb → rules →
> analysis → reco → ai → docs → admin → billing → audit → triggers → rls →
> indexes → seed`. `tax_kb`↔`rules` and `ai`↔`admin` use deferred FKs added by
> the later domain, so neither blocks the other.

## Apply the schema

**Via Alembic (forward-migration path — recommended):** the migration chain in
`backend/migrations/` mirrors these files 1:1 in the order above.
```bash
cd backend && ONYX_DATABASE_URL_SYNC="postgresql+psycopg2://onyx_migrator@host/onyx" alembic upgrade head
```

**Directly (psql, dev):**
```bash
for f in backend/db/sql/*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"; done
```

Both produce an identical database.

## SQLAlchemy / FastAPI notes

- Reflect or hand-map these tables to SQLAlchemy 2.x `DeclarativeBase` models,
  one module per schema. Set `__table_args__ = {"schema": "<domain>"}`.
- Generate future migrations with Alembic autogenerate, but keep partitioning,
  RLS, triggers, and the HNSW index as **hand-written** operations (autogenerate
  does not model them).
- The API must run, per request/transaction:
  `SET LOCAL app.user_id = '<uuid>'; SET LOCAL app.actor_type = 'user';`
  so RLS policies and the audit trigger capture the actor. Connect the runtime
  as `onyx_app_rw` (never the migrator/superuser).

## Partition & index maintenance

- Add next year's finance partitions and next month's `audit_log` partition via
  a scheduled job (or `pg_partman`). A `DEFAULT` partition catches anything
  missed so writes never fail.
- Build `17_indexes.sql` objects `CONCURRENTLY` in production after the initial
  data load to avoid long write locks.
