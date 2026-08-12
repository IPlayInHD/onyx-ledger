# SEALED DETAIL DISPOSITION — the purge was designed on a false premise

11B6H set out to resolve twenty-three engineering blockers as **one**
historical-derivative architecture decision, by proving they form a coherent
group with no consumer after the account is gone, and then purging them in a
single new lifecycle phase.

**Both hard-stop gates fired. The group is not coherent, and no purge SQL was
written.**

Integrity verification reads **8 of the 23**. Deleting any of the eight turns a
verified artifact into a mismatch or an unverifiable one — the outcome that is
indistinguishable from evidence tampering. A purge built on the previous
census would have destroyed them.

**27 → 19 blocking. 43 → 51 classified.**

## HARD STOP 1 — the rule pin is `ioe.run_rule_version`, and replay reads it

`ReplayDependencyResolver.pinned_rule_versions` selects `ioe.run_rule_version`
directly (`replay/resolver.py:216`), `for_optimization` calls it (`:236`), and
`OptimizationReplayService` feeds the result into `optimization_spec_hash` as
`rule_version_set` (`replay/services.py:163`).

That is not a table carried alongside the evidence. The **spec hash is
recomputed from these rows at verification time**. Deleting them moved a
verified optimization to `mismatch/RESULT_HASH_MISMATCH`.

`ioe.run_rule_snapshot` is a different table — also read, also retained, and
already classified in 11B6C. Both exist; conflating them is what made the
earlier finding look safe.

### The correction

11B6G recorded:

> replay reads none of the 20 `ioe` detail tables

The accurate statement is:

> **verification reads exactly 8 of the 20 `ioe` detail tables, and 0 of the 3
> `analysis` ones.**

The old sentence is withdrawn, not reinterpreted.

## HARD STOP 2 — case B, not case A, for eight tables

The question was whether detail was used *historically* to construct a sealed
root whose stored hash is now self-sufficient (**A**, purgeable), or whether the
verifier **reconstructs from the detail rows at verification time** (**B**,
retained evidence).

It is B, and the code says so in its own words. `PortfolioReplayService`:

> Re-derives a sealed portfolio's identity from its own persisted rows. The
> portfolio is the one entity whose canonical form is FULLY persisted — members,
> ledger, exclusions, every objective value and every per-concept total — so its
> expected identity can be rebuilt from storage.

and `ScenarioReplayService._sealed_spec`:

> The spec is what the hash was taken over, so it is read back rather than
> re-derived.

| table | what verification does with it | deleting it |
|---|---|---|
| `ioe.strategy_portfolio` | the PORTFOLIO entity; carries `portfolio_result_hash` | **DELETE refused** (see below) |
| `ioe.portfolio_member` | `members` array of the hashed canonical form **and** the input to I-1/I-2 invariant checks | `unavailable/REPLAY_EXECUTION_FAILED` |
| `ioe.resource_ledger_entry` | `ledger` array of the hashed canonical form | `mismatch/PORTFOLIO_HASH_MISMATCH` |
| `ioe.portfolio_exclusion` | `exclusions` array of the hashed canonical form | `mismatch/PORTFOLIO_HASH_MISMATCH` |
| `ioe.optimization_candidate` | resolves `opportunity_code:tax_rule_version_id` keys **inside** that canonical form | `mismatch/PORTFOLIO_HASH_MISMATCH` |
| `ioe.run_rule_version` | recomputed into `optimization_spec_hash` | `mismatch/RESULT_HASH_MISMATCH` |
| `ioe.scenario_lever` | sealed spec, read back for `scenario_result_hash` | `unavailable/SEALED_EVIDENCE_INCOMPLETE` |
| `ioe.scenario_assumption` | sealed spec, read back for `scenario_result_hash` | `mismatch/RESULT_HASH_MISMATCH` |

### `ioe.strategy_portfolio` cannot be deleted at all

Not "should not" — cannot. Every verification it has undergone left an
`ioe.integrity_check` row, and that table's guard refuses DELETE
**unconditionally**, with no purge-context escape hatch (unlike
`ioe.reject_result_mutation`). A purge phase that included the portfolio would
abort rather than quietly destroy evidence. That is the right failure mode and
it is now pinned, because weakening the integrity-check guard would silently
convert this refusal into a successful deletion of verification history.

## Method — measured twice, because either alone has a hole

`backend/tests/privacy/test_verification_consumers.py`.

1. **Destructive.** Build a production-sealed chain (analysis → optimization →
   portfolio → scenario) with `AnalysisService`, `OptimizationOrchestrator` and
   `ScenarioService`; confirm all three verifications green; delete one table's
   rows inside the sanctioned purge context; re-verify. A fresh account per
   case, deletion scoped to it, so no case can disturb another.
