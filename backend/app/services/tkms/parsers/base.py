"""Parser framework base — the ABC every parser subclasses + shared normalizer.

Adding a jurisdiction or format is a new file + a registry entry, never a change
to the pipeline (the same seam as OcrProvider / LlmClient). Structured formats
(CSV/JSON/XML/Manual) share one deterministic row→ExtractedRule normalizer so
they behave identically; only text/rule *extraction* differs per parser.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from app.services.tkms.domain.models import (
    VALID_CATEGORIES,
    VALID_OPERATORS,
    VALID_OUTCOME_TYPES,
    VALID_VALUE_TYPES,
    EligibilityCondition,
    ExtractedRule,
    ExtractedRuleSet,
    FormulaSpec,
    OutcomeSpec,
    _dec,
)

# re-export so callers can import the contract from the parser package too
__all__ = ["BaseParser", "ExtractedRuleSet", "normalize_rule"]

_MONEY_FIELDS = (
    "max_amount", "min_amount", "reduction_rate", "contribution_limit",
    "income_threshold_low", "income_threshold_high", "confidence",
)


def _s(v: object) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def normalize_rule(raw: dict) -> tuple[ExtractedRule | None, list[str]]:
    """Map a loosely-typed source row to the canonical ExtractedRule contract.

    Returns (rule, warnings). A hard schema failure returns (None, [errors]);
    soft issues (e.g. a rate outside 0..1) are attached as warnings but keep the
    rule (validation is a separate, stricter stage that can block promotion).
    """
    warnings: list[str] = []

    code = _s(raw.get("rule_code") or raw.get("code"))
    name = _s(raw.get("name"))
    category = _s(raw.get("category"))
    jurisdiction = _s(raw.get("jurisdiction"))
    tax_year_raw = _s(raw.get("tax_year"))

    missing = [
        label for label, val in (
            ("rule_code", code), ("name", name), ("category", category),
            ("jurisdiction", jurisdiction), ("tax_year", tax_year_raw),
        ) if not val
    ]
    if missing:
        return None, [f"missing required field(s): {', '.join(missing)}"]

    category = category.lower()
    if category not in VALID_CATEGORIES:
        return None, [f"invalid category '{category}'"]

    try:
        tax_year = int(tax_year_raw)
    except (TypeError, ValueError):
        return None, [f"tax_year '{tax_year_raw}' is not an integer"]
    if not (1900 <= tax_year <= 2200):
        return None, [f"tax_year {tax_year} out of range"]

    jurisdiction = jurisdiction.upper()
    province = _s(raw.get("province"))
    if province:
        province = province.upper()
    elif jurisdiction != "FED":
        province = jurisdiction

    nums: dict[str, Decimal | None] = {}
    for f in _MONEY_FIELDS:
        val = _dec(raw.get(f))
        if raw.get(f) not in (None, "") and val is None:
            warnings.append(f"{f} '{raw.get(f)}' is not numeric — ignored")
        nums[f] = val

    rate = nums["reduction_rate"]
    if rate is not None and not (Decimal(0) <= rate <= Decimal(1)):
        warnings.append(f"reduction_rate {rate} outside 0..1")

    formula, fwarn = _parse_formula(raw.get("formula"))
    warnings.extend(fwarn)
    outcome, owarn = _parse_outcome(raw.get("outcome"))
    warnings.extend(owarn)
    conditions, cwarn = _parse_conditions(raw.get("eligibility_conditions"))
    warnings.extend(cwarn)

    rule = ExtractedRule(
        rule_code=code.upper(),
        name=name,
        category=category,
        subcategory=_s(raw.get("subcategory")),
        jurisdiction=jurisdiction,
        province=province,
        tax_year=tax_year,
        effective_date=_s(raw.get("effective_date")),
        expiry_date=_s(raw.get("expiry_date")),
        description=_s(raw.get("description")) or name,
        formula=formula,
        outcome=outcome,
        eligibility_conditions=tuple(conditions),
        income_threshold_low=nums["income_threshold_low"],
        income_threshold_high=nums["income_threshold_high"],
        max_amount=nums["max_amount"],
        min_amount=nums["min_amount"],
        reduction_rate=rate,
        contribution_limit=nums["contribution_limit"],
        source_citation=_s(raw.get("source_citation")),
        source_url=_s(raw.get("source_url")),
        legislation_reference=_s(raw.get("legislation_reference")),
        confidence=nums["confidence"],
    )
    return rule, warnings


def _parse_formula(val: object) -> tuple[FormulaSpec | None, list[str]]:
    if not val:
        return None, []
    if not isinstance(val, dict):
        return None, [f"formula must be an object, got {type(val).__name__}"]
    expr = _s(val.get("expression"))
    code = _s(val.get("code"))
    if not expr or not code:
        return None, ["formula missing 'code' or 'expression'"]
    inputs = tuple(
        (str(pair[0]), str(pair[1]))
        for pair in (val.get("inputs") or [])
        if isinstance(pair, (list, tuple)) and len(pair) == 2
    )
    return FormulaSpec(
        code=code.upper(),
        expression=expr,
        expression_lang=_s(val.get("expression_lang")) or "rpn",
        output_unit=_s(val.get("output_unit")),
        inputs=inputs,
    ), []


def _parse_outcome(val: object) -> tuple[OutcomeSpec | None, list[str]]:
    if not val:
        return None, []
    if not isinstance(val, dict):
        return None, [f"outcome must be an object, got {type(val).__name__}"]
    otype = (_s(val.get("outcome_type")) or "recommend").lower()
    warnings: list[str] = []
    if otype not in VALID_OUTCOME_TYPES:
        warnings.append(f"unknown outcome_type '{otype}' — defaulting to recommend")
        otype = "recommend"
    try:
        priority = int(val.get("priority", 3))
    except (TypeError, ValueError):
        priority = 3
    return OutcomeSpec(
        outcome_type=otype,
        priority=priority,
        uses_formula=bool(val.get("uses_formula", True)),
        title_template=_s(val.get("title_template")),
        mechanism=_s(val.get("mechanism")),
        where_template=_s(val.get("where_template")),
        how_template=_s(val.get("how_template")),
        why_template=_s(val.get("why_template")),
    ), warnings


def _parse_conditions(val: object) -> tuple[list[EligibilityCondition], list[str]]:
    if not val:
        return [], []
    if not isinstance(val, list):
        return [], [f"eligibility_conditions must be a list, got {type(val).__name__}"]
    out: list[EligibilityCondition] = []
    warnings: list[str] = []
    for i, c in enumerate(val):
        if not isinstance(c, dict):
            warnings.append(f"condition {i} is not an object — skipped")
            continue
        fact = _s(c.get("fact_key"))
        op = _s(c.get("operator"))
        if not fact or not op:
            warnings.append(f"condition {i} missing fact_key/operator — skipped")
            continue
        if op not in VALID_OPERATORS:
            warnings.append(f"condition {i} unknown operator '{op}' — skipped")
            continue
        vtype = (_s(c.get("value_type")) or "number").lower()
        if vtype not in VALID_VALUE_TYPES:
            warnings.append(f"condition {i} unknown value_type '{vtype}' — defaulting to number")
            vtype = "number"
        out.append(EligibilityCondition(
            fact_key=fact,
            operator=op,
            value_type=vtype,
            value_number=_dec(c.get("value_number")),
            value_number_high=_dec(c.get("value_number_high")),
            value_text=_s(c.get("value_text")),
            value_boolean=c.get("value_boolean"),
            value_date=_s(c.get("value_date")),
            value_set=tuple(str(x) for x in (c.get("value_set") or [])),
        ))
    return out, warnings


class BaseParser(ABC):
    """Every parser declares its identity and implements the two phases."""

    name: str = "base"
    version: str = "0.0.0"
    source: str = "generic"
    fmt: str = "manual"
    base_confidence: Decimal = Decimal("0.98")

    def extract_text(self, raw: bytes) -> str:
        """Default: decode structured text formats as UTF-8."""
        return raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)

    @abstractmethod
    def extract_rules(self, text: str) -> ExtractedRuleSet:
        ...

    # shared helper for structured parsers: rows -> ExtractedRuleSet
    def _build_ruleset(self, rows: list[dict]) -> ExtractedRuleSet:
        rules: list[ExtractedRule] = []
        warnings: list[str] = []
        for i, row in enumerate(rows):
            rule, w = normalize_rule(row)
            warnings.extend(f"row {i}: {msg}" for msg in w)
            if rule is None:
                continue
            if rule.confidence is None:
                rule = _with_confidence(rule, self.base_confidence)
            rules.append(rule)
        return ExtractedRuleSet(
            parser_name=self.name,
            parser_version=self.version,
            source=self.source,
            rules=tuple(rules),
            confidence=self.base_confidence if rules else Decimal("0"),
            warnings=tuple(warnings),
        )


def _with_confidence(rule: ExtractedRule, confidence: Decimal) -> ExtractedRule:
    from dataclasses import replace

    return replace(rule, confidence=confidence)
