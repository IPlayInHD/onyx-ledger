# <ENTRY NAME>

CLOSED / PARTIAL / BLOCKED

> Adapt these sections to the entry. Drop the ones it does not touch — an
> empty "Performance" heading tells the reader less than its absence does.
> Every value below is discovered, never assumed.

## Starting state

branch =
starting SHA =
working tree =

## Final state

final SHA =
HEAD == upstream =
working tree =

## Objective

What the entry was asked to achieve, in its own terms, including what it
explicitly forbade.

## Architecture / authority decision

Which authority owns each decision involved, and why no new one was created.

## Implementation

What changed, and what deliberately did not.

## Persistence

tables =
columns =
migrations =
backfill =

State "none" explicitly where the entry forbade persistence.

## Compatibility

Historical versions preserved; consumers audited; golden corpus stability.

## Determinism

Canonicalization reused; hash domain; seed-variation evidence; ordering.

## Privacy

Classification reviewed; retention/deletion reconciled with replay needs.

## RLS / Security

Boundaries touched, or an explicit statement that none were.

## Historical-read invocation counts

(where relevant — e.g. engine runs, rules evaluations, SQL statements observed
during a historical read, with the counters shown to be non-vacuous)

## Performance

(where relevant — measured, with the shape the measurement establishes)

## Targeted tests

Suite, count, result.

## Full suite

count passed / skipped.

## Release gate

`release gate (--full): PASS | FAIL`, on which SHA, with the failing stage named
if it failed.

## Exact-SHA CI

run =
status =
conclusion =
blocking jobs individually enumerated = YES / NO

## Defects discovered

PRODUCTION_DEFECT =
DESIGN_DEFECT =
COMPATIBILITY_DEFECT =
PRIVACY_DEFECT =
SECURITY_DEFECT =
TEST_DEFECT =
GATE_DEFECT =
PERFORMANCE_DEFECT =
POLICY =

## Decision

CLOSED / PARTIAL / BLOCKED, justified by the evidence above rather than by
effort spent.

## NEXT

<next entry, when known>

Then STOP. Do not begin the next entry.
