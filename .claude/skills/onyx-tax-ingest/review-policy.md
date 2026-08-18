# Review policy — what a human must look at

Deterministic validation and human review answer different questions.

- **Validation** asks: is this well-formed, resolvable, internally consistent,
  and does it match its own examples? Machines answer this, and their answer is
  binding — a failed validator is not overridable.
- **Review** asks: does this mean what the law says? No machine answers this,
  and no amount of green validation substitutes for it.

Review burden scales with **interpretive distance**: how far the published
object sits from something a reader can check against the source by eye.

## Tier 1 — Mechanical: lower burden after deterministic validation

Values transcribed from an authoritative table, where the published object and
the source say the same thing in the same shape.

Typical: bracket tables, contribution limits, indexed parameters, prescribed
rates, rate schedules.

Why lighter: validation already proves the shape is right — thresholds strictly
ordered, intervals contiguous from zero, a terminal open-ended bracket, rates
in range, no duplicate semantic key, Decimal throughout. What remains is
transcription, and a reviewer can confirm it by looking at one table.

Still required: a qualifying citation with a precise locator, and a
**spot-check against the source table** — at minimum the first row, the last
row, and any boundary the engine will compare against.

Never skip review entirely. "Mechanical" describes the checking effort, not its
necessity.

## Tier 2 — Interpretive: strong review, always

Anything where a human decided what the law *means*.

Typical: eligibility conditions, exclusions, dependencies between rules,
outcome semantics, impact formulas, evidence requirements, deadline semantics,
declared assumptions.

Why heavier: validation can prove a condition is well-formed and references a
known fact. It cannot prove the condition is the *right* condition. A rule that
excludes everybody and a rule that excludes the right people validate
identically.

Required:

- a qualifying citation per interpretive element, precise enough that a
  reviewer can read the provision and the condition side by side
- **positive and negative examples** that actually execute — a rule with only
  positive cases has never been shown to exclude anyone, and the gate is half
  the rule
- **exact-Decimal golden vectors** for formulas, including boundary values.
  Never a tolerance: a tolerance is how a rounding defect ships
- a stated reason for every choice a reader might make differently

## Always escalate

Stop the item and put it in front of a human, regardless of tier:

- **Conflicting authoritative sources** — two qualifying sources disagree and
  neither clearly supersedes the other. Where they govern the same semantic
  over the same effective scope, this is the `AUTHORITY_REVIEW_REQUIRED` state
  defined in `source-policy.md`: report both sides symmetrically with Drive
  file ID, SHA-256, locator and publication info, withhold the affected
  semantic, and continue everything independent of it. Reach it by
  **investigating the documents**, not by asking an operator to choose between
  filenames.
- **Out-of-scope jurisdiction reached** — a FED/ON item turns out to depend on
  Quebec material, which is `DEFERRED_JURISDICTION`. Record the dependency and
  continue; do not author Quebec knowledge to unblock it.
- **Ambiguous provision** — the text supports more than one reading.
- **Unstated inference** — the published object asserts something the source
  does not say. This is the most dangerous category because it validates
  cleanly; the inference is invisible to every machine check.
- **Unsupported engine semantic** — the provision needs an operation or shape
  the engine does not have. Blocker report; do not approximate.
- **Source-version uncertainty** — several editions with different bytes and no
  declared supersession. Which edition is authoritative is an operator
  decision.
- **Mismatch with an authoritative worked example** — the source publishes a
  worked calculation and the implementation disagrees. The implementation is
  wrong until proven otherwise, and "the example is unusual" is a hypothesis
  that needs testing, not an explanation.

Escalation is a successful outcome. It costs a review cycle; publishing a wrong
reading costs a wrong number in somebody's tax position.

## Who may publish

The pipeline enforces this; do not attempt to route around it.

| Actor | May |
|---|---|
| AI (including you) | draft, extract, propose, run validators |
| Author/operator | stage drafts, record verdicts, submit for review |
| A **different** authorized operator | approve |
| Holder of the publish permission | publish |

Four-eyes is enforced by both the governance service and a database constraint:
**the submitter cannot approve their own version.** Ordinary customer runtime
roles cannot author or publish at all.

**AI may never approve or publish.** Not with high confidence, not with a
passing validator, not because the batch is large and review is slow. There is
no confidence threshold anywhere in this path and no override flag; wanting one
means you have found a blocker.

## Review evidence to carry into the report

For each published object, the report should make these answerable without
opening the database:

- which source version and which locator it rests on
- whether the citation qualifies as production authority, and if a
  non-qualifying source was cited alongside, that it was recorded as such
- whether the cited edition is superseded (permitted, but a reviewer should
  know)
- which examples ran and that they passed
- who approved it, and that the approver is not the submitter

## What review does not cover

Review does not re-derive the tax law from first principles, and it is not a
second extraction pass. It checks that a published object says what its cited
provision says. If a reviewer needs to consult a source that is not cited, the
citation is wrong — fix the provenance rather than widening the review.
