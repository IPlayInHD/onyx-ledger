# SURFACE CENSUS — what the deletion lifecycle actually does to all 70 tables

> **CORRECTION (11B6H).** The claim below that "replay does not read any of
> them" is **wrong**, and it is withdrawn rather than reinterpreted. Integrity
> verification reads **8 of the 20** `ioe` tables listed here —
> `optimization_candidate`, `portfolio_exclusion`, `portfolio_member`,
> `resource_ledger_entry`, `run_rule_version`, `scenario_assumption`,
> `scenario_lever` and `strategy_portfolio` — and deleting any of them turns a
> verified artifact into a mismatch or an unverifiable one. The rule pin replay
> resolves is `ioe.run_rule_version`, which this document counted as unread.
> The census measured *lifecycle fate*, which was sound; the readership
> sentence was inferred, not measured, and that is the difference.
> See `docs/privacy/11b6h-verification-consumers.md`. The counts in the
> **Accounting** section are superseded: 51 classified, 19 blocking.

Sixty of the seventy certified privacy surfaces were `UNCLASSIFIED_BLOCKING`, and
classifying them one at a time was never going to finish. This measured all
seventy at once, at every stage of the lifecycle, and classified 33 of them from
shared invariants that the measurement proved.

**60 → 27 blocking. 10 → 43 classified.**

## Method

`backend/tests/privacy/surface_census.py` derives each table's ownership path
from `pg_constraint` — direct `user_id`, or a foreign-key walk back to something
that has one — and counts one account's rows at six points:

```
T0  before the lifecycle          T3  after SCENARIO_RETENTION
T1  after SOURCE_DATA             T4  after AUDIT_AUTH_DEIDENTIFICATION
T2  after DOCUMENTS               T5  after a DIAGNOSTIC account removal
```

T5 is a **DIAGNOSTIC TERMINAL-STATE CENSUS** — an owner-level `DELETE` in a
disposable database, run only after all four phases reported COMPLETE and all
four completion guards read zero. It is not product terminal deletion and no
keyhole was created for it.

Ownership resolved for **70/70** tables: 34 direct, 36 parent-owned, 0
unreachable.

**The census measures; it does not decide.** A row disappearing does not make
deletion correct and survival does not make retention required. Both directions
have already produced real defects here, so every classification below is argued
from writers, readers and replay — the census only says what happened.

## Observed fate

| fate | tables |
|---|---|
| `NOT_PRESENT` (fixture did not populate) | 26 |
| `SURVIVES_UNCHANGED` | 25 |
| `DELETED_BY_ACCOUNT_CASCADE` | 11 |
| `DELETED_BY_PHASE` | 8 |

Replay after the diagnostic removal: **optimization verified, scenario
verified.**

## Classified this slice — 33

**SOURCE_DATA purged (14)** — `finance.income_source` and
`finance.expense_record` with their six partitions, `profile.tax_profile`,
`user_profile`, `dependent`, `spouse_profile`, `wealth.asset`, `wealth.liability`.
`count_remaining_source_data` names exactly these eight logical tables;
`purge_source_data` empties them; the phase refuses to complete while any
remain; the census shows every populated one gone at T1. The partitions *are*
those tables, so they cannot take a different verdict.
→ `LIVE_USER_DATA_DELETE`

**Cascading from purged source data (3)** — `wealth.asset_valuation`,
`liability_balance`, `registered_account_detail`. Reached only through an asset
or liability and joined by `ON DELETE CASCADE`.
→ `LIVE_USER_DATA_DELETE`

**Live authentication state (5)** — `identity.user_credential`, `mfa_method`,
`password_reset_token`, `email_verification_token`, `auth_session`. The Argon2id
hash, the MFA secret and its device label, unspent tokens, live sessions. These
are not a record that something happened; they are the ability to do it. Kept
distinct from `login_event`, which is security *history* and is de-identified
and retained.
→ `LIVE_USER_DATA_DELETE`

**Live product settings (2)** — `profile.user_preference`,
`user_privacy_setting`.
→ `LIVE_USER_DATA_DELETE`

**AI conversation content (4)** — `ai.ai_conversation`, `ai_message`,
`ai_message_citation`, `ai_prompt_context`. Nothing in the replay or integrity
stack reads any `ai` table — measured, not assumed — so none of it is tax
evidence.
→ `LIVE_USER_DATA_DELETE`

**Recommendation interaction (2)** — `reco.recommendation_feedback`,
`recommendation_status_event`.
→ `LIVE_USER_DATA_DELETE`

