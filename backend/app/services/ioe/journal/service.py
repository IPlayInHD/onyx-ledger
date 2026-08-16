"""Tax Decision Journal — persistence orchestration.

WHAT THIS SERVICE DOES: validates ownership, pins identities, appends events
under the thread's advisory lock, and reads history back. WHAT IT NEVER DOES:
compute tax, evaluate a rule, run the optimizer, resolve a current rule
version, or decide anything on the user's behalf. The one derived value it
touches — the Before-You-Act comparison hash pinned at creation — comes from
the CERTIFIED pure engine over sealed rows, and is pinned precisely so that
"what informed this decision" cannot drift with later code.

CONCURRENCY CONTRACT. Every append takes a transaction-scoped advisory lock
keyed on the thread id, assigns `max(sequence) + 1`, and inserts. Two
concurrent appends serialize on the lock (held to commit); the
`(journal_id, sequence)` unique constraint turns any lost race into an error
rather than a tie. Ordering is therefore total and deterministic. An advisory
lock rather than `FOR UPDATE`, because a row lock needs UPDATE privilege and
the application role deliberately holds none — that revocation is the
append-only guarantee.

IDEMPOTENCY CONTRACT. Every mutation carries a client-supplied `request_id`.
`(user_id, request_id)` on threads and `(journal_id, request_id)` on events
make a retry land on the row it already created, which is then returned as if
fresh — explicit identity, never fuzzy payload matching.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound, ValidationError
from app.database.models import (
    DecisionJournal,
    DecisionJournalEvent,
    Document,
    DocumentType,
    Scenario,
)
from app.schemas.before_you_act import BEFORE_YOU_ACT_SCHEMA_VERSION
from app.services.ioe.domain.integrity import DependencyUnavailable
from app.services.ioe.journal.domain import (
    JOURNAL_EVENT_SCHEMA_VERSION,
    Decision,
    JournalEventType,
)
from app.services.ioe.scenario import held_evidence
from app.services.ioe.scenario.before_you_act import comparison_side
from app.services.ioe.scenario.comparison import compare_scenario_graphs
from app.services.ioe.scenario.historical_source import load_sealed_sides

#: The earliest plausible user-reported action date. Older than any tax year
#: this product serves; a date before it is a typo, not history.
EARLIEST_REPORTABLE_ACTION = date(2000, 1, 1)


@dataclass(frozen=True)
class EvidenceContext:
    """Governed evidence semantics BESIDE the journal, never inside it.

    `sealed_readiness` is what the sealed scenario recorded — historical, and
    it will never change. `current_observed_readiness` is the same certified
    readiness primitive over the requirements the scenario pinned and the
    documents held NOW — labeled current precisely because it moves. Neither
    is proof an action occurred, and the field names say which moment each
    describes so they cannot be read as one another.
    """

    status: str  # AVAILABLE | UNAVAILABLE
    reason_code: str | None
    sealed_readiness: tuple[tuple[str, str, str], ...]
    current_observed_readiness: tuple[tuple[str, str, str], ...]
    #: The pinned deadline codes that informed the decision, from the sealed
    #: bundle. Codes, because that is what the seal authoritatively carries;
    #: CURRENT deadline state lives in the Assurance Map, deliberately apart.
    sealed_deadline_codes: tuple[str, ...]


@dataclass(frozen=True)
class LoadedJournal:
    journal: DecisionJournal
    events: tuple[DecisionJournalEvent, ...]
    evidence: EvidenceContext | None  # detail reads only; None on lists


class DecisionJournalService:
    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.s = session
        self.user_id = user_id

    # ------------------------------------------------------------- create --
    async def create(
        self,
        *,
        scenario_id: uuid.UUID,
        request_id: uuid.UUID,
        subject_opportunity_code: str | None = None,
    ) -> DecisionJournal:
        """Open a thread from a scenario the caller owns, pinning what they
        were looking at, and record the explicit CONSIDERING opening event.

        The ownership check answers identically for "not yours" and "not
        there" — the API must not confirm another tenant's scenario exists.
        """
        # The fast retry path first: this user already created a thread under
        # this request identity, so return it exactly as the first attempt did.
        existing = await self.s.scalar(
            select(DecisionJournal).where(
                DecisionJournal.user_id == self.user_id,
                DecisionJournal.request_id == request_id,
            )
        )
        if existing is not None:
            return existing

        scenario = await self.s.get(Scenario, scenario_id)
        if scenario is None or scenario.user_id != self.user_id:
            raise NotFound("Scenario not found")
        if scenario.workflow_status != "completed" or not scenario.scenario_result_hash:
            raise Conflict(
                "A decision journal records a decision about a sealed result; "
                "this scenario has none yet."
            )

        comparison_hash, comparison_version = await self._comparison_identity(
            scenario_id
        )

        journal = DecisionJournal(
            user_id=self.user_id,
            scenario_id=scenario_id,
            subject_opportunity_code=subject_opportunity_code,
            scenario_result_hash=scenario.scenario_result_hash,
            scenario_result_schema_version=scenario.result_schema_version,
            comparison_hash=comparison_hash,
            comparison_schema_version=comparison_version,
            request_id=request_id,
        )
        try:
            # A savepoint, so the race the pre-check cannot close — two
            # concurrent first attempts — fails only this insert, not the
            # caller's transaction.
            async with self.s.begin_nested():
                self.s.add(journal)
                await self.s.flush()  # the id is a server default; assign it now
                self.s.add(DecisionJournalEvent(
                    journal_id=journal.id,
                    sequence=1,
                    event_type=JournalEventType.CREATED.value,
                    decision=Decision.CONSIDERING.value,
                    event_schema_version=JOURNAL_EVENT_SCHEMA_VERSION,
                    request_id=request_id,
                ))
                await self.s.flush()
        except IntegrityError:
            raced = await self.s.scalar(
                select(DecisionJournal).where(
                    DecisionJournal.user_id == self.user_id,
                    DecisionJournal.request_id == request_id,
                )
            )
            if raced is None:  # a different constraint failed
                raise
            return raced
        return journal

    async def _comparison_identity(
        self, scenario_id: uuid.UUID
    ) -> tuple[str | None, str | None]:
        """The Before-You-Act comparison hash for this sealed scenario, from
        the certified pure engine — or nothing, honestly.

        A scenario whose seal cannot answer (v1/v2) pins NULLs rather than a
        reconstruction: the journal must never manufacture the artifact it
        claims informed the user.
        """
        try:
            baseline, counterfactual = await load_sealed_sides(
                self.s, self.user_id, scenario_id
            )
            comparison = compare_scenario_graphs(
                comparison_side(baseline, user_id=self.user_id),
                comparison_side(counterfactual, user_id=self.user_id),
            )
        except DependencyUnavailable:
            return None, None
        return comparison.comparison_hash, BEFORE_YOU_ACT_SCHEMA_VERSION

    # ------------------------------------------------------------- append --
    async def record_decision(
        self,
        journal_id: uuid.UUID,
        *,
        decision: Decision,
        request_id: uuid.UUID,
    ) -> DecisionJournalEvent:
        """Append a declaration. A change of mind is a new event; the one it
        supersedes stays untouched, which is the entire correction model."""
        return await self._append(
            journal_id,
            request_id=request_id,
            event_type=JournalEventType.DECISION_RECORDED,
            decision=decision,
            user_reported_action_date=None,
        )

    async def report_action(
        self,
        journal_id: uuid.UUID,
        *,
        request_id: uuid.UUID,
        action_date: date | None = None,
    ) -> DecisionJournalEvent:
        """Append the user's report that they acted. Recorded as their claim —
        `recorded_at` stays the server's clock, and the claimed date is only
        validated for sanity, never used to rewrite anything."""
        if action_date is not None:
            today = datetime.now(tz=UTC).date()
            if action_date > today:
                raise ValidationError(
                    "A reported action date cannot be in the future.")
            if action_date < EARLIEST_REPORTABLE_ACTION:
                raise ValidationError(
                    "A reported action date this old is outside anything this "
                    "product records.")
        return await self._append(
            journal_id,
            request_id=request_id,
            event_type=JournalEventType.ACTION_REPORTED,
            decision=None,
            user_reported_action_date=action_date,
        )

    async def _append(
        self,
        journal_id: uuid.UUID,
        *,
        request_id: uuid.UUID,
        event_type: JournalEventType,
        decision: Decision | None,
        user_reported_action_date: date | None,
    ) -> DecisionJournalEvent:
        # Ownership first, indistinguishable for absent and foreign threads.
        journal = await self.s.scalar(
            select(DecisionJournal).where(
                DecisionJournal.id == journal_id,
                DecisionJournal.user_id == self.user_id,
            )
        )
        if journal is None:
            raise NotFound("Decision journal not found")

        # Serialize appends per thread with a TRANSACTION-SCOPED ADVISORY LOCK
        # rather than `SELECT ... FOR UPDATE`: a row lock requires UPDATE
        # privilege, and the application role deliberately holds none on these
        # tables — that revocation IS the append-only guarantee. The lock is
        # held to commit, so a concurrent append waits and then reads the
        # winner's sequence; `(journal_id, sequence)` remains the backstop.
        await self.s.execute(
            sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:jid, 0))"),
            {"jid": str(journal_id)},
        )

        # Retry lands here: same request identity, same event back.
        existing = await self.s.scalar(
            select(DecisionJournalEvent).where(
                DecisionJournalEvent.journal_id == journal_id,
                DecisionJournalEvent.request_id == request_id,
            )
        )
        if existing is not None:
            return existing

        next_sequence = (
            await self.s.scalar(
                select(DecisionJournalEvent.sequence)
                .where(DecisionJournalEvent.journal_id == journal_id)
                .order_by(DecisionJournalEvent.sequence.desc())
                .limit(1)
            )
            or 0
        ) + 1

        event = DecisionJournalEvent(
            journal_id=journal_id,
            sequence=next_sequence,
            event_type=event_type.value,
            decision=decision.value if decision is not None else None,
            user_reported_action_date=user_reported_action_date,
            event_schema_version=JOURNAL_EVENT_SCHEMA_VERSION,
            request_id=request_id,
        )
        self.s.add(event)
        await self.s.flush()
        return event

    # --------------------------------------------------------------- read --
    async def detail(self, journal_id: uuid.UUID) -> LoadedJournal:
        journal = await self.s.scalar(
            select(DecisionJournal).where(
                DecisionJournal.id == journal_id,
                DecisionJournal.user_id == self.user_id,
            )
        )
        if journal is None:
            raise NotFound("Decision journal not found")
        events = tuple(await self.s.scalars(
            select(DecisionJournalEvent)
            .where(DecisionJournalEvent.journal_id == journal_id)
            .order_by(DecisionJournalEvent.sequence)
        ))
        evidence = await self._evidence_context(journal.scenario_id)
        return LoadedJournal(journal=journal, events=events, evidence=evidence)

    async def list_journals(self) -> tuple[LoadedJournal, ...]:
        """Every thread, newest first, with history loaded in ONE query — the
        projection needs events, and a per-thread query would be the N+1 this
        method exists to avoid."""
        journals = list(await self.s.scalars(
            select(DecisionJournal)
            .where(DecisionJournal.user_id == self.user_id)
            .order_by(DecisionJournal.created_at.desc(), DecisionJournal.id)
        ))
        if not journals:
            return ()
        events = await self.s.scalars(
            select(DecisionJournalEvent)
            .where(DecisionJournalEvent.journal_id.in_([j.id for j in journals]))
            .order_by(
                DecisionJournalEvent.journal_id, DecisionJournalEvent.sequence)
        )
        by_journal: dict[uuid.UUID, list[DecisionJournalEvent]] = {}
        for event in events:
            by_journal.setdefault(event.journal_id, []).append(event)
        return tuple(
            LoadedJournal(
                journal=journal,
                events=tuple(by_journal.get(journal.id, ())),
                evidence=None,
            )
            for journal in journals
        )

    async def _evidence_context(
        self, scenario_id: uuid.UUID
    ) -> EvidenceContext:
        """Governed readiness beside the journal. Sealed readiness from the
        sealed bundle; current observed readiness from the SAME certified
        primitive over the requirements the scenario pinned and the document
        types held now. Type codes only — no storage identity anywhere."""
        try:
            _, counterfactual = await load_sealed_sides(
                self.s, self.user_id, scenario_id
            )
        except DependencyUnavailable as exc:
            return EvidenceContext(
                status="UNAVAILABLE",
                reason_code=exc.reason.value,
                sealed_readiness=(),
                current_observed_readiness=(),
                sealed_deadline_codes=(),
            )

        sealed = held_evidence.historical_readiness(
            counterfactual.held_evidence, counterfactual.required_evidence)

        held_now = await self.s.scalars(
            select(DocumentType.code)
            .join(Document, Document.document_type_id == DocumentType.id)
            .where(
                Document.user_id == self.user_id,
                Document.deleted_at.is_(None),
                Document.status == "processed",
            )
        )
        current = held_evidence.historical_readiness(
            held_evidence.build_snapshot(list(held_now)),
            counterfactual.required_evidence,
        )

        def rows(
            pairs: tuple,
        ) -> tuple[tuple[str, str, str], ...]:
            return tuple(sorted(
                (r.document_type_code, r.necessity, readiness.value)
                for r, readiness in pairs
            ))

        return EvidenceContext(
            status="AVAILABLE",
            reason_code=None,
            sealed_readiness=rows(sealed),
            current_observed_readiness=rows(current),
            sealed_deadline_codes=tuple(sorted(
                code for _, code in counterfactual.deadlines)),
        )
