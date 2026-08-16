"""Decision Journal → product contract. Pure: no session, no computation.

The projection is derived here from the loaded history via the certified pure
fold — never read from a stored status column, because there isn't one: the
events are the record, and this layer just says so in Pydantic.
"""
from __future__ import annotations

from app.schemas.decision_journal import (
    DECISION_JOURNAL_SCHEMA_VERSION,
    DecisionJournalDetailOut,
    DecisionJournalEventOut,
    DecisionJournalSummaryOut,
    EvidenceContextOut,
    EvidenceReadinessRowOut,
    ScenarioReferenceOut,
)
from app.services.ioe.journal.domain import (
    Decision,
    JournalEventType,
    JournalEventView,
    project,
)
from app.services.ioe.journal.service import EvidenceContext, LoadedJournal


def _views(loaded: LoadedJournal) -> tuple[JournalEventView, ...]:
    return tuple(
        JournalEventView(
            sequence=event.sequence,
            event_type=JournalEventType(event.event_type),
            decision=Decision(event.decision) if event.decision else None,
            user_reported_action_date=event.user_reported_action_date,
            recorded_at=event.recorded_at,
        )
        for event in loaded.events
    )


def _summary_fields(loaded: LoadedJournal) -> dict:
    journal = loaded.journal
    projection = project(_views(loaded))
    return {
        "id": journal.id,
        "schema_version": DECISION_JOURNAL_SCHEMA_VERSION,
        "subject_opportunity_code": journal.subject_opportunity_code,
        "scenario_reference": ScenarioReferenceOut(
            scenario_id=journal.scenario_id,
            scenario_result_hash=journal.scenario_result_hash,
            scenario_result_schema_version=journal.scenario_result_schema_version,
            comparison_hash=journal.comparison_hash,
            comparison_schema_version=journal.comparison_schema_version,
        ),
        "current_decision": projection.current_decision.value,
        "decision_recorded_at": projection.decision_recorded_at,
        "execution_state": projection.execution.value,
        "last_reported_action_date": projection.last_reported_action_date,
        "event_count": projection.event_count,
        "created_at": journal.created_at,
        "last_event_at": projection.last_event_at,
    }


def _evidence_out(evidence: EvidenceContext) -> EvidenceContextOut:
    def rows(entries: tuple) -> list[EvidenceReadinessRowOut]:
        return [
            EvidenceReadinessRowOut(
                document_type_code=code, necessity=necessity, readiness=readiness)
            for code, necessity, readiness in entries
        ]

    return EvidenceContextOut(
        status=evidence.status,
        reason_code=evidence.reason_code,
        sealed_readiness=rows(evidence.sealed_readiness),
        current_observed_readiness=rows(evidence.current_observed_readiness),
        sealed_deadline_codes=list(evidence.sealed_deadline_codes),
    )


def journal_summary(loaded: LoadedJournal) -> DecisionJournalSummaryOut:
    return DecisionJournalSummaryOut(**_summary_fields(loaded))


def journal_detail(loaded: LoadedJournal) -> DecisionJournalDetailOut:
    assert loaded.evidence is not None, "detail requires the evidence context"
    return DecisionJournalDetailOut(
        **_summary_fields(loaded),
        events=[
            DecisionJournalEventOut.model_validate(event)
            for event in loaded.events
        ],
        evidence_context=_evidence_out(loaded.evidence),
    )
