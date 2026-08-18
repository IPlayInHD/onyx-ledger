"""The authoring contract — what a draft may say, and what identifies it.

What is at stake: that a field an author wrote is never silently dropped, that a
number never arrives as a float, and that two people who write the same rule in
a different order produce the same specification identity — because publication
binds to that identity and a hash that moved for cosmetic reasons would refuse a
draft nothing had changed about.
"""
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.tax_kb.authoring.spec import (
    KNOWLEDGE_SPEC_SCHEMA_VERSION,
    MAX_CONDITION_DEPTH,
    ReferenceDataSpec,
    SpecError,
    TaxKnowledgeDraftSpec,
    pack_identity,
)

CONDITION = {
    "fact_key": "employment_income", "operator": "gt",
    "value_type": "money", "value_number": "50000",
}
OUTCOME = {
    "outcome_type": "recommend", "priority": 1,
    "title_template": "Contribute to your RRSP",
    "economic_effect_type": "current_year_tax_reduction",
    "reversibility": "reversible",
    "portfolio_lever_code": "INCREASE_RRSP_DEDUCTION",
    "lever_parameters": {"amount": "action.cost_amount"},
}
DRAFT = {
    "rule_code": "RRSP_BASIC",
    "name": "RRSP deduction",
    "category": "deduction",
    "jurisdiction_code": "FED",
    "tax_year": 2025,
    "effective_from": "2025-01-01",
    "description": "Deduct RRSP contributions.",
    "eligibility_basis_codes": ["ITA_146"],
    "condition_group": {"logical_op": "AND", "conditions": [CONDITION]},
    "outcomes": [OUTCOME],
}


def draft(**over) -> dict:
    return {**DRAFT, **over}


# ===========================================================================
# Reading — fail closed
# ===========================================================================
def test_a_valid_draft_parses_into_governed_values():
    spec = TaxKnowledgeDraftSpec.read(draft())
    assert spec.rule_code == "RRSP_BASIC"
    assert spec.tax_year == 2025
    assert spec.effective_from == date(2025, 1, 1)
    assert spec.schema_version == KNOWLEDGE_SPEC_SCHEMA_VERSION
    assert spec.condition_group is not None
    assert spec.condition_group.conditions[0].value_number == Decimal("50000")
    assert spec.outcomes[0].portfolio_lever_code == "INCREASE_RRSP_DEDUCTION"


def test_an_unknown_top_level_field_is_refused_rather_than_ignored():
    """A field the pipeline drops is a rule that does not say what its author
    wrote. `legal_authority_rank` meant something to whoever typed it."""
    with pytest.raises(SpecError, match="unknown field"):
        TaxKnowledgeDraftSpec.read(draft(legal_authority_rank="1"))


@pytest.mark.parametrize("path,payload", [
    ("condition", {"condition_group": {"conditions": [{**CONDITION, "hint": "x"}]}}),
    ("outcome", {"outcomes": [{**OUTCOME, "confidence": "0.9"}]}),
    ("action", {"actions": [{"action_code": "C", "description": "d", "urgency": 1}]}),
    ("dependency", {"dependencies": [
        {"depends_on_rule_code": "X", "dependency_type": "requires", "weight": "1"}]}),
    ("deadline", {"deadlines": [
        {"deadline_code": "T1_FILING", "hard": True}]}),
    ("formula", {"formula": {
        "code": "F", "expression": "a", "language": "python"}}),
    ("example", {"examples": [
        {"name": "n", "expect_eligible": True, "note": "x"}]}),
])
def test_an_unknown_field_in_any_child_is_refused(path, payload):
    with pytest.raises(SpecError, match="unknown"):
        TaxKnowledgeDraftSpec.read(draft(**payload))


def test_a_draft_from_another_contract_version_is_refused():
    with pytest.raises(SpecError, match="authoring schema"):
        TaxKnowledgeDraftSpec.read(draft(schema_version="2.0.0"))


@pytest.mark.parametrize("field", [
    "rule_code", "name", "category", "jurisdiction_code", "description"])
def test_a_blank_required_field_is_refused(field):
    with pytest.raises(SpecError, match=field):
        TaxKnowledgeDraftSpec.read(draft(**{field: "   "}))


def test_a_missing_required_field_is_refused():
    payload = draft()
    del payload["effective_from"]
    with pytest.raises(SpecError, match="effective_from"):
        TaxKnowledgeDraftSpec.read(payload)


