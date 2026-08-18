"""The deterministic validators — what publication refuses, and why.

Two properties are load-bearing throughout:

**Fail closed.** A vocabulary the pipeline cannot see is a question it cannot
answer. The validation this replaced read `if ctx.known_facts and ...`, so an
empty context silently skipped every reference check and reported success.

**Resolve, never invent.** The formula checker this replaced bound any
unrecognized token to a placeholder before evaluating, which made an UNDECLARED
input pass. Tokens now resolve against declared inputs and registered operations
or they are errors.
"""
from decimal import Decimal

import pytest

from app.services.tax_kb.authoring import validation as v
from app.services.tax_kb.authoring.codes import Family, Severity, ValidationCode
from app.services.tax_kb.authoring.spec import (
    ReferenceDataSpec,
    TaxKnowledgeDraftSpec,
)
from tests.unit.tax_kb.test_knowledge_spec import CONDITION, OUTCOME, draft

CTX = v.KnowledgeContext(
    known_facts=frozenset({"employment_income", "age", "province", "is_resident"}),
    known_jurisdictions=frozenset({"FED", "ON", "BC"}),
    known_provinces=frozenset({"ON", "BC"}),
    known_years=frozenset({2024, 2025, 2026}),
    known_categories=frozenset({"deduction", "credit", "benefit"}),
    known_operators=frozenset({"eq", "gt", "gte", "lt", "lte", "between", "in",
                               "exists", "is_true"}),
    known_document_types=frozenset({"RRSP", "T4", "MEDICAL"}),
    known_registered_types=frozenset({"RRSP", "TFSA", "FHSA"}),
    known_deadline_codes=frozenset({"T1_FILING", "CONTRIBUTION"}),
    known_assumption_codes=frozenset({"INFLATION_2PCT"}),
    known_rule_codes=frozenset({"RRSP_BASIC", "OTHER_FED", "ON_ONLY", "BC_ONLY"}),
    rule_jurisdictions={"RRSP_BASIC": "FED", "OTHER_FED": "FED",
                        "ON_ONLY": "ON", "BC_ONLY": "BC"},
    published_reference_data=frozenset({("CALC_CONSTANT", "MEDICAL_FLOOR", 2025)}),
    published_formulas={},
    published_rule_versions={},
    rule_identities={},
)


def spec(**over) -> TaxKnowledgeDraftSpec:
    return TaxKnowledgeDraftSpec.read(draft(**over))


def codes(findings, severity=Severity.ERROR) -> set[str]:
    return {str(f.code) for f in findings if f.severity is severity}


# ===========================================================================
# Fail closed
# ===========================================================================
@pytest.mark.parametrize("check,expected", [
    (v.validate_structure, ValidationCode.UNKNOWN_CATEGORY),
    (v.validate_structure, ValidationCode.UNKNOWN_JURISDICTION),
    (v.validate_conditions, ValidationCode.UNKNOWN_CONDITION_FIELD),
    (v.validate_conditions, ValidationCode.UNKNOWN_OPERATOR),
])
def test_an_empty_vocabulary_refuses_rather_than_skips(check, expected):
    """The bug being prevented: `if ctx.known_facts and ...` turned a context
    that failed to load into a validator that approved everything."""
    assert str(expected) in codes(check(spec(), v.KnowledgeContext()))


def test_a_populated_context_accepts_the_reference_draft():
    findings = (v.validate_structure(spec(), CTX)
                + v.validate_conditions(spec(), CTX)
                + v.validate_outcomes(spec(), CTX))
    assert codes(findings) == set(), (
        "the fixture must be valid, or every negative test below proves nothing")


# ===========================================================================
# Scope
# ===========================================================================
def test_a_federal_rule_may_not_carry_a_province():
    findings = v.validate_structure(spec(province_code="ON"), CTX)
    assert str(ValidationCode.FEDERAL_RULE_WITH_PROVINCE) in codes(findings)


def test_an_unregistered_province_is_refused():
    findings = v.validate_structure(
        spec(jurisdiction_code="ON", province_code="ZZ"), CTX)
    assert str(ValidationCode.UNKNOWN_PROVINCE) in codes(findings)


def test_an_unseeded_tax_year_is_refused():
    findings = v.validate_structure(
        spec(tax_year=2099, effective_from="2099-01-01"), CTX)
    assert str(ValidationCode.UNKNOWN_TAX_YEAR) in codes(findings)


def test_a_rule_with_no_basis_codes_is_refused():
    """The evaluator reports an opportunity with no basis codes as
    'indeterminate', so such a rule could never say a user is eligible."""
    findings = v.validate_structure(spec(eligibility_basis_codes=[]), CTX)
    assert str(ValidationCode.MISSING_REQUIRED_FIELD) in codes(findings)


# ===========================================================================
# Effective period
# ===========================================================================
def test_an_inverted_effective_period_is_refused():
    findings = v.validate_structure(
        spec(effective_from="2025-01-01", effective_to="2024-06-01"), CTX)
    assert str(ValidationCode.INVALID_EFFECTIVE_PERIOD) in codes(findings)


