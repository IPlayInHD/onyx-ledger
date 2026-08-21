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

# ---------------------------------------------------------------------------
# Scenario result schema versions
#
# TWO DIFFERENT QUESTIONS, kept apart on purpose. "What do new writes use" and
# "what can replay still interpret" are not the same, and conflating them is the
# exact defect this vocabulary exists to prevent: `canonical_result` used to
# read the current-write constant while canonicalizing HISTORICAL artifacts, so
# bumping the constant would have silently rewritten the contract every sealed
# scenario was hashed under.
# ---------------------------------------------------------------------------
SCENARIO_RESULT_SCHEMA_V1 = "1.0.0"
SCENARIO_RESULT_SCHEMA_V2 = "2.0.0"
SCENARIO_RESULT_SCHEMA_V3 = "3.0.0"

#: What NEW seals are written as. v3 since Entry 12B: the sealed derived state
#: now carries the BASELINE opportunity set alongside the counterfactual one, so
#: a Before-You-Act comparison has an authoritative frozen source on both sides
#: instead of an absent one that a comparator would read as additions.
#:
#: v2 IS NOT REINTERPRETED. Its outer payload shape is unchanged and every
#: sealed v2 row still verifies under v2 — the difference lives inside the
#: derived state, whose own schema version dispatches its canonical form.
#:
#: MOVING THIS CONSTANT IS A PRIVACY EVENT, not only a format change. A v2
#: verification reads `ioe.scenario_result`, which a v1 verification never
#: touches, so the table becomes a replay dependency the moment this line
#: changes. `tests/privacy/test_verification_consumers.py` holds the
#: classification to whatever this says — reclassify first, move this second.
#: v3 reads the same table as v2, so the consumer set does not widen again.
CURRENT_SCENARIO_RESULT_SCHEMA_VERSION = SCENARIO_RESULT_SCHEMA_V3

#: What REPLAY can interpret. Strictly a superset of the write version.
SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS = frozenset({
    SCENARIO_RESULT_SCHEMA_V1, SCENARIO_RESULT_SCHEMA_V2,
    SCENARIO_RESULT_SCHEMA_V3,
})

#: Which versions BIND a counterfactual derived state.
#:
#: Named because the question "does this version carry sealed derived state"
#: was previously asked as `== SCENARIO_RESULT_SCHEMA_V2` in five places. The
#: moment a third such version existed, every one of those became a silent
#: "no" for v3 — a v3 scenario would have sealed its evidence and then had
#: replay decline to read it. One constant, asked once per site.
DERIVED_STATE_BEARING_VERSIONS = frozenset({
    SCENARIO_RESULT_SCHEMA_V2, SCENARIO_RESULT_SCHEMA_V3,
})

# DELIBERATELY ABSENT: a bare `SCENARIO_RESULT_SCHEMA_VERSION`.
#
# It used to exist as an alias for v1 and it was a trap. A reverted activation
# attempt flipped the write version and left THREE sites still writing the
# alias — the version manifest and both row-level persistence sites — so new
# scenarios stored "I am v1" beside a hash computed as v2, and replay correctly
# refused them with RESULT_HASH_MISMATCH. The bug was not the three lines; it
# was that two constants both read as plausible answers to "what version is
# this write". So there is now exactly one answer per question:
#
#     new writes          CURRENT_SCENARIO_RESULT_SCHEMA_VERSION
#     historical replay   the version stored on the row
#     protocol dispatch   SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS
#
# Adding a general-purpose alias back would restore the ambiguity.


class UnsupportedResultSchemaVersion(ValueError):
    """A sealed artifact names a schema version this build cannot interpret.

    Raised rather than resolved. Falling back to v1, to v2, or to "current"
    would produce a hash under a contract the artifact was never sealed with,
    and report the resulting mismatch as though the data had changed.
    """