**Export request (1)** — `audit.data_export_request`. A request to receive one's
own data, unservable once the account is gone. The durable record of *deletion*
is `identity.account_lifecycle`, which does outlive the account (PD-9).
→ `LIVE_USER_DATA_DELETE`

**Provenance edge (1)** — `docs.document_link`. Entry 11A keeps it when the
DOCUMENT is deleted, and the census confirms that. What it does not survive is
the FIGURE going: it cascades from `income_source` and `expense_record` and is
emptied at T1.
→ `LIVE_USER_DATA_DELETE`

**Frozen replay baseline (1)** — `analysis.analysis_input_snapshot`.
`ReplayDependencyResolver.baseline_input` reads it instead of the live financial
tables, which is why replay survives the purge at all.
→ `REPLAY_REQUIRED_RETAIN`

## Three declarations corrected

All three were contradicted by measurement, and all three are now reconciled, so
`KNOWN_AUTHORITY_DIVERGENCES` stays empty.

| table | was | now | why |
|---|---|---|---|
| `audit.data_export_request` | `RETAIN` | `CASCADE_DELETE` | its own note conceded the cascade; PD-9 fixed the sibling by dropping the FK, this one still has it, so RETAIN was never reachable |
| `profile.user_privacy_setting` | `DE_IDENTIFY` | `CASCADE_DELETE` | the old note argued it holds consent evidence — that is `audit.consent_log`, which 11B6D de-identifies and retains. This is the live switch positions |
| `reco.recommendation_status_event` | `DE_IDENTIFY` | `CASCADE_DELETE` | the note was right that the account FK is SET NULL and wrong that this makes the row survive: it also cascades from `reco.recommendation` |

## Remaining — 27

### `ioe` sealed calculation detail (20) — MISSING_LIFECYCLE_BEHAVIOR

`candidate_cost`, `candidate_economic_effect`, `confidence_component`,
`multi_year_projection`, `optimization_candidate`, `optimization_run_event`,
`portfolio_evaluation_step`, `portfolio_exclusion`, `portfolio_member`,
`recommendation_relationship`, `resource_ledger_entry`, `run_rule_version`,
`scenario_assumption`, `scenario_confidence_component`, `scenario_event`,
`scenario_input_change`, `scenario_lever`, `scenario_result`, `score_component`,
`strategy_portfolio`.

All twenty share one measured invariant: each carries
`ioe.reject_result_mutation` (insert-only; UPDATE always refused, DELETE refused
outside the sanctioned purge context) and hangs off `optimization_run` or
`scenario`, both proven retained. All twenty survive the diagnostic account
removal unchanged.

**They are not classified, and the reason is a finding rather than a gap.**
Replay does not read any of them — it resolves the root row, the analysis
snapshot and the rule pin, and nothing else. So there is no reader evidence for
retention. Yet no phase purges them either, and after 0060 detached the roots
they outlive the account with a dead `user_id`. That is the shape §45 describes:
all four phases complete while derived tax detail about a person remains, with
nothing governing it in either direction.

Two coherent futures exist and the repository does not yet choose between them:
retain them as the substance of a retained sealed artifact, or purge them in a
new phase. `ioe.reject_result_mutation`'s own comment anticipates the second —
DELETE is permitted inside the purge context "so the account-erasure path
(privacy/retention) can run" — and no such path was ever built for this branch.

**Also measured:** 19 of these declare `replay_dependency=True` in
`app/privacy/classification.py`. Replay reads none of them. That declaration is
wrong, and correcting it belongs with whichever future decides the branch.

### `analysis` sealed detail (3) — engineering evidence missing

`analysis_assumption`, `analysis_line_item`, `reconciliation_check`. Same shape,
smaller: children of a retained `analysis_run`, surviving unchanged, with no
reader in the replay stack. Not classified for the same reason.

### `billing` (4) — POLICY_DECISION_REQUIRED

`entitlement`, `invoice`, `payment_method_ref`, `subscription`. **No production
writer exists** — no service or route constructs any of them, and the census
found none populated. The engineering treatment is therefore undetermined, and
the retention question is a statutory one this repository is not entitled to
answer. The decision needed: how long financial records of a deleted account
must be kept, and by whom.

## Accounting

```
70 = 43 classified
   + 20 ioe sealed detail        MISSING_LIFECYCLE_BEHAVIOR
   +  3 analysis sealed detail   engineering evidence missing
   +  4 billing                  POLICY_DECISION_REQUIRED
```

No table left the accounting. The universe is still 70, and dropping foreign
keys or deleting rows cannot change that.