# ===========================================================================
# Decimals — the value an author wrote is the value that publishes
# ===========================================================================
def test_a_json_float_is_refused_where_a_decimal_belongs():
    """`0.1` in JSON is already not one tenth by the time Python reads it.
    Coercing it would publish a number nobody wrote."""
    bad = {**CONDITION, "value_number": 50000.5}
    with pytest.raises(SpecError, match="decimal STRING"):
        TaxKnowledgeDraftSpec.read(
            draft(condition_group={"conditions": [bad]}))


def test_a_decimal_string_keeps_every_digit():
    payload = {**CONDITION, "value_number": "0.1"}
    spec = TaxKnowledgeDraftSpec.read(
        draft(condition_group={"conditions": [payload]}))
    value = spec.condition_group.conditions[0].value_number
    assert value == Decimal("0.1")
    assert value + Decimal("0.2") == Decimal("0.3"), (
        "the point of refusing floats")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_decimal_is_refused(bad):
    with pytest.raises(SpecError, match="finite"):
        TaxKnowledgeDraftSpec.read(
            draft(condition_group={
                "conditions": [{**CONDITION, "value_number": bad}]}))


def test_a_boolean_where_a_number_belongs_is_refused():
    with pytest.raises(SpecError):
        TaxKnowledgeDraftSpec.read(
            draft(condition_group={
                "conditions": [{**CONDITION, "value_number": True}]}))


def test_a_number_where_a_flag_belongs_is_refused():
    with pytest.raises(SpecError, match="true or false"):
        TaxKnowledgeDraftSpec.read(draft(
            deadlines=[{"deadline_code": "T1_FILING", "is_hard": 1}]))


# ===========================================================================
# Structure limits
# ===========================================================================
def test_condition_nesting_is_bounded():
    group = {"logical_op": "AND", "conditions": [CONDITION]}
    for _ in range(MAX_CONDITION_DEPTH + 1):
        group = {"logical_op": "AND", "groups": [group]}
    with pytest.raises(SpecError, match="nest deeper"):
        TaxKnowledgeDraftSpec.read(draft(condition_group=group))


def test_a_list_where_an_object_belongs_is_refused():
    with pytest.raises(SpecError, match="must be an object"):
        TaxKnowledgeDraftSpec.read(draft(condition_group=[CONDITION]))


def test_an_object_where_a_list_belongs_is_refused():
    with pytest.raises(SpecError, match="must be a list"):
        TaxKnowledgeDraftSpec.read(draft(outcomes=OUTCOME))


def test_an_example_must_say_what_it_expects():
    """An example with no expectation proves nothing; defaulting it would
    invent the expectation."""
    with pytest.raises(SpecError, match="expect_eligible"):
        TaxKnowledgeDraftSpec.read(draft(examples=[{"name": "x"}]))


def test_a_malformed_identifier_is_refused():
    with pytest.raises(SpecError, match="not an identifier"):
        TaxKnowledgeDraftSpec.read(draft(citation_ids=["not-a-uuid"]))


# ===========================================================================
# Identity — semantic, not textual
# ===========================================================================
def test_the_same_knowledge_written_in_a_different_key_order_is_one_identity():
    a = TaxKnowledgeDraftSpec.read(draft())
    reordered = {k: draft()[k] for k in reversed(list(draft()))}
    b = TaxKnowledgeDraftSpec.read(reordered)
    assert a.spec_identity() == b.spec_identity()


def test_child_collections_that_are_sets_hash_order_independently():
    """Two evidence requirements are a SET of requirements. Which one an author
    typed first is not part of what the rule means."""
    a = TaxKnowledgeDraftSpec.read(draft(evidence_requirements=[
        {"document_type_code": "RRSP"}, {"document_type_code": "T4"}]))
    b = TaxKnowledgeDraftSpec.read(draft(evidence_requirements=[
        {"document_type_code": "T4"}, {"document_type_code": "RRSP"}]))
    assert a.spec_identity() == b.spec_identity()


def test_condition_order_is_part_of_the_rule_and_changes_identity():
    """Unlike a set of requirements, a condition list is read in order and the
    stored rows carry that order; sorting it would make two different stored
    rules hash alike."""
    second = {**CONDITION, "fact_key": "age", "value_type": "number",
              "value_number": "18"}
    a = TaxKnowledgeDraftSpec.read(
        draft(condition_group={"conditions": [CONDITION, second]}))
    b = TaxKnowledgeDraftSpec.read(
        draft(condition_group={"conditions": [second, CONDITION]}))
    assert a.spec_identity() != b.spec_identity()


