---
name: onyx-tax-ingest
description: Convert a batch of tax-source documents into governed production tax knowledge using the already-certified Onyx source registry, authoring pipeline and publication service. Content ingestion only — the architecture is frozen. Invoke manually with /onyx-tax-ingest and a batch scope. Do NOT auto-invoke; it registers immutable provenance and can publish runtime tax authority.
---

# Onyx Tax Content Ingestion

This skill turns source documents into published tax knowledge. It builds
nothing. Every subsystem it uses is already certified, and the architecture is
**frozen** for the duration of a run.

The distinction that governs everything below:

> A source document is **provenance**. Published structured knowledge is
> **runtime authority**. Nothing crosses from the first to the second except
> through deterministic validation and an authorized human publication.

## Invocation boundary — read this first

Run this only when the user explicitly invokes `/onyx-tax-ingest`, or names a
batch and asks you to ingest it under this skill. A run registers immutable
source versions and can publish rules the tax engine will evaluate for real
users. Starting it because a task merely mentions tax documents would write
provenance nobody asked for.

If you are reading this because the skill auto-triggered on an ordinary
question, stop and answer the question instead.

**This skill is not `/onyx-entry`.** It opens no engineering entry, changes no
schema, and adds no subsystem. If a batch genuinely cannot be represented, it
stops that item with a blocker report and continues the rest — it does not
design a fix.

## Reference files

| File | Read it when |
|---|---|
| `source-policy.md` | Fingerprinting, dedup, identity, classification, qualifying authority, supersession |
| `content-routing.md` | Deciding what a PDF / CSV / JSON / YAML may become |
| `review-policy.md` | Deciding what a human must look at before publication |

Templates: `templates/batch-report.md`, `templates/blocker-report.md`.

---

## PHASE 0 — Discover the contracts. Never hardcode them.

Version strings, vocabularies, error codes and service methods change between
entries. A number written into this file is a lie waiting to be told, so
**every run re-reads them from the repository.** Run this first, from
`backend/`:

```bash
PYTHONPATH=. python - <<'PY'
from app.services.tax_kb.sources.domain import (
    SOURCE_MANIFEST_SCHEMA_VERSION, SourceType, IssuerCode,
    FingerprintMethod, LOCATOR_KEYS)
from app.services.tax_kb.authoring.spec import (
    KNOWLEDGE_SPEC_SCHEMA_VERSION, ReferenceDataKind)
from app.services.tax_kb.authoring.manifest import MANIFEST_SCHEMA_VERSION
from app.services.tax_kb.authoring.policy import (
    PUBLICATION_POLICY_VERSION, QUALIFYING_PRODUCTION_AUTHORITY)
from app.services.tax_kb.authoring.codes import (
    PUBLISHABILITY_REPORT_SCHEMA_VERSION, ValidationCode, Family, Readiness)
from app.services.tax_kb.authoring import validation as v
from app.services.tax_engine.core.formula_sandbox import SUPPORTED_OPERATIONS

print("source manifest schema :", SOURCE_MANIFEST_SCHEMA_VERSION)
print("knowledge spec schema  :", KNOWLEDGE_SPEC_SCHEMA_VERSION)
print("authoring manifest     :", MANIFEST_SCHEMA_VERSION)
print("publishability report  :", PUBLISHABILITY_REPORT_SCHEMA_VERSION)
print("publication policy     :", PUBLICATION_POLICY_VERSION)
print("source types           :", sorted(t.value for t in SourceType))
print("QUALIFYING authority   :", sorted(t.value for t in QUALIFYING_PRODUCTION_AUTHORITY))
print("issuers                :", sorted(i.value for i in IssuerCode))
print("fingerprint methods    :", [m.value for m in FingerprintMethod])
print("locator keys           :", sorted(LOCATOR_KEYS))
print("reference data kinds   :", sorted(ReferenceDataKind.ALL))
print("formula operations     :", sorted(SUPPORTED_OPERATIONS))
print("outcome types          :", sorted(v.OUTCOME_TYPES))
print("dependency types       :", sorted(v.DEPENDENCY_TYPES))
print("validation codes       :", len(list(ValidationCode)))
print("readiness families     :", sorted(f.value for f in Family))
print("readiness states       :", sorted(r.value for r in Readiness))
PY
```

Then the service surface, so a renamed method is caught before it is called:

```bash
PYTHONPATH=. python -c "
import inspect
from app.services.tax_kb.authoring.service import KnowledgeAuthoringService as K
from app.services.tax_kb.sources.registry import TaxSourceRegistry as R
print('authoring:', [n for n in dir(K) if not n.startswith('_')])
print('registry :', [n for n in dir(R) if not n.startswith('_')])
print('publish  :', inspect.signature(K.publish))
"
grep -oE 'sub.add_parser\(\"[a-z]+\"' scripts/knowledge_pack.py
```

**If any import fails, stop.** The architecture moved and this skill's
assumptions need re-reading, not working around.

Also confirm the starting state is one you can attribute results to:

```bash
git status --short && git rev-parse HEAD
```

A dirty tree is not fatal for ingestion — no production code changes — but say
so in the report, because a manifest committed alongside unrelated edits is
hard to review.

## PHASE 1 — Locate the source library