def test_an_intra_year_effective_date_is_refused_with_the_reason():
    """The evaluator selects on (tax_year, status) and applies no date
    predicate, so a mid-year effective date would take effect on publication
    rather than on the date authored. Refused instead of published into a
    resolver that cannot honour it."""
    findings = v.validate_structure(spec(effective_from="2025-07-01"), CTX)
    assert str(ValidationCode.INTRA_YEAR_EFFECTIVE_SCOPE_UNSUPPORTED) in \
        codes(findings)


def test_future_effective_publication_is_supported_at_tax_year_granularity():
    """Publishing 2026 knowledge today is fine and stays invisible to every
    2025 evaluation, because tax year is the dimension the resolver selects on."""
    findings = v.validate_structure(
        spec(tax_year=2026, effective_from="2026-01-01"), CTX)
    assert codes(findings) == set()


def test_an_effective_period_outside_its_tax_year_is_refused():
    findings = v.validate_structure(
        spec(effective_from="2025-01-01", effective_to="2026-06-30"), CTX)
    assert str(ValidationCode.EFFECTIVE_PERIOD_OUTSIDE_TAX_YEAR) in codes(findings)


# ===========================================================================
# Conditions
# ===========================================================================
def test_an_undefined_fact_is_refused():
    findings = v.validate_conditions(spec(condition_group={
        "conditions": [{**CONDITION, "fact_key": "invented_fact"}]}), CTX)
    assert str(ValidationCode.UNKNOWN_CONDITION_FIELD) in codes(findings)


def test_an_unregistered_operator_is_refused():
    findings = v.validate_conditions(spec(condition_group={
        "conditions": [{**CONDITION, "operator": "approximately"}]}), CTX)
    assert str(ValidationCode.UNKNOWN_OPERATOR) in codes(findings)


def test_an_unknown_logical_operator_is_refused():
    findings = v.validate_conditions(spec(condition_group={
        "logical_op": "XOR", "conditions": [CONDITION]}), CTX)
    assert str(ValidationCode.UNKNOWN_LOGICAL_OPERATOR) in codes(findings)


def test_a_numeric_comparison_without_a_number_is_refused():
    findings = v.validate_conditions(spec(condition_group={
        "conditions": [{"fact_key": "age", "operator": "gt",
                        "value_type": "number"}]}), CTX)
    assert str(ValidationCode.CONDITION_OPERAND_MISSING) in codes(findings)


def test_between_needs_both_bounds_in_the_right_order():
    missing = v.validate_conditions(spec(condition_group={
        "conditions": [{"fact_key": "age", "operator": "between",
                        "value_type": "number", "value_number": "18"}]}), CTX)
    assert str(ValidationCode.CONDITION_OPERAND_MISSING) in codes(missing)

    inverted = v.validate_conditions(spec(condition_group={
        "conditions": [{"fact_key": "age", "operator": "between",
                        "value_type": "number", "value_number": "65",
                        "value_number_high": "18"}]}), CTX)
    assert str(ValidationCode.OPERAND_TYPE_MISMATCH) in codes(inverted)


def test_a_declared_type_that_does_not_match_the_operand_is_refused():
    """The engine would compare a number against a string and silently never
    match — a rule that excludes everybody without saying so."""
    findings = v.validate_conditions(spec(condition_group={
        "conditions": [{"fact_key": "province", "operator": "eq",
                        "value_type": "text", "value_number": "5"}]}), CTX)
    assert str(ValidationCode.OPERAND_TYPE_MISMATCH) in codes(findings)


def test_a_nullary_operator_with_an_operand_is_refused():
    findings = v.validate_conditions(spec(condition_group={
        "conditions": [{"fact_key": "is_resident", "operator": "exists",
                        "value_type": "boolean", "value_number": "1"}]}), CTX)
    assert str(ValidationCode.OPERAND_TYPE_MISMATCH) in codes(findings)


def test_a_duplicated_condition_is_refused():
    findings = v.validate_conditions(spec(condition_group={
        "conditions": [CONDITION, CONDITION]}), CTX)
    assert str(ValidationCode.DUPLICATE_CONDITION) in codes(findings)


def test_nested_groups_are_walked():
    findings = v.validate_conditions(spec(condition_group={
        "logical_op": "AND",
        "groups": [{"logical_op": "OR", "conditions": [
            {**CONDITION, "fact_key": "nope"}]}]}), CTX)
    assert str(ValidationCode.UNKNOWN_CONDITION_FIELD) in codes(findings)


# ===========================================================================
# Dependencies
# ===========================================================================
def test_an_unknown_dependency_target_is_refused():
    findings = v.validate_dependencies(spec(dependencies=[
        {"depends_on_rule_code": "NEVER_PUBLISHED",
         "dependency_type": "requires"}]), CTX)
    assert str(ValidationCode.UNKNOWN_DEPENDENCY_TARGET) in codes(findings)


def test_a_self_dependency_is_refused():
    findings = v.validate_dependencies(spec(dependencies=[
        {"depends_on_rule_code": "RRSP_BASIC", "dependency_type": "requires"}]),
        CTX)
    assert str(ValidationCode.SELF_DEPENDENCY) in codes(findings)


def test_an_unknown_dependency_kind_is_refused():
    findings = v.validate_dependencies(spec(dependencies=[
        {"depends_on_rule_code": "OTHER_FED", "dependency_type": "suggests"}]),
        CTX)
    assert str(ValidationCode.UNKNOWN_DEPENDENCY_KIND) in codes(findings)