def _canonical_result_v1(computed: dict) -> dict:
    """THE FROZEN v1 PROTOCOL. Historical, and therefore immutable.

    Every sealed v1 scenario in the database was hashed from exactly this
    shape. Renaming a key, reordering a field, changing a null, or tightening a
    Decimal here silently invalidates all of them, so nothing in this function
    may be tidied. `tests/golden/scenario_result_v1_vectors.json` holds five
    vectors captured before this function was ever touched, and they are the
    authority — if they disagree with this code, this code is wrong.
    """
    breakdown = computed["support"]
    return {
        "scenario_tax": c.money(computed["scenario_tax"]),
        "tax_delta": c.money(computed["tax_delta"]),
        "objective_value_baseline": c.money(computed["objective_baseline"]),
        "objective_value_scenario": c.money(computed["objective_scenario"]),
        "objective_delta": c.money(computed["objective_delta"]),
        "result_schema_version": SCENARIO_RESULT_SCHEMA_V1,
        "support": {
            "raw_support_score": c.rate(breakdown.raw_support_score),
            "assumption_adjusted_score": c.rate(
                breakdown.assumption_adjusted_score
            ),
            "display_support_score": c.rate(breakdown.display_support_score),
            "cap_applied": breakdown.cap_applied,
            "cap_reason_code": breakdown.cap_reason_code,
        },
        "changes": [
            {
                "apply_order": ch.apply_order,
                "lever_code": ch.lever_code,
                "field": ch.field,
                "new_value": str(ch.new_value),
            }
            for ch in computed["changes"]
        ],
    }


def _canonical_result_v2(
    computed: dict, *, counterfactual_derived_state_hash: str
) -> dict:
    """v1's semantics plus the binding that makes v2 worth having.

    The outer payload carries the derived state's HASH, not a second copy of the
    artifact: the counterfactual derived state already has its own
    domain-separated hash, and duplicating a 21KB payload into the result hash
    would buy nothing except a larger hash input.
    """
    payload = _canonical_result_v1(computed)
    payload["result_schema_version"] = SCENARIO_RESULT_SCHEMA_V2
    payload["counterfactual_derived_state_hash"] = counterfactual_derived_state_hash
    return payload


def _canonical_result_v3(
    computed: dict, *, counterfactual_derived_state_hash: str
) -> dict:
    """v2's OUTER SHAPE EXACTLY, under a new version.

    Nothing is added out here, and that is deliberate. What v3 adds — the sealed
    BASELINE opportunity set — lives inside the counterfactual derived state,
    which already has its own domain-separated hash that this payload binds. So
    the only outer differences are the version string and the digest that
    version's derived-state contract produces.

    Written as its own function rather than as `_canonical_result_v2` with a
    parameter: v2 is now frozen historical protocol, and a shared body is one
    edit away from silently rewriting the contract every sealed v2 row was
    hashed under. That is exactly the defect the version dispatch exists for.
    """
    payload = _canonical_result_v1(computed)
    payload["result_schema_version"] = SCENARIO_RESULT_SCHEMA_V3
    payload["counterfactual_derived_state_hash"] = counterfactual_derived_state_hash
    return payload


