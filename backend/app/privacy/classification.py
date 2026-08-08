"""The governed privacy classification of every user-derived table.

WHY THIS IS CODE AND NOT A DOCUMENT
-----------------------------------
A privacy inventory in a markdown file is accurate on the day it is written and
wrong by the next migration, silently. Nothing tells you that the table someone
added last week holds financial data with no declared retention — you find out
during an incident, or during an erasure request you cannot answer.

So the inventory is a registry the type checker reads and a test enforces.
`tests/security/test_privacy_inventory.py` derives the set of user-derived
tables from the LIVE schema — by following foreign keys out from
`identity.user_account`, not from a hand-kept list — and fails if any of them is
missing here, or if an entry here names a table that no longer exists. A new
user-derived table cannot reach production without someone deciding, on the
record, what happens to it when its owner asks to be forgotten.

This is the same shape as Entry 7's schema-drift policy and Entry 10's wiring
ledger: the claim and the enforcement live together.

WHAT THIS PACKAGE DOES NOT DO
-----------------------------
It deletes nothing. Entry 11A is specification-first by instruction, and for a
good reason: a deletion routine written before the data model is understood
deletes the wrong things confidently. Every `DeletionAction` here is a
DECLARATION of intent that Entry 11B must implement and prove.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PrivacyClass(StrEnum):
    """What KIND of personal data a column group is.

    Closed set. A field that fits none of these is a signal that the taxonomy is
    wrong, not that the field is unclassified.
    """

    DIRECT_IDENTIFIER = "DIRECT_IDENTIFIER"
    """Identifies a natural person on its own: email address, display name."""

    ACCOUNT_IDENTITY = "ACCOUNT_IDENTITY"
    """The account itself — status, timestamps, internal id."""

    AUTHENTICATION_SECURITY = "AUTHENTICATION_SECURITY"
    """Credentials, sessions, tokens, MFA. Compromise is immediate."""

    FINANCIAL_SOURCE_DATA = "FINANCIAL_SOURCE_DATA"
    """Amounts the user entered or confirmed: income, expenses, assets."""

    TAX_PROFILE_DATA = "TAX_PROFILE_DATA"
    """Circumstances that drive eligibility: province, marital status,
    dependants, employer."""

    DOCUMENT_BINARY = "DOCUMENT_BINARY"
    """The uploaded file itself, in object storage. Never in PostgreSQL."""

    DOCUMENT_EXTRACTED_DATA = "DOCUMENT_EXTRACTED_DATA"
    """Fields read out of a document — a T4's box 14, and its confidence."""

    USER_FREE_TEXT = "USER_FREE_TEXT"
    """Text the user wrote. The highest-variance class: a `notes` field can
    contain anything, including things the product never asked for."""

    DERIVED_TAX_INPUT = "DERIVED_TAX_INPUT"
    """Assembled engine input. Derived, but reconstitutes the source."""

    DERIVED_TAX_RESULT = "DERIVED_TAX_RESULT"
    """Computed figures: tax owed, refund, marginal rate."""

    RECOMMENDATION_DATA = "RECOMMENDATION_DATA"
    """What the platform suggested, and the user's response to it."""

    SEALED_EVIDENCE = "SEALED_EVIDENCE"
    """Immutable under normal operation because replay integrity depends on it.
    Immutable is NOT the same as undeletable — see the specification, §41."""

    AUDIT_SECURITY_RECORD = "AUDIT_SECURITY_RECORD"
    """Who did what, kept for security or governance rather than for the user."""

    OPERATIONAL_TELEMETRY = "OPERATIONAL_TELEMETRY"
    """Exists to run the system: queues, counters, leases, outbox rows."""

    PSEUDONYMOUS_IDENTIFIER = "PSEUDONYMOUS_IDENTIFIER"
    """Cannot be read as a person, still points at exactly one: an internal
    UUID, an HMAC digest of an email. NOT anonymous — see §25."""

    PUBLIC_REFERENCE_DATA = "PUBLIC_REFERENCE_DATA"
    """Legislation, brackets, provinces. Identical for every user."""

    SYSTEM_CONFIGURATION = "SYSTEM_CONFIGURATION"
    """Weights, versions, feature state. Not about a person."""


class SourceKind(StrEnum):
    """Where the data came from, which decides how deletion may treat it."""

    SOURCE = "SOURCE"
    """The user provided it and may change it. Deletable in place."""

    DERIVED = "DERIVED"
    """Computed from source and reproducible from it. Deletable and
    regenerable."""

    SEALED_DERIVED = "SEALED_DERIVED"
    """Computed and then FROZEN as the evidence for a historical decision.
    Deleting it does not corrupt anything, but it does destroy the ability to
    prove what was calculated — which is a choice, not an accident."""

    OPERATIONAL = "OPERATIONAL"
    """Machinery. Bounded retention, no user-facing meaning."""

    AUDIT = "AUDIT"
    """Kept to answer 'what happened', often about security."""


class RetentionClass(StrEnum):
    """How long, expressed as a POLICY name rather than a number.

    Durations are deliberately absent: several of them are legal determinations
    this repository is not entitled to make (see the decision register in the
    specification). Naming the class lets the engineering be built now and the
    number be set once, in one place, when it is known.
    """

    WHILE_ACCOUNT_ACTIVE = "WHILE_ACCOUNT_ACTIVE"
    """Lives and dies with the account."""

    TAX_YEAR_RETENTION = "TAX_YEAR_RETENTION"
    """Tied to how long a tax year stays relevant.
    TAX_RETENTION_REVIEW_REQUIRED."""

    SHORT_OPERATIONAL = "SHORT_OPERATIONAL"
    """Hours to days. Counters, leases, transient state."""

    BOUNDED_AUDIT = "BOUNDED_AUDIT"
    """A defined security-audit window, then purge or de-identify.
    LEGAL_REVIEW_REQUIRED for the window itself."""

    UNTIL_SUPERSEDED = "UNTIL_SUPERSEDED"
    """Kept only until a newer version replaces it."""

    RETAINED_PENDING_REVIEW = "RETAINED_PENDING_REVIEW"
    """No defensible retention decision can be made without input this
    repository does not have. Explicitly parked, not silently permanent."""