Take the path as an argument if given. Otherwise discover it. **Do not assume**,
and do not create a stand-in directory if it is absent — an empty local folder
that looks like the library is worse than no library.

The library may not be a filesystem path at all. If it is reachable only
through a connector (Google Drive and similar), every access is an API call and
`open()`/`glob` do not apply. Record which access mode is in force; it changes
how hashing and extraction are written.

**The library is READ ONLY.** You may list, read, hash, extract and inspect.
You may never rename, edit, normalize in place, move, delete, or write
generated artifacts into it. Everything you generate goes into the repository
or an explicit working directory.

## PHASE 2 — The run loop

Each numbered step is a gate on the next. See `source-policy.md` for 2–6 and
`content-routing.md` for 7–8.

1. **Discover** the selected source files for this batch.
2. **Fingerprint** — SHA-256 over exact bytes.
3. **Compare** against already-registered fingerprints.
4. **Skip** exact duplicates and unchanged files.
5. **Classify** each new file: issuer, jurisdiction, source type, tax year or
   effective scope, official identifier where one exists.
6. **Register** the immutable source and source-version metadata.
7. **Route** by format.
8. **Extract** candidate structured knowledge.
9. **Cite** — attach exact structured locators to registered source versions.
10. **Validate** through the existing authoring validators. Do not write your
    own checks.
11. **Test** — run the examples, golden vectors and boundary cases the spec
    carries.
12. **Identify** conflicts, ambiguities and capability gaps.
13. **Publish** only validated, reviewed content, through the existing
    publication path.
14. **Update** the deterministic ingestion/coverage index.
15. **Report** using `templates/batch-report.md`.

### Use the pipeline's own entry points

Do not reimplement any of this. The operator tool wraps the whole path:

```bash
PYTHONPATH=. python scripts/knowledge_pack.py validate <manifest.json>   # writes nothing
PYTHONPATH=. python scripts/knowledge_pack.py stage    <manifest.json>   # drafts + verdicts
PYTHONPATH=. python scripts/knowledge_pack.py publish --publisher <id> \
    <version-id>:<spec-hash> ...
```

`validate` is the one to run constantly — it is side-effect free by
construction, so running it against production costs nothing but time.

**Always dry-run the whole batch before staging anything.** The validator
reports every error at once with machine codes; discovering the eleventh
problem after publishing ten rules is the failure this ordering prevents.

## PHASE 3 — Batching and parallelism

Batch by **domain or topic**, not by folder or file count. A batch should be a
set of knowledge a reviewer can hold in their head at once — one credit, one
year's brackets, one filing regime.

**Parallelize read-only work freely:** hashing, extraction, classification,
draft generation. These touch nothing shared.

**Serialize publication wherever rule authority could overlap.** Two batches
publishing versions of the same rule and tax year must not run concurrently;
the pipeline locks and will refuse the loser, but a refused half-batch is
churn you can avoid by ordering.

**Avoid N+1 access.** Resolve provenance for a whole pack once and validate the
pack as a pack. Per-object provenance resolution costs several statements each
and turns a content pack into thousands of round trips. Measure the statement
count if a batch feels slow; it should be roughly flat in batch size.

## PHASE 4 — Authority, and what AI may not do

| Role | Authority |
|---|---|
| Source document | provenance only |
| Published structured knowledge | runtime knowledge |
| `RulesEvaluatorService` | sole eligibility authority |
| `TaxEngineService` | sole calculation authority |
| AI (including you) | **may draft. May not validate tax meaning authoritatively, may not approve, may not publish.** |

A draft you produced is a proposal. What makes it publishable is the
deterministic validators passing and an authorized human approving — never your
confidence in it. There is no override flag anywhere in this path; if you find
yourself wanting one, you have found a blocker, not an obstacle.

**Five counters must be zero in every report.** They are not aspirations, they
are invariants of the certified architecture, and a non-zero value means
something is being done outside the pipeline:

- unsourced production objects
- arbitrary executable logic
- runtime PDF reads
- runtime web lookups
- runtime AI calls

## PHASE 5 — Architecture freeze

Add nothing for convenience. If a **real, supplied** tax provision cannot be
represented correctly by the existing capability, do not approximate it, do not
widen a vocabulary, and do not invent a formula operation.

Produce `templates/blocker-report.md` for that item, containing the exact
source, its fingerprint and version, the exact locator, the provision, the
required semantic, the existing capability, the demonstrated mismatch, and the
**smallest possible** capability change that would close it.

Then **stop only that item** and continue every independent item in the batch.
A blocked provision is not a blocked batch.

## Stop conditions

Stop and report instead of improvising when:

- the source library cannot be located, or is writable-and-being-written
- a source cannot be fingerprinted (bytes unavailable)
- a provision needs a formula operation the engine does not implement
- two authoritative sources conflict and neither is clearly superseded
- provenance cannot be attached to a knowledge object that requires it
- publication would need an override
- the discovery step in PHASE 0 fails to import

Reporting a blocker with its exact locator is a successful outcome. Publishing
a guess is not.

## What this skill will not do

- Open an engineering entry, change schema, or add a subsystem.
- Modify, rename or delete anything in the source library.
- Treat AI-generated JSON as authoritative because it is machine-readable.
- Use a PDF, a web lookup or a model call as runtime tax authority.
- Publish anything a deterministic validator did not pass.
