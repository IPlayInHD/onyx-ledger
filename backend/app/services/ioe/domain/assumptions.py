"""Structured assumption registry (architecture §15).

Free-text assumptions are never calculation inputs. Each assumption is a
registered CODE with a declared type, permitted values, default materiality, and
whether it can affect eligibility. An unregistered code is rejected, so an
assumption can never introduce an unbounded input into a calculation.

`display_note` is presentation only: it is excluded from canonical payloads, so
rewording a note can never change a result hash.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.ioe.domain.enums import (
    AssumptionCertainty,
    AssumptionSource,
    Materiality,
)
from app.services.ioe.domain.models import StructuredAssumption

ASSUMPTION_REGISTRY_VERSION = "1.0.0"


class AssumptionNotRegistered(KeyError):
    """The assumption code is not in the registry at this version."""


class AssumptionValidationError(ValueError):
    """The assumption value is the wrong type or outside permitted values."""


@dataclass(frozen=True)
class AssumptionSpec:
    code: str
    description: str
    value_type: str                       # 'boolean' | 'money' | 'rate' | 'text' | 'integer'
    default_materiality: Materiality = Materiality.MEDIUM
    affects_eligibility: bool = False
    choices: tuple[str, ...] = ()
    minimum: Decimal | None = None
    maximum: Decimal | None = None


_SPECS: dict[str, AssumptionSpec] = {
    spec.code: spec
    for spec in (
        AssumptionSpec(
            code="EMPLOYMENT_INCOME_CONSTANT",
            description="Employment income stays at its current level",
            value_type="boolean", default_materiality=Materiality.HIGH,
        ),
        AssumptionSpec(
            code="INDEXATION_CONSTANT",
            description="Bracket/credit indexation is unchanged in future years",
            value_type="boolean", default_materiality=Materiality.MEDIUM,
        ),
        AssumptionSpec(
            code="PROVINCE_UNCHANGED",
            description="Province of residence is unchanged over the horizon",
            value_type="boolean", default_materiality=Materiality.HIGH,
            affects_eligibility=True,
        ),
        AssumptionSpec(
            code="MARITAL_STATUS_UNCHANGED",
            description="Marital status is unchanged over the horizon",
            value_type="boolean", default_materiality=Materiality.MEDIUM,
            affects_eligibility=True,
        ),
        AssumptionSpec(
            code="CONTRIBUTION_ROOM_AVAILABLE",
            description="Stated registered-account contribution room is available",
            value_type="money", default_materiality=Materiality.HIGH,
            affects_eligibility=True, minimum=Decimal(0),
        ),
        AssumptionSpec(
            code="EXPECTED_RETURN_RATE",
            description="Assumed annual rate of return on investments",
            value_type="rate", default_materiality=Materiality.MEDIUM,
            minimum=Decimal(0), maximum=Decimal(1),
        ),
        AssumptionSpec(
            code="RETIREMENT_YEAR",
            description="The year retirement begins",
            value_type="integer", default_materiality=Materiality.HIGH,
        ),
        AssumptionSpec(
            code="EXPENSES_RECUR_ANNUALLY",
            description="The modelled expense recurs each year of the horizon",
            value_type="boolean", default_materiality=Materiality.MEDIUM,
        ),
    )
}


def registry_version() -> str:
    return ASSUMPTION_REGISTRY_VERSION


def get(code: str) -> AssumptionSpec:
    try:
        return _SPECS[code]
    except KeyError as exc:
        raise AssumptionNotRegistered(
            f"assumption '{code}' is not registered at v{ASSUMPTION_REGISTRY_VERSION}"
        ) from exc


def exists(code: str) -> bool:
    return code in _SPECS


def all_specs() -> tuple[AssumptionSpec, ...]:
    return tuple(_SPECS[code] for code in sorted(_SPECS))


def build(
    code: str,
    value: Any,
    *,
    source: AssumptionSource,
    certainty: AssumptionCertainty,
    materiality: Materiality | None = None,
    effective_period: str | None = None,
    sensitivity: Decimal | None = None,
    display_note: str | None = None,
) -> StructuredAssumption:
    """Validate against the registry and build the structured assumption."""
    spec = get(code)
    coerced = _coerce(spec, value)
    return StructuredAssumption(
        code=spec.code,
        value=coerced,
        source=source,
        certainty=certainty,
        materiality=materiality or spec.default_materiality,
        effective_period=effective_period,
        affects_eligibility=spec.affects_eligibility,
        sensitivity=sensitivity,
        display_note=display_note,
    )


def _coerce(spec: AssumptionSpec, value: Any) -> Any:
    if spec.value_type == "boolean":
        if not isinstance(value, bool):
            raise AssumptionValidationError(
                f"assumption '{spec.code}' expects a boolean, got {type(value).__name__}"
            )
        return value

    if spec.value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise AssumptionValidationError(
                f"assumption '{spec.code}' expects an integer, got {type(value).__name__}"
            )
        return value

    if spec.value_type in ("money", "rate"):
        try:
            number = value if isinstance(value, Decimal) else Decimal(str(value))
        except Exception as exc:  # noqa: BLE001
            raise AssumptionValidationError(
                f"assumption '{spec.code}' expects a number, got {value!r}"
            ) from exc
        if spec.minimum is not None and number < spec.minimum:
            raise AssumptionValidationError(
                f"assumption '{spec.code}' value {number} is below minimum {spec.minimum}"
            )
        if spec.maximum is not None and number > spec.maximum:
            raise AssumptionValidationError(
                f"assumption '{spec.code}' value {number} is above maximum {spec.maximum}"
            )
        # canonical payloads carry the string form so the scale is explicit
        return str(number)

    text = str(value)
    if spec.choices and text not in spec.choices:
        raise AssumptionValidationError(
            f"assumption '{spec.code}' must be one of {list(spec.choices)}"
        )
    return text