def test_one_province_may_not_depend_on_another():
    findings = v.validate_dependencies(
        TaxKnowledgeDraftSpec.read(draft(
            rule_code="ON_ONLY", jurisdiction_code="ON", province_code="ON",
            dependencies=[{"depends_on_rule_code": "BC_ONLY",
                           "dependency_type": "requires"}])), CTX)
    assert str(ValidationCode.CROSS_JURISDICTION_DEPENDENCY) in codes(findings)


def test_a_provincial_rule_may_depend_on_a_federal_one():
    findings = v.validate_dependencies(
        TaxKnowledgeDraftSpec.read(draft(
            rule_code="ON_ONLY", jurisdiction_code="ON", province_code="ON",
            dependencies=[{"depends_on_rule_code": "OTHER_FED",
                           "dependency_type": "requires"}])), CTX)
    assert codes(findings) == set()


def test_a_duplicated_dependency_is_refused():
    findings = v.validate_dependencies(spec(dependencies=[
        {"depends_on_rule_code": "OTHER_FED", "dependency_type": "requires"},
        {"depends_on_rule_code": "OTHER_FED", "dependency_type": "requires"}]),
        CTX)
    assert str(ValidationCode.DUPLICATE_DEPENDENCY) in codes(findings)


def test_a_requires_cycle_is_named_rather_than_left_to_recursion():
    a = TaxKnowledgeDraftSpec.read(draft(
        rule_code="A", dependencies=[
            {"depends_on_rule_code": "B", "dependency_type": "requires"}]))
    b = TaxKnowledgeDraftSpec.read(draft(
        rule_code="B", dependencies=[
            {"depends_on_rule_code": "A", "dependency_type": "requires"}]))
    findings = v.detect_dependency_cycles([a, b])
    assert str(ValidationCode.DEPENDENCY_CYCLE) in codes(findings)
    assert any("A" in f.detail and "B" in f.detail for f in findings), (
        "a cycle report that names no rule cannot be acted on")


def test_a_longer_cycle_is_detected():
    specs = [
        TaxKnowledgeDraftSpec.read(draft(rule_code=code, dependencies=[
            {"depends_on_rule_code": nxt, "dependency_type": "requires"}]))
        for code, nxt in (("A", "B"), ("B", "C"), ("C", "A"))]
    assert str(ValidationCode.DEPENDENCY_CYCLE) in codes(
        v.detect_dependency_cycles(specs))


def test_mutual_exclusion_is_not_a_cycle():
    """Two rules that exclude each other is a correct statement about them.
    Walking every edge kind alike would reject valid knowledge."""
    specs = [
        TaxKnowledgeDraftSpec.read(draft(rule_code=code, dependencies=[
            {"depends_on_rule_code": other, "dependency_type": "excludes"}]))
        for code, other in (("A", "B"), ("B", "A"))]
    assert v.detect_dependency_cycles(specs) == []


def test_an_acyclic_chain_is_accepted():
    specs = [
        TaxKnowledgeDraftSpec.read(draft(rule_code="A", dependencies=[
            {"depends_on_rule_code": "B", "dependency_type": "requires"}])),
        TaxKnowledgeDraftSpec.read(draft(rule_code="B")),
    ]
    assert v.detect_dependency_cycles(specs) == []


# ===========================================================================
# Outcomes — including the §10 decision
# ===========================================================================
def test_a_rule_with_no_outcome_is_refused():
    findings = v.validate_outcomes(spec(outcomes=[]), CTX)
    assert str(ValidationCode.OUTCOME_REQUIRED) in codes(findings)


def test_an_unknown_outcome_type_is_refused():
    findings = v.validate_outcomes(spec(outcomes=[
        {**OUTCOME, "outcome_type": "suggest_strongly"}]), CTX)
    assert str(ValidationCode.UNKNOWN_OUTCOME_TYPE) in codes(findings)


def test_multiple_recommend_outcomes_are_permitted():
    """§10, decided on evidence. The crash this looked like a cardinality
    problem for was a collision in the IOE's semantic candidate key, fixed
    there. A publication restriction would not have fixed it either: two
    outcomes of DIFFERENT kinds collided identically, which is what settles it
    as a runtime defect rather than an invalid rule shape."""
    findings = v.validate_outcomes(spec(outcomes=[
        {**OUTCOME, "priority": 1, "title_template": "Contribute now"},
        {**OUTCOME, "priority": 2, "title_template": "Contribute in kind",
         "portfolio_lever_code": "INCREASE_DONATIONS"}]), CTX)
    assert codes(findings) == set()


def test_the_same_economic_consequence_twice_is_refused():
    """Differing prose does not make one consequence into two."""
    findings = v.validate_outcomes(spec(outcomes=[
        {**OUTCOME, "title_template": "Contribute"},
        {**OUTCOME, "title_template": "Put money in"}]), CTX)
    assert str(ValidationCode.DUPLICATE_OUTCOME) in codes(findings)


def test_an_unregistered_lever_is_refused():
    findings = v.validate_outcomes(spec(outcomes=[
        {**OUTCOME, "portfolio_lever_code": "MAKE_MONEY_APPEAR"}]), CTX)
    assert str(ValidationCode.UNKNOWN_PORTFOLIO_LEVER) in codes(findings)


