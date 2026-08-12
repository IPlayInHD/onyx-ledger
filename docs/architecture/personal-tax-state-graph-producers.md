# PERSONAL TAX STATE GRAPH — producer matrix and domain contract (Entry 12A)

The rule this document exists to enforce: **every live node type has exactly one
authoritative producer, and a node type without one does not get emitted.** The
matrix is written before the assembler so that a node type cannot acquire a
producer by being convenient to build.

Eight live types. Two reserved with zero producers.

## 1. Producer matrix

| # | node type | authoritative source / service | source identifier | provenance | freshness source | evidence / support | historical or current | emitted today |
|---|---|---|---|---|---|---|---|---|
| 1 | `FACT` | the tenant's own declarations — `finance.income_source`, `finance.expense_record`, `wealth.asset`, `wealth.liability`, `profile.tax_profile` | `(table, id, tax_year)`; historical: JSON pointer into the frozen snapshot | `USER_DECLARED`, or `DOCUMENT_EXTRACTED` when a document backs the row | **not tracked** — these tables have no freshness column | `verification_status`; backing `document_id` / `receipt_document_id` | both — current from live rows, historical from `analysis.analysis_input_snapshot.snapshot` | **yes** |
| 2 | `TAX_STATE` | `TaxEngineService`, sealed as `analysis.analysis_run` + `analysis.analysis_line_item` | `analysis_run.id`, `analysis_line_item.id` | `ENGINE_COMPUTED` | **not tracked** — a completed run is immutable | `analysis_run.confidence_score`, `data_verified` | both | **yes** |
| 3 | `OPPORTUNITY` | `RulesEvaluatorService` → IOE, sealed as `ioe.optimization_candidate` (presented as `reco.recommendation`) | `optimization_candidate.id` | `ENGINE_COMPUTED` | parent `ioe.optimization_run.freshness_status` + `stale_reason_codes` + `evaluated_at` | `eligibility_status`, `calculation_basis`, `evidence_status`, the five-stage support scores, `support_cap_applied` / `support_cap_reason_code` | both | **yes** |
| 4 | `DEADLINE` | governed rule data — `rules.rule_deadline`, reached through the rule versions the run pinned | `rule_deadline.id` | `RULE_DATA` | inherits the run's pinned `rule_snapshot_id` | `is_hard`, `jurisdiction_code` | both | **yes** |
| 5 | `EVIDENCE` | governed `rules.rule_required_document` resolved against held `docs.document`; and the held documents themselves | requirement: `(rule_version_id, document_type_code)`; document: `document.id` | `DERIVED_DETERMINISTIC` (requirement), `DOCUMENT_EXTRACTED` (held document) | not tracked | `necessity`, derived `readiness`, `document_type_code` | current | **yes** |
| 6 | `RESOURCE` | IOE portfolio assembly, sealed as `ioe.resource_ledger_entry` (reached through `ioe.strategy_portfolio`) | `resource_ledger_entry.id` | `ENGINE_COMPUTED` over `RULE_DATA` | inherits the run | `capacity`, `allocated`, `remaining`, `pool_scope` | both | **yes** |
| 7 | `ASSUMPTION` | the run's sealed `ioe.optimization_run.assumption_set`, and `ioe.scenario_assumption` for scenarios | `(run_id, code)` / `scenario_assumption.id` | `ASSUMPTION_DECLARED` | inherits its parent | `certainty`, `materiality`, `affects_eligibility`, `source` | both | **yes** |
| 8 | `SCENARIO` | `ScenarioService` / replay, sealed as `ioe.scenario` + `ioe.scenario_result` | `scenario.id` | `ENGINE_COMPUTED` | `scenario.freshness_status` + `stale_reason_code` + `freshness_evaluated_at` | `integrity_status`, `integrity_reason_code`, scenario confidence components | both | **yes** |
| — | `OBLIGATION` | **none** | — | — | — | — | — | **no — reserved** |
| — | `DECISION` | **none** | — | — | — | — | — | **no — reserved** |

### Why the two reserved rows stay empty

`rules.rule_outcome.outcome_type` is constrained by CHECK to `recommend`,
`apply_credit`, `apply_deduction`, `flag_benefit_eligibility`. There is no
filing, payment or instalment vocabulary in `tax_kb`, so an `OBLIGATION` node
would be the graph deciding what tax law obliges a person to do. No Decision
Journal table, model or service exists either; `reco.recommendation.status` is
recommendation interaction state that 11B6I classified `LIVE_USER_DATA_DELETE`,
and it does not survive account deletion.

