"""SQLAlchemy models for the ioe schema — the Income Optimization Engine.

Mirrors backend/db/sql/21_ioe.sql. Two record classes, enforced in the database
by different triggers and reflected in how these models are used:

  * MUTABLE WORKFLOW HEADERS — OptimizationRun, Scenario, WeightConfig. Status,
    freshness, and visibility transition through a guarded state machine; result
    columns seal once the record completes.
  * IMMUTABLE CALCULATION EVIDENCE — everything else. Insert-only; UPDATE is
    always rejected and DELETE only inside an explicit purge context.

CHECK constraints, partial-unique/idempotency indexes, RLS policies, and the
immutability/transition triggers remain hand-written SQL (autogenerate does not
model them).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk

MONEY = Numeric(14, 2)
RATE = Numeric(9, 6)


# ---------------------------------------------------------------------------
# Versioned configuration
# ---------------------------------------------------------------------------
class WeightConfig(Base):
    """Ranking weights as versioned data. The scoring ALGORITHM version is a
    separate manifest key and is never conflated with the weight version."""

    __tablename__ = "weight_config"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(Text, default="1")
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    activated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


# ---------------------------------------------------------------------------
# Rule snapshots — content-addressed pinning
# ---------------------------------------------------------------------------
class RuleSnapshot(Base):
    """Content-addressed: identical rule content across runs shares one row."""

    __tablename__ = "rule_snapshot"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    snapshot_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    artifact_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at_col()


class RuleSnapshotArtifact(Base):
    __tablename__ = "rule_snapshot_artifact"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.rule_snapshot.id", ondelete="CASCADE")
    )
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    artifact_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------------------
# Optimization run (workflow header)
# ---------------------------------------------------------------------------
class OptimizationRun(Base):
    __tablename__ = "optimization_run"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis.analysis_run.id", ondelete="CASCADE")
    )
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)

    workflow_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    freshness_status: Mapped[str] = mapped_column(Text, nullable=False, default="current")
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    superseded_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    previous_attempt_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    optimization_spec_hash: Mapped[str | None] = mapped_column(Text)
    optimization_result_hash: Mapped[str | None] = mapped_column(Text)
    rule_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    weight_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    version_manifest: Mapped[dict | None] = mapped_column(JSONB)
    manifest_hash: Mapped[str | None] = mapped_column(Text)

    portfolio_total_benefit: Mapped[Decimal | None] = mapped_column(MONEY)
    total_estimated_savings: Mapped[Decimal | None] = mapped_column(MONEY)

    idempotency_key: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class OptimizationRunEvent(Base):
    __tablename__ = "optimization_run_event"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_run.id", ondelete="CASCADE")
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RunRuleVersion(Base):
    __tablename__ = "run_rule_version"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_run.id", ondelete="CASCADE")
    )
    tax_rule_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class RunRuleSnapshot(Base):
    __tablename__ = "run_rule_snapshot"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_run.id", ondelete="CASCADE")
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    replay_status: Mapped[str] = mapped_column(Text, nullable=False, default="verified")
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------------------
# Structured assumptions
# ---------------------------------------------------------------------------
class AssumptionSet(Base):
    __tablename__ = "assumption_set"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    set_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class Assumption(Base):
    """Calculations depend on the STRUCTURED fields; display_note is presentation
    and is excluded from hashes."""

    __tablename__ = "assumption"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    assumption_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.assumption_set.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    certainty: Mapped[str] = mapped_column(Text, nullable=False)
    effective_period: Mapped[str | None] = mapped_column(Text)
    materiality: Mapped[str] = mapped_column(Text, default="medium")
    affects_eligibility: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitivity: Mapped[Decimal | None] = mapped_column(RATE)
    display_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------------------
# Candidates and evidence
# ---------------------------------------------------------------------------
class OptimizationCandidate(Base):
    __tablename__ = "optimization_candidate"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_run.id", ondelete="CASCADE")
    )
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    opportunity_code: Mapped[str] = mapped_column(Text, nullable=False)
    tax_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    eligibility_status: Mapped[str] = mapped_column(Text, nullable=False)
    calculation_basis: Mapped[str | None] = mapped_column(Text)
    evidence_status: Mapped[str | None] = mapped_column(Text)

    standalone_potential: Mapped[Decimal | None] = mapped_column(MONEY)
    incremental_portfolio_benefit: Mapped[Decimal | None] = mapped_column(MONEY)

    portfolio_membership: Mapped[str] = mapped_column(
        Text, nullable=False, default="excluded_not_evaluable"
    )
    exclusion_reason_code: Mapped[str | None] = mapped_column(Text)
    candidate_rank: Mapped[int | None] = mapped_column(Integer)
    recommendation_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    confidence_score: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = created_at_col()


class CandidateEconomicEffect(Base):
    """A candidate may carry several effects (e.g. a reduction AND a deferral);
    they are never collapsed into one number."""

    __tablename__ = "candidate_economic_effect"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_candidate.id", ondelete="CASCADE")
    )
    effect_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_year: Mapped[int | None] = mapped_column(Integer)
    horizon_years: Mapped[int] = mapped_column(SmallInteger, default=1)
    calculation_basis: Mapped[str] = mapped_column(Text, nullable=False)
    reversibility: Mapped[str | None] = mapped_column(Text)
    is_permanent: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()


class CandidateCost(Base):
    """A required cash contribution retains an asset and is NOT a cost in the
    objective; only expenditure and implementation cost are."""

    __tablename__ = "candidate_cost"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_candidate.id", ondelete="CASCADE")
    )
    cost_type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    timing: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ScoreComponent(Base):
    __tablename__ = "score_component"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_candidate.id", ondelete="CASCADE")
    )
    factor_code: Mapped[str] = mapped_column(Text, nullable=False)
    raw_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    normalized_value: Mapped[Decimal | None] = mapped_column(RATE)
    weight: Mapped[Decimal | None] = mapped_column(RATE)
    contribution: Mapped[Decimal | None] = mapped_column(RATE)
    created_at: Mapped[datetime] = created_at_col()


class ConfidenceComponent(Base):
    __tablename__ = "confidence_component"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_candidate.id", ondelete="CASCADE")
    )
    factor_code: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[Decimal | None] = mapped_column(RATE)
    weight: Mapped[Decimal | None] = mapped_column(RATE)
    contribution: Mapped[Decimal | None] = mapped_column(RATE)
    reason_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RecommendationRelationship(Base):
    """Typed edges replace free-text pairwise conflicts; human-readable text is
    rendered from explanation_code plus the structured fields."""

    __tablename__ = "recommendation_relationship"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_run.id", ondelete="CASCADE")
    )
    source_candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    shared_resource_code: Mapped[str | None] = mapped_column(Text)
    maximum_shared_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    measured_delta: Mapped[Decimal | None] = mapped_column(MONEY)
    explanation_code: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_options: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------------------
# Strategy portfolio — the only source of a user-facing total
# ---------------------------------------------------------------------------
class StrategyPortfolio(Base):
    __tablename__ = "strategy_portfolio"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_run.id", ondelete="CASCADE")
    )
    assembly_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    assembly_method: Mapped[str] = mapped_column(Text, nullable=False)
    optimality_claim: Mapped[str] = mapped_column(Text, nullable=False, default="none")

    objective_metric: Mapped[str] = mapped_column(Text, nullable=False)
    objective_version: Mapped[str] = mapped_column(Text, nullable=False)
    objective_value_baseline: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    objective_value_final: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    baseline_tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    portfolio_tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    portfolio_total_benefit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    # diagnostics only — sum_of_standalone must never be displayed as a total
    sum_of_standalone: Mapped[Decimal | None] = mapped_column(MONEY)
    interaction_delta: Mapped[Decimal | None] = mapped_column(MONEY)
    additivity_class: Mapped[str | None] = mapped_column(Text)
    additivity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    attribution_method: Mapped[str] = mapped_column(
        Text, nullable=False, default="incremental_path_dependent"
    )
    attribution_method_version: Mapped[str | None] = mapped_column(Text)

    total_required_cash_contribution: Mapped[Decimal | None] = mapped_column(MONEY)
    total_required_expenditure: Mapped[Decimal | None] = mapped_column(MONEY)
    total_implementation_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    net_current_year_benefit: Mapped[Decimal | None] = mapped_column(MONEY)
    total_deferral_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    total_recurring_annual: Mapped[Decimal | None] = mapped_column(MONEY)
    total_multi_year_projected: Mapped[Decimal | None] = mapped_column(MONEY)

    deferred_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    improvement_moves_applied: Mapped[int] = mapped_column(Integer, default=0)
    improvement_runs_used: Mapped[int] = mapped_column(Integer, default=0)
    unexplored_alternatives_count: Mapped[int] = mapped_column(Integer, default=0)
    engine_runs_used: Mapped[int] = mapped_column(Integer, default=0)

    portfolio_result_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class PortfolioMember(Base):
    __tablename__ = "portfolio_member"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.strategy_portfolio.id", ondelete="CASCADE")
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    apply_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    incremental_benefit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    resource_allocations: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()


class ResourceLedgerEntry(Base):
    """The anti-double-counting ledger: a shared pool is allocated once."""

    __tablename__ = "resource_ledger_entry"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.strategy_portfolio.id", ondelete="CASCADE")
    )
    resource_code: Mapped[str] = mapped_column(Text, nullable=False)
    pool_scope: Mapped[str] = mapped_column(Text, default="individual")
    capacity: Mapped[Decimal | None] = mapped_column(MONEY)
    allocated: Mapped[Decimal] = mapped_column(MONEY, default=0)
    remaining: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = created_at_col()


class MultiYearProjection(Base):
    __tablename__ = "multi_year_projection"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.optimization_run.id", ondelete="CASCADE")
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    horizon_year: Mapped[int] = mapped_column(Integer, nullable=False)
    projected_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    effect_type: Mapped[str] = mapped_column(Text, nullable=False)
    calculation_basis: Mapped[str] = mapped_column(Text, default="projection_estimate")
    assumption_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_indexation_known: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
class Scenario(Base):
    """Workflow header. 'Delete' is an archive (visibility_status), so the
    calculation evidence and audit trail survive."""

    __tablename__ = "scenario"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    base_analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis.analysis_run.id", ondelete="CASCADE")
    )
    label: Mapped[str | None] = mapped_column(Text)
    workflow_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    visibility_status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scenario_spec_hash: Mapped[str | None] = mapped_column(Text)
    scenario_result_hash: Mapped[str | None] = mapped_column(Text)
    rule_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lever_registry_version: Mapped[str | None] = mapped_column(Text)
    assumption_set_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    version_manifest: Mapped[dict | None] = mapped_column(JSONB)
    manifest_hash: Mapped[str | None] = mapped_column(Text)

    idempotency_key: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ScenarioEvent(Base):
    __tablename__ = "scenario_event"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.scenario.id", ondelete="CASCADE")
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ScenarioInputChange(Base):
    __tablename__ = "scenario_input_change"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.scenario.id", ondelete="CASCADE")
    )
    lever_code: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    apply_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = created_at_col()


class ScenarioResult(Base):
    __tablename__ = "scenario_result"
    __table_args__ = {"schema": "ioe"}

    id: Mapped[uuid.UUID] = uuid_pk()
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ioe.scenario.id", ondelete="CASCADE")
    )
    baseline_tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    scenario_tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_delta: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net_benefit: Mapped[Decimal | None] = mapped_column(MONEY)
    calculation_basis: Mapped[str] = mapped_column(Text, default="scenario_estimate")
    confidence_score: Mapped[int | None] = mapped_column(SmallInteger)
    affected_rule_versions: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()