def test_a_lever_without_parameters_is_refused():
    outcome = {k: val for k, val in OUTCOME.items() if k != "lever_parameters"}
    findings = v.validate_outcomes(spec(outcomes=[outcome]), CTX)
    assert str(ValidationCode.LEVER_PARAMETER_UNRESOLVABLE) in codes(findings)


def test_parameters_without_a_lever_are_refused():
    outcome = {k: val for k, val in OUTCOME.items()
               if k != "portfolio_lever_code"}
    findings = v.validate_outcomes(spec(outcomes=[outcome]), CTX)
    assert str(ValidationCode.LEVER_PARAMETER_UNRESOLVABLE) in codes(findings)


def test_an_unknown_economic_effect_type_is_refused():
    findings = v.validate_outcomes(spec(outcomes=[
        {**OUTCOME, "economic_effect_type": "makes_you_richer"}]), CTX)
    assert str(ValidationCode.UNKNOWN_ECONOMIC_EFFECT_TYPE) in codes(findings)


def test_a_recommendation_needs_a_title():
    outcome = {k: val for k, val in OUTCOME.items() if k != "title_template"}
    findings = v.validate_outcomes(spec(outcomes=[outcome]), CTX)
    assert str(ValidationCode.OUTCOME_FIELD_REQUIRED) in codes(findings)


# ===========================================================================
# Formula
# ===========================================================================
FORMULA = {
    "code": "RRSP_IMPACT",
    "expression": "contribution 0.3 *",
    "inputs": [{"param_name": "contribution", "fact_key": "employment_income"}],
}


def test_a_well_formed_formula_is_accepted():
    assert codes(v.validate_formula(spec(formula=FORMULA), CTX)) == set()


def test_an_undeclared_input_is_refused():
    """The check this replaced bound any unrecognized token to a placeholder,
    so an undeclared input made the expression VALID."""
    findings = v.validate_formula(spec(formula={
        **FORMULA, "expression": "contribution mystery_rate *"}), CTX)
    assert str(ValidationCode.FORMULA_INPUT_UNDECLARED) in codes(findings)


def test_an_unregistered_operation_is_refused():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "expression": "contribution 2 pow"}), CTX)
    assert str(ValidationCode.FORMULA_INPUT_UNDECLARED) in codes(findings)


def test_the_supported_operations_come_from_the_engine_not_a_second_list():
    from app.services.tax_engine.core import formula_sandbox

    for op in formula_sandbox.SUPPORTED_OPERATIONS:
        findings = v.validate_formula(spec(formula={
            **FORMULA, "expression": f"contribution 2 {op}"}), CTX)
        assert codes(findings) == set(), f"the engine implements {op!r}"


def test_a_formula_binding_an_unknown_fact_is_refused():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "inputs": [
            {"param_name": "contribution", "fact_key": "invented"}]}), CTX)
    assert str(ValidationCode.FORMULA_INPUT_UNKNOWN_FACT) in codes(findings)


def test_an_input_bound_to_nothing_is_refused():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "inputs": [{"param_name": "contribution"}]}), CTX)
    assert str(ValidationCode.FORMULA_INPUT_UNDECLARED) in codes(findings)


def test_a_literal_division_by_zero_is_refused():
    """The evaluator returns 0 for a zero divisor rather than raising, so this
    would publish as a silently wrong figure."""
    findings = v.validate_formula(spec(formula={
        **FORMULA, "expression": "contribution 0 /"}), CTX)
    assert str(ValidationCode.FORMULA_DIVIDES_BY_ZERO) in codes(findings)


@pytest.mark.parametrize("bad", [
    "contribution +",           # underflow: one operand, binary operator
    "contribution contribution",  # two values left on the stack
    "+ +",                      # underflow at the first token
])
def test_a_malformed_expression_is_refused(bad):
    findings = v.validate_formula(spec(formula={
        **FORMULA, "expression": bad}), CTX)
    assert str(ValidationCode.FORMULA_MALFORMED) in codes(findings)


def test_an_empty_expression_is_refused_before_validation_reaches_it():
    """Caught by the reader rather than the checker. A formula with no
    expression is not a malformed formula, it is a missing one."""
    from app.services.tax_kb.authoring.spec import SpecError

    with pytest.raises(SpecError, match="expression is required"):
        spec(formula={**FORMULA, "expression": "   "})


def test_an_unsupported_expression_language_is_refused():
    """No Python, no SQL, no eval. The only language is the one the sandbox
    evaluates."""
    findings = v.validate_formula(spec(formula={
        **FORMULA, "expression_lang": "python",
        "expression": "lambda x: x * 0.3"}), CTX)
    assert str(ValidationCode.FORMULA_LANGUAGE_UNSUPPORTED) in codes(findings)


def test_excess_literal_precision_is_refused():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "expression": "contribution 0.12345678 *"}), CTX)
    assert str(ValidationCode.FORMULA_PRECISION_EXCEEDED) in codes(findings)


def test_a_formula_conflicting_with_a_published_one_is_refused():
    ctx = v.KnowledgeContext(**{
        **CTX.__dict__, "published_formulas": {"RRSP_IMPACT": "contribution 0.4 *"}})
    findings = v.validate_formula(spec(formula=FORMULA), ctx)
    assert str(ValidationCode.FORMULA_CONFLICTS_WITH_PUBLISHED) in codes(findings)


