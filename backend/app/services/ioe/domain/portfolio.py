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
from app.services.ioe.domain.savings import EPSILON_ACCEPT, objective_cost

PORTFOLIO_ASSEMBLY_VERSION = "1.0.0"
ATTRIBUTION_METHOD_VERSION = "1.0.0"

MONEY = Decimal("0.01")
MAX_IMPROVEMENT_ENGINE_RUNS = 30
MAX_DEFERRED_RETESTS = 40

Evaluator = Callable[[dict], Decimal]


class LedgerConservationError(RuntimeError):
    """A shared pool was over-allocated or driven negative."""


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

    def first_exhausted(self, requests: dict[str, Decimal]) -> str | None:
        """Which pool blocked an allocation — reported so the exclusion reason
        names the actual constraint rather than 'a resource'."""
        for code in sorted(requests):
            remaining = self.remaining(code)
            if remaining is not None and requests[code] > remaining:
                return code
        return None

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
        """Atomic: either every allocation lands or none does."""
        snapshot = dict(self.allocated)
        try:
            for code, amount in granted.items():
                self.allocated[code] = self.allocated.get(code, Decimal(0)) + amount
            self.assert_conservation()
        except Exception:
            self.allocated = snapshot          # rollback
            raise

    def release(self, granted: dict[str, Decimal]) -> None:
        """Return a previously committed allocation to its pool."""
        for code, amount in granted.items():
            self.allocated[code] = self.allocated.get(code, Decimal(0)) - amount
        self.assert_conservation()

    def assert_conservation(self) -> None:
        """No pool may be over-allocated or negatively allocated.

        This is the anti-double-counting invariant stated directly: allocated
        never exceeds capacity, and never goes below zero.
        """
        for code, allocated in self.allocated.items():
            if allocated < 0:
                raise LedgerConservationError(
                    f"resource '{code}' has negative allocation {allocated}"
                )
            capacity = self.capacities.get(code)
            if capacity is not None and allocated > capacity:
                raise LedgerConservationError(
                    f"resource '{code}' over-allocated: {allocated} > capacity {capacity}"
                )

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
    # The objective is pinned by CODE and VERSION so a stored portfolio can be
    # re-derived rather than taken on trust.
    objective_metric: ObjectiveMetric = (
        ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE
    )
    objective_version: str = "1.0.0"
    resource_capacities: dict[str, Decimal] = field(default_factory=dict)
    jurisdiction: str | None = None
    tax_year: int | None = None
    max_deferred_retests: int = MAX_DEFERRED_RETESTS
    max_engine_runs: int = 200
    # Optional hook: re-check eligibility against the SAME pinned rule snapshot
    # after earlier actions have changed the facts. Returning False excludes the
    # candidate safely rather than acting on stale eligibility.
    eligibility_recheck: Callable[[OptimizationCandidate, dict], bool] | None = None


# Structured next steps for every exclusion reason the assembler can emit. A
# user is never told only that something was dropped — they are told what would
# change the answer.
RESOLUTION_OPTIONS: dict[str, tuple[str, ...]] = {
    "NOT_PORTFOLIO_EVALUABLE": ("COMPLETE_MISSING_DATA", "CONFIRM_ELIGIBILITY"),
    "LEVER_NOT_APPLICABLE": ("REVIEW_JURISDICTION", "REVIEW_TAX_YEAR"),
    "CONFLICTS_WITH_SELECTED": ("CHOOSE_ONE", "COMPARE_ALTERNATIVES"),
    "DEPENDENCY_NOT_SATISFIED": ("COMPLETE_PREREQUISITE",),
    "SHARED_RESOURCE_EXHAUSTED": ("INCREASE_CONTRIBUTION_ROOM", "SPLIT_ALLOCATION"),
    "INSUFFICIENT_CASH": ("INCREASE_AVAILABLE_CASH", "REDUCE_CONTRIBUTION_AMOUNT"),
    "ELIGIBILITY_CHANGED_BY_EARLIER_ACTION": ("REVIEW_EARLIER_ACTION", "RE_RUN_WITHOUT_IT"),
    "SEARCH_BUDGET_EXHAUSTED": ("RE_RUN_WITH_FEWER_CANDIDATES",),
    "NO_STANDALONE_IMPROVEMENT_AT_THIS_POINT": ("REVIEW_IN_A_LATER_YEAR",),
}


