"""Explanation assembly (§P6) — sealed rows to response objects.

This is where a stored number acquires its meaning. It sits between the read
repository and the routes so that routes stay thin: a route resolves the
principal, calls a service, and returns what it is given.

The only arithmetic in this module is `Decimal.quantize` to the stored money
scale. There is no summing, no ranking, no reconciliation, and no
re-derivation of a total — every figure returned was settled when the evidence
was written. `portfolio_total_benefit` in particular is read from the sealed
column, never rebuilt from members, because a total that can be recomputed at
read time is a total that can disagree with the one that was verified.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.database.models import (
    MultiYearProjection,
    PortfolioExclusion,
    PortfolioMember,
    Scenario,
    ScenarioAssumption,
    ScenarioInputChange,
    ScenarioLever,
    ScenarioResult,
    StrategyPortfolio,
)
from app.schemas.ioe import (
    AppliedChangeOut,
    AssumptionInput,
    ComparisonSideOut,
    FreshnessOut,
    IntegrityOut,
    LeverInput,
    MonetaryAmount,
    MultiYearProjectionOut,
    PortfolioExclusionOut,
    PortfolioMemberOut,
    ProjectionAssumptionOut,
    ProjectionUncertaintyOut,
    ScenarioComparisonOut,
    ScenarioDetailOut,
    ScenarioSummaryOut,
    StrategyPortfolioOut,
    SupportScore,
)

PRESENTATION_VERSION = "1.0.0"
MONEY = Decimal("0.01")

# Effect-type labels for figures the engine produced directly.
EFFECT_CURRENT_YEAR = "current_year_tax_reduction"
EFFECT_TOTAL_PAYABLE = "total_tax_payable"
EFFECT_NET_OBJECTIVE = "net_objective_benefit"


def _q(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(MONEY, ROUND_HALF_UP)


def freshness_of(scenario: Scenario) -> FreshnessOut:
    return FreshnessOut(
        freshness_status=scenario.freshness_status,
        stale_reason_code=scenario.stale_reason_code,
        freshness_evaluated_at=scenario.freshness_evaluated_at,
        superseded_by_scenario_id=scenario.superseded_by_scenario_id,
    )


def integrity_of(row) -> IntegrityOut:
    """Current reproducibility metadata for a sealed entity.

    Reads stored columns only — this never triggers a replay. Verifying on every
    read would put an engine run behind a page load, and a badge is not worth
    that.
    """
    from app.services.ioe.domain.integrity import integrity_warning, user_visible_state
    from app.services.ioe.frozen.models import (
        SCENARIO_EXECUTION_POLICY_INVALID,
        FrozenSnapshotError,
        scenario_execution_policy,
    )

    status = getattr(row, "integrity_status", None) or "not_checked"
    reason = getattr(row, "integrity_reason_code", None) or "NONE"
    # An optimization records the policy in a column; a scenario records it in
    # its version manifest, where absence means legacy — silence is never read
    # as the corrected policy, or a historical row would inherit a guarantee it
    # never had. A portfolio records neither: its policy is its run's, and
    # inventing one here would be a claim nothing backs.
    policy: str | None = getattr(row, "input_execution_policy_version", None)
    if policy is None and hasattr(row, "scenario_spec_hash"):
        try:
            policy = scenario_execution_policy(getattr(row, "version_manifest", None))
        except FrozenSnapshotError:
            # A read must not 500 on one corrupt row, and it must not pretend
            # the row is ordinary either. The defect is named, not swallowed.
            policy = SCENARIO_EXECUTION_POLICY_INVALID
    return IntegrityOut(
        integrity_status=status,
        integrity_state=user_visible_state(status, reason),
        integrity_reason_code=reason,
        last_integrity_checked_at=getattr(row, "last_integrity_checked_at", None),
        integrity_warning=integrity_warning(status, reason),
        execution_policy=policy,
    )


def support_of(row) -> SupportScore | None:
    if row is None or row.display_support_score is None:
        return None
    return SupportScore(
        display_support_score=row.display_support_score,
        assumption_adjusted_score=row.assumption_adjusted_score,
        raw_support_score=row.raw_support_score,
        support_cap_applied=row.support_cap_applied,
        support_cap_reason_code=row.support_cap_reason_code,
    )


def money(
    amount: Decimal | None,
    *,
    effect_type: str,
    calculation_basis: str,
    evidence_status: str,
    tax_year: int | None,
    horizon_years: int = 1,
    is_permanent: bool = True,
    support: SupportScore | None = None,
    freshness: FreshnessOut | None = None,
) -> MonetaryAmount | None:
    """Wrap a stored figure with everything needed to read it correctly."""
    if amount is None:
        return None
    return MonetaryAmount(
        amount=_q(amount),
        effect_type=effect_type,
        calculation_basis=calculation_basis,
        evidence_status=evidence_status,
        tax_year=tax_year,
        horizon_years=horizon_years,
        is_permanent=is_permanent,
        support=support,
        freshness=freshness,
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_summary(scenario: Scenario) -> ScenarioSummaryOut:
    return ScenarioSummaryOut(
        id=scenario.id,
        label=scenario.label,
        workflow_status=scenario.workflow_status,
        visibility_status=scenario.visibility_status,
        tax_year=scenario.tax_year,
        jurisdiction=scenario.jurisdiction,
        created_at=scenario.created_at,
        freshness=freshness_of(scenario),
    )


def scenario_detail(
    scenario: Scenario,
    result: ScenarioResult | None,
    levers: list[ScenarioLever],
    assumptions: list[ScenarioAssumption],
    changes: list[ScenarioInputChange],
) -> ScenarioDetailOut:
    """Assemble a scenario response entirely from sealed rows."""
    fresh = freshness_of(scenario)
    support = support_of(result)
    basis = result.calculation_basis if result else "scenario_estimate"
    # A scenario is a hypothetical, so its evidence status reflects that the
    # inputs are user-declared rather than documented.
    evidence = "user_attested"

    return ScenarioDetailOut(
        id=scenario.id,
        label=scenario.label,
        note=scenario.note,
        workflow_status=scenario.workflow_status,
        visibility_status=scenario.visibility_status,
        tax_year=scenario.tax_year,
        jurisdiction=scenario.jurisdiction,
        base_analysis_id=scenario.base_analysis_id,
        objective_code=scenario.objective_code,
        objective_version=scenario.objective_version,
        result_schema_version=scenario.result_schema_version,
        scenario_spec_hash=scenario.scenario_spec_hash,
        scenario_result_hash=scenario.scenario_result_hash,
        created_at=scenario.created_at,
        completed_at=scenario.completed_at,
        error_code=scenario.error_code,
        freshness=fresh,
        integrity=integrity_of(scenario),
        levers=[
            LeverInput(
                lever_code=x.lever_code,
                parameters={k: str(v) for k, v in (x.parameters or {}).items()},
            )
            for x in levers
        ],
        assumptions=[
            AssumptionInput(
                assumption_code=a.assumption_code,
                value_number=a.value_number,
                value_text=a.value_text,
                value_boolean=a.value_boolean,
                materiality=a.materiality,
                source=a.source,
                certainty=a.certainty,
            )
            for a in assumptions
        ],
        baseline_tax=money(
            scenario.baseline_tax, effect_type=EFFECT_TOTAL_PAYABLE,
            calculation_basis="engine_determined", evidence_status=evidence,
            tax_year=scenario.tax_year, freshness=fresh,
        ),
        scenario_tax=money(
            result.scenario_tax if result else None,
            effect_type=EFFECT_TOTAL_PAYABLE, calculation_basis=basis,
            evidence_status=evidence, tax_year=scenario.tax_year,
            support=support, freshness=fresh,
        ),
        tax_delta=money(
            result.tax_delta if result else None,
            effect_type=EFFECT_CURRENT_YEAR, calculation_basis=basis,
            evidence_status=evidence, tax_year=scenario.tax_year,
            support=support, freshness=fresh,
        ),
        objective_delta=money(
            result.objective_delta if result else None,
            effect_type=EFFECT_NET_OBJECTIVE, calculation_basis=basis,
            evidence_status=evidence, tax_year=scenario.tax_year,
            support=support, freshness=fresh,
        ),
        support=support,
        applied_changes=[
            AppliedChangeOut(
                apply_order=ch.apply_order, lever_code=ch.lever_code,
                field=ch.field, old_value=ch.old_value, new_value=ch.new_value,
            )
            for ch in changes
        ],
    )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
def portfolio_detail(
    portfolio: StrategyPortfolio,
    members: list[PortfolioMember],
    exclusions: list[PortfolioExclusion],
    tax_year: int | None,
) -> StrategyPortfolioOut:
    """Read the sealed portfolio. The total is NOT rebuilt from members.

    Members are returned alongside it so a reader can see the composition, but
    the headline comes from `portfolio_total_benefit` exactly as stored — the
    figure that passed the I-1/I-2 reconciliation when it was written.
    """
    def concept(amount, effect_type: str, *, permanent: bool = True):
        return money(
            amount, effect_type=effect_type,
            calculation_basis="engine_determined",
            evidence_status="documented_unverified", tax_year=tax_year,
            is_permanent=permanent,
        )

    return StrategyPortfolioOut(
        id=portfolio.id,
        run_id=portfolio.run_id,
        integrity=integrity_of(portfolio),
        objective_code=portfolio.portfolio_objective_code or portfolio.objective_metric,
        objective_version=portfolio.portfolio_objective_version
        or portfolio.objective_version,
        assembly_method=portfolio.assembly_method,
        optimality_claim=portfolio.optimality_claim,
        search_budget_exhausted=portfolio.search_budget_exhausted,
        engine_runs_used=portfolio.engine_runs_used,
        portfolio_total_benefit=concept(
            portfolio.portfolio_total_benefit, EFFECT_NET_OBJECTIVE
        ),
        objective_value_baseline=concept(
            portfolio.objective_value_baseline, "objective_cost_baseline"
        ),
        objective_value_final=concept(
            portfolio.objective_value_final, "objective_cost_final"
        ),
        total_tax_reduction=concept(
            portfolio.total_tax_reduction, EFFECT_CURRENT_YEAR
        ),
        total_refund_impact=concept(
            portfolio.total_refund_impact, "immediate_refund_impact"
        ),
        total_refundable_benefit=concept(
            portfolio.total_refundable_benefit, "refundable_benefit"
        ),
        # a deferral is a TIMING benefit, and says so
        total_deferral_amount=concept(
            portfolio.total_deferral_amount, "tax_deferral", permanent=False
        ),
        total_liquidity_commitment=concept(
            portfolio.total_liquidity_commitment, "liquidity_commitment"
        ),
        total_asset_transfer=concept(
            portfolio.total_asset_transfer, "asset_transfer"
        ),
        total_nonrecoverable_expenditure=concept(
            portfolio.total_nonrecoverable_expenditure, "nonrecoverable_expenditure"
        ),
        total_implementation_cost=concept(
            portfolio.total_implementation_cost, "implementation_cost"
        ),
        members=[
            PortfolioMemberOut(
                candidate_id=m.candidate_id,
                apply_order=m.apply_order,
                incremental_benefit=concept(
                    m.incremental_benefit, EFFECT_NET_OBJECTIVE
                ),
            )
            for m in members
        ],
        exclusions=[
            PortfolioExclusionOut(
                candidate_id=x.candidate_id,
                membership=x.membership,
                reason_code=x.reason_code,
                shared_resource_code=x.shared_resource_code,
                resolution_options=list(x.resolution_options or []),
            )
            for x in exclusions
        ],
    )


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------
def projection_detail(
    rows: list[MultiYearProjection],
    assumptions: list[ScenarioAssumption] | None,
    tax_year: int | None,
    *,
    methodology_version: str,
) -> MultiYearProjectionOut | None:
    """Assemble a projection, kept apart from any current-year total.

    A projection rests on assumptions a current-year engine result does not, so
    it carries its horizon, its assumptions, its methodology version and its
    uncertainty, and is returned on its own — never merged into a portfolio or
    scenario total.
    """
    if not rows:
        return None
    horizon_year = max(r.horizon_year for r in rows)
    horizon_years = len(rows)
    total = sum((r.projected_amount for r in rows), Decimal(0))

    def projected(amount, effect_type, horizon):
        return money(
            amount, effect_type=effect_type,
            calculation_basis="projection_estimate",
            evidence_status="user_attested", tax_year=tax_year,
            horizon_years=horizon, is_permanent=False,
        )

    return MultiYearProjectionOut(
        id=rows[0].id,
        horizon_year=horizon_year,
        horizon_years=horizon_years,
        methodology_version=methodology_version,
        projected_total=projected(total, "multi_year_projected_benefit", horizon_years),
        per_year=[
            projected(r.projected_amount, r.effect_type, 1) for r in rows
        ],
        assumptions=[
            ProjectionAssumptionOut(
                assumption_code=a.assumption_code,
                value=str(
                    a.value_number if a.value_number is not None
                    else a.value_text if a.value_text is not None
                    else a.value_boolean
                ),
                materiality=a.materiality,
                certainty=a.certainty,
                source=a.source,
            )
            for a in (assumptions or [])
        ],
        uncertainty=ProjectionUncertaintyOut(
            sensitivity_note=(
                "Projected figures depend on the assumptions listed above. A "
                "change to any of them changes the projection."
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def comparison_detail(loaded) -> ScenarioComparisonOut:
    """Map a completed comparison onto the response. Computes nothing."""
    def side(row, result) -> ComparisonSideOut:
        fresh = freshness_of(row)
        return ComparisonSideOut(
            scenario_id=row.id,
            label=row.label,
            freshness=fresh,
            objective_delta=money(
                result.objective_delta, effect_type="net_objective_benefit",
                calculation_basis=result.calculation_basis,
                evidence_status="user_attested", tax_year=row.tax_year,
                support=support_of(result), freshness=fresh,
            ),
            tax_delta=money(
                result.tax_delta, effect_type="current_year_tax_reduction",
                calculation_basis=result.calculation_basis,
                evidence_status="user_attested", tax_year=row.tax_year,
                support=support_of(result), freshness=fresh,
            ),
        )

    deltas = loaded.deltas
    tax_year = loaded.left_row.tax_year

    def difference(amount, effect_type, *, permanent=True):
        return money(
            amount, effect_type=effect_type, calculation_basis="scenario_estimate",
            evidence_status="user_attested", tax_year=tax_year,
            is_permanent=permanent,
        )

    return ScenarioComparisonOut(
        left=side(loaded.left_row, loaded.left_result),
        right=side(loaded.right_row, loaded.right_result),
        objective_code=loaded.result.objective_code,
        objective_version=loaded.result.objective_version,
        comparison_policy_version=loaded.policy_version,
        objective_difference=difference(
            deltas.objective_difference, "net_objective_benefit"
        ),
        tax_difference=difference(
            deltas.tax_difference, "current_year_tax_reduction"
        ),
        refund_balance_difference=difference(
            deltas.refund_balance_difference, "immediate_refund_impact"
        ),
        liquidity_commitment_difference=difference(
            deltas.liquidity_commitment_difference, "liquidity_commitment"
        ),
        effect_type_differences=[
            difference(amount, effect_type)
            for effect_type, amount in sorted(deltas.effect_type_differences.items())
        ],
        better=loaded.result.better,
        both_current=loaded.result.both_current,
        stale_notices=list(loaded.result.stale_notices),
    )


__all__ = [
    "PRESENTATION_VERSION",
    "comparison_detail",
    "freshness_of",
    "money",
    "portfolio_detail",
    "projection_detail",
    "scenario_detail",
    "scenario_summary",
    "support_of",
]
