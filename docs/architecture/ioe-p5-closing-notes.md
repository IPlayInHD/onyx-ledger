# IOE P5 Closing Note — how `baseline_result` is represented

Required before the API is exposed: state whether the pinned baseline result is
a full immutable snapshot, a foreign key plus hash, or both.

## Decision

**Neither a full snapshot nor a bare foreign key. The scenario stores a
*derived* pin: one monetary total plus a content hash of the inputs that
produced it.**

Concretely, `ioe.scenario` holds:

| Column | What it is | Why |
|---|---|---|
| `base_analysis_id` | FK → `analysis.analysis_run` | *which* baseline, so the scenario is attributable and comparisons can require the same one |
| `baseline_input_snapshot_hash` | content hash of the frozen analysis input snapshot | proves the inputs have not moved, without copying them |
| `baseline_tax` | `NUMERIC(14,2)` — the single figure `total_payable` | the one number the delta is measured from |
| `baseline_result_hash` | hash over `{baseline_tax, engine_version, reference_data_version}` | proves the *result* has not moved, including when only the engine changed |

`baseline_result_hash` deliberately covers the engine and reference-data
versions as well as the amount. The same inputs run through a different engine
version are a different baseline even when the total coincides, and a pin that
did not notice that would report `current` for a scenario whose foundation had
in fact shifted.

## Why not a full immutable snapshot

A full baseline-result snapshot on every scenario would copy the user's
computed tax position — income totals, net income, taxable income, per-bracket
tax, credits — into a second table, once per scenario. That is duplication of
sensitive financial data with no capability gained:

- **Attribution** is already served by `base_analysis_id`.
- **Tamper evidence** is already served by the two hashes; a full copy would not
  be *more* tamper-evident, only larger.
- **Reproduction** is already served by the pinned inputs plus the pinned engine
  version. `analysis.analysis_input_snapshot` is itself immutable, so the
  baseline can be recomputed exactly rather than remembered.

Every additional copy of a user's financial position is another row to
tenant-isolate, another row to encrypt, another row to honour in an erasure
request, and another place for a stale copy to diverge from the record. The
project's stated rule — *do not duplicate raw financial inputs into scenario
traces* — applies with equal force to duplicating derived financial results.

## Why not a bare foreign key

A foreign key alone says *which* analysis, not *what it said*. Analyses are
immutable, but the engine and reference data that turn inputs into a number are
not: a rate table correction or an engine fix changes what that same analysis
would produce today. With only an FK, a scenario could not distinguish "still
true" from "the ground moved", and freshness could not be evaluated at all.

## What this buys the freshness evaluator

Both `BASELINE_INPUTS_CHANGED` and `BASELINE_RESULT_CHANGED` are detectable, and
they are detectable *separately*:

- inputs moved → `baseline_input_snapshot_hash` differs
- inputs identical but the engine or reference data moved →
  `baseline_result_hash` differs while the input hash matches

The evaluator reports the more fundamental of the two first (§P4 verification
record, item 1 ordering rule), so a user is told the root cause rather than a
list of symptoms.

## Consequence for the API

A scenario response may state the baseline it was measured against
(`baseline_tax`, with its tax year, currency, calculation basis and freshness),
and may link to `base_analysis_id`. It must **not** attempt to reconstruct the
baseline's internal composition from scenario tables — that composition lives in
the analysis snapshot, is read under its own ownership checks, and is not copied
here.