def canonical_scenario_result(
    computed: dict,
    *,
    result_schema_version: str,
    counterfactual_derived_state_hash: str | None = None,
) -> dict:
    """THE single version-dispatch authority for scenario result canonicalization.

    `result_schema_version` is REQUIRED and has no default. That is the whole
    fix: a caller must say which contract it means, so creation can say "what I
    write" and replay can say "what this row was sealed under", and neither can
    accidentally inherit the other's answer from a module constant.

    Creation, replay and integrity all arrive here. Integrity verifies THROUGH
    replay, so there is one interpretation of any given version — a second
    dispatcher elsewhere is what would let two components disagree about what a
    sealed artifact means.
    """
    if result_schema_version == SCENARIO_RESULT_SCHEMA_V1:
        if counterfactual_derived_state_hash is not None:
            # Fail closed rather than ignore. Silently dropping it would let a
            # caller believe v2 evidence was bound when the bytes say v1.
            raise UnsupportedResultSchemaVersion(
                "a v1 scenario result cannot carry a counterfactual derived-state "
                "hash: v1 is frozen historical protocol and must not absorb v2 "
                "semantics"
            )
        return _canonical_result_v1(computed)

    if result_schema_version == SCENARIO_RESULT_SCHEMA_V2:
        if not counterfactual_derived_state_hash:
            raise UnsupportedResultSchemaVersion(
                "a v2 scenario result requires a counterfactual derived-state "
                "hash: v2 exists to bind that evidence, so sealing without it "
                "would claim a binding that is not there"
            )
        return _canonical_result_v2(
            computed,
            counterfactual_derived_state_hash=counterfactual_derived_state_hash,
        )

    if result_schema_version == SCENARIO_RESULT_SCHEMA_V3:
        if not counterfactual_derived_state_hash:
            raise UnsupportedResultSchemaVersion(
                "a v3 scenario result requires a counterfactual derived-state "
                "hash: v3 exists to bind BOTH sides' sealed opportunity sets, "
                "so sealing without it would claim a binding that is not there"
            )
        return _canonical_result_v3(
            computed,
            counterfactual_derived_state_hash=counterfactual_derived_state_hash,
        )

    raise UnsupportedResultSchemaVersion(
        f"unsupported scenario result schema version: {result_schema_version!r}; "
        f"this build interprets {sorted(SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS)}"
    )

MAX_LEVERS_PER_SCENARIO = 25
MAX_ASSUMPTIONS_PER_SCENARIO = 25

