"""Canonical default-deny privacy registry for account deletion.

Membership is the **privacy deletion universe**: every user-derived surface that
must carry a retention decision before an account can be terminally deleted. It
was certified in
``docs/privacy/11b6-account-delete-cascade-universe.md`` by walking all-CASCADE
paths from ``identity.user_account``, which is how the 70 tables were found —
but reachability was the *discovery* method, not the definition.

THE DISTINCTION IS LOAD-BEARING, and migration 0060 is why. That migration drops
four foreign keys, which removes four whole branches from the live cascade
closure. If membership were defined as "currently cascade-reachable", dozens of
tables nobody has classified would silently leave the registry and the terminal
delete would look closer to ready — privacy completeness satisfied by graph
surgery rather than by anyone deciding anything. So:

    REGISTRY                  certified privacy deletion universe; does not
                              shrink because a foreign key was dropped
    live cascade closure      what the current graph destroys; informational,
                              and asserted to be a SUBSET of the registry

A table leaving the closure stays in the registry and keeps blocking. A table
ENTERING the closure without a registry entry fails, because that is a migration
widening what deletion destroys.

Default-deny: a table with no read evidence behind it is ``UNCLASSIFIED_BLOCKING``.
That is a *workflow state*, not a retention classification. It never means
"retain indefinitely", "privacy-approved retention", "safe to expose", or
"safe to delete". It means: nobody has looked yet, so no destructive or
terminal action may rely on this row.

Regenerate by editing this file directly; the universe doc is the membership
authority and ``test_registry_invariants`` enforces the correspondence.
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
    "ai.ai_conversation": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="USER_AUTHORED_AND_GENERATED_CONTENT",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "What the user typed and what was generated back to them, plus "
            "the citations and prompt context assembled for one message. "
            "Written by app/services/ai/service.py from user input; the "
            "three children reach the account only through ai_conversation "
            "and are joined to it by ON DELETE CASCADE. Nothing in the "
            "replay or integrity stack reads any ai table — measured, not "
            "assumed — so none of this is tax evidence. It is conversation, "
            "and a conversation about somebody's taxes is exactly the "
            "content account deletion is for."
        ),
        evidence_references=(
            "app/services/ai/service.py",
            "db/sql/10_ai.sql",
            "tests/privacy/surface_census.py",
        ),
    ),
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
            "app/services/ioe/replay/resolver.py:172",
            "tests/security/test_sealed_history_after_purge.py:464",
        ),
    ),
    "audit.data_export_request": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="OPERATIONAL_REQUEST_STATE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "One request by the account to receive its own data, with a "
            "status and an object key for the produced export. Operational "
            "state for a request that can no longer be served or collected "
            "once the account is gone. Distinct from "
            "audit.data_deletion_request, which is deliberately not a "
            "foreign key because a deletion record must outlive its subject "
            "(PD-9); this one carries the account FK precisely because it "
            "need not. Census: removed by the account cascade, untouched by "
            "every phase."
        ),
        evidence_references=(
            "db/sql/14_audit.sql",
            "db/sql/44_pd9_durable_deletion_ledger.sql:122",
            "tests/privacy/surface_census.py",
        ),
    ),
    "billing.entitlement": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "billing.payment_method_ref": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "billing.subscription": Entry(state=UNCLASSIFIED_BLOCKING, depth=1),
    "docs.document": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="CONTENT_PURGED_BEFORE_ACCOUNT_REMOVAL",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The uploaded content goes and the row survives the DOCUMENTS "
            "phase as its own deletion record. That phase deletes the binary "
            "from object storage first, purges the extraction rows, then "
            "writes deleted_at — keeping content_hash, which proves WHICH "
            "document was deleted, and object_key, which is opaque since 11B4 "
            "(an account id, a version and a document id; the schema stores no "
            "filename at all) and lets an orphan check tell deleted-on-purpose "
            "from vanished. The row is tombstoned rather than deleted there "
            "because hard-deleting it would cascade away docs.document_link, "
            "and a confirmed figure must never look unsourced. Measured: a "
            "sealed optimization and scenario still replay with every document "
            "of the account gone, so the binary is not a replay dependency. "
            "DELETE at terminal removal rather than RETAIN: the content is "
            "already gone by then and nothing measured requires the tombstone "
            "to outlive the account it describes, so the existing ON DELETE "
            "CASCADE is left exactly as it is."
        ),
        evidence_references=(
            "db/sql/57_document_phase.sql",
            "app/services/document_processing/service.py:294",
            "tests/privacy/test_document_phase.py",
        ),
        row_state_dependent=True,
        state_conditioned_cleanup=(
            "A live row (deleted_at IS NULL) still has retrievable content "
            "behind it and must be purged before terminal removal; a tombstone "
            "is finished work. identity.count_remaining_document_privacy_work "
            "counts the live ones, and because the binary is deleted before "
            "the tombstone is written, zero live rows also means zero objects "
            "left in storage."
        ),
    ),
    "finance.expense_record": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "finance.expense_record_default": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "finance.expense_record_y2024": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "finance.expense_record_y2025": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "finance.income_source": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "finance.income_source_default": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "finance.income_source_y2024": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "finance.income_source_y2025": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "identity.auth_session": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_AUTHENTICATION_STATE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The credentials themselves: the Argon2id hash, the MFA secret "
            "and its user-supplied device label, unspent reset and "
            "verification tokens, and live refresh sessions. All are "
            "authorisation state for an account that will not exist, and "
            "keeping any of it would be keeping the means to act as that "
            "person. Distinct from identity.login_event, which is retained "
            "and de-identified because it is security HISTORY: these are "
            "not a record that something happened, they are the ability to "
            "do it. Census: each is untouched by all four phases and "
            "removed by the account cascade. No replay reader — the replay "
            "stack contains no identity reference at all."
        ),
        evidence_references=(
            "db/sql/02_identity.sql",
            "app/services/auth/service.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "identity.email_verification_token": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_AUTHENTICATION_STATE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The credentials themselves: the Argon2id hash, the MFA secret "
            "and its user-supplied device label, unspent reset and "
            "verification tokens, and live refresh sessions. All are "
            "authorisation state for an account that will not exist, and "
            "keeping any of it would be keeping the means to act as that "
            "person. Distinct from identity.login_event, which is retained "
            "and de-identified because it is security HISTORY: these are "
            "not a record that something happened, they are the ability to "
            "do it. Census: each is untouched by all four phases and "
            "removed by the account cascade. No replay reader — the replay "
            "stack contains no identity reference at all."
        ),
        evidence_references=(
            "db/sql/02_identity.sql",
            "app/services/auth/service.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "identity.legal_acceptance": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_LEGAL_STANDING_EVIDENCE_RETAINED_ELSEWHERE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Entry B4. Which legal document versions this account accepted "
            "and when. It is LIVE STATE rather than history: the only "
            "consumer is the gate deciding whether this person may use the "
            "application, and that question stops being asked the moment the "
            "account is gone. Deleting it therefore destroys no evidence "
            "anyone can act on. "
            "THE PROOF IS DELIBERATELY SOMEWHERE ELSE. Every acceptance also "
            "writes audit.consent_log, which 11B6D de-identifies and retains "
            "— so the record that an agreement was given survives the "
            "account, non-attributably, while the identifiable state does "
            "not. That is the same split identity.login_event and "
            "audit.security_event already use, and it is why this is a "
            "delete rather than a de-identify: de-identifying a row keyed to "
            "user_id would leave an orphan the gate can never read and the "
            "customer can never see. "
            "Append-only in life: the runtime holds INSERT and SELECT only, "
            "a trigger refuses UPDATE outright, and a second trigger refuses "
            "DELETE while the account still exists — measured, because a "
            "first attempt blocked the cascade itself and broke account "
            "deletion. The FK cascade is the single permitted removal."
        ),
        evidence_references=(
            "db/sql/66_legal_acceptance.sql",
            "app/services/legal/service.py",
            "app/privacy/classification.py",
        ),
    ),
    "identity.mfa_method": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_AUTHENTICATION_STATE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The credentials themselves: the Argon2id hash, the MFA secret "
            "and its user-supplied device label, unspent reset and "
            "verification tokens, and live refresh sessions. All are "
            "authorisation state for an account that will not exist, and "
            "keeping any of it would be keeping the means to act as that "
            "person. Distinct from identity.login_event, which is retained "
            "and de-identified because it is security HISTORY: these are "
            "not a record that something happened, they are the ability to "
            "do it. Census: each is untouched by all four phases and "
            "removed by the account cascade. No replay reader — the replay "
            "stack contains no identity reference at all."
        ),
        evidence_references=(
            "db/sql/02_identity.sql",
            "app/services/auth/service.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "identity.password_reset_token": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_AUTHENTICATION_STATE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The credentials themselves: the Argon2id hash, the MFA secret "
            "and its user-supplied device label, unspent reset and "
            "verification tokens, and live refresh sessions. All are "
            "authorisation state for an account that will not exist, and "
            "keeping any of it would be keeping the means to act as that "
            "person. Distinct from identity.login_event, which is retained "
            "and de-identified because it is security HISTORY: these are "
            "not a record that something happened, they are the ability to "
            "do it. Census: each is untouched by all four phases and "
            "removed by the account cascade. No replay reader — the replay "
            "stack contains no identity reference at all."
        ),
        evidence_references=(
            "db/sql/02_identity.sql",
            "app/services/auth/service.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "identity.user_credential": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_AUTHENTICATION_STATE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The credentials themselves: the Argon2id hash, the MFA secret "
            "and its user-supplied device label, unspent reset and "
            "verification tokens, and live refresh sessions. All are "
            "authorisation state for an account that will not exist, and "
            "keeping any of it would be keeping the means to act as that "
            "person. Distinct from identity.login_event, which is retained "
            "and de-identified because it is security HISTORY: these are "
            "not a record that something happened, they are the ability to "
            "do it. Census: each is untouched by all four phases and "
            "removed by the account cascade. No replay reader — the replay "
            "stack contains no identity reference at all."
        ),
        evidence_references=(
            "db/sql/02_identity.sql",
            "app/services/auth/service.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "ioe.decision_journal": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="USER_DECISION_RECORD_DIES_WITH_ACCOUNT",
        evidence_quality="DIRECT_SCHEMA_EVIDENCE",
        rationale=(
            "One user decision thread about one sealed scenario: the user's "
            "declared intent and self-reported actions, plus pinned artifact "
            "identities. Behavioral product state, not sealed evidence and not "
            "a replay dependency — nothing verifies against it. Dies with the "
            "account through the user_account CASCADE fired by terminal "
            "removal; the application role holds no UPDATE or DELETE, so the "
            "cascade is the only deletion path."
        ),
        evidence_references=(
            "db/sql/62_decision_journal.sql",
            "app/privacy/classification.py",
        ),
    ),
    "ioe.decision_journal_event": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="USER_DECISION_RECORD_DIES_WITH_ACCOUNT",
        evidence_quality="DIRECT_SCHEMA_EVIDENCE",
        rationale=(
            "Append-only history of one decision thread: declarations and "
            "self-reports, never system verification. Same lifecycle as its "
            "thread — removed by the decision_journal CASCADE, which the "
            "user_account cascade reaches at depth 2."
        ),
        evidence_references=(
            "db/sql/62_decision_journal.sql",
            "app/privacy/classification.py",
        ),
    ),
    "ioe.retention_checkpoint": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="ACKNOWLEDGED_PRODUCT_BASELINE_DIES_WITH_ACCOUNT",
        evidence_quality="DIRECT_SCHEMA_EVIDENCE",
        rationale=(
            "The product state a user acknowledged, stored so 'what changed "
            "since you last looked?' has a baseline. Behavioural product "
            "state, not sealed evidence and not a replay dependency — nothing "
            "verifies against it, and losing it costs one re-acknowledgement "
            "rather than any sealed guarantee, so there is no conflict between "
            "purging it and any certified replay contract. The snapshot holds "
            "governed semantic state only: no document id, object key, bucket, "
            "content hash or filename. Dies with the account through the "
            "user_account CASCADE fired by terminal removal; the application "
            "role holds no UPDATE or DELETE, so the cascade is the only "
            "deletion path. The self-referencing supersedes FK also cascades, "
            "so no successor row can outlive its predecessor."
        ),
        evidence_references=(
            "db/sql/63_retention_checkpoint.sql",
            "app/privacy/classification.py",
        ),
    ),
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
            "IMPLEMENTED by the SCENARIO_RETENTION lifecycle phase (Entry "
            "11B6E, migration 0062). Rows with scenario_result_hash IS NOT NULL "
            "sealed evidence replay can verify and are retained; rows without "
            "it never produced evidence and are deleted before terminal "
            "removal. `label` and `note` are cleared on every retained row — "
            "proven not to move any sealed hash, because ScenarioSpec excludes "
            "them from canonical_for_hash(). "
            "identity.count_remaining_scenario_privacy_work counts both "
            "obligations and the database refuses the phase while either is "
            "non-zero. NOTE the discriminator is the seal, not workflow_status: "
            "a `failed` scenario sealed nothing and is purged."
        ),
    ),
    "profile.dependent": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "profile.spouse_profile": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "profile.tax_profile": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "profile.user_preference": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_PRODUCT_SETTINGS",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Per-account product settings — locale, notification and "
            "privacy preferences. Live state read back to the subject while "
            "the account exists and meaningless afterwards; no replay, "
            "security or evidence reader resolves them. Census: untouched "
            "by every phase, removed by the account cascade."
        ),
        evidence_references=(
            "db/sql/03_profile.sql",
            "tests/privacy/surface_census.py",
        ),
    ),
    "profile.user_privacy_setting": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_PRODUCT_SETTINGS",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Per-account product settings — locale, notification and "
            "privacy preferences. Live state read back to the subject while "
            "the account exists and meaningless afterwards; no replay, "
            "security or evidence reader resolves them. Census: untouched "
            "by every phase, removed by the account cascade."
        ),
        evidence_references=(
            "db/sql/03_profile.sql",
            "tests/privacy/surface_census.py",
        ),
    ),
    "profile.user_profile": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
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
    "reco.recommendation_feedback": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_RECOMMENDATION_INTERACTION",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own reaction to a recommendation — free-text "
            "feedback and the status they moved it to. Both hang off "
            "reco.recommendation, which is already classified "
            "LIVE_USER_DATA_DELETE, and both reach it by ON DELETE CASCADE. "
            "Live product interaction rather than evidence: no replay path "
            "and no sealed artifact resolves them, and "
            "recommendation_feedback.comment is unbounded user free text."
        ),
        evidence_references=(
            "db/sql/09_reco.sql",
            "app/privacy/classification.py:599",
            "tests/privacy/surface_census.py",
        ),
    ),
    "wealth.asset": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "wealth.liability": Entry(
        state=CLASSIFIED,
        depth=1,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SOURCE_DATA_PHASE_PURGED",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own live financial and profile source data. "
            "identity.count_remaining_source_data names exactly these eight "
            "logical tables and identity.purge_source_data empties them "
            "under the subject's own RLS; the phase refuses to complete "
            "while any row remains. Census: every populated one goes at T1, "
            "before any other phase runs. The _default and _yNNNN entries "
            "are declarative partitions OF income_source and expense_record "
            "— the same rows counted through the parent, so they cannot "
            "take a different verdict. Replay does not read them: the "
            "frozen analysis snapshot exists precisely so historical "
            "results do not depend on figures the user can still edit."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "app/services/privacy/lifecycle.py",
            "tests/privacy/surface_census.py",
        ),
    ),
    "ai.ai_message": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="USER_AUTHORED_AND_GENERATED_CONTENT",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "What the user typed and what was generated back to them, plus "
            "the citations and prompt context assembled for one message. "
            "Written by app/services/ai/service.py from user input; the "
            "three children reach the account only through ai_conversation "
            "and are joined to it by ON DELETE CASCADE. Nothing in the "
            "replay or integrity stack reads any ai table — measured, not "
            "assumed — so none of this is tax evidence. It is conversation, "
            "and a conversation about somebody's taxes is exactly the "
            "content account deletion is for."
        ),
        evidence_references=(
            "app/services/ai/service.py",
            "db/sql/10_ai.sql",
            "tests/privacy/surface_census.py",
        ),
    ),
    "analysis.analysis_assumption": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Assumptions recorded against an analysis, including a free-text "
            "column. No production writer constructs one and no reader exists; "
            "the census found it unpopulated. Free text about a person with no "
            "reader and no writer is the clearest possible delete."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "analysis.analysis_input_snapshot": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="FROZEN_REPLAY_BASELINE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "THE frozen snapshot: the engine inputs a historical result was "
            "computed from, plus its hash. "
            "ReplayDependencyResolver.baseline_input reads it instead of "
            "the live financial tables — which is the whole reason replay "
            "survives the SOURCE_DATA purge — and its stored hash is "
            "compared against the scenario's pinned "
            "baseline_input_snapshot_hash before a replay is allowed to "
            "mean anything. Census: survives every phase and the diagnostic "
            "account removal unchanged, and optimization and scenario "
            "replay both verify afterwards."
        ),
        evidence_references=(
            "app/services/ioe/replay/resolver.py:172",
            "app/services/ioe/replay/resolver.py:272",
            "tests/privacy/surface_census.py",
        ),
    ),
    "analysis.analysis_line_item": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The computed line-by-line tax breakdown of one analysis. The "
            "retained artifact on this branch is analysis_input_snapshot — the "
            "frozen INPUTS, which every replay resolves. These are the OUTPUTS, "
            "and 11B6H measured that no verifier issues a statement against "
            "them. Only a DIGEST of these values survives, and a digest does "
            "not permit reconstructing its preimage, so this is NOT "
            "DERIVED_DELETE. The one reconstruction route — re-running the "
            "engine over retained inputs — is not durable: with the pins "
            "untouched and the running build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "analysis.reconciliation_check": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Per-analysis reconciliation outcomes with a system-generated "
            "detail string. Written by AnalysisService, read by nothing. Only a "
            "DIGEST of these values survives, and a digest does not permit "
            "reconstructing its preimage, so this is NOT DERIVED_DELETE. The "
            "one reconstruction route — re-running the engine over retained "
            "inputs — is not durable: with the pins untouched and the running "
            "build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "billing.invoice": Entry(state=UNCLASSIFIED_BLOCKING, depth=2),
    "docs.document_extraction": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="DERIVED_EXTRACTION_OF_PURGED_CONTENT",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Per-document extraction attempt. Deleted outright by the DOCUMENTS phase: it is derived from a binary that is itself deleted, and replay was measured to verify without it. Holds engine, status and confidence only — the values it produced live in docs.extraction_field, which goes with it."
        ),
        evidence_references=(
            "db/sql/57_document_phase.sql",
            "tests/privacy/test_document_phase.py",
        ),
    ),
    "docs.document_link": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="PROVENANCE_EDGE_OF_PURGED_FIGURE",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The edge recording that a confirmed figure came from a "
            "document. Entry 11A keeps it when the DOCUMENT is deleted, so "
            "a confirmed figure never looks unsourced — and the census "
            "shows it does survive that. What it does not survive is the "
            "FIGURE going: it is emptied at T1 by the SOURCE_DATA phase, "
            "because it cascades from finance.income_source and "
            "finance.expense_record. Once both endpoints are purged the "
            "edge records a relationship between two things that no longer "
            "exist."
        ),
        evidence_references=(
            "db/sql/11_docs.sql",
            "tests/privacy/surface_census.py",
        ),
    ),
    "ioe.multi_year_projection": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="ACCOUNT_SCOPED_HISTORICAL_PRODUCT_READ",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Per-year projected tax over the horizon. HAS A LIVE READER — GET "
            "/runs/{id}/projections, via ProjectionQueryService — and that is "
            "exactly why it needed measuring rather than assuming: the route "
            "sits behind Depends(current_user_id) and RLS, so it is reachable "
            "only by the account that owns the rows. Once that account is gone "
            "the reader is gone with it; a same-email successor is a different "
            "user_id and RLS excludes these rows. Retaining data for an "
            "interface nobody can reach is not retention, it is residue."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.optimization_candidate": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="CANDIDATE_KEY_RESOLVED_INTO_PORTFOLIO_HASH",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "`PortfolioReplayService._candidate_keys` resolves every member and "
            "exclusion to `opportunity_code:tax_rule_version_id`, and those "
            "keys ARE the canonical form the portfolio hash is taken over. "
            "Without the candidate row the key degrades to an empty string, so "
            "deleting them moved a verified portfolio to "
            "mismatch/PORTFOLIO_HASH_MISMATCH."
        ),
        evidence_references=(
            "app/services/ioe/replay/services.py:340",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.optimization_run_event": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="WORKFLOW_EVENT_LOG_NOT_THE_AUDIT_TRAIL",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Workflow events for one optimization run, with closed reason "
            "codes. NOT the audit trail: that is audit.audit_log, which 11B6D "
            "de-identifies and retains, and which is where a security or "
            "compliance reader would look. Nothing reads this table. Not "
            "reconstructable — it carries wall-clock timestamps — and that is "
            "not a retention reason."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.recommendation_relationship": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Derived edges between recommendations — conflicts, prerequisites, "
            "shared resources. Only a DIGEST of these values survives, and a "
            "digest does not permit reconstructing its preimage, so this is NOT "
            "DERIVED_DELETE. The one reconstruction route — re-running the "
            "engine over retained inputs — is not durable: with the pins "
            "untouched and the running build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
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
    "ioe.run_rule_version": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="RECOMPUTED_INTO_SPEC_HASH_AT_VERIFICATION",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "THE RULE PIN REPLAY ACTUALLY READS. `ReplayDependencyResolver."
            "pinned_rule_versions` selects this table and `OptimizationReplay"
            "Service` feeds the result into `optimization_spec_hash` as "
            "`rule_version_set` — so the spec hash is RECOMPUTED from these "
            "rows at verification time, not read back from the sealed root. "
            "Deleting them moved a verified optimization to "
            "mismatch/RESULT_HASH_MISMATCH, which is the worst possible "
            "outcome: indistinguishable from evidence tampering."
        ),
        evidence_references=(
            "app/services/ioe/replay/resolver.py:216",
            "app/services/ioe/replay/services.py:163",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.scenario_assumption": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="SEALED_SPEC_READ_BACK_AT_VERIFICATION",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "`ScenarioReplayService._sealed_spec` rebuilds the typed spec from "
            "these rows because, in its own words, 'the spec is what the hash "
            "was taken over, so it is read back rather than re-derived'. "
            "Deleting them moved a verified scenario to "
            "mismatch/RESULT_HASH_MISMATCH."
        ),
        evidence_references=(
            "app/services/ioe/replay/services.py:511",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.scenario_confidence_component": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The scenario support breakdown. IoeReadRepository defines a method "
            "for it and NOTHING CALLS THAT METHOD — measured across the whole "
            "repository, tests included — so even the account-scoped reader is "
            "dead code. Only a DIGEST of these values survives, and a digest "
            "does not permit reconstructing its preimage, so this is NOT "
            "DERIVED_DELETE. The one reconstruction route — re-running the "
            "engine over retained inputs — is not durable: with the pins "
            "untouched and the running build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.scenario_event": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="WORKFLOW_EVENT_LOG_NOT_THE_AUDIT_TRAIL",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The scenario-side twin of ioe.optimization_run_event, written by "
            "the scenario and freshness services. Same reasoning: not the audit "
            "trail, no reader, not reconstructable, no independent retention "
            "obligation."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.scenario_input_change": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="ACCOUNT_SCOPED_HISTORICAL_PRODUCT_READ",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The field-level diff a scenario's levers produced, shown in the "
            "scenario detail view. Account-scoped reader only. Only a DIGEST of "
            "these values survives, and a digest does not permit reconstructing "
            "its preimage, so this is NOT DERIVED_DELETE. The one "
            "reconstruction route — re-running the engine over retained inputs "
            "— is not durable: with the pins untouched and the running build "
            "moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: no reader survives the account."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.scenario_lever": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="SEALED_SPEC_READ_BACK_AT_VERIFICATION",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The other half of the sealed scenario spec, and the stricter half: "
            "`_sealed_spec` raises SealedEvidenceIncomplete outright when no "
            "levers remain. Deleting them moved a verified scenario to "
            "unavailable/SEALED_EVIDENCE_INCOMPLETE — the scenario stops being "
            "verifiable at all rather than merely disagreeing."
        ),
        evidence_references=(
            "app/services/ioe/replay/services.py:506",
            "app/services/ioe/replay/services.py:516",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.scenario_result": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="ACCOUNT_SCOPED_HISTORICAL_PRODUCT_READ",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The computed result of one scenario. Read by GET /scenarios/{id} "
            "and by the comparison route, both account-scoped under RLS. Only a "
            "DIGEST of these values survives, and a digest does not permit "
            "reconstructing its preimage, so this is NOT DERIVED_DELETE. The "
            "one reconstruction route — re-running the engine over retained "
            "inputs — is not durable: with the pins untouched and the running "
            "build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: no reader survives the account. Note the "
            "asymmetry that makes this safe: replay does NOT read this table — "
            "it recomputes the result and compares hashes — which is why "
            "deleting it leaves scenario verification green."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.strategy_portfolio": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="SEALED_REPLAY_ENTITY",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "PORTFOLIO is one of the three EntityType values the production "
            "verifier accepts, and this row carries `portfolio_result_hash` — "
            "the expected identity every portfolio replay compares against. "
            "Deleting it does not weaken verification, it removes the entity: "
            "the verifier raised NotFound."
        ),
        evidence_references=(
            "app/services/ioe/replay/services.py:207",
            "app/services/ioe/replay/verification.py:202",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "reco.recommendation_status_event": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="LIVE_RECOMMENDATION_INTERACTION",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The user's own reaction to a recommendation — free-text "
            "feedback and the status they moved it to. Both hang off "
            "reco.recommendation, which is already classified "
            "LIVE_USER_DATA_DELETE, and both reach it by ON DELETE CASCADE. "
            "Live product interaction rather than evidence: no replay path "
            "and no sealed artifact resolves them, and "
            "recommendation_feedback.comment is unbounded user free text."
        ),
        evidence_references=(
            "db/sql/09_reco.sql",
            "app/privacy/classification.py:599",
            "tests/privacy/surface_census.py",
        ),
    ),
    "wealth.asset_valuation": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="CASCADES_FROM_PURGED_SOURCE_DATA",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Detail of an asset or liability, reached only through it and "
            "joined to it by ON DELETE CASCADE (measured from "
            "pg_constraint). The parent is emptied by the SOURCE_DATA "
            "phase, so these go with it and cannot survive their own owner. "
            "Same class of data as the parent — valuations and balances are "
            "the user's own figures — and no replay or security reader "
            "resolves them."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "tests/privacy/surface_census.py",
        ),
    ),
    "wealth.liability_balance": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="CASCADES_FROM_PURGED_SOURCE_DATA",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Detail of an asset or liability, reached only through it and "
            "joined to it by ON DELETE CASCADE (measured from "
            "pg_constraint). The parent is emptied by the SOURCE_DATA "
            "phase, so these go with it and cannot survive their own owner. "
            "Same class of data as the parent — valuations and balances are "
            "the user's own figures — and no replay or security reader "
            "resolves them."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "tests/privacy/surface_census.py",
        ),
    ),
    "wealth.registered_account_detail": Entry(
        state=CLASSIFIED,
        depth=2,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="CASCADES_FROM_PURGED_SOURCE_DATA",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "Detail of an asset or liability, reached only through it and "
            "joined to it by ON DELETE CASCADE (measured from "
            "pg_constraint). The parent is emptied by the SOURCE_DATA "
            "phase, so these go with it and cannot survive their own owner. "
            "Same class of data as the parent — valuations and balances are "
            "the user's own figures — and no replay or security reader "
            "resolves them."
        ),
        evidence_references=(
            "db/sql/45_source_data_purge.sql:198",
            "tests/privacy/surface_census.py",
        ),
    ),
    "ai.ai_message_citation": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="USER_AUTHORED_AND_GENERATED_CONTENT",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "What the user typed and what was generated back to them, plus "
            "the citations and prompt context assembled for one message. "
            "Written by app/services/ai/service.py from user input; the "
            "three children reach the account only through ai_conversation "
            "and are joined to it by ON DELETE CASCADE. Nothing in the "
            "replay or integrity stack reads any ai table — measured, not "
            "assumed — so none of this is tax evidence. It is conversation, "
            "and a conversation about somebody's taxes is exactly the "
            "content account deletion is for."
        ),
        evidence_references=(
            "app/services/ai/service.py",
            "db/sql/10_ai.sql",
            "tests/privacy/surface_census.py",
        ),
    ),
    "ai.ai_prompt_context": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="USER_AUTHORED_AND_GENERATED_CONTENT",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "What the user typed and what was generated back to them, plus "
            "the citations and prompt context assembled for one message. "
            "Written by app/services/ai/service.py from user input; the "
            "three children reach the account only through ai_conversation "
            "and are joined to it by ON DELETE CASCADE. Nothing in the "
            "replay or integrity stack reads any ai table — measured, not "
            "assumed — so none of this is tax evidence. It is conversation, "
            "and a conversation about somebody's taxes is exactly the "
            "content account deletion is for."
        ),
        evidence_references=(
            "app/services/ai/service.py",
            "db/sql/10_ai.sql",
            "tests/privacy/surface_census.py",
        ),
    ),
    "docs.extraction_field": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="EXTRACTED_USER_CONTENT",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "What was read off the page — value_text and value_number, a salary or a figure from a slip. Deleted with the document, because deleting a PDF while keeping the numbers extracted from it is not content deletion. The facts the user CONFIRMED survive separately; those are the tax input the user asserted, not the document's contents."
        ),
        evidence_references=(
            "db/sql/57_document_phase.sql",
            "tests/privacy/test_document_phase.py",
        ),
    ),
    "ioe.candidate_cost": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The cost components behind one recommendation — amounts, timing "
            "and the normalisation provenance of each. Only a DIGEST of these "
            "values survives, and a digest does not permit reconstructing its "
            "preimage, so this is NOT DERIVED_DELETE. The one reconstruction "
            "route — re-running the engine over retained inputs — is not "
            "durable: with the pins untouched and the running build moved on, "
            "replay returns unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE "
            "(measured). It is deletable for the reason that does not depend on "
            "reconstructability: nothing reads it. No product route, no export, "
            "no operator or admin surface, no worker, and 11B6H's verification "
            "trace does not touch it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.candidate_economic_effect": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The per-candidate economic effects: amount, basis, horizon, "
            "reversibility. Only a DIGEST of these values survives, and a "
            "digest does not permit reconstructing its preimage, so this is NOT "
            "DERIVED_DELETE. The one reconstruction route — re-running the "
            "engine over retained inputs — is not durable: with the pins "
            "untouched and the running build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it, measured the same two ways."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.confidence_component": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The factors behind one candidate's confidence score. Only a DIGEST "
            "of these values survives, and a digest does not permit "
            "reconstructing its preimage, so this is NOT DERIVED_DELETE. The "
            "one reconstruction route — re-running the engine over retained "
            "inputs — is not durable: with the pins untouched and the running "
            "build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.portfolio_evaluation_step": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The assembly search trace: which candidate was considered in which "
            "order and what the objective did. Deliberately NOT part of the "
            "canonical portfolio form, so it is not even hash-committed. Only a "
            "DIGEST of these values survives, and a digest does not permit "
            "reconstructing its preimage, so this is NOT DERIVED_DELETE. The "
            "one reconstruction route — re-running the engine over retained "
            "inputs — is not durable: with the pins untouched and the running "
            "build moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.portfolio_exclusion": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="CANONICAL_PORTFOLIO_FORM_REBUILT_FROM_ROWS",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "An `exclusions` array inside the canonical portfolio form that "
            "`portfolio_result_hash` is computed over. Deleting them moved a "
            "verified portfolio to mismatch/PORTFOLIO_HASH_MISMATCH."
        ),
        evidence_references=(
            "app/services/ioe/replay/services.py:250",
            "app/services/ioe/replay/services.py:317",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.portfolio_member": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="CANONICAL_PORTFOLIO_FORM_AND_STORED_INVARIANTS",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The strongest dependency measured. Members are both the `members` "
            "array of the hashed canonical form AND the input to "
            "`_verify_invariants`, which checks I-1 telescoping and I-2 "
            "reconciliation over the stored rows. Deleting them did not produce "
            "a mismatch, it produced unavailable/REPLAY_EXECUTION_FAILED: the "
            "invariant check raises before any hash is compared."
        ),
        evidence_references=(
            "app/services/ioe/replay/services.py:241",
            "app/services/ioe/replay/services.py:374",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.resource_ledger_entry": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="REPLAY_REQUIRED_RETAIN",
        reason_code="CANONICAL_PORTFOLIO_FORM_REBUILT_FROM_ROWS",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The `ledger` array — per-resource capacity, allocated and "
            "remaining — inside the canonical portfolio form the hash is taken "
            "over. Deleting them moved a verified portfolio to "
            "mismatch/PORTFOLIO_HASH_MISMATCH."
        ),
        evidence_references=(
            "app/services/ioe/replay/services.py:246",
            "app/services/ioe/replay/services.py:291",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
    "ioe.score_component": Entry(
        state=CLASSIFIED,
        depth=3,
        classification="LIVE_USER_DATA_DELETE",
        reason_code="SEALED_DETAIL_NO_POST_ACCOUNT_READER",
        evidence_quality="DIRECT_TEST_EVIDENCE",
        rationale=(
            "The weighted factors behind one candidate's rank. Only a DIGEST of "
            "these values survives, and a digest does not permit reconstructing "
            "its preimage, so this is NOT DERIVED_DELETE. The one "
            "reconstruction route — re-running the engine over retained inputs "
            "— is not durable: with the pins untouched and the running build "
            "moved on, replay returns "
            "unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE (measured). It is "
            "deletable for the reason that does not depend on "
            "reconstructability: nothing reads it."
        ),
        evidence_references=(
            "db/sql/58_historical_detail_cleanup.sql",
            "tests/privacy/test_historical_detail_cleanup.py",
            "tests/privacy/test_verification_consumers.py",
        ),
    ),
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