class DeletionAction(StrEnum):
    """What ACCOUNT DELETION does to this table.

    Distinct from what ordinary object deletion does, which is described per
    table in the specification.
    """

    HARD_DELETE = "HARD_DELETE"
    """Rows go. Nothing depends on them."""

    CASCADE_DELETE = "CASCADE_DELETE"
    """Removed by the parent's cascade; no separate step."""

    DE_IDENTIFY = "DE_IDENTIFY"
    """Row survives with every path back to the person severed."""

    TOMBSTONE = "TOMBSTONE"
    """Content removed, a marker retained so the absence is provable and a
    restored backup can be told what to re-delete."""

    RETAIN = "RETAIN"
    """Survives deliberately, with a stated reason."""

    CUSTOM_WORKFLOW = "CUSTOM_WORKFLOW"
    """Needs its own logic — usually because the row is sealed evidence or
    crosses into object storage."""


class LifecycleState(StrEnum):
    """The conceptual states user-derived data moves through.

    A model, not a column. Most tables will never carry this as a value; the
    states describe what the SYSTEM must be able to distinguish. Where a table
    does need to express one, the specification names the mechanism —
    `deleted_at`, a supersession pointer, a deletion-ledger row.
    """

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"
    DELETION_REQUESTED = "DELETION_REQUESTED"
    DELETION_IN_PROGRESS = "DELETION_IN_PROGRESS"
    DELETED = "DELETED"
    DE_IDENTIFIED = "DE_IDENTIFIED"
    RETAINED_FOR_AUDIT = "RETAINED_FOR_AUDIT"
    RETAINED_IN_BACKUP = "RETAINED_IN_BACKUP"


@dataclass(frozen=True)
class TableLifecycle:
    """One user-derived table's complete privacy contract."""

    table: str
    classes: tuple[PrivacyClass, ...]
    source: SourceKind
    retention: RetentionClass
    on_account_deletion: DeletionAction
    #: Does replaying a sealed historical result need this row?
    replay_dependency: bool = False
    #: Is it immutable under normal application operations (triggers, no UPDATE
    #: path)? Immutable is not undeletable — see specification §41.
    immutable: bool = False
    #: Row-level security enabled AND forced on this table.
    rls: bool = False
    #: Included in a user data export.
    exportable: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.classes:
            raise ValueError(f"{self.table}: at least one PrivacyClass required")
        if "." not in self.table:
            raise ValueError(f"{self.table}: expected schema-qualified name")


def _e(
    table: str,
    classes: tuple[PrivacyClass, ...],
    source: SourceKind,
    retention: RetentionClass,
    action: DeletionAction,
    **kw: object,
) -> TableLifecycle:
    return TableLifecycle(
        table=table, classes=classes, source=source, retention=retention,
        on_account_deletion=action, **kw,  # type: ignore[arg-type]
    )


P = PrivacyClass
S = SourceKind
R = RetentionClass
D = DeletionAction