def test_republishing_an_identical_formula_is_not_a_conflict():
    ctx = v.KnowledgeContext(**{
        **CTX.__dict__, "published_formulas": {"RRSP_IMPACT": "contribution 0.3 *"}})
    assert codes(v.validate_formula(spec(formula=FORMULA), ctx)) == set()


# ---- golden vectors --------------------------------------------------------
def test_a_correct_vector_passes_through_the_engines_own_evaluator():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "vectors": [
            {"name": "ten thousand", "inputs": {"contribution": "10000"},
             "expected": "3000.0"}]}), CTX)
    assert codes(findings) == set()


def test_a_wrong_vector_fails_with_the_computed_value():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "vectors": [
            {"name": "wrong", "inputs": {"contribution": "10000"},
             "expected": "3001"}]}), CTX)
    assert str(ValidationCode.FORMULA_VECTOR_FAILED) in codes(findings)
    assert any("3000" in f.detail for f in findings)


def test_vectors_compare_exactly_with_no_tolerance():
    """§28. A tolerance is how a rounding defect ships."""
    findings = v.validate_formula(spec(formula={
        **FORMULA, "vectors": [
            {"name": "off by a cent", "inputs": {"contribution": "10000"},
             "expected": "3000.01"}]}), CTX)
    assert str(ValidationCode.FORMULA_VECTOR_FAILED) in codes(findings)


def test_a_vector_supplying_an_undeclared_input_fails():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "vectors": [
            {"name": "x", "inputs": {"nope": "1"}, "expected": "0"}]}), CTX)
    assert str(ValidationCode.FORMULA_VECTOR_FAILED) in codes(findings)


def test_a_vector_leaving_an_input_unbound_fails():
    findings = v.validate_formula(spec(formula={
        **FORMULA, "vectors": [{"name": "x", "inputs": {}, "expected": "0"}]}),
        CTX)
    assert str(ValidationCode.FORMULA_VECTOR_FAILED) in codes(findings)


def test_a_boundary_vector_is_expressible():
    boundary = {
        "code": "FLOOR", "expression": "expense floor - 0 max",
        "inputs": [{"param_name": "expense", "fact_key": "employment_income"},
                   {"param_name": "floor", "literal_value": "2834"}],
        "vectors": [
            {"name": "just below", "inputs": {"expense": "2833"},
             "expected": "0"},
            {"name": "exactly at", "inputs": {"expense": "2834"},
             "expected": "0"},
            {"name": "just above", "inputs": {"expense": "2835"},
             "expected": "1"},
        ],
    }
    assert codes(v.validate_formula(spec(formula=boundary), CTX)) == set()


# ===========================================================================
# Evidence, deadlines, assumptions
# ===========================================================================
def test_an_ungoverned_document_type_is_refused():
    findings = v.validate_evidence(spec(evidence_requirements=[
        {"document_type_code": "SHOEBOX_OF_RECEIPTS"}]), CTX)
    assert str(ValidationCode.EVIDENCE_TYPE_UNKNOWN) in codes(findings)


def test_an_unknown_necessity_is_refused():
    findings = v.validate_evidence(spec(evidence_requirements=[
        {"document_type_code": "T4", "necessity": "nice_to_have"}]), CTX)
    assert str(ValidationCode.UNKNOWN_EVIDENCE_NECESSITY) in codes(findings)


def test_an_unregistered_deadline_code_is_refused():
    findings = v.validate_deadlines(spec(deadlines=[
        {"deadline_code": "SOMETIME_SOON", "deadline_date": "2026-04-30"}]), CTX)
    assert str(ValidationCode.DEADLINE_CODE_UNKNOWN) in codes(findings)


def test_a_deadline_with_no_date_is_refused():
    """The schema has no date-formula representation, so an undated deadline is
    one nothing can compute."""
    findings = v.validate_deadlines(spec(deadlines=[
        {"deadline_code": "T1_FILING"}]), CTX)
    assert str(ValidationCode.DEADLINE_INVALID) in codes(findings)


def test_a_deadline_far_outside_its_tax_year_is_refused():
    findings = v.validate_deadlines(spec(deadlines=[
        {"deadline_code": "T1_FILING", "deadline_date": "2031-04-30"}]), CTX)
    assert str(ValidationCode.DEADLINE_INVALID) in codes(findings)


def test_a_filing_deadline_in_the_following_year_is_accepted():
    findings = v.validate_deadlines(spec(deadlines=[
        {"deadline_code": "T1_FILING", "deadline_date": "2026-04-30"}]), CTX)
    assert codes(findings) == set()


def test_an_action_pointing_at_an_undeclared_deadline_is_refused():
    findings = v.validate_deadlines(spec(
        actions=[{"action_code": "FILE", "description": "File",
                  "deadline_code": "CONTRIBUTION"}],
        deadlines=[{"deadline_code": "T1_FILING",
                    "deadline_date": "2026-04-30"}]), CTX)
    assert str(ValidationCode.DEADLINE_INVALID) in codes(findings)


def test_an_unknown_assumption_code_is_refused():
    findings = v.validate_assumptions(spec(assumption_codes=["MARKETS_GO_UP"]), CTX)
    assert str(ValidationCode.ASSUMPTION_CODE_UNKNOWN) in codes(findings)


