"""RuleLifecycle state machine — legal + illegal transitions."""
import pytest

from app.services.tkms.domain.lifecycle import (
    APPROVED,
    ARCHIVED,
    DRAFT,
    PENDING_REVIEW,
    PUBLISHED,
    SUPERSEDED,
    VALIDATED,
    IllegalTransition,
    RuleLifecycle,
)

LEGAL = [
    (DRAFT, VALIDATED),
    (DRAFT, PENDING_REVIEW),
    (DRAFT, ARCHIVED),
    (VALIDATED, PENDING_REVIEW),
    (VALIDATED, DRAFT),
    (PENDING_REVIEW, APPROVED),
    (PENDING_REVIEW, DRAFT),
    (PENDING_REVIEW, ARCHIVED),
    (APPROVED, PUBLISHED),
    (PUBLISHED, SUPERSEDED),
    (SUPERSEDED, PUBLISHED),   # rollback path
]

ILLEGAL = [
    (DRAFT, PUBLISHED),        # cannot skip review
    (DRAFT, APPROVED),
    (VALIDATED, PUBLISHED),
    (PENDING_REVIEW, PUBLISHED),
    (PUBLISHED, DRAFT),
    (ARCHIVED, DRAFT),         # terminal
    (ARCHIVED, PUBLISHED),
]


@pytest.mark.parametrize("frm,to", LEGAL)
def test_legal_transitions(frm, to):
    assert RuleLifecycle.can_transition(frm, to)
    RuleLifecycle.assert_transition(frm, to)  # must not raise


@pytest.mark.parametrize("frm,to", ILLEGAL)
def test_illegal_transitions_raise(frm, to):
    assert not RuleLifecycle.can_transition(frm, to)
    with pytest.raises(IllegalTransition):
        RuleLifecycle.assert_transition(frm, to)


def test_engine_consumes_only_published():
    assert RuleLifecycle.is_consumable_by_engine(PUBLISHED)
    for s in (DRAFT, VALIDATED, PENDING_REVIEW, APPROVED, SUPERSEDED, ARCHIVED):
        assert not RuleLifecycle.is_consumable_by_engine(s)


def test_archived_is_terminal():
    assert RuleLifecycle.is_terminal(ARCHIVED)
    assert RuleLifecycle.allowed_from(ARCHIVED) == frozenset()


def test_unknown_state_is_illegal():
    with pytest.raises(IllegalTransition):
        RuleLifecycle.assert_transition("bogus", PUBLISHED)
