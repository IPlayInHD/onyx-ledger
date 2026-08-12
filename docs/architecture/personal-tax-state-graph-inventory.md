# PERSONAL TAX STATE GRAPH — architecture inventory (Entry 12A, §8)

What already exists, what the graph may assemble from, and the two node types
that have **no authoritative source today**. Written before any schema, because
the instruction that shapes this entry is §4: the graph is *assembled state*,
never a second tax or rules engine — and the way that rule gets broken is by
designing a node type first and looking for its data afterwards.

## Authorities that stay authoritative

| concern | authority | the graph may |
|---|---|---|
| tax calculation | `TaxEngineService` | reference results |
| eligibility | `RulesEvaluatorService` | reference verdicts |
| scenarios | `ScenarioService` / replay | reference sealed results |
| portfolio assembly | `portfolio/service.py`, `domain/portfolio.py` | reference members, ledger, exclusions |

None of these may be reimplemented. Every number in the graph must arrive
already computed by one of them.

## Contracts to reuse, not rebuild

The repository already has the infrastructure §25–§28 asks for:

* **Canonicalization** — `app/services/ioe/domain/canonical.py`:
  `canonicalize`, `dumps`, `canonical_text`, `canonical_hash`, `ordered`,
  `normalize_text`, and the typed decimal helpers `money` / `rate` / `factor` /
  `quantity`. **Domain-separated hashing already exists** as
  `domain_tag(domain)` / `domain_hash(domain, payload)`, which is exactly the
  shape `graph_hash` needs. A second serializer would be a defect, not a
  feature.
* **Freshness** — `domain/freshness.py`: `PinnedState`, `FreshnessVerdict`
  (`status`, `stale_reason`, `changed_fields`, `policy_version`),
  `evaluate_freshness`, and an ordered `_FRESHNESS_CHECKS` table that reports
  the *first* meaningful mismatch rather than ten symptoms.
* **Integrity** — `domain/integrity.py`: `IntegrityStatus`
  (`not_checked` / `verified` / `mismatch` / `unavailable`) and
  `IntegrityReason`. Kept separate from freshness, which §21 requires.
* **Support** — `domain/confidence.py`: `raw_support_score`,
  `assumption_adjusted_score`, `display_support_score`, `cap_applied`,
  `cap_reason_code`. §15's semantics are already implemented; the graph carries
  them through unchanged.
* **Assumptions** — `domain/assumptions.py`: a typed registry with value types
  and materiality. No free-form expressions.
* **Batched reads** — `IoeReadRepository` already loads per entity family
  (candidates, members, exclusions, ledger, projections), which is the shape
  §24 demands.

## Node taxonomy vs. available sources

| node type | authoritative source | verdict |
|---|---|---|
| `FACT` | `finance.income_source`, `finance.expense_record`, `profile.*`, `wealth.asset`/`liability`, confirmed `docs` extractions | **available** |
| `TAX_STATE` | `analysis.analysis_run` + `analysis.analysis_line_item` (TaxEngineService output) | **available** |
| `OPPORTUNITY` | `ioe.optimization_candidate` — carries `eligibility_status`, `evidence_status`, `calculation_basis`, `standalone_potential`, `incremental_portfolio_benefit`; plus `reco.recommendation` | **available** |
| `DEADLINE` | `tax_kb.rule_deadline` (`deadline_code`, `deadline_date`) | **available** |
| `EVIDENCE` | `docs.document`, `docs.document_extraction`, `tax_kb.rule_required_document`, candidate `evidence_status` | **available** |
| `RESOURCE` | `ioe.resource_ledger_entry`, `tax_kb.rule_shared_resource` | **available** |
| `ASSUMPTION` | `ioe.scenario_assumption`, `domain/assumptions.py`, `RuleOutcome.required_assumption_codes` | **available** |
| `SCENARIO` | `ioe.scenario` + `ioe.scenario_result` | **available** |
| `OBLIGATION` | — | **NO SOURCE** |
| `DECISION` | — | **NO SOURCE** |

