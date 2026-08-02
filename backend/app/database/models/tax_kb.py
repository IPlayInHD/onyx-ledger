"""Minimal mappings for the tax KB + rules engine tables the evaluator reads."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import UUID
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
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


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
