"""FrozenAnalysisInputService — the only door between storage and computation.

Eleven checks, in order, and every one of them fails closed. There is no path
through this service that returns a partially-trusted input, and no path that
falls back to live data: if the pinned snapshot cannot be loaded, matched,
reconstructed and reconciled, the workflow fails and no engine run begins.

Ownership is verified here as well as by RLS. RLS is the tenant-correctness
boundary; an explicit ownership check is what makes the failure a deliberate
`NotFound` rather than an empty result somebody later reads as "no data".
"""
from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.database.models import AnalysisInputSnapshot, AnalysisRun
from app.services.ioe.domain import canonical as c
from app.services.ioe.frozen.models import (
    FROZEN_INPUT_RECONSTRUCTION_FAILED,
    PINNED_BASELINE_RESULT_HASH_MISMATCH,
    PINNED_SNAPSHOT_HASH_MISMATCH,
    PINNED_SNAPSHOT_INCOMPLETE,
    PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED,
    PINNED_SNAPSHOT_UNAVAILABLE,
    SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS,
    FrozenAnalysisInput,
    FrozenSnapshotError,
    reconstruct_tax_input,
    snapshot_hash,
)
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.service import ENGINE_VERSION, TaxEngineService

MONEY = Decimal("0.01")


def baseline_result_pin(baseline_tax: Decimal) -> str:
    """Identity of the baseline RESULT, not just of the inputs.

    Pinning only the inputs would leave a stored delta unable to show what it
    was a delta from: the engine that turned those inputs into a number could
    have changed underneath it.
    """
    return c.canonical_hash({
        "baseline_tax": c.money(baseline_tax),
        "engine_version": ENGINE_VERSION,
        "reference_data_version": engine_data.REFERENCE_DATA_VERSION,
    })


class FrozenAnalysisInputService:
    """Reconstructs a `FrozenAnalysisInput` from pinned evidence, or refuses."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID):
        self.s = session
        self.user_id = user_id

    async def resolve(
        self,
        analysis_id: uuid.UUID,
        *,
        expected_snapshot_hash: str | None = None,
        expected_baseline_result_hash: str | None = None,
    ) -> FrozenAnalysisInput:
        """Load, verify and reconstruct. Fails closed at every step.

        `expected_*` are supplied when REPLAYING or resuming a pinned run: the
        hashes the run was sealed under must still be the hashes the stored
        evidence produces. They are omitted in TX-1, where this call is what
        establishes them.
        """
        # 1-3. the analysis exists, is complete, and belongs to this user
        analysis = await self.s.get(AnalysisRun, analysis_id)
        if analysis is None or analysis.user_id != self.user_id:
            # ownership checked in the application layer as well as by RLS
            raise NotFound("Analysis not found")

        # 4. the snapshot exists and belongs to that analysis (it is keyed by
        #    analysis_id, so identity and membership are the same lookup)
        row = await self.s.get(AnalysisInputSnapshot, analysis_id)
        if row is None or not isinstance(row.snapshot, dict):
            raise FrozenSnapshotError(PINNED_SNAPSHOT_UNAVAILABLE)

        payload = row.snapshot

        # 5. schema version, before anything tries to interpret the payload
        schema_version = payload.get("schema_version")
        if schema_version not in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
            raise FrozenSnapshotError(PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED)

        # 6-7. recompute the content address and compare, twice: against the
        #      snapshot's own stored hash, and against the hash the run pinned.
        actual = snapshot_hash(payload)
        if row.snapshot_hash != actual:
            raise FrozenSnapshotError(PINNED_SNAPSHOT_HASH_MISMATCH)
        if expected_snapshot_hash is not None and expected_snapshot_hash != actual:
            raise FrozenSnapshotError(PINNED_SNAPSHOT_HASH_MISMATCH)

        # 8. reconstruct — a missing field is INCOMPLETE, never a default
        tax_input = reconstruct_tax_input(payload)

        # 9. the snapshot must describe the analysis it hangs off
        tax_year = payload.get("tax_year")
        jurisdiction = payload.get("jurisdiction")
        if not isinstance(tax_year, int) or not isinstance(jurisdiction, str):
            raise FrozenSnapshotError(PINNED_SNAPSHOT_INCOMPLETE)
        if tax_year != analysis.tax_year:
            raise FrozenSnapshotError(PINNED_SNAPSHOT_INCOMPLETE)
        if tax_input.year != tax_year or tax_input.province != jurisdiction:
            raise FrozenSnapshotError(FROZEN_INPUT_RECONSTRUCTION_FAILED)

        # 10. the baseline RESULT identity, re-derived from the frozen input by
        #     the one authority allowed to calculate tax
        baseline = TaxEngineService(self.s).run(tax_input)
        baseline_tax = baseline.total_payable.quantize(MONEY, ROUND_HALF_UP)
        # RETAINED, NOT RECOMPUTED. The one permitted engine run just produced
        # these; discarding them left the baseline opportunity set with no way
        # to be evaluated except a second run that could disagree with the
        # number this scenario is about to be sealed from. `facts_for` is the
        # same pure mapping the counterfactual side uses, so the two sets are
        # evaluated from facts derived identically.
        baseline_facts = TaxEngineService.facts_for(tax_input, baseline)
        result_hash = baseline_result_pin(baseline_tax)
        if (
            expected_baseline_result_hash is not None
            and expected_baseline_result_hash != result_hash
        ):
            raise FrozenSnapshotError(PINNED_BASELINE_RESULT_HASH_MISMATCH)

        # 11. one immutable object, and no route back to mutable state
        return FrozenAnalysisInput(
            user_id=self.user_id,
            analysis_id=analysis_id,
            snapshot_id=analysis_id,          # the snapshot is keyed by analysis
            snapshot_hash=actual,
            snapshot_schema_version=str(schema_version),
            tax_year=tax_year,
            jurisdiction=jurisdiction,
            tax_input=tax_input,
            baseline_result_hash=result_hash,
            baseline_tax=baseline_tax,
            baseline_facts=baseline_facts,
        )


__all__ = ["FrozenAnalysisInputService", "baseline_result_pin"]
