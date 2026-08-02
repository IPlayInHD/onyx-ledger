"""Pure validation checks — errors block, warnings advise."""
from decimal import Decimal

from app.services.tkms.domain.models import EligibilityCondition, ExtractedRule, FormulaSpec
from app.services.tkms.validation.checks import (
    ValidationContext,
    check_batch_duplicates,
    check_dates,
    check_formula,
    check_jurisdiction,
    check_reference_integrity,
    check_thresholds,
    validate_rule,
)

CTX = ValidationContext(
    known_facts=frozenset({"income.total", "profile.age"}),
    known_jurisdictions=frozenset({"FED", "ON", "BC"}),
    known_provinces=frozenset({"ON", "BC"}),
    known_years=frozenset({2024, 2025}),
)


def _rule(**kw) -> ExtractedRule:
    base = dict(rule_code="R", name="R", category="credit", jurisdiction="FED", tax_year=2025)
    base.update(kw)
    return ExtractedRule(**base)


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_clean_rule_has_no_errors():
    r = _rule(description="A credit", max_amount=Decimal("1000"), reduction_rate=Decimal("0.03"))
    findings = validate_rule(r, CTX)
    assert not any(f.severity == "error" for f in findings)


def test_unknown_jurisdiction_and_year_error():
    r = _rule(jurisdiction="ZZ", tax_year=1999)
    codes = _codes(validate_rule(r, CTX))
    assert "UNKNOWN_JURISDICTION" in codes
    assert "UNKNOWN_TAX_YEAR" in codes


def test_federal_rule_with_province_errors():
    r = _rule(jurisdiction="FED", province="ON")
    assert "FED_WITH_PROVINCE" in _codes(check_jurisdiction(r, CTX))


def test_inverted_thresholds_and_bad_rate():
    r = _rule(income_threshold_low=Decimal("50000"), income_threshold_high=Decimal("10000"),
              reduction_rate=Decimal("1.5"), min_amount=Decimal("100"), max_amount=Decimal("10"))
    codes = _codes(check_thresholds(r))
    assert {"THRESHOLD_INVERTED", "RATE_OUT_OF_RANGE", "AMOUNT_INVERTED"} <= codes


def test_expiry_before_effective_errors():
    r = _rule(effective_date="2025-06-01", expiry_date="2025-01-01")
    assert "EXPIRY_BEFORE_EFFECTIVE" in _codes(check_dates(r))


def test_effective_year_mismatch_warns():
    r = _rule(effective_date="2024-01-01", tax_year=2025)
    findings = check_dates(r)
    assert any(f.code == "EFFECTIVE_YEAR_MISMATCH" and f.severity == "warning" for f in findings)


def test_unknown_fact_in_condition_errors():
    r = _rule(eligibility_conditions=(
        EligibilityCondition(fact_key="not.a.fact", operator="gt", value_number=Decimal("1")),
    ))
    assert "UNKNOWN_FACT" in _codes(check_reference_integrity(r, CTX))


def test_bad_operator_errors():
    r = _rule(eligibility_conditions=(
        EligibilityCondition(fact_key="profile.age", operator="wat"),
    ))
    assert "BAD_OPERATOR" in _codes(check_reference_integrity(r, CTX))


def test_valid_formula_passes_and_bad_formula_errors():
    good = _rule(formula=FormulaSpec(code="F", expression="income.total 0.18 *",
                                     inputs=(("income.total", "income.total"),)))
    assert check_formula(good) == []

    bad = _rule(formula=FormulaSpec(code="F", expression="income.total 0.18 * +"))  # stack underflow
    assert "BAD_FORMULA" in _codes(check_formula(bad))


def test_formula_input_unknown_fact_errors():
    r = _rule(formula=FormulaSpec(code="F", expression="x 2 *", inputs=(("x", "unknown.fact"),)))
    assert "UNKNOWN_FORMULA_FACT" in _codes(check_reference_integrity(r, CTX))


def test_batch_duplicate_and_conflict():
    a = _rule(rule_code="DUP", max_amount=Decimal("1"))
    a2 = _rule(rule_code="DUP", max_amount=Decimal("1"))          # identical
    b = _rule(rule_code="CON", max_amount=Decimal("1"))
    b2 = _rule(rule_code="CON", max_amount=Decimal("2"))          # conflicting
    codes = _codes(check_batch_duplicates([a, a2, b, b2]))
    assert "DUPLICATE_RULE" in codes
    assert "CONFLICTING_RULE" in codes