# Closed assumption-origin vocabularies. `source` says WHO put the assumption
# on the scenario and `certainty` says WHY its value is believed; downstream
# language (sealed records, the explanation renderer) keys off both, so an
# out-of-vocabulary value is refused rather than stored and interpreted later.
ASSUMPTION_SOURCES = frozenset({"user", "platform", "analysis"})
ASSUMPTION_CERTAINTIES = frozenset({
    "user_asserted", "platform_default", "derived_from_data", "statutory_known",
})
ASSUMPTION_MATERIALITIES = frozenset({"high", "medium", "low"})

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
    # A newer analysis exists for this year. Deliberately distinct from
    # BASELINE_INPUTS_CHANGED: "your figures moved" sends a user to check their
    # data, "a newer analysis exists" sends them to open it.
    NEWER_ANALYSIS_AVAILABLE = "NEWER_ANALYSIS_AVAILABLE"
    RULE_SNAPSHOT_SUPERSEDED = "RULE_SNAPSHOT_SUPERSEDED"
    ENGINE_VERSION_CHANGED = "ENGINE_VERSION_CHANGED"
    REFERENCE_DATA_CHANGED = "REFERENCE_DATA_CHANGED"
    LEVER_REGISTRY_CHANGED = "LEVER_REGISTRY_CHANGED"
    OBJECTIVE_POLICY_CHANGED = "OBJECTIVE_POLICY_CHANGED"
    ASSUMPTION_SET_CHANGED = "ASSUMPTION_SET_CHANGED"
    RELATIONSHIP_REGISTRY_CHANGED = "RELATIONSHIP_REGISTRY_CHANGED"
    SUPPORT_SCORE_POLICY_CHANGED = "SUPPORT_SCORE_POLICY_CHANGED"
    PROJECTION_METHODOLOGY_CHANGED = "PROJECTION_METHODOLOGY_CHANGED"
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
    """A registered assumption code plus exactly one typed value.

    Origin fields are validated against the closed vocabularies on EVERY
    construction — platform code paths included — because a sealed record
    carrying an unknown origin would be uninterpretable by everything that
    renders it. WHO may claim which origin is enforced one level up, at the
    parser that user payloads cross.
    """

    assumption_code: str
    value_number: Decimal | None = None
    value_text: str | None = None
    value_boolean: bool | None = None
    materiality: str = "medium"
    source: str = "user"
    certainty: str = "user_asserted"
    affects_eligibility: bool = False

    def __post_init__(self) -> None:
        if self.source not in ASSUMPTION_SOURCES:
            raise ScenarioSpecError(
                f"assumption '{self.assumption_code}': source must be one of "
                f"{sorted(ASSUMPTION_SOURCES)}, not {self.source!r}"
            )
        if self.certainty not in ASSUMPTION_CERTAINTIES:
            raise ScenarioSpecError(
                f"assumption '{self.assumption_code}': certainty must be one of "
                f"{sorted(ASSUMPTION_CERTAINTIES)}, not {self.certainty!r}"
            )
        if self.materiality not in ASSUMPTION_MATERIALITIES:
            raise ScenarioSpecError(
                f"assumption '{self.assumption_code}': materiality must be one "
                f"of {sorted(ASSUMPTION_MATERIALITIES)}, not {self.materiality!r}"
            )

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
        trusted_provenance: bool = False,
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

        parsed_assumptions = cls._parse_assumptions(
            assumptions, trusted_provenance=trusted_provenance
        )
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
    def _parse_assumptions(
        assumptions: Any, *, trusted_provenance: bool = False
    ) -> tuple[AssumptionRequest, ...]:
        """`trusted_provenance` distinguishes the two callers this parser has.

        False — the default — is the UNTRUSTED boundary: an API payload, whose
        assumptions are by definition the caller's own declarations, so
        platform/statutory origin labels are refused. True is for rebuilding a
        spec from SEALED STORED ROWS the platform itself wrote (replay,
        refresh): those rows legitimately carry the platform-attached origins
        the room policy sealed, and re-litigating them would make sealed
        scenarios unreplayable. The closed-vocabulary check in
        `AssumptionRequest.__post_init__` applies to both.
        """
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
            materiality = raw.get("materiality", "medium")
            if materiality not in ASSUMPTION_MATERIALITIES:
                raise ScenarioSpecError(
                    f"assumption '{code}': materiality must be one of "
                    f"{sorted(ASSUMPTION_MATERIALITIES)}"
                )
            # Origin integrity: on the untrusted path everything this parser
            # accepts is BY DEFINITION the caller's own declaration. Platform
            # and statutory provenance are attached by platform code
            # constructing AssumptionRequest directly; accepting those labels
            # from a payload would let it impersonate governed provenance, and
            # the sealed record would then misstate who said the value and why
            # it is believed.
            source = raw.get("source", "user")
            certainty = raw.get("certainty", "user_asserted")
            if not trusted_provenance:
                if source != "user":
                    raise ScenarioSpecError(
                        f"assumption '{code}': source must be 'user' — an "
                        "API-supplied assumption is the caller's own "
                        "declaration; platform/analysis provenance is attached "
                        "by the platform, never claimed through a payload"
                    )
                if certainty != "user_asserted":
                    raise ScenarioSpecError(
                        f"assumption '{code}': certainty must be "
                        "'user_asserted' — platform_default, derived_from_data "
                        "and statutory_known cannot be claimed through a "
                        "payload"
                    )
                source = "user"
                certainty = "user_asserted"
            out.append(AssumptionRequest(
                assumption_code=code,
                value_number=Decimal(number) if number is not None else None,
                value_text=raw.get("value_text"),
                value_boolean=raw.get("value_boolean"),
                materiality=materiality,
                source=source,
                certainty=certainty,
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
    "ASSUMPTION_CERTAINTIES",
    "ASSUMPTION_MATERIALITIES",
    "ASSUMPTION_SOURCES",
    "CURRENT_SCENARIO_RESULT_SCHEMA_VERSION",
    "DERIVED_STATE_BEARING_VERSIONS",
    "MAX_ASSUMPTIONS_PER_SCENARIO",
    "MAX_LEVERS_PER_SCENARIO",
    "SCENARIO_RESULT_SCHEMA_V1",
    "SCENARIO_RESULT_SCHEMA_V2",
    "SCENARIO_RESULT_SCHEMA_V3",
    "SCENARIO_SPEC_VERSION",
    "SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS",
    "AssumptionRequest",
    "FreshnessStatus",
    "LeverRequest",
    "ScenarioSpec",
    "ScenarioSpecError",
    "StaleReason",
    "UnsupportedResultSchemaVersion",
    "canonical_scenario_result",
]
