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

WHERE EACH SIDE'S TAX STATE COMES FROM, AND WHY THEY DIFFER
-----------------------------------------------------------
The counterfactual tax state is SEALED into the scenario artifact, because
nothing else persists it. The baseline tax state is LOADED from
`analysis.analysis_run` + `analysis.analysis_line_item`, because something
already does: `scenario.base_analysis_id` pins exactly one run, its results are
immutable once completed, and the run is the parent of the frozen baseline every
replay resolves. Two different authoritative sources, neither recomputed.

BASELINE OPPORTUNITY HAS NO SUCH SOURCE — A RECORDED BLOCKER
-------------------------------------------------------------
There is no frozen historical source for the baseline's OPPORTUNITY set, and
this module does not invent one. Measured, not assumed:

  `reco.recommendation` is written per analysis by `AnalysisService.run`, but it
  is classified LIVE_USER_DATA_DELETE / LIVE_PRODUCT_STATE — live user-facing
  product state with a mutable `status`, deleted as the SUBJECT of a deletion
  request rather than retained as evidence about one. Reading it would make a
  sealed comparison depend on state the user can change by pressing a button,
  and would carry none of the eligibility, support, required-document or
  deadline semantics the counterfactual candidate carries.

  `ioe.optimization_candidate` has those semantics, but it hangs off an
  `ioe.optimization_run` that a scenario never records. Reaching it through
  `analysis_id` would mean picking a run — a latest-or-non-superseded
  resolution that can answer differently after the scenario was sealed, which
  is the exact drift this module exists to prevent. A run may also not exist
  for the analysis at all.

So the baseline reports `OPPORTUNITY = MISSING_AUTHORITY` and says so in a
value. It does NOT report zero opportunities: a comparator handed a zero would
read every counterfactual opportunity as one the scenario created.
`assert_comparison_ready` refuses such a side outright.

SEPARATE FROM REPLAY. Integrity verification MAY execute pinned authorities —
that is how it proves a hash still reconciles. This path may not. The two have
different jobs and different permissions, and conflating them is how a read path
acquires an engine.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    RuleDeadline,
    Scenario,
    ScenarioAssumption,
    ScenarioInputChange,
    ScenarioResult,
)
from app.services.ioe.domain import canonical as c
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

#: Per-family ceiling on the baseline detail one read may load. Generous enough
#: that a real analysis is never truncated, low enough that no single request
#: reads an unbounded history — the same bound the State Graph loader applies.
MAX_BASELINE_LINE_ITEMS = 2000


class SourceAuthority(StrEnum):
    """Where a comparable family's content came from — and whether it came from
    anywhere at all.

    THE DISTINCTION THIS TYPE EXISTS FOR. A family that is authoritatively
    EMPTY and a family that was never LOADED look identical downstream: both are
    zero nodes. A comparator handed the second would report every counterpart on
    the other side as an addition the scenario caused, which is a statement
    about the user's tax position that nothing in the seal supports.

    So emptiness is only ever reported by a source that was actually consulted.
    """

    #: Loaded from a pinned, frozen source, and it holds content.
    AUTHORITATIVE = "AUTHORITATIVE"
    #: Loaded from a pinned, frozen source that genuinely holds nothing. A real
    #: answer, and safe to compare.
    AUTHORITATIVE_EMPTY = "AUTHORITATIVE_EMPTY"
    #: NO frozen source exists to load this family from on this side. Never a
    #: zero: nothing was asked, so nothing was answered.
    MISSING_AUTHORITY = "MISSING_AUTHORITY"
    #: The family does not apply to a single-scenario comparison at all.
    NOT_APPLICABLE = NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON


#: The families a comparison reads. `RESOURCE` is deliberately absent: it is not
#: a family whose authority can be missing, it is one that does not apply.
COMPARISON_REQUIRED_FAMILIES: tuple[str, ...] = (
    "FACT", "TAX_STATE", "OPPORTUNITY", "DEADLINE", "EVIDENCE_REQUIRED",
    "EVIDENCE_HELD", "ASSUMPTION", "SCENARIO",
)


def _authority(loaded: bool, *, present: bool) -> SourceAuthority:
    """`loaded` says a source was consulted; `present` says it had content."""
    if not loaded:
        return SourceAuthority.MISSING_AUTHORITY
    return (SourceAuthority.AUTHORITATIVE if present
            else SourceAuthority.AUTHORITATIVE_EMPTY)


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

    #: Per-family provenance. A count alone cannot say whether a family is
    #: empty or absent, so the answer is carried rather than inferred.
    authority: Mapping[str, SourceAuthority] = field(default_factory=dict)

    def missing_authority(self) -> tuple[str, ...]:
        """The families this side could not load from any frozen source."""
        return tuple(
            family for family in COMPARISON_REQUIRED_FAMILIES
            if self.authority.get(family) is SourceAuthority.MISSING_AUTHORITY
        )


