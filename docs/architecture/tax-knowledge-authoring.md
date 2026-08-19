# GOVERNED TAX KNOWLEDGE AUTHORING + PUBLICATION

The governed answer to: *how does an authoritative source become a rule the
engine is allowed to run?*

```
tax_kb.tax_source_version        the pinned edition
    └── tax_kb.source_citation           edition + structured locator
            ↓
TaxKnowledgeDraftSpec            one aggregate, validated whole
    ↓  stage_draft
tax_kb.tax_rule_version (draft)  + conditions, outcomes, actions, evidence,
    ↓  validate                    dependencies, deadlines, examples
PublishabilityReport             closed codes, per-family readiness, a hash
    ↓  submit → approve            (four-eyes: reviewer ≠ submitter)
    ↓  publish                     binds to that hash, or refuses
tax_kb.tax_rule_version (published)
    ↓
RulesEvaluatorService / TaxEngineService
```

Internal operator tooling. No customer route reaches it, and the evaluator
never reads a table it introduced.

## 1. Why not a parallel draft model

The repository already stores a rule version and eight child tables, and the
TKMS lifecycle already moves one from `draft` to `published`. A separate draft
mirror would be a second representation of the same knowledge, evolving on its
own, and the day the two disagreed the engine would be reading whichever one
nobody had checked.

So the draft representation **is** the existing rule version in a non-published
status. What this entry adds is everything that must be true before it is
allowed to become published.

| Added | Why the existing schema could not carry it |
|---|---|
| `ref.deadline_type` | `rule_deadline.deadline_code` was free text with no constraint and no vocabulary, so "is this a known deadline code?" had no answer |
| `rules.rule_example`, `rules.formula_vector` | a production rule publishes with cases that were **run**; kept only in the manifest they would be unrunnable the moment publication finished |
| `tax_rule_version.supersedes_version_id` | `superseded_by_version_id` records what publication *did*; nothing recorded what an author *declared*, so a reviewer could not see what a correction corrected |
| `validation_report.spec_hash` + verdict | publication accepted any passed report *targeting* the version, which does not say the report describes its current content |
| `rule_publication.channel`, `pack_hash` | §52 forbids inferring production-ness from a code prefix or an environment name |
| `knowledge_citation.calc_constant_id` | the registry's exclusive arc covered rule versions, formulas, bracket sets, limits and benefit parameters — but not a calc constant, which the engine reads **directly** and can therefore inherit provenance from nothing |

## 2. One aggregate, validated whole

The alternative the repository offered was: insert a version, then its condition
groups, then conditions, outcomes, actions, documents, dependencies, deadlines —
each a separate partially-valid write. That makes *"is this rule coherent?"* a
question nobody can ask, because there is no moment at which the answer is
defined.

`TaxKnowledgeDraftSpec` defines it. Three properties carry the weight:

**Unknown fields are refused, at every level.** A field an author wrote and the
pipeline ignored is a silent disagreement about what the rule says.
`legal_authority_rank: 1` meant something to whoever typed it.

**Decimals parse from strings, never floats.** A JSON number is a float by the
time Python sees it, so a submitted `0.1` is already not one tenth. `float`,
`NaN` and `Infinity` are refused where they appear rather than coerced.

**Identity is semantic.** `spec_identity()` is `domain_hash(knowledge_spec, …)`
over what the knowledge *means* — not key order, not who submitted it, not when.
Sets of children (evidence, dependencies, deadlines) sort by content; condition
order does not, because the stored rows carry it and sorting would make two
different rules hash alike.

## 3. Validation fails closed, and resolves rather than invents

Two corrections to validation that already existed, both of which passed things
they should have refused:

```python
if ctx.known_facts and cond.fact_key not in ctx.known_facts:   # before
```

An empty reference set — a context that failed to load, a fresh database, a
caller that passed nothing — turned every reference-integrity check into a no-op
that reported success. A vocabulary the pipeline cannot see is a question it
cannot answer, and an unanswerable question is not a pass.

```python
for token in expression.split():                                # before
    if not _is_number(token) and token not in _RPN_OPS and token not in variables:
        variables[token] = Decimal(1)
```

Binding every unrecognized token to a placeholder made an **undeclared input**
valid. Tokens now resolve against declared inputs and the engine's own
`SUPPORTED_OPERATIONS` or they are errors — exported from the sandbox rather
than restated, because a second hardcoded list is a second source of truth
waiting to drift.

