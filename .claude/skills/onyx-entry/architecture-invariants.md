# Architecture orientation

A map, not a specification. The authoritative descriptions live in
`docs/architecture/`; read the ones an entry touches before designing against
them. Treat this file as an index that goes stale slowly, and the repository as
the thing that is actually true.

## Layout

```
backend/
  app/
    services/
      tax_engine/        calculation + rule/eligibility authorities
      ioe/               intelligent optimization engine
        domain/          canonical.py (hash authority), scenario.py (version authority)
        scenario/        scenario service, sealed historical source, counterfactual,
                         historical graph, comparison engine
        frozen/          frozen baseline snapshots
        replay/          historical replay
      state_graph/       Personal Tax State Graph: contracts, hashing, projection
    privacy/             classification.py — per-table privacy authority
    database/            models, session, unit of work
  db/sql/                authoritative schema (numbered, applied in order)
  migrations/            Alembic chain, mirrors db/sql 1:1
  scripts/               gates, proofs, probes, test runner
  tests/
    unit/ integration/ security/ privacy/ golden/
docs/
  architecture/  privacy/  operations/
```

## Subsystems worth knowing before you design

**Personal Tax State Graph** — a typed node/edge model of a person's tax
position, with certified semantic keys, canonical payloads and a domain-separated
graph hash. Node and edge types are a governed taxonomy: some are live, some are
reserved with no producer yet. Reserved is not the same as absent.

**Sealed scenarios** — a scenario seals its result under a protocol version,
pinning the rule versions and input snapshot it used. Sealed bytes are evidence
and are never rewritten to suit current code.

**Frozen baseline** — the baseline side of a scenario, captured at seal time so
a later comparison is between two frozen artifacts rather than one artifact and
the present.

**Historical source layer** — loads sealed rows only, and reports per-family
`SourceAuthority` so downstream code can distinguish authoritative emptiness
from an unloaded family.

**Freshness relay and outbox** — propagates "newer inputs exist" signals through
a claimed, leased, bounded queue. Freshness is about suitability as a current
recommendation, not about integrity.

**Account lifecycle / privacy workers** — governed deletion with a durable
ledger, claimed through a bounded oldest-first queue by a worker holding its own
role.

**Admission control** — rate limiting, concurrency leases, idempotency in front
of expensive work.

## Invariants that survive individual entries

- One governed authority per material decision (`authority-model.md`).
- Sealed history is reproducible from what it was sealed with, and is never
  reconstructed from live state.
- Freshness and integrity are separate verdicts.
- Empty is not missing; not-applicable is not zero.
- Determinism is a contract, not an aspiration (`determinism.md`).
- Tenancy is enforced in the database, not only in application code
  (`privacy-security.md`).
- Tests establish their own state (`test-isolation.md`).
- The schema in `db/sql/` and the Alembic chain must agree; drift is a gate
  failure, not a formality.
