"""The frozen analysis snapshot: codec, value object, and reconstruction.

The defect this exists to close: TX-1 pinned an immutable snapshot and put its
hash into `optimization_spec_hash`, and then the compute phase rebuilt its tax
inputs from the user's *live* financial tables. Between those two moments a
person can edit last year's income. The sequence

    pin snapshot A → live data becomes B → calculate from B → seal under A

produces a result whose identity claims to describe A and whose numbers describe
B. The spec hash is then not an identity for the calculation it labels, which
makes replay verification structurally impossible and makes the sealed evidence
a statement about nothing in particular.

The invariant now enforced: an optimization is calculated **exclusively** from
the exact frozen snapshot pinned in TX-1. There is no fallback to live data —
a missing, corrupt, incomplete or unsupported snapshot fails closed, before any
engine run.

`FrozenAnalysisInput` is the only channel through which the compute pipeline
receives user data. It is immutable, carries a reconstructed `TaxInput`, and
exposes no repository, no session and no user-id-keyed loader, so a downstream
service *cannot* reach live state even by accident.
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.services.ioe.domain import canonical as c
from app.services.tax_engine.core.engine import TaxInput

# The stored snapshot's own schema. Bumped when the payload SHAPE changes, which
# is a different thing from the canonical serialization version: a reader must
# be able to refuse a payload it does not know how to reconstruct.
SNAPSHOT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({SNAPSHOT_SCHEMA_VERSION})

# How a run's inputs were obtained. Pinned on the run so a reader can tell,
# without guessing from a date, whether a historical result was calculated under
# the corrected rule.
class InputExecutionPolicy(StrEnum):
    LIVE_SOURCE_LEGACY = "live_source_legacy"
    FROZEN_SNAPSHOT_V1 = "frozen_snapshot_v1"


INPUT_EXECUTION_POLICY_VERSION = InputExecutionPolicy.FROZEN_SNAPSHOT_V1


class FrozenSnapshotError(Exception):
    """A structured, sanitized snapshot failure.

    Carries a reason code, never a message destined for storage or a log. The
    payload it failed on is financial data and must not travel with the error.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# Enumerated failure reasons. Sanitized: no payload, no exception text.
PINNED_SNAPSHOT_UNAVAILABLE = "PINNED_SNAPSHOT_UNAVAILABLE"
PINNED_SNAPSHOT_HASH_MISMATCH = "PINNED_SNAPSHOT_HASH_MISMATCH"
PINNED_SNAPSHOT_INCOMPLETE = "PINNED_SNAPSHOT_INCOMPLETE"
PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED = "PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED"
PINNED_BASELINE_RESULT_UNAVAILABLE = "PINNED_BASELINE_RESULT_UNAVAILABLE"
PINNED_BASELINE_RESULT_HASH_MISMATCH = "PINNED_BASELINE_RESULT_HASH_MISMATCH"
FROZEN_INPUT_RECONSTRUCTION_FAILED = "FROZEN_INPUT_RECONSTRUCTION_FAILED"

SNAPSHOT_REASONS = frozenset({
    PINNED_SNAPSHOT_UNAVAILABLE,
    PINNED_SNAPSHOT_HASH_MISMATCH,
    PINNED_SNAPSHOT_INCOMPLETE,
    PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED,
    PINNED_BASELINE_RESULT_UNAVAILABLE,
    PINNED_BASELINE_RESULT_HASH_MISMATCH,
    FROZEN_INPUT_RECONSTRUCTION_FAILED,
})


# ---------------------------------------------------------------------------
# Codec — one definition, used by the writer and the reader
# ---------------------------------------------------------------------------
def canonical_snapshot(
    tax_input: TaxInput, *, tax_year: int, jurisdiction: str
) -> dict[str, Any]:
    """The payload an analysis freezes. Every engine input field, explicitly.

    Written by `AnalysisService` and read back by `FrozenAnalysisInputService`.
    Sharing one definition is what guarantees a snapshot can be reconstructed,
    rather than hoping the two ends agree.
    """
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "tax_year": tax_year,
        "jurisdiction": jurisdiction,
        "inputs": {
            field.name: _encode(getattr(tax_input, field.name))
            for field in dataclasses.fields(TaxInput)
        },
    }


def snapshot_hash(payload: dict[str, Any]) -> str:
    """Content-address the payload through the project's canonicalizer.

    Not an ad-hoc `json.dumps` + sha256: the canonicalizer already fixes
    Unicode form, key order and Decimal scale, and a snapshot hash that differed
    from every other hash in the system by construction would be one more thing
    to get wrong.
    """
    return c.canonical_hash(payload)


