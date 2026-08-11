"""Canonical default-deny privacy registry for account-deletion cascade.

Generated from ``docs/privacy/11b6-account-delete-cascade-universe.md`` — the
certified 70-table set reachable from ``identity.user_account`` through
all-CASCADE paths. Regenerate by editing this file directly; the universe doc
is the membership authority and ``test_registry_invariants`` enforces the
correspondence.

Default-deny: a table with no read evidence behind it is ``UNCLASSIFIED_BLOCKING``.
That is a *workflow state*, not a retention classification. It never means
"retain indefinitely", "privacy-approved retention", "safe to expose", or
"safe to delete". It means: nobody has looked yet, so no destructive or
terminal action may rely on this row.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "CLASSIFIED",
    "UNCLASSIFIED_BLOCKING",
    "RETAINING_CLASSIFICATIONS",
    "DELETING_CLASSIFICATIONS",
    "VALID_CLASSIFICATIONS",
    "VALID_EVIDENCE_QUALITIES",
    "Entry",
    "REGISTRY",
    "terminal_delete_blockers",
    "proven_retained_tables",
    "proven_deletable_tables",
    "assert_terminal_account_delete_ready",
]

#: Workflow states. ``UNCLASSIFIED_BLOCKING`` is not a classification.
CLASSIFIED = "CLASSIFIED"
UNCLASSIFIED_BLOCKING = "UNCLASSIFIED_BLOCKING"

#: Classifications whose tables must survive an account delete.
RETAINING_CLASSIFICATIONS = frozenset(
    {
        "DEIDENTIFY_THEN_RETAIN",
        "SEALED_IMMUTABLE_RETAIN",
        "REPLAY_REQUIRED_RETAIN",
        "SECURITY_EVIDENCE_RETAIN",
        "DURABLE_LEDGER_RETAIN",
    }
)

#: Classifications whose tables may be destroyed by an account delete.
DELETING_CLASSIFICATIONS = frozenset(
    {
        "LIVE_USER_DATA_DELETE",
        "DERIVED_DELETE",
    }
)

VALID_CLASSIFICATIONS = RETAINING_CLASSIFICATIONS | DELETING_CLASSIFICATIONS

#: How the classification was established. Ranked strongest first.
VALID_EVIDENCE_QUALITIES = frozenset(
    {
        "DIRECT_CODE_EVIDENCE",
        "DIRECT_SCHEMA_EVIDENCE",
        "DIRECT_TEST_EVIDENCE",
    }
)


@dataclass(frozen=True)
class Entry:
    """One cascade-reachable table and what is known about it."""

    state: str
    depth: int
    classification: str | None = None
    reason_code: str | None = None
    evidence_quality: str | None = None
    rationale: str | None = None
    evidence_references: tuple[str, ...] = field(default_factory=tuple)
    #: True when the retention verdict depends on the STATE of the row rather
    #: than on the table. A table can be genuinely mixed — sealed historical
    #: rows that must survive alongside live rows that must not — and forcing a
    #: single table-wide answer would be a false statement either way.
    row_state_dependent: bool = False
    #: What still has to happen per row when `row_state_dependent` is True.
    #: Required in that case: "retain" without saying what happens to the live
    #: rows is how a retention verdict quietly becomes a licence to keep
    #: everything.
    state_conditioned_cleanup: str | None = None

    @property
    def protected_from_destructive_cascade(self) -> bool | None:
        """True retain, False delete, None *unknown*.

        ``None`` — never ``False`` — while unclassified. A caller that treats
        an unknown table as deletable is exactly the bug this registry exists
        to prevent, so the unknown case is not falsy-compatible with 'delete'.
        """
        if self.state == UNCLASSIFIED_BLOCKING or self.classification is None:
            return None
        return self.classification in RETAINING_CLASSIFICATIONS


#: Every certified cascade-reachable table, exactly once.
REGISTRY: dict[str, Entry] = {
    "ai.ai_conversation": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "analysis.analysis_run": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="SEALED_REPLAY_BASELINE_PARENT",
        evidence_quality="DIRECT_SCHEMA_EVIDENCE",
        rationale=(
            "The header of a completed tax analysis, and the parent of the "
            "frozen baseline every replay resolves. "
            "analysis.analysis_input_snapshot references it ON DELETE CASCADE, "
            "and that snapshot is what ReplayDependencyResolver.baseline_input "
            "reads instead of the live financial tables. The already-proven "
            "ioe.optimization_run is also its ON DELETE CASCADE child, so "
            "destroying this row destroys a table already proven to require "
            "survival. Measured: DELETE FROM analysis.analysis_run is refused "
            "outright by the database, with and without the sanctioned "
            "app.allow_evidence_purge context."
        ),
        evidence_references=(
            "db/sql/08_analysis.sql",
            "app/services/ioe/replay/resolver.py:170",
            "tests/security/test_sealed_history_after_purge.py:464",
        ),
    ),
    "audit.data_export_request": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "billing.entitlement": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "billing.payment_method_ref": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "billing.subscription": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "docs.document": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.expense_record": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.expense_record_default": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.expense_record_y2024": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.expense_record_y2025": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.income_source": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.income_source_default": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.income_source_y2024": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "finance.income_source_y2025": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "identity.auth_session": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "identity.email_verification_token": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "identity.mfa_method": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "identity.password_reset_token": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "identity.user_credential": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "ioe.freshness_outbox": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="DERIVED_DELETE",
        reason_code="OPERATIONAL_QUEUE",
        evidence_quality="DIRECT_SCHEMA_EVIDENCE",
        rationale=(
            "Transient relay queue. Rows are drained and deleted in normal "
            "operation; the queue holds no evidence that survives its own "
            "drain."
        ),
        evidence_references=(
            "db/sql/29_ioe_outbox_and_projection.sql:114",
        ),
    ),
    "ioe.integrity_check": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="SECURITY_EVIDENCE_RETAIN",
        reason_code="APPEND_ONLY_VERIFICATION_HISTORY",
        evidence_quality="DIRECT_SCHEMA_EVIDENCE",
        rationale=(
            "The durable record THAT verification ran, which is a different "
            "question from whether the artifact can still be reproduced. "
            "Replay does not need old rows — each verification APPENDS a new "
            "one — but the table refuses to give the old ones up: "
            "ioe.guard_integrity_check_transition raises on every DELETE with "
            "no escape, unlike ioe.reject_result_mutation which yields to the "
            "sanctioned app.allow_evidence_purge context. Only SELECT, INSERT "
            "and UPDATE are granted; evidence columns are write-once. "
            "Measured: this guard is what refuses DELETE FROM "
            "identity.user_account, so the terminal delete cannot execute at "
            "all today."
        ),
        evidence_references=(
            "db/sql/32_integrity_verification.sql:162",
            "db/sql/32_integrity_verification.sql:175",
            "db/sql/32_integrity_verification.sql:220",
        ),
    ),
    "ioe.optimization_run": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="SEALED_REPLAY_OUTPUT",
        evidence_quality="DIRECT_CODE_EVIDENCE",
        rationale=(
            "Replay verification reads the sealed run output to prove a "
            "historical optimization can be reproduced. Destroying the row "
            "destroys the proof."
        ),
        evidence_references=(
            "app/services/ioe/replay/verification.py:187",
            "tests/security/test_sealed_history_after_purge.py:464",
        ),
    ),
    "ioe.scenario": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="DEIDENTIFY_THEN_RETAIN",
        reason_code="SEALED_WHEN_VERIFIED_FREE_TEXT_ALWAYS",
        evidence_quality="DIRECT_SCHEMA_EVIDENCE",
        rationale=(
            "Genuinely mixed, and the only honest single answer is retain with "
            "cleanup. A scenario is a first-class replay entity "
            "(EntityType.SCENARIO) whose scenario_result_hash replay verifies, "
            "and ioe.run_rule_snapshot — already proven retained — hangs off it "
            "ON DELETE CASCADE. But workflow_status ranges over pending, "
            "running, completed, failed and cancelled, so a row can also be "
            "live product state that never sealed anything. Measured on one "
            "fixture: with the scenario verified, DELETE is refused both "
            "without the purge context (ioe.scenario_event immutability) and "
            "with it (ioe.integrity_check append-only); with the scenario "
            "unverified, the same DELETE inside the purge context succeeds and "
            "destroys ioe.scenario_result. `label` and `note` are user free "
            "text on an otherwise sealed row and must be cleared in either "
            "case, which is what makes this DEIDENTIFY rather than RETAIN."
        ),
        evidence_references=(
            "app/services/ioe/replay/verification.py:194",
            "db/sql/28_ioe_scenarios.sql",
            "tests/security/test_sealed_history_after_purge.py:238",
        ),
        row_state_dependent=True,
        state_conditioned_cleanup=(
            "Sealed rows (scenario_result_hash IS NOT NULL, or any row with an "
            "ioe.integrity_check attached) must survive. Unsealed rows are live "
            "product state and still owe an explicit purge — retaining this "
            "table is not permission to keep them. `label` and `note` must be "
            "cleared on every retained row. The per-state purge is "
            "BRANCH_CLEANUP_PENDING and is not implemented here."
        ),
    ),
    "profile.dependent": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "profile.spouse_profile": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "profile.tax_profile": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "profile.user_preference": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "profile.user_privacy_setting": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "profile.user_profile": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "reco.recommendation": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_PRODUCT_STATE",
        evidence_quality="DIRECT_CODE_EVIDENCE",
        rationale=(
            "Live user-facing product state, read straight back to the "
            "subject by an owner-scoped GET. It is the personal data the "
            "deletion request is about, not evidence about it."
        ),
        evidence_references=(
            "app/api/v1/analysis/routes.py:59",
            "app/privacy/classification.py:599",
        ),
    ),
    "reco.recommendation_feedback": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "wealth.asset": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "wealth.liability": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "ai.ai_message": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "analysis.analysis_assumption": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "analysis.analysis_input_snapshot": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "analysis.analysis_line_item": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "analysis.reconciliation_check": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "billing.invoice": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "docs.document_extraction": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "docs.document_link": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.multi_year_projection": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.optimization_candidate": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.optimization_run_event": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.recommendation_relationship": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.run_rule_snapshot": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="SEALED_IMMUTABLE_RETAIN",
        reason_code="RULE_VERSION_PIN",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Pins the rule versions a sealed run was computed under. The "
            "purge test captures the rule pin and asserts it is "
            "byte-identical after the account purge, so a retained run "
            "whose pin was deleted can no longer be verified. Note: "
            "app/privacy/classification.py declares this table "
            "CASCADE_DELETE, which the test contradicts; the declaration is "
            "not evidence."
        ),
        evidence_references=(
            "tests/security/test_sealed_history_after_purge.py:250",
            "tests/security/test_sealed_history_after_purge.py:462",
        ),
    ),
    "ioe.run_rule_version": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.scenario_assumption": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.scenario_confidence_component": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.scenario_event": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.scenario_input_change": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.scenario_lever": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.scenario_result": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ioe.strategy_portfolio": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "reco.recommendation_status_event": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "wealth.asset_valuation": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "wealth.liability_balance": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "wealth.registered_account_detail": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "ai.ai_message_citation": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ai.ai_prompt_context": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "docs.extraction_field": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.candidate_cost": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.candidate_economic_effect": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.confidence_component": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.portfolio_evaluation_step": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.portfolio_exclusion": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.portfolio_member": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.resource_ledger_entry": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
    "ioe.score_component": Entry(state=UNCLASSIFIED_BLOCKING, depth=3),
}


def terminal_delete_blockers() -> list[str]:
    """Tables that must be classified before a terminal account delete is safe."""
    return sorted(
        table
        for table, entry in REGISTRY.items()
        if entry.state == UNCLASSIFIED_BLOCKING
    )


def proven_retained_tables() -> list[str]:
    """Tables proven, from read evidence, to require survival of an account delete."""
    return sorted(
        table
        for table, entry in REGISTRY.items()
        if entry.protected_from_destructive_cascade is True
    )


def proven_deletable_tables() -> list[str]:
    """Tables proven, from read evidence, to be safe to destroy with the account."""
    return sorted(
        table
        for table, entry in REGISTRY.items()
        if entry.protected_from_destructive_cascade is False
    )


def assert_terminal_account_delete_ready() -> None:
    """Fail closed unless every cascade-reachable table carries a classification.

    Question B — terminal privacy completeness. Distinct from Question A
    (evidence-survival safety), which is answerable from a single proven-retained
    root and does not wait on this.
    """
    blockers = terminal_delete_blockers()
    if blockers:
        raise AssertionError(
            f"terminal account delete is not certified: {len(blockers)} of "
            f"{len(REGISTRY)} cascade-reachable tables are UNCLASSIFIED_BLOCKING. "
            "UNCLASSIFIED_BLOCKING is a workflow state, not permission to delete "
            "or retain. First unclassified: " + ", ".join(blockers[:5])
        )