Both remain in the taxonomy so a future governed authority has somewhere to
land. `test_reserved_node_types_have_no_producer` asserts the producer registry
holds no entry for either, and
`test_assembly_emits_no_reserved_nodes` asserts assembled graphs contain zero of
each. Adding a producer has to be a deliberate act with a governed source
behind it, not a line in an unrelated diff.

## 2. Provenance model

A closed vocabulary. Every node carries exactly one.

```
USER_DECLARED         the person entered it
DOCUMENT_EXTRACTED    read from a processed document extraction
RULE_DATA             governed TKMS rule data, four-eyes authored
ENGINE_COMPUTED       produced by a tax / rules / scenario / optimization service
ASSUMPTION_DECLARED   a declared assumption with a registered code
DERIVED_DETERMINISTIC a deterministic join the graph performs itself
```

`DERIVED_DETERMINISTIC` is deliberately narrow: it is permitted **only** for
evidence readiness, which is a set difference over one shared vocabulary. It is
not a licence for the graph to compute tax, eligibility, or totals. Any new use
of it is a design review, because it is the category through which a second
rules engine would arrive.

## 3. Stable identity

```
node_key = "{node_type}:{source_kind}:{source_identifier}"
```

`source_kind` names the table or artifact, so two node types can never collide
and a key says where to go and look. Keys are stable across assemblies because
every component is either a primary key or a governed code — nothing derives
from row order, iteration order, or assembly time.

`EVIDENCE` keys use `(rule_version_id, document_type_code)` rather than a
document id, because a requirement exists whether or not a document satisfies
it. That is what lets `MISSING` be a state rather than an absence.

## 4. Canonical hash input

`graph_hash = domain_hash(DOMAIN_TAX_STATE_GRAPH, payload)`, using the existing
`app/services/ioe/domain/canonical.py`. `DOMAIN_TAX_STATE_GRAPH` is added to
`ALL_HASH_DOMAINS` — `domain_hash` rejects unregistered domains, so registering
is how the existing system is reused. **A second canonicalization or hashing
implementation would be a design defect**; money passes through `canonical.money`
and rates through `canonical.rate` exactly as every sealed artifact does.

The payload:

```
schema_version        the graph contract version
scope                 subject scope: tax_year, and which view (current/historical)
anchors               the frozen/sealed artifacts this graph is derived from —
                      analysis_id + snapshot_hash, optimization_run id +
                      result_hash + manifest_hash, scenario ids + result hashes
nodes                 sorted by node_key
edges                 sorted by (edge_type, source_key, target_key)
summary               the counts below
```

Deliberately **excluded**: `assembled_at`, and every user-supplied label or note.
This follows the precedent already set on `ioe.scenario`, whose `label` and
`note` carry column comments saying they are excluded from both hashes because
renaming must not change identity. A graph whose hash changed when a scenario
was renamed would be reporting a change that did not happen.

Two assemblies of the same underlying state produce the same `graph_hash`. That
is the property the tests assert, and it is what makes the read model
deterministic without persisting it.

## 5. Edge taxonomy

Every edge type names the row that authorises it. An edge with no origin row is
not emitted.

| edge type | from → to | origin |
|---|---|---|
| `DERIVED_FROM` | TAX_STATE line item → TAX_STATE run | `analysis_line_item.analysis_id` |
| `REQUIRES` | OPPORTUNITY → EVIDENCE requirement | `rules.rule_required_document` |
| `SUPPORTED_BY` | EVIDENCE requirement → EVIDENCE document | a requirement's satisfying `docs.document` rows |
| `INELIGIBLE_BECAUSE` | OPPORTUNITY → OPPORTUNITY | `portfolio_exclusion.blocking_candidate_id` |
| `CONSTRAINED_BY` | OPPORTUNITY → RESOURCE | `portfolio_exclusion.shared_resource_code` |
| `CONSUMES_RESOURCE` | OPPORTUNITY → RESOURCE | `portfolio_member.resource_allocations` |
| `CONFLICTS_WITH` | OPPORTUNITY → OPPORTUNITY | `ioe.recommendation_relationship` |
| `EXPIRES_AT` | OPPORTUNITY → DEADLINE | `rules.rule_deadline` via the pinned rule version |
| `ASSUMES` | SCENARIO → ASSUMPTION | `ioe.scenario_assumption` |
| `REFERENCES_SCENARIO` | SCENARIO → TAX_STATE run | `ioe.scenario.base_analysis_id` |
| `REFERENCES_RULE` | — | **RESERVED, zero producers** |

