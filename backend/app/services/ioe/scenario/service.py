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
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, DomainError, NotFound
from app.core.logging import get_logger
from app.database.bulk import bulk_insert
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
    CURRENT_SCENARIO_RESULT_SCHEMA_VERSION,
    DERIVED_STATE_BEARING_VERSIONS,
    SCENARIO_SPEC_VERSION,
    SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS,
    FreshnessStatus,
    ScenarioSpec,
    StaleReason,
    UnsupportedResultSchemaVersion,
    canonical_scenario_result,
)
from app.services.ioe.domain.workflow import WorkflowStateMachine
from app.services.ioe.frozen import (
    FrozenScenarioExecutionInput,
    FrozenScenarioInputService,
)
from app.services.ioe.frozen.models import (
    SCENARIO_EXECUTION_POLICY_VERSION,
    ScenarioExecutionPolicy,
    ScenarioFrozenInputError,
    assert_current_scenario_policy,
)
from app.services.ioe.portfolio.service import to_tax_input
from app.services.ioe.scenario import counterfactual, held_evidence
from app.services.ioe.scenario.counterfactual import CounterfactualDerivedState
from app.services.ioe.scenario.held_evidence import HistoricalHeldEvidenceSnapshot
from app.services.ioe.snapshot.service import RuleSnapshotService
from app.services.tax_engine.contracts import CONTRACT_VERSION
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.core.data import TaxDataset
from app.services.tax_engine.core.engine import compute
from app.services.tax_engine.rules_service import RulesEvaluatorService
from app.services.tax_engine.service import ENGINE_VERSION, TaxEngineService

SCENARIO_SERVICE_VERSION = "1.0.0"

MONEY = Decimal("0.01")

log = get_logger("onyx.ioe.scenario")

# Non-sensitive counters for refused scenario baselines, keyed by enumerated
# reason. Counts only — no identifier, no tenant, no payload — so the whole
# mapping is safe to scrape or log. `PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED`
# is the one an operator watches during the legacy-snapshot transition: it counts
# analyses that predate the self-describing format and must be re-run.
BASELINE_REFUSAL_COUNTS: Counter[str] = Counter()


def baseline_refusal_metrics() -> dict[str, int]:
    """Snapshot of the refusal counters. Counts only, safe to publish."""
    return dict(BASELINE_REFUSAL_COUNTS)


def reset_baseline_refusal_metrics() -> None:
    BASELINE_REFUSAL_COUNTS.clear()

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


class ScenarioBaselineUnavailable(DomainError):
    """The pinned analysis snapshot cannot serve as a scenario baseline.

    A refusal, never a fallback. `detail` is the enumerated reason code and
    nothing else — the payload that failed is the user's financial data and does
    not travel with the error.

    409 rather than 5xx because the condition is about the state of the pinned
    analysis, not about the service: re-running the analysis is the remedy, and
    retrying the same request will fail identically until then.
    """

    status_code = 409
    error_type = "https://onyx.ledger/errors/scenario-baseline-unavailable"
    title = "Scenario Baseline Unavailable"


