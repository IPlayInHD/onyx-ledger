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


class ScenarioExecutionPolicy(StrEnum):
    """How a scenario's BASELINE was obtained (item 3B).

    Deliberately a separate enum from `InputExecutionPolicy`: an optimization
    and a scenario were corrected at different commits, and one value covering
    both would let a scenario inherit a guarantee it never had.
    """

    LIVE_BASELINE_LEGACY = "live_baseline_legacy"
    FROZEN_SNAPSHOT_V1 = "frozen_snapshot_v1"


SCENARIO_EXECUTION_POLICY_VERSION = ScenarioExecutionPolicy.FROZEN_SNAPSHOT_V1


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


# ---------------------------------------------------------------------------
# Scenario execution (item 3B)
# ---------------------------------------------------------------------------
# Scenario-specific failure codes. Distinct from the optimization ones so an
# operator reading an alert knows which workflow refused, and so a client can
# offer the right remedy — "re-run the analysis" for an unsupported legacy
# format is a different action from "the snapshot was tampered with".
PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE = "PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE"
PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH = "PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH"
PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED = (
    "PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED")
# Distinct from UNAVAILABLE: the snapshot row exists and its hash is intact, but
# it does not carry every engine input. "We cannot find it" and "we found it and
# a field is missing" call for different operator action, so they are different
# codes rather than one bucket.
PINNED_SCENARIO_SNAPSHOT_INCOMPLETE = "PINNED_SCENARIO_SNAPSHOT_INCOMPLETE"
PINNED_SCENARIO_BASELINE_UNAVAILABLE = "PINNED_SCENARIO_BASELINE_UNAVAILABLE"
PINNED_SCENARIO_BASELINE_HASH_MISMATCH = "PINNED_SCENARIO_BASELINE_HASH_MISMATCH"
PINNED_SCENARIO_BASELINE_REPRODUCTION_FAILED = (
    "PINNED_SCENARIO_BASELINE_REPRODUCTION_FAILED")
SCENARIO_FROZEN_INPUT_RECONSTRUCTION_FAILED = (
    "SCENARIO_FROZEN_INPUT_RECONSTRUCTION_FAILED")
SCENARIO_EXECUTION_POLICY_INVALID = "SCENARIO_EXECUTION_POLICY_INVALID"

# The optimization reason a scenario failure maps FROM. One reconstruction
# service raises the analysis-level codes; the scenario layer translates them so
# the workflow that failed is identifiable from the code alone.
SCENARIO_REASON_FOR: dict[str, str] = {
    PINNED_SNAPSHOT_UNAVAILABLE: PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE,
    PINNED_SNAPSHOT_HASH_MISMATCH: PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH,
    PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED: PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED,
    PINNED_SNAPSHOT_INCOMPLETE: PINNED_SCENARIO_SNAPSHOT_INCOMPLETE,
    PINNED_BASELINE_RESULT_UNAVAILABLE: PINNED_SCENARIO_BASELINE_UNAVAILABLE,
    PINNED_BASELINE_RESULT_HASH_MISMATCH: PINNED_SCENARIO_BASELINE_HASH_MISMATCH,
    FROZEN_INPUT_RECONSTRUCTION_FAILED: SCENARIO_FROZEN_INPUT_RECONSTRUCTION_FAILED,
}

SCENARIO_SNAPSHOT_REASONS = frozenset(SCENARIO_REASON_FOR.values()) | {
    # not reachable by translation — raised directly when the stored manifest
    # itself is the problem rather than the snapshot behind it
    SCENARIO_EXECUTION_POLICY_INVALID,
}


class ScenarioFrozenInputError(FrozenSnapshotError):
    """A scenario-scoped snapshot or baseline failure."""


class ScenarioPolicyError(ScenarioFrozenInputError):
    """The stored execution policy is absent where required, or malformed.

    A subclass of the scenario input error so it reaches the same sanitized 409
    the other refusals do: a client cannot act differently on it, and the
    difference that matters — which code — is already carried by `reason`.
    """


def scenario_execution_policy(manifest: dict[str, Any] | None) -> str:
    """How a stored scenario's baseline was obtained. Strict.

    Exactly one thing is tolerated: **absence**. A manifest written before this
    correction cannot carry the key, and every manifest written after it must
    (`assert_current_scenario_policy` refuses to seal one that does not), so an
    absent key is a reliable marker of a historical scenario rather than a
    guess. Reading that silence as `frozen_snapshot_v1` would hand a historical
    scenario a guarantee it never had — undetectably.

    Everything else fails closed. An explicit `null`, an unknown string, a
    number, a nested object or a manifest that is not an object at all is a
    defect in the stored evidence, and quietly answering `live_baseline_legacy`
    would bury it: the row would be filed beside genuinely historical scenarios
    and never looked at again. A corrupt manifest is not a historical manifest.
    """
    if manifest is None:
        return ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value
    if not isinstance(manifest, dict):
        raise ScenarioPolicyError(SCENARIO_EXECUTION_POLICY_INVALID)
    if "scenario_execution_policy_version" not in manifest:
        return ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value

    value = manifest["scenario_execution_policy_version"]
    if not isinstance(value, str) or value not in tuple(ScenarioExecutionPolicy):
        # includes explicit null, wrong JSON type, and unsupported versions
        raise ScenarioPolicyError(SCENARIO_EXECUTION_POLICY_INVALID)
    return value


