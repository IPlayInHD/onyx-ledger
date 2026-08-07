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
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, exportable=True,
       notes="NO RLS: reachable only through `wealth.asset`. See defect PD-1."),
    _e("wealth.liability", (P.FINANCIAL_SOURCE_DATA, P.USER_FREE_TEXT),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True),
    _e("wealth.liability_balance", (P.FINANCIAL_SOURCE_DATA,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, exportable=True,
       notes="NO RLS. See defect PD-1."),
    _e("wealth.registered_account_detail", (P.FINANCIAL_SOURCE_DATA, P.TAX_PROFILE_DATA),
       S.SOURCE, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, exportable=True,
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
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE,
       notes="NO RLS. See defect PD-1."),
    _e("docs.extraction_field",
       (P.DOCUMENT_EXTRACTED_DATA, P.FINANCIAL_SOURCE_DATA),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE, exportable=True,
       notes="`value_number` carries amounts read off a tax slip; "
             "`bounding_box` locates them on the page. NO RLS — this is the "
             "most sensitive unprotected table. See defect PD-1."),
    _e("docs.document_link", (P.PSEUDONYMOUS_IDENTIFIER,),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE,
       notes="Provenance edge from a confirmed fact back to its document. "
             "Deleting a document must decide this edge's fate explicitly — "
             "an orphaned link claims evidence that no longer exists."),

    # --------------------------------------------------------------- analysis
    _e("analysis.analysis_run", (P.DERIVED_TAX_RESULT,),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CUSTOM_WORKFLOW, rls=True,
       replay_dependency=True, exportable=True),
    _e("analysis.analysis_input_snapshot",
       (P.DERIVED_TAX_INPUT, P.FINANCIAL_SOURCE_DATA, P.SEALED_EVIDENCE),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CUSTOM_WORKFLOW,
       replay_dependency=True, immutable=True,
       notes="THE frozen snapshot: 27 engine inputs plus its hash. Contains no "
             "identifier and no free text (§10), but every financial figure. "
             "Erasing it makes replay impossible — see §40. NO RLS: defect "
             "PD-1, and the highest-value row in that finding."),
    _e("analysis.analysis_line_item", (P.DERIVED_TAX_RESULT,),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE,
       replay_dependency=True, exportable=True,
       notes="Computed amounts with display labels. NO RLS. See PD-1."),
    _e("analysis.analysis_assumption", (P.DERIVED_TAX_INPUT, P.USER_FREE_TEXT),
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE,
       notes="Free-text `text` column inside sealed evidence. NO RLS."),
    _e("analysis.reconciliation_check", (P.DERIVED_TAX_RESULT,),
       S.DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE,
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
       S.SEALED_DERIVED, R.TAX_YEAR_RETENTION, D.CASCADE_DELETE,
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
       S.AUDIT, R.BOUNDED_AUDIT, D.DE_IDENTIFY,
       notes="FK already SET NULL on user delete; `note` free text must be "
             "cleared too or de-identification is incomplete. NO RLS."),

    # ------------------------------------------------------------------- AI
    _e("ai.ai_conversation", (P.USER_FREE_TEXT,),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, rls=True,
       exportable=True, notes="`title` is derived from the first question."),
    _e("ai.ai_message", (P.USER_FREE_TEXT, P.DERIVED_TAX_RESULT),
       S.SOURCE, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE, exportable=True,
       notes="Both sides of the conversation: the user's question verbatim and "
             "the generated answer. NO RLS. See defect PD-1."),
    _e("ai.ai_prompt_context", (P.DERIVED_TAX_RESULT, P.FINANCIAL_SOURCE_DATA),
       S.DERIVED, R.SHORT_OPERATIONAL, D.CASCADE_DELETE,
       notes="JSONB holding the verified figures that were put in front of the "
             "model — taxable income, tax, savings, marginal rate. A debugging "
             "artifact holding financial data with NO RLS. See PD-1; a strong "
             "candidate for the shortest retention in the system."),
    _e("ai.ai_message_citation", (P.PSEUDONYMOUS_IDENTIFIER,),
       S.DERIVED, R.WHILE_ACCOUNT_ACTIVE, D.CASCADE_DELETE,
       notes="Links a message to rule versions and to an analysis. Its FK to "
             "`analysis.analysis_run` is NO ACTION and will BLOCK deletion of "
             "an analysis — see the dependency graph, §23."),

    # -------------------------------------------------------------- billing
    _e("billing.subscription", (P.ACCOUNT_IDENTITY, P.PSEUDONYMOUS_IDENTIFIER),
       S.SOURCE, R.RETAINED_PENDING_REVIEW, D.RETAIN, rls=True, exportable=True,
       notes="Provider identifiers. Financial-record retention is "
             "LEGAL_REVIEW_REQUIRED."),
    _e("billing.invoice", (P.FINANCIAL_SOURCE_DATA, P.PSEUDONYMOUS_IDENTIFIER),
       S.AUDIT, R.RETAINED_PENDING_REVIEW, D.RETAIN, exportable=True,
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

    # NOTE: `admin.admin_user_role` is deliberately ABSENT. An early draft
    # listed it, because the survey that produced this registry seeded on any
    # column matching `%admin_id` as well as `user_id`. The enforcement test
    # rejected it, correctly: it keys on an OPERATOR principal, is not
    # reachable from `identity.user_account`, and contains no customer data.
    # Operator access records are governed by the admin/four-eyes model, not by
    # the customer privacy lifecycle.
)

LIFECYCLE: dict[str, TableLifecycle] = {entry.table: entry for entry in _ENTRIES}

if len(LIFECYCLE) != len(_ENTRIES):  # pragma: no cover - construction invariant
    raise RuntimeError("duplicate table in the privacy lifecycle registry")


__all__ = [
    "LIFECYCLE",
    "DeletionAction",
    "LifecycleState",
    "PrivacyClass",
    "RetentionClass",
    "SourceKind",
    "TableLifecycle",
]