@dataclass(frozen=True)
class ExclusionRecord:
    """Why a candidate is not in the portfolio, kept rather than discarded.

    Excluded candidates are retained deliberately: "not recommended" is itself
    an explainable result, and hiding it would make the portfolio look like the
    whole opportunity set.
    """

    candidate_key: str
    membership: PortfolioMembership
    reason_code: str
    blocking_candidate_key: str | None = None
    shared_resource_code: str | None = None
    resolution_options: tuple[str, ...] = ()

    def as_canonical(self) -> dict:
        return {
            "candidate_key": self.candidate_key,
            "membership": self.membership,
            "reason_code": self.reason_code,
            "blocking_candidate_key": self.blocking_candidate_key,
            "shared_resource_code": self.shared_resource_code,
            "resolution_options": list(self.resolution_options),
        }


@dataclass
class TraceStep:
    """One engine run, recorded so assembly is explainable step by step."""

    step_index: int
    stage: str
    candidate_key: str | None
    apply_order: int | None
    objective_value: Decimal
    objective_delta: Decimal
    accepted: bool | None
    reason_code: str | None = None

    def as_canonical(self) -> dict:
        from app.services.ioe.domain import canonical as _c

        return {
            "step_index": self.step_index, "stage": self.stage,
            "candidate_key": self.candidate_key, "apply_order": self.apply_order,
            "objective_value": _c.money(self.objective_value),
            "objective_delta": _c.money(self.objective_delta),
            "accepted": self.accepted, "reason_code": self.reason_code,
        }


@dataclass
class _State:
    """Mutable assembly state, kept explicit rather than smuggled onto objects."""

    inputs: dict
    objective: Decimal
    engine_runs: int = 0
    selected: list[OptimizationCandidate] = field(default_factory=list)
    allocations: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    trace: list[TraceStep] = field(default_factory=list)
    budget_exhausted: bool = False
    # keyed by candidate_key so a retested candidate's LATEST outcome wins
    exclusions: dict[str, ExclusionRecord] = field(default_factory=dict)