def is_legacy_scenario_policy(manifest: dict[str, Any] | None) -> bool:
    """True when the scenario predates the frozen-baseline guarantee.

    The one interpretation of the policy, shared by presentation, sealing and
    replay verification. A second reading of the same key is how two parts of a
    system come to disagree about what a stored row means.
    """
    return scenario_execution_policy(manifest) == (
        ScenarioExecutionPolicy.LIVE_BASELINE_LEGACY.value)


def assert_current_scenario_policy(manifest: dict[str, Any] | None) -> str:
    """Refuse to seal a scenario that does not state the current policy.

    This is what makes "absent means historical" a property of the data rather
    than an assumption about it: after this check, no scenario can be created
    without an explicit, current, well-formed policy, so any row lacking one was
    necessarily written before the policy existed.
    """
    policy = scenario_execution_policy(manifest)
    if policy != SCENARIO_EXECUTION_POLICY_VERSION.value:
        raise ScenarioPolicyError(SCENARIO_EXECUTION_POLICY_INVALID)
    return policy


def as_scenario_failure(error: FrozenSnapshotError) -> ScenarioFrozenInputError:
    """Translate an analysis-level reason into its scenario equivalent.

    The underlying check is identical — one reconstruction service, one codec —
    but the code a client receives names the workflow that refused.
    """
    return ScenarioFrozenInputError(
        SCENARIO_REASON_FOR.get(
            error.reason, SCENARIO_FROZEN_INPUT_RECONSTRUCTION_FAILED)
    )


@dataclass(frozen=True)
class FrozenScenarioExecutionInput:
    """Everything a scenario computation is permitted to know.

    A thin immutable wrapper around item 3A's `FrozenAnalysisInput` rather than
    a parallel implementation: the snapshot, its hash, the ownership rule and
    the reconstruction are shared, and only the scenario-specific pins are added
    here. Two reconstruction systems would be two chances to disagree about what
    a baseline is.

    Like its parent it holds no session, no repository and no loader, so the
    scenario compute pipeline has no route back to mutable state.
    """

    frozen_analysis_input: FrozenAnalysisInput
    scenario_id: uuid.UUID | None
    scenario_spec_hash: str
    objective_code: str
    objective_version: str
    lever_registry_version: str
    assumption_registry_version: str
    support_score_version: str
    scenario_execution_policy_version: str = (
        SCENARIO_EXECUTION_POLICY_VERSION.value)

    @property
    def baseline_tax_input(self) -> TaxInput:
        return self.frozen_analysis_input.tax_input

    @property
    def baseline_tax(self) -> Decimal:
        return self.frozen_analysis_input.baseline_tax

    @property
    def baseline_result_hash(self) -> str:
        return self.frozen_analysis_input.baseline_result_hash

    @property
    def snapshot_hash(self) -> str:
        return self.frozen_analysis_input.snapshot_hash

    def baseline_clone(self) -> dict[str, Any]:
        """A fresh mutable clone for a hypothetical.

        Every lever application starts here. The frozen baseline itself is never
        the thing a lever mutates, so a half-applied composite lever cannot
        corrupt the baseline the next candidate is measured against.
        """
        return self.frozen_analysis_input.inputs_as_dict()

    def bind(
        self,
        *,
        scenario_id: uuid.UUID | None = None,
        scenario_spec_hash: str | None = None,
    ) -> FrozenScenarioExecutionInput:
        """A new object carrying the scenario identity. Replaces, never mutates.

        The spec hash is taken OVER the pinned baseline, so it cannot exist when
        the baseline is resolved. Binding it afterwards keeps the ordering
        honest — identity is computed from the pins, then attached to them —
        without making the execution input mutable.
        """
        return dataclasses.replace(
            self,
            scenario_id=self.scenario_id if scenario_id is None else scenario_id,
            scenario_spec_hash=(
                self.scenario_spec_hash if scenario_spec_hash is None
                else scenario_spec_hash
            ),
        )


__all__ = [
    "FROZEN_INPUT_RECONSTRUCTION_FAILED",
    "INPUT_EXECUTION_POLICY_VERSION",
    "PINNED_BASELINE_RESULT_HASH_MISMATCH",
    "PINNED_BASELINE_RESULT_UNAVAILABLE",
    "PINNED_SCENARIO_BASELINE_HASH_MISMATCH",
    "PINNED_SCENARIO_BASELINE_REPRODUCTION_FAILED",
    "PINNED_SCENARIO_BASELINE_UNAVAILABLE",
    "PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH",
    "PINNED_SCENARIO_SNAPSHOT_INCOMPLETE",
    "PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED",
    "PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE",
    "PINNED_SNAPSHOT_HASH_MISMATCH",
    "PINNED_SNAPSHOT_INCOMPLETE",
    "PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED",
    "PINNED_SNAPSHOT_UNAVAILABLE",
    "SCENARIO_EXECUTION_POLICY_INVALID",
    "SCENARIO_EXECUTION_POLICY_VERSION",
    "SCENARIO_FROZEN_INPUT_RECONSTRUCTION_FAILED",
    "SCENARIO_REASON_FOR",
    "SCENARIO_SNAPSHOT_REASONS",
    "SNAPSHOT_REASONS",
    "SNAPSHOT_SCHEMA_VERSION",
    "SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS",
    "FrozenAnalysisInput",
    "FrozenScenarioExecutionInput",
    "FrozenSnapshotError",
    "InputExecutionPolicy",
    "ScenarioExecutionPolicy",
    "ScenarioFrozenInputError",
    "ScenarioPolicyError",
    "as_scenario_failure",
    "assert_current_scenario_policy",
    "canonical_snapshot",
    "is_legacy_scenario_policy",
    "scenario_execution_policy",
    "reconstruct_tax_input",
    "snapshot_hash",
]
