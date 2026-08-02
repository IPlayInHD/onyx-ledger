"""OptimizationOrchestrator — the P3 workflow (architecture §10, §22, §23).

Transaction pattern, normative:

  TX-1 (short)  resolve idempotency, PIN every material specification input,
                compute `optimization_spec_hash`, insert the run and move it to
                `running`. Commit — the user can now see work in progress.
  compute       outside any transaction: read-only reads, rules evaluation
                CONSTRAINED to the pinned snapshot, and pure domain maths. No
                locks are held during the expensive phase.
  TX-2 (atomic) insert ALL immutable evidence, seal the result hash, move to
                `completed`. Commit — children and completion become visible
                together, so a partial result set is never reachable.
  TX-3 (short)  on failure: move to `failed` with a SANITIZED error code. No
                result children exist.

Idempotency is resolved against the canonical spec hash, not the request body:
two canonically equivalent requests replay the same run, while the same
`Idempotency-Key` presented with a different spec is refused rather than
silently replaying a result that does not match what was asked for.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, DomainError, NotFound
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    OptimizationRun,
    OptimizationRunEvent,
    RunRuleSnapshot,
    RunRuleVersion,
    WeightConfig,
)
from app.database.models import (
    ConfidenceComponent as ConfidenceComponentRow,
)
from app.database.models import (
    OptimizationCandidate as CandidateRow,
)
from app.database.models import (
    RecommendationRelationship as RelationshipRow,
)
from app.database.models import (
    ScoreComponent as ScoreComponentRow,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain import portfolio as assembly
from app.services.ioe.domain import relationships as relationship_rules
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.domain import scoring
from app.services.ioe.domain.enums import ScoreFactor, WorkflowStatus
from app.services.ioe.domain.workflow import WorkflowStateMachine
from app.services.ioe.normalization.service import (
    NORMALIZATION_VERSION,
    OpportunityNormalizationService,
)
from app.services.ioe.portfolio.service import (
    PORTFOLIO_SERVICE_VERSION,
    PortfolioEvaluationService,
)
from app.services.ioe.snapshot.service import RuleSnapshotService
from app.services.tax_engine.contracts import CONTRACT_VERSION
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.rules_service import RulesEvaluatorService
from app.services.tax_engine.service import ENGINE_VERSION, TaxEngineService

IOE_ORCHESTRATOR_VERSION = "1.0.0"


class IdempotencyKeyReused(DomainError):
    """The same Idempotency-Key was presented with a different specification."""

    status_code = 409
    error_type = "https://onyx.ledger/errors/idempotency-key-reused"
    title = "Idempotency Key Reused"


# Sanitized, enumerated failure codes. Never a message, stack trace, or PII.
ERROR_ANALYSIS_NOT_READY = "ANALYSIS_NOT_READY"
ERROR_RULES_EVALUATION_FAILED = "RULES_EVALUATION_FAILED"
ERROR_NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
ERROR_SCORING_FAILED = "SCORING_FAILED"
ERROR_PORTFOLIO_ASSEMBLY_FAILED = "PORTFOLIO_ASSEMBLY_FAILED"
ERROR_PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
ERROR_INTERNAL = "INTERNAL_ERROR"


@dataclass
class PinnedSpec:
    """Everything material to the calculation, resolved BEFORE the hash."""

    analysis_id: uuid.UUID
    tax_year: int
    jurisdiction: str
    baseline_input_snapshot_hash: str
    rule_snapshot_id: uuid.UUID
    rule_snapshot_hash: str
    pinned_rule_version_ids: list[uuid.UUID]
    weight_config_id: uuid.UUID | None
    weight_config_version: str
    version_manifest: dict
    manifest_hash: str
    user_constraints: dict
    assumption_set: list[dict]
    spec_hash: str = ""


@dataclass
class OptimizationOutcome:
    run_id: uuid.UUID
    spec_hash: str
    result_hash: str | None
    workflow_status: str
    replayed: bool = False
    candidate_count: int = 0
    relationship_count: int = 0
    portfolio_member_count: int = 0
    error_code: str | None = None
    warnings: list[str] = field(default_factory=list)


class OptimizationOrchestrator:
    """Owns the transaction boundaries. Services it calls stay transaction-free."""

    def __init__(self, user_id: uuid.UUID):
        self.user_id = user_id

    # ---------------------------------------------------------------- TX-1 ---
    async def _pin_specification(
        self,
        session: AsyncSession,
        analysis_id: uuid.UUID,
        *,
        user_constraints: dict | None,
        assumptions: list[dict] | None,
    ) -> PinnedSpec:
        """Resolve and pin EVERY material input, then compute the spec hash.

        The hash is computed last on purpose: an input resolved after hashing
        could change the calculation without changing its identity.
        """
        analysis = await session.get(AnalysisRun, analysis_id)
        if analysis is None or analysis.user_id != self.user_id:
            # ownership is validated in the application layer as well as by RLS
            raise NotFound("Analysis not found")
        if analysis.status != "completed":
            raise Conflict("analysis_not_ready: run an analysis before optimizing")

        snapshot_row = await session.get(AnalysisInputSnapshot, analysis_id)
        baseline_hash = snapshot_row.snapshot_hash if snapshot_row else ""

        # immutable rule snapshot — pinned here, and it CONSTRAINS evaluation
        pinned = await RuleSnapshotService(session).capture(analysis.tax_year)

        weight_config = await session.scalar(
            select(WeightConfig).where(WeightConfig.is_active.is_(True))
        )

        manifest = {
            "ioe_orchestrator_version": IOE_ORCHESTRATOR_VERSION,
            "tax_engine_version": ENGINE_VERSION,
            "engine_reference_data_version": engine_data.REFERENCE_DATA_VERSION,
            "rules_evaluator_contract_version": CONTRACT_VERSION,
            "opportunity_normalization_version": NORMALIZATION_VERSION,
            "scoring_algorithm_version": scoring.SCORING_ALGORITHM_VERSION,
            "confidence_algorithm_version": support.CONFIDENCE_ALGORITHM_VERSION,
            "relationship_registry_version": relationship_rules.RELATIONSHIP_REGISTRY_VERSION,
            "canonical_serialization_version": c.CANONICAL_SERIALIZATION_VERSION,
            # P4 — these change portfolio results, so they change run identity
            "portfolio_service_version": PORTFOLIO_SERVICE_VERSION,
            "portfolio_assembly_version": assembly.PORTFOLIO_ASSEMBLY_VERSION,
            "lever_registry_version": lever_registry.LEVER_REGISTRY_VERSION,
            "portfolio_objective_code": savings_domain.PORTFOLIO_OBJECTIVE_CODE.value,
            "portfolio_objective_version": savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            "rule_snapshot_hash": pinned.snapshot_hash,
            "weight_config_version": weight_config.version if weight_config else "default",
        }
        manifest_hash = c.version_manifest_hash(manifest)

        constraints = _canonical_constraints(user_constraints or {})
        assumption_set = list(assumptions or [])

        spec = PinnedSpec(
            analysis_id=analysis_id,
            tax_year=analysis.tax_year,
            jurisdiction=analysis.province_code or "FED",
            baseline_input_snapshot_hash=baseline_hash,
            rule_snapshot_id=pinned.snapshot_id,
            rule_snapshot_hash=pinned.snapshot_hash,
            pinned_rule_version_ids=pinned.version_ids(),
            weight_config_id=weight_config.id if weight_config else None,
            weight_config_version=weight_config.version if weight_config else "default",
            version_manifest=manifest,
            manifest_hash=manifest_hash,
            user_constraints=constraints,
            assumption_set=assumption_set,
        )
        # every material input is now resolved — only now is identity computed
        spec.spec_hash = c.optimization_spec_hash(
            baseline_input_snapshot_hash=spec.baseline_input_snapshot_hash,
            tax_year=spec.tax_year,
            jurisdiction=spec.jurisdiction,
            rule_version_set=spec.pinned_rule_version_ids,
            version_manifest=spec.version_manifest,
            user_constraints=spec.user_constraints,
            assumption_set=spec.assumption_set,
        )
        return spec

    async def _resolve_idempotency(
        self, session: AsyncSession, spec: PinnedSpec, idempotency_key: str | None
    ) -> OptimizationRun | None:
        """Replay an equivalent run, or refuse a reused key.

        Resolution is against the canonical SPEC HASH: equivalence is a property
        of the calculation, not of how the request happened to be written.
        """
        if idempotency_key:
            keyed = await session.scalar(
                select(OptimizationRun).where(
                    OptimizationRun.user_id == self.user_id,
                    OptimizationRun.idempotency_key == idempotency_key,
                )
            )
            if keyed is not None:
                if keyed.optimization_spec_hash != spec.spec_hash:
                    raise IdempotencyKeyReused(
                        "idempotency_key_reused: this key was used for a "
                        "materially different specification"
                    )
                return keyed

        return await session.scalar(
            select(OptimizationRun).where(
                OptimizationRun.user_id == self.user_id,
                OptimizationRun.optimization_spec_hash == spec.spec_hash,
                OptimizationRun.workflow_status.in_(("pending", "running", "completed")),
                OptimizationRun.freshness_status == "current",
            )
        )

    # ------------------------------------------------------------ pipeline ---
    async def generate(
        self,
        analysis_id: uuid.UUID,
        *,
        idempotency_key: str | None = None,
        user_constraints: dict | None = None,
        assumptions: list[dict] | None = None,
    ) -> OptimizationOutcome:
        # ---- TX-1: pin, resolve idempotency, create the header ----
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            spec = await self._pin_specification(
                session, analysis_id,
                user_constraints=user_constraints, assumptions=assumptions,
            )
            existing = await self._resolve_idempotency(session, spec, idempotency_key)
            if existing is not None:
                return OptimizationOutcome(
                    run_id=existing.id, spec_hash=spec.spec_hash,
                    result_hash=existing.optimization_result_hash,
                    workflow_status=existing.workflow_status, replayed=True,
                )

            run = OptimizationRun(
                user_id=self.user_id, analysis_id=analysis_id, tax_year=spec.tax_year,
                optimization_spec_hash=spec.spec_hash,
                rule_snapshot_id=spec.rule_snapshot_id,
                weight_config_id=spec.weight_config_id,
                version_manifest=spec.version_manifest,
                manifest_hash=spec.manifest_hash,
                idempotency_key=idempotency_key,
                started_at=datetime.now(tz=UTC),
            )
            session.add(run)
            try:
                await session.flush()
            except IntegrityError:
                # a concurrent request won the unique index; attach to its run
                await session.rollback()
                raise Conflict(
                    "concurrent_optimization: an equivalent run is already in progress"
                ) from None

            session.add(OptimizationRunEvent(
                run_id=run.id, from_status=None, to_status=WorkflowStatus.PENDING.value,
            ))
            WorkflowStateMachine.assert_transition(
                WorkflowStatus.PENDING, WorkflowStatus.RUNNING
            )
            run.workflow_status = WorkflowStatus.RUNNING.value
            session.add(OptimizationRunEvent(
                run_id=run.id, from_status=WorkflowStatus.PENDING.value,
                to_status=WorkflowStatus.RUNNING.value,
            ))
            await session.flush()
            run_id = run.id

        # ---- compute: no transaction held ----
        try:
            computed = await self._compute(spec)
        except Exception as exc:  # noqa: BLE001
            await self._fail(run_id, _classify(exc))
            raise

        # ---- TX-2: persist all evidence and complete, atomically ----
        try:
            result_hash = await self._persist(run_id, spec, computed)
        except Exception:  # noqa: BLE001
            await self._fail(run_id, ERROR_PERSISTENCE_FAILED)
            raise

        return OptimizationOutcome(
            run_id=run_id, spec_hash=spec.spec_hash, result_hash=result_hash,
            workflow_status=WorkflowStatus.COMPLETED.value,
            candidate_count=len(computed["candidates"]),
            relationship_count=len(computed["relationships"]),
            portfolio_member_count=(
                len(computed["portfolio"].members) if computed.get("portfolio") else 0
            ),
        )

    async def _compute(self, spec: PinnedSpec) -> dict:
        """Read-only reads plus pure domain maths. Holds no transaction."""
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            engine = TaxEngineService(session)
            inp = await engine.build_input(self.user_id, spec.tax_year)
            result = engine.run(inp)
            facts = engine.facts(inp, result)

            # CONSTRAINED to the pinned snapshot: a rule published after TX-1
            # cannot enter this run.
            opportunities = await RulesEvaluatorService(session).evaluate(
                spec.tax_year, facts,
                pinned_rule_version_ids=spec.pinned_rule_version_ids,
            )

        normalizer = OpportunityNormalizationService()
        candidates = [normalizer.normalize(o) for o in opportunities]

        for candidate in candidates:
            candidate.confidence = support.compute(
                evidence_status=candidate.evidence_status,
                calculation_basis=(
                    candidate.calculation_basis
                    or support.CalculationBasis.RULE_FORMULA_DETERMINED
                ),
            )

        available_cash = _available_cash(spec.user_constraints)
        ranked = scoring.rank(candidates, available_cash=available_cash)
        relationships = relationship_rules.derive(ranked)

        # ---- P4: constrained assembly, every figure measured by the engine ----
        constraints = assembly.AssemblyConstraints(
            available_cash=available_cash,
            objective_metric=savings_domain.PORTFOLIO_OBJECTIVE_CODE,
            objective_version=savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            resource_capacities=_resource_capacities(spec.user_constraints),
            jurisdiction=spec.jurisdiction,
            tax_year=spec.tax_year,
        )
        portfolio = PortfolioEvaluationService().evaluate(
            ranked, relationships, inp, constraints
        )
        return {
            "candidates": ranked,
            "relationships": relationships,
            "portfolio": portfolio,
        }

    # ---------------------------------------------------------------- TX-2 ---
    async def _persist(self, run_id: uuid.UUID, spec: PinnedSpec, computed: dict) -> str:
        """One atomic transaction: all children, the sealed hash, and completion."""
        candidates = computed["candidates"]
        relationships = computed["relationships"]
        portfolio = computed.get("portfolio")

        result_payload = {
            "candidates": [x.as_canonical() for x in candidates],
            "relationships": [r.as_canonical() for r in relationships],
            "portfolio": portfolio.as_canonical() if portfolio else None,
        }
        result_hash = c.optimization_result_hash(
            spec_hash=spec.spec_hash, result=result_payload
        )

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            run = await session.get(OptimizationRun, run_id)
            if run is None:
                raise NotFound("Optimization run not found")

            session.add(RunRuleSnapshot(
                run_id=run_id, snapshot_id=spec.rule_snapshot_id, replay_status="verified",
            ))
            for version_id in spec.pinned_rule_version_ids:
                session.add(RunRuleVersion(run_id=run_id, tax_rule_version_id=version_id))

            key_to_row: dict[str, CandidateRow] = {}
            for candidate in candidates:
                row = CandidateRow(
                    run_id=run_id,
                    opportunity_code=candidate.opportunity_code,
                    tax_rule_version_id=(
                        uuid.UUID(candidate.rule_version_id)
                        if candidate.rule_version_id else None
                    ),
                    eligibility_status=candidate.eligibility_status.value,
                    calculation_basis=(
                        candidate.calculation_basis.value
                        if candidate.calculation_basis else None
                    ),
                    evidence_status=candidate.evidence_status.value,
                    portfolio_membership=candidate.portfolio_membership.value,
                    exclusion_reason_code=candidate.exclusion_reason_code,
                    candidate_rank=candidate.rank,
                    recommendation_score=candidate.score.overall if candidate.score else None,
                    # five-stage support score (migration 0029); confidence_score
                    # is derived from display_support_score by DB trigger
                    raw_support_score=candidate.confidence.raw_support_score,
                    assumption_adjusted_score=candidate.confidence.assumption_adjusted_score,
                    display_support_score=candidate.confidence.display_support_score,
                    support_cap_applied=candidate.confidence.cap_applied,
                    support_cap_reason_code=candidate.confidence.cap_reason_code,
                )
                session.add(row)
                await session.flush()
                key_to_row[candidate.candidate_key] = row

                for comp in candidate.score.components if candidate.score else ():
                    session.add(ScoreComponentRow(
                        candidate_id=row.id, factor_code=comp.factor_code.value,
                        raw_value=comp.raw_value, normalized_value=comp.normalized_value,
                        weight=comp.weight, contribution=comp.contribution,
                    ))
                for comp in candidate.confidence.components:
                    session.add(ConfidenceComponentRow(
                        candidate_id=row.id, factor_code=comp.factor_code.value,
                        value=comp.value, weight=comp.weight,
                        contribution=comp.contribution, reason_code=comp.reason_code,
                    ))

            for edge in relationships:
                source, target = key_to_row.get(edge.source_key), key_to_row.get(edge.target_key)
                if source is None or target is None:
                    continue
                session.add(RelationshipRow(
                    run_id=run_id,
                    source_candidate_id=source.id, target_candidate_id=target.id,
                    relationship_type=edge.relationship_type.value,
                    shared_resource_code=edge.shared_resource_code,
                    maximum_shared_amount=edge.maximum_shared_amount,
                    measured_delta=edge.measured_delta,
                    explanation_code=edge.explanation_code,
                    resolution_options=list(edge.resolution_options),
                ))

            if portfolio is not None:
                await PortfolioEvaluationService().persist(
                    session, run_id, portfolio,
                    {key: row.id for key, row in key_to_row.items()},
                )

            WorkflowStateMachine.assert_transition(
                WorkflowStatus.RUNNING, WorkflowStatus.COMPLETED
            )
            run.optimization_result_hash = result_hash
            run.workflow_status = WorkflowStatus.COMPLETED.value
            run.completed_at = datetime.now(tz=UTC)
            run.evaluated_at = datetime.now(tz=UTC)
            session.add(OptimizationRunEvent(
                run_id=run_id, from_status=WorkflowStatus.RUNNING.value,
                to_status=WorkflowStatus.COMPLETED.value,
            ))
            await session.flush()
        return result_hash

    # ---------------------------------------------------------------- TX-3 ---
    async def _fail(self, run_id: uuid.UUID, error_code: str) -> None:
        """Short transaction recording a SANITIZED failure. No result children."""
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            run = await session.get(OptimizationRun, run_id)
            if run is None or run.workflow_status != WorkflowStatus.RUNNING.value:
                return
            run.workflow_status = WorkflowStatus.FAILED.value
            run.error_code = error_code
            run.completed_at = datetime.now(tz=UTC)
            session.add(OptimizationRunEvent(
                run_id=run_id, from_status=WorkflowStatus.RUNNING.value,
                to_status=WorkflowStatus.FAILED.value, reason_code=error_code,
            ))


def _classify(exc: Exception) -> str:
    """Map an exception to an enumerated code; the message never escapes."""
    name = type(exc).__name__
    # A portfolio that cannot reconcile, or a ledger that lost conservation, is
    # never shown — the run fails rather than reporting an unverifiable total.
    if "Reconciliation" in name or "LedgerConservation" in name:
        return ERROR_PORTFOLIO_ASSEMBLY_FAILED
    if "Rules" in name or "Evaluat" in name:
        return ERROR_RULES_EVALUATION_FAILED
    if "Lever" in name or "Normal" in name:
        return ERROR_NORMALIZATION_FAILED
    if "WeightConfig" in name or "Scoring" in name:
        return ERROR_SCORING_FAILED
    return ERROR_INTERNAL


def _canonical_constraints(constraints: dict) -> dict:
    """Constraints enter the spec hash, so they are canonicalized explicitly."""
    out: dict = {}
    for key in sorted(constraints):
        value = constraints[key]
        out[key] = c.money(value) if isinstance(value, Decimal) else value
    return out


def _available_cash(constraints: dict) -> Decimal | None:
    raw = constraints.get("available_cash")
    return Decimal(raw) if raw is not None else None


def _resource_capacities(constraints: dict) -> dict[str, Decimal]:
    """Declared shared-pool capacities (RRSP room, FHSA room, ...).

    An UNDECLARED pool is uncapped, not zero: the assembler must not invent a
    contribution limit the user never stated and the rules did not publish.
    """
    raw = constraints.get("resource_capacities") or {}
    return {code: Decimal(str(raw[code])) for code in sorted(raw)}


__all__ = [
    "IdempotencyKeyReused",
    "OptimizationOrchestrator",
    "OptimizationOutcome",
    "ScoreFactor",
]