def reconstruct_tax_input(payload: dict[str, Any]) -> TaxInput:
    """Rebuild the exact `TaxInput` the analysis froze.

    A field the payload does not carry is an INCOMPLETE snapshot, not a field
    to default: reconstructing a missing income from zero would silently invent
    an input and produce a confidently wrong result.
    """
    if not isinstance(payload, dict):
        raise FrozenSnapshotError(PINNED_SNAPSHOT_INCOMPLETE)

    version = payload.get("schema_version")
    if version not in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        raise FrozenSnapshotError(PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED)

    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        raise FrozenSnapshotError(PINNED_SNAPSHOT_INCOMPLETE)

    values: dict[str, Any] = {}
    for field in dataclasses.fields(TaxInput):
        if field.name not in inputs:
            raise FrozenSnapshotError(PINNED_SNAPSHOT_INCOMPLETE)
        try:
            values[field.name] = _decode(inputs[field.name], field.type)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise FrozenSnapshotError(FROZEN_INPUT_RECONSTRUCTION_FAILED) from exc
    try:
        return TaxInput(**values)
    except TypeError as exc:
        raise FrozenSnapshotError(FROZEN_INPUT_RECONSTRUCTION_FAILED) from exc


def _encode(value: Any) -> Any:
    return None if value is None else str(value)


def _decode(raw: Any, declared: Any) -> Any:
    if raw is None or raw == "None":
        return None
    text = str(declared)
    if "Decimal" in text:
        return Decimal(str(raw))
    if "int" in text:
        return int(str(raw))
    return str(raw)


# ---------------------------------------------------------------------------
# The value object
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FrozenAnalysisInput:
    """Everything the compute phase is permitted to know about a user.

    Frozen, constructed once from pinned evidence, and passed explicitly. It
    carries no session, no repository and no user-keyed loader — a downstream
    service holding this object has no route back to mutable state, which is the
    structural half of the guarantee that comments cannot provide.

    It is deliberately NOT persisted: the reconstructed inputs are the user's
    financial data, they already live in the analysis snapshot, and copying them
    into IOE result tables would duplicate raw financial inputs into evidence.
    """

    user_id: uuid.UUID
    analysis_id: uuid.UUID
    snapshot_id: uuid.UUID
    snapshot_hash: str
    snapshot_schema_version: str
    tax_year: int
    jurisdiction: str
    tax_input: TaxInput
    baseline_result_hash: str
    baseline_tax: Decimal
    rule_snapshot_id: uuid.UUID | None = None
    pinned_rule_version_ids: tuple[uuid.UUID, ...] = ()
    version_manifest: dict[str, Any] | None = None
    input_execution_policy_version: str = INPUT_EXECUTION_POLICY_VERSION.value

    def inputs_as_dict(self) -> dict[str, Any]:
        """A fresh mutable CLONE for a hypothetical. The frozen input is never
        the thing a lever mutates."""
        return {
            field.name: getattr(self.tax_input, field.name)
            for field in dataclasses.fields(TaxInput)
        }

    def with_pins(
        self, *, rule_snapshot_id: uuid.UUID,
        pinned_rule_version_ids: tuple[uuid.UUID, ...],
        version_manifest: dict[str, Any],
    ) -> FrozenAnalysisInput:
        """A new object carrying the rule pins. Replaces, never mutates."""
        return dataclasses.replace(
            self,
            rule_snapshot_id=rule_snapshot_id,
            pinned_rule_version_ids=pinned_rule_version_ids,
            version_manifest=version_manifest,
        )


__all__ = [
    "FROZEN_INPUT_RECONSTRUCTION_FAILED",
    "INPUT_EXECUTION_POLICY_VERSION",
    "PINNED_BASELINE_RESULT_HASH_MISMATCH",
    "PINNED_BASELINE_RESULT_UNAVAILABLE",
    "PINNED_SNAPSHOT_HASH_MISMATCH",
    "PINNED_SNAPSHOT_INCOMPLETE",
    "PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED",
    "PINNED_SNAPSHOT_UNAVAILABLE",
    "SNAPSHOT_REASONS",
    "SNAPSHOT_SCHEMA_VERSION",
    "SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS",
    "FrozenAnalysisInput",
    "FrozenSnapshotError",
    "InputExecutionPolicy",
    "canonical_snapshot",
    "reconstruct_tax_input",
    "snapshot_hash",
]
