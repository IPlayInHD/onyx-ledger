"""Replay services — re-execute a sealed calculation and re-derive its identity.

Three replays, one shape each: resolve the pinned dependencies, re-run the
deterministic calculation the original used, rebuild the canonical payload the
hash was taken over, and hand back an actual hash for the verifier to compare.
None of them writes anything, and none of them decides anything: a replay
returns a hash, and `IntegrityVerificationService` turns hashes into verdicts.

Canonicalization and hashing stay in `domain.canonical`. Nothing here
re-implements a digest.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    OptimizationCandidate as CandidateRow,
)
from app.database.models import (
    OptimizationRun,
    PortfolioExclusion,
    PortfolioMember,
    ResourceLedgerEntry,
    Scenario,
    ScenarioResult,
    StrategyPortfolio,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.domain.integrity import (
    DependencyUnavailable,
    IntegrityReason,
    SealedEvidenceIncomplete,
)
from app.services.ioe.domain.scenario import DERIVED_STATE_BEARING_VERSIONS
from app.services.ioe.frozen import FrozenScenarioInputService, FrozenSnapshotError
from app.services.ioe.frozen.models import (
    PINNED_SCENARIO_BASELINE_HASH_MISMATCH,
    PINNED_SCENARIO_BASELINE_UNAVAILABLE,
    SCENARIO_EXECUTION_POLICY_INVALID,
    SCENARIO_EXECUTION_POLICY_VERSION,
    InputExecutionPolicy,
    scenario_execution_policy,
)
from app.services.ioe.portfolio.service import engine_evaluator
from app.services.ioe.replay.resolver import (
    ReplayDependencyResolver,
    ResolvedDependencies,
)
from app.services.ioe.scenario import counterfactual, held_evidence

if TYPE_CHECKING:   # runtime import stays local: the domain module imports back
    from app.services.ioe.domain.scenario import ScenarioSpec

MONEY = Decimal("0.01")

#: Returned in place of a reconciled counterfactual hash when the sealed
#: derived state cannot be reconciled. Deliberately not a hash: binding it into
#: the outer canonical result guarantees the result-hash comparison fails, so a
#: corrupted v2 artifact reports MISMATCH through the ordinary path instead of
#: needing a second verdict channel. It can never collide with a real digest
#: because it is not 64 hex characters.
_IRRECONCILABLE_DERIVED_STATE = "COUNTERFACTUAL_DERIVED_STATE_IRRECONCILABLE"

# A scenario refusal, expressed in the verifier's vocabulary. Everything that is
# not specifically about the baseline RESULT is a snapshot-level dependency
# problem: nothing was compared, so the verdict is `unavailable`, never
# `mismatch`.
_SCENARIO_DEPENDENCY_REASON: dict[str, IntegrityReason] = {
    PINNED_SCENARIO_BASELINE_UNAVAILABLE: IntegrityReason.BASELINE_RESULT_UNAVAILABLE,
    PINNED_SCENARIO_BASELINE_HASH_MISMATCH: (
        IntegrityReason.BASELINE_RESULT_UNAVAILABLE),
    SCENARIO_EXECUTION_POLICY_INVALID: IntegrityReason.SEALED_EVIDENCE_INCOMPLETE,
}


def _refuse_legacy(policy: str | None, current: str) -> None:
    """Refuse to replay a result produced before the frozen-input correction.

    Replaying one would compare its sealed numbers against a snapshot it was
    never computed from. The difference that produces is not a replay
    regression — the guarantee simply did not exist yet — so reporting it as a
    `mismatch` would assert something no evidence supports, and would bury real
    regressions inside a population of old rows.

    `unavailable` with an explicit legacy reason is the honest verdict: the
    dependency that would make verification possible was never recorded.
    """
    if (policy or "") != current:
        raise DependencyUnavailable(
            IntegrityReason.LEGACY_EXECUTION_POLICY_UNVERIFIABLE)


@dataclass
class ReplayOutcome:
    """What a replay observed. Never a verdict — the verifier decides."""

    expected_hash: str
    actual_hash: str | None
    expected_spec_hash: str | None = None
    actual_spec_hash: str | None = None
    engine_runs: int = 0
    mismatch_reason: IntegrityReason = IntegrityReason.RESULT_HASH_MISMATCH

    @property
    def matches(self) -> bool:
        return self.actual_hash is not None and self.actual_hash == self.expected_hash


class OptimizationReplayService:
    """Re-runs the sealed optimization against its pinned dependencies."""

    def __init__(self, user_id: uuid.UUID):
        self.user_id = user_id

    async def replay(self, run_id: uuid.UUID) -> ReplayOutcome:
        # Imported here: the orchestrator imports the replay package for its own
        # integrity hooks, and a module-level import would close the cycle.
        from app.services.ioe.orchestrator import OptimizationOrchestrator, PinnedSpec

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            run = await session.get(OptimizationRun, run_id)
            if run is None or run.user_id != self.user_id:
                raise SealedEvidenceIncomplete()
            if run.workflow_status != "completed" or not run.optimization_result_hash:
                raise SealedEvidenceIncomplete()
            _refuse_legacy(
                run.input_execution_policy_version,
                InputExecutionPolicy.FROZEN_SNAPSHOT_V1.value,
            )

            deps = await ReplayDependencyResolver(session, self.user_id).for_optimization(run)
            expected_result_hash = run.optimization_result_hash
            expected_spec_hash = run.optimization_spec_hash
            weight_config_version = str(
                deps.version_manifest.get("weight_config_version", "default"))
            constraints = dict(run.user_constraints or {})
            assumption_set = list(run.assumption_set or [])
            spec_inputs_stored = run.user_constraints is not None

        spec = PinnedSpec(
            analysis_id=run.analysis_id,
            tax_year=deps.tax_year,
            jurisdiction=deps.jurisdiction,
            baseline_input_snapshot_hash=deps.baseline_input_snapshot_hash,
            rule_snapshot_id=deps.rule_snapshot_id,
            rule_snapshot_hash=deps.rule_snapshot_hash,
            pinned_rule_version_ids=deps.pinned_rule_version_ids,
            weight_config_id=None,
            weight_config_version=weight_config_version,
            version_manifest=deps.version_manifest,
            manifest_hash=c.version_manifest_hash(deps.version_manifest),
            user_constraints=constraints,
            assumption_set=assumption_set,
            # The sealed dataset, rebuilt from the snapshot artifact. `_compute`
            # takes its constants from here, so the replay computes from the tax
            # law the run was sealed under rather than today's rows.
            dataset=deps.dataset,
        )
        # The RESULT hash anchors on the spec hash that was sealed, so a result
        # is verifiable even on a run whose spec inputs predate being stored.
        spec.spec_hash = expected_spec_hash or ""

        actual_spec_hash = None
        if spec_inputs_stored:
            actual_spec_hash = c.optimization_spec_hash(
                baseline_input_snapshot_hash=spec.baseline_input_snapshot_hash,
                tax_year=spec.tax_year,
                jurisdiction=spec.jurisdiction,
                rule_version_set=spec.pinned_rule_version_ids,
                version_manifest=spec.version_manifest,
                user_constraints=spec.user_constraints,
                assumption_set=spec.assumption_set,
            )

        orchestrator = OptimizationOrchestrator(self.user_id)
        computed = await orchestrator._compute(spec, baseline_input=deps.baseline_inputs)

        portfolio = computed.get("portfolio")
        result_payload = {
            "candidates": [x.as_canonical() for x in computed["candidates"]],
            "relationships": [r.as_canonical() for r in computed["relationships"]],
            "portfolio": portfolio.as_canonical() if portfolio else None,
        }
        actual = c.optimization_result_hash(
            spec_hash=spec.spec_hash, result=result_payload)

        return ReplayOutcome(
            expected_hash=expected_result_hash,
            actual_hash=actual,
            expected_spec_hash=expected_spec_hash,
            actual_spec_hash=actual_spec_hash,
            engine_runs=portfolio.engine_runs_used if portfolio else 0,
            mismatch_reason=IntegrityReason.RESULT_HASH_MISMATCH,
        )


class PortfolioReplayService:
    """Re-derives a sealed portfolio's identity from its own persisted rows.

    The portfolio is the one entity whose canonical form is FULLY persisted —
    members, ledger, exclusions, every objective value and every per-concept
    total — so its expected identity can be rebuilt from storage and does not
    depend on re-running the rules layer. The engine is still re-run over the
    members in apply order, because a hash that matched while the objective
    values no longer reproduced would be a hash of the wrong thing.
    """

    def __init__(self, user_id: uuid.UUID):
        self.user_id = user_id

    async def replay(self, portfolio_id: uuid.UUID) -> ReplayOutcome:
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            portfolio = await session.get(StrategyPortfolio, portfolio_id)
            if portfolio is None:
                raise SealedEvidenceIncomplete()
            run = await session.get(OptimizationRun, portfolio.run_id)
            if run is None or run.user_id != self.user_id:
                raise SealedEvidenceIncomplete()
            if not portfolio.portfolio_result_hash:
                # Sealed before the portfolio hash was written. Nothing to
                # compare against; that is unavailable, not a mismatch.
                raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)
            # A portfolio has no policy of its own; it inherits its run's, and
            # a legacy run's portfolio is exactly as unverifiable as the run.
            _refuse_legacy(
                run.input_execution_policy_version,
                InputExecutionPolicy.FROZEN_SNAPSHOT_V1.value,
            )

            deps = await ReplayDependencyResolver(session, self.user_id).for_optimization(run)
            canonical, engine_runs = await self._rebuild(session, portfolio, deps)
            expected = portfolio.portfolio_result_hash

        return ReplayOutcome(
            expected_hash=expected,
            actual_hash=c.portfolio_result_hash(canonical),
            engine_runs=engine_runs,
            mismatch_reason=IntegrityReason.PORTFOLIO_HASH_MISMATCH,
        )

    async def _rebuild(
        self,
        session: AsyncSession,
        portfolio: StrategyPortfolio,
        deps: ResolvedDependencies,
    ) -> tuple[dict[str, Any], int]:
        members = list(await session.scalars(
            select(PortfolioMember)
            .where(PortfolioMember.portfolio_id == portfolio.id)
            .order_by(PortfolioMember.apply_order)
        ))
        ledger = list(await session.scalars(
            select(ResourceLedgerEntry)
            .where(ResourceLedgerEntry.portfolio_id == portfolio.id)
        ))
        exclusions = list(await session.scalars(
            select(PortfolioExclusion)
            .where(PortfolioExclusion.portfolio_id == portfolio.id)
        ))
        keys = await self._candidate_keys(
            session, [m.candidate_id for m in members]
            + [x.candidate_id for x in exclusions]
            + [x.blocking_candidate_id for x in exclusions if x.blocking_candidate_id]
        )

        engine_runs = self._verify_objective(portfolio, deps)
        self._verify_invariants(portfolio, members)

        breakdown = {
            "current_year_reduction": c.money(portfolio.total_tax_reduction),
            "refund_impact": c.money(portfolio.total_refund_impact),
            "refundable_benefit": c.money(portfolio.total_refundable_benefit),
            "deferral_amount": c.money(portfolio.total_deferral_amount),
            "recurring_annual": c.money(portfolio.total_recurring_annual),
            "multi_year_projected": c.money(portfolio.total_multi_year_projected),
            "future_option_value": c.money(portfolio.total_future_option_value),
            "liquidity_commitment": c.money(portfolio.total_liquidity_commitment),
            "asset_transfer": c.money(portfolio.total_asset_transfer),
            "nonrecoverable_expenditure": c.money(
                portfolio.total_nonrecoverable_expenditure),
            "implementation_cost": c.money(portfolio.total_implementation_cost),
            "net_current_year_benefit": c.money(portfolio.net_current_year_benefit),
        }
        canonical = {
            "members": [
                {
                    "candidate_key": keys.get(m.candidate_id, ""),
                    "apply_order": m.apply_order,
                    "incremental_benefit": c.money(m.incremental_benefit),
                    "resource_allocations": [
                        {"resource_code": code, "amount": c.money(Decimal(str(amount)))}
                        for code, amount in sorted((m.resource_allocations or {}).items())
                    ],
                }
                for m in members
            ],
            "ledger": [
                {
                    "resource_code": e.resource_code,
                    "capacity": c.money(e.capacity),
                    "allocated": c.money(e.allocated),
                    "remaining": c.money(e.remaining),
                }
                for e in sorted(ledger, key=lambda e: e.resource_code)
            ],
            "baseline_tax": c.money(portfolio.baseline_tax),
            "portfolio_tax": c.money(portfolio.portfolio_tax),
            "portfolio_total_benefit": c.money(portfolio.portfolio_total_benefit),
            "sum_of_standalone": c.money(portfolio.sum_of_standalone),
            "interaction_delta": c.money(portfolio.interaction_delta),
            "additivity_class": portfolio.additivity_class,
            "additivity_verified": portfolio.additivity_verified,
            "objective_metric": portfolio.objective_metric,
            "objective_value_baseline": c.money(portfolio.objective_value_baseline),
            "objective_value_final": c.money(portfolio.objective_value_final),
            "assembly_method": portfolio.assembly_method,
            "optimality_claim": portfolio.optimality_claim,
            "objective_code": portfolio.portfolio_objective_code,
            "objective_version": portfolio.portfolio_objective_version,
            "objective_delta": c.money(portfolio.objective_delta),
            "search_budget_exhausted": portfolio.search_budget_exhausted,
            "savings": breakdown,
            # SORTED BY candidate_key, BECAUSE THE WRITER SEALED THEM THAT WAY.
            # `PortfolioAssemblyState` emits `sorted(state.exclusions)` — keyed
            # by candidate_key — so the hash was taken over that order. This
            # rebuild used raw fetch order from an unordered SELECT, which
            # agrees with it only by luck: any different plan or page layout
            # reorders the list and an untampered portfolio reports
            # PORTFOLIO_HASH_MISMATCH, the alert that means evidence was
            # altered. Caught by CI on a fresh database, where the orders
            # diverged.
            "exclusions": [
                {
                    "candidate_key": keys.get(x.candidate_id, ""),
                    "membership": x.membership,
                    "reason_code": x.reason_code,
                    "blocking_candidate_key": (
                        keys.get(x.blocking_candidate_id)
                        if x.blocking_candidate_id else None
                    ),
                    "shared_resource_code": x.shared_resource_code,
                    "resolution_options": list(x.resolution_options or []),
                }
                for x in sorted(
                    exclusions, key=lambda e: keys.get(e.candidate_id, "")
                )
            ],
            "deferred_count": portfolio.deferred_count,
            "excluded_count": portfolio.excluded_count,
            "improvement_moves_applied": portfolio.improvement_moves_applied,
            "engine_runs_used": portfolio.engine_runs_used,
            "unexplored_alternatives_count": portfolio.unexplored_alternatives_count,
        }
        return canonical, engine_runs

    @staticmethod
    async def _candidate_keys(session: AsyncSession, ids: list) -> dict:
        wanted = {i for i in ids if i is not None}
        if not wanted:
            return {}
        rows = await session.scalars(
            select(CandidateRow).where(CandidateRow.id.in_(wanted)))
        return {
            r.id: f"{r.opportunity_code}:{r.tax_rule_version_id}" for r in rows
        }

    @staticmethod
    def _verify_objective(
        portfolio: StrategyPortfolio, deps: ResolvedDependencies
    ) -> int:
        """The baseline objective must still come out of the ENGINE.

        Re-derived from the pinned baseline inputs rather than trusted from the
        stored column, so a rewritten `objective_value_baseline` is caught even
        though the row it lives on is immutable.
        """
        baseline_tax = engine_evaluator(
            {f: getattr(deps.baseline_inputs, f)
             for f in deps.baseline_inputs.__dataclass_fields__}
        ).quantize(MONEY, ROUND_HALF_UP)
        expected_baseline = savings_domain.objective_cost(
            metric=savings_domain.PORTFOLIO_OBJECTIVE_CODE, current_tax=baseline_tax,
        ).quantize(MONEY, ROUND_HALF_UP)
        stored = (portfolio.objective_value_baseline or Decimal(0)).quantize(
            MONEY, ROUND_HALF_UP)
        if expected_baseline != stored:
            raise PortfolioObjectiveMismatch()
        return 1

    @staticmethod
    def _verify_invariants(
        portfolio: StrategyPortfolio, members: Sequence[PortfolioMember]
    ) -> None:
        """I-1 telescoping and I-2 reconciliation, over the STORED rows."""
        total = sum(
            (m.incremental_benefit or Decimal(0) for m in members), Decimal(0)
        ).quantize(MONEY, ROUND_HALF_UP)
        delta = (portfolio.objective_delta or Decimal(0)).quantize(MONEY, ROUND_HALF_UP)
        if total != delta:
            raise PortfolioInvariantViolated()          # I-1
        benefit = (portfolio.portfolio_total_benefit or Decimal(0)).quantize(
            MONEY, ROUND_HALF_UP)
        if benefit != delta:
            raise PortfolioInvariantViolated()          # I-2


class PortfolioObjectiveMismatch(Exception):
    """The engine no longer reproduces the stored baseline objective."""


class PortfolioInvariantViolated(Exception):
    """I-1 or I-2 does not hold over the stored rows."""


class ScenarioReplayService:
    """Re-runs the sealed scenario against its pinned baseline and registries."""

    def __init__(self, user_id: uuid.UUID):
        self.user_id = user_id

    async def replay(self, scenario_id: uuid.UUID) -> ReplayOutcome:
        from app.services.ioe.scenario.service import (
            PinnedScenarioSpec,
            ScenarioService,
        )

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            scenario = await session.get(Scenario, scenario_id)
            if scenario is None or scenario.user_id != self.user_id:
                raise SealedEvidenceIncomplete()
            if scenario.workflow_status != "completed" or not scenario.scenario_result_hash:
                raise SealedEvidenceIncomplete()

            # Before any dependency work: was this scenario computed under a
            # policy that makes deterministic replay meaningful at all?
            try:
                policy = scenario_execution_policy(scenario.version_manifest)
            except FrozenSnapshotError as exc:
                raise DependencyUnavailable(
                    _SCENARIO_DEPENDENCY_REASON.get(
                        exc.reason, IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)
                ) from exc
            _refuse_legacy(policy, SCENARIO_EXECUTION_POLICY_VERSION.value)

            deps = await ReplayDependencyResolver(
                session, self.user_id).for_scenario(scenario)
            expected = scenario.scenario_result_hash
            spec_hash = scenario.scenario_spec_hash or ""
            # A sealed row that does not say which contract it was hashed under
            # cannot be replayed against any contract. Fail closed as an
            # unavailable dependency rather than assuming v1: assuming would
            # produce a confident MISMATCH for an artifact nobody can interpret.
            sealed_schema_version = scenario.result_schema_version
            if not sealed_schema_version:
                raise DependencyUnavailable(
                    IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)
            spec = await self._sealed_spec(session, scenario_id)

            # The replay is confined to the same frozen input production was
            # confined to, resolved by the same service. `expected_snapshot_hash`
            # makes a replaced snapshot an UNAVAILABLE dependency rather than a
            # mismatch: comparing against a different input proves nothing.
            #
            # The baseline RESULT identity is deliberately NOT asserted here.
            # The baseline is re-derived from the frozen snapshot exactly as it
            # is in production, so a baseline that no longer reproduces changes
            # `tax_delta` and surfaces as a result-hash mismatch — which is the
            # honest verdict, and the one a legacy live-baseline scenario earns.
            try:
                frozen = await FrozenScenarioInputService(
                    session, self.user_id
                ).resolve(
                    scenario.base_analysis_id,
                    objective_code=deps.objective_code,
                    objective_version=deps.objective_version,
                    lever_registry_version=str(
                        deps.version_manifest.get("lever_registry_version", "")),
                    assumption_registry_version=str(
                        deps.version_manifest.get("assumption_registry_version", "")),
                    support_score_version=str(
                        deps.version_manifest.get("confidence_algorithm_version", "")),
                    scenario_id=scenario_id,
                    scenario_spec_hash=spec_hash,
                    expected_snapshot_hash=scenario.baseline_input_snapshot_hash,
                )
            except FrozenSnapshotError as exc:
                raise DependencyUnavailable(
                    _SCENARIO_DEPENDENCY_REASON.get(
                        exc.reason, IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE)
                ) from exc

            # Loaded only for a version that binds it. A v1 result hash commits
            # to nothing in this row, so reading it would add `ioe.scenario_result`
            # to what every verification consumes — a measured privacy input
            # (`tests/privacy/test_verification_consumers.py`), not an
            # implementation detail.
            sealed_result = (
                await session.scalar(
                    select(ScenarioResult).where(
                        ScenarioResult.scenario_id == scenario_id))
                if sealed_schema_version in DERIVED_STATE_BEARING_VERSIONS
                else None
            )

        pinned = PinnedScenarioSpec(
            analysis_id=scenario.base_analysis_id,
            tax_year=deps.tax_year,
            jurisdiction=deps.jurisdiction,
            frozen=frozen,
            rule_snapshot_id=deps.rule_snapshot_id,
            rule_snapshot_hash=deps.rule_snapshot_hash,
            pinned_rule_version_ids=list(deps.pinned_rule_version_ids),
            objective_code=deps.objective_code,
            objective_version=deps.objective_version,
            version_manifest=deps.version_manifest,
            manifest_hash=c.version_manifest_hash(deps.version_manifest),
            spec=spec,
            spec_hash=spec_hash,
            dataset=deps.dataset,
        )

        service = ScenarioService(self.user_id)
        computed = service._compute(pinned)

        derived_hash: str | None = None
        if sealed_schema_version in DERIVED_STATE_BEARING_VERSIONS:
            derived_hash = await self._verify_counterfactual_state(
                service, pinned, computed, sealed_result)

        # THE VERSION COMES FROM THE ROW, never from the current-write constant.
        # A sealed scenario was hashed under the contract it named, and replay
        # exists to reproduce that contract — reading the build's current
        # version here is precisely the defect this fix removes, because it
        # would make every historical hash follow whatever the newest code
        # writes. An unsupported version raises rather than falling back.
        actual = c.scenario_result_hash(
            spec_hash=spec_hash,
            result=service.canonical_result(
                computed,
                result_schema_version=sealed_schema_version,
                counterfactual_derived_state_hash=derived_hash,
            ),
        )

        return ReplayOutcome(
            expected_hash=expected, actual_hash=actual, expected_spec_hash=spec_hash,
            engine_runs=1, mismatch_reason=IntegrityReason.RESULT_HASH_MISMATCH,
        )

    async def _verify_counterfactual_state(
        self,
        service: Any,
        pinned: Any,
        computed: dict,
        sealed_result: ScenarioResult | None,
    ) -> str:
        """Reconcile a v2 scenario's sealed counterfactual state, and return the
        hash the outer result must be canonicalized with.

        TWO INDEPENDENT CHECKS, because they catch different edits and neither
        subsumes the other.

        1. THE STORED PAYLOAD AGAINST THE STORED DIGEST. Hashing the bytes that
           are actually in the column is the only thing that notices a payload
           edited in place, or a digest edited beside an intact payload. A
           rebuild cannot see either: it would agree with the true digest in
           both cases and report VERIFIED over corrupted evidence.

        2. THE REBUILD AGAINST THE STORED DIGEST. Hashing a fresh derivation is
           the only thing that notices the underlying determination no longer
           reproducing — a pinned rule whose data moved, a builder whose output
           changed.

        Both feed the same verdict rather than raising, so a corrupted artifact
        is reported through the ordinary mismatch path instead of as an
        exception the caller has to interpret.
        """
        if (sealed_result is None
                or sealed_result.counterfactual_derived_state is None
                or not sealed_result.counterfactual_derived_state_hash):
            # A v2 seal without its evidence is not a mismatch — there is
            # nothing to compare against. Fail closed as incomplete.
            raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)

        stored_payload = sealed_result.counterfactual_derived_state
        stored_hash = sealed_result.counterfactual_derived_state_hash

        if counterfactual.payload_hash(stored_payload) != stored_hash:
            # The row contradicts itself. Returning the stored hash here would
            # let the outer comparison succeed over evidence that does not
            # match its own digest, so return a value that cannot reconcile and
            # let the ordinary result-hash comparison report the mismatch.
            return _IRRECONCILABLE_DERIVED_STATE

        # THE SEALED SNAPSHOT, NOT THE LIVE LIBRARY. Held evidence is read back
        # from the artifact under verification: re-capturing it would answer
        # "what does this user hold today", and a slip uploaded after sealing
        # would then surface as a hash mismatch indistinguishable from tampering.
        sealed_evidence = held_evidence.from_payload(
            stored_payload.get("baseline_held_evidence") or {})

        # THE SEALED DERIVED-STATE VERSION, NOT THIS BUILD'S CURRENT ONE. The
        # same rule the outer result hash already follows: an artifact was
        # hashed under the contract it names, so a rebuild under a newer one
        # would report every older artifact as irreconcilable the moment the
        # contract moved. Read from the payload itself, which is the only place
        # that records what this row was actually sealed as.
        sealed_derived_version = str(
            stored_payload.get("schema_version")
            or counterfactual.DERIVED_STATE_SCHEMA_V1)

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            rebuilt = await service.build_counterfactual_derived_state(
                session, pinned, computed,
                baseline_held_evidence=sealed_evidence,
                derived_state_schema_version=sealed_derived_version)
        rebuilt_hash = counterfactual.derived_state_hash(rebuilt)

        if rebuilt_hash != stored_hash:
            return _IRRECONCILABLE_DERIVED_STATE
        return rebuilt_hash

    @staticmethod
    async def _sealed_spec(
        session: AsyncSession, scenario_id: uuid.UUID
    ) -> ScenarioSpec:
        """Rebuild the typed lever/assumption spec from the sealed rows.

        The spec is what the hash was taken over, so it is read back rather than
        re-derived: levers in their stored apply order, assumptions with the
        typed value each carried.
        """
        from app.database.models import ScenarioAssumption, ScenarioLever
        from app.services.ioe.domain.scenario import ScenarioSpec

        levers = list(await session.scalars(
            select(ScenarioLever)
            .where(ScenarioLever.scenario_id == scenario_id)
            .order_by(ScenarioLever.apply_order)
        ))
        assumptions = list(await session.scalars(
            select(ScenarioAssumption)
            .where(ScenarioAssumption.scenario_id == scenario_id)
            .order_by(ScenarioAssumption.assumption_code)
        ))
        if not levers:
            raise SealedEvidenceIncomplete()

        return ScenarioSpec.parse(
            [
                {
                    "lever_code": lever.lever_code,
                    "parameters": {
                        k: Decimal(str(v)) if _is_number(v) else v
                        for k, v in (lever.parameters or {}).items()
                    },
                }
                for lever in levers
            ],
            assumptions=[
                {
                    "assumption_code": a.assumption_code,
                    "value_number": a.value_number,
                    "value_text": a.value_text,
                    "value_boolean": a.value_boolean,
                    "materiality": a.materiality,
                    "source": a.source,
                    "certainty": a.certainty,
                    "affects_eligibility": a.affects_eligibility,
                }
                for a in assumptions
            ] or None,
            # Sealed rows the platform wrote: their platform/statutory origins
            # are the record, not a claim to re-litigate.
            trusted_provenance=True,
        )


def _is_number(value: object) -> bool:
    try:
        Decimal(str(value))
    except Exception:  # noqa: BLE001
        return False
    return True


__all__ = [
    "OptimizationReplayService",
    "PortfolioInvariantViolated",
    "PortfolioObjectiveMismatch",
    "PortfolioReplayService",
    "ReplayOutcome",
    "ScenarioReplayService",
]
