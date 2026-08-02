"""TKMS domain value objects — pure, framework-free, deterministic.

`ExtractedRule` is the single output contract EVERY parser must produce, whatever
the source format. It is stored verbatim (as JSON) in tkms.extracted_rule and is
the canonical, immutable input to promotion/validation/comparison. Keeping it a
plain dataclass with an explicit primitive `as_payload()` / `from_payload()`
round-trip makes parsers golden-file testable and the stored payload stable.

Nothing here touches the DB, FastAPI, or the engine — it is the shared language
the whole pipeline speaks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

# The condition operators the engine understands (ref.condition_operator).
VALID_OPERATORS = {
    "eq", "neq", "gt", "gte", "lt", "lte", "between",
    "in", "contains", "exists", "is_true",
}
# Rule categories (ref.rule_category).
VALID_CATEGORIES = {"credit", "deduction", "benefit", "bracket", "limit", "threshold"}
# Leaf value types (rules.rule_condition.value_type).
VALID_VALUE_TYPES = {"number", "money", "percent", "boolean", "text", "date", "set"}


def _dec(v: object) -> Decimal | None:
    """Coerce to Decimal or None; never raises (invalid → None)."""
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _iso(v: object) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


@dataclass(frozen=True)
class EligibilityCondition:
    """One leaf of a rule's boolean gate: fact <operator> value(s)."""

    fact_key: str
    operator: str
    value_type: str = "number"
    value_number: Decimal | None = None
    value_number_high: Decimal | None = None   # 'between' upper bound
    value_text: str | None = None
    value_boolean: bool | None = None
    value_date: str | None = None               # ISO date string
    value_set: tuple[str, ...] = ()             # for 'in' / 'contains'

    def as_payload(self) -> dict:
        return {
            "fact_key": self.fact_key,
            "operator": self.operator,
            "value_type": self.value_type,
            "value_number": str(self.value_number) if self.value_number is not None else None,
            "value_number_high": (
                str(self.value_number_high) if self.value_number_high is not None else None
            ),
            "value_text": self.value_text,
            "value_boolean": self.value_boolean,
            "value_date": self.value_date,
            "value_set": list(self.value_set),
        }

    @classmethod
    def from_payload(cls, d: dict) -> EligibilityCondition:
        return cls(
            fact_key=str(d["fact_key"]),
            operator=str(d["operator"]),
            value_type=str(d.get("value_type", "number")),
            value_number=_dec(d.get("value_number")),
            value_number_high=_dec(d.get("value_number_high")),
            value_text=d.get("value_text"),
            value_boolean=d.get("value_boolean"),
            value_date=_iso(d.get("value_date")),
            value_set=tuple(d.get("value_set") or ()),
        )


@dataclass(frozen=True)
class FormulaSpec:
    """The math half of a rule (kept separate from prose), sandboxed DSL."""

    code: str
    expression: str
    expression_lang: str = "rpn"
    output_unit: str | None = None
    # param_name -> fact_key (bind engine facts to formula inputs)
    inputs: tuple[tuple[str, str], ...] = ()

    def as_payload(self) -> dict:
        return {
            "code": self.code,
            "expression": self.expression,
            "expression_lang": self.expression_lang,
            "output_unit": self.output_unit,
            "inputs": [list(pair) for pair in self.inputs],
        }

    @classmethod
    def from_payload(cls, d: dict) -> FormulaSpec:
        return cls(
            code=str(d["code"]),
            expression=str(d["expression"]),
            expression_lang=str(d.get("expression_lang", "rpn")),
            output_unit=d.get("output_unit"),
            inputs=tuple((str(a), str(b)) for a, b in (d.get("inputs") or [])),
        )


VALID_OUTCOME_TYPES = {
    "recommend", "apply_credit", "apply_deduction", "flag_benefit_eligibility",
}


@dataclass(frozen=True)
class OutcomeSpec:
    """The THEN half of a rule — what the engine emits when the gate passes.

    `uses_formula` binds the impact to the rule's own staged formula (the common
    case); the where/how/why templates power the user-facing recommendation.
    """

    outcome_type: str = "recommend"
    priority: int = 3
    uses_formula: bool = True
    title_template: str | None = None
    mechanism: str | None = None
    where_template: str | None = None
    how_template: str | None = None
    why_template: str | None = None

    def as_payload(self) -> dict:
        return {
            "outcome_type": self.outcome_type,
            "priority": self.priority,
            "uses_formula": self.uses_formula,
            "title_template": self.title_template,
            "mechanism": self.mechanism,
            "where_template": self.where_template,
            "how_template": self.how_template,
            "why_template": self.why_template,
        }

    @classmethod
    def from_payload(cls, d: dict) -> OutcomeSpec:
        return cls(
            outcome_type=str(d.get("outcome_type", "recommend")),
            priority=int(d.get("priority", 3)),
            uses_formula=bool(d.get("uses_formula", True)),
            title_template=d.get("title_template"),
            mechanism=d.get("mechanism"),
            where_template=d.get("where_template"),
            how_template=d.get("how_template"),
            why_template=d.get("why_template"),
        )


