"""Lever registry — the ONLY authority for what a hypothetical does to an input.

Rule data references a lever by CODE plus parameters (contract v2
`portfolio_lever_ref`); it never names an engine input field. This module owns
the mapping, so the set of fields any lever may write is a closed, reviewable
allow-list rather than something rule authors can extend (architecture §C).

Enforcement, in order:
  1. unknown `lever_code`                    → LeverNotRegistered
  2. parameter missing / wrong type / out of declared bounds → LeverValidationError
  3. a lever attempting to write a field outside its allow-list → LeverSafetyError

The registry operates on a plain ``dict`` of engine input fields so the domain
stays framework-free; the service layer adapts `TaxInput` to and from that dict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

LEVER_REGISTRY_VERSION = "1.0.0"


class LeverNotRegistered(KeyError):
    """A referenced lever code does not exist at this registry version."""


class LeverValidationError(ValueError):
    """A lever parameter is missing, mistyped, or outside its declared bounds."""


class LeverSafetyError(RuntimeError):
    """A lever attempted to write a field outside its allow-list."""


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: str = "money"                  # 'money' | 'rate' | 'text'
    required: bool = True
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class LeverSpec:
    code: str
    description: str
    writable_fields: tuple[str, ...]     # closed allow-list
    direction: str                       # 'increase' | 'decrease' | 'set'
    parameters: tuple[ParameterSpec, ...] = ()
    shared_resource_code: str | None = None
    portfolio_eligible: bool = True
    composite_children: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    effort_rating: int = 3
    # Applicability. Empty means "no restriction"; a populated set restricts the
    # lever to those jurisdictions / tax years.
    jurisdictions: tuple[str, ...] = ()
    tax_years: tuple[int, ...] = ()

    def applies_to(self, *, jurisdiction: str | None, tax_year: int | None) -> bool:
        if self.jurisdictions and jurisdiction is not None:
            if jurisdiction not in self.jurisdictions:
                return False
        if self.tax_years and tax_year is not None:
            if tax_year not in self.tax_years:
                return False
        return True

    def as_canonical(self) -> dict:
        return {
            "code": self.code,
            "direction": self.direction,
            "writable_fields": list(self.writable_fields),
            "shared_resource_code": self.shared_resource_code,
            "portfolio_eligible": self.portfolio_eligible,
            "composite_children": list(self.composite_children),
        }


# ---------------------------------------------------------------------------
# The registry. Levers here map cleanly onto existing engine input fields
# (decision D-4); levers needing new engine inputs are deliberately deferred.
# ---------------------------------------------------------------------------
_AMOUNT = ParameterSpec("amount", kind="money", required=True, minimum=Decimal(0))

_LEVERS: dict[str, LeverSpec] = {
    spec.code: spec
    for spec in (
        LeverSpec(
            code="INCREASE_RRSP_DEDUCTION",
            description="Contribute to an RRSP and deduct it this year",
            writable_fields=("rrsp_deduction",), direction="increase",
            parameters=(_AMOUNT,), shared_resource_code="RRSP_ROOM", effort_rating=2,
        ),
        LeverSpec(
            code="INCREASE_FHSA_DEDUCTION",
            description="Contribute to an FHSA and deduct it this year",
            writable_fields=("fhsa_deduction",), direction="increase",
            parameters=(_AMOUNT,), shared_resource_code="FHSA_ROOM", effort_rating=2,
        ),
        LeverSpec(
            code="INCREASE_DONATIONS",
            description="Make an eligible charitable donation",
            writable_fields=("donations",), direction="increase",
            parameters=(_AMOUNT,), shared_resource_code="DONATION_POOL", effort_rating=1,
        ),
        LeverSpec(
            code="INCREASE_MEDICAL_EXPENSES",
            description="Claim additional eligible medical expenses",
            writable_fields=("medical_expenses",), direction="increase",
            parameters=(_AMOUNT,), shared_resource_code="MEDICAL_POOL", effort_rating=1,
        ),
        LeverSpec(
            code="INCREASE_CHILDCARE",
            description="Claim additional eligible child-care expenses",
            writable_fields=("child_care",), direction="increase",
            parameters=(_AMOUNT,), shared_resource_code="CHILDCARE_POOL", effort_rating=1,
        ),
        LeverSpec(
            code="INCREASE_TUITION",
            description="Claim eligible tuition",
            writable_fields=("tuition",), direction="increase",
            parameters=(_AMOUNT,), shared_resource_code="TUITION_POOL", effort_rating=1,
        ),
        LeverSpec(
            code="INCREASE_BUSINESS_EXPENSES",
            description="Claim additional eligible self-employment expenses",
            writable_fields=("self_employment_expenses",), direction="increase",
            parameters=(_AMOUNT,), effort_rating=3,
        ),
        LeverSpec(
            code="REALIZE_CAPITAL_GAINS",
            description="Realize capital gains this year",
            writable_fields=("capital_gains",), direction="increase",
            parameters=(_AMOUNT,), effort_rating=3,
        ),
        LeverSpec(
            code="DEFER_CAPITAL_GAINS",
            description="Defer realizing capital gains to a later year",
            writable_fields=("capital_gains",), direction="decrease",
            parameters=(_AMOUNT,), effort_rating=3,
            conflicts_with=("REALIZE_CAPITAL_GAINS",),
        ),
        LeverSpec(
            code="ADJUST_ELIGIBLE_DIVIDENDS",
            description="Change eligible dividend income",
            writable_fields=("eligible_dividends",), direction="set",
            parameters=(_AMOUNT,), effort_rating=4,
        ),
        LeverSpec(
            code="ADJUST_NON_ELIGIBLE_DIVIDENDS",
            description="Change non-eligible dividend income",
            writable_fields=("non_eligible_dividends",), direction="set",
            parameters=(_AMOUNT,), effort_rating=4,
        ),
        LeverSpec(
            code="ADD_EMPLOYMENT_INCOME",
            description="Add employment income (e.g. a bonus)",
            writable_fields=("employment_income",), direction="increase",
            parameters=(_AMOUNT,), portfolio_eligible=False, effort_rating=4,
        ),
        LeverSpec(
            code="REMOVE_EMPLOYMENT_INCOME",
            description="Reduce employment income",
            writable_fields=("employment_income",), direction="decrease",
            parameters=(_AMOUNT,), portfolio_eligible=False, effort_rating=4,
            conflicts_with=("ADD_EMPLOYMENT_INCOME",),
        ),
        LeverSpec(
            code="CHANGE_PROVINCE",
            description="Model residency in a different province",
            writable_fields=("province",), direction="set",
            parameters=(ParameterSpec("province", kind="text", required=True),),
            portfolio_eligible=False, effort_rating=5,
        ),
        LeverSpec(
            code="RETIRE",
            description="Model retirement: employment income ends, pension begins",
            writable_fields=("employment_income", "pension_income"), direction="set",
            parameters=(
                ParameterSpec("pension_income", kind="money", required=True,
                              minimum=Decimal(0)),
            ),
            portfolio_eligible=False, effort_rating=5,
        ),
    )
}


def registry_version() -> str:
    return LEVER_REGISTRY_VERSION


def get(lever_code: str) -> LeverSpec:
    try:
        return _LEVERS[lever_code]
    except KeyError as exc:
        raise LeverNotRegistered(
            f"lever '{lever_code}' is not in registry v{LEVER_REGISTRY_VERSION}"
        ) from exc


def exists(lever_code: str) -> bool:
    return lever_code in _LEVERS


def all_levers() -> tuple[LeverSpec, ...]:
    return tuple(_LEVERS[code] for code in sorted(_LEVERS))


def validate_parameters(spec: LeverSpec, parameters: dict[str, Any]) -> dict[str, Any]:
    """Coerce and bounds-check parameters. Unknown parameters are rejected."""
    unknown = set(parameters) - {p.name for p in spec.parameters}
    if unknown:
        raise LeverValidationError(
            f"lever '{spec.code}': unknown parameter(s) {sorted(unknown)}"
        )

    resolved: dict[str, Any] = {}
    for p in spec.parameters:
        if p.name not in parameters or parameters[p.name] is None:
            if p.required:
                raise LeverValidationError(
                    f"lever '{spec.code}': parameter '{p.name}' is required"
                )
            continue
        raw = parameters[p.name]
        if p.kind in ("money", "rate"):
            try:
                value: Any = raw if isinstance(raw, Decimal) else Decimal(str(raw))
            except Exception as exc:  # noqa: BLE001
                raise LeverValidationError(
                    f"lever '{spec.code}': parameter '{p.name}' is not numeric: {raw!r}"
                ) from exc
            if p.minimum is not None and value < p.minimum:
                raise LeverValidationError(
                    f"lever '{spec.code}': '{p.name}' {value} is below minimum {p.minimum}"
                )
            if p.maximum is not None and value > p.maximum:
                raise LeverValidationError(
                    f"lever '{spec.code}': '{p.name}' {value} is above maximum {p.maximum}"
                )
        else:
            value = str(raw)
            if p.choices and value not in p.choices:
                raise LeverValidationError(
                    f"lever '{spec.code}': '{p.name}' must be one of {list(p.choices)}"
                )
        resolved[p.name] = value
    return resolved


@dataclass
class AppliedChange:
    """One field delta, recorded so a scenario is explainable and replayable."""

    lever_code: str
    field: str
    old_value: str | None
    new_value: str | None
    apply_order: int = 0

    def as_canonical(self) -> dict:
        return {
            "lever_code": self.lever_code,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "apply_order": self.apply_order,
        }


@dataclass
class LeverApplicationResult:
    inputs: dict[str, Any]
    changes: list[AppliedChange] = field(default_factory=list)


def assert_applicable(
    spec: LeverSpec, *, jurisdiction: str | None, tax_year: int | None
) -> None:
    """A lever may only be applied where the registry says it applies."""
    if not spec.applies_to(jurisdiction=jurisdiction, tax_year=tax_year):
        raise LeverValidationError(
            f"lever '{spec.code}' does not apply to jurisdiction={jurisdiction!r}, "
            f"tax_year={tax_year!r}"
        )


def apply_lever(
    inputs: dict[str, Any],
    lever_code: str,
    parameters: dict[str, Any],
    *,
    apply_order: int = 0,
    jurisdiction: str | None = None,
    tax_year: int | None = None,
) -> LeverApplicationResult:
    """Apply one lever to a COPY of `inputs`; the original is never mutated.

    Writes are restricted to the lever's declared `writable_fields`; any attempt
    to touch another field raises LeverSafetyError. A COMPOSITE lever is applied
    ATOMICALLY: every child succeeds or the caller's input is returned untouched,
    so a half-applied composite can never reach the engine.
    """
    spec = get(lever_code)
    assert_applicable(spec, jurisdiction=jurisdiction, tax_year=tax_year)
    resolved = validate_parameters(spec, parameters)
    clone = dict(inputs)
    changes: list[AppliedChange] = []

    if spec.composite_children:
        # Atomic: build the whole composite on a scratch copy first. If any child
        # fails validation or safety, nothing is applied at all.
        scratch = dict(inputs)
        staged: list[AppliedChange] = []
        for child in spec.composite_children:
            nested = apply_lever(
                scratch, child, parameters, apply_order=apply_order,
                jurisdiction=jurisdiction, tax_year=tax_year,
            )
            scratch = nested.inputs
            staged.extend(nested.changes)
        return LeverApplicationResult(scratch, staged)

    for target_field in spec.writable_fields:
        old = clone.get(target_field)
        new = _new_value(spec, target_field, old, resolved)
        if new is None:
            continue
        _assert_writable(spec, target_field)
        clone[target_field] = new
        changes.append(AppliedChange(
            lever_code=spec.code, field=target_field,
            old_value=None if old is None else str(old),
            new_value=str(new), apply_order=apply_order,
        ))

    # Defence in depth: nothing outside the allow-list may have moved.
    for key, value in clone.items():
        if value != inputs.get(key) and key not in spec.writable_fields:
            raise LeverSafetyError(
                f"lever '{spec.code}' wrote field '{key}', which is not in its "
                f"allow-list {list(spec.writable_fields)}"
            )
    return LeverApplicationResult(clone, changes)


def _assert_writable(spec: LeverSpec, target_field: str) -> None:
    if target_field not in spec.writable_fields:
        raise LeverSafetyError(
            f"lever '{spec.code}' may not write field '{target_field}'"
        )


def _new_value(spec: LeverSpec, target_field: str, old: Any, resolved: dict[str, Any]):
    """Compute the new value for one field under the lever's direction."""
    # A composite lever names its parameter after the field it sets.
    if target_field in resolved:
        return resolved[target_field]

    if spec.direction == "set":
        if "province" in resolved and target_field == "province":
            return resolved["province"]
        if "amount" in resolved:
            return resolved["amount"]
        # RETIRE-style: a field with no parameter of its own is zeroed
        return Decimal(0) if isinstance(old, Decimal) else old

    amount = resolved.get("amount")
    if amount is None:
        return None
    base = old if isinstance(old, Decimal) else Decimal(str(old or 0))
    if spec.direction == "increase":
        return base + amount
    if spec.direction == "decrease":
        return max(Decimal(0), base - amount)
    raise LeverValidationError(f"lever '{spec.code}': unknown direction '{spec.direction}'")


def apply_all(
    inputs: dict[str, Any],
    applications: list[tuple[str, dict[str, Any]]],
    *,
    jurisdiction: str | None = None,
    tax_year: int | None = None,
) -> LeverApplicationResult:
    """Apply levers in the given order, accumulating the recorded changes.

    Atomic as a whole: a failure part-way leaves the caller's input untouched.
    """
    current = dict(inputs)
    changes: list[AppliedChange] = []
    for order, (code, params) in enumerate(applications):
        result = apply_lever(
            current, code, params, apply_order=order,
            jurisdiction=jurisdiction, tax_year=tax_year,
        )
        current = result.inputs
        changes.extend(result.changes)
    return LeverApplicationResult(current, changes)
