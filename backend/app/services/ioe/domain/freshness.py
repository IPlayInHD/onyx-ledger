"""Freshness evaluation and scenario comparability (§P5).

Two related refusals live here, and both exist for the same reason: a stored
scenario result is evidence produced under a specific set of pinned inputs, and
it stays true only of those inputs.

**Freshness.** When the world moves on, the result is not wrong — it is a
correct statement about a baseline that no longer holds. So it is LABELLED
`stale` with a structured reason, never silently recomputed and never silently
presented as current. Refreshing produces a NEW scenario; the old one keeps its
own pins and its own numbers.

**Comparability.** Two scenarios can only be compared if they are answers to
questions of the same shape. Comparing a 2024 result to a 2025 one, or results
measured under different objective policies, would produce a difference that
looks like an insight and is actually an artefact. Incompatible pairs are
refused with a reason rather than reconciled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.core.exceptions import DomainError
from app.services.ioe.domain.scenario import FreshnessStatus, StaleReason

FRESHNESS_POLICY_VERSION = "1.0.0"
COMPARISON_POLICY_VERSION = "1.0.0"

MONEY = Decimal("0.01")


class ScenariosNotComparable(DomainError):
    """Two scenarios are not answers to questions of the same shape.

    A DomainError rather than a bare ValueError so the API refuses the request
    with a 409 and an explanation, instead of a 500 that reads like a bug.
    """

    status_code = 409
    error_type = "https://onyx.ledger/errors/scenarios-not-comparable"
    title = "Scenarios Not Comparable"

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class PinnedState:
    """The pinned inputs of a scenario, or the same values as they are now."""

    baseline_input_snapshot_hash: str | None = None
    baseline_result_hash: str | None = None
    rule_snapshot_hash: str | None = None
    engine_version: str | None = None
    reference_data_version: str | None = None
    lever_registry_version: str | None = None
    objective_code: str | None = None
    objective_version: str | None = None
    assumption_set_hash: str | None = None
    tax_year: int | None = None


# Ordered most-fundamental first: the FIRST mismatch is reported, because a
# changed baseline explains a changed rule snapshot but not the reverse, and a
# list of ten reasons is less actionable than the one that actually matters.
_FRESHNESS_CHECKS: tuple[tuple[str, StaleReason], ...] = (
    ("baseline_input_snapshot_hash", StaleReason.BASELINE_INPUTS_CHANGED),
    ("baseline_result_hash", StaleReason.BASELINE_RESULT_CHANGED),
    ("tax_year", StaleReason.TAX_YEAR_ROLLED_OVER),
    ("rule_snapshot_hash", StaleReason.RULE_SNAPSHOT_SUPERSEDED),
    ("engine_version", StaleReason.ENGINE_VERSION_CHANGED),
    ("reference_data_version", StaleReason.REFERENCE_DATA_CHANGED),
    ("objective_version", StaleReason.OBJECTIVE_POLICY_CHANGED),
    ("objective_code", StaleReason.OBJECTIVE_POLICY_CHANGED),
    ("lever_registry_version", StaleReason.LEVER_REGISTRY_CHANGED),
    ("assumption_set_hash", StaleReason.ASSUMPTION_SET_CHANGED),
)


@dataclass(frozen=True)
class FreshnessVerdict:
    status: FreshnessStatus
    stale_reason: StaleReason | None = None
    changed_fields: tuple[str, ...] = ()
    policy_version: str = FRESHNESS_POLICY_VERSION

    @property
    def is_current(self) -> bool:
        return self.status is FreshnessStatus.CURRENT


def evaluate_freshness(
    pinned: PinnedState,
    current: PinnedState,
    *,
    superseded_by: object | None = None,
) -> FreshnessVerdict:
    """Compare what a scenario pinned against what is true now.

    A pinned value of None means "this scenario did not pin that input", and it
    cannot then be said to have gone stale on it — absence of a pin is reported
    as such by `unknown`, never as freshness.
    """
    if superseded_by is not None:
        return FreshnessVerdict(
            FreshnessStatus.SUPERSEDED, StaleReason.SUPERSEDED_BY_REFRESH
        )

    unpinned = [
        name for name in ("baseline_input_snapshot_hash", "baseline_result_hash",
                          "rule_snapshot_hash", "engine_version")
        if getattr(pinned, name) is None
    ]
    if unpinned:
        # Not "fresh" and not "stale": we cannot tell, and saying so is the
        # honest answer.
        return FreshnessVerdict(FreshnessStatus.UNKNOWN, None, tuple(unpinned))

    changed: list[str] = []
    first_reason: StaleReason | None = None
    for name, reason in _FRESHNESS_CHECKS:
        pinned_value = getattr(pinned, name)
        current_value = getattr(current, name)
        if pinned_value is None or current_value is None:
            continue
        if pinned_value != current_value:
            changed.append(name)
            if first_reason is None:
                first_reason = reason

    if first_reason is None:
        return FreshnessVerdict(FreshnessStatus.CURRENT)
    return FreshnessVerdict(FreshnessStatus.STALE, first_reason, tuple(changed))


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ComparableScenario:
    """Just enough of a stored scenario to compare it, read from sealed rows."""

    scenario_id: object
    base_analysis_id: object
    tax_year: int | None
    jurisdiction: str | None
    objective_code: str | None
    objective_version: str | None
    result_schema_version: str | None
    baseline_input_snapshot_hash: str | None
    objective_value_baseline: Decimal | None
    objective_value_scenario: Decimal | None
    objective_delta: Decimal | None
    scenario_tax: Decimal | None
    label: str | None = None
    freshness_status: str = FreshnessStatus.UNKNOWN.value
    stale_reason_code: str | None = None


@dataclass(frozen=True)
class ComparisonResult:
    """A difference between two scenarios measured on the same objective."""

    left: ComparableScenario
    right: ComparableScenario
    objective_code: str
    objective_version: str
    objective_delta_left: Decimal
    objective_delta_right: Decimal
    difference: Decimal
    better: str                       # 'left' | 'right' | 'equivalent'
    both_current: bool
    stale_notices: tuple[str, ...] = field(default_factory=tuple)
    policy_version: str = COMPARISON_POLICY_VERSION


def assert_comparable(
    left: ComparableScenario, right: ComparableScenario
) -> None:
    """Refuse a comparison that would produce an artefact rather than a finding.

    Every check names a way the two results could differ for a reason that has
    nothing to do with the levers the user is actually comparing.
    """
    reasons: list[str] = []
    if left.scenario_id == right.scenario_id:
        reasons.append("a scenario cannot be compared with itself")
    if left.base_analysis_id != right.base_analysis_id:
        reasons.append(
            "different baselines: the scenarios were measured against different "
            "analyses, so the difference would not be attributable to the levers"
        )
    if left.baseline_input_snapshot_hash != right.baseline_input_snapshot_hash:
        reasons.append(
            "different baseline input snapshots: the same analysis produced "
            "different frozen inputs for these scenarios"
        )
    if left.tax_year != right.tax_year:
        reasons.append(
            f"different tax years ({left.tax_year} vs {right.tax_year}): rates, "
            "brackets and credits differ between years"
        )
    if left.jurisdiction != right.jurisdiction:
        reasons.append(
            f"different jurisdictions ({left.jurisdiction} vs {right.jurisdiction})"
        )
    if (left.objective_code, left.objective_version) != (
        right.objective_code, right.objective_version
    ):
        reasons.append(
            "different objective policies "
            f"({left.objective_code}@{left.objective_version} vs "
            f"{right.objective_code}@{right.objective_version}): the two deltas "
            "do not measure the same quantity"
        )
    if left.result_schema_version != right.result_schema_version:
        reasons.append(
            "different result schema versions "
            f"({left.result_schema_version} vs {right.result_schema_version})"
        )
    if left.objective_delta is None or right.objective_delta is None:
        reasons.append("one or both scenarios have no completed objective delta")
    if reasons:
        raise ScenariosNotComparable(reasons)


def compare(left: ComparableScenario, right: ComparableScenario) -> ComparisonResult:
    """Compare two scenarios, or refuse. Reads sealed values; recomputes nothing."""
    assert_comparable(left, right)

    delta_left = left.objective_delta.quantize(MONEY, ROUND_HALF_UP)
    delta_right = right.objective_delta.quantize(MONEY, ROUND_HALF_UP)
    difference = (delta_left - delta_right).quantize(MONEY, ROUND_HALF_UP)

    if difference > 0:
        better = "left"
    elif difference < 0:
        better = "right"
    else:
        better = "equivalent"

    notices: list[str] = []
    for side, scenario in (("left", left), ("right", right)):
        if scenario.freshness_status != FreshnessStatus.CURRENT.value:
            notices.append(
                f"{side} scenario is {scenario.freshness_status}"
                + (f" ({scenario.stale_reason_code})" if scenario.stale_reason_code else "")
            )

    return ComparisonResult(
        left=left, right=right,
        objective_code=left.objective_code,
        objective_version=left.objective_version,
        objective_delta_left=delta_left,
        objective_delta_right=delta_right,
        difference=difference,
        better=better,
        both_current=not notices,
        stale_notices=tuple(notices),
    )


__all__ = [
    "COMPARISON_POLICY_VERSION",
    "FRESHNESS_POLICY_VERSION",
    "ComparableScenario",
    "ComparisonResult",
    "FreshnessVerdict",
    "PinnedState",
    "ScenariosNotComparable",
    "assert_comparable",
    "compare",
    "evaluate_freshness",
]
