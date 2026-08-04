# Onyx Ledger — Production Replay-Integrity Verification

**Scope.** The running system can verify that a sealed optimization, portfolio, or scenario can
still be reproduced from its own pinned dependencies. This is not a test-time determinism proof; it
is an operational capability with persisted history, tenant isolation, a worker, a schedule, an API
surface, and alerting.

**What it does not claim.** Integrity concerns *reproducibility*, nothing else. A verified result is
not thereby correct, not accepted by the CRA, and not a promise that the displayed amount will be
received. Every user-facing string in this feature is written to make that distinction impossible to
blur.

---

## 1. Integrity state model

Three independent axes. Collapsing any two of them loses information a reader needs.

| Axis | Values | Question it answers |
|---|---|---|
| `workflow_status` | `pending` `running` `completed` `failed` `cancelled` | Did the run finish? |
| `freshness_status` | `unknown` `current` `stale` `superseded` | Do the inputs still hold? |
| `integrity_status` | `not_checked` `verified` `mismatch` `unavailable` | Can we still reproduce it? |

A result can be `completed` + `stale` + `verified`: the user's income changed, but the answer we gave
them is exactly the answer that evidence produces. It can equally be `completed` + `current` +
`mismatch`, which is far more serious. Showing the second behind the word "stale" would read as
"your data changed" when the truth is "we cannot reproduce what we told you", so a mismatch is
surfaced to users under a distinct classification:

```
integrity_status = mismatch   →   user-visible state = non_reproducible
```

`domain/integrity.py::user_visible_state` is the only place that mapping exists, and
`test_a_mismatch_is_surfaced_as_non_reproducible` asserts the word "stale" never appears in the
mismatch wording.

A verification *attempt* additionally has `failed`, which the entity never inherits: a verifier that
crashed has proved nothing about the result, so the entity becomes `unavailable`, never `mismatch`.

## 2. Reason codes

A closed enumeration, mirrored by a CHECK constraint. An exception message is never a reason code —
it is unbounded text that has already been near financial values and would end up in an alert.

| Group | Codes |
|---|---|
| Success | `NONE` |
| Compared and differed | `RESULT_HASH_MISMATCH`, `PORTFOLIO_HASH_MISMATCH` |
| Dependency unloadable | `BASELINE_SNAPSHOT_UNAVAILABLE`, `BASELINE_RESULT_UNAVAILABLE`, `PINNED_RULE_SNAPSHOT_UNAVAILABLE`, `PINNED_ENGINE_VERSION_UNAVAILABLE`, `PINNED_ENGINE_CONFIG_UNAVAILABLE`, `REFERENCE_DATA_VERSION_UNAVAILABLE`, `OBJECTIVE_VERSION_UNAVAILABLE`, `LEVER_REGISTRY_VERSION_UNAVAILABLE`, `ASSUMPTION_REGISTRY_VERSION_UNAVAILABLE`, `SCORING_VERSION_UNAVAILABLE`, `SUPPORT_SCORE_VERSION_UNAVAILABLE`, `PROJECTION_VERSION_UNAVAILABLE`, `CANONICAL_SERIALIZATION_VERSION_UNSUPPORTED` |
| Verifier or evidence | `REPLAY_EXECUTION_FAILED`, `SEALED_EVIDENCE_INCOMPLETE` |

## 3. Persistence model

**`ioe.integrity_check`** — append-only history, one row per attempt.

Exactly one of `optimization_run_id`, `portfolio_id`, `scenario_id` is populated, enforced by
`ck_integrity_check_one_entity`, and it must agree with `entity_type`
(`ck_integrity_check_entity_matches_type`). Separate typed FKs rather than a polymorphic pair, so
the database enforces referential integrity and the planner uses a real index.

Further CHECKs encode the state model itself: a terminal check has a completion time and a running
one does not; a `verified` row's observed hash must equal the expected one and its reason must be
`NONE`; a `mismatch` row's reason must be one of the two mismatch codes.