def test_declaring_no_assumptions_is_not_an_error():
    assert v.validate_assumptions(spec(), CTX) == []


def test_an_unknown_cost_type_is_refused():
    findings = v.validate_actions(spec(actions=[
        {"action_code": "C", "description": "d", "cost_type": "vibes"}]))
    assert str(ValidationCode.MALFORMED_VALUE) in codes(findings)


def test_an_effort_rating_outside_the_scale_is_refused():
    findings = v.validate_actions(spec(actions=[
        {"action_code": "C", "description": "d", "effort_rating": 9}]))
    assert str(ValidationCode.MALFORMED_VALUE) in codes(findings)


# ===========================================================================
# Examples
# ===========================================================================
EXAMPLES = [
    {"name": "high earner", "facts": {"employment_income": "90000"},
     "expect_eligible": True, "expected_outcome_types": ["recommend"]},
    {"name": "low earner", "facts": {"employment_income": "10000"},
     "expect_eligible": False},
]


def test_a_rule_with_no_examples_is_refused():
    findings = v.validate_examples(spec(), CTX)
    assert str(ValidationCode.EXAMPLES_REQUIRED) in codes(findings)


def test_a_rule_needs_a_negative_example():
    """Without one the gate has never been shown to exclude anybody, and the
    gate is half the rule."""
    findings = v.validate_examples(spec(examples=[EXAMPLES[0]]), CTX)
    assert str(ValidationCode.NEGATIVE_EXAMPLE_REQUIRED) in codes(findings)


def test_a_rule_needs_a_positive_example():
    findings = v.validate_examples(spec(examples=[EXAMPLES[1]]), CTX)
    assert str(ValidationCode.POSITIVE_EXAMPLE_REQUIRED) in codes(findings)


def test_an_example_supplying_an_undefined_fact_is_refused():
    findings = v.validate_examples(spec(examples=[
        {**EXAMPLES[0], "facts": {"invented": "1"}}, EXAMPLES[1]]), CTX)
    assert str(ValidationCode.EXAMPLE_FACT_UNKNOWN) in codes(findings)


def test_examples_run_against_the_engines_own_condition_evaluator():
    assert codes(v.run_examples(spec(examples=EXAMPLES))) == set()


def test_an_example_whose_expectation_is_wrong_fails():
    wrong = {"name": "wrong", "facts": {"employment_income": "10000"},
             "expect_eligible": True}
    findings = v.run_examples(spec(examples=[wrong]))
    assert str(ValidationCode.EXAMPLE_FAILED) in codes(findings)
    assert any("ineligible" in f.detail for f in findings)


def test_an_example_expecting_an_undeclared_outcome_type_fails():
    findings = v.run_examples(spec(examples=[
        {**EXAMPLES[0], "expected_outcome_types": ["apply_credit"]},
        EXAMPLES[1]]))
    assert str(ValidationCode.EXAMPLE_FAILED) in codes(findings)


def test_a_boolean_example_fact_reaches_the_engine_as_a_boolean():
    findings = v.run_examples(spec(
        condition_group={"conditions": [
            {"fact_key": "is_resident", "operator": "is_true",
             "value_type": "boolean"}]},
        examples=[
            {"name": "resident", "facts": {"is_resident": "true"},
             "expect_eligible": True},
            {"name": "non-resident", "facts": {"is_resident": "false"},
             "expect_eligible": False}]))
    assert codes(findings) == set()


# ===========================================================================
# Versioning
# ===========================================================================
def test_replacing_a_published_version_must_declare_supersession():
    import uuid as _uuid

    ctx = v.KnowledgeContext(**{
        **CTX.__dict__,
        "published_rule_versions": {("RRSP_BASIC", 2025): _uuid.uuid4()}})
    findings = v.validate_versioning(spec(), ctx)
    assert str(ValidationCode.SUPERSESSION_REQUIRED) in codes(findings)


def test_declaring_supersession_with_nothing_to_supersede_is_refused():
    findings = v.validate_versioning(spec(
        supersedes_version_id="00000000-0000-0000-0000-000000000001"), CTX)
    assert str(ValidationCode.SUPERSESSION_TARGET_MISSING) in codes(findings)


def test_a_first_version_needs_no_supersession():
    assert codes(v.validate_versioning(spec(), CTX)) == set()


# ===========================================================================
# Reference data
# ===========================================================================
def bracket_set(**over) -> ReferenceDataSpec:
    payload = {
        "kind": "TAX_BRACKET_SET", "key": "FED:income_tax", "tax_year": 2025,
        "jurisdiction_code": "FED",
        "brackets": [
            {"ordinal": 1, "lower_bound": "0", "upper_bound": "55867",
             "rate": "0.15"},
            {"ordinal": 2, "lower_bound": "55867", "rate": "0.205"},
        ],
    }
    return ReferenceDataSpec.read({**payload, **over})


def test_a_well_formed_bracket_table_is_accepted():
    assert codes(v.validate_reference_data(bracket_set(), CTX)) == set()


def test_overlapping_brackets_are_refused():
    findings = v.validate_reference_data(bracket_set(brackets=[
        {"ordinal": 1, "lower_bound": "0", "upper_bound": "60000",
         "rate": "0.15"},
        {"ordinal": 2, "lower_bound": "55867", "rate": "0.205"}]), CTX)
    assert str(ValidationCode.BRACKETS_OVERLAP) in codes(findings)