Ten vocabularies the DDL owns are mirrored in Python so validation stays a pure
function; a test reads `pg_get_constraintdef` and asserts the two agree.

The error vocabulary keeps the same promise in the other direction. A code
nothing can emit tells a reader a check exists when it does not, so a test
asserts every `ValidationCode` member is produced by a real check. Writing it
found five that were not: two were **missing checks** and are now implemented —
`RULE_CODE_CONFLICT` (a draft reusing a globally-unique rule code would silently
inherit the registered row's jurisdiction and category) and
`SUPERSESSION_TARGET_INVALID` (a correction naming a predecessor that is not the
version it would actually replace). Three were unreachable and were removed:
`ACTIVE_VERSION_OVERLAP` cannot occur while `uq_rule_version_published` stands,
and `UNKNOWN_FORMULA_OPERATION` / `ASSUMPTION_UNDECLARED` implied distinctions
the evaluator and the schema do not make.

## 4. The multiple-outcome defect: a production defect, fixed

A rule version with two outcomes crashed optimization on
`UNIQUE (portfolio_id, candidate_id)`. Reproduced on a clean database before
anything was decided:

```
OPPORTUNITIES_FROM_ONE_VERSION=2
DISTINCT_CANDIDATE_KEYS=1
RUN_STATUS=RAISED IntegrityError: duplicate key … portfolio_member_portfolio_id_candidate_id_key
PERSISTED_CANDIDATES_FOR_VERSION=0        ← the whole transaction rolled back
```

The measurement that settled the classification: replacing one `recommend` with
`apply_deduction` collided **identically**. So restricting recommend-cardinality
at publication would not have fixed it, and the schema plainly intends many
outcomes per version — `rule_outcome` has no uniqueness on
`(rule_version_id, outcome_type)`, carries a `priority` that only means
something when there is more than one, and the evaluator loops over them.

The defect was in identity. `opportunity_code` is the **rule's** code, so
`candidate_key = opportunity_code:rule_version_id` identifies a rule version
wearing an opportunity's name. Two opportunities collapsed to one key,
`key_to_id` kept only the last id, and both portfolio members pointed at it.

Fixed at the identity: the evaluator supplies an `outcome_discriminator`, and
`candidate_key` appends it **only when the rules layer supplied one**, which it
does only for a version emitting more than one opportunity. Every key sealed
before this is reproduced byte for byte, so scenarios, counterfactual
comparisons and historical graphs keep matching what they were sealed with. The
dict comprehension that swallowed the collision now raises `DuplicateCandidateKey`
where it happens.

After the fix, same fixture: `DISTINCT_CANDIDATE_KEYS=2`, run completed,
`portfolio_member_count=2`.

### The formula language, and what it does not have

The sandbox implements exactly six operations: `+ - * / min max`. The authoring
validator imports that set from the sandbox rather than restating it, so the two
cannot drift, and a test pins the membership.

**This is a recorded gap for the production content entry, not for this one.**
Real Canadian formulas will want `ROUND` above all, and probably a bracket
lookup and a reference-data lookup — §14 names all three as likely. None exists.
Adding one is a change to the calculation authority with its own determinism and
replay obligations; inventing it here, or working around it with free-form
expressions, is exactly what §7 forbids. Discovering it while loading content
would be worse than knowing it now.

## 5. Provenance is required, and *registered* is not *qualifying*

Every production rule version publishes with at least one governed citation.
A free-form URL is not provenance; neither is a citation the registry never saw.

The registry deliberately declines to rank sources, because legal precedence is
a tax opinion. The publication **policy** answers a narrower product question —
may this *kind* of source be the sole authority for something a user acts on? —
as a closed allow list. `SECONDARY_COMMENTARY` is absent: it may be registered,
cited and read alongside a statute, never alone.

| Situation | Outcome |
|---|---|
| No citation | `SOURCE_REQUIRED`, blocks |
| Citation not in the registry | `SOURCE_CITATION_UNKNOWN`, blocks |
| Only commentary | `SOURCE_NOT_PRODUCTION_QUALIFYING`, blocks |
| Commentary **beside** a statute | publishes; the non-qualifying source is still reported, as a warning |
| Pinned edition `WITHDRAWN` | `SOURCE_WITHDRAWN`, blocks — the publisher retracted it |
| Pinned edition superseded | publishes unchanged, reported as a warning |
| Pinned edition unresolvable | `SOURCE_INTEGRITY_UNAVAILABLE`, blocks |

A superseded edition is not a broken one. A rule authored in 2024 rests on the
words in force then; treating a later edition's existence as an integrity
failure would make every historical rule rot on a schedule. The pinned edition
is returned, never substituted.

## 6. Publication binds to what was validated

`publish()` does not trust a stored verdict. It rebuilds the specification from
the draft's **current rows**, hashes it, and refuses any mismatch:

```
SPEC_CHANGED_SINCE_VALIDATION: draft … now hashes to af84fae9ecb1…
but was validated as 7125a52e834e…; revalidate before publishing
```

Hashing the submitted document would prove only that the caller sent the same
bytes twice. Hashing what the database holds proves the knowledge that will
publish is the knowledge that was reviewed.

It then **revalidates against live state**, because between validation and
publication a cited source can be superseded, a reference-data dependency can
appear, or a competing version can be published — and a stored verdict cannot
know about any of it. Finally it requires a stored report for that exact hash
marked publishable. A pre-existing report has `NULL` there, and NULL is neither
true nor false: the question was never asked, so silence cannot approve.

There is no `--force`, no `ignore_errors`, no `skip_provenance`. A test asserts
the parameter names do not exist. An emergency override is a governance design
with its own audit trail; a boolean parameter is how one gets built by accident.

### Readiness has four states, not three

`READY`, `NOT_APPLICABLE` (the draft declares this family does not arise),
`MISSING` (declared, but something it names could not be resolved), `INVALID`
(present, but a hard check failed) — and `DEFERRED_TO_PUBLICATION` for
publication authority, which is real, is checked, and is simply not
validation's to answer. Reporting it `NOT_APPLICABLE` would assert the question
does not arise, which is the collapse §74 forbids.

## 7. Releases, and why there is no release table

Publication is atomic across a whole set. Reference data writes first — a rule
pinning a bracket table must not be readable for a moment before the table it
reads exists — then each rule version, all in one transaction. Nine valid rules
out of ten is not a partial success; it is a tax year that half exists.

A release needs an *identity*, and `pack_hash` is one: `domain_hash` over the
sorted member spec hashes, stamped on every `rule_publication` row and indexed.
"What published together, under which policy" is one query. A `KnowledgeRelease`
entity would have added a lifecycle, an owner and a status that nothing reads —
the ceremony §49 warns against.

Cross-member dependencies resolve **within** a candidate pack, so B may depend on
A before A publishes. Forcing A to publish so B could validate would mean
publishing knowledge in order to check knowledge, which is the opposite of a dry
run.

## 8. Future-effective publication, honestly scoped

`RulesEvaluatorService` resolves the current rule set by `(tax_year, status)`
and applies **no date predicate**. That was measured, not assumed.

So future-effective publication works at the granularity the resolver actually
has: a 2027 version publishes today and is invisible to every 2026 evaluation.
An **intra-year** effective date is refused —
`INTRA_YEAR_EFFECTIVE_SCOPE_UNSUPPORTED` — because such a version would take
effect the moment it published rather than on the date authored, which is
exactly what §36 forbids. Refused at the gate rather than published into a
resolver that cannot honour it.

Supporting intra-year effective scope means giving the evaluator an evaluation
date and a date predicate. That is a change to a certified authority and belongs
to an entry of its own; it is a **DESIGN limitation**, recorded rather than
worked around.

## 9. Immutability

Published rule versions are corrected by publishing a successor. The predecessor
moves to `superseded`, its `superseded_by_version_id` is set, and **nothing else
about it changes** — a test rebuilds its specification before and after and
asserts the same hash.

Published **reference data** is immutable by uniqueness: `tax_bracket_set`,
`contribution_limit` and `calc_constant` carry no version column, so the pipeline
INSERTs and refuses ground that is already covered
(`REFERENCE_DATA_ALREADY_PUBLISHED`). For brackets and limits that ground is the
semantic key. For `calc_constant` it is the semantic key **and an effective
period**: since
[reference-data effective periods](reference-data-effective-periods.md) a code
may hold several non-overlapping periods within one tax year, so republication
is detected by OVERLAP rather than by key equality — a second quarter of a
prescribed rate is new content, not a correction. Correcting published reference
data would still require a version dimension on tables the engine and the
snapshotter read; that is a redesign, and it is recorded as a limitation rather
than improvised.
Historical replay is unaffected either way, because `rule_snapshot_artifact`
materializes reference-data content with its own hash.

## 10. Security

| Role | Authoring pipeline |
|---|---|
| `onyx_app_rw` (customers) | **cannot author** — the path writes the source registry, which migration 64 revoked |
| `onyx_app_rw` | SELECT only on `rule_example` / `formula_vector`; the `rules` DEFAULT PRIVILEGES grant was taken back explicitly |
| `onyx_kb_admin` | SELECT + INSERT on the example tables |
| operator without `tkms.publish` | `Forbidden` |
| AI | never — see §11 |

Four-eyes is unchanged and reused: the submitter cannot approve, enforced by
`GovernanceService` and by a DB CHECK on `admin.rule_change_request`.

**No single role can complete the governed path, and that is recorded rather
than papered over.** `onyx_kb_admin` holds writes on `tax_kb` and `rules` — it
can author a draft — and holds nothing in `tkms` or `admin`, so it cannot record
a verdict or a publication. The pipeline therefore runs with owner authority
today, which is the narrowest operator keyhole available without inventing a
role model. §29 explicitly prefers a documented limitation to a fabricated
enterprise workflow. A test reads `information_schema.role_table_grants` and
asserts this exact shape, so it cannot change unnoticed.

**Recorded debt, measured not assumed:** `18_admin_grants.sql` grants
`onyx_app_rw` INSERT/UPDATE/DELETE on `tax_kb` and `rules`, so the customer
runtime role retains DB-level write access to rule tables generally. The
governed path is closed to it — the citation writes are not permitted — but the
legacy TKMS import path still runs under that role. Narrowing it would change
behaviour for existing certified paths and belongs to a security entry of its
own.

## 11. The AI boundary

| Question | Answer |
|---|---|
| AI may draft a candidate manifest | **YES**, in a future entry |
| AI may validate authoritatively | **NO** — validation is deterministic code |
| AI may approve | **NO** |
| AI may publish | **NO** |

The authoring contract is shaped so a future AI-assisted extraction tool can
produce a **draft manifest** and nothing else. There is no confidence threshold
anywhere in the publication path, no field a runtime would `eval`, and no route
from a document to an eligibility decision. No AI is implemented in this entry.

## 12. Measured

- **The §10 defect**: reproduced on a clean database and re-run after the fix.
  Also measured that it was never about `recommend` cardinality.
- **Batch validation does not N+1** — flat, not merely sublinear. Vocabularies
  load once and provenance resolves once per pack, so pack size does not enter
  the statement count:

  | rules | SQL statements | wall clock | per rule |
  |---|---|---|---|
  | 1 | 22 | 46 ms | 45.5 ms |
  | 10 | 22 | 19 ms | 1.9 ms |
  | 100 | 22 | 55 ms | 0.55 ms |
  | 500 | 22 | 219 ms | 0.44 ms |

  Staging and validating one rule end to end costs 94 ms; publishing one costs
  184 ms. The first version of the statement-count test watched an engine
  nothing ran on and would have reported a passing `0 == 0`, so the counter
  asserts it saw real traffic before comparing.
- **Determinism**: spec identity invariant under `PYTHONHASHSEED` 0/1/42, and
  invariant to key order, child order for set-like collections, and money scale.
- **Concurrency**: two competing versions for one rule and tax year, published
  simultaneously. Measured before it was believed, and BOTH succeeded — neither
  is a unique violation, because publication supersedes whatever is currently
  published, so the second silently retired the first operator's version moments
  after it went live. `uq_rule_version_published` kept the end state coherent
  and could not make the second publisher notice. Publication now locks the
  parent `tax_rule` row (in a deterministic order, so two packs cannot
  deadlock), the two serialize, and the loser is refused with a code rather than
  replaced without being told.
- **Schema drift 0** on a freshly migrated database.
- **No tax-result change**: the evaluator references no authoring table, and the
  closed-entry suites are unchanged.
