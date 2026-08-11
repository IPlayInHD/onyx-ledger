# CLASSIFICATION AUTHORITY, AND THE FIRST THREE ROOTS

Two files in this repository make retention claims about the same tables, and
until now nothing said which one to believe. This resolves that, then uses the
resolution to classify three of the major unresolved account-delete roots.

## The two registries

|  | `app/privacy/classification.py` | `tests/privacy/account_delete_registry.py` |
|---|---|---|
| **Purpose** | Declared privacy contract per user-derived table: privacy class, source kind, retention class, RLS, export, SOURCE_DATA purge participation, and `on_account_deletion` | Measured account-delete retention verdict for the certified cascade universe |
| **Membership** | Hand-maintained | Re-derived from `pg_constraint` on every run |
| **Consumers** | `scripts/export_privacy_inventory.py` (CSV view), `tests/security/test_privacy_inventory.py` (the gate), two unit tests | `tests/privacy/*` only |
| **Runtime authority** | **None.** No production code imports it; it drives no SQL mutation | **None.** Certification only |

Neither file deletes anything. Both are gates over a deletion implemented in
SQL. That matters for how alarming a wrong value is: a mistaken declaration is a
wrong *statement*, not a wrong *deletion*.

## Do they answer the same question?

**Yes.** `DeletionAction`'s own docstring is *"What ACCOUNT DELETION does to this
table"*, which is exactly what the account-delete registry decides. They are not
two scopes that happen to look alike, so per §7 they must agree.

Where they disagree the **registry wins**, because its verdicts are measured and
the declaration's are asserted. That is not licence to ignore the declaration:
`tests/privacy/test_classification_authority.py` reconciles them table by table
and fails on any disagreement not recorded in `KNOWN_AUTHORITY_DIVERGENCES`
with a reason. That list is currently **empty**.

Note the deliberate third category. `CUSTOM_WORKFLOW` means "needs its own
logic", which is a statement about how the answer is reached rather than what it
is; it is compatible with either verdict and is counted as neither.

## `ioe.run_rule_snapshot` — the contradiction, resolved

**Old declaration:** `CASCADE_DELETE`, alongside `replay_dependency=True`.

**Measured behaviour:**

- survives `identity.purge_source_data` with its rule pin byte-identical
  (`test_the_account_purge_leaves_every_seal_byte_identical`);
- the cascade that would destroy it cannot fire — `DELETE FROM
  ioe.optimization_run` is refused by the database, with and without the
  sanctioned `app.allow_evidence_purge` context;
- replay verifies from it after the purge.

**Finding: `DESIGN_DEFECT`, not a production defect.** `CASCADE_DELETE` reads as
*"removed by the parent's cascade; no separate step"* — a statement about the
removal **mechanism**. But the enum it belongs to claims to answer what account
deletion **does**. Those are different questions, and the value answered the
wrong one. The shape is systematic rather than a typo: of 24 tables carrying
`replay_dependency=True`, the 4 branch roots say `CUSTOM_WORKFLOW` and the 20
descendants say `CASCADE_DELETE` — descendants described relative to a parent
that is itself handled by custom logic.

It is not `PRODUCTION_DEFECT` because nothing consumes the value. Runtime logic
cannot delete a rule snapshot on the strength of this declaration, because no
runtime logic reads it.

**Corrected** to `CUSTOM_WORKFLOW`, which is what this file's own coherence gate
already demands of a replay dependency: *"replay depends on it, so deletion
cannot be an unqualified hard delete — say CUSTOM_WORKFLOW and specify it."*

The other 19 replay-dependent descendants still declare `CASCADE_DELETE`. They
are `UNCLASSIFIED_BLOCKING` in the registry, so nothing has been measured about
them and correcting them now would be a guess of exactly the kind that produced
the superseded "31 protected" figure. `test_a_replay_dependency_never_declares_
an_unqualified_destructive_action` is therefore scoped to classified tables, and
widening it is the follow-up.

## The terminal delete cannot run

The headline measurement. `DELETE FROM identity.user_account` against a
production-sealed account does not destroy evidence — **it is refused**:

```
ioe.integrity_check is append-only (DELETE rejected)
```

`ioe.guard_integrity_check_transition` raises on every DELETE with no escape,
unlike `ioe.reject_result_mutation`, which yields to
`app.allow_evidence_purge = 'on'`. An `ON DELETE CASCADE` foreign key pointing
at a table that refuses DELETE is a cascade that can never fire, so the
registry's terminal-readiness gate failing closed matches the database's actual
behaviour rather than merely anticipating it.

Measured on a production-sealed fixture, every statement rolled back:

