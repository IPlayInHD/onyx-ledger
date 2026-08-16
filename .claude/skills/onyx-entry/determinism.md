# Determinism

A sealed artifact must reproduce byte-for-byte across processes, machines and
deploys. Verification, replay and integrity all rest on that; if a hash moves
for a reason unrelated to the data, every downstream guarantee is noise.

## Reuse the canonicalizer — do not write a second one

`app/services/ioe/domain/canonical.py` is the sole canonicalization and hashing
authority. It provides value renderers (`money`, `rate`, `factor`, `quantity`),
`canonicalize`, `canonical_text`, `canonical_hash`, and domain-separated
`domain_hash` over a registered domain list.

A second canonicalization implementation is a second answer to "what are these
bytes", which is the same failure as a second tax engine.

**Registering a new hash domain**: add the constant and include it in
`ALL_HASH_DOMAINS`. `domain_hash` refuses an unregistered domain — that refusal
is what makes reuse enforceable rather than conventional.

## The rules

**Decimal, never float.** Governed financial values are `Decimal` rendered
through the canonicalizer's scale. A float is not a money type and the
canonicalizer will refuse it.

**Stable ordering everywhere.** Sort before serializing. Sort before hashing.

**No dependence on set iteration order.** `PYTHONHASHSEED` randomizes it, so
anything built from a set moves between processes.

**No dependence on dict insertion order** unless the canonicalizer explicitly
fixes it.

**No dependence on database row order without `ORDER BY`** or a canonical sort
after loading. PostgreSQL makes no ordering promise you did not ask for.

**No semantic equality from presentation strings.** Labels are for humans.
Identity comes from the certified semantic key.

**No array-position matching for domain identity.** Match on keys. Positional
matching silently pairs unrelated things the moment a list changes length.

**No uncontrolled wall-clock data in deterministic artifacts.** A timestamp
captured at serialization time makes every artifact unique and every comparison
meaningless. Timestamps that are genuinely part of the data are fine; timestamps
that record when the bytes were produced are not.

**Prove it under seed variation.** Where an artifact is hashed or compared, run
the production in subprocesses under `PYTHONHASHSEED` 0, 1 and 42 and assert the
output is identical. Build the payload inside the child where practical — a
payload shipped through `argv` is capped at 128 KiB (`MAX_ARG_STRLEN`) and will
fail with `E2BIG` once the data grows. Pass a file path instead.

## Versioning and compatibility

`app/services/ioe/domain/scenario.py` owns the scenario-result protocol
versions. Read the current values from the module — never hardcode them here or
in code that dispatches on them.

The module distinguishes three questions deliberately:

- which version **new writes** use;
- which versions the **protocol supports**;
- which versions **bear** a given derived structure.

**Do not scatter literal version equality checks.** When the architecture offers
a governed version-family or dispatch mechanism, use it. A literal
`== SCHEMA_V2` in five call sites silently goes false the day V3 arrives, and
each site fails differently. That exact failure has happened here.

**When introducing a new protocol version, audit every consumer.** Grep for the
previous version literal and for the dispatch constants. Include the privacy
gate and any policy predicate that names a version — a gate holding a stale
literal will demand the wrong thing.

**Preserve existing historical versions.** Migration or backfill of sealed data
requires explicit approval; it is not a side effect of shipping a new version.

**Never rewrite historical sealed bytes to fit current code.** The bytes are the
evidence. Code accommodates them, not the reverse.

**Golden corpus stays byte/hash stable** where that is the existing contract
(`tests/golden/`). A golden vector that needs updating is a signal to stop and
ask why, not a file to regenerate.
