"""Replay-integrity vocabulary — pure, no I/O, no clock, no session.

Integrity answers one question about a SEALED result: can it still be
reproduced from its own pinned dependencies? That is deliberately a different
axis from freshness, and the two are never collapsed:

    workflow_status   completed / failed / cancelled  — did the run finish?
    freshness_status  current / stale / superseded    — do the inputs still hold?
    integrity_status  not_checked / verified /
                      mismatch / unavailable          — can we still reproduce it?

A result can be `stale` and perfectly reproducible: the user's income changed,
but the answer we gave them at the time is exactly the answer that evidence
produces. It can equally be `current` and NOT reproducible, which is a far more
serious condition and must never be shown behind the word "stale" — that reads
as "your data changed" when the truth is "we cannot reproduce what we told you".
`mismatch` is therefore surfaced to users as `non_reproducible`.

Reason codes are a closed enumeration mirrored by a CHECK constraint. An
exception message is never a reason code: it is unbounded text that has already
been near financial values, and it would end up in an operational event.
"""
from __future__ import annotations

from enum import StrEnum

# Bumped when the replay ALGORITHM changes in a way that could change a verdict.
# Part of the active-check arbitration key, so a new verifier may legitimately
# re-verify an entity an old verifier already checked.
VERIFIER_VERSION = "1.0.0"

# Bumped when the SCHEDULING policy changes — batch sizes, ordering, triggers.
INTEGRITY_CHECK_POLICY_VERSION = "1.0.0"

# Canonical serialization versions this verifier knows how to reproduce. A
# result sealed under a version not listed here cannot be replayed by this
# build; that is `unavailable`, never `mismatch`, because nothing has been shown
# to differ.
SUPPORTED_CANONICAL_SERIALIZATION_VERSIONS = frozenset({"1.1.0"})


class IntegrityStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


class CheckStatus(StrEnum):
    """The lifecycle of one verification attempt.

    `failed` exists only for the attempt, never for the entity: a verifier that
    crashed has proved nothing about the result, so the entity's own status
    stays `unavailable` rather than being downgraded on the strength of a bug.
    """

    RUNNING = "running"
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class EntityType(StrEnum):
    OPTIMIZATION = "optimization"
    PORTFOLIO = "portfolio"
    SCENARIO = "scenario"


class IntegrityReason(StrEnum):
    NONE = "NONE"

    # the replay ran and produced a different identity
    RESULT_HASH_MISMATCH = "RESULT_HASH_MISMATCH"
    PORTFOLIO_HASH_MISMATCH = "PORTFOLIO_HASH_MISMATCH"

    # a pinned dependency could not be loaded — nothing was compared
    BASELINE_SNAPSHOT_UNAVAILABLE = "BASELINE_SNAPSHOT_UNAVAILABLE"
    BASELINE_RESULT_UNAVAILABLE = "BASELINE_RESULT_UNAVAILABLE"
    PINNED_RULE_SNAPSHOT_UNAVAILABLE = "PINNED_RULE_SNAPSHOT_UNAVAILABLE"
    PINNED_ENGINE_VERSION_UNAVAILABLE = "PINNED_ENGINE_VERSION_UNAVAILABLE"
    PINNED_ENGINE_CONFIG_UNAVAILABLE = "PINNED_ENGINE_CONFIG_UNAVAILABLE"
    REFERENCE_DATA_VERSION_UNAVAILABLE = "REFERENCE_DATA_VERSION_UNAVAILABLE"
    OBJECTIVE_VERSION_UNAVAILABLE = "OBJECTIVE_VERSION_UNAVAILABLE"
    LEVER_REGISTRY_VERSION_UNAVAILABLE = "LEVER_REGISTRY_VERSION_UNAVAILABLE"
    ASSUMPTION_REGISTRY_VERSION_UNAVAILABLE = "ASSUMPTION_REGISTRY_VERSION_UNAVAILABLE"
    SCORING_VERSION_UNAVAILABLE = "SCORING_VERSION_UNAVAILABLE"
    SUPPORT_SCORE_VERSION_UNAVAILABLE = "SUPPORT_SCORE_VERSION_UNAVAILABLE"
    PROJECTION_VERSION_UNAVAILABLE = "PROJECTION_VERSION_UNAVAILABLE"
    CANONICAL_SERIALIZATION_VERSION_UNSUPPORTED = (
        "CANONICAL_SERIALIZATION_VERSION_UNSUPPORTED"
    )

    # the verifier itself failed, or the sealed rows are not complete enough
    REPLAY_EXECUTION_FAILED = "REPLAY_EXECUTION_FAILED"
    SEALED_EVIDENCE_INCOMPLETE = "SEALED_EVIDENCE_INCOMPLETE"


MISMATCH_REASONS = frozenset({
    IntegrityReason.RESULT_HASH_MISMATCH,
    IntegrityReason.PORTFOLIO_HASH_MISMATCH,
})

# The user-visible classification for a mismatch. Kept as a distinct word so no
# presentation layer can quietly render it as ordinary staleness.
NON_REPRODUCIBLE = "non_reproducible"


def user_visible_state(status: IntegrityStatus | str) -> str:
    """The classification shown to a user. `mismatch` becomes explicit."""
    value = IntegrityStatus(status)
    return NON_REPRODUCIBLE if value is IntegrityStatus.MISMATCH else value.value


# Wording is fixed here rather than in a template so a copy edit cannot turn a
# reproducibility statement into a correctness claim. None of these say the
# figure is right, accepted by the CRA, or legally sound — only whether the
# recorded calculation still reproduces.
INTEGRITY_WARNINGS: dict[str, str] = {
    IntegrityStatus.VERIFIED.value: (
        "This historical result was successfully reproduced from its sealed "
        "inputs and pinned calculation versions."
    ),
    IntegrityStatus.NOT_CHECKED.value: (
        "This result has not yet undergone a production replay check."
    ),
    IntegrityStatus.UNAVAILABLE.value: (
        "This result could not currently be replay-verified because a pinned "
        "dependency is unavailable."
    ),
    NON_REPRODUCIBLE: (
        "This historical result could not be reproduced exactly. The original "
        "result has been preserved and is under review."
    ),
}


def integrity_warning(status: IntegrityStatus | str) -> str:
    return INTEGRITY_WARNINGS[user_visible_state(status)]


class DependencyUnavailable(Exception):
    """A pinned dependency could not be resolved.

    Carries a reason code, never a message destined for storage. The verifier
    converts it to `unavailable`; it must never be reported as a mismatch,
    because nothing was compared.
    """

    def __init__(self, reason: IntegrityReason):
        self.reason = reason
        super().__init__(reason.value)


class SealedEvidenceIncomplete(Exception):
    """The stored rows do not carry enough to reconstruct the sealed identity."""

    def __init__(self, reason: IntegrityReason = IntegrityReason.SEALED_EVIDENCE_INCOMPLETE):
        self.reason = reason
        super().__init__(reason.value)


__all__ = [
    "INTEGRITY_CHECK_POLICY_VERSION",
    "INTEGRITY_WARNINGS",
    "MISMATCH_REASONS",
    "NON_REPRODUCIBLE",
    "SUPPORTED_CANONICAL_SERIALIZATION_VERSIONS",
    "VERIFIER_VERSION",
    "CheckStatus",
    "DependencyUnavailable",
    "EntityType",
    "IntegrityReason",
    "IntegrityStatus",
    "SealedEvidenceIncomplete",
    "integrity_warning",
    "user_visible_state",
]
