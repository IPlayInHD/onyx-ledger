# BATCH REPORT — <batch name>

Adapt the sections to the batch. Do not pad with fields that do not apply, and
do not drop the counts — a missing count reads as zero.

## Scope

BATCH =
SOURCE_LIBRARY =
ACCESS_MODE =            <!-- filesystem path | connector (no path) -->
REPO_HEAD =
CONTRACT_VERSIONS =      <!-- as discovered this run, not from memory -->

## Discovery

```
FILES DISCOVERED     =
DUPLICATES SKIPPED   =
NEW SOURCE VERSIONS  =

PDF                  =
CSV                  =
JSON                 =
YAML                 =
```

State the basis for each skip: same fingerprint as an already-registered
version. A skip justified by filename is not a skip, it is a gap.

## Produced

```
RULE DRAFTS          =
FORMULA DRAFTS       =
REFERENCE DATA       =
EVIDENCE REQUIREMENTS=
DEADLINES            =
CITATIONS            =
```

## Outcome

```
VALIDATED            =
PUBLISHED            =
REVIEW REQUIRED      =
AMBIGUOUS            =
BLOCKED              =
```

`VALIDATED` counts objects a deterministic validator passed. `PUBLISHED` counts
objects that additionally cleared review and publication. The two are never
assumed equal.

## Invariants

```
UNSOURCED PRODUCTION OBJECTS =
ARBITRARY EXECUTABLE LOGIC   =
RUNTIME PDF READS            =
RUNTIME WEB LOOKUPS          =
RUNTIME AI CALLS             =
```

**Expected last four: 0 / 0 / 0 / 0.**
`UNSOURCED PRODUCTION OBJECTS` is expected 0 as well — governed provenance is a
publication precondition, so a non-zero count there means something published
without a citation the pipeline should have demanded.

A non-zero value is not a metric that drifted — it means something reached
production outside the pipeline. Explain it in full or treat the batch as
failed.

## Provenance

- qualifying citations attached =
- non-qualifying sources cited alongside (recorded, not sole authority) =
- superseded editions cited (permitted; pinned edition used unchanged) =
- provenance resolution failures =

## Verification

- examples executed / passed =
- formula golden vectors executed / passed =
- boundary cases covered =
- worked examples from the source reproduced =

Exact Decimal comparison throughout. If any comparison used a tolerance, say
so and explain why — the default answer is that it should not have.

## Publication

- pack/release identity =
- versions published =
- versions superseded =
- publisher =
- approver (must differ from submitter) =

## Performance

- statements per batch =
- wall clock =
- per-object cost =

Statement count should be roughly flat in batch size. If it grew with the
number of objects, provenance or reference data is being resolved per object;
say so rather than accepting it.

## Blockers

One entry per blocked item, each with its exact source and locator. Use
`blocker-report.md` for the detail; list them here so the batch's shape is
visible at a glance.

| Item | Source (identifier + fingerprint prefix) | Locator | Reason |
|---|---|---|---|

## Escalations for a human

Decisions this run deliberately did not make — conflicting sources, ambiguous
provisions, competing editions with no declared supersession, mismatches
against a worked example. State the options and the evidence, not a
recommendation dressed as a finding.

## Coverage index

- index updated = YES / NO
- sources registered to date =
- provisions covered / known outstanding =

## Decision

Answer plainly:

- Did every published object come from a registered source with a qualifying
  citation and a precise locator? YES / NO
- Did anything publish that a deterministic validator did not pass? YES / NO
- Did AI approve or publish anything? Expected NO.

NEXT: <the next batch, or the escalation blocking it>

Then stop. Do not begin another batch, and do not open an engineering entry.