_ENTRIES: tuple[TableLifecycle, ...] = (
    # ---------------------------------------------------------------- identity
    _e("identity.user_account", (P.DIRECT_IDENTIFIER, P.ACCOUNT_IDENTITY),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CUSTOM_WORKFLOW, exportable=True,
       notes="Holds the email. The root of the deletion graph; 39 tables "
             "reference it. Deleting the row cascades most of the account, "
             "which is why deletion must be orchestrated rather than issued as "
             "one DELETE."),
    _e("identity.user_credential", (P.AUTHENTICATION_SECURITY,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE,
       notes="Argon2id hash. Never exported."),
    _e("identity.auth_session", (P.AUTHENTICATION_SECURITY, P.PSEUDONYMOUS_IDENTIFIER),
       S.OPERATIONAL, R.SHORT_OPERATIONAL, D.CASCADE_DELETE,
       notes="Refresh-token hashes and source addresses. Must be revoked at "
             "DELETION_REQUESTED, not merely deleted at the end."),
    _e("identity.password_reset_token", (P.AUTHENTICATION_SECURITY,),
       S.OPERATIONAL, R.SHORT_OPERATIONAL, D.CASCADE_DELETE),
    _e("identity.email_verification_token", (P.AUTHENTICATION_SECURITY,),
       S.OPERATIONAL, R.SHORT_OPERATIONAL, D.CASCADE_DELETE),
    _e("identity.mfa_method", (P.AUTHENTICATION_SECURITY, P.USER_FREE_TEXT),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE,
       notes="`label` is user free text and may name a device."),
    _e("identity.login_event",
       (P.AUDIT_SECURITY_RECORD, P.DIRECT_IDENTIFIER, P.PSEUDONYMOUS_IDENTIFIER),
       S.AUDIT, R.BOUNDED_AUDIT, D.DE_IDENTIFY,
       notes="FK already SET NULL on user delete — but `email_tried` and "
             "`ip_address` remain in the clear, so severing user_id alone does "
             "NOT de-identify it. See specification §25 and defect PD-3."),

    _e("identity.account_lifecycle",
       (P.ACCOUNT_IDENTITY, P.AUDIT_SECURITY_RECORD),
       S.AUDIT, R.BOUNDED_AUDIT, D.RETAIN, rls=True, exportable=True,
       notes="Entry 11B1. One row per account undergoing privacy deletion; the "
             "absence of a row means active. RETAIN on account deletion is the "
             "point — this row IS the deletion record, and a restored backup "
             "needs it to know what to re-delete. Carries no email, no "
             "financial value and no exception text: failure is a closed code."),
    _e("identity.account_lifecycle_event",
       (P.AUDIT_SECURITY_RECORD, P.PSEUDONYMOUS_IDENTIFIER),
       S.AUDIT, R.BOUNDED_AUDIT, D.RETAIN, rls=True,
       notes="Append-only lifecycle transitions. Deliberately carries NO "
             "foreign key to the account, because it must outlive the account "
             "it describes — that is what makes the backup-restore invariant "
             "implementable. Closed event codes, states and worker ids only."),

    # ----------------------------------------------------------------- profile
    _e("profile.user_profile", (P.DIRECT_IDENTIFIER, P.TAX_PROFILE_DATA),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True, notes="`display_name` is a direct identifier."),
    _e("profile.tax_profile", (P.TAX_PROFILE_DATA, P.USER_FREE_TEXT),
       S.SOURCE, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       exportable=True,
       notes="`employer_name` is user free text and an employment identifier. "
             "NOT present in the frozen snapshot — see §10."),
    _e("profile.spouse_profile", (P.TAX_PROFILE_DATA, P.FINANCIAL_SOURCE_DATA),
       S.SOURCE, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       exportable=True,
       notes="Third-party data: describes someone who is not the account "
             "holder and cannot consent through this account."),
    _e("profile.dependent", (P.TAX_PROFILE_DATA, P.USER_FREE_TEXT),
       S.SOURCE, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       exportable=True, notes="Third-party data, frequently about children."),
    _e("profile.user_preference", (P.ACCOUNT_IDENTITY,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True),
    _e("profile.user_privacy_setting", (P.ACCOUNT_IDENTITY, P.AUDIT_SECURITY_RECORD),
       S.SOURCE, R.BOUNDED_AUDIT, D.DE_IDENTIFY, rls=True, exportable=True,
       notes="A record of consent choices. Deleting it destroys the evidence "
             "of what the user consented to — LEGAL_REVIEW_REQUIRED."),

    # ----------------------------------------------------------------- finance
    *(_e(t, (P.FINANCIAL_SOURCE_DATA, P.USER_FREE_TEXT),
         S.SOURCE, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
         exportable=True,
         notes="`source_name` and `notes` are user free text; the amount is "
               "financial source data. Partitioned by tax year, so tax-year "
               "deletion is a partition-scoped operation.")
      for t in ("finance.income_source", "finance.income_source_default",
                "finance.income_source_y2024", "finance.income_source_y2025")),
    *(_e(t, (P.FINANCIAL_SOURCE_DATA, P.USER_FREE_TEXT),
         S.SOURCE, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
         exportable=True,
         notes="`description` and `notes` are user free text.")
      for t in ("finance.expense_record", "finance.expense_record_default",
                "finance.expense_record_y2024", "finance.expense_record_y2025")),

    # ------------------------------------------------------------------ wealth
    _e("wealth.asset", (P.FINANCIAL_SOURCE_DATA, P.USER_FREE_TEXT),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True),
    _e("wealth.asset_valuation", (P.FINANCIAL_SOURCE_DATA,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True, exportable=True,
       notes="NO RLS: reachable only through `wealth.asset`. See defect PD-1."),
    _e("wealth.liability", (P.FINANCIAL_SOURCE_DATA, P.USER_FREE_TEXT),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True),
    _e("wealth.liability_balance", (P.FINANCIAL_SOURCE_DATA,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True, exportable=True,
       notes="NO RLS. See defect PD-1."),
    _e("wealth.registered_account_detail", (P.FINANCIAL_SOURCE_DATA, P.TAX_PROFILE_DATA),
       S.SOURCE, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True, exportable=True,
       notes="RRSP/TFSA contribution room. NO RLS. See defect PD-1."),

    # -------------------------------------------------------------- documents
    _e("docs.document",
       (P.DOCUMENT_BINARY, P.USER_FREE_TEXT, P.PSEUDONYMOUS_IDENTIFIER),
       S.SOURCE, R.TAX_YEAR_RETENTION, D.CUSTOM_WORKFLOW, rls=True,
       exportable=True,
       notes="The row is metadata; the bytes are in object storage and are NOT "
             "removed by any database cascade. `object_key` embeds the "
             "user-supplied filename — see defect PD-2. `deleted_at` exists "
             "and is unused."),
    _e("docs.document_extraction", (P.DOCUMENT_EXTRACTED_DATA,),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       notes="NO RLS. See defect PD-1."),
    _e("docs.extraction_field",
       (P.DOCUMENT_EXTRACTED_DATA, P.FINANCIAL_SOURCE_DATA),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True, exportable=True,
       notes="`value_number` carries amounts read off a tax slip; "
             "`bounding_box` locates them on the page. NO RLS — this is the "
             "most sensitive unprotected table. See defect PD-1."),
    _e("docs.document_link", (P.PSEUDONYMOUS_IDENTIFIER,),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       notes="Provenance edge from a confirmed fact back to its document. "
             "Deleting a document must decide this edge's fate explicitly — "
             "an orphaned link claims evidence that no longer exists."),

    # --------------------------------------------------------------- analysis
    _e("analysis.analysis_run", (P.DERIVED_TAX_RESULT,),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CUSTOM_WORKFLOW, rls=True,
       replay_dependency=True, exportable=True),
    _e("analysis.analysis_input_snapshot",
       (P.DERIVED_TAX_INPUT, P.FINANCIAL_SOURCE_DATA, P.SEALED_EVIDENCE),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CUSTOM_WORKFLOW, rls=True,
       replay_dependency=True, immutable=True,
       notes="THE frozen snapshot: 27 engine inputs plus its hash. Contains no "
             "identifier and no free text (§10), but every financial figure. "
             "Erasing it makes replay impossible — see §40. NO RLS: defect "
             "PD-1, and the highest-value row in that finding."),
    _e("analysis.analysis_line_item", (P.DERIVED_TAX_RESULT,),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       replay_dependency=True, exportable=True,
       notes="Computed amounts with display labels. NO RLS. See PD-1."),
    _e("analysis.analysis_assumption", (P.DERIVED_TAX_INPUT, P.USER_FREE_TEXT),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       notes="Free-text `text` column inside sealed evidence. NO RLS."),
    _e("analysis.reconciliation_check", (P.DERIVED_TAX_RESULT,),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       notes="`detail` is free text produced by the system, not the user."),

    # ------------------------------------------------------------------- IOE
    _e("ioe.optimization_run", (P.DERIVED_TAX_RESULT, P.SEALED_EVIDENCE),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CUSTOM_WORKFLOW, rls=True,
       replay_dependency=True, immutable=True, exportable=True),
    _e("ioe.scenario", (P.DERIVED_TAX_RESULT, P.USER_FREE_TEXT, P.SEALED_EVIDENCE),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CUSTOM_WORKFLOW, rls=True,
       replay_dependency=True, exportable=True,
       notes="`label` and `note` are user free text on an otherwise sealed row."),
    *(_e(t, (P.DERIVED_TAX_RESULT, P.SEALED_EVIDENCE),
         S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
         replay_dependency=True, immutable=True,
         notes="Sealed optimization/scenario evidence child. RLS present "
               "(Entry 3B) — the pattern the analysis/docs/wealth children "
               "should follow.")
      for t in ("ioe.candidate_cost", "ioe.candidate_economic_effect",
                "ioe.confidence_component", "ioe.multi_year_projection",
                "ioe.optimization_candidate", "ioe.portfolio_evaluation_step",
                "ioe.portfolio_exclusion", "ioe.portfolio_member",
                "ioe.recommendation_relationship", "ioe.resource_ledger_entry",
                "ioe.run_rule_version", "ioe.scenario_assumption",
                "ioe.scenario_confidence_component", "ioe.scenario_input_change",
                "ioe.scenario_lever", "ioe.scenario_result",
                "ioe.score_component", "ioe.strategy_portfolio")),
    _e("ioe.optimization_run_event", (P.OPERATIONAL_TELEMETRY,),
       S.AUDIT, R.BOUNDED_AUDIT, D.CASCADE_DELETE, rls=True,
       notes="Closed reason codes only."),
    _e("ioe.scenario_event", (P.OPERATIONAL_TELEMETRY,),
       S.AUDIT, R.BOUNDED_AUDIT, D.CASCADE_DELETE, rls=True),
    _e("ioe.run_rule_snapshot", (P.PSEUDONYMOUS_IDENTIFIER,),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       replay_dependency=True,
       notes="Join from a run to the shared rule snapshot. Carries no personal "
             "data but links a user's run to a version manifest. NO RLS."),
    _e("ioe.integrity_check", (P.AUDIT_SECURITY_RECORD, P.PSEUDONYMOUS_IDENTIFIER),
       S.AUDIT, R.BOUNDED_AUDIT, D.DE_IDENTIFY, rls=True,
       notes="Replay-verification history. Survives de-identified so the "
             "platform can still show that verification ran; see §20 and §40 "
             "for what its status must become once evidence is erased."),
    _e("ioe.freshness_outbox", (P.OPERATIONAL_TELEMETRY, P.PSEUDONYMOUS_IDENTIFIER),
       S.OPERATIONAL, R.SHORT_OPERATIONAL, D.CASCADE_DELETE, rls=True,
       notes="Carries user_id and closed reason codes, never values."),

    # ------------------------------------------------------- recommendations
    _e("reco.recommendation", (P.RECOMMENDATION_DATA, P.DERIVED_TAX_RESULT),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, rls=True,
       exportable=True),
    _e("reco.recommendation_feedback", (P.RECOMMENDATION_DATA, P.USER_FREE_TEXT),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True, notes="`comment` is unbounded user free text."),
    _e("reco.recommendation_status_event", (P.RECOMMENDATION_DATA, P.USER_FREE_TEXT),
       S.AUDIT, R.BOUNDED_AUDIT, D.DE_IDENTIFY, rls=True,
       notes="FK already SET NULL on user delete; `note` free text must be "
             "cleared too or de-identification is incomplete. NO RLS."),

    # ------------------------------------------------------------------- AI
    _e("ai.ai_conversation", (P.USER_FREE_TEXT,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True, notes="`title` is derived from the first question."),
    _e("ai.ai_message", (P.USER_FREE_TEXT, P.DERIVED_TAX_RESULT),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True, exportable=True,
       notes="Both sides of the conversation: the user's question verbatim and "
             "the generated answer. NO RLS. See defect PD-1."),
    _e("ai.ai_prompt_context", (P.DERIVED_TAX_RESULT, P.FINANCIAL_SOURCE_DATA),
       S.DERIVED, R.SHORT_OPERATIONAL, D.CASCADE_DELETE, rls=True,
       notes="JSONB holding the verified figures that were put in front of the "
             "model — taxable income, tax, savings, marginal rate. A debugging "
             "artifact holding financial data with NO RLS. See PD-1; a strong "
             "candidate for the shortest retention in the system."),
    _e("ai.ai_message_citation", (P.PSEUDONYMOUS_IDENTIFIER,),
       S.DERIVED, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       notes="Links a message to rule versions and to an analysis. Its FK to "
             "`analysis.analysis_run` is NO ACTION and will BLOCK deletion of "
             "an analysis — see the dependency graph, §23."),

    # -------------------------------------------------------------- billing
    _e("billing.subscription", (P.ACCOUNT_IDENTITY, P.PSEUDONYMOUS_IDENTIFIER),
       S.SOURCE, R.RETAINED_PENDING_REVIEW, D.RETAIN, rls=True, exportable=True,
       notes="Provider identifiers. Financial-record retention is "
             "LEGAL_REVIEW_REQUIRED."),
    _e("billing.invoice", (P.FINANCIAL_SOURCE_DATA, P.PSEUDONYMOUS_IDENTIFIER),
       S.AUDIT, R.RETAINED_PENDING_REVIEW, D.RETAIN, rls=True, exportable=True,
       notes="Amounts and a provider invoice id. NO RLS. Almost certainly "
             "subject to a statutory retention period — LEGAL_REVIEW_REQUIRED."),
    _e("billing.entitlement", (P.ACCOUNT_IDENTITY,),
       S.DERIVED, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True),
    _e("billing.payment_method_ref", (P.PSEUDONYMOUS_IDENTIFIER,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       notes="A reference to a provider-held instrument, not card data. "
             "Deletion must also revoke it at the provider — "
             "EXTERNAL_PROVIDER_REVIEW_REQUIRED."),

    # ---------------------------------------------------------------- audit
    _e("audit.security_event", (P.AUDIT_SECURITY_RECORD,),
       S.AUDIT, R.BOUNDED_AUDIT, D.DE_IDENTIFY,
       notes="FK already SET NULL. `detail` is JSONB and unvalidated — it can "
             "carry anything a caller put in it. NO RLS."),
    _e("audit.consent_log", (P.AUDIT_SECURITY_RECORD,),
       S.AUDIT, R.BOUNDED_AUDIT, D.DE_IDENTIFY,
       notes="Evidence of consent. Erasing it destroys the proof that consent "
             "was given — LEGAL_REVIEW_REQUIRED."),
    _e("audit.data_deletion_request", (P.AUDIT_SECURITY_RECORD, P.USER_FREE_TEXT),
       S.AUDIT, R.BOUNDED_AUDIT, D.RETAIN,
       notes="EXISTS AND IS UNUSED — no service, no endpoint. This is the "
             "natural home for the Entry 11B deletion ledger. Its FK to the "
             "user is CASCADE, which is wrong for a record that must OUTLIVE "
             "the account it describes: see §30 and §48."),
    _e("audit.data_export_request", (P.AUDIT_SECURITY_RECORD,),
       S.AUDIT, R.BOUNDED_AUDIT, D.RETAIN,
       notes="EXISTS AND IS UNUSED. Same CASCADE problem as the deletion "
             "request table."),

    # --- the audit log itself (PD-4, Entry 11B0) -----------------------------
    # Entry 11A named this table as PD-4 and classified its RLS exception, but
    # never gave it a LIFECYCLE entry — so the registry that decides what
    # happens to user-derived data on deletion had nothing to say about the one
    # table the defect was about. That is what §10 of Entry 11B0 calls the
    # prose and the registry drifting apart, and it is fixed here rather than
    # noted.
    #
    # Entry 11B0 removed the personal VALUES from the payload. What remains is
    # pseudonymous: an actor id, an entity id, ownership ids, timestamps,
    # workflow states, and — on sealed relations — content addresses. That is
    # PSEUDONYMOUS_IDENTIFIER, not anonymous, and de-identification is
    # therefore still owed at account deletion.
    *(_e(f"audit.{t}",
         (P.AUDIT_SECURITY_RECORD, P.PSEUDONYMOUS_IDENTIFIER),
         S.AUDIT, R.BOUNDED_AUDIT, D.DE_IDENTIFY,
         immutable=True, rls=False,
         notes="Append-only, partitioned by month, NO foreign key to the "
               "account. Not reachable by cascade, so DE_IDENTIFY must be "
               "driven by the composite ownership key (PD-15): actor_id, the "
               "payload's user_id, and — on user_account rows — the payload's "
               "id. Registration and login run anonymously, so actor_id alone "
               "misses the account and credential rows. Payload VALUES are "
               "minimized at write time by audit.log_change (PD-4); rows "
               "written before migration 0048 need "
               "scripts/audit_payload_scan.py.")
      for t in ("audit_log", "audit_log_2025m01", "audit_log_2025m02",
                "audit_log_default")),

    # NOTE: `admin.admin_user_role` is deliberately ABSENT. An early draft
    # listed it, because the survey that produced this registry seeded on any
    # column matching `%admin_id` as well as `user_id`. The enforcement test
    # rejected it, correctly: it keys on an OPERATOR principal, is not
    # reachable from `identity.user_account`, and contains no customer data.
    # Operator access records are governed by the admin/four-eyes model, not by
    # the customer privacy lifecycle.
)

#: Tables that hold user-derived data but have NO foreign key to the account,
#: so foreign-key reachability cannot find them.
#:
#: Entry 11A named this blind spot when `audit.audit_log` turned out to hold
#: whole copies of user rows while being invisible to the derivation. Entry 11B1
#: creates one deliberately: `account_lifecycle_event` must OUTLIVE the account
#: it describes, so a restored backup can be told which accounts were deleted
#: after the snapshot — and a foreign key would delete exactly the record the
#: restore needs.
#:
#: Membership here is a claim that a table is user-derived despite the missing
#: edge. It must still be classified in LIFECYCLE; this set only teaches the
#: derivation to expect it.
MANUALLY_DECLARED_USER_DERIVED: frozenset[str] = frozenset({
    "identity.account_lifecycle_event",
    # PD-4's own table. Entry 11A described it as holding whole copies of user
    # rows and left it out of this set, so the derivation still could not see
    # it and the LIFECYCLE registry had no entry for it — the registry was
    # blind to precisely the table the defect was about. Declared here in
    # Entry 11B0 so a future user-derived column in the audit schema fails the
    # inventory guard like any other.
    "audit.audit_log",
    "audit.audit_log_2025m01",
    "audit.audit_log_2025m02",
    "audit.audit_log_default",
})


LIFECYCLE: dict[str, TableLifecycle] = {entry.table: entry for entry in _ENTRIES}

if len(LIFECYCLE) != len(_ENTRIES):  # pragma: no cover - construction invariant
    raise RuntimeError("duplicate table in the privacy lifecycle registry")


class NonRlsReason(StrEnum):
    """Why a table has no row-level security.

    Every table without RLS must name one. "It seemed fine" is not a category,
    and neither is silence: the point of a closed set is that an unexplained
    exception cannot exist, because there is nowhere to put it.
    """

    GLOBAL_REFERENCE_OR_REGISTRY = "GLOBAL_REFERENCE_OR_REGISTRY"
    """Published or reference data, identical for every user. Provinces,
    brackets, legislation, rule definitions."""

    GLOBAL_SYSTEM_STATE = "GLOBAL_SYSTEM_STATE"
    """Platform configuration and version state. Not about a person."""

    OPERATOR_ADMIN_STATE = "OPERATOR_ADMIN_STATE"
    """Operator accounts, roles and governance. Personal data about STAFF, not
    about customers — governed by the admin/four-eyes model."""

    CROSS_TENANT_OPERATIONAL_STATE = "CROSS_TENANT_OPERATIONAL_STATE"
    """A policy keyed on `app.user_id` would BREAK the function. Global capacity
    counting must see every row; authentication must read the account before any
    user is known."""

    PINNED_SYSTEM_EVIDENCE = "PINNED_SYSTEM_EVIDENCE"
    """Immutable snapshots of the RULES a run was pinned to — shared across
    users by construction, carrying no tenant data."""

    WORKER_AUDIT_STATE = "WORKER_AUDIT_STATE"
    """Worker claim/transition records. Codes, tokens and timings; read by
    privileged operational paths that must see across tenants."""

    PSEUDONYMOUS_OPERATIONAL_STATE = "PSEUDONYMOUS_OPERATIONAL_STATE"
    """Counters and leases keyed on an internal UUID or a keyed digest. No
    direct identifier, no financial value, no free text."""

    PRIVACY_DEFECT_REQUIRES_REMEDIATION = "PRIVACY_DEFECT_REQUIRES_REMEDIATION"
    """Tenant-owned data that SHOULD have RLS and does not. Not a
    justification — a defect with a name."""


@dataclass(frozen=True)
class NonRlsTable:
    """One table without RLS, and the reason it is allowed to be."""

    table: str
    reason: NonRlsReason
    user_derived: bool
    direct_identifier: bool
    pseudonymous_identifier: bool
    access_model: str
    note: str

    @property
    def is_defect(self) -> bool:
        return self.reason is NonRlsReason.PRIVACY_DEFECT_REQUIRES_REMEDIATION


N = NonRlsReason


def _n(table: str, reason: NonRlsReason, access: str, note: str, *,
       ud: bool = False, direct: bool = False, pseudo: bool = False) -> NonRlsTable:
    return NonRlsTable(table=table, reason=reason, user_derived=ud,
                       direct_identifier=direct, pseudonymous_identifier=pseudo,
                       access_model=access, note=note)


_GRANTS_RW = "onyx_app_rw CRUD; onyx_app_ro SELECT; PUBLIC revoked"
_GRANTS_RO = "read-only to the application; written by migrations/operators"
_ADMIN_ONLY = "admin plane only; admin JWT + require_permission"
_OWNER_ONLY = "owner/migration role only; runtime roles have no direct access"

_NON_RLS: tuple[NonRlsTable, ...] = (
    # --- published / reference data: identical for every user -----------------
    *(_n(f"ref.{t}", N.GLOBAL_REFERENCE_OR_REGISTRY, _GRANTS_RO,
         "Reference code table. No tenant data of any kind.")
      for t in ("account_registered_type", "asset_category", "condition_operator",
                "currency", "document_type", "employment_type", "expense_category",
                "housing_status", "income_type", "jurisdiction", "liability_category",
                "marital_status", "province", "residency_status", "rule_category",
                "tax_year", "verification_status")),
    *(_n(f"rules.{t}", N.GLOBAL_REFERENCE_OR_REGISTRY, _GRANTS_RO,
         "Rule-engine definition. Authored by operators, identical for all users.")
      for t in ("calc_constant", "calc_formula", "calc_formula_input",
                "condition_value_set", "condition_value_set_item", "fact_definition",
                "rule_action", "rule_condition", "rule_condition_group",
                "rule_deadline", "rule_dependency", "rule_outcome",
                "rule_required_document", "rule_shared_resource")),
    *(_n(f"tax_kb.{t}", N.GLOBAL_REFERENCE_OR_REGISTRY, _GRANTS_RO,
         "Published Canadian tax knowledge. Public law, not personal data.")
      for t in ("benefit_parameter", "benefit_program", "contribution_limit",
                "gov_source", "legislation_reference", "tax_bracket",
                "tax_bracket_set", "tax_rule", "tax_rule_version")),
    *(_n(f"tkms.{t}", N.GLOBAL_REFERENCE_OR_REGISTRY, _ADMIN_ONLY,
         "Legislation ingestion pipeline. Government documents and operator "
         "workflow; no customer data reaches it.")
      for t in ("change_item", "change_report", "dead_letter", "extracted_rule",
                "import_job", "parse_result", "raw_document", "rollback_record",
                "validation_finding", "validation_report")),

    # --- operator plane -------------------------------------------------------
    *(_n(f"admin.{t}", N.OPERATOR_ADMIN_STATE, _ADMIN_ONLY,
         "Operator identity and governance. Staff personal data, governed by "
         "the admin/four-eyes model rather than by tenant RLS.")
      for t in ("admin_user", "admin_user_role", "permission", "role",
                "role_permission", "rule_change_request", "rule_publication")),

    # --- system state ---------------------------------------------------------
    _n("ioe.active_calculation_version", N.GLOBAL_SYSTEM_STATE, _GRANTS_RW,
       "Which engine/rule versions are active platform-wide."),
    _n("ioe.weight_config", N.GLOBAL_SYSTEM_STATE, _GRANTS_RO,
       "Scoring weights. Proprietary configuration, not tenant data."),
    _n("ioe.assumption", N.GLOBAL_SYSTEM_STATE, _GRANTS_RO,
       "Named planning assumptions shared by every scenario."),
    _n("ioe.assumption_set", N.GLOBAL_SYSTEM_STATE, _GRANTS_RO,
       "Versioned collections of the above."),
    _n("billing.plan", N.GLOBAL_SYSTEM_STATE, _GRANTS_RO,
       "Product catalogue. No customer data."),
    _n("ai.knowledge_embedding", N.GLOBAL_SYSTEM_STATE, _GRANTS_RW,
       "pgvector index over PUBLISHED RULE VERSIONS only. Written exclusively "
       "with source_type='rule_version'; no user document, conversation or "
       "financial value is ever embedded. Verified in Entry 11A."),
    _n("ai.ai_explanation", N.GLOBAL_SYSTEM_STATE, _ADMIN_ONLY,
       "Operator-authored plain-language rule explanations, reviewed by an "
       "admin. Attached to rule versions, not to users."),

    # --- pinned evidence about RULES, not about people ------------------------
    _n("ioe.rule_snapshot", N.PINNED_SYSTEM_EVIDENCE, _GRANTS_RW,
       "Immutable manifest of the rule versions in force. Shared by every run "
       "that pinned it; carries no tenant column."),
    _n("ioe.rule_snapshot_artifact", N.PINNED_SYSTEM_EVIDENCE, _GRANTS_RW,
       "Content-addressed artifact of the above."),

    # --- worker/operational ---------------------------------------------------
    _n("ioe.freshness_outbox_audit", N.WORKER_AUDIT_STATE, _GRANTS_RW,
       "Worker claim transitions: event id, worker id, claim token, closed "
       "error code. No user column, no values.", pseudo=True),
    _n("admission.rate_counter", N.PSEUDONYMOUS_OPERATIONAL_STATE, _GRANTS_RW,
       "Fixed-window counters. RLS would BREAK global capacity accounting — a "
       "policy keyed on app.user_id makes the platform-wide count return only "
       "the caller's rows, so the limiter stops limiting exactly when the "
       "platform is busiest. scope_id is an internal UUID or an HMAC-SHA256 "
       "digest under a dedicated secret; never a plaintext address. Purged "
       "hourly (2h counters / 24h leases).", pseudo=True),
    _n("admission.lease", N.PSEUDONYMOUS_OPERATIONAL_STATE, _GRANTS_RW,
       "In-flight operation slots. Same argument and same key discipline as "
       "rate_counter.", pseudo=True),

    # --- cross-tenant by necessity -------------------------------------------
    *(_n(f"identity.{t}", N.CROSS_TENANT_OPERATIONAL_STATE, _GRANTS_RW,
         "Authentication reads these BEFORE app.user_id is set — unit_of_work "
         "deliberately leaves the GUC unset for anonymous sessions — so a "
         "policy keyed on it would deny the login lookup outright. Protected "
         "by grants and by the service layer.", ud=True, pseudo=True)
      for t in ("auth_session", "email_verification_token", "mfa_method",
                "password_reset_token", "user_credential")),
    _n("identity.user_account", N.CROSS_TENANT_OPERATIONAL_STATE, _GRANTS_RW,
       "Same pre-authentication argument. Holds the email address — the "
       "product's one unavoidable direct identifier.", ud=True, direct=True),
    _n("identity.login_event", N.CROSS_TENANT_OPERATIONAL_STATE, _GRANTS_RW,
       "Written during authentication, before a principal exists. Retains "
       "email_tried and ip_address after the user FK is nulled — see PD-3.",
       ud=True, direct=True, pseudo=True),
    *(_n(f"audit.{t}", N.WORKER_AUDIT_STATE, _OWNER_ONLY,
         "Security/governance record read by privileged paths that must see "
         "across tenants. onyx_app_rw has no SELECT; the audit trigger writes "
         "through SECURITY DEFINER.", ud=True, pseudo=True)
      for t in ("consent_log", "data_deletion_request", "data_export_request",
                "security_event")),
    *(_n(f"audit.{t}", N.WORKER_AUDIT_STATE, _OWNER_ONLY,
         "Append-only change log. Reachable only by the owner role — but it "
         "holds whole copies of user rows, which is PD-4 and is an 11B0 "
         "workstream. The RLS exception itself is justified; the CONTENT is "
         "not.", ud=True, direct=True, pseudo=True)
      for t in ("audit_log", "audit_log_2025m01", "audit_log_2025m02",
                "audit_log_default")),

    # --- PD-1: closed in Entry 11B1 -------------------------------------------
    # These sixteen tenant-owned child tables used to live here as
    # PRIVACY_DEFECT_REQUIRES_REMEDIATION. They now carry RLS, FORCE RLS and a
    # FOR ALL policy with both USING and WITH CHECK, resolved through their
    # parent to ref.current_app_user() — so they are no longer non-RLS tables
    # and have no entry in this registry at all.
    #
    # The entries were removed only after the isolation was proven: removing
    # the RLS makes tests/security/test_pd1_tenant_isolation.py fail 81 times,
    # and test_the_non_rls_registry_describes_no_table_that_gained_rls fails if
    # a table listed here has policies in the live database.
)

NON_RLS: dict[str, NonRlsTable] = {entry.table: entry for entry in _NON_RLS}

if len(NON_RLS) != len(_NON_RLS):  # pragma: no cover - construction invariant
    raise RuntimeError("duplicate table in the non-RLS justification registry")


class StorageKind(StrEnum):
    """A place user data can live that is NOT a PostgreSQL table.

    The table registry above is derived from `pg_catalog`, which is exactly why
    it cannot see these: object storage, a broker, a log stream and a second
    application's datastore have no foreign keys to walk. They are enumerated by
    hand — and the closeout pass found one that had been missed entirely, the
    Node application's store, so this list exists to make the next omission
    visible rather than silent.
    """

    RELATIONAL_DATABASE = "RELATIONAL_DATABASE"
    OBJECT_STORAGE = "OBJECT_STORAGE"
    EXTERNAL_MANAGED_STORE = "EXTERNAL_MANAGED_STORE"
    LOCAL_FILE = "LOCAL_FILE"
    MESSAGE_BROKER = "MESSAGE_BROKER"
    RESULT_BACKEND = "RESULT_BACKEND"
    LOG_STREAM = "LOG_STREAM"
    IN_PROCESS_METRICS = "IN_PROCESS_METRICS"
    VECTOR_INDEX = "VECTOR_INDEX"
    EXTERNAL_PROVIDER = "EXTERNAL_PROVIDER"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


@dataclass(frozen=True)
class StorageSurface:
    """One place user data can be, and what is true about it."""

    name: str
    application: str
    kind: StorageKind
    live_user_data: bool
    derived_user_data: bool
    direct_identifiers: bool
    financial_data: bool
    document_data: bool
    #: Is retention decided by something in THIS repository?
    retention_in_repo: bool
    #: Does a deletion mechanism exist today?
    deletion_exists: bool
    note: str


K = StorageKind

_SURFACES: tuple[StorageSurface, ...] = (
    StorageSurface(
        "PostgreSQL", "backend", K.RELATIONAL_DATABASE,
        live_user_data=True, derived_user_data=True, direct_identifiers=True,
        financial_data=True, document_data=False,
        retention_in_repo=False, deletion_exists=False,
        note="148 tables, 75 user-derived. Direct identifiers in exactly three "
             "tables. Document BYTES are not here, only metadata. No deletion "
             "capability exists (PD-7).",
    ),
    StorageSurface(
        "Object storage (documents, legislation)", "backend", K.OBJECT_STORAGE,
        live_user_data=True, derived_user_data=False, direct_identifiers=True,
        financial_data=False, document_data=True,
        retention_in_repo=False, deletion_exists=False,
        note="Adapter is an in-memory fake; a real bucket is deployment "
             "configuration. Document keys embed the user-supplied filename "
             "(PD-2), and the port has no delete method at all (PD-8).",
    ),
    StorageSurface(
        "Netlify Blobs", "server (Node/Express)", K.EXTERNAL_MANAGED_STORE,
        live_user_data=True, derived_user_data=True, direct_identifiers=True,
        financial_data=False, document_data=True,
        retention_in_repo=False, deletion_exists=True,
        note="THE DEPLOYED STORE. Root netlify.toml deploys `server/`, which "
             "persists email, name, bcrypt passwordHash, tax profile, documents "
             "and an audit result per user. Absent from the first inventory "
             "pass — PD-14. `deleteDocument` exists, which is more deletion "
             "than the Python backend has. Whether it is live is "
             "OPERATIONAL_REVIEW_REQUIRED.",
    ),
    StorageSurface(
        "server/data/db.json", "server (Node/Express)", K.LOCAL_FILE,
        live_user_data=False, derived_user_data=False, direct_identifiers=True,
        financial_data=False, document_data=True,
        retention_in_repo=False, deletion_exists=False,
        note="The FileStore backend, used for local `npm start` and tests. "
             "GITIGNORED AND UNTRACKED — verified with git check-ignore and "
             "git ls-files. The working copy holds a demo session only: safe "
             "aggregates show 7 users on one email domain inside an 8-minute "
             "window. Contents are never read into documentation or logs.",
    ),
    StorageSurface(
        "Redis - Celery broker", "backend", K.MESSAGE_BROKER,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=False, deletion_exists=False,
        note="Task payloads are identifiers, integers and closed codes only; "
             "two tasks carry a user_id, which is the minimum to do the work. "
             "Broker persistence and eviction are DEPLOYMENT_REVIEW_REQUIRED.",
    ),
    StorageSurface(
        "Redis - Celery result backend", "backend", K.RESULT_BACKEND,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=True, deletion_exists=False,
        note="DISABLED in the closeout (PD-12). Before that, a failed task's "
             "exception was serialized here verbatim, and SQLAlchemy renders a "
             "DBAPIError as the statement plus its bound parameters — so a "
             "database error during a financial write persisted the amount. "
             "task_ignore_result is now on, errors are not stored even when "
             "ignored, and result_expires is an explicit 24h rather than the "
             "framework default it silently was.",
    ),
    StorageSurface(
        "Application logs", "backend", K.LOG_STREAM,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=False, deletion_exists=False,
        note="40 structured call sites, closed codes. Carries user_id and "
             "request paths containing entity UUIDs — RESTRICTED_IDENTIFIER, "
             "not harmless. Retention and access are "
             "DEPLOYMENT_REVIEW_REQUIRED. Celery's own worker log still "
             "receives raw exception text (PD-13).",
    ),
    StorageSurface(
        "Metrics", "backend", K.IN_PROCESS_METRICS,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=True, deletion_exists=False,
        note="In-process counters keyed on operation and reason codes. No "
             "exporter exists, so nothing leaves the process. No user id, "
             "hash, email or financial value appears in any label.",
    ),
    StorageSurface(
        "pgvector knowledge index", "backend", K.VECTOR_INDEX,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=True, deletion_exists=True,
        note="Written exclusively with source_type='rule_version' — published "
             "legislation. No user document, conversation or financial value "
             "is embedded, so there are no orphaned user-derived vectors to "
             "delete. Re-indexing is idempotent.",
    ),
    StorageSurface(
        "AI provider", "backend", K.EXTERNAL_PROVIDER,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=False, deletion_exists=False,
        note="NONE EXISTS. get_llm_client() unconditionally returns an "
             "in-process TemplateLlmClient; no HTTP client is present anywhere "
             "in production code. No user data leaves the process today. "
             "Everything about a future provider is "
             "EXTERNAL_PROVIDER_REVIEW_REQUIRED.",
    ),
    StorageSurface(
        "Temporary local files", "backend", K.NOT_IMPLEMENTED,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=True, deletion_exists=False,
        note="None exist. No tempfile, no /tmp, no local disk write anywhere "
             "in the request or worker path.",
    ),
    StorageSurface(
        "Backups / PITR", "backend", K.NOT_IMPLEMENTED,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=False, deletion_exists=False,
        note="Not implemented. The restore invariant is a mandatory acceptance "
             "criterion for the future backup entry: a backup restored from "
             "before a deletion must replay the deletion ledger before the "
             "service accepts traffic.",
    ),
    StorageSurface(
        "User data export", "backend", K.NOT_IMPLEMENTED,
        live_user_data=False, derived_user_data=False, direct_identifiers=False,
        financial_data=False, document_data=False,
        retention_in_repo=False, deletion_exists=False,
        note="Not implemented. audit.data_export_request exists and is unused; "
             "32 tables are marked exportable in LIFECYCLE.",
    ),
)

STORAGE_SURFACES: dict[str, StorageSurface] = {s.name: s for s in _SURFACES}

if len(STORAGE_SURFACES) != len(_SURFACES):  # pragma: no cover
    raise RuntimeError("duplicate storage surface")


# ---------------------------------------------------------------------------
# How other applications relate to THIS application's account lifecycle
# (Entry 11B1 §19)
# ---------------------------------------------------------------------------
class ApplicationLifecycleRelation(StrEnum):
    """What a deletion request in the Python backend means for another
    application that also holds accounts."""

    #: One account, one deletion. A request here must purge there too.
    SAME_ACCOUNT_LIFECYCLE = "SAME_ACCOUNT_LIFECYCLE"
    #: Its own accounts, its own store, its own deletion. A request here does
    #: nothing there, and saying otherwise would be a false completion.
    SEPARATE_APPLICATION = "SEPARATE_APPLICATION"
    DEMO_DEVELOPMENT_ONLY = "DEMO_DEVELOPMENT_ONLY"
    LEGACY_UNUSED = "LEGACY_UNUSED"
    UNKNOWN_REQUIRES_OPERATIONAL_REVIEW = "UNKNOWN_REQUIRES_OPERATIONAL_REVIEW"


@dataclass(frozen=True)
class ApplicationRelation:
    application: str
    relation: ApplicationLifecycleRelation
    #: What in the repository establishes it.
    evidence: tuple[str, ...]
    #: What is still an operational fact rather than a repository fact.
    operational_question: str | None
    #: What a deletion request in this backend actually does to it.
    effect_of_backend_deletion: str


#: The Node/Express application under `server/`.
#:
#: The determinable answer is SEPARATE_APPLICATION, and it is determinable
#: from the repository rather than inferred: the two systems share no code, no
#: database, no identifier and no call. `server/` authenticates with its own
#: bcrypt hashes and its own JWTs against its own store, and nothing in it
#: names the Python API.
#:
#: What is NOT determinable here is whether the deployed instance currently
#: holds real people's data. That is an operational fact about a running
#: Netlify site, and no amount of reading this repository settles it.
NODE_APPLICATION = ApplicationRelation(
    application="server (Node/Express)",
    relation=ApplicationLifecycleRelation.SEPARATE_APPLICATION,
    evidence=(
        "server/ contains no reference to the Python API — no base URL, no "
        "api/v1 path, no shared client. Verified by search.",
        "It authenticates independently: bcryptjs password hashes and "
        "jsonwebtoken sessions, in its own store.",
        "It persists its own users — email, name, passwordHash, tax profile, "
        "documents — to Netlify Blobs, keyed by its own generated ids.",
        "Root netlify.toml deploys it: base `server`, publish `public`, the "
        "Express app as a serverless function. It is configured to run.",
        "No GitHub Actions workflow builds, tests or deploys it, so the "
        "repository's gates say nothing about it either way.",
        "It carries no lifecycle table and no notion of a deletion request; "
        "its `deleteUser` is an immediate store deletion.",
    ),
    operational_question=(
        "Is the deployed instance live, and does it hold data belonging to "
        "real people rather than the demo session visible locally? The "
        "untracked FileStore holds 7 accounts on a single email domain inside "
        "an 8-minute window, which reads as a demo — but the deployed Blobs "
        "store is a different store and this repository cannot see it."
    ),
    effect_of_backend_deletion=(
        "NOTHING. A deletion requested through this backend does not reach the "
        "Node application's store, and no phase of Entry 11B plans to. An "
        "account deleted here may still exist there, under the same email "
        "address, with a password hash and uploaded documents."
    ),
)


__all__ = [
    "LIFECYCLE",
    "NODE_APPLICATION",
    "MANUALLY_DECLARED_USER_DERIVED",
    "NON_RLS",
    "STORAGE_SURFACES",
    "ApplicationLifecycleRelation",
    "ApplicationRelation",
    "DeletionAction",
    "LifecycleState",
    "NonRlsReason",
    "NonRlsTable",
    "PrivacyClass",
    "StorageKind",
    "StorageSurface",
    "RetentionClass",
    "SourceKind",
    "TableLifecycle",
]
