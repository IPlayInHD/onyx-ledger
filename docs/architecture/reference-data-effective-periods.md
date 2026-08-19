# Reference-data effective periods, and governed constants at runtime

Entry: **Consolidated P0 reference-data integration.**

Two gaps, both demonstrated by real ingestion rather than anticipated. The P0
corpus was ingested batch by batch; six batches published cleanly and the
seventh could not be represented at all. That refusal, and a measurement taken
while closing it, are what this entry is.

## Gap A — a quarter is not a year

CRA announces prescribed interest rates per calendar quarter. The B04 source
says so itself: *"These rates will be in effect from July 1, 2026 to September
30, 2026."* `rules.calc_constant` identified a value by `(code, tax_year)`, so
four quarters of one year collided on one key.

Three ways out were available without a schema change and all three are wrong:

- **Encode the quarter into the code** (`PRESCRIBED_RATE_OVERDUE_2026Q3`). Puts
  data in the key, makes every code single-use, and leaves "what was the rate in
  August" unanswerable.
- **Publish one quarter as the annual value.** False for nine months of the year.
- **Drop the source.** The product needs prescribed rates.

So the period is stored as what it is. `effective_from` and `effective_to` are
nullable dates; both NULL means the whole tax year, which is what every annual
parameter is and what every pre-existing row stays.

### The constraint says the actual rule

```sql
CONSTRAINT ex_calc_constant_no_overlap EXCLUDE USING gist (
    code WITH =, tax_year WITH =,
    daterange(effective_from, effective_to, '[]') WITH &&)
```

This **replaces** `UNIQUE (code, tax_year)` and is strictly stronger:

- Two annual rows both range over `(-infinity, infinity)` and overlap, so the
  old guarantee survives intact.
- Two overlapping quarters are refused. `UNIQUE (code, tax_year,
  effective_from)` would have permitted them merely for starting on different
  days — the contradiction most worth preventing, and the easiest to introduce
  without noticing.
- An annual row beside a period row for the same code and year is refused.
  "The 2026 value is X" and "the Q3 2026 value is Y" are not two facts that
  coexist; one of them is wrong.
- Adjacent quarters — one ending 09-30, the next starting 10-01 — do not
  overlap. Both bounds are inclusive.

`btree_gist` is required for the scalar `=` operators inside a GiST exclusion.
It is trusted, so it needs no superuser.

`app.services.tax_kb.authoring.validation.periods_overlap` mirrors this in
Python, so the validator refuses with a coded finding what the database would
otherwise refuse with an `IntegrityError`.

### Rule versions still refuse intra-year scope

`INTRA_YEAR_EFFECTIVE_SCOPE_UNSUPPORTED` still applies to a **rule version**,
and that is not an inconsistency with the above. A rule version is selected by
the evaluator on `(tax_year, status)` with no date predicate, so a mid-year rule
would silently take effect on publication rather than on the date authored.
Reference data is resolved through `TaxDataProvider`, which takes the effective
date and applies it. The period a constant declares is the period it gets;
the period a rule declared would not be.

### Two compatibility obligations, verified rather than intended

**Annual spec hashes must not move.** `ValidationReport.spec_hash` is what
publication rebinds against, so changing the canonical form of an annual object
would invalidate every validation already recorded. The period keys are
therefore emitted into `as_canonical()` **only when set** — absent and null mean
the same thing, so absent is the canonical form. All 51 constants staged by the
completed ingestion batches still hash to their recorded values.

**Annual snapshot artifacts must stay byte-identical.**
`SnapshotService.verify()` recomputes artifacts and compares by
`(artifact_kind, artifact_key)`. Widening the key for annual rows would report
every historical snapshot `unavailable`; adding two nulls to the content would
report every one of them `drifted`. Both are pure false positives across all
history. So the annual key stays `code:tax_year` and only a row that carries a
period extends it to `code:tax_year:from..to` — which it must, or two quarters
would collide in the pinned set and one would silently overwrite the other.

## Gap B — governed constants nobody read

`TaxDataProvider` consumed `TAX_BRACKET_SET` and nothing else, so 51 published
constants carried provenance that reached no calculation.

They were classified against actual runtime consumers rather than mapped
wholesale:

