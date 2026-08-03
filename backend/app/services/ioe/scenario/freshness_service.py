"""Freshness wiring (§P6.1) — three paths, deliberately not one.

No single mechanism is sufficient, so all three run and reinforce each other:

  EVENT-DRIVEN   the moment something a scenario pinned actually moves — a new
                 analysis, a rule publication, an engine or registry version
                 change — the scenarios that pinned it are marked stale. This is
                 the fast path and it is precise.
  READ-TIME      a detail read re-evaluates the scenario it is about to return.
                 This is the *correct* path: whatever the event plumbing missed,
                 a user is never shown a result labelled `current` that is not.
  SCHEDULED      a bounded sweep re-evaluates the rest. This is the safety net
                 for events that were never emitted and scenarios nobody has
                 opened, so staleness cannot lurk indefinitely.

Relying on the sweep alone would mean a user can be shown a stale result as
current for up to one sweep interval; relying on events alone would mean any
missed event is permanent. Hence all three.

What none of them ever do is change a stored RESULT. A transition writes only
freshness columns — status, reason, evaluated-at — on the scenario header. The
numbers stay exactly as they were computed, because they remain true statements
about the baseline they were measured against.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Scenario, ScenarioEvent
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.domain.freshness import (
    FreshnessVerdict,
    PinnedState,
    evaluate_freshness,
)
from app.services.ioe.domain.scenario import FreshnessStatus, StaleReason
from app.services.ioe.snapshot.service import RuleSnapshotService
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.service import ENGINE_VERSION, TaxEngineService

FRESHNESS_SERVICE_VERSION = "1.0.0"

# A sweep is bounded so it cannot become an unbounded table scan; the remainder
# is picked up by the next run.
SWEEP_BATCH_SIZE = 200


@dataclass(frozen=True)
class FreshnessTransition:
    scenario_id: uuid.UUID
    previous_status: str
    new_status: str
    stale_reason_code: str | None
    changed: bool


class ScenarioFreshnessService:
    """Evaluates and records freshness. Never touches a stored result."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID | None = None):
        self.s = session
        self.user_id = user_id

    # ------------------------------------------------------------ evaluate ---
    async def current_state(self, scenario: Scenario) -> PinnedState:
        """What the pinned inputs look like NOW, for comparison against the pins."""
        baseline_input_hash = scenario.baseline_input_snapshot_hash
        baseline_result_hash = scenario.baseline_result_hash

        if scenario.base_analysis_id is not None and scenario.tax_year is not None:
            from app.database.models import AnalysisInputSnapshot

            snapshot = await self.s.get(AnalysisInputSnapshot, scenario.base_analysis_id)
            if snapshot is not None:
                baseline_input_hash = snapshot.snapshot_hash

            owner = scenario.user_id
            engine = TaxEngineService(self.s)
            try:
                current_input = await engine.build_input(owner, scenario.tax_year)
                current_tax = engine.run(current_input).total_payable
                baseline_result_hash = c.canonical_hash({
                    "baseline_tax": c.money(current_tax),
                    "engine_version": ENGINE_VERSION,
                    "reference_data_version": engine_data.REFERENCE_DATA_VERSION,
                })
            except Exception:  # noqa: BLE001
                # An engine failure must not silently produce "current".
                baseline_result_hash = None

        snapshot_hash = None
        if scenario.tax_year is not None:
            live = await RuleSnapshotService(self.s).capture(scenario.tax_year)
            snapshot_hash = live.snapshot_hash

        return PinnedState(
            baseline_input_snapshot_hash=baseline_input_hash,
            baseline_result_hash=baseline_result_hash,
            rule_snapshot_hash=snapshot_hash,
            engine_version=ENGINE_VERSION,
            reference_data_version=engine_data.REFERENCE_DATA_VERSION,
            lever_registry_version=lever_registry.LEVER_REGISTRY_VERSION,
            objective_code=savings_domain.PORTFOLIO_OBJECTIVE_CODE.value,
            objective_version=savings_domain.PORTFOLIO_OBJECTIVE_VERSION,
            tax_year=scenario.tax_year,
        )

    @staticmethod
    def pinned_state(scenario: Scenario) -> PinnedState:
        manifest = scenario.version_manifest or {}
        return PinnedState(
            baseline_input_snapshot_hash=scenario.baseline_input_snapshot_hash,
            baseline_result_hash=scenario.baseline_result_hash,
            rule_snapshot_hash=manifest.get("rule_snapshot_hash"),
            engine_version=manifest.get("tax_engine_version"),
            reference_data_version=manifest.get("engine_reference_data_version"),
            lever_registry_version=scenario.lever_registry_version,
            objective_code=scenario.objective_code,
            objective_version=scenario.objective_version,
            tax_year=scenario.tax_year,
        )

    async def evaluate(self, scenario: Scenario) -> FreshnessVerdict:
        """Compare the pins against the world. Pure read; writes nothing."""
        if scenario.superseded_by_scenario_id is not None:
            return FreshnessVerdict(
                FreshnessStatus.SUPERSEDED, StaleReason.SUPERSEDED_BY_REFRESH
            )
        if scenario.workflow_status != "completed":
            return FreshnessVerdict(FreshnessStatus.UNKNOWN)
        current = await self.current_state(scenario)
        return evaluate_freshness(self.pinned_state(scenario), current)

    # -------------------------------------------------------------- record ---
    async def apply(
        self, scenario: Scenario, verdict: FreshnessVerdict
    ) -> FreshnessTransition:
        """Persist a CONTROLLED transition.

        Only freshness columns move. The result rows are not touched, and the
        transition is refused in the one direction that would be a lie: a stale
        or superseded scenario never silently becomes `current` again, because
        the only honest way back is a refresh, which creates a new scenario.
        """
        previous = scenario.freshness_status
        new_status = verdict.status.value

        going_backwards = (
            previous in (FreshnessStatus.STALE.value, FreshnessStatus.SUPERSEDED.value)
            and new_status == FreshnessStatus.CURRENT.value
        )
        if going_backwards or previous == new_status:
            scenario.freshness_evaluated_at = datetime.now(tz=UTC)
            return FreshnessTransition(
                scenario.id, previous, previous, scenario.stale_reason_code, False
            )

        scenario.freshness_status = new_status
        scenario.stale_reason_code = (
            verdict.stale_reason.value if verdict.stale_reason else None
        )
        scenario.freshness_evaluated_at = datetime.now(tz=UTC)
        self.s.add(ScenarioEvent(
            scenario_id=scenario.id,
            from_status=scenario.workflow_status,
            to_status=scenario.workflow_status,
            reason_code=f"FRESHNESS_{new_status.upper()}",
        ))
        return FreshnessTransition(
            scenario.id, previous, new_status, scenario.stale_reason_code, True
        )

    async def evaluate_and_record(self, scenario: Scenario) -> FreshnessTransition:
        """The READ-TIME path: used by detail reads so nothing stale is ever
        returned labelled `current`."""
        verdict = await self.evaluate(scenario)
        return await self.apply(scenario, verdict)

    # ------------------------------------------------------- event-driven ---
    async def invalidate_for_analysis(
        self, analysis_id: uuid.UUID, reason: StaleReason
    ) -> int:
        """EVENT path: a baseline moved, so every scenario on it is stale.

        A single qualified UPDATE keyed on `base_analysis_id`, which is indexed.
        No result row is touched.
        """
        result = await self.s.execute(
            update(Scenario)
            .where(
                Scenario.base_analysis_id == analysis_id,
                Scenario.workflow_status == "completed",
                Scenario.freshness_status == FreshnessStatus.CURRENT.value,
            )
            .values(
                freshness_status=FreshnessStatus.STALE.value,
                stale_reason_code=reason.value,
                freshness_evaluated_at=datetime.now(tz=UTC),
            )
        )
        return result.rowcount or 0

    async def invalidate_for_tax_year(
        self, tax_year: int, reason: StaleReason
    ) -> int:
        """EVENT path: a rule publication or reference-data change for a year."""
        result = await self.s.execute(
            update(Scenario)
            .where(
                Scenario.tax_year == tax_year,
                Scenario.workflow_status == "completed",
                Scenario.freshness_status == FreshnessStatus.CURRENT.value,
            )
            .values(
                freshness_status=FreshnessStatus.STALE.value,
                stale_reason_code=reason.value,
                freshness_evaluated_at=datetime.now(tz=UTC),
            )
        )
        return result.rowcount or 0

    # ----------------------------------------------------------- scheduled ---
    async def sweep(self, *, limit: int = SWEEP_BATCH_SIZE) -> list[FreshnessTransition]:
        """SCHEDULED path: re-evaluate the least-recently-checked scenarios.

        Bounded, and ordered by `freshness_evaluated_at` so nothing can be
        starved. This is a fallback for events that were never emitted and
        scenarios nobody has opened — not the primary mechanism.
        """
        rows = list(await self.s.scalars(
            select(Scenario)
            .where(
                Scenario.workflow_status == "completed",
                Scenario.visibility_status == "active",
                Scenario.freshness_status == FreshnessStatus.CURRENT.value,
            )
            .order_by(Scenario.freshness_evaluated_at.asc().nullsfirst())
            .limit(limit)
        ))
        transitions: list[FreshnessTransition] = []
        for scenario in rows:
            transitions.append(await self.evaluate_and_record(scenario))
        return transitions


__all__ = [
    "FRESHNESS_SERVICE_VERSION",
    "SWEEP_BATCH_SIZE",
    "FreshnessTransition",
    "ScenarioFreshnessService",
]