2. **SQL trace.** Record every statement the three verifications issue. This
   does not depend on a fixture populating anything, so it covers the tables the
   destructive pass could not reach.

Both methods return **the same eight**. Reading a table does not prove the read
matters, and a table a fixture never fills is not thereby unread — so neither
method was trusted alone.

The scenario spec is built with **assumptions as well as levers**: without them
`ioe.scenario_assumption` is empty and reads as "nothing needs it" for entirely
the wrong reason. That is how it was missed before.

### One measurement limit, stated

`ioe.portfolio_member` and `ioe.resource_ledger_entry` are populated only when
portfolio assembly admits a candidate. On a shared database that has
accumulated hundreds of published rule versions it admits none — measured at
~890 rules: all 886 candidates excluded, 0 members, `objective_delta` 0.00. The
destructive case for those two therefore **skips with that reason** rather than
passing vacuously. Their readership stays under the unconditional SQL-trace
assertion.

## `replay_dependency` now means what its docstring says

The field promises "does replaying a sealed historical result need this row?"
It was set by a single comprehension over every sealed child — which encodes
*is descended from a replayable run*, i.e. lineage. **Eleven declarations were
wrong.** All eleven are corrected here, none deferred:

`ioe.candidate_cost`, `ioe.candidate_economic_effect`,
`ioe.confidence_component`, `ioe.multi_year_projection`,
`ioe.portfolio_evaluation_step`, `ioe.recommendation_relationship`,
`ioe.scenario_confidence_component`, `ioe.scenario_input_change`,
`ioe.scenario_result`, `ioe.score_component`, `analysis.analysis_line_item`.

Registry-wide `replay_dependency=True` is now 13: the 8 measured children plus
the 5 roots and pins the resolver genuinely reads (`analysis_run`,
`analysis_input_snapshot`, `optimization_run`, `scenario`, `run_rule_snapshot`).
`test_replay_dependency_declares_consumption_not_lineage` asserts the
declaration against the traced set, so the two cannot drift apart again.

### A third defect this surfaced

All eight proven-retained tables declared `on_account_deletion =
CASCADE_DELETE` while declaring `replay_dependency=True`. Both cannot be true.
The contradiction was unreachable in practice only because 0060 had already
detached the roots from `identity.user_account` — an accident of an earlier
entry, not a decision. They are now `RETAIN`.

`test_a_replay_dependency_never_declares_an_unqualified_destructive_action`
had scoped itself to classified tables and named this exact follow-up in its
docstring. It now covers all eight.

## Where the twenty-three landed

```
23 = 8 REPLAY_REQUIRED_RETAIN     proven read at verification time
   + 12 ioe   still blocking      read by NOTHING in the replay/integrity stack
   +  3 analysis still blocking   same shape
```

The twelve and the three are **not cleared for deletion**. "No verifier reads
it" is half an argument; the other half — that nothing else does, and that
removing it is correct rather than merely possible — was not established, and
this entry will not infer it from silence. Several are surfaced by
`read_repository`/`presentation` to the historical product UI, which is a live
consumer question, not a replay one. They remain `MISSING_LIFECYCLE_BEHAVIOR`:
no phase purges them, nothing reads them for verification, and after 0060 they
outlive the account with a dead `user_id`.

## Accounting

```
70 = 51 classified
   + 12 ioe sealed detail        MISSING_LIFECYCLE_BEHAVIOR
   +  3 analysis sealed detail   engineering evidence missing
   +  4 billing                  POLICY_DECISION_REQUIRED
```

Nothing left the universe. `ACCOUNT_REMOVAL_ENGINEERING_READY` is **NO**, and
was not going to be reachable this entry — the target of 66 classified assumed
the twenty-three were disposable, and eight of them are the evidence.

## What the next entry has to decide

The remaining fifteen are a genuinely different question from the one 11B6H
was scoped to answer, and it should not be answered by lineage either:

1. **Is a historical-UI read a retention requirement after the account is
   gone?** No account, no session, no reader — but that argument needs the same
   two-method treatment applied to `read_repository` and `presentation`, not an
   assertion.
2. **If purge: one phase, and it must exclude the eight.** The keyhole would
   have to name its tables explicitly rather than walk the parent, because
   walking `optimization_run`'s children reaches `portfolio_member` — which is
   how this entry would have destroyed evidence had the gates not held.
3. `ioe.strategy_portfolio` cannot participate regardless; the append-only
   integrity guard refuses it.
