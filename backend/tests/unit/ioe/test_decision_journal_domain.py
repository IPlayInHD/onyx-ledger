"""The Decision Journal projection — pure, over synthetic histories.

What is at stake: that folding an append-only history is ALL the projection
does. No inference — a PROCEED is not an action, a report is not verification,
and the same events produce the same state no matter how the rows arrived.
"""
from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.ioe.journal.domain import (
    Decision,
    ExecutionState,
    JournalEventType,
    JournalEventView,
    project,
)

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _created(sequence: int = 1, at: datetime = T0) -> JournalEventView:
    return JournalEventView(
        sequence=sequence, event_type=JournalEventType.CREATED,
        decision=Decision.CONSIDERING, user_reported_action_date=None,
        recorded_at=at)


def _decided(sequence: int, decision: Decision, *, minutes: int) -> JournalEventView:
    return JournalEventView(
        sequence=sequence, event_type=JournalEventType.DECISION_RECORDED,
        decision=decision, user_reported_action_date=None,
        recorded_at=T0 + timedelta(minutes=minutes))


def _reported(
    sequence: int, *, minutes: int, action_date: date | None = None
) -> JournalEventView:
    return JournalEventView(
        sequence=sequence, event_type=JournalEventType.ACTION_REPORTED,
        decision=None, user_reported_action_date=action_date,
        recorded_at=T0 + timedelta(minutes=minutes))


def test_a_new_thread_is_considering_and_unreported():
    projection = project([_created()])
    assert projection.current_decision is Decision.CONSIDERING
    assert projection.execution is ExecutionState.NOT_REPORTED
    assert projection.event_count == 1
    assert projection.last_reported_action_date is None


@pytest.mark.parametrize(
    "decision", [Decision.PROCEED, Decision.DEFER, Decision.DECLINE])
def test_each_declaration_becomes_the_current_decision(decision):
    projection = project([_created(), _decided(2, decision, minutes=5)])
    assert projection.current_decision is decision
    assert projection.decision_recorded_at == T0 + timedelta(minutes=5)


def test_a_change_of_mind_supersedes_without_erasing():
    """DEFER then PROCEED: the later declaration wins the projection; both
    events remain in the history the projection was computed from."""
    events = [
        _created(),
        _decided(2, Decision.DEFER, minutes=5),
        _decided(3, Decision.PROCEED, minutes=10),
    ]
    projection = project(events)
    assert projection.current_decision is Decision.PROCEED
    assert projection.event_count == 3  # nothing was rewritten away


def test_deciding_to_proceed_is_not_an_action():
    """THE central distinction. PROCEED moves intent, never execution."""
    projection = project([_created(), _decided(2, Decision.PROCEED, minutes=5)])
    assert projection.current_decision is Decision.PROCEED
    assert projection.execution is ExecutionState.NOT_REPORTED


def test_a_report_moves_execution_and_not_the_decision():
    projection = project([
        _created(),
        _reported(2, minutes=20, action_date=date(2026, 2, 27)),
    ])
    assert projection.execution is ExecutionState.USER_REPORTED
    assert projection.current_decision is Decision.CONSIDERING
    assert projection.last_reported_action_date == date(2026, 2, 27)
    assert projection.last_action_reported_at == T0 + timedelta(minutes=20)


def test_the_latest_report_wins_and_earlier_reports_remain_counted():
    projection = project([
        _created(),
        _reported(2, minutes=20, action_date=date(2026, 2, 27)),
        _reported(3, minutes=40, action_date=date(2026, 3, 1)),
    ])
    assert projection.last_reported_action_date == date(2026, 3, 1)
    assert projection.event_count == 3


def test_row_order_cannot_change_the_projection():
    """Sequence is the ordering authority; the list order is a query accident."""
    events = [
        _created(),
        _decided(2, Decision.DEFER, minutes=5),
        _decided(3, Decision.PROCEED, minutes=10),
        _reported(4, minutes=20),
    ]
    forward = project(events)
    shuffled = project(list(reversed(events)))
    assert forward == shuffled


def test_the_same_history_projects_identically_every_time():
    events = [_created(), _decided(2, Decision.PROCEED, minutes=5)]
    assert project(events) == project(events)


def test_an_empty_history_is_refused():
    with pytest.raises(ValueError):
        project([])


def test_a_history_that_does_not_begin_with_created_is_refused():
    with pytest.raises(ValueError):
        project([_decided(1, Decision.PROCEED, minutes=5)])
