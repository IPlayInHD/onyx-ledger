"""Minimal mappings for the tax KB + rules engine tables the evaluator reads."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class TaxRule(Base):
    __tablename__ = "tax_rule"
    __table_args__ = {"schema": "tax_kb"}

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    subcategory: Mapped[str | None] = mapped_column(String)
    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    province_code: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class TaxRuleVersion(Base):
    __tablename__ = "tax_rule_version"
    __table_args__ = {"schema": "tax_kb"}

    id: Mapped[uuid.UUID] = uuid_pk()
    tax_rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule.id")
    )
    tax_year: Mapped[int] = mapped_column(nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default="draft")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    ai_explanation: Mapped[str | None] = mapped_column(Text)
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    income_threshold_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    income_threshold_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    reduction_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    formula_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    gov_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_url: Mapped[str | None] = mapped_column(String)
    legislation_reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    superseded_by_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # TKMS provenance (nullable — hand-authored / legacy versions predate TKMS).
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parser_version: Mapped[str | None] = mapped_column(String)
    parser_confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    validation_report_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Opportunity contract v2: coded eligibility reasons. Its ABSENCE is the
    # signal that this version carries no contract metadata, which the evaluator
    # reports as eligibility_status='indeterminate' (never guessed).
    eligibility_basis_codes: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class FactDefinition(Base):
    __tablename__ = "fact_definition"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    fact_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    data_type: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    resolver_hint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class CalcFormula(Base):
    __tablename__ = "calc_formula"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    expression_lang: Mapped[str] = mapped_column(String, default="rpn")
    output_unit: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class CalcFormulaInput(Base):
    __tablename__ = "calc_formula_input"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    formula_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rules.calc_formula.id", ondelete="CASCADE")
    )
    param_name: Mapped[str] = mapped_column(String, nullable=False)
    fact_key: Mapped[str | None] = mapped_column(String)
    literal_value: Mapped[Decimal | None] = mapped_column(Numeric)


class RuleConditionGroup(Base):
    __tablename__ = "rule_condition_group"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    parent_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    logical_op: Mapped[str] = mapped_column(String, default="AND")
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = created_at_col()


class RuleCondition(Base):
    __tablename__ = "rule_condition"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rules.rule_condition_group.id", ondelete="CASCADE")
    )
    fact_key: Mapped[str] = mapped_column(String, nullable=False)
    operator: Mapped[str] = mapped_column(String, nullable=False)
    value_type: Mapped[str] = mapped_column(String, nullable=False)
    value_number: Mapped[Decimal | None] = mapped_column(Numeric)
    value_number_high: Mapped[Decimal | None] = mapped_column(Numeric)
    value_text: Mapped[str | None] = mapped_column(String)
    value_boolean: Mapped[bool | None] = mapped_column()
    value_date: Mapped[date | None] = mapped_column(Date)
    value_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = created_at_col()


class RuleOutcome(Base):
    __tablename__ = "rule_outcome"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    outcome_type: Mapped[str] = mapped_column(String, nullable=False)
    impact_formula_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    priority: Mapped[int] = mapped_column(SmallInteger, default=3)
    title_template: Mapped[str | None] = mapped_column(String)
    mechanism: Mapped[str | None] = mapped_column(String)
    where_template: Mapped[str | None] = mapped_column(Text)
    how_template: Mapped[str | None] = mapped_column(Text)
    why_template: Mapped[str | None] = mapped_column(Text)
    # ---- Opportunity contract v2 (additive) ----
    # economic classification so the IOE never treats a deferral as a reduction;
    # portfolio_lever_code references the IOE lever registry BY CODE only — rule
    # data never names an engine input field.
    economic_effect_type: Mapped[str | None] = mapped_column(Text)
    reversibility: Mapped[str | None] = mapped_column(Text)
    portfolio_lever_code: Mapped[str | None] = mapped_column(Text)
    lever_parameters: Mapped[dict | None] = mapped_column(JSONB)
    projection_eligibility: Mapped[str | None] = mapped_column(
        Text,
        comment="Whether published legislation supports projecting this opportunity into future years. Authored as rule data under four-eyes governance; the IOE never infers recurrence.",
    )
    projection_method: Mapped[str | None] = mapped_column(Text)
    maximum_projection_horizon: Mapped[int | None] = mapped_column(
        SmallInteger,
        comment="The furthest year the rule authorizes projecting to. A consumer may project less, never more.",
    )
    required_assumption_codes: Mapped[list | None] = mapped_column(
        # none_as_null: a Python None must become SQL NULL ("not specified"),
        # not JSON null, which would read as a malformed empty declaration.
        JSONB(none_as_null=True),
        comment="Assumption codes that must be present and satisfied before a projection may be generated. Absent any of them, no projection is produced.",
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


# ---------------------------------------------------------------------------
# Opportunity contract v2 — legally meaningful metadata authored as RULE DATA
# (TKMS-governed). The IOE consumes these verbatim and never infers them.
# ---------------------------------------------------------------------------
class RuleAction(Base):
    __tablename__ = "rule_action"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    action_code: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    effort_rating: Mapped[int] = mapped_column(SmallInteger, default=3)
    cost_type: Mapped[str | None] = mapped_column(
        Text,
        comment=(
            "Rule-authored commitment class. Liquidity commitments and asset "
            "transfers constrain feasibility but are not losses; only "
            "nonrecoverable expenditure and implementation cost reduce the "
            "portfolio objective."
        ),
    )
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    deadline_code: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = created_at_col()


class RuleRequiredDocument(Base):
    __tablename__ = "rule_required_document"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    document_type_code: Mapped[str] = mapped_column(Text, nullable=False)
    necessity: Mapped[str] = mapped_column(Text, default="required")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RuleDependency(Base):
    __tablename__ = "rule_dependency"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    depends_on_rule_code: Mapped[str] = mapped_column(Text, nullable=False)
    dependency_type: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RuleDeadline(Base):
    __tablename__ = "rule_deadline"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    deadline_code: Mapped[str] = mapped_column(Text, nullable=False)
    deadline_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    is_hard: Mapped[bool] = mapped_column(Boolean, default=True)
    jurisdiction_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RuleSharedResource(Base):
    __tablename__ = "rule_shared_resource"
    __table_args__ = {"schema": "rules"}

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    resource_code: Mapped[str] = mapped_column(Text, nullable=False)
    pool_scope: Mapped[str] = mapped_column(Text, default="individual")
    created_at: Mapped[datetime] = created_at_col()


class GovSource(Base):
    __tablename__ = "gov_source"
    __table_args__ = {"schema": "tax_kb"}
    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = created_at_col()


class LegislationReference(Base):
    __tablename__ = "legislation_reference"
    __table_args__ = {"schema": "tax_kb"}
    id: Mapped[uuid.UUID] = uuid_pk()
    citation: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = created_at_col()


class TaxBracketSet(Base):
    __tablename__ = "tax_bracket_set"
    __table_args__ = {"schema": "tax_kb"}
    id: Mapped[uuid.UUID] = uuid_pk()
    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tax_year: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String, default="income_tax")
    created_at: Mapped[datetime] = created_at_col()


class TaxBracket(Base):
    __tablename__ = "tax_bracket"
    __table_args__ = {"schema": "tax_kb"}
    id: Mapped[uuid.UUID] = uuid_pk()
    bracket_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_bracket_set.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lower_bound: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    upper_bound: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ContributionLimit(Base):
    __tablename__ = "contribution_limit"
    __table_args__ = {"schema": "tax_kb"}
    id: Mapped[uuid.UUID] = uuid_pk()
    registered_type: Mapped[str] = mapped_column(String, nullable=False)
    tax_year: Mapped[int] = mapped_column(nullable=False)
    annual_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    lifetime_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    percent_of_income: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    allows_carryforward: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()


class BenefitProgram(Base):
    __tablename__ = "benefit_program"
    __table_args__ = {"schema": "tax_kb"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    is_refundable: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()


class BenefitParameter(Base):
    __tablename__ = "benefit_parameter"
    __table_args__ = {"schema": "tax_kb"}
    id: Mapped[uuid.UUID] = uuid_pk()
    benefit_program_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.benefit_program.id", ondelete="CASCADE")
    )
    tax_year: Mapped[int] = mapped_column(nullable=False)
    param_key: Mapped[str] = mapped_column(String, nullable=False)
    param_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    param_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    formula_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class CalcConstant(Base):
    __tablename__ = "calc_constant"
    __table_args__ = {"schema": "rules"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, nullable=False)
    tax_year: Mapped[int] = mapped_column(nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    unit: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = created_at_col()


class ConditionValueSet(Base):
    __tablename__ = "condition_value_set"
    __table_args__ = {"schema": "rules"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str | None] = mapped_column(String, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ConditionValueSetItem(Base):
    __tablename__ = "condition_value_set_item"
    __table_args__ = {"schema": "rules"}
    id: Mapped[uuid.UUID] = uuid_pk()
    value_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rules.condition_value_set.id", ondelete="CASCADE")
    )
    value_text: Mapped[str] = mapped_column(String, nullable=False)


class TaxSource(Base):
    """A continuing authoritative tax publication — the Income Tax Act, CRA
    Guide T4044 — identified independently of any single edition.

    NOT `GovSource`, which is a (name, url) stub with no version, issuer,
    jurisdiction, fingerprint or supersession. That stub is left alone rather
    than grown into something it was never shaped to be.
    """

    __tablename__ = "tax_source"
    __table_args__ = (
        UniqueConstraint("jurisdiction_id", "issuer_code", "official_identifier",
                         name="uq_tax_source_identity"),
        {"schema": "tax_kb"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    issuer_code: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ref.jurisdiction.id"), nullable=False)
    official_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class TaxSourceVersion(Base):
    """One retrieved edition, immutable once written.

    Supersession points BACKWARD from the successor, so registering a newer
    edition never rewrites the row a historical rule was authored against.
    """

    __tablename__ = "tax_source_version"
    __table_args__ = (
        UniqueConstraint("source_id", "content_fingerprint",
                         name="uq_tax_source_version_content"),
        UniqueConstraint("supersedes_version_id",
                         name="uq_tax_source_version_supersedes"),
        {"schema": "tax_kb"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_source.id"), nullable=False)
    edition: Mapped[str] = mapped_column(Text, nullable=False)
    #: Declared generically so it matches every other tax_year in the schema:
    #: the stored type is the ref.tax_year_num domain, a governed TYPE_AFFINITY
    #: divergence the drift policy already accounts for.
    tax_year: Mapped[int | None] = mapped_column()
    publication_date: Mapped[date | None] = mapped_column(Date)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    official_locator: Mapped[str] = mapped_column(Text, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint_method: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="ACTIVE", server_default=text("'ACTIVE'"))
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_source_version.id"))
    manifest_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class SourceCitation(Base):
    """A source version plus a structured locator. Reusable: the same location
    in the same edition is one row, cited by many knowledge objects."""

    __tablename__ = "source_citation"
    __table_args__ = (
        UniqueConstraint("source_version_id", "locator_hash",
                         name="uq_source_citation_identity"),
        {"schema": "tax_kb"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_source_version.id"),
        nullable=False)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False)
    locator_hash: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class KnowledgeCitation(Base):
    """Which governed knowledge object a citation supports.

    An exclusive arc — one nullable FK per referent kind, exactly one required —
    so every provenance link keeps real referential integrity. A
    (kind, object_id) pair would have been shorter and would have allowed a
    citation to point at a formula that no longer exists.
    """

    __tablename__ = "knowledge_citation"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(rule_version_id, formula_id, tax_bracket_set_id, "
            "contribution_limit_id, benefit_parameter_id) = 1",
            name="ck_knowledge_citation_exactly_one_subject"),
        {"schema": "tax_kb"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    citation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.source_citation.id"),
        nullable=False)
    rule_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id"))
    formula_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rules.calc_formula.id"))
    tax_bracket_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_bracket_set.id"))
    contribution_limit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.contribution_limit.id"))
    benefit_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.benefit_parameter.id"))
    created_at: Mapped[datetime] = created_at_col()