def assert_comparison_ready(bundle: HistoricalSourceBundle) -> None:
    """THE GATE A COMPARATOR MUST PASS BEFORE CONSUMING A SIDE.

    Deliberately NOT called by the rendering path. Assembling and projecting a
    side is a read of what was sealed and stays legal with a family missing;
    COMPARING two sides is not, because a comparator cannot tell an absent
    family from an empty one and would manufacture differences out of the gap.

    Fails closed through the existing taxonomy: a family with no frozen source
    is incomplete sealed evidence, which is what `SEALED_EVIDENCE_INCOMPLETE`
    already means. It is emphatically NOT a MISMATCH — absence here is a known
    gap in what was sealed, not evidence that anything was tampered with, and
    reporting tampering for it would be an accusation the data does not support.
    """
    if bundle.missing_authority():
        raise DependencyUnavailable(IntegrityReason.SEALED_EVIDENCE_INCOMPLETE)


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

    # The BASELINE tax state, from the analysis the scenario pinned. Loaded
    # rather than sealed a second time because it is already both: pinned by
    # `scenario.base_analysis_id`, and immutable once the run completed.
    baseline_line_items, baseline_tax_state_loaded = await _baseline_line_items(
        session, user_id, scenario.base_analysis_id)

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
        *,
        tax_state_loaded: bool,
        opportunity_loaded: bool,
    ) -> HistoricalSourceBundle:
        """Both sides share every sealed field except the three that differ.

        Written as one constructor rather than two literals so a future field
        cannot be added to one side and forgotten on the other — which would
        surface later as a phantom difference in a comparison.

        `*_loaded` records whether a SOURCE was consulted, which is a different
        question from whether it returned anything. Only a source that was
        actually read may report emptiness.
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
            authority={
                "FACT": _authority(True, present=bool(snapshot_row.snapshot)),
                "TAX_STATE": _authority(
                    tax_state_loaded, present=bool(line_items)),
                "OPPORTUNITY": _authority(
                    opportunity_loaded, present=bool(side_candidates)),
                # DEADLINE and required EVIDENCE are reached THROUGH a side's
                # opportunities, so their authority cannot outrank the
                # opportunity authority they hang off. A side whose
                # opportunities were never loaded has no authoritative
                # reachability either, whatever rows happen to be in hand.
                "DEADLINE": _authority(
                    opportunity_loaded, present=bool(deadlines)),
                "EVIDENCE_REQUIRED": _authority(
                    opportunity_loaded, present=bool(required)),
                # Held evidence is the SAME sealed T1 snapshot on both sides —
                # a scenario changes what evidence is required, never what the
                # user possesses.
                "EVIDENCE_HELD": _authority(
                    True, present=bool(snapshot.document_type_codes)),
                "ASSUMPTION": _authority(True, present=bool(assumptions)),
                "SCENARIO": _authority(True, present=bool(metadata)),
                "RESOURCE": SourceAuthority.NOT_APPLICABLE,
            },
        )

    # THE BASELINE. Its tax state is LOADED from the analysis the scenario
    # pinned; its opportunities are not, because no frozen source for them
    # exists — see this module's docstring. `opportunity_loaded=False` is the
    # whole point: it makes the absence a recorded fact rather than a zero.
    baseline = _side(
        SIDE_BASELINE, (), baseline_line_items, (),
        tax_state_loaded=baseline_tax_state_loaded,
        opportunity_loaded=False,
    )
    counterfactual = _side(
        SIDE_COUNTERFACTUAL, changes,
        tuple(derived.get("line_items") or ()), candidates,
        tax_state_loaded=True,
        opportunity_loaded=True,
    )
    return baseline, counterfactual


async def _baseline_line_items(
    session: AsyncSession, user_id: uuid.UUID, analysis_id: uuid.UUID
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """The baseline tax state, from the analysis run the scenario pinned.

    WHY THIS IS A READ AND NOT A SECOND SEAL. `analysis.analysis_run` is the
    parent of the frozen baseline every replay resolves, its results are
    immutable once the run completes, and `scenario.base_analysis_id` pins
    exactly one of them. Sealing a copy into the scenario artifact would store a
    value that is already stored, immutably, one join away — and two copies of
    one truth is one more thing that can disagree.

    NOTHING IS COMPUTED. `TaxEngineService` is not called and cannot be: the
    rows were written when the analysis ran, and money passes through
    `canonical.money` exactly as the sealed counterfactual line items do, so the
    two sides render the same value the same way.

    Returns `(rows, loaded)`. `loaded` is False when the pinned analysis is gone
    or never completed — the account-deletion workflow removes this detail while
    Decision B retains the sealed scenario, so "the run had no line items" and
    "the run is no longer there" must not collapse into one answer.
    """
    analysis = await session.get(AnalysisRun, analysis_id)
    if (analysis is None or analysis.user_id != user_id
            or analysis.status != "completed"):
        return (), False

    rows = await session.scalars(
        select(AnalysisLineItem)
        .where(AnalysisLineItem.analysis_id == analysis_id)
        # Ordered by SEMANTIC identity, not by `sort_order`: presentation order
        # is a property of the run, and a historical node keyed by (kind, label)
        # must not change identity because a later run reordered its display.
        .order_by(AnalysisLineItem.kind, AnalysisLineItem.label)
        .limit(MAX_BASELINE_LINE_ITEMS)
    )
    return tuple(
        {
            "kind": row.kind,
            "label": row.label,
            "amount": c.money(row.amount),
            "fact_key": row.fact_key,
            "tax_rule_version_id": (
                str(row.tax_rule_version_id)
                if row.tax_rule_version_id is not None else None
            ),
        }
        for row in rows
    ), True


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