def test_a_gap_between_brackets_is_refused():
    """Income between the two would fall in no bracket and be taxed by
    accident rather than by rule."""
    findings = v.validate_reference_data(bracket_set(brackets=[
        {"ordinal": 1, "lower_bound": "0", "upper_bound": "50000",
         "rate": "0.15"},
        {"ordinal": 2, "lower_bound": "55867", "rate": "0.205"}]), CTX)
    assert str(ValidationCode.BRACKETS_NOT_CONTIGUOUS) in codes(findings)


def test_a_bounded_top_bracket_is_refused():
    findings = v.validate_reference_data(bracket_set(brackets=[
        {"ordinal": 1, "lower_bound": "0", "upper_bound": "55867",
         "rate": "0.15"},
        {"ordinal": 2, "lower_bound": "55867", "upper_bound": "200000",
         "rate": "0.205"}]), CTX)
    assert str(ValidationCode.TERMINAL_BRACKET_MISSING) in codes(findings)


def test_a_table_not_starting_at_zero_is_refused():
    findings = v.validate_reference_data(bracket_set(brackets=[
        {"ordinal": 1, "lower_bound": "1000", "rate": "0.15"}]), CTX)
    assert str(ValidationCode.BRACKETS_NOT_CONTIGUOUS) in codes(findings)


def test_unordered_ordinals_are_refused():
    findings = v.validate_reference_data(bracket_set(brackets=[
        {"ordinal": 2, "lower_bound": "0", "upper_bound": "55867",
         "rate": "0.15"},
        {"ordinal": 1, "lower_bound": "55867", "rate": "0.205"}]), CTX)
    assert str(ValidationCode.BRACKETS_NOT_ORDERED) in codes(findings)


def test_a_rate_outside_zero_to_one_is_refused():
    findings = v.validate_reference_data(bracket_set(brackets=[
        {"ordinal": 1, "lower_bound": "0", "rate": "1.5"}]), CTX)
    assert str(ValidationCode.RATE_OUT_OF_RANGE) in codes(findings)


def test_an_open_ended_bracket_in_the_middle_is_refused():
    findings = v.validate_reference_data(bracket_set(brackets=[
        {"ordinal": 1, "lower_bound": "0", "rate": "0.15"},
        {"ordinal": 2, "lower_bound": "55867", "rate": "0.205"}]), CTX)
    assert str(ValidationCode.TERMINAL_BRACKET_MISSING) in codes(findings)


def test_republishing_an_existing_reference_key_is_refused():
    """Published reference data is immutable: the tables are unique on their
    semantic key and carry no version column, so a correction would rewrite
    what sealed snapshots were computed from."""
    spec_ = ReferenceDataSpec.read({
        "kind": "CALC_CONSTANT", "key": "MEDICAL_FLOOR", "tax_year": 2025,
        "value": "0.03"})
    findings = v.validate_reference_data(spec_, CTX)
    assert str(ValidationCode.REFERENCE_DATA_ALREADY_PUBLISHED) in codes(findings)


def test_an_unknown_registered_type_is_refused():
    spec_ = ReferenceDataSpec.read({
        "kind": "CONTRIBUTION_LIMIT", "key": "CRYPTO_PLAN", "tax_year": 2025,
        "annual_limit": "31560"})
    findings = v.validate_reference_data(spec_, CTX)
    assert str(ValidationCode.UNKNOWN_REGISTERED_TYPE) in codes(findings)


def test_a_limit_that_limits_nothing_is_refused():
    spec_ = ReferenceDataSpec.read({
        "kind": "CONTRIBUTION_LIMIT", "key": "RRSP", "tax_year": 2025})
    findings = v.validate_reference_data(spec_, CTX)
    assert str(ValidationCode.MISSING_REQUIRED_FIELD) in codes(findings)


def test_a_reference_pin_to_nothing_published_is_refused():
    findings = v.validate_reference_data_refs(spec(
        reference_data_dependencies=[
            {"kind": "CALC_CONSTANT", "key": "NOT_THERE", "tax_year": 2025}]),
        CTX)
    assert str(ValidationCode.REFERENCE_DATA_MISSING) in codes(findings)


def test_a_reference_pin_to_published_data_resolves():
    findings = v.validate_reference_data_refs(spec(
        reference_data_dependencies=[
            {"kind": "CALC_CONSTANT", "key": "MEDICAL_FLOOR",
             "tax_year": 2025}]), CTX)
    assert codes(findings) == set()


def test_an_unknown_reference_data_kind_is_refused():
    findings = v.validate_reference_data_refs(spec(
        reference_data_dependencies=[
            {"kind": "VIBES_TABLE", "key": "X", "tax_year": 2025}]), CTX)
    assert str(ValidationCode.UNKNOWN_REFERENCE_DATA_KIND) in codes(findings)


# ===========================================================================
# The vocabularies themselves
# ===========================================================================
def test_every_finding_carries_a_family_and_a_closed_code():
    findings = (
        v.validate_structure(spec(category="nope"), CTX)
        + v.validate_conditions(spec(condition_group={
            "conditions": [{**CONDITION, "fact_key": "nope"}]}), CTX)
        + v.validate_outcomes(spec(outcomes=[]), CTX))
    assert findings
    for finding in findings:
        assert isinstance(finding.family, Family)
        assert isinstance(finding.code, ValidationCode)
        assert finding.severity in (Severity.ERROR, Severity.WARNING)


