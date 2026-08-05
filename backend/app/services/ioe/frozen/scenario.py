"""FrozenScenarioInputService — the scenario door onto item 3A's reconstruction.

This is deliberately a *composition* of `FrozenAnalysisInputService`, not a
second snapshot system. The scenario workflow needs exactly the same eleven
fail-closed checks over exactly the same codec; what it adds is the set of
registry and objective versions a scenario is measured under, and a reason-code
vocabulary that names the workflow that refused.

Two reconstruction implementations would be two chances to disagree about what a
baseline is, and the disagreement would surface as an unexplainable replay
mismatch months later.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ioe.frozen.models import (
    FrozenScenarioExecutionInput,
    FrozenSnapshotError,
    as_scenario_failure,
)
from app.services.ioe.frozen.service import FrozenAnalysisInputService


class FrozenScenarioInputService:
    """Resolves the immutable input a scenario computation is confined to."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID):
        self.s = session
        self.user_id = user_id

    async def resolve(
        self,
        analysis_id: uuid.UUID,
        *,
        objective_code: str,
        objective_version: str,
        lever_registry_version: str,
        assumption_registry_version: str,
        support_score_version: str,
        scenario_id: uuid.UUID | None = None,
        scenario_spec_hash: str = "",
        expected_snapshot_hash: str | None = None,
        expected_baseline_result_hash: str | None = None,
    ) -> FrozenScenarioExecutionInput:
        """Reconstruct the pinned baseline, or refuse.

        `expected_*` are supplied when REPLAYING a sealed scenario: the snapshot
        hash and the baseline-result identity the scenario was sealed under must
        still be what the stored evidence produces. They are omitted at creation
        time, where this call is what establishes them.

        Failures are translated, not wrapped: the caller receives a scenario
        reason code carrying no payload, so nothing derived from the user's
        financial data can travel with the error into a log or an event.
        """
        try:
            frozen = await FrozenAnalysisInputService(self.s, self.user_id).resolve(
                analysis_id,
                expected_snapshot_hash=expected_snapshot_hash,
                expected_baseline_result_hash=expected_baseline_result_hash,
            )
        except FrozenSnapshotError as exc:
            raise as_scenario_failure(exc) from None

        return FrozenScenarioExecutionInput(
            frozen_analysis_input=frozen,
            scenario_id=scenario_id,
            scenario_spec_hash=scenario_spec_hash,
            objective_code=objective_code,
            objective_version=objective_version,
            lever_registry_version=lever_registry_version,
            assumption_registry_version=assumption_registry_version,
            support_score_version=support_score_version,
        )


__all__ = ["FrozenScenarioInputService"]