def test_any_semantic_change_moves_the_identity():
    base = TaxKnowledgeDraftSpec.read(draft()).spec_identity()
    for change in (
        {"tax_year": 2024, "effective_from": "2024-01-01"},
        {"description": "Something else."},
        {"category": "credit"},
        {"eligibility_basis_codes": ["ITA_147"]},
        {"condition_group": {"conditions": [
            {**CONDITION, "value_number": "50001"}]}},
        {"outcomes": [{**OUTCOME, "priority": 2}]},
    ):
        assert TaxKnowledgeDraftSpec.read(draft(**change)).spec_identity() != base


def test_a_money_scale_difference_is_not_a_semantic_difference():
    """`50000` and `50000.00` are the same threshold. The canonical scale says
    so, which is what lets a spec rebuilt from a numeric column match the one
    that was authored."""
    a = TaxKnowledgeDraftSpec.read(draft(condition_group={
        "conditions": [{**CONDITION, "value_number": "50000"}]}))
    b = TaxKnowledgeDraftSpec.read(draft(condition_group={
        "conditions": [{**CONDITION, "value_number": "50000.00"}]}))
    assert a.spec_identity() == b.spec_identity()


def test_a_money_threshold_and_a_rate_threshold_do_not_collide():
    money = TaxKnowledgeDraftSpec.read(draft(condition_group={
        "conditions": [{**CONDITION, "value_type": "money",
                        "value_number": "0.15"}]}))
    percent = TaxKnowledgeDraftSpec.read(draft(condition_group={
        "conditions": [{**CONDITION, "value_type": "percent",
                        "value_number": "0.15"}]}))
    assert money.spec_identity() != percent.spec_identity()


def test_a_pack_identity_ignores_the_order_members_were_listed_in():
    assert pack_identity(["a", "b"]) == pack_identity(["b", "a"])
    assert pack_identity(["a", "b"]) != pack_identity(["a", "c"])


def test_the_spec_identity_is_invariant_under_pythonhashseed(tmp_path: Path):
    script = tmp_path / "run.py"
    script.write_text(
        "from tests.unit.tax_kb.test_knowledge_spec import draft\n"
        "from app.services.tax_kb.authoring.spec import TaxKnowledgeDraftSpec\n"
        "print(TaxKnowledgeDraftSpec.read(draft()).spec_identity())\n")
    digests = set()
    for seed in ("0", "1", "42"):
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True,
            check=True, cwd=str(Path(__file__).parents[3]),
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
                 "PYTHONPATH": str(Path(__file__).parents[3])})
        digests.add(result.stdout.strip())
    assert len(digests) == 1, f"seed variation moved the identity: {digests}"


def test_the_identity_is_domain_separated_from_every_other_artifact():
    from app.services.ioe.domain import canonical as c

    spec = TaxKnowledgeDraftSpec.read(draft())
    payload = spec.as_canonical()
    assert spec.spec_identity() == c.domain_hash(c.DOMAIN_KNOWLEDGE_SPEC, payload)
    assert spec.spec_identity() != c.canonical_hash(payload), (
        "an undomained digest could coincide with another artifact type's")


# ===========================================================================
# Reference data
# ===========================================================================
BRACKETS = {
    "kind": "TAX_BRACKET_SET", "key": "FED:income_tax", "tax_year": 2025,
    "jurisdiction_code": "FED",
    "brackets": [
        {"ordinal": 1, "lower_bound": "0", "upper_bound": "55867", "rate": "0.15"},
        {"ordinal": 2, "lower_bound": "55867", "rate": "0.205"},
    ],
}


def test_reference_data_parses_and_identifies_itself():
    spec = ReferenceDataSpec.read(BRACKETS)
    assert spec.brackets[0].rate == Decimal("0.15")
    assert spec.brackets[1].upper_bound is None
    assert len(spec.spec_identity()) == 64


def test_reference_data_refuses_an_unknown_field():
    with pytest.raises(SpecError, match="unknown field"):
        ReferenceDataSpec.read({**BRACKETS, "province": "ON"})


def test_reference_data_refuses_a_float_rate():
    with pytest.raises(SpecError, match="decimal STRING"):
        ReferenceDataSpec.read({**BRACKETS, "brackets": [
            {"ordinal": 1, "lower_bound": "0", "rate": 0.15}]})


def test_bracket_order_is_part_of_the_table_identity():
    reversed_table = {**BRACKETS, "brackets": list(reversed(BRACKETS["brackets"]))}
    assert ReferenceDataSpec.read(BRACKETS).spec_identity() != \
        ReferenceDataSpec.read(reversed_table).spec_identity()
