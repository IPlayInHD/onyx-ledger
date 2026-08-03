"""ScenarioService — P5 what-if simulation on a cloned frozen input.

Same transaction shape as the optimization orchestrator, for the same reasons:

  TX-1  pin EVERY material input — the baseline input snapshot, the baseline
        RESULT, the rule snapshot, the objective policy, both registries and the
        full version manifest — and only then compute `scenario_spec_hash`.
        Resolve idempotency against that hash.
  compute  outside any transaction: clone the frozen baseline input, apply the
        typed levers atomically, and run the deterministic engine.
  TX-2  write the sealed evidence and `scenario_result_hash`, atomically.
  TX-3  on failure, a sanitized error code and no result children.

Three rules this module exists to hold:

1. **The baseline is cloned, never touched.** `simulate()` reads the user's
   profile once, freezes it, and every hypothetical is applied to a copy. No
   scenario can write to production data because nothing here writes to it.
2. **Levers arrive as typed codes.** `ScenarioSpec.parse()` has already refused
   anything else before this service sees it; the registry resolves what a code
   does. There is no path from a request to an engine field name.
3. **Historical results are preserved.** A refresh creates a NEW scenario and
   points the old one at it. Nothing recomputes a stored result in place.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, DomainError, NotFound
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    RunRuleSnapshot,
    Scenario,
    ScenarioAssumption,
    ScenarioConfidenceComponent,
    ScenarioEvent,
    ScenarioInputChange,
    ScenarioLever,
    ScenarioResult,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import assumptions as assumption_registry
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.domain import cost_taxonomy
from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.domain.enums import (
    AssumptionCertainty,
    AssumptionSource,
    CalculationBasis,
    EvidenceStatus,
    Materiality,
    WorkflowStatus,
)
from app.services.ioe.domain.freshness import (
    COMPARISON_POLICY_VERSION,
    FRESHNESS_POLICY_VERSION,
)
from app.services.ioe.domain.scenario import (
    SCENARIO_RESULT_SCHEMA_VERSION,
    SCENARIO_SPEC_VERSION,
    FreshnessStatus,
    ScenarioSpec,
    StaleReason,
)
from app.services.ioe.domain.workflow import WorkflowStateMachine
from app.services.ioe.portfolio.service import inputs_from, to_tax_input
from app.services.ioe.snapshot.service import RuleSnapshotService
from app.services.tax_engine.contracts import CONTRACT_VERSION
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.core.engine import compute
from app.services.tax_engine.service import ENGINE_VERSION, TaxEngineService

SCENARIO_SERVICE_VERSION = "1.0.0"

MONEY = Decimal("0.01")

# Sanitized, enumerated failure codes. Never a message, stack trace, or PII.
ERROR_ANALYSIS_NOT_READY = "ANALYSIS_NOT_READY"
ERROR_LEVER_APPLICATION_FAILED = "LEVER_APPLICATION_FAILED"
ERROR_ENGINE_FAILED = "ENGINE_FAILED"
ERROR_PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
ERROR_INTERNAL = "INTERNAL_ERROR"


class ScenarioIdempotencyKeyReused(DomainError):
    """The same Idempotency-Key was presented with a different specification."""

    status_code = 409
    error_type = "https://onyx.ledger/errors/idempotency-key-reused"
    title = "Idempotency Key Reused"


@dataclass
class PinnedScenarioSpec:
    """Everything material to the calculation, resolved BEFORE the hash."""

    analysis_id: uuid.UUID
    tax_year: int
    jurisdiction: str
    baseline_input_snapshot_hash: str
    baseline_result_hash: str
    baseline_tax: Decimal
    baseline_inputs: dict
    rule_snapshot_id: uuid.UUID
    rule_snapshot_hash: str
    pinned_rule_version_ids: list[uuid.UUID]
    objective_code: str
    objective_version: str
    version_manifest: dict
    manifest_hash: str
    spec: ScenarioSpec
    spec_hash: str = ""


@dataclass
class ScenarioOutcome:
    scenario_id: uuid.UUID
    spec_hash: str
    result_hash: str | None
    workflow_status: str
    replayed: bool = False
    tax_delta: Decimal | None = None
    objective_delta: Decimal | None = None
    error_code: str | None = None
    warnings: list[str] = field(default_factory=list)


class ScenarioService:
    """Owns the transaction boundaries. Domain helpers stay transaction-free."""

    def __init__(self, user_id: uuid.UUID):
        self.user_id = user_id

    # ---------------------------------------------------------------- TX-1 ---
    async def _pin_specification(
        self,
        session: AsyncSession,
        analysis_id: uuid.UUID,
        spec: ScenarioSpec,
    ) -> PinnedScenarioSpec:
        """Resolve and pin EVERY material input, then compute the spec hash.

        The baseline RESULT is pinned alongside the baseline inputs. Pinning only
        the inputs would leave a stored delta unable to show what it was a delta
        from, since the engine that turned those inputs into a number could have
        changed underneath it.
        """
        analysis = await session.get(AnalysisRun, analysis_id)
        if analysis is None or analysis.user_id != self.user_id:
            # ownership validated in the application layer as well as by RLS
            raise NotFound("Analysis not found")
        if analysis.status != "completed":
            raise Conflict("analysis_not_ready: run an analysis before simulating")

        snapshot_row = await session.get(AnalysisInputSnapshot, analysis_id)
        baseline_input_hash = snapshot_row.snapshot_hash if snapshot_row else ""

        # The frozen baseline. Read once, then cloned for every hypothetical.
        engine = TaxEngineService(session)
        baseline_input = await engine.build_input(self.user_id, analysis.tax_year)
        baseline_result = engine.run(baseline_input)
        baseline_inputs = inputs_from(baseline_input)
        baseline_tax = baseline_result.total_payable.quantize(MONEY, ROUND_HALF_UP)
        baseline_result_hash = c.canonical_hash({
            "baseline_tax": c.money(baseline_tax),
            "engine_version": ENGINE_VERSION,
            "reference_data_version": engine_data.REFERENCE_DATA_VERSION,
        })

        pinned = await RuleSnapshotService(session).capture(analysis.tax_year)

        manifest = {
            "scenario_service_version": SCENARIO_SERVICE_VERSION,
            "scenario_spec_version": SCENARIO_SPEC_VERSION,
            "scenario_result_schema_version": SCENARIO_RESULT_SCHEMA_VERSION,
            "tax_engine_version": ENGINE_VERSION,
            "engine_reference_data_version": engine_data.REFERENCE_DATA_VERSION,
            "rules_evaluator_contract_version": CONTRACT_VERSION,
            "canonical_serialization_version": c.CANONICAL_SERIALIZATION_VERSION,
            "confidence_algorithm_version": support.CONFIDENCE_ALGORITHM_VERSION,
            "cost_taxonomy_version": cost_taxonomy.COST_TAXONOMY_VERSION,
            "lever_registry_version": lever_registry.LEVER_REGISTRY_VERSION,
            "assumption_registry_version": assumption_registry.registry_version(),
            "objective_code": savings_domain.PORTFOLIO_OBJECTIVE_CODE.value,
            "objective_version": savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            "freshness_policy_version": FRESHNESS_POLICY_VERSION,
            "comparison_policy_version": COMPARISON_POLICY_VERSION,
            "rule_snapshot_hash": pinned.snapshot_hash,
        }
        manifest_hash = c.version_manifest_hash(manifest)

        pinned_spec = PinnedScenarioSpec(
            analysis_id=analysis_id,
            tax_year=analysis.tax_year,
            jurisdiction=analysis.province_code or "FED",
            baseline_input_snapshot_hash=baseline_input_hash,
            baseline_result_hash=baseline_result_hash,
            baseline_tax=baseline_tax,
            baseline_inputs=baseline_inputs,
            rule_snapshot_id=pinned.snapshot_id,
            rule_snapshot_hash=pinned.snapshot_hash,
            pinned_rule_version_ids=pinned.version_ids(),
            objective_code=savings_domain.PORTFOLIO_OBJECTIVE_CODE.value,
            objective_version=savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            version_manifest=manifest,
            manifest_hash=manifest_hash,
            spec=spec,
        )
        # Every material input is now resolved — only now is identity computed.
        pinned_spec.spec_hash = self.compute_spec_hash(pinned_spec)
        return pinned_spec

    @staticmethod
    def compute_spec_hash(pinned: PinnedScenarioSpec) -> str:
        """Identity of a scenario. Label and note are absent by construction.

        Exposed as a static method so a replay test can recompute it from stored
        columns without going through the service.
        """
        return c.scenario_spec_hash(
            baseline_input_snapshot_hash=pinned.baseline_input_snapshot_hash,
            canonical_scenario_input=[
                *pinned.spec.canonical_for_hash(),
                {"baseline_result_hash": pinned.baseline_result_hash},
                {"objective_code": pinned.objective_code,
                 "objective_version": pinned.objective_version},
                {"manifest_hash": pinned.manifest_hash},
            ],
            tax_year=pinned.tax_year,
            jurisdiction=pinned.jurisdiction,
            lever_registry_version=lever_registry.LEVER_REGISTRY_VERSION,
            engine_version=ENGINE_VERSION,
            engine_config_version=engine_data.REFERENCE_DATA_VERSION,
            rule_version_set=pinned.pinned_rule_version_ids,
            reference_data_versions={
                "engine": engine_data.REFERENCE_DATA_VERSION,
                "rule_snapshot": pinned.rule_snapshot_hash,
            },
            calculation_policy_version=SCENARIO_SERVICE_VERSION,
            decimal_policy_version=c.CANONICAL_SERIALIZATION_VERSION,
        )

    async def _resolve_idempotency(
        self, session: AsyncSession, pinned: PinnedScenarioSpec, key: str | None
    ) -> Scenario | None:
        """Replay an equivalent scenario, or refuse a reused key.

        Resolution is against the canonical SPEC HASH, so two requests that
        differ only in label, note, or assumption ordering replay the same
        scenario, while the same key with a different spec is refused.
        """
        if key:
            keyed = await session.scalar(
                select(Scenario).where(
                    Scenario.user_id == self.user_id,
                    Scenario.idempotency_key == key,
                )
            )
            if keyed is not None:
                if keyed.scenario_spec_hash != pinned.spec_hash:
                    raise ScenarioIdempotencyKeyReused(
                        "idempotency_key_reused: this key was used for a "
                        "materially different scenario specification"
                    )
                return keyed

        return await session.scalar(
            select(Scenario).where(
                Scenario.user_id == self.user_id,
                Scenario.scenario_spec_hash == pinned.spec_hash,
                Scenario.workflow_status.in_(("pending", "running", "completed")),
                Scenario.visibility_status == "active",
            )
        )

    # ------------------------------------------------------------ pipeline ---
    async def simulate(
        self,
        analysis_id: uuid.UUID,
        spec: ScenarioSpec,
        *,
        idempotency_key: str | None = None,
        refreshed_from_scenario_id: uuid.UUID | None = None,
    ) -> ScenarioOutcome:
        # ---- TX-1: pin, resolve idempotency, create the header ----
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            pinned = await self._pin_specification(session, analysis_id, spec)
            existing = await self._resolve_idempotency(session, pinned, idempotency_key)
            if existing is not None:
                return ScenarioOutcome(
                    scenario_id=existing.id, spec_hash=pinned.spec_hash,
                    result_hash=existing.scenario_result_hash,
                    workflow_status=existing.workflow_status, replayed=True,
                )

            scenario = Scenario(
                user_id=self.user_id,
                base_analysis_id=analysis_id,
                label=spec.label,
                note=spec.note,
                tax_year=pinned.tax_year,
                jurisdiction=pinned.jurisdiction,
                scenario_spec_hash=pinned.spec_hash,
                baseline_input_snapshot_hash=pinned.baseline_input_snapshot_hash,
                baseline_result_hash=pinned.baseline_result_hash,
                baseline_tax=pinned.baseline_tax,
                rule_snapshot_id=pinned.rule_snapshot_id,
                lever_registry_version=lever_registry.LEVER_REGISTRY_VERSION,
                objective_code=pinned.objective_code,
                objective_version=pinned.objective_version,
                result_schema_version=SCENARIO_RESULT_SCHEMA_VERSION,
                version_manifest=pinned.version_manifest,
                manifest_hash=pinned.manifest_hash,
                idempotency_key=idempotency_key,
                refreshed_from_scenario_id=refreshed_from_scenario_id,
                freshness_status=FreshnessStatus.CURRENT.value,
                freshness_evaluated_at=datetime.now(tz=UTC),
                started_at=datetime.now(tz=UTC),
            )
            session.add(scenario)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                raise Conflict(
                    "concurrent_scenario: an equivalent scenario is already running"
                ) from None

            session.add(ScenarioEvent(
                scenario_id=scenario.id, from_status=None,
                to_status=WorkflowStatus.PENDING.value,
            ))
            WorkflowStateMachine.assert_transition(
                WorkflowStatus.PENDING, WorkflowStatus.RUNNING
            )
            scenario.workflow_status = WorkflowStatus.RUNNING.value
            session.add(ScenarioEvent(
                scenario_id=scenario.id, from_status=WorkflowStatus.PENDING.value,
                to_status=WorkflowStatus.RUNNING.value,
            ))
            # The pinned SPEC is written now, before compute: it is what the
            # hash was taken over, so it must exist even if compute fails.
            for lever in spec.levers:
                session.add(ScenarioLever(
                    scenario_id=scenario.id,
                    apply_order=lever.apply_order,
                    lever_code=lever.lever_code,
                    parameters={k: str(v) for k, v in sorted(lever.parameters.items())},
                ))
            for assumption in spec.assumptions:
                session.add(ScenarioAssumption(
                    scenario_id=scenario.id,
                    assumption_code=assumption.assumption_code,
                    value_number=assumption.value_number,
                    value_text=assumption.value_text,
                    value_boolean=assumption.value_boolean,
                    materiality=assumption.materiality,
                    source=assumption.source,
                    certainty=assumption.certainty,
                    affects_eligibility=assumption.affects_eligibility,
                ))
            await session.flush()
            scenario_id = scenario.id

        # ---- compute: no transaction held, production data untouched ----
        try:
            computed = self._compute(pinned)
        except Exception as exc:  # noqa: BLE001
            await self._fail(scenario_id, _classify(exc))
            raise

        # ---- TX-2: persist evidence and seal, atomically ----
        try:
            result_hash = await self._persist(scenario_id, pinned, computed)
        except Exception:  # noqa: BLE001
            await self._fail(scenario_id, ERROR_PERSISTENCE_FAILED)
            raise

        return ScenarioOutcome(
            scenario_id=scenario_id, spec_hash=pinned.spec_hash,
            result_hash=result_hash,
            workflow_status=WorkflowStatus.COMPLETED.value,
            tax_delta=computed["tax_delta"],
            objective_delta=computed["objective_delta"],
        )

    def _compute(self, pinned: PinnedScenarioSpec) -> dict:
        """Apply the levers to a CLONE of the frozen baseline and run the engine.

        `apply_all` is atomic: a failure part-way leaves the clone untouched and
        raises, so a half-applied composite lever can never reach the engine.
        """
        clone = dict(pinned.baseline_inputs)          # the frozen input, cloned
        applications = [
            (lever.lever_code, dict(lever.parameters)) for lever in pinned.spec.levers
        ]
        applied = lever_registry.apply_all(
            clone, applications,
            jurisdiction=pinned.jurisdiction, tax_year=pinned.tax_year,
        )
        # the baseline dict must be unchanged — the clone is what was mutated
        assert clone == pinned.baseline_inputs, "baseline input was mutated in place"

        scenario_result = compute(to_tax_input(applied.inputs))
        scenario_tax = scenario_result.total_payable.quantize(MONEY, ROUND_HALF_UP)
        tax_delta = (pinned.baseline_tax - scenario_tax).quantize(MONEY, ROUND_HALF_UP)

        # Same objective, same sign convention, same rounding point as P4: the
        # objective is a COST, so delta = baseline − scenario is positive for an
        # improvement.
        objective_baseline = savings_domain.objective_cost(
            metric=savings_domain.PORTFOLIO_OBJECTIVE_CODE,
            current_tax=pinned.baseline_tax,
        )
        objective_scenario = savings_domain.objective_cost(
            metric=savings_domain.PORTFOLIO_OBJECTIVE_CODE,
            current_tax=scenario_tax,
        )
        objective_delta = (
            objective_baseline - objective_scenario
        ).quantize(MONEY, ROUND_HALF_UP)

        breakdown = support.compute(
            evidence_status=EvidenceStatus.VERIFIED,
            calculation_basis=CalculationBasis.SCENARIO_ESTIMATE,
            assumptions=self._structured_assumptions(pinned.spec),
        )
        return {
            "scenario_tax": scenario_tax,
            "tax_delta": tax_delta,
            "objective_baseline": objective_baseline,
            "objective_scenario": objective_scenario,
            "objective_delta": objective_delta,
            "changes": applied.changes,
            "support": breakdown,
        }

    # ---------------------------------------------------------------- TX-2 ---
    async def _persist(
        self, scenario_id: uuid.UUID, pinned: PinnedScenarioSpec, computed: dict
    ) -> str:
        result_payload = self.canonical_result(computed)
        result_hash = c.scenario_result_hash(
            spec_hash=pinned.spec_hash, result=result_payload
        )

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            scenario = await session.get(Scenario, scenario_id)
            if scenario is None:
                raise NotFound("Scenario not found")

            session.add(RunRuleSnapshot(
                scenario_id=scenario_id, snapshot_id=pinned.rule_snapshot_id,
                replay_status="verified",
            ))

            breakdown = computed["support"]
            session.add(ScenarioResult(
                scenario_id=scenario_id,
                baseline_tax=pinned.baseline_tax,
                scenario_tax=computed["scenario_tax"],
                tax_delta=computed["tax_delta"],
                net_benefit=computed["objective_delta"],
                calculation_basis="scenario_estimate",
                result_schema_version=SCENARIO_RESULT_SCHEMA_VERSION,
                objective_code=pinned.objective_code,
                objective_version=pinned.objective_version,
                objective_value_baseline=computed["objective_baseline"],
                objective_value_scenario=computed["objective_scenario"],
                objective_delta=computed["objective_delta"],
                raw_support_score=breakdown.raw_support_score,
                assumption_adjusted_score=breakdown.assumption_adjusted_score,
                display_support_score=breakdown.display_support_score,
                support_cap_applied=breakdown.cap_applied,
                support_cap_reason_code=breakdown.cap_reason_code,
                affected_rule_versions=[str(v) for v in pinned.pinned_rule_version_ids],
            ))
            for component in breakdown.components:
                session.add(ScenarioConfidenceComponent(
                    scenario_id=scenario_id,
                    factor_code=component.factor_code.value,
                    value=component.value, weight=component.weight,
                    contribution=component.contribution,
                    reason_code=component.reason_code,
                ))

            # The applied-change trace records FIELD NAMES and the values the
            # registry produced. It does not copy the user's other financial
            # inputs; those stay in the frozen analysis snapshot.
            for change in computed["changes"]:
                session.add(ScenarioInputChange(
                    scenario_id=scenario_id,
                    lever_code=change.lever_code,
                    field=change.field,
                    old_value=str(change.old_value),
                    new_value=str(change.new_value),
                    apply_order=change.apply_order,
                ))

            WorkflowStateMachine.assert_transition(
                WorkflowStatus.RUNNING, WorkflowStatus.COMPLETED
            )
            scenario.scenario_result_hash = result_hash
            scenario.workflow_status = WorkflowStatus.COMPLETED.value
            scenario.completed_at = datetime.now(tz=UTC)
            session.add(ScenarioEvent(
                scenario_id=scenario_id, from_status=WorkflowStatus.RUNNING.value,
                to_status=WorkflowStatus.COMPLETED.value,
            ))
            await session.flush()
        return result_hash

    @staticmethod
    def _structured_assumptions(spec: ScenarioSpec) -> tuple:
        """Build registry-validated assumptions for the support score.

        Going through `assumptions.build()` rather than passing the raw request
        through means an assumption's materiality and eligibility impact come
        from the registry, not from whatever the caller asserted.
        """
        out = []
        for a in spec.assumptions:
            value = (
                a.value_number if a.value_number is not None
                else a.value_text if a.value_text is not None
                else a.value_boolean
            )
            out.append(assumption_registry.build(
                a.assumption_code, value,
                source=AssumptionSource(a.source),
                certainty=AssumptionCertainty(a.certainty),
                materiality=Materiality(a.materiality),
            ))
        return tuple(out)

    @staticmethod
    def canonical_result(computed: dict) -> dict:
        """The exact payload the result hash is taken over.

        Static and free of any session or clock so a replay can rebuild it from
        stored columns and get the same hash.
        """
        breakdown = computed["support"]
        return {
            "scenario_tax": c.money(computed["scenario_tax"]),
            "tax_delta": c.money(computed["tax_delta"]),
            "objective_value_baseline": c.money(computed["objective_baseline"]),
            "objective_value_scenario": c.money(computed["objective_scenario"]),
            "objective_delta": c.money(computed["objective_delta"]),
            "result_schema_version": SCENARIO_RESULT_SCHEMA_VERSION,
            "support": {
                "raw_support_score": c.rate(breakdown.raw_support_score),
                "assumption_adjusted_score": c.rate(
                    breakdown.assumption_adjusted_score
                ),
                "display_support_score": c.rate(breakdown.display_support_score),
                "cap_applied": breakdown.cap_applied,
                "cap_reason_code": breakdown.cap_reason_code,
            },
            "changes": [
                {
                    "apply_order": ch.apply_order,
                    "lever_code": ch.lever_code,
                    "field": ch.field,
                    "new_value": str(ch.new_value),
                }
                for ch in computed["changes"]
            ],
        }

    # ---------------------------------------------------------------- TX-3 ---
    async def _fail(self, scenario_id: uuid.UUID, error_code: str) -> None:
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            scenario = await session.get(Scenario, scenario_id)
            if scenario is None or scenario.workflow_status != WorkflowStatus.RUNNING.value:
                return
            scenario.workflow_status = WorkflowStatus.FAILED.value
            scenario.error_code = error_code
            scenario.completed_at = datetime.now(tz=UTC)
            session.add(ScenarioEvent(
                scenario_id=scenario_id, from_status=WorkflowStatus.RUNNING.value,
                to_status=WorkflowStatus.FAILED.value, reason_code=error_code,
            ))

    # ------------------------------------------------------- archive/refresh --
    async def archive(self, scenario_id: uuid.UUID) -> None:
        """"Delete" is a VISIBILITY change. No evidence is removed.

        The result, the applied-change trace, the pinned spec and the audit
        events all survive; the scenario simply stops appearing in active lists.
        """
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            scenario = await self._own_scenario(session, scenario_id)
            if scenario.visibility_status == "archived":
                return
            scenario.visibility_status = "archived"
            scenario.archived_at = datetime.now(tz=UTC)
            session.add(ScenarioEvent(
                scenario_id=scenario_id, from_status=scenario.workflow_status,
                to_status=scenario.workflow_status, reason_code="ARCHIVED",
            ))

    async def unarchive(self, scenario_id: uuid.UUID) -> None:
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            scenario = await self._own_scenario(session, scenario_id)
            scenario.visibility_status = "active"
            scenario.archived_at = None
            session.add(ScenarioEvent(
                scenario_id=scenario_id, from_status=scenario.workflow_status,
                to_status=scenario.workflow_status, reason_code="UNARCHIVED",
            ))

    async def refresh(self, scenario_id: uuid.UUID) -> ScenarioOutcome:
        """Re-run a scenario's specification against today's world.

        Creates a NEW scenario and marks the old one superseded. The historical
        result keeps its own pinned versions and its own numbers — it is never
        recomputed in place, because it is a true statement about the baseline it
        was measured against.
        """
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            original = await self._own_scenario(session, scenario_id)
            analysis_id = original.base_analysis_id
            spec = await self._load_spec(session, original)

        outcome = await self.simulate(
            analysis_id, spec, refreshed_from_scenario_id=scenario_id
        )
        if outcome.scenario_id == scenario_id:
            # Nothing material changed: the canonical spec replayed the same
            # scenario, so there is nothing to supersede.
            return outcome

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            original = await self._own_scenario(session, scenario_id)
            original.superseded_by_scenario_id = outcome.scenario_id
            original.superseded_at = datetime.now(tz=UTC)
            original.freshness_status = FreshnessStatus.SUPERSEDED.value
            original.stale_reason_code = StaleReason.SUPERSEDED_BY_REFRESH.value
            original.freshness_evaluated_at = datetime.now(tz=UTC)
            session.add(ScenarioEvent(
                scenario_id=scenario_id, from_status=original.workflow_status,
                to_status=original.workflow_status, reason_code="SUPERSEDED_BY_REFRESH",
            ))
        return outcome

    async def _own_scenario(self, session: AsyncSession, scenario_id: uuid.UUID):
        scenario = await session.get(Scenario, scenario_id)
        if scenario is None or scenario.user_id != self.user_id:
            raise NotFound("Scenario not found")
        return scenario

    @staticmethod
    async def _load_spec(session: AsyncSession, scenario: Scenario) -> ScenarioSpec:
        """Rebuild the typed spec from stored rows, parent-key-qualified."""
        levers = list(await session.scalars(
            select(ScenarioLever)
            .where(ScenarioLever.scenario_id == scenario.id)
            .order_by(ScenarioLever.apply_order)
        ))
        assumptions = list(await session.scalars(
            select(ScenarioAssumption)
            .where(ScenarioAssumption.scenario_id == scenario.id)
            .order_by(ScenarioAssumption.assumption_code)
        ))
        return ScenarioSpec.parse(
            [
                {"lever_code": x.lever_code,
                 "parameters": {k: Decimal(v) for k, v in (x.parameters or {}).items()}}
                for x in levers
            ],
            assumptions=[
                {
                    "assumption_code": a.assumption_code,
                    "value_number": a.value_number,
                    "value_text": a.value_text,
                    "value_boolean": a.value_boolean,
                    "materiality": a.materiality, "source": a.source,
                    "certainty": a.certainty,
                    "affects_eligibility": a.affects_eligibility,
                }
                for a in assumptions
            ],
            label=scenario.label,
            note=scenario.note,
            jurisdiction=scenario.jurisdiction,
            tax_year=scenario.tax_year,
        )


def _classify(exc: Exception) -> str:
    """Map an exception to an enumerated code; the message never escapes."""
    name = type(exc).__name__
    if "Lever" in name:
        return ERROR_LEVER_APPLICATION_FAILED
    if "Canonical" in name or "Engine" in name:
        return ERROR_ENGINE_FAILED
    return ERROR_INTERNAL


__all__ = [
    "SCENARIO_SERVICE_VERSION",
    "PinnedScenarioSpec",
    "ScenarioIdempotencyKeyReused",
    "ScenarioOutcome",
    "ScenarioService",
]
