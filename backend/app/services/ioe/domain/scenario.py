"""Scenario specification: what a user is allowed to ask for (§P5).

The whole security posture of scenario simulation rests on one decision: a
scenario request names **typed lever codes with structured parameters**, and
nothing else. It never carries an engine field path, a JSON patch, a formula, an
expression, or any other data that some later stage would have to execute or
interpret. The lever registry — pinned and versioned — is the only thing that
knows how a code touches an input.

`ScenarioSpec.parse()` is therefore written as a *rejecting* parser rather than
a permissive one: anything that is not a registered code with declared
parameters is refused at the boundary, with a reason, before it reaches any
component that could act on it.

Label and note are held on the spec for convenience but are excluded from
`canonical_for_hash()`. Renaming a scenario must not change its identity or
invalidate its evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import levers as lever_registry

SCENARIO_SPEC_VERSION = "1.0.0"
SCENARIO_RESULT_SCHEMA_VERSION = "1.0.0"

MAX_LEVERS_PER_SCENARIO = 25
MAX_ASSUMPTIONS_PER_SCENARIO = 25

# Shapes that must never appear in a scenario request. Listed explicitly so the
# refusal message can name what was rejected instead of failing obscurely.
_FORBIDDEN_KEYS = frozenset({
    "field", "fields", "field_path", "path", "target", "target_field",
    "patch", "patches", "op", "ops", "operation", "operations",
    "formula", "expression", "expr", "eval", "code", "script", "lambda",
    "set", "mutation", "mutations", "apply", "__class__",
})


class ScenarioSpecError(ValueError):
    """A scenario request is not expressible as typed levers and assumptions."""


class FreshnessStatus(StrEnum):
    UNKNOWN = "unknown"
    CURRENT = "current"
    STALE = "stale"
    SUPERSEDED = "superseded"


class StaleReason(StrEnum):
    BASELINE_INPUTS_CHANGED = "BASELINE_INPUTS_CHANGED"
    BASELINE_RESULT_CHANGED = "BASELINE_RESULT_CHANGED"
    RULE_SNAPSHOT_SUPERSEDED = "RULE_SNAPSHOT_SUPERSEDED"
    ENGINE_VERSION_CHANGED = "ENGINE_VERSION_CHANGED"
    REFERENCE_DATA_CHANGED = "REFERENCE_DATA_CHANGED"
    LEVER_REGISTRY_CHANGED = "LEVER_REGISTRY_CHANGED"
    OBJECTIVE_POLICY_CHANGED = "OBJECTIVE_POLICY_CHANGED"
    ASSUMPTION_SET_CHANGED = "ASSUMPTION_SET_CHANGED"
    TAX_YEAR_ROLLED_OVER = "TAX_YEAR_ROLLED_OVER"
    SUPERSEDED_BY_REFRESH = "SUPERSEDED_BY_REFRESH"


@dataclass(frozen=True)
class LeverRequest:
    """One typed lever application, in a fixed position."""

    apply_order: int
    lever_code: str
    parameters: dict[str, Decimal | str]

    def as_canonical(self) -> dict:
        return {
            "apply_order": self.apply_order,
            "lever_code": self.lever_code,
            "parameters": {
                name: (
                    c.money(value) if isinstance(value, Decimal)
                    else c.normalize_text(value)
                )
                for name, value in sorted(self.parameters.items())
            },
        }


@dataclass(frozen=True)
class AssumptionRequest:
    """A registered assumption code plus exactly one typed value."""

    assumption_code: str
    value_number: Decimal | None = None
    value_text: str | None = None
    value_boolean: bool | None = None
    materiality: str = "medium"
    source: str = "user"
    certainty: str = "user_asserted"
    affects_eligibility: bool = False

    def as_canonical(self) -> dict:
        return {
            "assumption_code": self.assumption_code,
            "value_number": (
                c.quantity(self.value_number) if self.value_number is not None else None
            ),
            "value_text": (
                c.normalize_text(self.value_text) if self.value_text is not None else None
            ),
            "value_boolean": self.value_boolean,
            "materiality": self.materiality,
            "source": self.source,
            "certainty": self.certainty,
            "affects_eligibility": self.affects_eligibility,
        }


@dataclass(frozen=True)
class ScenarioSpec:
    """A validated scenario request. Constructing one is the security boundary."""

    levers: tuple[LeverRequest, ...]
    assumptions: tuple[AssumptionRequest, ...] = ()
    # Descriptive only, and deliberately outside the hash.
    label: str | None = None
    note: str | None = None
    spec_version: str = SCENARIO_SPEC_VERSION

    def canonical_for_hash(self) -> list[dict]:
        """The ordered lever/assumption content that identifies this scenario.

        Label and note are absent by construction, not by filtering: they are
        never added, so a rename can never change the hash.
        """
        return [
            {"levers": [x.as_canonical() for x in self.levers]},
            {"assumptions": [a.as_canonical() for a in self.assumptions]},
            {"spec_version": self.spec_version},
        ]

    @property
    def lever_codes(self) -> tuple[str, ...]:
        return tuple(x.lever_code for x in self.levers)

    # ------------------------------------------------------------------ parse
    @classmethod
    def parse(
        cls,
        levers: Any,
        *,
        assumptions: Any = None,
        label: str | None = None,
        note: str | None = None,
        jurisdiction: str | None = None,
        tax_year: int | None = None,
    ) -> ScenarioSpec:
        """Build a spec from untrusted input, refusing anything else.

        Rejects, with a reason: unknown lever codes, levers not eligible for
        scenario use in this jurisdiction/tax year, undeclared or out-of-bounds
        parameters, and any request shaped like a field path, patch, formula, or
        other executable mutation.
        """
        if not isinstance(levers, (list, tuple)) or not levers:
            raise ScenarioSpecError(
                "a scenario must declare at least one lever, as a list"
            )
        if len(levers) > MAX_LEVERS_PER_SCENARIO:
            raise ScenarioSpecError(
                f"a scenario may declare at most {MAX_LEVERS_PER_SCENARIO} levers"
            )

        parsed: list[LeverRequest] = []
        for index, raw in enumerate(levers):
            parsed.append(cls._parse_lever(
                raw, index, jurisdiction=jurisdiction, tax_year=tax_year
            ))

        parsed_assumptions = cls._parse_assumptions(assumptions)
        return cls(
            levers=tuple(parsed),
            assumptions=parsed_assumptions,
            label=label,
            note=note,
        )

    @staticmethod
    def _parse_lever(
        raw: Any, index: int, *, jurisdiction: str | None, tax_year: int | None
    ) -> LeverRequest:
        if not isinstance(raw, dict):
            raise ScenarioSpecError(
                f"lever at position {index} must be an object with a lever_code"
            )
        _refuse_executable_shapes(raw, f"lever at position {index}")

        code = raw.get("lever_code")
        if not isinstance(code, str) or not code:
            raise ScenarioSpecError(
                f"lever at position {index} has no lever_code; a scenario names "
                "levers by CODE, never by engine field"
            )
        if not lever_registry.exists(code):
            # An unknown code is refused, never guessed at.
            raise ScenarioSpecError(f"unknown lever_code '{code}'")

        spec = lever_registry.get(code)
        if not spec.applies_to(jurisdiction=jurisdiction, tax_year=tax_year):
            raise ScenarioSpecError(
                f"lever '{code}' does not apply to jurisdiction={jurisdiction} "
                f"tax_year={tax_year}"
            )

        raw_parameters = raw.get("parameters", {})
        if not isinstance(raw_parameters, dict):
            raise ScenarioSpecError(f"lever '{code}': parameters must be an object")
        _refuse_executable_shapes(raw_parameters, f"lever '{code}' parameters")

        declared = {p.name for p in spec.parameters}
        unknown = set(raw_parameters) - declared
        if unknown:
            raise ScenarioSpecError(
                f"lever '{code}': undeclared parameter(s) {sorted(unknown)}; "
                f"the registry declares {sorted(declared)}"
            )

        # Coerce and bounds-check through the registry's own validation, so a
        # scenario parameter is held to exactly the same rules as a portfolio one.
        coerced = lever_registry.validate_parameters(spec, raw_parameters)
        return LeverRequest(
            apply_order=index, lever_code=code, parameters=coerced
        )

    @staticmethod
    def _parse_assumptions(assumptions: Any) -> tuple[AssumptionRequest, ...]:
        if assumptions is None:
            return ()
        if not isinstance(assumptions, (list, tuple)):
            raise ScenarioSpecError("assumptions must be a list")
        if len(assumptions) > MAX_ASSUMPTIONS_PER_SCENARIO:
            raise ScenarioSpecError(
                f"at most {MAX_ASSUMPTIONS_PER_SCENARIO} assumptions per scenario"
            )
        from app.services.ioe.domain import assumptions as assumption_registry

        out: list[AssumptionRequest] = []
        seen: set[str] = set()
        for index, raw in enumerate(assumptions):
            if not isinstance(raw, dict):
                raise ScenarioSpecError(f"assumption at position {index} must be an object")
            _refuse_executable_shapes(raw, f"assumption at position {index}")

            code = raw.get("assumption_code")
            if not isinstance(code, str) or not code:
                raise ScenarioSpecError(
                    f"assumption at position {index} has no assumption_code"
                )
            if not assumption_registry.exists(code):
                raise ScenarioSpecError(f"unregistered assumption_code '{code}'")
            if code in seen:
                raise ScenarioSpecError(f"duplicate assumption_code '{code}'")
            seen.add(code)

            typed = [
                raw.get("value_number"), raw.get("value_text"), raw.get("value_boolean"),
            ]
            if sum(1 for v in typed if v is not None) != 1:
                raise ScenarioSpecError(
                    f"assumption '{code}' must carry exactly one typed value "
                    "(value_number, value_text, or value_boolean)"
                )
            number = raw.get("value_number")
            if number is not None and not isinstance(number, (Decimal, int)):
                raise ScenarioSpecError(
                    f"assumption '{code}': value_number must be a Decimal, "
                    "never a float or a string expression"
                )
            out.append(AssumptionRequest(
                assumption_code=code,
                value_number=Decimal(number) if number is not None else None,
                value_text=raw.get("value_text"),
                value_boolean=raw.get("value_boolean"),
                materiality=raw.get("materiality", "medium"),
                source=raw.get("source", "user"),
                certainty=raw.get("certainty", "user_asserted"),
                affects_eligibility=bool(raw.get("affects_eligibility", False)),
            ))
        # Sorted so two requests differing only in assumption order are the same
        # scenario, and hash identically.
        return tuple(sorted(out, key=lambda a: a.assumption_code))


def _refuse_executable_shapes(payload: dict, where: str) -> None:
    """Refuse any key that would name a field, a patch, or an expression.

    This is a shape check, not a sanitizer. Nothing is stripped and nothing is
    escaped — a request containing such a key is rejected whole, because a
    request that wants to name an engine field is not a scenario request.
    """
    offending = sorted(_FORBIDDEN_KEYS.intersection(map(str, payload)))
    if offending:
        raise ScenarioSpecError(
            f"{where}: keys {offending} are not accepted. A scenario declares "
            "typed lever codes and structured assumptions; it never supplies "
            "field paths, patches, formulas, or executable mutation data."
        )
    for key, value in payload.items():
        if callable(value):
            raise ScenarioSpecError(f"{where}: '{key}' is callable and is not accepted")
        if isinstance(value, float):
            raise ScenarioSpecError(
                f"{where}: '{key}' is a float; money and rates must be Decimal"
            )


__all__ = [
    "MAX_ASSUMPTIONS_PER_SCENARIO",
    "MAX_LEVERS_PER_SCENARIO",
    "SCENARIO_RESULT_SCHEMA_VERSION",
    "SCENARIO_SPEC_VERSION",
    "AssumptionRequest",
    "FreshnessStatus",
    "LeverRequest",
    "ScenarioSpec",
    "ScenarioSpecError",
    "StaleReason",
]