### OBLIGATION has no authoritative source

`rules.rule_outcome.outcome_type` is constrained by CHECK to exactly four
values:

```
recommend · apply_credit · apply_deduction · flag_benefit_eligibility
```

There is no filing obligation, no payment obligation, no instalment
obligation, and no required-action vocabulary anywhere in `tax_kb`. Building
`OBLIGATION` nodes would mean deciding what Canadian tax law obliges this user
to do — which §11 forbids in the plainest terms available ("Do not invent tax
law") and which this repository has no authority to do.

**Recommendation:** define `OBLIGATION` in the taxonomy as a typed extension
point with **zero producers** in 12A, so the future rules work that adds an
obligation outcome type has a place to land. Do not emit one node.

### DECISION has no Decision Journal

No `decision_journal` table, model or service exists — confirmed by search. The
nearest real data is `reco.recommendation.status`
(`generated / viewed / accepted / rejected / completed`) and
`reco.recommendation_status_event`.

That is *recommendation interaction state*, not a decision journal, and 11B6I
classified both as `LIVE_USER_DATA_DELETE` — they do not survive account
deletion. Treating them as durable decision history would misrepresent both
their meaning and their lifecycle.

**Recommendation:** design compatibility only, per §11. No `DECISION` nodes in
12A.

## Edge taxonomy vs. available semantics

Edges with a real origin today:

```
SUPPORTED_BY        candidate.evidence_status, rule_required_document → docs
DERIVED_FROM        analysis_line_item → analysis_input_snapshot
ELIGIBLE_BECAUSE    tax_rule_version.eligibility_basis_codes
INELIGIBLE_BECAUSE  candidate.eligibility_status + exclusion.reason_code
DEPENDS_ON          tax_kb.rule_dependency
REQUIRES            rule_required_document
CONSTRAINED_BY      portfolio_exclusion.shared_resource_code
CONSUMES_RESOURCE   portfolio_member.resource_allocations, resource_ledger_entry
CONFLICTS_WITH      ioe.recommendation_relationship
EXPIRES_AT          rule_deadline
ASSUMES             scenario_assumption, required_assumption_codes
REFERENCES_SCENARIO ioe.scenario
REFERENCES_RULE     tax_rule_version, legislation_reference, gov_source
```

`SUPERSEDES` exists in the data (`optimization_run.superseded_by_run_id`) but
belongs to run lineage rather than the state model; including it is a judgement
call for the design step, not a given.

**§48 warning taken seriously.** `ioe.recommendation_relationship` is the
pairwise relationship table, and the optimization work already hit O(n²) growth
there. `CONFLICTS_WITH` must not be emitted as an all-pairs edge set; only
relationships the product actually explains belong in the graph, and edge count
must be measured against node count before closure.

## Persistence — the §44 decision, argued

**Recommendation: assembled read model for current state; historical graphs
derived from the sealed artifacts that already exist. No new tables, no
migration.**

Three reasons, the third being decisive:

1. **The data already exists.** Every node above is a projection of a row that
   is already stored and already governed. §8 says not to duplicate structures
   that already express the concept.
2. **Historical graphs are already frozen.** `analysis_input_snapshot` is the
   frozen input, and the sealed roots carry their own hashes and version
   manifests. A historical graph is a deterministic function of artifacts that
   cannot change — so storing it would store a derivable value.
3. **Persisting graphs would reopen the privacy programme that just closed.** A
   `tax_state_graph` table holding a person's income, opportunities and
   evidence would be a new personal-data surface. It would need a registry
   classification, a purge keyhole, a completion guard, a write cutoff, and a
   place in the five-phase lifecycle — the privacy universe was certified at
   **70 tables with zero engineering blockers**, and this would take it to 71
   with one. That cost is real and should only be paid for a capability the
   read model cannot provide.

If a later entry needs a persisted graph artifact (for example to diff two
points in time that are not both reconstructable), it can be added then, with
the lifecycle work priced in rather than discovered.

## The three open questions, resolved

Each was answered by reading the convention that already exists rather than
choosing one.

### §33 deletion cutoff — REFUSED, and it needs no new code

`db_authed` in `app/api/deps.py` is described in its own docstring as "THE
central lifecycle boundary", and it applies `assert_may_act` to every protected
route — **reads included**. `_BLOCKING_STATES` is `frozenset(LifecycleState)`,
i.e. every state, so a lifecycle row in any state refuses the request with
`AccountDeletionInProgress` (403).

The only exemption in the repository is `db_authed_lifecycle_exempt`, and it is
reserved for the deletion endpoints themselves — requesting deletion again and
reading its status. Granting the graph a second exemption would be inventing a
policy; using `db_authed` inherits the existing one.

Reads are not incidentally covered, they are *deliberately* covered. The
`assert_account_active` docstring names reading a scenario as a route that
needed the cutoff added, because that read persists the freshness transition it
just evaluated. The graph assembles from freshness and scenario state, so it is
the same shape.

**Decision: the graph endpoint takes `db_authed` like every other protected
route, and an account past its cutoff is REFUSED with 403.**

### §40 MISSING_INPUT — projection-scoped only

`required_assumption_codes` reads as a general facility and is not one. Its
column comment scopes it explicitly — "Assumption codes that must be present and
satisfied before a **projection** may be generated" — and the only consumer is
`ProjectionAuthorization`, whose sole reader is `projection.authorize()`. There
it produces a genuinely deterministic result: `missing = required - available`,
returned as `NOT_GENERATED_MISSING_ASSUMPTIONS` with the codes attached.

It does **not** generalise, and the evaluator shows why. A rule condition on a
fact the user has not supplied simply evaluates falsy, and
`rules_service._load_impacts` coerces an absent fact to `Decimal(0)`:

```python
elif fi.literal_value is not None:
    variables[fi.param_name] = Decimal(str(fi.literal_value))
else:
    variables[fi.param_name] = Decimal(0)
```

So the tax engine does not distinguish *ineligible because the law says no* from
*ineligible because we do not know*. Deriving a general `MISSING_INPUT` would
mean the graph deciding which facts a rule needed — inference, and precisely the
second rules engine §4 forbids.

**Decision: `MISSING_INPUT` is emitted for projections only, carrying
`ProjectionDecision.missing_assumption_codes` verbatim. It is not emitted for
facts, tax state or opportunities.**

### §19 evidence readiness — derivable, but on a second axis

The join is exact, not approximate. `rules.rule_required_document.
document_type_code` references `ref.document_type(code)` and
`docs.document.document_type_id` references `ref.document_type(id)` — the same
table, so required-versus-held is a set difference over a shared vocabulary with
no mapping layer to get wrong.

`necessity` is constrained to `required / recommended / conditional`, which
supplies the readiness states honestly:

```
no required rows for the rule version   NOT_REQUIRED
every required type held                READY
some held                               PARTIAL
none held                               MISSING
necessity = 'conditional'               UNKNOWN
```

"Held" means a `docs.document` of that type with `deleted_at IS NULL` and
`status = 'processed'` — the only status in the CHECK vocabulary meaning
successful ingestion; `quarantined` and `failed` are not evidence and
`uploaded` / `processing` are not yet.

`UNKNOWN` is not a hedge. Whether a `conditional` document applies is carried
only in a free-text `note`, so asserting MISSING for one would require reading
prose and deciding — inference again. UNKNOWN is the truthful state.

**But readiness must not be folded into `evidence_status`.** They answer
different questions: `evidence_status` describes how well the inputs to a
computed figure are supported, while readiness describes whether the documents
the rule demands are held. The repository already refuses exactly this kind of
collapse one level down — `EvidenceStatus` is documented as "Never collapsed
into CalculationBasis: a precisely calculated figure over unverified data must
show both facts."

**Decision: `EVIDENCE` nodes carry both — `evidence_status` verbatim from the
candidate, and a separate derived `readiness` from required-document coverage.
Neither is derived from the other.**
