"""Workflow state machine — the single definition of legal transitions (§8.2).

Mirrors the database trigger `ioe.guard_workflow_transition()`. Both exist on
purpose: this layer gives expressive errors at the point of the mistake, the
trigger is a backstop that holds even if a write bypasses the service.

Workflow status and freshness are SEPARATE axes. A completed run may become
stale or superseded without any of its calculation evidence changing.
"""
from __future__ import annotations

from app.services.ioe.domain.enums import FreshnessStatus, VisibilityStatus, WorkflowStatus

_WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.PENDING: frozenset({
        WorkflowStatus.RUNNING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED,
    }),
    WorkflowStatus.RUNNING: frozenset({
        WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED,
    }),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}

_FRESHNESS_TRANSITIONS: dict[FreshnessStatus, frozenset[FreshnessStatus]] = {
    FreshnessStatus.CURRENT: frozenset({FreshnessStatus.STALE, FreshnessStatus.SUPERSEDED}),
    FreshnessStatus.STALE: frozenset({FreshnessStatus.SUPERSEDED}),
    FreshnessStatus.SUPERSEDED: frozenset(),
}

_VISIBILITY_TRANSITIONS: dict[VisibilityStatus, frozenset[VisibilityStatus]] = {
    # Archiving is reversible; it hides a scenario, it does not destroy evidence.
    VisibilityStatus.ACTIVE: frozenset({VisibilityStatus.ARCHIVED}),
    VisibilityStatus.ARCHIVED: frozenset({VisibilityStatus.ACTIVE}),
}


class IllegalTransition(ValueError):
    def __init__(self, kind: str, from_state: object, to_state: object) -> None:
        self.kind, self.from_state, self.to_state = kind, from_state, to_state
        super().__init__(f"Illegal {kind} transition: {from_state} → {to_state}")


class WorkflowStateMachine:
    """Stateless helpers over the transition tables."""

    # ---- workflow ----
    @staticmethod
    def can_transition(from_state: WorkflowStatus, to_state: WorkflowStatus) -> bool:
        return to_state in _WORKFLOW_TRANSITIONS.get(from_state, frozenset())

    @classmethod
    def assert_transition(cls, from_state: WorkflowStatus, to_state: WorkflowStatus) -> None:
        if not cls.can_transition(from_state, to_state):
            raise IllegalTransition("workflow", from_state, to_state)

    @staticmethod
    def is_terminal(state: WorkflowStatus) -> bool:
        return not _WORKFLOW_TRANSITIONS.get(state, frozenset())

    @staticmethod
    def allowed_from(state: WorkflowStatus) -> frozenset[WorkflowStatus]:
        return _WORKFLOW_TRANSITIONS.get(state, frozenset())

    # ---- freshness (independent of workflow status) ----
    @staticmethod
    def can_transition_freshness(from_state: FreshnessStatus, to_state: FreshnessStatus) -> bool:
        return to_state in _FRESHNESS_TRANSITIONS.get(from_state, frozenset())

    @classmethod
    def assert_freshness_transition(
        cls, from_state: FreshnessStatus, to_state: FreshnessStatus
    ) -> None:
        if not cls.can_transition_freshness(from_state, to_state):
            raise IllegalTransition("freshness", from_state, to_state)

    # ---- visibility (scenario archive) ----
    @staticmethod
    def can_transition_visibility(
        from_state: VisibilityStatus, to_state: VisibilityStatus
    ) -> bool:
        return to_state in _VISIBILITY_TRANSITIONS.get(from_state, frozenset())

    @classmethod
    def assert_visibility_transition(
        cls, from_state: VisibilityStatus, to_state: VisibilityStatus
    ) -> None:
        if not cls.can_transition_visibility(from_state, to_state):
            raise IllegalTransition("visibility", from_state, to_state)

    @staticmethod
    def results_are_sealed(state: WorkflowStatus) -> bool:
        """Once completed, computed columns are evidence and cannot change."""
        return state is WorkflowStatus.COMPLETED
