"""The Before-You-Act read path (Entry 12B): sealed rows in, certified
comparison out.

Thin by design, and the order is the whole point:

  1. OWNERSHIP, through the read repository's own check — which answers
     identically for "not yours" and "not there", so the API never confirms
     another user's scenario exists.
  2. LOAD both sealed sides in one pass (§16). Sealed rows only.
  3. ASSEMBLE each side's historical graph, then PROJECT it (§17).
  4. COMPARE, through the certified engine (Entry 12B).

Nothing here computes tax, evaluates a rule, resolves a current rule version,
reads a document, or asks an LLM anything. Every figure it returns was
determined by a governed authority at seal time. This module's entire job is to
put four certified steps in the right order and translate one exception.

WHY THERE IS NO FALLBACK. A scenario whose seal cannot answer for a comparison
family is refused, not approximated. The alternative — comparing what loaded and
staying quiet about what did not — would report every counterpart on the other
side as something the scenario caused, which is a statement about a person's tax
position that nothing in the seal supports.

NOT TO BE CONFUSED WITH `comparison_service.py`, which compares two DIFFERENT
scenarios against each other. This compares the two sides of ONE scenario.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict
from app.database.models import Scenario
from app.services.ioe.domain.integrity import DependencyUnavailable, IntegrityReason
from app.services.ioe.read_repository import IoeReadRepository
from app.services.ioe.scenario.comparison import (
    ComparisonSide,
    TaxStateComparison,
    compare_scenario_graphs,
)
from app.services.ioe.scenario.historical_graph import assemble_historical_graph
from app.services.ioe.scenario.historical_source import (
    HistoricalSourceBundle,
    load_sealed_sides,
)
from app.services.state_graph.scenario_projection import (
    project_scenario_comparable_graph,
)


class ComparisonUnavailable(Conflict):
    """The scenario exists and is yours, but its seal cannot answer.

    A `Conflict` rather than a `NotFound`, because the distinction is real and
    safe to make: the caller already proved they own this scenario, so telling
    them *why* it cannot be compared reveals nothing about anyone else. Using
    404 here would also be a lie — the scenario is there.

    Carries a closed reason code, following `AdmissionRejected`'s precedent: the
    caller learns which governed condition applies, never a message, a stack, or
    anything about storage.
    """

    error_type = "https://onyx.ledger/errors/comparison-unavailable"
    title = "Comparison Unavailable"

    def __init__(self, reason: IntegrityReason) -> None:
        self.reason = reason
        super().__init__(
            "This scenario's sealed evidence cannot answer for a comparison."
        )


@dataclass(frozen=True)
class LoadedComparison:
    """The certified comparison plus the scenario it belongs to.

    They travel together because a comparison alone cannot be titled or dated,
    and re-reading the scenario in the serializer would be a second query for a
    row this path already holds.
    """

    scenario: Scenario
    comparison: TaxStateComparison


def _side(bundle: HistoricalSourceBundle, *, user_id: uuid.UUID) -> ComparisonSide:
    """Assemble and project one loaded side. Pure — no session, no I/O."""
    return ComparisonSide(
        graph=project_scenario_comparable_graph(
            assemble_historical_graph(bundle, user_id=user_id)
        ),
        authority=bundle.authority,
    )


class BeforeYouActService:
    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.s = session
        self.user_id = user_id

    async def comparison_for(self, scenario_id: uuid.UUID) -> LoadedComparison:
        """Sealed rows in, certified comparison out.

        The ownership check runs FIRST and on its own. `load_sealed_sides` would
        also refuse a scenario belonging to someone else, but it refuses with
        `SEALED_EVIDENCE_INCOMPLETE` — which would tell a prober that the id
        exists while an unrelated id gets a 404. Checking ownership first keeps
        both inaccessible cases identical and leaves the seal's verdict to mean
        only what it says.
        """
        scenario = await IoeReadRepository(self.s, self.user_id).get_scenario(
            scenario_id
        )

        # BOTH governed refusals are translated, because there are two of them
        # and they are easy to mistake for one. The source layer refuses what it
        # cannot load; the engine's authority gate refuses what loaded but cannot
        # answer for a comparison-required family. A sealed v2 scenario takes the
        # SECOND path — it is derived-state-bearing, so its sides load cleanly and
        # only the missing baseline OPPORTUNITY authority stops it. Wrapping only
        # the load would let that case reach the framework as a 500 with a stack
        # trace, which is the one thing the error contract must never do.
        try:
            baseline, counterfactual = await load_sealed_sides(
                self.s, self.user_id, scenario_id
            )
            # Pure from here on: no statement is issued below this line.
            comparison = compare_scenario_graphs(
                _side(baseline, user_id=self.user_id),
                _side(counterfactual, user_id=self.user_id),
            )
        except DependencyUnavailable as exc:
            raise ComparisonUnavailable(exc.reason) from exc

        return LoadedComparison(scenario=scenario, comparison=comparison)
