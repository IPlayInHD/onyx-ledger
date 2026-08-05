"""ReplayDependencyResolver — load the EXACT pinned dependencies, or refuse.

The rule this module exists to enforce: a replay may never substitute a current
version for a pinned one. If a run was sealed under tax engine `py-1.0.0` and
this process ships `py-1.1.0`, the historical calculation cannot be re-executed
here at all — the code that produced it is gone. Running the new engine and
comparing hashes would be worse than useless: it would report a mismatch that
means "we upgraded", indistinguishable from one that means "the evidence was
tampered with".

So every pinned EXECUTABLE version is compared against what this build actually
implements, and any difference resolves to `unavailable` with a precise reason
code — never to `mismatch`. Nothing has been shown to differ; we simply cannot
look.

Data dependencies (the frozen baseline snapshot, the rule snapshot, the pinned
rule-version set) are loaded from storage under ordinary tenant RLS. A missing
one is likewise `unavailable`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    RuleSnapshot,
    RunRuleSnapshot,
    RunRuleVersion,
)
from app.services.ioe.domain import assumptions as assumption_registry
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.domain import cost_taxonomy, scoring
from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain import portfolio as assembly
from app.services.ioe.domain import relationships as relationship_rules
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.domain.integrity import (
    SUPPORTED_CANONICAL_SERIALIZATION_VERSIONS,
    DependencyUnavailable,
    IntegrityReason,
)
from app.services.ioe.frozen.models import (
    FrozenSnapshotError,
    reconstruct_tax_input,
)
from app.services.ioe.normalization.service import NORMALIZATION_VERSION
from app.services.ioe.portfolio.eligibility import ELIGIBILITY_RECHECK_VERSION
from app.services.ioe.portfolio.service import PORTFOLIO_SERVICE_VERSION
from app.services.ioe.projection import PROJECTION_METHODOLOGY_VERSION
from app.services.tax_engine.contracts import CONTRACT_VERSION
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.core.engine import TaxInput
from app.services.tax_engine.service import ENGINE_VERSION

# manifest key → (what this build implements, why a difference blocks replay)
#
# Every entry names an EXECUTABLE component: a difference means the historical
# code is not present in this process. Pure identifiers that do not change a
# computed value are deliberately absent — a renamed orchestrator cannot alter
# a digest.
EXECUTABLE_VERSIONS: dict[str, tuple[str, IntegrityReason]] = {
    "tax_engine_version": (
        ENGINE_VERSION, IntegrityReason.PINNED_ENGINE_VERSION_UNAVAILABLE),
    "engine_reference_data_version": (
        engine_data.REFERENCE_DATA_VERSION,
        IntegrityReason.REFERENCE_DATA_VERSION_UNAVAILABLE),
    "rules_evaluator_contract_version": (
        CONTRACT_VERSION, IntegrityReason.PINNED_ENGINE_CONFIG_UNAVAILABLE),
    "opportunity_normalization_version": (
        NORMALIZATION_VERSION, IntegrityReason.PINNED_ENGINE_CONFIG_UNAVAILABLE),
    "scoring_algorithm_version": (
        scoring.SCORING_ALGORITHM_VERSION, IntegrityReason.SCORING_VERSION_UNAVAILABLE),
    "confidence_algorithm_version": (
        support.CONFIDENCE_ALGORITHM_VERSION,
        IntegrityReason.SUPPORT_SCORE_VERSION_UNAVAILABLE),
    "relationship_registry_version": (
        relationship_rules.RELATIONSHIP_REGISTRY_VERSION,
        IntegrityReason.PINNED_ENGINE_CONFIG_UNAVAILABLE),
    "portfolio_service_version": (
        PORTFOLIO_SERVICE_VERSION, IntegrityReason.PINNED_ENGINE_CONFIG_UNAVAILABLE),
    "portfolio_assembly_version": (
        assembly.PORTFOLIO_ASSEMBLY_VERSION,
        IntegrityReason.PINNED_ENGINE_CONFIG_UNAVAILABLE),
    "lever_registry_version": (
        lever_registry.LEVER_REGISTRY_VERSION,
        IntegrityReason.LEVER_REGISTRY_VERSION_UNAVAILABLE),
    "assumption_registry_version": (
        assumption_registry.ASSUMPTION_REGISTRY_VERSION,
        IntegrityReason.ASSUMPTION_REGISTRY_VERSION_UNAVAILABLE),
    "cost_taxonomy_version": (
        cost_taxonomy.COST_TAXONOMY_VERSION,
        IntegrityReason.PINNED_ENGINE_CONFIG_UNAVAILABLE),
    "eligibility_recheck_version": (
        ELIGIBILITY_RECHECK_VERSION, IntegrityReason.PINNED_ENGINE_CONFIG_UNAVAILABLE),
    "portfolio_objective_version": (
        savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
        IntegrityReason.OBJECTIVE_VERSION_UNAVAILABLE),
    "projection_methodology_version": (
        PROJECTION_METHODOLOGY_VERSION, IntegrityReason.PROJECTION_VERSION_UNAVAILABLE),
}


@dataclass
class ResolvedDependencies:
    """Everything a replay is allowed to use. Nothing here is read live."""

    tax_year: int
    jurisdiction: str
    baseline_inputs: TaxInput
    baseline_input_snapshot_hash: str
    rule_snapshot_id: uuid.UUID
    rule_snapshot_hash: str
    pinned_rule_version_ids: list[uuid.UUID]
    version_manifest: dict[str, Any]
    objective_code: str
    objective_version: str
    canonical_serialization_version: str
    baseline_result_hash: str | None = None
    baseline_tax: Decimal | None = None
    checked_versions: dict[str, str] = field(default_factory=dict)


class ReplayDependencyResolver:
    """Resolves pinned dependencies under ordinary tenant RLS."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID):
        self.s = session
        self.user_id = user_id

    # ---- version gate --------------------------------------------------------
    @staticmethod
    def check_versions(manifest: dict[str, Any]) -> dict[str, str]:
        """Refuse when a pinned executable version is not the one running.

        Returns the versions that were checked and matched, so the check record
        can show what was actually held constant.
        """
        pinned_canonical = manifest.get("canonical_serialization_version")
        if pinned_canonical not in SUPPORTED_CANONICAL_SERIALIZATION_VERSIONS:
            raise DependencyUnavailable(
                IntegrityReason.CANONICAL_SERIALIZATION_VERSION_UNSUPPORTED
            )

        checked: dict[str, str] = {
            "canonical_serialization_version": str(pinned_canonical)
        }
        for key, (current, reason) in EXECUTABLE_VERSIONS.items():
            pinned = manifest.get(key)
            if pinned is None:
                # A manifest that predates a component cannot pin it. Absence is
                # not a difference, so it does not block replay; the component's
                # effect on the digest is covered by the hash comparison itself.
                continue
            if str(pinned) != str(current):
                raise DependencyUnavailable(reason)
            checked[key] = str(current)
        return checked

    # ---- data dependencies ---------------------------------------------------
    async def baseline_input(self, analysis_id: uuid.UUID) -> tuple[TaxInput, str]:
        """Rebuild the FROZEN baseline, never the user's current figures.

        This is the difference between verifying a historical result and
        recomputing a new one. `TaxEngineService.build_input` reads live
        financial tables; a replay that used it would report a mismatch every
        time a user edited last year's income, which says nothing about whether
        the sealed result was reproducible.
        """
        row = await self.s.get(AnalysisInputSnapshot, analysis_id)
        if row is None or not isinstance(row.snapshot, dict):
            raise DependencyUnavailable(IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE)
        # One codec, shared with the writer and with TX-1. A second
        # reconstruction here would be a second chance to disagree.
        try:
            return reconstruct_tax_input(row.snapshot), row.snapshot_hash
        except FrozenSnapshotError as exc:
            raise DependencyUnavailable(
                IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE) from exc

    async def rule_snapshot(
        self, *, run_id: uuid.UUID | None = None, scenario_id: uuid.UUID | None = None,
        expected_hash: str | None = None,
    ) -> tuple[uuid.UUID, str]:
        stmt = select(RunRuleSnapshot)
        stmt = (
            stmt.where(RunRuleSnapshot.run_id == run_id) if run_id is not None
            else stmt.where(RunRuleSnapshot.scenario_id == scenario_id)
        )
        pin = await self.s.scalar(stmt)
        if pin is None:
            raise DependencyUnavailable(
                IntegrityReason.PINNED_RULE_SNAPSHOT_UNAVAILABLE)

        snapshot = await self.s.get(RuleSnapshot, pin.snapshot_id)
        if snapshot is None:
            raise DependencyUnavailable(
                IntegrityReason.PINNED_RULE_SNAPSHOT_UNAVAILABLE)
        # The manifest recorded the snapshot's content hash. If the stored
        # snapshot no longer carries that identity the pin is broken, and that
        # is an unavailable dependency rather than a differing result.
        if expected_hash and snapshot.snapshot_hash != expected_hash:
            raise DependencyUnavailable(
                IntegrityReason.PINNED_RULE_SNAPSHOT_UNAVAILABLE)
        return snapshot.id, snapshot.snapshot_hash

    async def pinned_rule_versions(self, run_id: uuid.UUID) -> list[uuid.UUID]:
        rows = list(await self.s.scalars(
            select(RunRuleVersion).where(RunRuleVersion.run_id == run_id)
        ))
        return [r.tax_rule_version_id for r in rows]

    async def for_optimization(self, run) -> ResolvedDependencies:
        manifest = dict(run.version_manifest or {})
        if not manifest or not run.optimization_result_hash:
            raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)
        checked = self.check_versions(manifest)

        analysis = await self.s.get(AnalysisRun, run.analysis_id)
        if analysis is None:
            raise DependencyUnavailable(IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE)

        baseline_inputs, snapshot_hash = await self.baseline_input(run.analysis_id)
        snapshot_id, snapshot_hash_stored = await self.rule_snapshot(
            run_id=run.id, expected_hash=manifest.get("rule_snapshot_hash"),
        )
        version_ids = await self.pinned_rule_versions(run.id)

        return ResolvedDependencies(
            tax_year=run.tax_year,
            jurisdiction=analysis.province_code or "FED",
            baseline_inputs=baseline_inputs,
            baseline_input_snapshot_hash=snapshot_hash,
            rule_snapshot_id=snapshot_id,
            rule_snapshot_hash=snapshot_hash_stored,
            pinned_rule_version_ids=version_ids,
            version_manifest=manifest,
            objective_code=str(manifest.get(
                "portfolio_objective_code",
                savings_domain.PORTFOLIO_OBJECTIVE_CODE.value)),
            objective_version=str(manifest.get(
                "portfolio_objective_version",
                savings_domain.PORTFOLIO_OBJECTIVE_VERSION)),
            canonical_serialization_version=str(
                manifest.get("canonical_serialization_version",
                            c.CANONICAL_SERIALIZATION_VERSION)),
            checked_versions=checked,
        )

    async def for_scenario(self, scenario) -> ResolvedDependencies:
        manifest = dict(scenario.version_manifest or {})
        if not manifest or not scenario.scenario_result_hash:
            raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)
        checked = self.check_versions(manifest)

        if not scenario.baseline_result_hash or scenario.baseline_tax is None:
            # Without the pinned baseline RESULT a stored delta cannot be shown
            # to be a delta from anything in particular.
            raise DependencyUnavailable(IntegrityReason.BASELINE_RESULT_UNAVAILABLE)

        baseline_inputs, snapshot_hash = await self.baseline_input(
            scenario.base_analysis_id)
        if (
            scenario.baseline_input_snapshot_hash
            and scenario.baseline_input_snapshot_hash != snapshot_hash
        ):
            # The snapshot the scenario pinned is not the snapshot now stored
            # under that analysis. Replaying against a different input would
            # produce a mismatch that says nothing about the sealed result, so
            # this is an unavailable dependency instead.
            raise DependencyUnavailable(IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE)
        snapshot_id, snapshot_hash_stored = await self.rule_snapshot(
            scenario_id=scenario.id, expected_hash=manifest.get("rule_snapshot_hash"),
        )

        return ResolvedDependencies(
            tax_year=scenario.tax_year or 0,
            jurisdiction=scenario.jurisdiction or "FED",
            baseline_inputs=baseline_inputs,
            baseline_input_snapshot_hash=snapshot_hash,
            rule_snapshot_id=snapshot_id,
            rule_snapshot_hash=snapshot_hash_stored,
            pinned_rule_version_ids=[],
            version_manifest=manifest,
            objective_code=str(scenario.objective_code or ""),
            objective_version=str(scenario.objective_version or ""),
            canonical_serialization_version=str(
                manifest.get("canonical_serialization_version",
                            c.CANONICAL_SERIALIZATION_VERSION)),
            baseline_result_hash=scenario.baseline_result_hash,
            baseline_tax=scenario.baseline_tax,
            checked_versions=checked,
        )


__all__ = [
    "EXECUTABLE_VERSIONS",
    "ReplayDependencyResolver",
    "ResolvedDependencies",
]
