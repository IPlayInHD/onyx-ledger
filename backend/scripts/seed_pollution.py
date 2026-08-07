"""Add the kinds of accumulated state a long-lived database really has.

Running the suite once already leaves history behind. This adds the shapes that
history does NOT reliably produce, and that the freshness/scheduler/replay paths
are most exposed to:

  * several tenants, so "the user" is never the only user;
  * many distinct stale reason codes, so a consumer that keyed off whichever
    reason it saw first is caught;
  * events across several tax years and jurisdictions, so a fan-out that ignores
    its qualifier over-stales visibly;
  * outbox rows already in every claim state, so a scheduler that assumes it is
    looking at a fresh queue has to prove it.

Everything written here is identifiers and enumerated codes. No financial value,
no personal narrative, nothing that would be sensitive if it leaked into a log —
the same rule the production producers follow.

Used by scripts/pollution_regression.sh; not imported by application code.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select, text

from app.database.models import UserAccount
from app.database.session import unit_of_work
from app.services.ioe.domain.scenario import StaleReason
from app.services.ioe.freshness_events import FreshnessEvent, emit

TENANTS = 6
YEARS = (2024, 2025)
JURISDICTIONS = ("ON", "BC", "AB", "QC", None)

# One event per stale reason the producers can emit, so no reason code is the
# only one present or absent.
EVENTS = [
    FreshnessEvent.ANALYSIS_COMPLETED,
    FreshnessEvent.ANALYSIS_SUPERSEDED,
    FreshnessEvent.BASELINE_INPUTS_CHANGED,
    FreshnessEvent.FINANCIAL_DATA_CHANGED,
    FreshnessEvent.PROFILE_CHANGED,
    FreshnessEvent.DOCUMENT_STATUS_CHANGED,
    FreshnessEvent.RULE_PUBLISHED,
    FreshnessEvent.RULE_WITHDRAWN,
    FreshnessEvent.RULE_SUPERSEDED,
    FreshnessEvent.REFERENCE_DATA_CHANGED,
    FreshnessEvent.ENGINE_VERSION_CHANGED,
    FreshnessEvent.OBJECTIVE_POLICY_CHANGED,
    FreshnessEvent.LEVER_REGISTRY_CHANGED,
    FreshnessEvent.ASSUMPTION_REGISTRY_CHANGED,
    FreshnessEvent.RELATIONSHIP_REGISTRY_CHANGED,
    FreshnessEvent.SUPPORT_SCORE_POLICY_CHANGED,
    FreshnessEvent.PROJECTION_METHODOLOGY_CHANGED,
]


async def _tenants() -> list[uuid.UUID]:
    """Reuse whatever users the suite left behind, topping up to TENANTS."""
    async with unit_of_work(actor_type="admin") as session:
        existing = list(await session.scalars(select(UserAccount.id).limit(TENANTS)))
        made = []
        for _ in range(TENANTS - len(existing)):
            user = UserAccount(
                email=f"pollution+{uuid.uuid4().hex[:10]}@example.invalid",
            )
            session.add(user)
            await session.flush()
            made.append(user.id)
        return [*existing, *made]


async def _events(tenants: list[uuid.UUID]) -> int:
    written = 0
    async with unit_of_work(actor_type="admin") as session:
        for index, event in enumerate(EVENTS):
            for year in YEARS:
                jurisdiction = JURISDICTIONS[index % len(JURISDICTIONS)]
                user_id = tenants[index % len(tenants)]
                # A unique dedupe key per row: the point is volume and variety,
                # not to exercise the collision path (the suite already does).
                ok = await emit(
                    session, event,
                    user_id=user_id if event.value.startswith(("analysis", "baseline",
                                                               "financial", "profile",
                                                               "document")) else None,
                    tax_year=year,
                    jurisdiction=jurisdiction,
                    dedupe_key=f"pollution:{event.value}:{year}:{jurisdiction}:{uuid.uuid4().hex[:8]}",
                )
                written += int(ok)
    return written


async def _spread_claim_states() -> None:
    """Leave rows in every claim state, not just `pending`.

    A scheduler or relay that implicitly assumed an empty queue passes on a
    fresh database and fails here.
    """
    async with unit_of_work(actor_type="admin") as session:
        await session.execute(text("""
            WITH ranked AS (
              SELECT id, row_number() OVER (ORDER BY created_at) AS rn
              FROM ioe.freshness_outbox
              WHERE dedupe_key LIKE 'pollution:%'
            )
            UPDATE ioe.freshness_outbox o
               SET claim_state = CASE ranked.rn % 4
                                   WHEN 1 THEN 'completed'
                                   WHEN 2 THEN 'failed'
                                   ELSE o.claim_state
                                 END,
                   attempts    = CASE ranked.rn % 4 WHEN 2 THEN 3 ELSE o.attempts END,
                   last_error_code = CASE ranked.rn % 4 WHEN 2 THEN 'SEEDED_TERMINAL' END,
                   processed_at = CASE WHEN ranked.rn % 4 IN (1, 2) THEN now() END
              FROM ranked
             WHERE o.id = ranked.id
        """))


async def main() -> None:
    tenants = await _tenants()
    written = await _events(tenants)
    await _spread_claim_states()

    async with unit_of_work(actor_type="admin") as session:
        reasons = await session.scalar(
            text("SELECT count(DISTINCT stale_reason_code) FROM ioe.freshness_outbox"))
        rows = await session.scalar(text("SELECT count(*) FROM ioe.freshness_outbox"))

    print(f"   tenants: {len(tenants)}   events written: {written}   "
          f"outbox rows: {rows}   distinct stale reasons: {reasons}")
    print(f"   stale reasons defined by the domain: {len(list(StaleReason))}")

    from app.database.session import engine
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
