"""Pure validation checks over the canonical ExtractedRule contract.

Every check is a pure function: given a rule (and a reference context of what the
system knows) it returns findings, never touching the DB. The service loads the
context, runs these, and persists the results. Errors block promotion/publication;
warnings are advisory. This is the gate that keeps a botched import — including
anything an AI-assisted parser proposed — from ever reaching the deterministic
engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.services.tax_engine.core.formula_sandbox import evaluate_rpn
from app.services.tkms.domain.models import (
    VALID_CATEGORIES,
    VALID_OPERATORS,
    ExtractedRule,
    ValidationFinding,
)


@dataclass(frozen=True)
class ValidationContext:
    """What the system currently knows — used for reference-integrity checks."""

    known_facts: frozenset[str] = frozenset()
    known_jurisdictions: frozenset[str] = frozenset()
    known_provinces: frozenset[str] = frozenset()
    known_years: frozenset[int] = frozenset()
    valid_categories: frozenset[str] = field(default_factory=lambda: frozenset(VALID_CATEGORIES))
    valid_operators: frozenset[str] = field(default_factory=lambda: frozenset(VALID_OPERATORS))


def _err(code: str, msg: str, ref: str | None) -> ValidationFinding:
    return ValidationFinding(severity="error", code=code, message=msg, rule_ref=ref)


def _warn(code: str, msg: str, ref: str | None) -> ValidationFinding:
    return ValidationFinding(severity="warning", code=code, message=msg, rule_ref=ref)


def check_schema(rule: ExtractedRule) -> list[ValidationFinding]:
    ref = rule.rule_code
    out: list[ValidationFinding] = []
    if not rule.rule_code:
        out.append(_err("MISSING_CODE", "rule_code is required", ref))
    if not rule.name:
        out.append(_err("MISSING_NAME", "name is required", ref))
    if not rule.description:
        out.append(_warn("MISSING_DESCRIPTION", "description is empty", ref))
    return out


def check_category(rule: ExtractedRule, ctx: ValidationContext) -> list[ValidationFinding]:
    if rule.category not in ctx.valid_categories:
        return [_err("BAD_CATEGORY", f"unknown category '{rule.category}'", rule.rule_code)]
    return []


def check_jurisdiction(rule: ExtractedRule, ctx: ValidationContext) -> list[ValidationFinding]:
    ref = rule.rule_code
    out: list[ValidationFinding] = []
    if ctx.known_jurisdictions and rule.jurisdiction not in ctx.known_jurisdictions:
        out.append(_err("UNKNOWN_JURISDICTION",
                        f"jurisdiction '{rule.jurisdiction}' is not recognized", ref))
    if rule.jurisdiction == "FED" and rule.province:
        out.append(_err("FED_WITH_PROVINCE",
                        f"federal rule must not carry a province ('{rule.province}')", ref))
    if rule.province and ctx.known_provinces and rule.province not in ctx.known_provinces:
        out.append(_err("UNKNOWN_PROVINCE", f"province '{rule.province}' is not recognized", ref))
    return out


def check_year(rule: ExtractedRule, ctx: ValidationContext) -> list[ValidationFinding]:
    if ctx.known_years and rule.tax_year not in ctx.known_years:
        return [_err("UNKNOWN_TAX_YEAR",
                     f"tax_year {rule.tax_year} is not a seeded reference year", rule.rule_code)]
    return []


def check_dates(rule: ExtractedRule) -> list[ValidationFinding]:
    ref = rule.rule_code
    out: list[ValidationFinding] = []
    eff = _try_date(rule.effective_date_or_default())
    if eff is None:
        out.append(_err("BAD_EFFECTIVE_DATE",
                        f"effective_date '{rule.effective_date}' is not a valid date", ref))
        return out
    if rule.expiry_date:
        exp = _try_date(rule.expiry_date)
        if exp is None:
            out.append(_err("BAD_EXPIRY_DATE",
                            f"expiry_date '{rule.expiry_date}' is not a valid date", ref))
        elif exp < eff:
            out.append(_err("EXPIRY_BEFORE_EFFECTIVE",
                            f"expiry_date {exp} precedes effective_date {eff}", ref))
    if eff.year != rule.tax_year:
        out.append(_warn("EFFECTIVE_YEAR_MISMATCH",
                         f"effective_date year {eff.year} != tax_year {rule.tax_year}", ref))
    return out


def check_thresholds(rule: ExtractedRule) -> list[ValidationFinding]:
    ref = rule.rule_code
    out: list[ValidationFinding] = []
    lo, hi = rule.income_threshold_low, rule.income_threshold_high
    if lo is not None and hi is not None and lo > hi:
        out.append(_err("THRESHOLD_INVERTED",
                        f"income_threshold_low {lo} > income_threshold_high {hi}", ref))
    if rule.max_amount is not None and rule.min_amount is not None and rule.min_amount > rule.max_amount:
        out.append(_err("AMOUNT_INVERTED",
                        f"min_amount {rule.min_amount} > max_amount {rule.max_amount}", ref))
    if rule.reduction_rate is not None and not (Decimal(0) <= rule.reduction_rate <= Decimal(1)):
        out.append(_err("RATE_OUT_OF_RANGE",
                        f"reduction_rate {rule.reduction_rate} outside 0..1", ref))
    for f in ("max_amount", "min_amount", "contribution_limit"):
        val = getattr(rule, f)
        if val is not None and val < 0:
            out.append(_err("NEGATIVE_AMOUNT", f"{f} {val} is negative", ref))
    return out


def check_reference_integrity(rule: ExtractedRule, ctx: ValidationContext) -> list[ValidationFinding]:
    """Conditions/formula-inputs must reference facts & operators the engine knows."""
    ref = rule.rule_code
    out: list[ValidationFinding] = []
    for i, cond in enumerate(rule.eligibility_conditions):
        if cond.operator not in ctx.valid_operators:
            out.append(_err("BAD_OPERATOR",
                            f"condition {i}: unknown operator '{cond.operator}'", ref))
        if ctx.known_facts and cond.fact_key not in ctx.known_facts:
            out.append(_err("UNKNOWN_FACT",
                            f"condition {i}: fact '{cond.fact_key}' is not defined", ref))
        if cond.operator == "between" and cond.value_number_high is None:
            out.append(_err("BETWEEN_MISSING_HIGH",
                            f"condition {i}: 'between' needs value_number_high", ref))
    if rule.formula is not None:
        for param, fact_key in rule.formula.inputs:
            if ctx.known_facts and fact_key not in ctx.known_facts:
                out.append(_err("UNKNOWN_FORMULA_FACT",
                                f"formula input '{param}' → unknown fact '{fact_key}'", ref))
    return out


def check_formula(rule: ExtractedRule) -> list[ValidationFinding]:
    """The RPN expression must be well-formed under the engine's sandbox."""
    if rule.formula is None:
        return []
    ref = rule.rule_code
    if rule.formula.expression_lang != "rpn":
        return [_warn("FORMULA_LANG_UNCHECKED",
                      f"formula language '{rule.formula.expression_lang}' not statically checked", ref)]
    # bind every declared param (and any bare identifier) to a placeholder so a
    # structurally valid expression evaluates; a malformed one raises.
    variables = {param: Decimal(1) for param, _ in rule.formula.inputs}
    for token in rule.formula.expression.split():
        if not _is_number(token) and token not in _RPN_OPS and token not in variables:
            variables[token] = Decimal(1)
    try:
        evaluate_rpn(rule.formula.expression, variables)
    except (ValueError, ArithmeticError) as e:
        return [_err("BAD_FORMULA", f"formula does not evaluate: {e}", ref)]
    return []