@dataclass(frozen=True)
class ExtractedRule:
    """The canonical parser output contract (§4 of the TKMS architecture).

    Every field a downstream stage may need is present and normalized. Money and
    rate values are Decimals; dates are ISO strings. `as_payload()` yields a
    JSON-serializable dict stored verbatim in tkms.extracted_rule.payload.
    """

    rule_code: str
    name: str
    category: str
    jurisdiction: str                       # 'FED' or a province code
    tax_year: int
    subcategory: str | None = None
    province: str | None = None             # explicit province override
    effective_date: str | None = None       # ISO; defaults to Jan 1 of tax_year
    expiry_date: str | None = None
    description: str = ""
    formula: FormulaSpec | None = None
    outcome: OutcomeSpec | None = None
    eligibility_conditions: tuple[EligibilityCondition, ...] = ()
    income_threshold_low: Decimal | None = None
    income_threshold_high: Decimal | None = None
    max_amount: Decimal | None = None
    min_amount: Decimal | None = None
    reduction_rate: Decimal | None = None
    contribution_limit: Decimal | None = None
    source_citation: str | None = None
    source_url: str | None = None
    legislation_reference: str | None = None
    confidence: Decimal | None = None

    def effective_date_or_default(self) -> str:
        return self.effective_date or date(self.tax_year, 1, 1).isoformat()

    def as_payload(self) -> dict:
        return {
            "rule_code": self.rule_code,
            "name": self.name,
            "category": self.category,
            "subcategory": self.subcategory,
            "jurisdiction": self.jurisdiction,
            "province": self.province,
            "tax_year": self.tax_year,
            "effective_date": self.effective_date_or_default(),
            "expiry_date": self.expiry_date,
            "description": self.description,
            "formula": self.formula.as_payload() if self.formula else None,
            "outcome": self.outcome.as_payload() if self.outcome else None,
            "eligibility_conditions": [c.as_payload() for c in self.eligibility_conditions],
            "income_threshold_low": _iso_num(self.income_threshold_low),
            "income_threshold_high": _iso_num(self.income_threshold_high),
            "max_amount": _iso_num(self.max_amount),
            "min_amount": _iso_num(self.min_amount),
            "reduction_rate": _iso_num(self.reduction_rate),
            "contribution_limit": _iso_num(self.contribution_limit),
            "source_citation": self.source_citation,
            "source_url": self.source_url,
            "legislation_reference": self.legislation_reference,
            "confidence": _iso_num(self.confidence),
        }

    @classmethod
    def from_payload(cls, d: dict) -> ExtractedRule:
        return cls(
            rule_code=str(d["rule_code"]),
            name=str(d["name"]),
            category=str(d["category"]),
            jurisdiction=str(d["jurisdiction"]),
            tax_year=int(d["tax_year"]),
            subcategory=d.get("subcategory"),
            province=d.get("province"),
            effective_date=_iso(d.get("effective_date")),
            expiry_date=_iso(d.get("expiry_date")),
            description=str(d.get("description", "")),
            formula=FormulaSpec.from_payload(d["formula"]) if d.get("formula") else None,
            outcome=OutcomeSpec.from_payload(d["outcome"]) if d.get("outcome") else None,
            eligibility_conditions=tuple(
                EligibilityCondition.from_payload(c) for c in (d.get("eligibility_conditions") or [])
            ),
            income_threshold_low=_dec(d.get("income_threshold_low")),
            income_threshold_high=_dec(d.get("income_threshold_high")),
            max_amount=_dec(d.get("max_amount")),
            min_amount=_dec(d.get("min_amount")),
            reduction_rate=_dec(d.get("reduction_rate")),
            contribution_limit=_dec(d.get("contribution_limit")),
            source_citation=d.get("source_citation"),
            source_url=d.get("source_url"),
            legislation_reference=d.get("legislation_reference"),
            confidence=_dec(d.get("confidence")),
        )


def _iso_num(v: Decimal | None) -> str | None:
    return str(v) if v is not None else None


@dataclass(frozen=True)
class ExtractedRuleSet:
    """A parser's whole output: the rules plus provenance about the run."""

    parser_name: str
    parser_version: str
    source: str
    rules: tuple[ExtractedRule, ...] = ()
    confidence: Decimal | None = None
    warnings: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.rules)


@dataclass
class ValidationFinding:
    """One validation-check outcome (domain VO; persisted as a table row)."""

    severity: str          # 'error' | 'warning' | 'info'
    code: str              # machine code, e.g. 'DUPLICATE_RULE'
    message: str
    rule_ref: str | None = None


@dataclass
class ChangeItem:
    """One field-level difference between a draft and its published baseline."""

    field: str
    change_type: str       # 'added' | 'removed' | 'changed'
    old_value: str | None = None
    new_value: str | None = None


@dataclass
class ValidationOutcome:
    """The aggregate result of validating an ExtractedRule / draft."""

    findings: list[ValidationFinding] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "warning" for f in self.findings)

    @property
    def status(self) -> str:
        if self.has_errors:
            return "failed"
        if self.has_warnings:
            return "warnings"
        return "passed"