def test_a_decimal_never_becomes_a_float_anywhere_in_validation():
    """A float that reached a threshold comparison would make eligibility
    depend on binary rounding."""
    parsed = spec(condition_group={
        "conditions": [{**CONDITION, "value_number": "50000.10"}]})
    value = parsed.condition_group.conditions[0].value_number
    assert isinstance(value, Decimal)
    assert not isinstance(value, float)


# ===========================================================================
# The vocabulary promises only what it delivers
# ===========================================================================
def test_every_validation_code_is_emitted_by_a_real_check():
    """A code nothing can produce is a promise the pipeline does not keep.

    Machine consumers branch on these; an enum member that never appears tells a
    reader a check exists when it does not.
    """
    import inspect

    from app.services.tax_kb.authoring import (
        manifest,
        report,
        service,
        validation,
    )
    from app.services.tax_kb.authoring import (
        spec as spec_module,
    )

    text = "".join(inspect.getsource(m) for m in
                   (manifest, report, service, spec_module, validation))
    unused = [c.name for c in ValidationCode
              if f"ValidationCode.{c.name}" not in text]
    assert unused == [], f"codes nothing can emit: {unused}"


def test_a_rule_code_already_registered_under_a_different_identity_is_refused():
    """`tax_rule.code` is globally unique, so a draft reusing a code inherits
    that row's jurisdiction and category whatever the draft says — and would
    publish as something it does not claim to be."""
    ctx = v.KnowledgeContext(**{
        **CTX.__dict__,
        "rule_identities": {
            "RRSP_BASIC": ("ON", "credit", None, "ON", "RRSP deduction")}})
    findings = v.validate_structure(spec(), ctx)
    assert str(ValidationCode.RULE_CODE_CONFLICT) in codes(findings)


def test_a_matching_registered_identity_is_not_a_conflict():
    ctx = v.KnowledgeContext(**{
        **CTX.__dict__,
        "rule_identities": {
            "RRSP_BASIC": ("FED", "deduction", None, None,
                           "RRSP deduction")}})
    assert str(ValidationCode.RULE_CODE_CONFLICT) not in \
        codes(v.validate_structure(spec(), ctx))


def test_superseding_a_version_that_is_not_the_current_one_is_refused():
    """Replacing live authority while naming a different predecessor is a
    correction aimed at the wrong version."""
    import uuid as _uuid

    current = _uuid.uuid4()
    ctx = v.KnowledgeContext(**{
        **CTX.__dict__,
        "published_rule_versions": {("RRSP_BASIC", 2025): current}})
    findings = v.validate_versioning(
        spec(supersedes_version_id=str(_uuid.uuid4())), ctx)
    assert str(ValidationCode.SUPERSESSION_TARGET_INVALID) in codes(findings)


def test_superseding_the_current_version_is_accepted():
    import uuid as _uuid

    current = _uuid.uuid4()
    ctx = v.KnowledgeContext(**{
        **CTX.__dict__,
        "published_rule_versions": {("RRSP_BASIC", 2025): current}})
    assert codes(v.validate_versioning(
        spec(supersedes_version_id=str(current)), ctx)) == set()


def test_the_pipeline_never_reaches_for_customer_data():
    """§40. Whether knowledge is publishable is decided from the specification,
    its provenance and synthetic examples. A validator that consulted a real
    taxpayer would make publication depend on who happened to be in the
    database."""
    import inspect
    import re

    from app.services.tax_kb.authoring import (
        manifest,
        policy,
        report,
        service,
        validation,
    )
    from app.services.tax_kb.authoring import (
        spec as spec_module,
    )

    # Word-bounded: `DocumentType` is a reference VOCABULARY the validators must
    # read, and a substring match would flag it as customer data. A test that
    # fails on the thing it is supposed to allow gets weakened, not fixed.
    customer = (r"\bUserAccount\b", r"\bTaxProfile\b", r"\bIncomeSource\b",
                r"\bExpenseRecord\b", r"\bDocument\b", r"\bAnalysisRun\b",
                r"\bOptimizationRun\b", r"\bRecommendation\b", r"\buser_id\b",
                r"\bprofile\.", r"\bfinance\.", r"\bwealth\.", r"\bdocs\.")
    for module in (manifest, policy, report, service, spec_module, validation):
        source = inspect.getsource(module)
        for pattern in customer:
            assert not re.search(pattern, source), \
                f"{module.__name__} references {pattern}"


def test_the_formula_language_is_exactly_what_the_engine_implements():
    """§7/§14. The registered primitives are the sandbox's, and there are six.

    Recorded rather than assumed, because the production Canadian corpus will
    want operations this list does not have — ROUND above all — and discovering
    that while loading content is worse than knowing it now.
    """
    from app.services.tax_engine.core.formula_sandbox import SUPPORTED_OPERATIONS

    assert SUPPORTED_OPERATIONS == frozenset({"+", "-", "*", "/", "min", "max"})
    for absent in ("round", "ROUND", "bracket_lookup", "reference_lookup", "abs"):
        assert absent not in SUPPORTED_OPERATIONS, (
            f"{absent!r} appeared without the authoring validator learning about it")
