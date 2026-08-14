"""Historical source bundles for a sealed scenario (Entry 12B1 §16).

THE RULE THIS MODULE EXISTS TO ENFORCE. A customer reading a sealed scenario
gets what was sealed — never a recomputation. So nothing here runs the tax
engine, the rules evaluator or the optimizer, and nothing reads
`docs.document`. Every field is loaded from a row that was written when the
scenario was sealed and has been immutable since.

That is not a style preference. Re-deriving any of it would answer a question
about MARCH using JUNE's inputs: today's published rules, today's document
library, today's financial figures. The user would see a historical comparison
quietly rewrite itself, and nothing in the artifact would record that it had.

WHY THERE ARE TWO BUNDLES AND NOT TWO IMPLEMENTATIONS. Baseline and
counterfactual differ in exactly three families — FACT, TAX_STATE, OPPORTUNITY —
and agree on everything else, because a scenario changes what would be true, not
what the user has. Held evidence is therefore the SAME object on both sides:
that is what makes a `READY → MISSING` transition mean "this scenario needs a
document you do not have" instead of "your library changed".

SEPARATE FROM REPLAY. Integrity verification MAY execute pinned authorities —
that is how it proves a hash still reconciles. This path may not. The two have
different jobs and different permissions, and conflating them is how a read path
acquires an engine.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AnalysisInputSnapshot,
    RuleDeadline,
    Scenario,
    ScenarioAssumption,
    ScenarioInputChange,
    ScenarioResult,
)
from app.services.ioe.domain.integrity import (
    DependencyUnavailable,
    IntegrityReason,
)
from app.services.ioe.domain.scenario import SCENARIO_RESULT_SCHEMA_V2
from app.services.ioe.scenario import held_evidence
from app.services.ioe.scenario.held_evidence import (
    HistoricalHeldEvidenceSnapshot,
)
from app.services.state_graph.readiness import DocumentRequirement

HISTORICAL_SOURCE_BUNDLE_VERSION = "1.0.0"

#: A single scenario has no portfolio, so there is no resource ledger to compare
#: and RESOURCE is not "empty" — it does not apply. Stated as a value so a later
#: comparison renders it as inapplicable rather than reporting every baseline
#: resource as REMOVED.
NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON = (
    "NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON")

#: Which side of the comparison a bundle describes.
SIDE_BASELINE = "FROZEN_BASELINE"
SIDE_COUNTERFACTUAL = "SEALED_COUNTERFACTUAL"


@dataclass(frozen=True)
class HistoricalSourceBundle:
    """Everything one side of a comparison needs, all of it already sealed."""

    side: str
    scenario_id: uuid.UUID
    tax_year: int

    #: FACT — the frozen analysis input snapshot. For the counterfactual side,
    #: the same frozen baseline plus the sealed governed lever changes; the
    #: scenario's own input deltas, never a live financial row.
    facts: dict[str, Any]
    applied_changes: tuple[dict[str, Any], ...]

    #: TAX_STATE — sealed line items. Baseline uses the analysis line items the
    #: State Graph already treats as authoritative; the counterfactual uses the
    #: `TaxResult.line_items` retained at seal time.
    line_items: tuple[dict[str, Any], ...]

    #: OPPORTUNITY — sealed normalized candidates.
    candidates: tuple[dict[str, Any], ...]

    #: DEADLINE — resolved through each candidate's PINNED rule version.
    deadlines: tuple[tuple[str, str], ...]

    #: EVIDENCE — required comes from sealed candidate semantics, held from the
    #: sealed T1 snapshot. Readiness is derived, never stored.
    required_evidence: tuple[DocumentRequirement, ...]
    held_evidence: HistoricalHeldEvidenceSnapshot

    #: ASSUMPTION / SCENARIO — sealed rows.
    assumptions: tuple[dict[str, Any], ...]
    scenario_metadata: dict[str, Any]

    resource: str = NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON
    bundle_version: str = HISTORICAL_SOURCE_BUNDLE_VERSION


async def load_sealed_sides(
    session: AsyncSession, user_id: uuid.UUID, scenario_id: uuid.UUID
) -> tuple[HistoricalSourceBundle, HistoricalSourceBundle]:
    """Both sides of one sealed v2 scenario, in one pass over the sealed rows.

    Loaded together deliberately: the two sides must agree on held evidence,
    assumptions and scenario metadata, and loading them separately would leave
    two chances to read a different row.

    Refuses anything it cannot answer from the seal. A v1 scenario has no
    counterfactual state at all, and a v2 scenario whose evidence was purged by
    the account-deletion workflow has none any more — both are
    `SEALED_EVIDENCE_INCOMPLETE`, never a reconstruction from live sources.
    """
    scenario = await session.get(Scenario, scenario_id)
    if scenario is None or scenario.user_id != user_id:
        raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)
    if scenario.result_schema_version != SCENARIO_RESULT_SCHEMA_V2:
        # Not a defect and not a fallback: a v1 seal never carried the
        # counterfactual state, so there is nothing historical to read.
        raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)

    result = await session.scalar(
        select(ScenarioResult).where(ScenarioResult.scenario_id == scenario_id))
    if result is None or result.counterfactual_derived_state is None:
        raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)
    derived = result.counterfactual_derived_state

    snapshot_row = await session.scalar(
        select(AnalysisInputSnapshot).where(
            AnalysisInputSnapshot.analysis_id == scenario.base_analysis_id))
    if snapshot_row is None:
        raise DependencyUnavailable(
            IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE)
    if (scenario.baseline_input_snapshot_hash
            and scenario.baseline_input_snapshot_hash != snapshot_row.snapshot_hash):
        # The snapshot under this analysis is not the one the scenario pinned.
        # Reading it would describe a baseline this scenario never had.
        raise DependencyUnavailable(
            IntegrityReason.BASELINE_SNAPSHOT_UNAVAILABLE)

    changes = tuple(
        {"lever_code": row.lever_code, "field": row.field,
         "old_value": row.old_value, "new_value": row.new_value,
         "apply_order": row.apply_order}
        for row in await session.scalars(
            select(ScenarioInputChange)
            .where(ScenarioInputChange.scenario_id == scenario_id)
            .order_by(ScenarioInputChange.apply_order))
    )
    assumptions = tuple(
        {"assumption_code": row.assumption_code, "materiality": row.materiality,
         "source": row.source, "certainty": row.certainty,
         "affects_eligibility": row.affects_eligibility}
        for row in await session.scalars(
            select(ScenarioAssumption)
            .where(ScenarioAssumption.scenario_id == scenario_id)
            .order_by(ScenarioAssumption.assumption_code))
    )

    candidates = tuple(derived.get("candidates") or ())
    snapshot = held_evidence.from_payload(
        derived.get("baseline_held_evidence") or {})
    deadlines = await _pinned_deadlines(session, candidates)
    required = _required_evidence(candidates)

    metadata = {
        "scenario_id": str(scenario_id),
        "tax_year": scenario.tax_year,
        "jurisdiction": scenario.jurisdiction,
        "scenario_spec_hash": scenario.scenario_spec_hash,
        "scenario_result_hash": scenario.scenario_result_hash,
        "result_schema_version": scenario.result_schema_version,
        "objective_code": scenario.objective_code,
        "objective_version": scenario.objective_version,
    }

    def _side(
        side: str,
        applied_changes: tuple[dict[str, Any], ...],
        line_items: tuple[dict[str, Any], ...],
        side_candidates: tuple[dict[str, Any], ...],
    ) -> HistoricalSourceBundle:
        """Both sides share every sealed field except the three that differ.

        Written as one constructor rather than two literals so a future field
        cannot be added to one side and forgotten on the other — which would
        surface later as a phantom difference in a comparison.
        """
        return HistoricalSourceBundle(
            side=side,
            scenario_id=scenario_id,
            tax_year=scenario.tax_year or 0,
            facts=dict(snapshot_row.snapshot or {}),
            applied_changes=applied_changes,
            line_items=line_items,
            candidates=side_candidates,
            deadlines=deadlines,
            required_evidence=required,
            held_evidence=snapshot,
            assumptions=assumptions,
            scenario_metadata=metadata,
        )

    # The baseline is the frozen snapshot with NO lever applied; its tax state
    # and opportunities are not re-derived here.
    baseline = _side(SIDE_BASELINE, (), (), ())
    counterfactual = _side(
        SIDE_COUNTERFACTUAL, changes,
        tuple(derived.get("line_items") or ()), candidates)
    return baseline, counterfactual


async def _pinned_deadlines(
    session: AsyncSession, candidates: tuple[dict[str, Any], ...]
) -> tuple[tuple[str, str], ...]:
    """Deadlines through each candidate's PINNED rule version.

    `rules.rule_deadline` rows are keyed by `rule_version_id` and are never
    deleted, so a superseded version's deadlines stay reachable — which is the
    whole reason a sealed scenario can still answer "what was the deadline"
    after the rule moved on. No status filter and no latest-version lookup: the
    pin is the authority.
    """
    version_ids = [
        uuid.UUID(str(c["rule_version_id"]))
        for c in candidates if c.get("rule_version_id")
    ]
    if not version_ids:
        return ()
    rows = await session.execute(
        select(RuleDeadline.rule_version_id, RuleDeadline.deadline_code)
        .where(RuleDeadline.rule_version_id.in_(version_ids))
    )
    return tuple(sorted(
        (str(version_id), code) for version_id, code in rows.all()))


def _required_evidence(
    candidates: tuple[dict[str, Any], ...],
) -> tuple[DocumentRequirement, ...]:
    """Required documents, read out of the sealed candidate semantics.

    The rules layer decided these when the scenario was sealed; this reshapes
    them into the type the certified readiness function already consumes, so
    historical readiness runs through exactly one implementation.
    """
    out: list[DocumentRequirement] = []
    for candidate in candidates:
        version_id = str(candidate.get("rule_version_id") or "")
        for entry in candidate.get("required_documents") or ():
            type_code, necessity = entry[0], entry[1]
            out.append(DocumentRequirement(
                rule_version_id=version_id,
                document_type_code=type_code,
                necessity=necessity,
            ))
    return tuple(sorted(
        out, key=lambda r: (r.rule_version_id, r.document_type_code, r.necessity)))


def readiness_for_bundle(
    bundle: HistoricalSourceBundle,
) -> tuple[tuple[DocumentRequirement, Any], ...]:
    """Historical readiness for one side. Pure — the same certified function the
    live graph uses, over two sealed inputs."""
    return held_evidence.historical_readiness(
        bundle.held_evidence, bundle.required_evidence)