def assemble(
    ranked: list[OptimizationCandidate],
    relationships: list[RecommendationRelationship],
    baseline_inputs: dict,
    evaluate: Evaluator,
    constraints: AssemblyConstraints | None = None,
) -> StrategyPortfolio:
    """Greedy, constrained assembly evaluated entirely through the tax engine.

    Everything — standalone, incremental, and the final combined result — is
    measured with the SAME objective, the SAME baseline, the SAME sign
    convention (positive = improvement), the SAME Decimal policy, and a SINGLE
    rounding point at the end. That is what makes the two reconciliation
    invariants meaningful rather than coincidental.

    The result is a FEASIBLE, DETERMINISTIC, ENGINE-EVALUATED strategy set. It is
    not a globally optimal portfolio, and `optimality_claim` is NONE.
    """
    cons = constraints or AssemblyConstraints()
    ledger = ResourceLedger(capacities=dict(cons.resource_capacities))
    excludes, requires = _relationship_maps(relationships)

    def _reject(
        candidate: OptimizationCandidate,
        membership: PortfolioMembership,
        reason_code: str,
        *,
        blocking_key: str | None = None,
        resource_code: str | None = None,
    ) -> None:
        """Record the exclusion in structured form as well as on the candidate."""
        candidate.portfolio_membership = membership
        candidate.exclusion_reason_code = reason_code
        state.exclusions[candidate.candidate_key] = ExclusionRecord(
            candidate_key=candidate.candidate_key,
            membership=membership,
            reason_code=reason_code,
            blocking_candidate_key=blocking_key,
            shared_resource_code=resource_code,
            resolution_options=RESOLUTION_OPTIONS.get(reason_code, ()),
        )

    baseline_tax = evaluate(dict(baseline_inputs))
    baseline_objective = _objective(cons, baseline_tax, baseline_tax, [])
    state = _State(inputs=dict(baseline_inputs), objective=baseline_objective, engine_runs=1)
    state.trace.append(TraceStep(
        step_index=0, stage="baseline", candidate_key=None, apply_order=None,
        objective_value=baseline_objective, objective_delta=Decimal(0), accepted=None,
    ))

    def _budget_left() -> bool:
        if state.engine_runs >= cons.max_engine_runs:
            state.budget_exhausted = True
            return False
        return True

    def _trace(stage, candidate, objective_val, accepted, reason=None, order=None) -> None:
        state.trace.append(TraceStep(
            step_index=len(state.trace), stage=stage,
            candidate_key=candidate.candidate_key if candidate else None,
            apply_order=order,
            objective_value=objective_val,
            objective_delta=(baseline_objective - objective_val),
            accepted=accepted, reason_code=reason,
        ))

    # ---- standalone: each evaluable candidate measured against the BASELINE ----
    for candidate in ranked:
        if not candidate.is_portfolio_evaluable:
            _reject(candidate, PortfolioMembership.EXCLUDED_NOT_EVALUABLE,
                    "NOT_PORTFOLIO_EVALUABLE")
            continue
        if not _budget_left():
            break
        try:
            trial = _apply(baseline_inputs, candidate, cons)
        except Exception:                       # invalid lever for this context
            _reject(candidate, PortfolioMembership.EXCLUDED_NOT_EVALUABLE,
                    "LEVER_NOT_APPLICABLE")
            continue
        trial_tax = evaluate(trial)
        state.engine_runs += 1
        standalone_objective = _objective(cons, baseline_tax, trial_tax, [candidate])
        candidate.standalone_potential = (
            baseline_objective - standalone_objective
        ).quantize(MONEY, ROUND_HALF_UP)
        _trace("standalone", candidate, standalone_objective, None)

    def try_admit(candidate) -> tuple[str, dict | None, Decimal | None]:
        """Returns (outcome, inputs, objective). 'hard_reject' means a constraint
        forbids it; 'no_improvement' means it did not pay off AT THIS POINT, and
        the caller defers rather than discards."""
        if not candidate.is_portfolio_evaluable:
            _reject(candidate, PortfolioMembership.EXCLUDED_NOT_EVALUABLE,
                    "NOT_PORTFOLIO_EVALUABLE")
            return "hard_reject", None, None
        blocker = _blocking_key(candidate, state.selected, excludes)
        if blocker is not None:
            _reject(candidate, PortfolioMembership.EXCLUDED_CONFLICT,
                    "CONFLICTS_WITH_SELECTED", blocking_key=blocker)
            return "hard_reject", None, None
        if not _dependencies_met(candidate, state.selected, requires):
            _reject(candidate, PortfolioMembership.DEFERRED_TIMING,
                    "DEPENDENCY_NOT_SATISFIED",
                    blocking_key=_first_missing_dependency(
                        candidate, state.selected, requires
                    ))
            return "hard_reject", None, None
        requests = _resource_requests(candidate)
        if ledger.try_allocate(requests) is None:
            _reject(candidate, PortfolioMembership.EXCLUDED_CONSTRAINT,
                    "SHARED_RESOURCE_EXHAUSTED",
                    resource_code=ledger.first_exhausted(requests))
            return "hard_reject", None, None
        if not _cash_available(candidate, state.selected, cons.available_cash):
            _reject(candidate, PortfolioMembership.EXCLUDED_CONSTRAINT,
                    "INSUFFICIENT_CASH")
            return "hard_reject", None, None
        # Earlier actions may have changed the facts this candidate depends on.
        # Re-check against the SAME pinned rule snapshot; on doubt, exclude.
        if cons.eligibility_recheck is not None:
            if not cons.eligibility_recheck(candidate, state.inputs):
                _reject(candidate, PortfolioMembership.EXCLUDED_CONSTRAINT,
                        "ELIGIBILITY_CHANGED_BY_EARLIER_ACTION")
                return "hard_reject", None, None
        if not _budget_left():
            _reject(candidate, PortfolioMembership.EXCLUDED_CONSTRAINT,
                    "SEARCH_BUDGET_EXHAUSTED")
            return "hard_reject", None, None

        try:
            trial_inputs = _apply(state.inputs, candidate, cons)
        except Exception:
            _reject(candidate, PortfolioMembership.EXCLUDED_NOT_EVALUABLE,
                    "LEVER_NOT_APPLICABLE")
            return "hard_reject", None, None
        trial_tax = evaluate(trial_inputs)
        state.engine_runs += 1
        trial_objective = _objective(
            cons, baseline_tax, trial_tax, [*state.selected, candidate]
        )
        if state.objective - trial_objective < EPSILON_ACCEPT:   # cost must FALL
            _trace("incremental_trial", candidate, trial_objective, False,
                   "NO_IMPROVEMENT")
            return "no_improvement", None, None
        _trace("incremental_trial", candidate, trial_objective, True)
        return "accepted", trial_inputs, trial_objective

    def accept(candidate, trial_inputs: dict, trial_objective: Decimal) -> None:
        granted = ledger.try_allocate(_resource_requests(candidate)) or {}
        ledger.commit(granted)                  # conservation asserted inside
        state.allocations[candidate.candidate_key] = granted
        candidate.incremental_portfolio_benefit = (
            state.objective - trial_objective
        ).quantize(MONEY, ROUND_HALF_UP)
        candidate.portfolio_membership = PortfolioMembership.SELECTED
        candidate.exclusion_reason_code = None
        state.exclusions.pop(candidate.candidate_key, None)
        state.selected.append(candidate)
        state.inputs = trial_inputs
        state.objective = trial_objective
        _trace("accepted", candidate, trial_objective, True,
               order=len(state.selected) - 1)

    # ---- greedy pass ----
    deferred: list[OptimizationCandidate] = []
    for candidate in ranked:
        outcome, trial_inputs, trial_objective = try_admit(candidate)
        if outcome == "accepted":
            accept(candidate, trial_inputs, trial_objective)
        elif outcome == "no_improvement":
            _reject(candidate, PortfolioMembership.DEFERRED_PENDING_COMBINATION,
                    "NO_STANDALONE_IMPROVEMENT_AT_THIS_POINT")
            deferred.append(candidate)

    # ---- deferred reconsideration ----
    retests = 0
    changed = True
    while changed and retests < cons.max_deferred_retests:
        changed = False
        for candidate in list(deferred):
            if retests >= cons.max_deferred_retests or not _budget_left():
                break
            retests += 1
            outcome, trial_inputs, trial_objective = try_admit(candidate)
            if outcome == "accepted":
                deferred.remove(candidate)
                accept(candidate, trial_inputs, trial_objective)
                changed = True
            elif outcome == "no_improvement":
                _reject(candidate, PortfolioMembership.DEFERRED_PENDING_COMBINATION,
                        "NO_STANDALONE_IMPROVEMENT_AT_THIS_POINT")

    # ---- final combined run: THE authoritative result ----
    final_tax = evaluate(state.inputs)
    state.engine_runs += 1
    final_objective = _objective(cons, baseline_tax, final_tax, state.selected)
    _trace("final_combined", None, final_objective, None)

    objective_delta = (baseline_objective - final_objective).quantize(MONEY, ROUND_HALF_UP)

    # Invariant I-1: the delta equals the sum of incremental deltas in EXACT
    # apply order. Structural (the increments telescope), so it is exact.
    incremental_sum = sum(
        (x.incremental_portfolio_benefit or Decimal(0) for x in state.selected), Decimal(0)
    ).quantize(MONEY, ROUND_HALF_UP)
    if incremental_sum != objective_delta:
        raise PortfolioReconciliationError(
            f"I-1 violated: incremental deltas sum to {incremental_sum} but the "
            f"portfolio delta is {objective_delta}"
        )

    # Invariant I-2: the delta equals baseline objective minus the FINAL COMBINED
    # objective — i.e. the headline is reproducible by re-running the engine.
    if objective_delta != (baseline_objective - final_objective).quantize(MONEY, ROUND_HALF_UP):
        raise PortfolioReconciliationError("I-2 violated: objective delta is not reproducible")
    if state.selected and abs(state.objective - final_objective) > EPSILON_ACCEPT:
        raise PortfolioReconciliationError(
            f"I-2 violated: combined run objective {final_objective} disagrees with "
            f"the last accepted trial {state.objective}"
        )

    ledger.assert_conservation()

    sum_of_standalone = sum(
        (x.standalone_potential or Decimal(0) for x in state.selected), Decimal(0)
    ).quantize(MONEY, ROUND_HALF_UP)
    interaction_delta = (sum_of_standalone - objective_delta).quantize(MONEY, ROUND_HALF_UP)

    members = tuple(
        PortfolioMember(
            candidate_key=x.candidate_key, apply_order=i,
            incremental_benefit=x.incremental_portfolio_benefit or Decimal(0),
            resource_allocations=tuple(
                sorted(state.allocations.get(x.candidate_key, {}).items())
            ),
        )
        for i, x in enumerate(state.selected)
    )

    breakdown = _decompose_selected(state.selected)
    return StrategyPortfolio(
        members=members,
        ledger=ledger.entries(),
        baseline_tax=baseline_tax.quantize(MONEY, ROUND_HALF_UP),
        portfolio_tax=final_tax.quantize(MONEY, ROUND_HALF_UP),
        portfolio_total_benefit=objective_delta,
        sum_of_standalone=sum_of_standalone,
        interaction_delta=interaction_delta,
        additivity_class=classify_additivity(interaction_delta),
        additivity_verified=abs(interaction_delta) <= EPSILON_ACCEPT,
        objective_metric=cons.objective_metric,
        objective_code=cons.objective_metric.value,
        objective_version=cons.objective_version,
        objective_value_baseline=baseline_objective,
        objective_value_final=final_objective,
        objective_delta=objective_delta,
        # A rule-based assembler makes no optimality claim.
        assembly_method=AssemblyMethod.GREEDY_RANKED,
        optimality_claim=OptimalityClaim.NONE,
        search_budget_exhausted=state.budget_exhausted,
        deferred_count=len(deferred),
        excluded_count=sum(1 for x in ranked if x.portfolio_membership in _HARD_EXCLUSIONS),
        engine_runs_used=state.engine_runs,
        unexplored_alternatives_count=len(deferred) + sum(
            1 for x in ranked
            if x.portfolio_membership is PortfolioMembership.EXCLUDED_CONFLICT
        ),
        savings=breakdown,
        trace=tuple(state.trace),
        exclusions=tuple(
            state.exclusions[key] for key in sorted(state.exclusions)
        ),
    )


