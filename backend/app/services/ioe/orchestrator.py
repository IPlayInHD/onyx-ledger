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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, DomainError, NotFound
from app.database.base import uuid7
from app.database.bulk import bulk_insert
from app.database.models import (
    AnalysisRun,
    OptimizationRun,
    OptimizationRunEvent,
    RunRuleSnapshot,
    RunRuleVersion,
    WeightConfig,
)
from app.database.models import (
    CandidateCost as CostRow,
)
from app.database.models import (
    CandidateEconomicEffect as EconomicEffectRow,
)
from app.database.models import (
    ConfidenceComponent as ConfidenceComponentRow,
)
from app.database.models import (
    MultiYearProjection as MultiYearProjectionRow,
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
from app.services.admission import OperationClass, admission_guard
from app.services.admission.guard import owned_dedupe_key, user_scope
from app.services.ioe.domain import assumptions as assumption_registry
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.domain import cost_taxonomy, scoring
from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain import models as domain_models
from app.services.ioe.domain import portfolio as assembly
from app.services.ioe.domain import relationships as relationship_rules
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.domain.enums import (
    PortfolioMembership,
    ScoreFactor,
    WorkflowStatus,
)
from app.services.ioe.domain.workflow import WorkflowStateMachine
from app.services.ioe.frozen import (
    INPUT_EXECUTION_POLICY_VERSION,
    FrozenAnalysisInput,
    FrozenAnalysisInputService,
    FrozenSnapshotError,
)
from app.services.ioe.frozen.models import PINNED_SNAPSHOT_UNAVAILABLE
from app.services.ioe.normalization.service import (
    NORMALIZATION_VERSION,
    OpportunityNormalizationService,
)
from app.services.ioe.portfolio.eligibility import (
    ELIGIBILITY_RECHECK_VERSION,
    PinnedEligibilityRechecker,
    engine_facts_for_dataset,
    load_pinned_condition_trees,
)
from app.services.ioe.portfolio.service import (
    PORTFOLIO_SERVICE_VERSION,
    PortfolioEvaluationService,
)
from app.services.ioe.projection import PROJECTION_METHODOLOGY_VERSION
from app.services.ioe.snapshot.service import RuleSnapshotService
from app.services.tax_engine.contracts import CONTRACT_VERSION
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.core.engine import TaxInput
from app.services.tax_engine.rules_service import RulesEvaluatorService
from app.services.tax_engine.service import ENGINE_VERSION, TaxEngineService

IOE_ORCHESTRATOR_VERSION = "1.0.0"


class IdempotencyKeyReused(DomainError):
    """The same Idempotency-Key was presented with a different specification."""

    status_code = 409
    error_type = "https://onyx.ledger/errors/idempotency-key-reused"
    title = "Idempotency Key Reused"


class DuplicateCandidateKey(DomainError):
    """Two candidates in one run claimed the same semantic identity.

    `candidate_key` is what a portfolio member, a counterfactual comparison and
    a historical graph node all match on. Two candidates sharing one does not
    make them the same opportunity; it makes every one of those matches wrong.
    """

    status_code = 500
    error_type = "https://onyx.ledger/errors/duplicate-candidate-key"
    title = "Duplicate Candidate Key"

    def __init__(self, candidate_key: str):
        self.candidate_key = candidate_key
        super().__init__(
            f"two candidates share the semantic key {candidate_key!r}; "
            "one would silently stand in for the other"
        )


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
    # The reconstructed frozen baseline. Held in memory for the run only and
    # never persisted: it is the user's financial data, it already lives in the
    # analysis snapshot, and copying it into IOE evidence would duplicate raw
    # financial inputs into result tables.
    frozen: FrozenAnalysisInput | None = None
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

        # The frozen baseline is resolved FIRST and verified before anything
        # else is pinned. Everything downstream — the spec hash, the rule
        # snapshot, idempotency — describes THIS input or it describes nothing.
        # A missing, corrupt, incomplete or unsupported snapshot fails closed
        # here, before a single engine run.
        frozen = await FrozenAnalysisInputService(session, self.user_id).resolve(
            analysis_id
        )
        baseline_hash = frozen.snapshot_hash

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
            "cost_taxonomy_version": cost_taxonomy.COST_TAXONOMY_VERSION,
            "eligibility_recheck_version": ELIGIBILITY_RECHECK_VERSION,
            "portfolio_objective_code": savings_domain.PORTFOLIO_OBJECTIVE_CODE.value,
            "portfolio_objective_version": savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            "assumption_registry_version": assumption_registry.ASSUMPTION_REGISTRY_VERSION,
            # How the inputs were obtained. A reader can tell a corrected run
            # from a legacy one without inferring it from a timestamp.
            "input_execution_policy_version": INPUT_EXECUTION_POLICY_VERSION.value,
            "snapshot_schema_version": frozen.snapshot_schema_version,
            "baseline_result_hash": frozen.baseline_result_hash,
            "projection_methodology_version": PROJECTION_METHODOLOGY_VERSION,
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
            frozen=frozen,
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

        # Bound to a name with a declared type: `AsyncSession.scalar` is typed
        # `-> Any`, so returning its result directly erased this method's own
        # declared return type at every call site.
        existing: OptimizationRun | None = await session.scalar(
            select(OptimizationRun).where(
                OptimizationRun.user_id == self.user_id,
                OptimizationRun.optimization_spec_hash == spec.spec_hash,
                OptimizationRun.workflow_status.in_(("pending", "running", "completed")),
                OptimizationRun.freshness_status == "current",
            )
        )
        return existing

    # ------------------------------------------------------------ pipeline ---
    async def generate(
        self,
        analysis_id: uuid.UUID,
        *,
        idempotency_key: str | None = None,
        user_constraints: dict | None = None,
        assumptions: list[dict] | None = None,
    ) -> OptimizationOutcome:
        """Admission-controlled. Guarded HERE rather than at an endpoint because
        the only production trigger today is the Celery task, and a limit that
        lives on a route protects nothing a worker does."""
        async with admission_guard(
            OperationClass.OPTIMIZATION_RUN,
            scope_id=user_scope(self.user_id),
            # Keyed on the ANALYSIS, not on a client-supplied string: a retry
            # storm for the same analysis must resolve to the run already in
            # flight. Owner-prefixed, so no key can reach another account.
            dedupe_key=owned_dedupe_key(self.user_id, "optimization", str(analysis_id)),
        ):
            # No branch on `duplicate_of_active` is needed: TX-1's existing
            # spec-hash resolution already finds a run in pending/running/
            # completed for this specification and returns it instead of
            # starting a second engine run. The dedupe key's job here is
            # narrower — stop a retry storm from consuming a second LEASE while
            # that resolution happens.
            return await self._generate(
                analysis_id,
                idempotency_key=idempotency_key,
                user_constraints=user_constraints,
                assumptions=assumptions,
            )

    async def _generate(
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
                # Both entered the spec hash. Storing them is what makes that
                # hash independently recomputable during replay verification.
                user_constraints=spec.user_constraints,
                assumption_set=spec.assumption_set,
                input_execution_policy_version=INPUT_EXECUTION_POLICY_VERSION.value,
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

    async def _compute(
        self, spec: PinnedSpec, *, baseline_input: TaxInput | None = None
    ) -> dict[str, Any]:
        """Pure domain maths over the FROZEN baseline. Holds no transaction.

        The input comes from the snapshot pinned in TX-1 and from nowhere else.
        There is no live builder call here and no fallback to one: a run whose
        snapshot could not be resolved never reaches this method, because TX-1
        failed closed before creating the header.

        `baseline_input` is how a REPLAY supplies the same frozen baseline when
        re-deriving a sealed identity. It is the same object TX-1 resolved, not
        an alternative source.
        """
        inp = baseline_input
        if inp is None:
            if spec.frozen is None:           # defensive: never reachable
                raise FrozenSnapshotError(PINNED_SNAPSHOT_UNAVAILABLE)
            inp = spec.frozen.tax_input

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            engine = TaxEngineService(session)
            # Resolved ONCE per run. The same dataset then reaches the baseline,
            # every candidate cost and every eligibility recheck, so a run
            # cannot measure a candidate against tax law its baseline never saw.
            dataset = await engine.resolve_dataset(spec.tax_year)
            result = engine.run(inp, dataset)
            facts = engine.facts(inp, result)

            # CONSTRAINED to the pinned snapshot: a rule published after TX-1
            # cannot enter this run.
            opportunities = await RulesEvaluatorService(session).evaluate(
                spec.tax_year, facts,
                pinned_rule_version_ids=spec.pinned_rule_version_ids,
            )
            # Loaded ONCE, from the pinned versions only. Re-evaluation during
            # assembly is then pure and cannot reach the rules tables at all.
            condition_trees = await load_pinned_condition_trees(
                session, spec.pinned_rule_version_ids
            )

        normalizer = OpportunityNormalizationService()
        candidates = [normalizer.normalize(o) for o in opportunities]
        # Projection authorization travels with the opportunity, keyed by
        # candidate. It is rule data, so it is carried, never derived.
        authorizations = {
            normalizer.candidate_key(o): (o.projection, o.rule_version_id)
            for o in opportunities
        }

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
        rechecker = PinnedEligibilityRechecker(
            condition_trees, engine_facts_for_dataset(dataset))
        constraints = assembly.AssemblyConstraints(
            available_cash=available_cash,
            objective_metric=savings_domain.PORTFOLIO_OBJECTIVE_CODE,
            objective_version=savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            resource_capacities=_resource_capacities(spec.user_constraints),
            jurisdiction=spec.jurisdiction,
            tax_year=spec.tax_year,
            eligibility_recheck=rechecker.check,
        )
        portfolio = PortfolioEvaluationService().evaluate(
            ranked, relationships, inp, constraints, dataset=dataset
        )

        # Measured interactions exist only among the candidates the engine
        # actually evaluated together, so they are derived after assembly and
        # never fed back into it — the portfolio was decided before these edges
        # existed, and rewriting it from them would be circular. Apply order is
        # the portfolio's own, so each edge names the member that was already in
        # place when the engine measured the next one.
        by_key = {x.candidate_key: x for x in ranked}
        applied = [
            by_key[m.candidate_key]
            for m in sorted(portfolio.members, key=lambda m: m.apply_order)
            if m.candidate_key in by_key
        ]
        relationships = relationships + relationship_rules.measured_edges(applied)

        return {
            "candidates": ranked,
            "relationships": relationships,
            "portfolio": portfolio,
            "projection_authorizations": authorizations,
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

            # Candidate ids are generated here rather than read back per row.
            # Children reference their parent, so the alternative is one
            # `INSERT ... RETURNING` round trip per candidate — the fan-out this
            # is removing. `uuid7()` produces the same value shape as the column
            # default, and the ordering below is still the domain's.
            candidate_ids = [uuid7() for _ in candidates]
            key_to_id: dict[str, uuid.UUID] = {}
            for candidate, candidate_id in zip(candidates, candidate_ids, strict=True):
                # A dict comprehension here let a duplicate key overwrite its
                # predecessor without a sound. Both candidates were still
                # persisted, but every portfolio member that named either one
                # resolved to the SAME id, and `UNIQUE (portfolio_id,
                # candidate_id)` failed an INSERT far from the cause. The
                # semantic key is an identity; two candidates sharing one is a
                # contract violation, so it is stated where it happens.
                if candidate.candidate_key in key_to_id:
                    raise DuplicateCandidateKey(candidate.candidate_key)
                key_to_id[candidate.candidate_key] = candidate_id

            candidate_rows: list[dict] = []
            effect_rows: list[dict] = []
            cost_rows: list[dict] = []
            score_rows: list[dict] = []
            support_rows: list[dict] = []

            for candidate, candidate_id in zip(candidates, candidate_ids, strict=True):
                candidate_rows.append({
                    "id": candidate_id,
                    "run_id": run_id,
                    "opportunity_code": candidate.opportunity_code,
                    "tax_rule_version_id": (
                        uuid.UUID(candidate.rule_version_id)
                        if candidate.rule_version_id else None
                    ),
                    "eligibility_status": candidate.eligibility_status.value,
                    "calculation_basis": (
                        candidate.calculation_basis.value
                        if candidate.calculation_basis else None
                    ),
                    "evidence_status": candidate.evidence_status.value,
                    "portfolio_membership": candidate.portfolio_membership.value,
                    "exclusion_reason_code": candidate.exclusion_reason_code,
                    "candidate_rank": candidate.rank,
                    "recommendation_score": (
                        candidate.score.overall if candidate.score else None
                    ),
                    # five-stage support score (migration 0029); confidence_score
                    # is derived from display_support_score by DB trigger
                    "raw_support_score": candidate.confidence.raw_support_score,
                    "assumption_adjusted_score":
                        candidate.confidence.assumption_adjusted_score,
                    "display_support_score": candidate.confidence.display_support_score,
                    "support_cap_applied": candidate.confidence.cap_applied,
                    "support_cap_reason_code": candidate.confidence.cap_reason_code,
                    # eligibility that earlier actions unsettled, left visible
                    "requires_re_evaluation": (
                        candidate.portfolio_membership
                        is PortfolioMembership.REQUIRES_RE_EVALUATION
                    ),
                    "re_evaluation_reason_code": (
                        candidate.exclusion_reason_code
                        if candidate.portfolio_membership
                        is PortfolioMembership.REQUIRES_RE_EVALUATION else None
                    ),
                })

                # The economics behind every displayed figure, kept auditable.
                for effect in candidate.economic_effects:
                    effect_rows.append({
                        "candidate_id": candidate_id,
                        "effect_type": effect.effect_type.value,
                        "amount": effect.amount,
                        "calculation_basis": effect.calculation_basis.value,
                        "tax_year": effect.tax_year,
                        "horizon_years": effect.horizon_years,
                        "is_permanent": effect.is_permanent,
                        "reversibility": (
                            effect.reversibility.value if effect.reversibility else None
                        ),
                    })
                for cost in candidate.costs:
                    cost_rows.append({
                        "candidate_id": candidate_id,
                        "cost_type": cost.cost_type.value,
                        "amount": cost.amount,
                        "timing": cost.timing,
                        # what the RULE said, kept beside what it resolved to
                        "authored_cost_type": (
                            cost.authored_cost_type.value
                            if cost.authored_cost_type else None
                        ),
                        "cost_type_source": cost.cost_type_source,
                        "taxonomy_version": cost.taxonomy_version,
                    })
                for comp in candidate.score.components if candidate.score else ():
                    score_rows.append({
                        "candidate_id": candidate_id,
                        "factor_code": comp.factor_code.value,
                        "raw_value": comp.raw_value,
                        "normalized_value": comp.normalized_value,
                        "weight": comp.weight,
                        "contribution": comp.contribution,
                    })
                for comp in candidate.confidence.components:
                    support_rows.append({
                        "candidate_id": candidate_id,
                        "factor_code": comp.factor_code.value,
                        "value": comp.value,
                        "weight": comp.weight,
                        "contribution": comp.contribution,
                        "reason_code": comp.reason_code,
                    })

            relationship_rows: list[dict] = []
            for edge in relationships:
                source_id = key_to_id.get(edge.source_key)
                target_id = key_to_id.get(edge.target_key)
                if source_id is None or target_id is None:
                    continue
                relationship_rows.append({
                    "run_id": run_id,
                    "source_candidate_id": source_id,
                    "target_candidate_id": target_id,
                    "relationship_type": edge.relationship_type.value,
                    "shared_resource_code": edge.shared_resource_code,
                    "maximum_shared_amount": edge.maximum_shared_amount,
                    "measured_delta": edge.measured_delta,
                    "explanation_code": edge.explanation_code,
                    "derivation_source": edge.derivation_source.value,
                    "resolution_options": list(edge.resolution_options),
                })

            # Parents before children, so every foreign key resolves. The whole
            # set is one atomic unit: a failure anywhere leaves no evidence.
            await bulk_insert(session, RunRuleSnapshot, [{
                "run_id": run_id, "snapshot_id": spec.rule_snapshot_id,
                "replay_status": "verified",
            }])
            await bulk_insert(session, RunRuleVersion, [
                {"run_id": run_id, "tax_rule_version_id": version_id}
                for version_id in spec.pinned_rule_version_ids
            ])
            await bulk_insert(session, CandidateRow, candidate_rows)
            await bulk_insert(session, EconomicEffectRow, effect_rows)
            await bulk_insert(session, CostRow, cost_rows)
            await bulk_insert(session, ScoreComponentRow, score_rows)
            await bulk_insert(session, ConfidenceComponentRow, support_rows)
            await bulk_insert(session, RelationshipRow, relationship_rows)

            await self._persist_projections(
                session, run_id, spec, candidates,
                computed.get("projection_authorizations") or {},
            )

            if portfolio is not None:
                await PortfolioEvaluationService().persist(
                    session, run_id, portfolio, key_to_id,
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

    async def _persist_projections(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        spec: PinnedSpec,
        candidates: Sequence[domain_models.OptimizationCandidate],
        authorizations: dict[str, Any],
    ) -> int:
        """Generate projections ONLY for candidates a published rule authorized.

        No inference happens here. A candidate whose rule declared no projection
        metadata, or declared it incompletely, produces nothing — and produces
        nothing silently, because the absence is reported by the endpoint's
        status rather than by a zero.
        """
        from app.services.ioe.projection import (
            PROJECTION_METHODOLOGY_VERSION,
            ProjectionNotApplicable,
            authorize,
            project_recurring,
        )

        supplied = frozenset(
            a.get("assumption_code", "") for a in (spec.assumption_set or [])
        )
        rows: list[dict] = []
        for candidate in candidates:
            authorization, rule_version_id = authorizations.get(
                candidate.candidate_key, (None, None)
            )
            decision = authorize(
                authorization, available_assumption_codes=supplied
            )
            if not decision.authorized:
                continue
            for effect in candidate.economic_effects:
                try:
                    projection = project_recurring(
                        annual_amount=effect.amount,
                        effect_type=effect.effect_type,
                        base_tax_year=spec.tax_year,
                        horizon_years=decision.horizon_years,
                    )
                except ProjectionNotApplicable:
                    # the rule authorized a projection but this effect is
                    # one-off; authorizing does not make it recurrent
                    continue
                for year in projection.years:
                    rows.append({
                        "run_id": run_id,
                        "horizon_year": year.horizon_year,
                        "projected_amount": year.amount,
                        "effect_type": year.effect_type,
                        "calculation_basis": "projection_estimate",
                        "is_indexation_known": year.is_indexation_known,
                        "projection_method": decision.method,
                        "methodology_version": PROJECTION_METHODOLOGY_VERSION,
                        "authorizing_rule_version_id": rule_version_id,
                    })
        return await bulk_insert(session, MultiYearProjectionRow, rows)

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
    # A snapshot failure already carries a closed reason code. It is used
    # verbatim so an operator sees WHICH pinned dependency failed, and the
    # exception's own text — which has been near financial values — never
    # reaches storage.
    if isinstance(exc, FrozenSnapshotError):
        return exc.reason
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
