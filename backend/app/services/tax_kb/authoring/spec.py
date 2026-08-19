"""The governed authoring contract — what an author submits, and its identity.

One aggregate, validated whole. The alternative the repository already offered
was to insert a rule version, then its condition groups, then its conditions,
outcomes, actions, documents, dependencies and deadlines, each a separate
partially-valid write. That makes "is this rule coherent?" a question nobody can
ask, because there is no moment at which the answer is defined. Here the answer
is defined before anything is written.

Three properties do the work:

**Unknown fields are refused.** A field an author wrote and the pipeline ignored
is a silent disagreement about what the rule says. `legal_authority_rank: 1`
means something to whoever typed it; dropping it publishes a rule its author
would not recognize.

**Decimals are parsed from strings, never floats.** `0.1 + 0.2` is not `0.3`,
and tax is not a domain where that is acceptable. A `float` anywhere in a
submitted payload is refused where it appears rather than quietly coerced.

**Identity is semantic.** `spec_identity()` hashes what the knowledge MEANS —
not the order keys arrived in, not who submitted it, not when. Publication binds
to that digest, so a draft edited after validation cannot reach production
wearing its predecessor's approval (§71).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from app.services.ioe.domain import canonical as c
from app.services.tax_kb.authoring.codes import Family, ValidationCode

#: The authoring contract's own version. Independent of the rule, scenario,
#: assurance, lifecycle, retention and source-manifest versions: a manifest
#: written against a different authoring contract is refused, not reinterpreted.
KNOWLEDGE_SPEC_SCHEMA_VERSION = "1.0.0"

#: How deep a condition tree may nest. Not a style rule — an unbounded tree is
#: an unbounded evaluation, and the depth at which a human can still say what a
#: rule means is well under this.
MAX_CONDITION_DEPTH = 5


class SpecError(ValueError):
    """A submitted payload could not be read as a governed specification.

    Carries a `ValidationCode` so a parse refusal lands in the publishability
    report as a coded finding rather than an exception an operator has to read
    a stack trace to understand.
    """

    def __init__(self, code: ValidationCode, detail: str, *,
                 family: Family = Family.STRUCTURE, object_ref: str = ""):
        self.code = code
        self.detail = detail
        self.family = family
        self.object_ref = object_ref
        super().__init__(f"{code}: {detail}")


# ---------------------------------------------------------------------------
# Primitive readers — every one fails closed
# ---------------------------------------------------------------------------
class _Fields:
    """A payload reader that refuses to leave a field unread.

    Reading is how a field gets consumed; `done()` then reports anything left
    over. So forgetting to read a field is caught by the same mechanism that
    catches an author inventing one.
    """

    def __init__(self, payload: Any, *, what: str, object_ref: str = ""):
        if not isinstance(payload, Mapping):
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{what} must be an object, got {type(payload).__name__}",
                object_ref=object_ref)
        self._payload: Mapping[str, Any] = payload
        self._seen: set[str] = set()
        self._what = what
        self._ref = object_ref

    def _get(self, key: str) -> Any:
        self._seen.add(key)
        return self._payload.get(key)

    def done(self) -> None:
        unknown = sorted(set(self._payload) - self._seen)
        if unknown:
            raise SpecError(
                ValidationCode.UNKNOWN_FIELD,
                f"{self._what} has unknown field(s) {unknown}; a field the "
                "pipeline ignores is a rule that does not say what its author "
                "wrote",
                object_ref=self._ref)

    # ---- typed reads --------------------------------------------------------
    def text(self, key: str, *, required: bool = False) -> str | None:
        value = self._get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise SpecError(
                    ValidationCode.MISSING_REQUIRED_FIELD,
                    f"{self._what}.{key} is required", object_ref=self._ref)
            return None
        if not isinstance(value, str):
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} must be text, got {type(value).__name__}",
                object_ref=self._ref)
        return value.strip()

    def flag(self, key: str, *, default: bool | None = None) -> bool | None:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} must be true or false", object_ref=self._ref)
        return value

    def integer(self, key: str, *, required: bool = False,
                default: int | None = None) -> int | None:
        value = self._get(key)
        if value is None:
            if required:
                raise SpecError(
                    ValidationCode.MISSING_REQUIRED_FIELD,
                    f"{self._what}.{key} is required", object_ref=self._ref)
            return default
        # bool is an int subclass; a flag where a count belongs is a mistake.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} must be a whole number", object_ref=self._ref)
        return value

    def number(self, key: str, *, required: bool = False) -> Decimal | None:
        return _decimal(self._get(key), f"{self._what}.{key}",
                        required=required, object_ref=self._ref)

    def day(self, key: str, *, required: bool = False) -> date | None:
        value = self._get(key)
        if value is None:
            if required:
                raise SpecError(
                    ValidationCode.MISSING_REQUIRED_FIELD,
                    f"{self._what}.{key} is required", object_ref=self._ref)
            return None
        if not isinstance(value, str):
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} must be an ISO date string",
                object_ref=self._ref)
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} is not an ISO date: {value!r}",
                object_ref=self._ref) from exc

    def identifier(self, key: str, *, required: bool = False) -> uuid.UUID | None:
        raw = self.text(key, required=required)
        if raw is None:
            return None
        try:
            return uuid.UUID(raw)
        except ValueError as exc:
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} is not an identifier: {raw!r}",
                object_ref=self._ref) from exc

    def texts(self, key: str) -> tuple[str, ...]:
        values = self.items(key)
        out: list[str] = []
        for i, value in enumerate(values):
            if not isinstance(value, str) or not value.strip():
                raise SpecError(
                    ValidationCode.MALFORMED_VALUE,
                    f"{self._what}.{key}[{i}] must be non-empty text",
                    object_ref=self._ref)
            out.append(value.strip())
        return tuple(out)

    def identifiers(self, key: str) -> tuple[uuid.UUID, ...]:
        out: list[uuid.UUID] = []
        for i, raw in enumerate(self.texts(key)):
            try:
                out.append(uuid.UUID(raw))
            except ValueError as exc:
                raise SpecError(
                    ValidationCode.MALFORMED_VALUE,
                    f"{self._what}.{key}[{i}] is not an identifier: {raw!r}",
                    object_ref=self._ref) from exc
        return tuple(out)

    def items(self, key: str) -> list[Any]:
        value = self._get(key)
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} must be a list", object_ref=self._ref)
        return list(value)

    def mapping(self, key: str) -> dict[str, str] | None:
        value = self._get(key)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise SpecError(
                ValidationCode.MALFORMED_VALUE,
                f"{self._what}.{key} must be an object", object_ref=self._ref)
        out: dict[str, str] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise SpecError(
                    ValidationCode.MALFORMED_VALUE,
                    f"{self._what}.{key} must map text to text; a number here "
                    "would be a float in disguise", object_ref=self._ref)
            out[k] = v
        return out


def _decimal(value: Any, what: str, *, required: bool = False,
             object_ref: str = "") -> Decimal | None:
    """Parse an exact decimal, refusing every inexact representation.

    A JSON number is a float by the time Python sees it, so a submitted `0.1` is
    already not one tenth. Requiring the string form is the only way the value
    an author wrote is the value that publishes.
    """
    if value is None:
        if required:
            raise SpecError(ValidationCode.MISSING_REQUIRED_FIELD,
                            f"{what} is required", object_ref=object_ref)
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise SpecError(
            ValidationCode.MALFORMED_VALUE,
            f"{what} must be a decimal STRING, not a float — a JSON number has "
            "already lost precision by the time it is read", object_ref=object_ref)
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, str):
        raise SpecError(ValidationCode.MALFORMED_VALUE,
                        f"{what} must be a decimal string", object_ref=object_ref)
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation as exc:
        raise SpecError(ValidationCode.MALFORMED_VALUE,
                        f"{what} is not a decimal: {value!r}",
                        object_ref=object_ref) from exc
    if not parsed.is_finite():
        raise SpecError(
            ValidationCode.MALFORMED_VALUE,
            f"{what} must be finite; {value!r} is not a number tax can be "
            "computed with", object_ref=object_ref)
    return parsed


# ---------------------------------------------------------------------------
# Child specifications
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConditionSpec:
    """One leaf of the eligibility gate: fact <operator> value."""

    fact_key: str
    operator: str
    value_type: str
    value_number: Decimal | None = None
    value_number_high: Decimal | None = None
    value_text: str | None = None
    value_boolean: bool | None = None
    value_date: date | None = None
    value_set: tuple[str, ...] = ()

    @classmethod
    def read(cls, payload: Any, ref: str) -> ConditionSpec:
        f = _Fields(payload, what="condition", object_ref=ref)
        spec = cls(
            fact_key=_require(f.text("fact_key", required=True)),
            operator=_require(f.text("operator", required=True)),
            value_type=_require(f.text("value_type", required=True)),
            value_number=f.number("value_number"),
            value_number_high=f.number("value_number_high"),
            value_text=f.text("value_text"),
            value_boolean=f.flag("value_boolean"),
            value_date=f.day("value_date"),
            value_set=f.texts("value_set"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        scale = _numeric_scale(self.value_type)
        return {
            "fact_key": self.fact_key,
            "operator": self.operator,
            "value_type": self.value_type,
            "value_number": scale(self.value_number),
            "value_number_high": scale(self.value_number_high),
            "value_text": self.value_text,
            "value_boolean": self.value_boolean,
            "value_date": self.value_date,
            "value_set": list(self.value_set),
        }


_T = TypeVar("_T")


def _numeric_scale(value_type: str) -> Callable[[Any], str | None]:
    """Which canonical scale a condition's operand serializes at.

    A money threshold and a percentage are different quantities; giving them one
    scale would let `0.15` the rate and `0.15` the dollar amount hash alike.
    """
    if value_type == "money":
        return c.money
    if value_type == "percent":
        return c.rate
    return c.quantity


@dataclass(frozen=True)
class ConditionGroupSpec:
    """A boolean node. Groups nest; conditions are the leaves."""

    logical_op: str = "AND"
    conditions: tuple[ConditionSpec, ...] = ()
    groups: tuple[ConditionGroupSpec, ...] = ()

    @classmethod
    def read(cls, payload: Any, ref: str, *, depth: int = 1) -> ConditionGroupSpec:
        if depth > MAX_CONDITION_DEPTH:
            raise SpecError(
                ValidationCode.CONDITION_GROUP_TOO_DEEP,
                f"condition groups nest deeper than {MAX_CONDITION_DEPTH}",
                family=Family.CONDITIONS, object_ref=ref)
        f = _Fields(payload, what="condition_group", object_ref=ref)
        spec = cls(
            logical_op=f.text("logical_op") or "AND",
            conditions=tuple(
                ConditionSpec.read(item, ref) for item in f.items("conditions")),
            groups=tuple(
                cls.read(item, ref, depth=depth + 1) for item in f.items("groups")),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {
            "logical_op": self.logical_op,
            # Authored order is preserved: a rule's conditions are read in the
            # order they were written, and reordering them to canonicalize would
            # change which draft a spec hash refers to.
            "conditions": [x.as_canonical() for x in self.conditions],
            "groups": [g.as_canonical() for g in self.groups],
        }

    def walk(self) -> list[ConditionSpec]:
        out = list(self.conditions)
        for group in self.groups:
            out.extend(group.walk())
        return out

    def walk_groups(self) -> list[ConditionGroupSpec]:
        out = [self]
        for group in self.groups:
            out.extend(group.walk_groups())
        return out


@dataclass(frozen=True)
class OutcomeSpec:
    """What the engine emits when the gate passes.

    A rule version may carry several. `formula_code` names the impact formula by
    its authored code rather than a row id, so a manifest is portable between
    environments.
    """

    outcome_type: str
    priority: int = 3
    formula_code: str | None = None
    title_template: str | None = None
    mechanism: str | None = None
    where_template: str | None = None
    how_template: str | None = None
    why_template: str | None = None
    economic_effect_type: str | None = None
    reversibility: str | None = None
    portfolio_lever_code: str | None = None
    lever_parameters: dict[str, str] | None = None

    @classmethod
    def read(cls, payload: Any, ref: str) -> OutcomeSpec:
        f = _Fields(payload, what="outcome", object_ref=ref)
        spec = cls(
            outcome_type=_require(f.text("outcome_type", required=True)),
            priority=f.integer("priority", default=3) or 3,
            formula_code=f.text("formula_code"),
            title_template=f.text("title_template"),
            mechanism=f.text("mechanism"),
            where_template=f.text("where_template"),
            how_template=f.text("how_template"),
            why_template=f.text("why_template"),
            economic_effect_type=f.text("economic_effect_type"),
            reversibility=f.text("reversibility"),
            portfolio_lever_code=f.text("portfolio_lever_code"),
            lever_parameters=f.mapping("lever_parameters"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {
            "outcome_type": self.outcome_type,
            "priority": self.priority,
            "formula_code": self.formula_code,
            "title_template": self.title_template,
            "mechanism": self.mechanism,
            "where_template": self.where_template,
            "how_template": self.how_template,
            "why_template": self.why_template,
            "economic_effect_type": self.economic_effect_type,
            "reversibility": self.reversibility,
            "portfolio_lever_code": self.portfolio_lever_code,
            "lever_parameters": self.lever_parameters,
        }

    def identity(self) -> tuple:
        """What makes two outcomes the same outcome, for duplicate detection.

        Deliberately NOT the whole outcome: two outcomes differing only in their
        prose templates are the same economic consequence written twice.
        """
        return (self.outcome_type, self.portfolio_lever_code, self.formula_code,
                self.economic_effect_type)


@dataclass(frozen=True)
class ActionSpec:
    action_code: str
    description: str
    effort_rating: int = 3
    cost_type: str | None = None
    cost_amount: Decimal | None = None
    deadline_code: str | None = None
    sort_order: int = 0

    @classmethod
    def read(cls, payload: Any, ref: str) -> ActionSpec:
        f = _Fields(payload, what="action", object_ref=ref)
        spec = cls(
            action_code=_require(f.text("action_code", required=True)),
            description=_require(f.text("description", required=True)),
            effort_rating=f.integer("effort_rating", default=3) or 3,
            cost_type=f.text("cost_type"),
            cost_amount=f.number("cost_amount"),
            deadline_code=f.text("deadline_code"),
            sort_order=f.integer("sort_order", default=0) or 0,
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {
            "action_code": self.action_code,
            "description": self.description,
            "effort_rating": self.effort_rating,
            "cost_type": self.cost_type,
            "cost_amount": c.money(self.cost_amount),
            "deadline_code": self.deadline_code,
            "sort_order": self.sort_order,
        }


@dataclass(frozen=True)
class EvidenceSpec:
    document_type_code: str
    necessity: str = "required"
    note: str | None = None

    @classmethod
    def read(cls, payload: Any, ref: str) -> EvidenceSpec:
        f = _Fields(payload, what="evidence_requirement", object_ref=ref)
        spec = cls(
            document_type_code=_require(f.text("document_type_code", required=True)),
            necessity=f.text("necessity") or "required",
            note=f.text("note"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"document_type_code": self.document_type_code,
                "necessity": self.necessity, "note": self.note}


@dataclass(frozen=True)
class DependencySpec:
    depends_on_rule_code: str
    dependency_type: str
    note: str | None = None

    @classmethod
    def read(cls, payload: Any, ref: str) -> DependencySpec:
        f = _Fields(payload, what="dependency", object_ref=ref)
        spec = cls(
            depends_on_rule_code=_require(
                f.text("depends_on_rule_code", required=True)),
            dependency_type=_require(f.text("dependency_type", required=True)),
            note=f.text("note"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"depends_on_rule_code": self.depends_on_rule_code,
                "dependency_type": self.dependency_type, "note": self.note}


@dataclass(frozen=True)
class DeadlineSpec:
    deadline_code: str
    deadline_date: date | None = None
    description: str | None = None
    is_hard: bool = True
    jurisdiction_code: str | None = None

    @classmethod
    def read(cls, payload: Any, ref: str) -> DeadlineSpec:
        f = _Fields(payload, what="deadline", object_ref=ref)
        spec = cls(
            deadline_code=_require(f.text("deadline_code", required=True)),
            deadline_date=f.day("deadline_date"),
            description=f.text("description"),
            is_hard=bool(f.flag("is_hard", default=True)),
            jurisdiction_code=f.text("jurisdiction_code"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"deadline_code": self.deadline_code,
                "deadline_date": self.deadline_date,
                "description": self.description, "is_hard": self.is_hard,
                "jurisdiction_code": self.jurisdiction_code}


@dataclass(frozen=True)
class SharedResourceSpec:
    resource_code: str
    pool_scope: str = "individual"

    @classmethod
    def read(cls, payload: Any, ref: str) -> SharedResourceSpec:
        f = _Fields(payload, what="shared_resource", object_ref=ref)
        spec = cls(
            resource_code=_require(f.text("resource_code", required=True)),
            pool_scope=f.text("pool_scope") or "individual",
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"resource_code": self.resource_code, "pool_scope": self.pool_scope}


@dataclass(frozen=True)
class FormulaInputSpec:
    """One binding of a formula parameter to a fact or a literal."""

    param_name: str
    fact_key: str | None = None
    literal_value: Decimal | None = None

    @classmethod
    def read(cls, payload: Any, ref: str) -> FormulaInputSpec:
        f = _Fields(payload, what="formula_input", object_ref=ref)
        spec = cls(
            param_name=_require(f.text("param_name", required=True)),
            fact_key=f.text("fact_key"),
            literal_value=f.number("literal_value"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"param_name": self.param_name, "fact_key": self.fact_key,
                "literal_value": c.quantity(self.literal_value)}


@dataclass(frozen=True)
class FormulaSpec:
    """The math half of a rule, in the engine's constrained expression language."""

    code: str
    expression: str
    expression_lang: str = "rpn"
    output_unit: str | None = None
    description: str | None = None
    inputs: tuple[FormulaInputSpec, ...] = ()
    #: Deterministic test vectors: exact inputs to an exact expected Decimal.
    vectors: tuple[FormulaVector, ...] = ()

    @classmethod
    def read(cls, payload: Any, ref: str) -> FormulaSpec:
        f = _Fields(payload, what="formula", object_ref=ref)
        spec = cls(
            code=_require(f.text("code", required=True)),
            expression=_require(f.text("expression", required=True)),
            expression_lang=f.text("expression_lang") or "rpn",
            output_unit=f.text("output_unit"),
            description=f.text("description"),
            inputs=tuple(
                FormulaInputSpec.read(item, ref) for item in f.items("inputs")),
            vectors=tuple(
                FormulaVector.read(item, ref) for item in f.items("vectors")),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {
            "code": self.code,
            "expression": self.expression,
            "expression_lang": self.expression_lang,
            "output_unit": self.output_unit,
            "description": self.description,
            "inputs": [i.as_canonical() for i in self.inputs],
            "vectors": [v.as_canonical() for v in self.vectors],
        }


@dataclass(frozen=True)
class FormulaVector:
    """Inputs to an EXACT expected output. No tolerance, ever — §28."""

    name: str
    inputs: dict[str, str]
    expected: Decimal

    @classmethod
    def read(cls, payload: Any, ref: str) -> FormulaVector:
        f = _Fields(payload, what="formula_vector", object_ref=ref)
        spec = cls(
            name=_require(f.text("name", required=True)),
            inputs=f.mapping("inputs") or {},
            expected=_require(f.number("expected", required=True)),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"name": self.name,
                "inputs": dict(sorted(self.inputs.items())),
                "expected": c.quantity(self.expected)}


@dataclass(frozen=True)
class ReferenceDataRef:
    """A pin to reference data this knowledge depends on.

    Named by SEMANTIC key plus tax year, which is the identity the reference
    tables are unique on — not a row id, so a manifest resolves the same way in
    every environment.
    """

    kind: str
    key: str
    tax_year: int

    @classmethod
    def read(cls, payload: Any, ref: str) -> ReferenceDataRef:
        f = _Fields(payload, what="reference_data_dependency", object_ref=ref)
        spec = cls(
            kind=_require(f.text("kind", required=True)),
            key=_require(f.text("key", required=True)),
            tax_year=_require(f.integer("tax_year", required=True)),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"kind": self.kind, "key": self.key, "tax_year": self.tax_year}


@dataclass(frozen=True)
class ExampleSpec:
    """A governed test case. Verifies the knowledge; never becomes authority."""

    name: str
    facts: dict[str, str]
    expect_eligible: bool
    expected_outcome_types: tuple[str, ...] = ()

    @classmethod
    def read(cls, payload: Any, ref: str) -> ExampleSpec:
        f = _Fields(payload, what="example", object_ref=ref)
        expect = f.flag("expect_eligible")
        if expect is None:
            raise SpecError(
                ValidationCode.MISSING_REQUIRED_FIELD,
                "example.expect_eligible is required; an example that does not "
                "say what it expects proves nothing",
                family=Family.EXAMPLES, object_ref=ref)
        spec = cls(
            name=_require(f.text("name", required=True)),
            facts=f.mapping("facts") or {},
            expect_eligible=expect,
            expected_outcome_types=f.texts("expected_outcome_types"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"name": self.name,
                "facts": dict(sorted(self.facts.items())),
                "expect_eligible": self.expect_eligible,
                "expected_outcome_types": list(self.expected_outcome_types)}


# ---------------------------------------------------------------------------
# The aggregate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TaxKnowledgeDraftSpec:
    """One complete candidate rule version, validated before anything is written."""

    rule_code: str
    name: str
    category: str
    jurisdiction_code: str
    tax_year: int
    effective_from: date
    description: str
    subcategory: str | None = None
    province_code: str | None = None
    effective_to: date | None = None
    eligibility_basis_codes: tuple[str, ...] = ()
    condition_group: ConditionGroupSpec | None = None
    outcomes: tuple[OutcomeSpec, ...] = ()
    actions: tuple[ActionSpec, ...] = ()
    evidence_requirements: tuple[EvidenceSpec, ...] = ()
    dependencies: tuple[DependencySpec, ...] = ()
    deadlines: tuple[DeadlineSpec, ...] = ()
    shared_resources: tuple[SharedResourceSpec, ...] = ()
    formula: FormulaSpec | None = None
    reference_data_dependencies: tuple[ReferenceDataRef, ...] = ()
    #: Registry citation ids. The Source Registry stays canonical: a manifest
    #: never restates an issuer, a title or a URL — §43.
    citation_ids: tuple[uuid.UUID, ...] = ()
    assumption_codes: tuple[str, ...] = ()
    examples: tuple[ExampleSpec, ...] = ()
    supersedes_version_id: uuid.UUID | None = None
    schema_version: str = KNOWLEDGE_SPEC_SCHEMA_VERSION

    # ---- reading ------------------------------------------------------------
    @classmethod
    def read(cls, payload: Any) -> TaxKnowledgeDraftSpec:
        probe = payload.get("rule_code") if isinstance(payload, Mapping) else None
        ref = probe if isinstance(probe, str) else ""
        f = _Fields(payload, what="draft", object_ref=ref)

        declared = f.text("schema_version") or KNOWLEDGE_SPEC_SCHEMA_VERSION
        if declared != KNOWLEDGE_SPEC_SCHEMA_VERSION:
            raise SpecError(
                ValidationCode.SPEC_SCHEMA_VERSION_UNSUPPORTED,
                f"authoring schema {declared!r} is not "
                f"{KNOWLEDGE_SPEC_SCHEMA_VERSION!r}; a draft written against a "
                "different contract is refused rather than reinterpreted",
                object_ref=ref)

        group_payload = f._get("condition_group")  # noqa: SLF001 — same class
        spec = cls(
            rule_code=_require(f.text("rule_code", required=True)),
            name=_require(f.text("name", required=True)),
            category=_require(f.text("category", required=True)),
            jurisdiction_code=_require(f.text("jurisdiction_code", required=True)),
            tax_year=_require(f.integer("tax_year", required=True)),
            effective_from=_require(f.day("effective_from", required=True)),
            description=_require(f.text("description", required=True)),
            subcategory=f.text("subcategory"),
            province_code=f.text("province_code"),
            effective_to=f.day("effective_to"),
            eligibility_basis_codes=f.texts("eligibility_basis_codes"),
            condition_group=(
                ConditionGroupSpec.read(group_payload, ref)
                if group_payload is not None else None),
            outcomes=tuple(OutcomeSpec.read(x, ref) for x in f.items("outcomes")),
            actions=tuple(ActionSpec.read(x, ref) for x in f.items("actions")),
            evidence_requirements=tuple(
                EvidenceSpec.read(x, ref)
                for x in f.items("evidence_requirements")),
            dependencies=tuple(
                DependencySpec.read(x, ref) for x in f.items("dependencies")),
            deadlines=tuple(DeadlineSpec.read(x, ref) for x in f.items("deadlines")),
            shared_resources=tuple(
                SharedResourceSpec.read(x, ref) for x in f.items("shared_resources")),
            formula=(
                FormulaSpec.read(f._get("formula"), ref)  # noqa: SLF001
                if f._get("formula") is not None else None),  # noqa: SLF001
            reference_data_dependencies=tuple(
                ReferenceDataRef.read(x, ref)
                for x in f.items("reference_data_dependencies")),
            citation_ids=f.identifiers("citation_ids"),
            assumption_codes=f.texts("assumption_codes"),
            examples=tuple(ExampleSpec.read(x, ref) for x in f.items("examples")),
            supersedes_version_id=f.identifier("supersedes_version_id"),
        )
        f.done()
        return spec

    # ---- identity -----------------------------------------------------------
    def as_canonical(self) -> dict:
        """Everything the published knowledge will MEAN, and nothing else.

        No submitter, no timestamp, no request id, no row id: §34 requires that
        review noise stay out of a semantic hash, or a resubmitted identical
        draft would look like a different rule.
        """
        return {
            "schema_version": self.schema_version,
            "rule_code": self.rule_code,
            "name": self.name,
            "category": self.category,
            "subcategory": self.subcategory,
            "jurisdiction_code": self.jurisdiction_code,
            "province_code": self.province_code,
            "tax_year": self.tax_year,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "description": self.description,
            "eligibility_basis_codes": list(self.eligibility_basis_codes),
            "condition_group": (
                self.condition_group.as_canonical() if self.condition_group else None),
            # Child collections sort on their own content, so the same knowledge
            # written in a different order is the same knowledge. Conditions are
            # the exception: their authored order is part of the rule.
            "outcomes": c.ordered([x.as_canonical() for x in self.outcomes]),
            "actions": c.ordered([x.as_canonical() for x in self.actions]),
            "evidence_requirements": c.ordered(
                [x.as_canonical() for x in self.evidence_requirements]),
            "dependencies": c.ordered([x.as_canonical() for x in self.dependencies]),
            "deadlines": c.ordered([x.as_canonical() for x in self.deadlines]),
            "shared_resources": c.ordered(
                [x.as_canonical() for x in self.shared_resources]),
            "formula": self.formula.as_canonical() if self.formula else None,
            "reference_data_dependencies": c.ordered(
                [x.as_canonical() for x in self.reference_data_dependencies]),
            "citation_ids": sorted(str(x) for x in self.citation_ids),
            "assumption_codes": sorted(self.assumption_codes),
            "examples": c.ordered([x.as_canonical() for x in self.examples]),
            "supersedes_version_id": (
                str(self.supersedes_version_id)
                if self.supersedes_version_id else None),
        }

    def spec_identity(self) -> str:
        return c.domain_hash(c.DOMAIN_KNOWLEDGE_SPEC, self.as_canonical())


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
class ReferenceDataKind:
    """The reference-data families the engine reads and the snapshotter pins."""

    TAX_BRACKET_SET = "TAX_BRACKET_SET"
    CONTRIBUTION_LIMIT = "CONTRIBUTION_LIMIT"
    CALC_CONSTANT = "CALC_CONSTANT"

    ALL = frozenset({TAX_BRACKET_SET, CONTRIBUTION_LIMIT, CALC_CONSTANT})


@dataclass(frozen=True)
class BracketSpec:
    ordinal: int
    lower_bound: Decimal
    rate: Decimal
    upper_bound: Decimal | None = None

    @classmethod
    def read(cls, payload: Any, ref: str) -> BracketSpec:
        f = _Fields(payload, what="bracket", object_ref=ref)
        spec = cls(
            ordinal=_require(f.integer("ordinal", required=True)),
            lower_bound=_require(f.number("lower_bound", required=True)),
            rate=_require(f.number("rate", required=True)),
            upper_bound=f.number("upper_bound"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        return {"ordinal": self.ordinal, "lower_bound": c.money(self.lower_bound),
                "upper_bound": c.money(self.upper_bound), "rate": c.rate(self.rate)}


@dataclass(frozen=True)
class ReferenceDataSpec:
    """One reference-data object: a bracket table, a limit, or a constant."""

    kind: str
    key: str
    tax_year: int
    jurisdiction_code: str | None = None
    brackets: tuple[BracketSpec, ...] = ()
    annual_limit: Decimal | None = None
    lifetime_limit: Decimal | None = None
    percent_of_income: Decimal | None = None
    allows_carryforward: bool = False
    value: Decimal | None = None
    unit: str | None = None
    #: Intra-year applicability. Both absent is the ordinary case and means the
    #: value applies to the whole tax year. A bound is set only when the source
    #: publishes one — CRA prescribes interest rates per calendar quarter, so
    #: one code and one tax year carry four different values.
    effective_from: date | None = None
    effective_to: date | None = None
    citation_ids: tuple[uuid.UUID, ...] = ()
    schema_version: str = KNOWLEDGE_SPEC_SCHEMA_VERSION

    @classmethod
    def read(cls, payload: Any) -> ReferenceDataSpec:
        probe = payload.get("key") if isinstance(payload, Mapping) else None
        ref = probe if isinstance(probe, str) else ""
        f = _Fields(payload, what="reference_data", object_ref=ref)
        declared = f.text("schema_version") or KNOWLEDGE_SPEC_SCHEMA_VERSION
        if declared != KNOWLEDGE_SPEC_SCHEMA_VERSION:
            raise SpecError(
                ValidationCode.SPEC_SCHEMA_VERSION_UNSUPPORTED,
                f"authoring schema {declared!r} is not "
                f"{KNOWLEDGE_SPEC_SCHEMA_VERSION!r}", object_ref=ref)
        spec = cls(
            kind=_require(f.text("kind", required=True)),
            key=_require(f.text("key", required=True)),
            tax_year=_require(f.integer("tax_year", required=True)),
            jurisdiction_code=f.text("jurisdiction_code"),
            brackets=tuple(BracketSpec.read(x, ref) for x in f.items("brackets")),
            annual_limit=f.number("annual_limit"),
            lifetime_limit=f.number("lifetime_limit"),
            percent_of_income=f.number("percent_of_income"),
            allows_carryforward=bool(f.flag("allows_carryforward", default=False)),
            value=f.number("value"),
            unit=f.text("unit"),
            effective_from=f.day("effective_from"),
            effective_to=f.day("effective_to"),
            citation_ids=f.identifiers("citation_ids"),
        )
        f.done()
        return spec

    def as_canonical(self) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "key": self.key,
            "tax_year": self.tax_year,
            "jurisdiction_code": self.jurisdiction_code,
            # Bracket order IS the table: ordinals are declared, and sorting
            # them here would hide a table authored out of order.
            "brackets": [b.as_canonical() for b in self.brackets],
            "annual_limit": c.money(self.annual_limit),
            "lifetime_limit": c.money(self.lifetime_limit),
            "percent_of_income": c.rate(self.percent_of_income),
            "allows_carryforward": self.allows_carryforward,
            "value": c.quantity(self.value),
            "unit": self.unit,
            "citation_ids": sorted(str(x) for x in self.citation_ids),
        }
        # The period keys appear ONLY on a spec that carries a period. An
        # annual object has no intra-year applicability to state, so emitting
        # two nulls for it would say nothing while changing every annual spec
        # hash ever recorded — including the ones stored on validation reports
        # that publication rebinds against. Absent and null mean the same thing
        # here, so absent is the canonical form and existing content keeps its
        # identity.
        if self.effective_from is not None:
            payload["effective_from"] = self.effective_from
        if self.effective_to is not None:
            payload["effective_to"] = self.effective_to
        return payload

    def spec_identity(self) -> str:
        return c.domain_hash(c.DOMAIN_KNOWLEDGE_SPEC, self.as_canonical())


def _require(value: _T | None) -> _T:
    """Narrow an Optional the readers already proved non-None.

    `_Fields.text(required=True)` raises rather than returning None, but its
    signature cannot say so; this makes the guarantee explicit for the checker
    without a cast that would also silence a real None.
    """
    if value is None:  # pragma: no cover — the readers raise first
        raise SpecError(ValidationCode.MISSING_REQUIRED_FIELD,
                        "a required field was absent")
    return value


def pack_identity(spec_hashes: Sequence[str]) -> str:
    """The identity of one coherent publication set.

    Order-independent: a release is a SET of knowledge published together, and
    the order an operator listed it in is not part of what published.
    """
    return c.domain_hash(c.DOMAIN_KNOWLEDGE_PACK,
                         {"members": sorted(spec_hashes)})
