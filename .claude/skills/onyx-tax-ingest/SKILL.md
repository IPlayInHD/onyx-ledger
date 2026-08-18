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

## PHASE 1 — Reach the source library

**The source library is reached through Claude's native Google Drive
connector.** That is the configured, working, intended primary path, and it is
addressed by **Drive folder/file ID**.

The library is **not** a filesystem directory and is not expected to be one.
Do **not** search the filesystem for it as your discovery mechanism, and do not
treat the absence of a mount as a failure — there is nothing to find.

```
GOOGLE DRIVE MCP  →  download once  →  local bytes  →  RAW_BYTES_SHA256
                                                    →  parse / extract
                                                    →  Source Registry + authoring pipeline
```

Verify the connector, not the filesystem. Discovery is `search_files` scoped by
`parentId`, paging until exhausted, recursing into child folders. Bytes come
from `download_file_content`, which returns base64 that decodes to the exact
file — so `RAW_BYTES_SHA256` is available and is what you use.

If the connector is unauthorized, that is a **stop condition**: say so and ask
for it to be reconnected. Do not route around it.

**Never build a replacement path.** No rclone, no FUSE, no
`google-drive-ocamlfuse`, no custom Drive API integration, no separately
engineered sync service, and no manual bulk download standing in for the
connector. If a filesystem copy of the library happens to exist, it is
**optional** and never required; the connector remains authoritative.

### Google Drive is operationally READ ONLY

The authenticated account has write capability. **Ignore it.** For ingestion the
library is an immutable evidence store.

| Allowed | Forbidden |
|---|---|
| `search_files` / list | `create_file` |
| `get_file_metadata` | `update_file` |
| `download_file_content` (bytes) | `copy_file` writing back into source storage |
| `read_file_content` / inspect | `trash_file` |
| | rename, move, restructure folders |
| | replacing a source file |
| | editing CSV/JSON/YAML in place |

A run that performs any Drive write has failed, whatever else it produced.

### Optional local cache — an optimization, never a source

You **may** materialize downloaded bytes into a temporary local cache so one
ingestion run does not download the same large PDF repeatedly. Choose a safe
temporary path outside the repository; do not commit to one location by habit.

The cache must be:

- **outside Git**, never committed, never staged
- **disposable and rebuildable** from the connector at any time
- **not authoritative** — it is a copy, and the Drive object is the source
- carrying, per cached file: the **Drive file ID**, the **SHA-256**, and a
  **byte-size check** against Drive's reported size where metadata permits

Never treat a cached file as the original, and never let a cache hit substitute
for provenance. If the cache and Drive disagree on size or digest, the cache is
wrong — discard it and re-download.

**Do not dump large base64 payloads into the main context.** A single mid-size
PDF's base64 costs tens of thousands of tokens for no benefit.

**Never route the payload through model output.** Re-emitting base64 — writing
it to a file by generating it, in a subagent or anywhere else — is
transcription, and transcription of a high-entropy string is not exact.
Measured: a 10,244-byte file relayed this way arrived as 4,228 bytes, and a
7,343-byte file arrived with an invalid base64 length. Nothing raised; only the
byte-size check caught it. The connector's own result must reach disk
unmodified — read it where the harness already wrote it (an oversized result is
saved to a file and the tool returns that path) rather than copying it.

**Verify decoded length against Drive's `fileSize` before keeping any digest.**
That check is the only thing standing between a truncated transfer and a
fingerprint that will be wrong forever.

The connector refuses files over **10 MB** outright, and in practice can fail
below that on large transfers. Its own error suggests the raw Drive API; that
is exactly the replacement path this skill forbids. An unfingerprintable source
is a blocker to report, not a reason to build one.

## PHASE 1b — Scope: jurisdiction and material class

Two scope decisions are settled. Apply them without re-litigating each run.

**Current production jurisdictions: `FED` and `ON`.**

**`05-quebec` is `DEFERRED_JURISDICTION`.** Quebec material is deferred for
future product expansion, not discarded. During inventory you **may** list,
fingerprint, classify, preserve metadata and identify exact duplicates. You
must **not** author Quebec rules or formulas, publish Quebec reference data or
knowledge, or change architecture to accommodate Quebec. A Quebec dependency
must never block unrelated FED/ON work — treat it as out of scope and carry on.

**The `00-engine` folder is `UNVERIFIED_CANDIDATE_INTERPRETATION`.** Its JSON
carries its own uncertainty and deprecation markers. See `content-routing.md`;
the short version is that it may accelerate mapping and suggest candidates, and
may never become authoritative because it is machine-readable.

Folder names are used **verbatim**. One currently carries a trailing space in
Drive. Do not normalize or rename it — matching is by folder ID anyway.

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

- the Google Drive connector is unauthorized or unreachable
- a source cannot be fingerprinted (bytes unavailable)
- a provision needs a formula operation the engine does not implement
- two authoritative sources govern the same semantic and the same effective
  scope and evidence does not settle the relationship
  (`AUTHORITY_REVIEW_REQUIRED` — see `source-policy.md`)
- provenance cannot be attached to a knowledge object that requires it
- publication would need an override
- the discovery step in PHASE 0 fails to import

Note what is **not** a stop condition: the absence of a filesystem mount. There
is no mount, none is required, and none should be built.

Reporting a blocker with its exact locator is a successful outcome. Publishing
a guess is not.

## What this skill will not do

- Open an engineering entry, change schema, or add a subsystem.
- Write to Google Drive, or modify, rename or delete anything in the library.
- Build a filesystem mount, sync, or custom Drive integration to replace the
  connector.
- Treat AI-generated JSON as authoritative because it is machine-readable.
- Use a PDF, a web lookup or a model call as runtime tax authority.
- Infer supersession from a filename, a size, a timestamp or an ordering.
- Author or publish Quebec knowledge while it is deferred.
- Publish anything a deterministic validator did not pass.