### `REFERENCES_RULE` is reserved, and the eight-type constraint is why

An edge needs a node at both ends. `REFERENCES_RULE` needs a `RULE` node, and
there is no rule node among the eight live types — adding one to carry an edge
would make a ninth. So rule identity travels as a `tax_rule_version_id`
**attribute** on every node that references one (TAX_STATE line items,
OPPORTUNITY, DEADLINE, EVIDENCE requirements). Nothing is lost, and
`GraphEdge.__post_init__` refuses to construct the reserved type, so promoting
it has to be deliberate.

`INELIGIBLE_BECAUSE` and `CONSTRAINED_BY` were split rather than merged: one
exclusion row can produce both, and they answer different questions — *the
candidate that won* versus *the resource that ran out*.

**`CONFLICTS_WITH` is budgeted, not exhaustive.** `ioe.recommendation_relationship`
is pairwise and the optimization work already met O(n²) growth there. Only rows
that exist are emitted — the graph never derives the missing pairs — and
`derivation_source` is carried through, because `rules_contract` edges are
authoritative while the rest are derived and may already have been trimmed by
the sparse-derivation budget upstream. Edge count is measured against node count
before this entry closes.

`SUPERSEDES` is **not** emitted. `optimization_run.superseded_by_run_id` is run
lineage rather than state, and a graph is a view of one state, not of the
history of how that state was recomputed.

## 6. Freshness and integrity

Both are carried through, never computed, and never merged — 12A keeps the
separation `domain/freshness.py` and `domain/integrity.py` already enforce.

* **Freshness** — `FreshnessVerdict` verbatim where the source tracks it
  (`optimization_run`, `scenario`). Where the source has no freshness column
  (`FACT`, `TAX_STATE`, `EVIDENCE`), the node reports `NOT_TRACKED`. It does not
  report "current". Claiming currency for something nothing measures would be
  the graph inventing a fact about itself.
* **Integrity** — `IntegrityStatus` verbatim: `not_checked`, `verified`,
  `mismatch`, `unavailable`. `not_checked` is reported as `not_checked` and not
  softened.

A node may be stale and verified, or current and non-reproducible. The summary
reports the two independently for the same reason.

## 7. Graph summary

```
node_count, edge_count
nodes_by_type          counts per node type, including explicit zeros for the
                       two reserved types
edges_by_type          counts per edge type
freshness_rollup       counts per freshness status, NOT_TRACKED counted openly
integrity_rollup       counts per integrity status
readiness_rollup       counts per evidence readiness state
```

**The summary computes no money total.** `ioe.strategy_portfolio` is documented
as the only source of a user-facing total, and the graph references that total
rather than summing benefits itself — summing `standalone_potential` across
candidates is exactly the double-count the resource ledger exists to prevent.

## 8. Evidence readiness — the second axis

Derived, never guessed:

```
no required rows for the rule version    NOT_REQUIRED
every required type held                 READY
some required types held                 PARTIAL
no required types held                   MISSING
necessity = 'conditional'                UNKNOWN
```

"Held" means a `docs.document` of that type with `deleted_at IS NULL` and
`status = 'processed'` — the only value in the CHECK vocabulary that means
successful ingestion. `quarantined` and `failed` are not evidence; `uploaded`
and `processing` are not evidence yet.

`UNKNOWN` for `conditional` is not a hedge. Whether a conditional requirement
applies is carried only in a free-text `note`, so resolving it would mean reading
prose and deciding — inference, which this entry forbids.

**Readiness never replaces `evidence_status`.** They answer different questions:
`evidence_status` describes how well the inputs to a computed figure are
supported; readiness describes whether the documents the rule demands are held.
Both are carried. The repository already refuses this same collapse one level
down, where `EvidenceStatus` is documented as "Never collapsed into
CalculationBasis: a precisely calculated figure over unverified data must show
both facts."

## 9. Batch loading plan

One query per entity family, never one per node. The families:

```
1  analysis_run + analysis_line_item        by analysis_id
2  analysis_input_snapshot                  by analysis_id   (historical anchor)
3  optimization_run                         by user + tax_year
4  optimization_candidate                   by run_id
5  portfolio + member + exclusion + ledger  by portfolio_id
6  recommendation_relationship              by run_id
7  assumption_set + assumption              by set id
8  scenario + scenario_result + assumptions by user + tax_year
9  rule_required_document + rule_deadline   by the pinned rule_version_id set
10 document joined to ref.document_type     by user + tax_year, processed only
11 finance / wealth / profile rows          by user + tax_year
```