@dataclass
class PinnedScenarioSpec:
    """Everything material to the calculation, resolved BEFORE the hash.

    Note what is NOT here: a dict of the user's financial figures. The baseline
    reaches the compute phase only inside `frozen`, and the three baseline
    identities below are read THROUGH it. There is therefore no second copy of
    the inputs that could drift from the snapshot the spec hash names, and no
    field a future edit could quietly populate from a live query.
    """

    analysis_id: uuid.UUID
    tax_year: int
    jurisdiction: str
    frozen: FrozenScenarioExecutionInput
    rule_snapshot_id: uuid.UUID
    rule_snapshot_hash: str
    pinned_rule_version_ids: list[uuid.UUID]
    objective_code: str
    objective_version: str
    version_manifest: dict
    manifest_hash: str
    spec: ScenarioSpec
    spec_hash: str = ""
    #: The reference data this scenario computes from, resolved in TX-1 where a
    #: session exists. Carried rather than looked up because the compute phase
    #: deliberately holds no session and must have no route to live data.
    #:
    #: Absent from `compute_spec_hash` by design, and that is not a gap: the
    #: dataset is covered by the rule snapshot's `engine_reference_dataset`
    #: artifact, and `rule_snapshot_hash` is already part of the identity. A
    #: changed bracket therefore moves the spec hash through the snapshot, once.
    dataset: TaxDataset | None = None

    @property
    def baseline_input_snapshot_hash(self) -> str:
        return self.frozen.snapshot_hash

    @property
    def baseline_result_hash(self) -> str:
        return self.frozen.baseline_result_hash

    @property
    def baseline_tax(self) -> Decimal:
        return self.frozen.baseline_tax


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
        *,
        result_schema_version: str,
    ) -> PinnedScenarioSpec:
        """Resolve and pin EVERY material input, then compute the spec hash.

        `result_schema_version` HAS NO DEFAULT on purpose. The manifest records
        which result contract this scenario will be sealed under, and the
        manifest hash feeds the spec hash — so a caller that let this default
        while writing a v2 row would reproduce the reverted activation defect
        exactly: the row would say v2 and the manifest inside its own identity
        would say v1. Making it required means that mistake cannot be made by
        omission, only by writing the wrong thing on purpose.

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

        # The frozen baseline, reconstructed from the snapshot this scenario
        # pins — NOT rebuilt from the user's current financial tables.
        #
        # This is the item 3B correction. Reading live sources here while
        # pinning `baseline_input_snapshot_hash` admitted the same defect item
        # 3A removed from the optimizer: pin snapshot A, live data becomes B,
        # apply levers to B, seal under A. The stored delta then described a
        # baseline that nothing recorded.
        #
        # Item 3A's reconstruction service is reused rather than reimplemented:
        # one codec, one set of hashes, one ownership rule. A second
        # implementation would be a second chance to disagree.
        frozen = await FrozenScenarioInputService(session, self.user_id).resolve(
            analysis_id,
            objective_code=savings_domain.PORTFOLIO_OBJECTIVE_CODE.value,
            objective_version=savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            lever_registry_version=lever_registry.LEVER_REGISTRY_VERSION,
            assumption_registry_version=assumption_registry.registry_version(),
            support_score_version=support.CONFIDENCE_ALGORITHM_VERSION,
        )

        pinned = await RuleSnapshotService(session).capture(analysis.tax_year)

        manifest = {
            "scenario_service_version": SCENARIO_SERVICE_VERSION,
            "scenario_spec_version": SCENARIO_SPEC_VERSION,
            "scenario_result_schema_version": result_schema_version,
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
            # How the baseline was obtained. A reader can tell a corrected
            # scenario from a legacy one without inferring it from a date.
            "scenario_execution_policy_version": (
                SCENARIO_EXECUTION_POLICY_VERSION.value),
            "snapshot_schema_version": (
                frozen.frozen_analysis_input.snapshot_schema_version),
            "baseline_result_hash": frozen.baseline_result_hash,
        }
        # Re-read what we just wrote, through the same normalizer replay and
        # presentation use. This is what makes "an absent policy key means the
        # scenario is historical" a property of the data rather than an
        # assumption about it: after this line no scenario can be created
        # without an explicit, current, well-formed policy, so any stored row
        # lacking one was necessarily written before the policy existed.
        assert_current_scenario_policy(manifest)
        manifest_hash = c.version_manifest_hash(manifest)

        pinned_spec = PinnedScenarioSpec(
            analysis_id=analysis_id,
            tax_year=analysis.tax_year,
            jurisdiction=analysis.province_code or "FED",
            frozen=frozen,
            rule_snapshot_id=pinned.snapshot_id,
            rule_snapshot_hash=pinned.snapshot_hash,
            pinned_rule_version_ids=pinned.version_ids(),
            objective_code=savings_domain.PORTFOLIO_OBJECTIVE_CODE.value,
            objective_version=savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            version_manifest=manifest,
            manifest_hash=manifest_hash,
            spec=spec,
            dataset=await TaxEngineService(session).resolve_dataset(
                analysis.tax_year),
        )
        # Every material input is now resolved — only now is identity computed,
        # and only then is it bound back onto the frozen input it describes.
        pinned_spec.spec_hash = self.compute_spec_hash(pinned_spec)
        pinned_spec.frozen = frozen.bind(scenario_spec_hash=pinned_spec.spec_hash)
        return pinned_spec

    async def _record_baseline_failure(
        self, session: AsyncSession, analysis_id: uuid.UUID,
        exc: ScenarioFrozenInputError,
    ) -> None:
        """Sanitized internal diagnostics for a refused baseline.

        The external 409 carries one enumerated code, which is correct and also
        useless to an operator at 3am: it says a baseline could not be resolved
        but not which artifact, which stage, or whether the analysis is even
        capable of producing one. This records the missing half.

        Everything here is an identifier, an enumerated value, or a content
        HASH. No decoded snapshot, no financial figure, no document content and
        no user-supplied text — a snapshot hash names a payload without
        revealing a byte of it, which is exactly the property wanted for a log
        line that will be shipped to an aggregator.
        """
        analysis_state = "unknown"
        expected_snapshot_hash = None
        snapshot_present = False
        try:
            analysis = await session.get(AnalysisRun, analysis_id)
            analysis_state = str(getattr(analysis, "status", "absent"))
            snapshot = await session.get(AnalysisInputSnapshot, analysis_id)
            snapshot_present = snapshot is not None
            expected_snapshot_hash = getattr(snapshot, "snapshot_hash", None)
        except Exception:  # noqa: BLE001 - diagnostics must never mask the refusal
            pass

        BASELINE_REFUSAL_COUNTS[exc.reason] += 1
        log.warning(
            "scenario_baseline_unavailable",
            user_id=str(self.user_id),
            analysis_id=str(analysis_id),
            failure_stage="tx1_pin_specification",
            expected_artifact="analysis_input_snapshot",
            expected_artifact_id=str(analysis_id),
            expected_snapshot_hash=expected_snapshot_hash,
            snapshot_present=snapshot_present,
            analysis_status=analysis_state,
            # The policy this attempt ran UNDER. Always the current one — no
            # scenario row exists to be legacy, and reporting `legacy` merely
            # because a snapshot is missing would conflate two different facts.
            execution_policy=ScenarioExecutionPolicy.FROZEN_SNAPSHOT_V1.value,
            reason_code=exc.reason,
        )

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

        # Bound to a declared name: `AsyncSession.scalar` is typed `-> Any`, so
        # returning it directly would erase this method's declared row type.
        existing: Scenario | None = await session.scalar(
            select(Scenario).where(
                Scenario.user_id == self.user_id,
                Scenario.scenario_spec_hash == pinned.spec_hash,
                Scenario.workflow_status.in_(("pending", "running", "completed")),
                Scenario.visibility_status == "active",
            )
        )
        return existing

    # ------------------------------------------------------------ pipeline ---
    async def simulate(
        self,
        analysis_id: uuid.UUID,
        spec: ScenarioSpec,
        *,
        idempotency_key: str | None = None,
        refreshed_from_scenario_id: uuid.UUID | None = None,
    ) -> ScenarioOutcome:
        """THE PUBLIC ENTRY POINT. It has no schema-version parameter and will
        not grow one.

        Which contract a new scenario is sealed under is a property of the
        build, not a request field. If a caller could choose it, a customer
        request would decide what evidence a sealed artifact carries and what
        contract it is hashed under — so the choice stays on the inside of this
        module, expressed once, in the one write authority below.
        """
        return await self._simulate(
            analysis_id, spec,
            idempotency_key=idempotency_key,
            refreshed_from_scenario_id=refreshed_from_scenario_id,
            result_schema_version=CURRENT_SCENARIO_RESULT_SCHEMA_VERSION,
        )

    async def _simulate(
        self,
        analysis_id: uuid.UUID,
        spec: ScenarioSpec,
        *,
        idempotency_key: str | None = None,
        refreshed_from_scenario_id: uuid.UUID | None = None,
        result_schema_version: str,
    ) -> ScenarioOutcome:
        """THE INTERNAL SEAM (Entry 12B1 Phase A2).

        Private, and reachable only from inside this package. It exists so that
        a v2 artifact can be created, sealed and replayed end to end while
        ordinary production creation keeps writing v1 — the capability is
        proved before it is switched on, rather than switched on and then
        proved.

        It is NOT runtime version negotiation. There is no fallback, no "latest",
        and no widening: an unsupported version raises here, before a header row
        exists, so a scenario can never be created under a contract this build
        cannot also canonicalize and replay.

        One version travels from this argument to every version-bearing field —
        the manifest inside the spec hash, the Scenario row, the ScenarioResult
        row and the canonicalization used for hashing. They cannot disagree
        because there is nothing else for any of them to read.
        """
        if result_schema_version not in SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS:
            raise UnsupportedResultSchemaVersion(
                f"cannot create a scenario under unsupported result schema "
                f"version {result_schema_version!r}"
            )

        # ---- TX-1: pin, resolve idempotency, create the header ----
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            try:
                pinned = await self._pin_specification(
                    session, analysis_id, spec,
                    result_schema_version=result_schema_version,
                )
            except ScenarioFrozenInputError as exc:
                # Fail closed BEFORE the header exists: an unresolvable baseline
                # produces no scenario row, no levers, no assumptions and no
                # sealed children — nothing that would later look like a result.
                await self._record_baseline_failure(session, analysis_id, exc)
                raise ScenarioBaselineUnavailable(exc.reason) from None
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
                result_schema_version=result_schema_version,
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
            await bulk_insert(session, ScenarioLever, [
                {
                    "scenario_id": scenario.id,
                    "apply_order": lever.apply_order,
                    "lever_code": lever.lever_code,
                    "parameters": {
                        k: str(v) for k, v in sorted(lever.parameters.items())
                    },
                }
                for lever in spec.levers
            ])
            await bulk_insert(session, ScenarioAssumption, [
                {
                    "scenario_id": scenario.id,
                    "assumption_code": assumption.assumption_code,
                    "value_number": assumption.value_number,
                    "value_text": assumption.value_text,
                    "value_boolean": assumption.value_boolean,
                    "materiality": assumption.materiality,
                    "source": assumption.source,
                    "certainty": assumption.certainty,
                    "affects_eligibility": assumption.affects_eligibility,
                }
                for assumption in spec.assumptions
            ])
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
            result_hash = await self._persist(
                scenario_id, pinned, computed,
                result_schema_version=result_schema_version,
            )
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

        Every number below descends from `pinned.frozen` and from nothing else.
        The hypothetical starts as a clone of the pinned snapshot, the baseline
        it is compared against is the one that snapshot produced, and the
        objective is measured over both. There is no session in scope here, so
        the compute phase has no route to live data even by mistake.

        `apply_all` is atomic: a failure part-way leaves the clone untouched and
        raises, so a half-applied composite lever can never reach the engine.
        """
        frozen = pinned.frozen
        clone = frozen.baseline_clone()               # the frozen input, cloned
        applications = [
            (lever.lever_code, dict(lever.parameters)) for lever in pinned.spec.levers
        ]
        applied = lever_registry.apply_all(
            clone, applications,
            jurisdiction=pinned.jurisdiction, tax_year=pinned.tax_year,
        )
        # The frozen baseline must be untouched: a second clone taken AFTER the
        # levers ran still has to equal the one taken before them.
        assert clone == frozen.baseline_clone(), "baseline input was mutated in place"

        scenario_input = to_tax_input(applied.inputs)
        scenario_result = compute(scenario_input, pinned.dataset)
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
            evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
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
            # ---- retained, not recomputed (Entry 12B1 Phase A1) ----
            #
            # Both of these were already produced by the single engine run above
            # and then discarded. Keeping them costs nothing and is the only way
            # a counterfactual derived state can be built WITHOUT running the
            # engine a second time — a second run would be a second chance to
            # disagree with the number this scenario was sealed from.
            #
            # `facts` deliberately goes through `TaxEngineService.facts_for`,
            # which takes the already-computed result. The tempting alternative,
            # `eligibility.engine_facts_for(inputs)`, re-enters `compute()`
            # internally; `tests/unit/ioe/test_scenario_compute_retention.py`
            # counts engine executions so that substitution cannot pass review.
            #
            # NEITHER KEY REACHES A HASH. `_canonical_result_v1` selects its
            # fields by name, so this addition is inert for every sealed v1
            # artifact — asserted directly, not assumed.
            "line_items": [dict(item) for item in scenario_result.line_items],
            "facts": TaxEngineService.facts_for(scenario_input, scenario_result),
        }

    # ---------------------------------------------------------------- TX-2 ---
    async def _persist(
        self, scenario_id: uuid.UUID, pinned: PinnedScenarioSpec, computed: dict,
        *, result_schema_version: str,
    ) -> str:
        """TX-2. Every piece of evidence a seal commits to is built INSIDE this
        transaction, before the seal.

        The ordering matters more than it looks. A v2 artifact binds a
        counterfactual derived state through its hash, so the state, its
        canonical payload and that payload's digest all have to exist before
        the outer result hash can be computed at all — and the outer hash is
        what the seal records. Building the derived state after the commit
        would leave a window in which a scenario claimed to be v2, carried a
        hash over evidence, and had no evidence; the CHECK constraint would
        reject the row, but only after the seal had already been written by a
        different statement. Doing the work here means the failure mode is a
        rolled-back transaction instead of a torn historical artifact.

        For v1 the derived state is not built at all. A v1 seal binds nothing
        to it, so building it would cost a rules evaluation per scenario to
        produce something no hash covers and no column stores.
        """
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            scenario = await session.get(Scenario, scenario_id)
            if scenario is None:
                raise NotFound("Scenario not found")

            derived_payload: dict | None = None
            derived_hash: str | None = None
            if result_schema_version in DERIVED_STATE_BEARING_VERSIONS:
                # T1, captured inside TX-2 and therefore inside the same
                # transaction as the seal. Capturing after the commit would
                # describe a library that had already moved on; capturing in a
                # separate transaction would leave a window where the scenario
                # is sealed against evidence nothing recorded.
                baseline_held_evidence = await held_evidence.capture_held_evidence(
                    session, self.user_id, pinned.tax_year)
                # The certified Phase A1 builder, reused rather than
                # reimplemented — persistence and replay verification must
                # derive the same state from the same inputs or the hash they
                # agree on means nothing.
                derived_payload, derived_hash = (
                    counterfactual.canonical_payload_and_hash(
                        await self.build_counterfactual_derived_state(
                            session, pinned, computed,
                            baseline_held_evidence=baseline_held_evidence,
                            # BOUND to the result contract being sealed, never
                            # defaulted: a result-v2 row must carry the derived
                            # shape every result-v2 row already carries, or
                            # "v2" would mean two things depending on the date.
                            derived_state_schema_version=(
                                counterfactual.DERIVED_STATE_FOR_RESULT_VERSION[
                                    result_schema_version]),
                        )
                    )
                )

            result_payload = self.canonical_result(
                computed,
                result_schema_version=result_schema_version,
                counterfactual_derived_state_hash=derived_hash,
            )
            result_hash = c.scenario_result_hash(
                spec_hash=pinned.spec_hash, result=result_payload
            )

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
                result_schema_version=result_schema_version,
                counterfactual_derived_state=derived_payload,
                counterfactual_derived_state_hash=derived_hash,
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
            await bulk_insert(session, ScenarioConfidenceComponent, [
                {
                    "scenario_id": scenario_id,
                    "factor_code": component.factor_code.value,
                    "value": component.value,
                    "weight": component.weight,
                    "contribution": component.contribution,
                    "reason_code": component.reason_code,
                }
                for component in breakdown.components
            ])

            # The applied-change trace records FIELD NAMES and the values the
            # registry produced. It does not copy the user's other financial
            # inputs; those stay in the frozen analysis snapshot.
            await bulk_insert(session, ScenarioInputChange, [
                {
                    "scenario_id": scenario_id,
                    "lever_code": change.lever_code,
                    "field": change.field,
                    "old_value": str(change.old_value),
                    "new_value": str(change.new_value),
                    "apply_order": change.apply_order,
                }
                for change in computed["changes"]
            ])

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
    def canonical_result(
        computed: dict,
        *,
        result_schema_version: str,
        counterfactual_derived_state_hash: str | None = None,
    ) -> dict:
        """The exact payload the result hash is taken over.

        Static and free of any session or clock so a replay can rebuild it from
        stored columns and get the same hash.

        `result_schema_version` IS REQUIRED and has no default. It used to be
        read from a module constant, which meant replay canonicalized historical
        artifacts under whatever contract the current build declared — so
        bumping that constant would have invalidated every sealed scenario.
        A caller must now state which contract it means: creation passes what it
        writes, replay passes what the row was sealed under.

        Delegates to the one dispatcher in `domain/scenario.py` so no second
        interpretation of a version can grow here.
        """
        return canonical_scenario_result(
            computed,
            result_schema_version=result_schema_version,
            counterfactual_derived_state_hash=counterfactual_derived_state_hash,
        )

    async def build_counterfactual_derived_state(
        self,
        session: AsyncSession,
        pinned: PinnedScenarioSpec,
        computed: dict,
        *,
        baseline_held_evidence: HistoricalHeldEvidenceSnapshot,
        derived_state_schema_version: str = (
            counterfactual.COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION),
    ) -> CounterfactualDerivedState:
        """Derive the counterfactual state from an ALREADY-COMPUTED scenario.

        THE ONE BUILDER. Every future caller — sealing, replay verification, a
        comparison — goes through this method, because two builders would be two
        chances to disagree about what a counterfactual state contains, and the
        disagreement would surface as a hash mismatch that looked like data
        corruption.

        NOTHING IS RECOMPUTED HERE. The tax state arrives as
        `computed["line_items"]`, retained from the single engine run in
        `_compute`. The facts arrive as `computed["facts"]`, derived from that
        same run's result. This method's only external call is the rules
        evaluation, which the scenario never performed at all.

        WHY THE PINNED SET IS PASSED THROUGH UNCHANGED. `evaluate` distinguishes
        three cases, and all three are meaningful:

            None        resolve today's published rules  — WRONG here, it would
                        let a rule published after sealing enter a historical
                        counterfactual
            []          nothing was pinned, so nothing is eligible
            [ids...]    exactly this immutable version set

        So the list is forwarded as-is. An `or None` — the obvious-looking way
        to "handle the empty case" — silently converts the second into the
        first, turning "this scenario pinned no rules" into "evaluate whatever
        exists now". That is why the list is built once and passed twice with no
        conditional between.

        `baseline_held_evidence` IS REQUIRED AND HAS NO DEFAULT. It is passed
        in — captured from the live library at creation, loaded from the seal at
        replay — and never fetched here. A default would let replay build a
        March scenario against an empty or a June library and report the
        difference as tampering.
        """
        for key in ("line_items", "facts"):
            if key not in computed:
                # Refused rather than defaulted. Building from a computed dict
                # that never retained the engine output would produce a
                # perfectly well-formed derived state describing nothing, and
                # an empty counterfactual is indistinguishable from a scenario
                # in which nothing was eligible.
                raise ValueError(
                    f"computed result did not retain {key!r}; the counterfactual "
                    "state cannot be derived without it"
                )

        pinned_rule_version_ids = list(pinned.pinned_rule_version_ids)
        evaluator = RulesEvaluatorService(session)
        opportunities = await evaluator.evaluate(
            pinned.tax_year,
            computed["facts"],
            pinned_rule_version_ids=pinned_rule_version_ids,
        )
        # THE SAME CALL, THE SAME PINNED SET, THE OTHER SIDE'S FACTS.
        #
        # Entry 12B: without this the baseline had no frozen opportunity source
        # at all, and a comparator would have read every counterfactual
        # opportunity as one the scenario created. Nothing new is computed —
        # `baseline_facts` were produced by the single engine run that already
        # pinned this scenario's baseline result and were previously discarded.
        #
        # Deliberately the identical evaluator instance, tax year and pinned
        # list: the two sides must differ ONLY in their facts, or a difference
        # in how they were evaluated would be reported as a difference in the
        # user's tax position.
        #
        # SKIPPED ENTIRELY for a v1 derived state. Replay rebuilds under the
        # contract the row was SEALED with, and a v1 artifact never carried a
        # baseline set — evaluating one would change the rebuilt payload and
        # report every sealed v1 artifact as irreconcilable.
        baseline_opportunities = None
        if (derived_state_schema_version
                != counterfactual.DERIVED_STATE_SCHEMA_V1):
            baseline_opportunities = await evaluator.evaluate(
                pinned.tax_year,
                dict(pinned.frozen.baseline_facts),
                pinned_rule_version_ids=pinned_rule_version_ids,
            )
        return counterfactual.build_derived_state(
            line_items=computed["line_items"],
            opportunities=opportunities,
            pinned_rule_version_ids=pinned_rule_version_ids,
            baseline_held_evidence=baseline_held_evidence,
            baseline_opportunities=baseline_opportunities,
            schema_version=derived_state_schema_version,
        )

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

    async def _own_scenario(
        self, session: AsyncSession, scenario_id: uuid.UUID
    ) -> Scenario:
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
    "BASELINE_REFUSAL_COUNTS",
    "SCENARIO_SERVICE_VERSION",
    "baseline_refusal_metrics",
    "reset_baseline_refusal_metrics",
    "PinnedScenarioSpec",
    "ScenarioBaselineUnavailable",
    "ScenarioIdempotencyKeyReused",
    "ScenarioOutcome",
    "ScenarioService",
]