| | count | disposition |
|---|---|---|
| **A** direct existing runtime counterpart | 8 codes | wired |
| **B** rule or formula input | 8 codes | left governed |
| **C** no runtime consumer | 9 codes | left governed |
| **D** semantic engine gap | 2 codes | wired (see CPP2) |

Category B is GST/HST, meal and vehicle rates, and automobile allowance rates —
expense and sales-tax semantics that belong to rules and guidance. Category C is
the registered-plan limits plus three figures the engine **derives** from the
ceiling, exemption and rate; storing those would create a second authority for a
number already computed, and the two could disagree.

Inventing a `TaxEngineService` field per governed row to make a count look
complete would put each tax figure in a second place. `FEDERAL_CONSTANT_FIELDS`
in `provider.py` is the whole map, and it is short on purpose.

Each mapping declares the unit its field is in, and a mismatch **raises**. A
rate published as `CAD` is a premium amount wearing a rate's name; multiplying
insurable earnings by it would produce a confident, enormous, wrong number. A
governed row that exists but cannot be used is never quietly skipped.

A code with no published row keeps its bootstrap value and is simply absent from
`TaxDataset.governed_constants`. That is the same visible pre-publication state
the bracket path already reports, not a live-data fallback.

## The CPP2 production defect

The targeted CPP2 ingestion made this measurable. `TaxResult.cpp_payable_se` is
labelled "CPP payable (self-employment)" and the engine scopes it as
"self-employed CPP (Schedule 8)". Schedule 8 computes base CPP **and** CPP2 for
a self-employed filer. The engine computed only the base.

It is not cosmetic: `total_payable = income_tax + cpp_payable_se`, and
`total_payable` is the quantity every IOE comparison is built on — the frozen
baseline, each scenario, and the value the portfolio optimises over.

Measured against the registered CRA source, 2025:

| self-employment income | was | published |
|---|---|---|
| 60,000 | 6,723.50 | 6,723.50 (below the ceiling — correct) |
| 75,000 | 8,068.20 | 8,364.20 |
| 90,000+ | 8,068.20 | 8,860.20 |

The existing golden case used 60,000, below the year's maximum pensionable
earnings, which is why the defect survived the suite.

`compute` now walks two bands, both incremental to employment income because
contributions withheld on a salary are not owed again on the return. The
deduction/credit split for the new component follows the engine's existing
self-employed convention. CPP2 sits in the *enhanced* portion, whose real
treatment is deduction rather than credit — but no registered source states
that, and asserting it here would make the engine the authority for a rule
nothing published. **Deferred, and recorded as such.** What is fixed is the
amount owed, which was wrong.

`REFERENCE_DATA_VERSION` moves to `2025.2.0`. That is the constant's stated
purpose: a run sealed under `2025.1.0` now reports DRIFTED on verification
instead of quietly replaying to a different number.

## Determinism

`dataset_from_sealed` short-circuits to the in-code bootstrap only when a run
governed **neither** jurisdictions **nor** constants. Testing jurisdictions
alone — which was the previous condition — would hand a constants-only run
today's constants instead of the ones it was sealed with, and a constants-only
run is exactly what the first published constants create.

Tamper-evidence sits in two places, and the distinction is worth being exact
about. An edited *value* is caught by the round-trip proof: the sealed
description carries canonical scale, so a hand-edited `0.0700` re-describes as
`0.070000` and no longer equals the bytes it claims to be. An artifact edited to
claim it governed *nothing* does reconstruct, because replaying a bootstrap run
from in-code constants is what lets pre-governance runs replay at all — that
case is caught by the artifact's stored content hash and reported as drift by
`verify()`. Tamper-evidence for relabelling lives in the seal, not in the reader.

## One resolution per run

Closing Gap A added one statement to `TaxDataProvider.resolve`. Measuring it
showed the run resolving the dataset **twice** — once for the frozen baseline,
once for the run itself — so one added query cost two statements.

The baseline now receives the run's dataset instead of resolving its own. That
holds the measured budget at its previous figure while adding governed
constants, and it makes "one run, one dataset" structural: a baseline and the
candidates it is the comparison point for can no longer come from different tax
law.