def _decompose_selected(selected):
    from app.services.ioe.domain.savings import decompose

    effects = tuple(e for x in selected for e in x.economic_effects)
    costs = tuple(cost for x in selected for cost in x.costs)
    return decompose(effects, costs)


_HARD_EXCLUSIONS = frozenset({
    PortfolioMembership.EXCLUDED_CONFLICT,
    PortfolioMembership.EXCLUDED_CONSTRAINT,
    PortfolioMembership.EXCLUDED_NOT_EVALUABLE,
    PortfolioMembership.DEFERRED_TIMING,
})


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


def _blocking_key(candidate, selected, excludes) -> str | None:
    """The selected candidate that forbids this one, or None."""
    blocked = excludes.get(candidate.candidate_key, set())
    for x in selected:
        if x.candidate_key in blocked:
            return x.candidate_key
    return None


def _dependencies_met(candidate, selected, requires) -> bool:
    needed = requires.get(candidate.candidate_key, set())
    return not needed or needed.issubset({x.candidate_key for x in selected})


def _first_missing_dependency(candidate, selected, requires) -> str | None:
    needed = requires.get(candidate.candidate_key, set())
    present = {x.candidate_key for x in selected}
    missing = sorted(needed - present)
    return missing[0] if missing else None


def _resource_requests(candidate: OptimizationCandidate) -> dict[str, Decimal]:
    """How much of each shared pool this candidate would consume."""
    amount = candidate.liquidity_commitment()
    if amount <= 0:
        amount = sum((e.amount for e in candidate.economic_effects), Decimal(0))
    return {code: amount for code in candidate.shared_resource_codes}