**Current metadata** lives on the three sealed parents — `optimization_run`, `scenario`,
`strategy_portfolio` — as `integrity_status`, `integrity_reason_code`,
`last_integrity_checked_at`, `latest_integrity_check_id`. That is a cache of "what the latest
completed check said", so rendering a badge does not scan history.

**Why the running→terminal update is allowed on an append-only table.** The verdict is unknown when
the claim is taken, and the alternative — holding a transaction open across an engine replay to
avoid an update — is far worse. So the row is *workflow* for exactly one transition, and everything
that is *evidence* is write-once, enforced field by field by
`ioe.guard_integrity_check_transition()`: both hashes, the reason code, the entity references, the
pinned versions and `started_at` are immutable, a terminal row cannot transition again, and DELETE
is rejected outright. A later verification appends a new row.

**Portfolio immutability.** `strategy_portfolio` is sealed calculation evidence guarded by
`ioe.reject_result_mutation()`. That guard now permits exactly the four integrity-metadata columns
to move, and the check is exact rather than by omission: the two row images are compared with those
four keys stripped, so an UPDATE that changes a financial column is still rejected even when bundled
with a legitimate integrity change. No trigger is disabled and no heavier lock is taken.

## 4. Transaction boundaries

```
TX-1   authorize · claim the check · INSERT running · COMMIT
——     replay, holding no transaction (it re-runs the tax engine)
TX-2   transition the claimed row · update current metadata · emit event · COMMIT
```

The operational event is emitted *before* the terminal write so its id lands in the same UPDATE; a
second UPDATE to attach it afterwards is rejected by the append-only guard, and rightly so.

## 5. Dependency-resolution model

`ReplayDependencyResolver` enforces one rule: **a replay may never substitute a current version for
a pinned one.** If a run was sealed under engine `py-1.0.0` and this process ships `py-1.1.0`, the
code that produced the result is gone. Running the new engine and comparing hashes would report a
mismatch meaning "we upgraded", indistinguishable from one meaning "the evidence was tampered with".

So each pinned **executable** version in the sealed manifest is compared against what this build
implements, and any difference resolves to `unavailable` with its own reason code — never
`mismatch`, because nothing was compared. `EXECUTABLE_VERSIONS` maps sixteen manifest keys to their
running counterpart and reason.

Data dependencies are loaded under ordinary tenant RLS: the frozen analysis snapshot, the rule
snapshot (whose stored content hash must still equal the pinned one), the pinned rule-version set,
and for a scenario the pinned baseline result. A missing one is likewise `unavailable`.

**The baseline is the frozen snapshot, never live data.** `TaxEngineService.build_input` reads the
user's current financial tables. A replay that used it would report a mismatch every time somebody
edited last year's income, which says nothing about whether the sealed result was reproducible.
`_compute` therefore accepts a pre-resolved baseline; production passes nothing and is unchanged.

## 6. Replay algorithms

**Optimization.** Resolve dependencies → rebuild the pinned spec → re-run the full compute path
(rules evaluation constrained to the pinned versions, normalization, scoring, support scoring,
relationship derivation, portfolio assembly) against the frozen baseline → rebuild the canonical
result payload → `optimization_result_hash(spec_hash=sealed, result=payload)` → compare. The spec
hash is independently recomputed when the run stored its constraints and assumption set.

**Portfolio.** The portfolio is the one entity whose canonical form is fully persisted, so its
expected identity is rebuilt from its own rows: members in apply order, ledger, exclusions, every
objective value and all eleven per-concept totals. The engine is still re-run to re-derive the
baseline objective from the pinned inputs — a hash that matched while the objective no longer
reproduced would be a hash of the wrong thing — and **I-1 telescoping** (member increments sum to
the objective delta) and **I-2 reconciliation** (portfolio total benefit equals the objective delta)
are both re-checked over the stored rows.

