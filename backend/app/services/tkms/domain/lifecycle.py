"""RuleLifecycle — the single place the version status state machine lives.

A tax_rule_version moves through a fixed set of states; only legal transitions
are permitted and any illegal transition raises. The DB CHECK guards the set of
valid *values*; this guards the valid *moves* between them, so no service can
publish a draft that skipped review or resurrect an archived version.

    draft ─────────────► validated ─────► pending_review ─────► approved ─► published
      │  ▲                   │                   │  │                          │   ▲
      │  └───────────────────┘ (rework)          │  └─► archived               │   │ (rollback)
      └────────────────────────────────────────► archived        superseded ◄─┘   │
                                                                       └───────────┘
"""
from __future__ import annotations

DRAFT = "draft"
VALIDATED = "validated"
PENDING_REVIEW = "pending_review"
APPROVED = "approved"
PUBLISHED = "published"
SUPERSEDED = "superseded"
ARCHIVED = "archived"

ALL_STATES: frozenset[str] = frozenset(
    {DRAFT, VALIDATED, PENDING_REVIEW, APPROVED, PUBLISHED, SUPERSEDED, ARCHIVED}
)

# Legal transitions: from_state -> allowed next states.
_TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({VALIDATED, PENDING_REVIEW, ARCHIVED}),
    VALIDATED: frozenset({PENDING_REVIEW, DRAFT, ARCHIVED}),
    PENDING_REVIEW: frozenset({APPROVED, DRAFT, ARCHIVED}),   # approve / rework / discard
    APPROVED: frozenset({PUBLISHED, PENDING_REVIEW, ARCHIVED}),
    PUBLISHED: frozenset({SUPERSEDED, ARCHIVED}),
    SUPERSEDED: frozenset({PUBLISHED}),                       # rollback re-publishes a prior version
    ARCHIVED: frozenset(),                                    # terminal
}


class IllegalTransition(ValueError):
    """Raised when a version is asked to move between two states illegally."""

    def __init__(self, from_state: str, to_state: str):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Illegal rule-version transition: {from_state!r} → {to_state!r}")


class RuleLifecycle:
    """Stateless helper over the transition table (pure, import-free)."""

    @staticmethod
    def is_valid_state(state: str) -> bool:
        return state in ALL_STATES

    @staticmethod
    def can_transition(from_state: str, to_state: str) -> bool:
        return to_state in _TRANSITIONS.get(from_state, frozenset())

    @staticmethod
    def allowed_from(from_state: str) -> frozenset[str]:
        return _TRANSITIONS.get(from_state, frozenset())

    @classmethod
    def assert_transition(cls, from_state: str, to_state: str) -> None:
        if from_state not in ALL_STATES:
            raise IllegalTransition(from_state, to_state)
        if not cls.can_transition(from_state, to_state):
            raise IllegalTransition(from_state, to_state)

    @staticmethod
    def is_terminal(state: str) -> bool:
        return len(_TRANSITIONS.get(state, frozenset())) == 0

    @staticmethod
    def is_consumable_by_engine(state: str) -> bool:
        """The deterministic engine reads ONLY published versions."""
        return state == PUBLISHED