def check_batch_duplicates(rules: list[ExtractedRule]) -> list[ValidationFinding]:
    """Within one import, (rule_code, tax_year) must be unique; clashing content
    is a conflict."""
    out: list[ValidationFinding] = []
    seen: dict[tuple[str, int], ExtractedRule] = {}
    for rule in rules:
        key = (rule.rule_code, rule.tax_year)
        if key in seen:
            prior = seen[key]
            if prior.as_payload() == rule.as_payload():
                out.append(_err("DUPLICATE_RULE",
                                f"duplicate rule {rule.rule_code} for {rule.tax_year}", rule.rule_code))
            else:
                out.append(_err("CONFLICTING_RULE",
                                f"conflicting definitions for {rule.rule_code} in {rule.tax_year}",
                                rule.rule_code))
        else:
            seen[key] = rule
    return out


_RPN_OPS = {"+", "-", "*", "/", "min", "max"}


def _is_number(token: str) -> bool:
    try:
        Decimal(token)
        return True
    except Exception:
        return False


def _try_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def validate_rule(rule: ExtractedRule, ctx: ValidationContext) -> list[ValidationFinding]:
    """Run every per-rule check and collect findings."""
    findings: list[ValidationFinding] = []
    findings += check_schema(rule)
    findings += check_category(rule, ctx)
    findings += check_jurisdiction(rule, ctx)
    findings += check_year(rule, ctx)
    findings += check_dates(rule)
    findings += check_thresholds(rule)
    findings += check_reference_integrity(rule, ctx)
    findings += check_formula(rule)
    return findings
