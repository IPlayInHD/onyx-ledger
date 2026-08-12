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

## Open questions for the design step

* **§33 deletion cutoff** — an account past `DELETION_REQUESTED` cannot write,
  but the graph is a read. Existing admission conventions need checking before
  choosing REFUSED vs READ_ONLY; the answer should match how other read paths
  behave, not be invented here.
* **§40 MISSING_INPUT** — `RuleOutcome.required_assumption_codes` exists and
  `projection.py` already computes a required-vs-supplied difference. Whether
  that generalises to a deterministic missing-input state, or only covers
  projections, needs measuring before the node type is promised.
* **§19 evidence readiness** — `evidence_status` on a candidate has four values
  (`documented_verified`, `documented_unverified`, `user_attested`,
  `incomplete`). Whether `READY / PARTIAL / MISSING / NOT_REQUIRED / UNKNOWN`
  can be derived from those plus `rule_required_document` without inference is
  the question; if it cannot, the graph should surface the existing four rather
  than invent a fifth vocabulary.