**Scenario.** Resolve dependencies → read the sealed typed lever spec and structured assumptions
back from their rows → clone the frozen baseline → apply levers through the pinned registry → run
the engine → rebuild the canonical result including the support-score fields →
`scenario_result_hash(spec_hash=sealed, result=payload)` → compare.

All three return a `ReplayOutcome` — a pair of hashes. None of them decides anything; the
verification service turns hashes into verdicts.

## 7. Mismatch handling

* the sealed result is preserved byte for byte; nothing is regenerated or repaired;
* the expected hash is never overwritten — only the observed hash is written, and only into the
  restricted integrity record;
* an immutable check row is appended;
* current state becomes `mismatch`, surfaced as `non_reproducible`;
* a sanitized operational event is emitted plus an alert-compatible signal carrying only the
  correlation id, so an alert routed to a wide audience still discloses nothing;
* the API returns an explicit integrity warning;
* **no replacement run is created** — asserted by `test_a_mismatch_does_not_create_a_replacement_run`.

## 8. Unavailable handling

A check row is appended with the precise structured reason, the historical result is preserved, and
`actual_result_hash` stays NULL because nothing was compared. Severity policy: `unavailable` is
expected during a version rollout, so it is recorded and **never paged**; only `mismatch` alerts.

## 9. Concurrency strategy

Three partial unique indexes — one per entity column, each on `(entity_id, verifier_version) WHERE
status = 'running'` — are the arbitration. Three workers racing produce one running row and two
unique violations, which the service reads as "already claimed" and reports as a typed 409. Claims
carry `claimed_by` and a 15-minute `claim_expires_at`; the append-only trigger refuses a completion
by a different worker. `ioe.recover_stale_integrity_checks()` transitions abandoned claims to
`failed`, releasing the index while keeping the abandoned attempt in history.

## 10. Worker privilege model

Same keyhole as the freshness relay. `ioe.claim_integrity_targets(batch, kind)` is SECURITY DEFINER
with a fixed `search_path` including `pg_catalog`, owned by a non-login role, not executable by
PUBLIC, granted only to `onyx_freshness_worker`, bounded to 50, and returns **three fields**:
entity type, entity id, owner id. No hash, no result column, no financial value. The replay then
runs under ordinary tenant context with `app.user_id` set from the claimed row, so every statement
is subject to the same policies as that user's own request. Both new functions are in the
catalogue-driven privilege allowlist.

## 11. API behaviour

| Endpoint | Behaviour |
|---|---|
| `GET /ioe/{entity_type}/{entity_id}/integrity` | Current metadata from stored columns. Never triggers a replay. |
| `POST /ioe/{entity_type}/{entity_id}/integrity/verify` | Explicit verification. 409 while another is running. |
| Scenario / portfolio detail | Carry an `integrity` block alongside `freshness`. |

Hashes are never exposed to ordinary users. An unknown entity type and another tenant's entity both
return the same 404, so an integrity endpoint cannot be used to discover that another user's entity
exists. Wording:

```
verified:        This historical result was successfully reproduced from its sealed inputs
                 and pinned calculation versions.
not_checked:     This result has not yet undergone a production replay check.
unavailable:     This result could not currently be replay-verified because a pinned
                 dependency is unavailable.
non_reproducible: This historical result could not be reproduced exactly. The original
                 result has been preserved and is under review.
```

`test_integrity_wording_makes_no_correctness_or_cra_claim` fails if any of these gains the words
CRA, correct, guarantee, accepted, or legal.

## 12. Operational events and metrics

Emitted: verification started, verified, mismatch, unavailable, execution failure, stale claim
recovered. Metrics: per-outcome counts, per-reason counts, average duration, stale claims recovered.

A mismatch event may carry entity type and id, tenant reference, expected and actual hash, reason
code, verifier version, correlation id, duration. It may never carry tax inputs, income, deductions,
SIN-like values, document text, assumption narrative, or a serialized canonical payload — a
canonical payload *is* the financial data. The event functions have no parameter through which one
could travel.

