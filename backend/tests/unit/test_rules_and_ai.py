"""Unit tests for the pure rules-engine primitives + the AI guardrail."""
from decimal import Decimal

from app.services.ai.service import AiExplanationService
from app.services.tax_engine.core.condition_eval import eval_group
from app.services.tax_engine.core.formula_sandbox import evaluate_rpn


def test_formula_medical_credit():
    # max(0, medical - min(net*0.03, 2834)) * 0.145
    expr = "medical net 0.03 * 2834 min - 0 max 0.145 *"
    val = evaluate_rpn(expr, {"medical": Decimal(3000), "net": Decimal(75000)})
    # (3000 - min(2250, 2834)) * 0.145 = 750 * 0.145 = 108.75
    assert val == Decimal("108.750")


def test_formula_rejects_unknown_token():
    import pytest
    with pytest.raises(ValueError):
        evaluate_rpn("a __import__", {"a": Decimal(1)})


def test_condition_tree_and_or_not():
    facts = {"expense.medical.total": 3000, "income.net": 75000, "profile.province": "ON"}
    tree = {
        "logical_op": "AND",
        "conditions": [
            {"fact_key": "expense.medical.total", "operator": "gt", "value_number": 0,
             "value_type": "money", "value_set": None},
            {"fact_key": "income.net", "operator": "exists", "value_type": "number",
             "value_set": None},
        ],
        "groups": [
            {"logical_op": "OR", "conditions": [
                {"fact_key": "profile.province", "operator": "in", "value_type": "set",
                 "value_set": {"ON", "BC"}},
            ], "groups": []},
        ],
    }
    assert eval_group(tree, facts) is True
    facts2 = dict(facts, **{"expense.medical.total": 0})
    assert eval_group(tree, facts2) is False


def test_ai_guardrail_blocks_invented_numbers():
    verified = {"estimated_tax": Decimal("8733.86"), "refund": Decimal("805")}
    # allowed figure passes through
    ok = AiExplanationService._validate("Your estimated tax is about $8734.", verified)
    assert "estimated tax" in ok.lower()
    # a fabricated figure is rejected
    blocked = AiExplanationService._validate("You will save $99999 guaranteed.", verified)
    assert "already computed" in blocked
