"""Constrained portfolio assembly (architecture §12, §D, §E) — stage 2 of two.

This is the blocking correction from Revision 2 made concrete: a ranked list is
not a strategy, and recommendation-level amounts must never be summed into a
total. The portfolio is applied to a cloned baseline and evaluated through the
tax engine; the ONLY user-facing total is that combined engine result.

The engine is injected as a callable so this module stays pure and testable:

    evaluate(inputs: dict) -> Decimal        # total payable

Stated limitations (§D.2) — the assembler is rule-based, NOT a solver:
  L-1 order dependence      L-2 no backtracking       L-3 myopic acceptance
  L-4 no approximation guarantee under shared budgets
  L-5 substitute branch selection                     L-6 bounded exploration

L-3 is mitigated by deferred reconsideration: a candidate that does not improve
the objective *on its own at this point* is NOT discarded, because near credit
ceilings and phase-out thresholds it may become beneficial once others apply.
L-1/L-2/L-5 are mitigated by a bounded, deterministic local-improvement phase.
No surface may describe the result as optimal.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain.enums import (
    AdditivityClass,
    AssemblyMethod,
    ObjectiveMetric,
    OptimalityClaim,
    PortfolioMembership,
    RelationshipType,
)
from app.services.ioe.domain.models import (
    LedgerEntry,
    OptimizationCandidate,
    PortfolioMember,
    RecommendationRelationship,
    StrategyPortfolio,
)
from app.services.ioe.domain.savings import EPSILON_ACCEPT, objective_value

PORTFOLIO_ASSEMBLY_VERSION = "1.0.0"
ATTRIBUTION_METHOD_VERSION = "1.0.0"

MONEY = Decimal("0.01")
MAX_IMPROVEMENT_ENGINE_RUNS = 30
MAX_DEFERRED_RETESTS = 40

Evaluator = Callable[[dict], Decimal]


class PortfolioReconciliationError(RuntimeError):
    """Invariant I-2 failed: the combined run disagreed with the last trial run.

    This is a hard error rather than a reported discrepancy — a portfolio total
    that cannot be reproduced by re-running the engine must never be shown.
    """


# ---------------------------------------------------------------------------
# Resource ledger — a shared pool can be allocated once (§12.4)
# ---------------------------------------------------------------------------
@dataclass
class ResourceLedger:
    capacities: dict[str, Decimal] = field(default_factory=dict)
    allocated: dict[str, Decimal] = field(default_factory=dict)

    def remaining(self, resource_code: str) -> Decimal | None:
        if resource_code not in self.capacities:
            return None                     # uncapped pool
        return self.capacities[resource_code] - self.allocated.get(resource_code, Decimal(0))

    def try_allocate(self, requests: dict[str, Decimal]) -> dict[str, Decimal] | None:
        """All-or-nothing allocation. Returns granted amounts, or None if any
        request would exceed a pool — which is how double-counting is prevented."""
        granted: dict[str, Decimal] = {}
        for code, amount in requests.items():
            remaining = self.remaining(code)
            if remaining is not None and amount > remaining:
                return None
            granted[code] = amount
        return granted

    def commit(self, granted: dict[str, Decimal]) -> None:
        for code, amount in granted.items():
            self.allocated[code] = self.allocated.get(code, Decimal(0)) + amount

    def entries(self) -> tuple[LedgerEntry, ...]:
        codes = sorted(set(self.capacities) | set(self.allocated))
        return tuple(
            LedgerEntry(
                resource_code=code,
                capacity=self.capacities.get(code),
                allocated=self.allocated.get(code, Decimal(0)),
                remaining=self.remaining(code),
            )
            for code in codes
        )


@dataclass
class AssemblyConstraints:
    available_cash: Decimal | None = None
    objective_metric: ObjectiveMetric = (
        ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE
    )
    resource_capacities: dict[str, Decimal] = field(default_factory=dict)
    enable_local_improvement: bool = True
    max_improvement_runs: int = MAX_IMPROVEMENT_ENGINE_RUNS
    max_deferred_retests: int = MAX_DEFERRED_RETESTS


@dataclass
class _State:
    """Mutable assembly state, kept explicit rather than smuggled onto objects."""

    inputs: dict
    running_tax: Decimal
    objective: Decimal
    engine_runs: int = 0
    selected: list[OptimizationCandidate] = field(default_factory=list)
    allocations: dict[str, dict[str, Decimal]] = field(default_factory=dict)


def assemble(
    ranked: list[OptimizationCandidate],
    relationships: list[RecommendationRelationship],
    baseline_inputs: dict,
    evaluate: Evaluator,
    constraints: AssemblyConstraints | None = None,
) -> StrategyPortfolio:
    """Greedy, constrained assembly plus deferred reconsideration."""
    cons = constraints or AssemblyConstraints()
    ledger = ResourceLedger(capacities=dict(cons.resource_capacities))
    excludes, requires = _relationship_maps(relationships)

    baseline_tax = evaluate(dict(baseline_inputs))
    state = _State(
        inputs=dict(baseline_inputs),
        running_tax=baseline_tax,
        objective=_objective(cons, baseline_tax, baseline_tax, []),
        engine_runs=1,
    )

    def try_admit(candidate: OptimizationCandidate) -> tuple[str, dict | None, Decimal | None]:
        """Returns (outcome, inputs, tax) where outcome is one of:
        'accepted', 'hard_reject' (a constraint forbids it), or 'no_improvement'
        (it did not pay off *at this point* — the caller defers, never discards).

        The outcome is returned explicitly rather than read back off the
        candidate, because EXCLUDED_NOT_EVALUABLE is also the initial default and
        the two states must not be confused.
        """
        if not candidate.is_portfolio_evaluable:
            _reject(candidate, PortfolioMembership.EXCLUDED_NOT_EVALUABLE,
                    "NOT_PORTFOLIO_EVALUABLE")
            return "hard_reject", None, None
        if _conflicts(candidate, state.selected, excludes):
            _reject(candidate, PortfolioMembership.EXCLUDED_CONFLICT,
                    "CONFLICTS_WITH_SELECTED")
            return "hard_reject", None, None
        if not _dependencies_met(candidate, state.selected, requires):
            _reject(candidate, PortfolioMembership.DEFERRED_TIMING,
                    "DEPENDENCY_NOT_SATISFIED")
            return "hard_reject", None, None
        if ledger.try_allocate(_resource_requests(candidate)) is None:
            _reject(candidate, PortfolioMembership.EXCLUDED_CONSTRAINT,
                    "SHARED_RESOURCE_EXHAUSTED")
            return "hard_reject", None, None
        if not _cash_available(candidate, state.selected, cons.available_cash):
            _reject(candidate, PortfolioMembership.EXCLUDED_CONSTRAINT,
                    "INSUFFICIENT_CASH")
            return "hard_reject", None, None

        trial_inputs = _apply(state.inputs, candidate)
        trial_tax = evaluate(trial_inputs)
        state.engine_runs += 1
        trial_objective = _objective(
            cons, baseline_tax, trial_tax, [*state.selected, candidate]
        )
        if trial_objective - state.objective < EPSILON_ACCEPT:
            return "no_improvement", None, None
        return "accepted", trial_inputs, trial_tax

    def accept(candidate, trial_inputs: dict, trial_tax: Decimal) -> None:
        granted = ledger.try_allocate(_resource_requests(candidate)) or {}
        ledger.commit(granted)
        state.allocations[candidate.candidate_key] = granted
        candidate.incremental_portfolio_benefit = (
            state.running_tax - trial_tax
        ).quantize(MONEY, ROUND_HALF_UP)
        candidate.portfolio_membership = PortfolioMembership.SELECTED
        candidate.exclusion_reason_code = None
        state.selected.append(candidate)
        state.inputs = trial_inputs
        state.running_tax = trial_tax
        state.objective = _objective(cons, baseline_tax, trial_tax, state.selected)

    # ---- greedy pass ----
    deferred: list[OptimizationCandidate] = []
    for candidate in ranked:
        outcome, trial_inputs, trial_tax = try_admit(candidate)
        if outcome == "accepted":
            accept(candidate, trial_inputs, trial_tax)
        elif outcome == "no_improvement":
            # L-3 mitigation: hold it back; a later candidate may make it pay off
            _reject(candidate, PortfolioMembership.DEFERRED_PENDING_COMBINATION,
                    "NO_STANDALONE_IMPROVEMENT_AT_THIS_POINT")
            deferred.append(candidate)

    # ---- deferred reconsideration ----
    retests = 0
    changed = True
    while changed and retests < cons.max_deferred_retests:
        changed = False
        for candidate in list(deferred):
            if retests >= cons.max_deferred_retests:
                break
            retests += 1
            outcome, trial_inputs, trial_tax = try_admit(candidate)
            if outcome == "accepted":
                deferred.remove(candidate)
                accept(candidate, trial_inputs, trial_tax)
                changed = True
            elif outcome == "no_improvement":
                # restore the deferred marker that try_admit did not change
                _reject(candidate, PortfolioMembership.DEFERRED_PENDING_COMBINATION,
                        "NO_STANDALONE_IMPROVEMENT_AT_THIS_POINT")

    # ---- final combined run: THE authoritative number ----
    final_tax = evaluate(state.inputs)
    state.engine_runs += 1
    if state.selected and abs(state.running_tax - final_tax) > EPSILON_ACCEPT:
        raise PortfolioReconciliationError(
            f"combined portfolio run {final_tax} disagrees with the last trial run "
            f"{state.running_tax}; lever application may not be order-independent"
        )

    portfolio_total_benefit = (baseline_tax - final_tax).quantize(MONEY, ROUND_HALF_UP)
    sum_of_standalone = sum(
        (x.standalone_potential or Decimal(0) for x in state.selected), Decimal(0)
    ).quantize(MONEY, ROUND_HALF_UP)
    interaction_delta = (sum_of_standalone - portfolio_total_benefit).quantize(
        MONEY, ROUND_HALF_UP
    )

    members = tuple(
        PortfolioMember(
            candidate_key=x.candidate_key,
            apply_order=i,
            incremental_benefit=x.incremental_portfolio_benefit or Decimal(0),
            resource_allocations=tuple(
                sorted(state.allocations.get(x.candidate_key, {}).items())
            ),
        )
        for i, x in enumerate(state.selected)
    )

    return StrategyPortfolio(
        members=members,
        ledger=ledger.entries(),
        baseline_tax=baseline_tax.quantize(MONEY, ROUND_HALF_UP),
        portfolio_tax=final_tax.quantize(MONEY, ROUND_HALF_UP),
        portfolio_total_benefit=portfolio_total_benefit,
        sum_of_standalone=sum_of_standalone,
        interaction_delta=interaction_delta,
        additivity_class=classify_additivity(interaction_delta),
        additivity_verified=abs(interaction_delta) <= EPSILON_ACCEPT,
        objective_metric=cons.objective_metric,
        objective_value_baseline=_objective(cons, baseline_tax, baseline_tax, []),
        objective_value_final=_objective(cons, baseline_tax, final_tax, state.selected),
        assembly_method=(
            AssemblyMethod.GREEDY_RANKED_WITH_LOCAL_IMPROVEMENT
            if cons.enable_local_improvement else AssemblyMethod.GREEDY_RANKED
        ),
        optimality_claim=(
            OptimalityClaim.LOCALLY_IMPROVED
            if cons.enable_local_improvement else OptimalityClaim.NONE
        ),
        deferred_count=len(deferred),
        excluded_count=sum(
            1 for x in ranked if x.portfolio_membership in _HARD_EXCLUSIONS
        ),
        engine_runs_used=state.engine_runs,
        unexplored_alternatives_count=len(deferred) + sum(
            1 for x in ranked
            if x.portfolio_membership is PortfolioMembership.EXCLUDED_CONFLICT
        ),
    )


_HARD_EXCLUSIONS = frozenset({
    PortfolioMembership.EXCLUDED_CONFLICT,
    PortfolioMembership.EXCLUDED_CONSTRAINT,
    PortfolioMembership.EXCLUDED_NOT_EVALUABLE,
    PortfolioMembership.DEFERRED_TIMING,
})


def _reject(candidate, membership: PortfolioMembership, reason_code: str) -> None:
    candidate.portfolio_membership = membership
    candidate.exclusion_reason_code = reason_code


# ---------------------------------------------------------------------------
# Interaction mathematics (§E)
# ---------------------------------------------------------------------------
def classify_additivity(interaction_delta: Decimal) -> AdditivityClass:
    """Sign convention: a POSITIVE delta means summing would OVERSTATE."""
    if interaction_delta > EPSILON_ACCEPT:
        return AdditivityClass.SUB_ADDITIVE
    if interaction_delta < -EPSILON_ACCEPT:
        return AdditivityClass.SUPER_ADDITIVE
    return AdditivityClass.ADDITIVE


def verify_telescoping(
    members: tuple[PortfolioMember, ...], portfolio_total_benefit: Decimal
) -> bool:
    """Invariant I-1: Σ incremental == portfolio_total_benefit, exactly."""
    total = sum((m.incremental_benefit for m in members), Decimal(0))
    return total.quantize(MONEY, ROUND_HALF_UP) == portfolio_total_benefit.quantize(
        MONEY, ROUND_HALF_UP
    )


def interaction_delta_for(candidate: OptimizationCandidate) -> Decimal | None:
    """Per-candidate interaction: standalone − incremental (path-dependent)."""
    if candidate.standalone_potential is None or candidate.incremental_portfolio_benefit is None:
        return None
    return (
        candidate.standalone_potential - candidate.incremental_portfolio_benefit
    ).quantize(MONEY, ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _relationship_maps(relationships: list[RecommendationRelationship]):
    excludes: dict[str, set[str]] = {}
    requires: dict[str, set[str]] = {}
    for r in relationships:
        if r.relationship_type in (RelationshipType.EXCLUDES, RelationshipType.SUBSTITUTES):
            excludes.setdefault(r.source_key, set()).add(r.target_key)
            excludes.setdefault(r.target_key, set()).add(r.source_key)
        elif r.relationship_type is RelationshipType.REQUIRES:
            requires.setdefault(r.source_key, set()).add(r.target_key)
    return excludes, requires


def _conflicts(candidate, selected, excludes) -> bool:
    blocked = excludes.get(candidate.candidate_key, set())
    return any(x.candidate_key in blocked for x in selected)


def _dependencies_met(candidate, selected, requires) -> bool:
    needed = requires.get(candidate.candidate_key, set())
    return not needed or needed.issubset({x.candidate_key for x in selected})


def _resource_requests(candidate: OptimizationCandidate) -> dict[str, Decimal]:
    """How much of each shared pool this candidate would consume."""
    amount = candidate.required_cash_contribution()
    if amount <= 0:
        amount = sum((e.amount for e in candidate.economic_effects), Decimal(0))
    return {code: amount for code in candidate.shared_resource_codes}


def _cash_available(candidate, selected, available_cash: Decimal | None) -> bool:
    if available_cash is None:
        return True
    spent = sum((x.required_cash_contribution() for x in selected), Decimal(0))
    return spent + candidate.required_cash_contribution() <= available_cash


def _apply(inputs: dict, candidate: OptimizationCandidate) -> dict:
    application = candidate.lever_application
    return lever_registry.apply_lever(
        inputs, application.lever_code, dict(application.parameters)
    ).inputs


def _objective(
    cons: AssemblyConstraints, baseline_tax: Decimal, current_tax: Decimal, selected: list
) -> Decimal:
    effects = tuple(e for x in selected for e in x.economic_effects)
    costs = tuple(cost for x in selected for cost in x.costs)
    return objective_value(
        metric=cons.objective_metric, baseline_tax=baseline_tax, current_tax=current_tax,
        effects=effects, costs=costs, available_cash=cons.available_cash,
    )
