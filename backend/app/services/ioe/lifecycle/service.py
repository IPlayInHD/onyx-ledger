"""Opportunity Lifecycle — the read path.

Two certified services in, one pure derivation out. It bypasses neither: the
Assurance Map and the Decision Journal each own business derivation this
module must not duplicate, so it asks them rather than querying their tables.

The only clock read is at the route boundary, where `as_of` is chosen and
injected. Both the Assurance derivation and this one receive it explicitly.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ioe.journal.domain import (
    Decision,
    JournalEventType,
    JournalEventView,
    project,
)
from app.services.ioe.journal.service import DecisionJournalService
from app.services.ioe.lifecycle.domain import (
    JournalThreadView,
    OpportunityLifecycleMap,
    derive_opportunity_lifecycle,
)
from app.services.state_graph.assurance import derive_assurance_map
from app.services.state_graph.service import TaxStateGraphService


class OpportunityLifecycleService:
    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.s = session
        self.user_id = user_id

    async def build(self, *, tax_year: int, as_of: date) -> OpportunityLifecycleMap:
        graph = await TaxStateGraphService(self.s, self.user_id).build(
            tax_year=tax_year)
        assurance = derive_assurance_map(graph, as_of=as_of)

        # Every thread with its full history, in the Journal's own two
        # queries — never one lookup per opportunity.
        loaded = await DecisionJournalService(self.s, self.user_id).list_journals()
        threads = tuple(
            JournalThreadView(
                journal_id=entry.journal.id,
                subject=entry.journal.subject_opportunity_code,
                created_at=entry.journal.created_at,
                decision=projection.current_decision,
                execution=projection.execution,
                last_reported_action_date=projection.last_reported_action_date,
            )
            for entry, projection in (
                (entry, project([
                    JournalEventView(
                        sequence=event.sequence,
                        event_type=JournalEventType(event.event_type),
                        decision=(
                            Decision(event.decision) if event.decision else None),
                        user_reported_action_date=event.user_reported_action_date,
                        recorded_at=event.recorded_at,
                    )
                    for event in entry.events
                ]))
                for entry in loaded
            )
        )

        # Pure from here: no statement is issued below this line.
        return derive_opportunity_lifecycle(assurance, threads, as_of=as_of)