## 13. Privacy controls

Synthetic SIN, income, account number and document strings are asserted absent from logs, stored
error fields, integrity reason fields, operational events, and queue payloads. The queue payload is
a three-field dataclass of identifiers, asserted by field name rather than by inspection of values.

## 14. Worked examples

**Verified optimization.** Run sealed with `optimization_result_hash = 7c1e…`. Replay resolves 16
pinned versions, rebuilds the baseline from the frozen snapshot, re-runs the pipeline, recomputes
`7c1e…`. → `verified`, `NONE`, `actual = expected`.

**Verified scenario.** Sealed `scenario_result_hash` recomputed from the pinned baseline, the sealed
lever spec and the pinned registries. → `verified` in ~13 ms.

**Verified portfolio.** `portfolio_result_hash = 2baf…` rebuilt from members, ledger, exclusions and
all eleven totals; baseline objective re-derived from the engine; I-1 and I-2 both hold. →
`verified`.

**Hash mismatch.** A replay double returns `aaaa…` against sealed `e3f0…`. → `mismatch`,
`RESULT_HASH_MISMATCH`, user-visible `non_reproducible`. The run keeps `e3f0…`, stays `completed`,
gains no replacement, and the check row stores expected `e3f0…` and actual `aaaa…`.

**Unavailable pinned dependency.** A run whose analysis snapshot is a stub cannot have its baseline
rebuilt. → `unavailable`, `BASELINE_SNAPSHOT_UNAVAILABLE`, `actual_result_hash` NULL, sealed result
untouched.

## 15. Known limitations and remaining risks

1. **Historical executable code is not archived.** When a pinned executable version differs from the
   running build, the result is *permanently* `unavailable` in that build — correct, but it means
   verification coverage drops to zero for older results after any engine, scoring, canonicalizer or
   registry bump. Genuine long-horizon replay needs versioned executable artefacts, which is an
   architecture decision, not a bug fix.
2. **Runs sealed before this item cannot have their spec hash independently recomputed.** The
   constraints and assumption set that entered `optimization_spec_hash` were never stored. They are
   stored from now on; historical rows verify their *result* hash only.
3. **Optimization replay is sensitive to a pre-existing defect** (§16): a run computed from live
   financial data that differed from its pinned snapshot will report a genuine `mismatch`.
4. **In-process metrics.** The counters are per-process. A real deployment needs them scraped or
   pushed; no exporter is wired.
5. **No automatic scheduling trigger is installed.** `IntegrityScheduler.run_once` is callable and
   bounded, but nothing invokes it periodically — that needs the operational ownership of item 9.

## 16. Defects this work surfaced

**D-1. The optimization computes from live data while pinning a frozen snapshot.**
`_pin_specification` pins `baseline_input_snapshot_hash` from the frozen analysis snapshot, but
`_compute` builds its input from the user's *current* financial tables. If those differ, the spec
hash is not an identity for the computation it labels. Replay reads the snapshot — the thing the
spec actually pinned — so such a run reports `mismatch`. **Not fixed here**: making TX-1 compute from
the snapshot changes production behaviour and requires every fixture to write a complete snapshot.
Recorded as the highest-priority follow-up.

**D-2. `strategy_portfolio.portfolio_result_hash` existed but was never written.** A portfolio had
no expected identity to be replayed against. Now written in `PortfolioEvaluationService.persist`.

**D-3. `future_option_value` entered the portfolio hash and was then discarded.** The canonical form
carries eleven per-concept totals; the table stored ten, so a portfolio's own identity could not be
rebuilt from its own row. Added as `total_future_option_value`.

**D-4. The optimization spec's constraints and assumption set were hashed but never stored.** Added
as `user_constraints` and `assumption_set` on `optimization_run`.

**D-5. `assumption_registry_version` and `projection_methodology_version` affected results but were
not in the version manifest.** Both now pinned.