def _cash_available(candidate, selected, available_cash: Decimal | None) -> bool:
    if available_cash is None:
        return True
    spent = sum((x.liquidity_commitment() for x in selected), Decimal(0))
    return spent + candidate.liquidity_commitment() <= available_cash


def _apply(
    inputs: dict, candidate: OptimizationCandidate, cons: AssemblyConstraints
) -> dict:
    """Resolve the lever ONLY through the pinned registry, validating the
    allow-list, parameter bounds, jurisdiction, and tax year. Rule data never
    supplies a direct engine-field mutation path."""
    application = candidate.lever_application
    return lever_registry.apply_lever(
        inputs, application.lever_code, dict(application.parameters),
        jurisdiction=cons.jurisdiction, tax_year=cons.tax_year,
    ).inputs


def _objective(
    cons: AssemblyConstraints, baseline_tax: Decimal, current_tax: Decimal, selected: list
) -> Decimal:
    """The objective as a MINIMIZED cost, so delta = baseline - final is positive
    for an improvement. `baseline_tax` is unused here and kept for signature
    stability; the cost depends only on the CURRENT state."""
    effects = tuple(e for x in selected for e in x.economic_effects)
    costs = tuple(cost for x in selected for cost in x.costs)
    return objective_cost(
        metric=cons.objective_metric, current_tax=current_tax,
        effects=effects, costs=costs, available_cash=cons.available_cash,
    )