| statement | plain | inside `app.allow_evidence_purge='on'` |
|---|---|---|
| `DELETE FROM identity.user_account` | refused (`integrity_check`) | refused (`integrity_check`) |
| `DELETE FROM ioe.optimization_run` | refused (`optimization_run_event`) | refused (`integrity_check`) |
| `DELETE FROM analysis.analysis_run` | refused (`optimization_run_event`) | refused (`integrity_check`) |
| `DELETE FROM ioe.scenario` (verified) | refused (`scenario_event`) | refused (`integrity_check`) |
| `DELETE FROM ioe.scenario` (unverified) | refused (`scenario_event`) | **succeeds**, destroying `scenario_result` |

## The three roots

All three are direct depth-1 `ON DELETE CASCADE` children of
`identity.user_account`.

### `analysis.analysis_run` → `REPLAY_REQUIRED_RETAIN`

The header of a completed analysis and the parent of the frozen replay
baseline. `analysis.analysis_input_snapshot` references it `ON DELETE CASCADE`,
and that snapshot is what `ReplayDependencyResolver.baseline_input` reads
*instead of* the live financial tables. The already-proven `ioe.optimization_run`
is also its `ON DELETE CASCADE` child — so destroying this row destroys a table
already proven to require survival, before any argument about the analysis
itself. Cascade descendants: 31.

### `ioe.scenario` → `DEIDENTIFY_THEN_RETAIN`, `ROW_STATE_DEPENDENT = TRUE`

Genuinely mixed, and a single table-wide verdict would be false about half the
rows. A scenario is a first-class replay entity (`EntityType.SCENARIO`) whose
`scenario_result_hash` replay verifies, and the proven-retained
`ioe.run_rule_snapshot` hangs off it `ON DELETE CASCADE`. But `workflow_status`
ranges over `pending, running, completed, failed, cancelled`, so a row can also
be live product state that never sealed anything — and the table proves the
distinction itself: the same DELETE inside the purge context is refused for a
verified scenario and succeeds for an unverified one.

`label` and `note` are user free text on an otherwise sealed row and must be
cleared on every retained row, which is what makes this `DEIDENTIFY` rather than
plain `RETAIN`. State-conditioned cleanup is recorded on the entry and remains
`BRANCH_CLEANUP_PENDING`. Cascade descendants: 8.

### `ioe.integrity_check` → `SECURITY_EVIDENCE_RETAIN`

§17's distinction decided it. **Replay does not need old rows** — each
verification appends a new one, and `test_replay_after_the_account_purge_
verifies_from_sealed_inputs` asserts the count goes up by exactly one. Artifact
reproducibility is independent of the verification history.

But the rows are durable evidence *that verification occurred*, and the schema
enforces it rather than merely intending it: DELETE is rejected unconditionally,
evidence columns are write-once, and only `SELECT, INSERT, UPDATE` are granted.
This is the guard that refuses the terminal account delete. Cascade
descendants: 0.

## Known protected cascade conflicts

A conflict is a table proven to require survival that is nonetheless reachable
from the account through an unbroken `ON DELETE CASCADE` chain.

| retained table | depth | minimal direct-account roots exposing it |
|---|---|---|
| `analysis.analysis_run` | 1 | `analysis.analysis_run` |
| `ioe.optimization_run` | 1 | `analysis.analysis_run`, `ioe.optimization_run` |
| `ioe.scenario` | 1 | `analysis.analysis_run`, `ioe.scenario` |
| `ioe.integrity_check` | 1 | `analysis.analysis_run`, `ioe.integrity_check`, `ioe.optimization_run`, `ioe.scenario` |
| `ioe.run_rule_snapshot` | 2 | `analysis.analysis_run`, `ioe.optimization_run`, `ioe.scenario` |

`KNOWN_PROTECTED_CASCADE_CONFLICTS = 5`, over **4 direct roots**:
`analysis.analysis_run`, `ioe.optimization_run`, `ioe.scenario`,
`ioe.integrity_check`. `analysis.analysis_run` exposes all five and is the
outermost of them.

`UNCLASSIFIED_BLOCKING = 63`. These two numbers must never be summed or
conflated: a blocking table is not proven retained and not proven safe. It is
unmeasured.

## `ROOT_CASCADE_MUST_CHANGE`

Facts only. No replacement mechanism is chosen here — not `SET NULL`, not
dropping the FK, not a subject key, not a sidecar.

```
identity.user_account -> analysis.analysis_run    ON DELETE CASCADE
identity.user_account -> ioe.optimization_run     ON DELETE CASCADE
identity.user_account -> ioe.scenario             ON DELETE CASCADE
identity.user_account -> ioe.integrity_check      ON DELETE CASCADE
```

Each currently destroys, or would destroy, a table proven to require survival.
Today all four are inoperable anyway, because the cascade reaches
`ioe.integrity_check` and stops.

## Scope

No migration `0060`. No foreign key changed. No lifecycle phase wired. 63 of 70
tables remain `UNCLASSIFIED_BLOCKING` and terminal deletion remains blocked,
which is the expected state.