Every load is owner-qualified in the query as well as by RLS, following
`IoeReadRepository`, whose docstring says ownership is checked there *as well as*
by RLS. Every load is bounded by an explicit limit. `test_assembly_issues_a_bounded_number_of_queries`
counts statements and asserts the count does not grow with node count — the
regression this plan exists to prevent is per-node lookups appearing in a later
edit.

## 10. Deletion lifecycle

The graph is read through the ordinary protected-route admission model. An
account past its deletion cutoff is refused by `db_authed` →
`assert_may_act` → `AccountDeletionInProgress` (403), exactly as every other
protected read is. **No graph-specific exemption is added**; the only exemption
in the repository belongs to the deletion endpoints themselves.

## 11. Persistence

None. The graph is an assembled deterministic read model. Current state is
assembled from live rows; historical context is anchored to the frozen and
sealed artifacts that already exist — `analysis_input_snapshot.snapshot_hash`,
`optimization_run.optimization_result_hash` and `manifest_hash`,
`scenario.scenario_result_hash`.

No new table, and therefore no migration. A `tax_state_graph` table holding a
person's income, opportunities and evidence would need a registry
classification, a purge keyhole, a completion guard, a write cutoff and a
lifecycle phase slot; the privacy universe was certified at 70 tables with zero
engineering blockers and this would make it 71 with one. If a later entry proves
a persisted artifact is genuinely required, it can be added then with that cost
priced in rather than discovered.

## 12. Measured cost

`scripts/probe_state_graph_cost.py`, medians over 15 rounds per size, load and
assembly timed separately because they fail differently — loading is I/O under
RLS, assembly is pure CPU over already-fetched rows.

```
  facts   nodes    load ms   assemble ms   total ms   us/node
      1       3       7.91          0.33       8.24     2746.2
     25      27       8.10          0.92       9.01      333.8
    100     102       8.16          2.77      10.94      107.2
    400     402       9.77         10.03      19.80       49.3
```

**Loading is flat.** A 400× increase in data cost 23% more load time (7.91ms →
9.77ms). That is the eleven-family plan behaving as designed, and it is the
number that would move first if a per-node lookup ever crept in — which is why
`test_assembly_issues_a_bounded_number_of_queries` compares two sizes rather
than asserting a fixed ceiling.

**Assembly is linear at roughly 25µs per node**, which is what a projection over
in-memory rows should cost.

The per-node figure falls from 2746µs to 49µs purely because the fixed ~8ms
load floor is amortised. That floor is the eleven family queries plus the unit
of work, not the graph — a single-fact tenant pays it too.

## 13. What implementation changed, and what it measured

Three corrections the design step did not anticipate, each forced by evidence:

* **`REFERENCES_RULE` became reserved.** See §5 — the eight-type constraint
  leaves its far endpoint undefined.
* **`ASSUMPTION`'s run-side source was wrong in the first draft.** It named
  `ioe.assumption` / `ioe.assumption_set`; the actual sealed source is the
  run's own `assumption_set` JSON, which is what enters the spec hash. The type
  checker then found a related latent defect: `OptimizationRun.assumption_set`
  was annotated `Mapped[dict | None]` while `generate()` writes a list and
  `PortfolioReplayService` reads it back with `list(run.assumption_set)`. That
  typechecked only because `list(mapping)` is legal — it would have silently
  yielded keys had the value ever been an object. The annotation is corrected;
  the column type is unchanged, so there is no schema effect.
* **`EVIDENCE` has two shapes.** Keying only by requirement made held documents
  invisible and left `SUPPORTED_BY` with nothing to point at.

**A measured coverage finding.** A real assembly on a freshly provisioned
database produced 13 nodes and 5 edges across `FACT`, `TAX_STATE`,
`OPPORTUNITY` and `EVIDENCE`, with `portfolio_total_benefit` of `0.00` — a
**degenerate portfolio**, every candidate excluded, so no ledger entries and no
members. `RESOURCE`, `CONSUMES_RESOURCE`, `CONSTRAINED_BY`,
`INELIGIBLE_BECAUSE` and `CONFLICTS_WITH` were therefore untouched by the
integration suite.

Waiting for the rule landscape to cooperate is not coverage. Because assembly
is pure and takes a `GraphSources` value, `tests/unit/state_graph/test_assembler.py`
drives all eight producers and all ten live edge types directly with the shapes
the loader returns — which is the payoff for separating loading from assembly.
That suite also caught a nondeterminism in its own fixture (unpinned `uuid4()`
ids), which is the behaviour a determinism test is for.
