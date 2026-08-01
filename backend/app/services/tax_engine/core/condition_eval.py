"""Evaluate a stored boolean condition tree against a fact map.

The tree is a set of groups (AND/OR/NOT) with leaf conditions; groups nest via
parent_group_id. This module is pure — it takes plain dicts, so it is trivially
unit-testable and shared by the DB-backed rules service.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

# A leaf condition, framework-free.
#   {"fact_key","operator","value_type","value_number","value_number_high",
#    "value_text","value_boolean","value_set": set[str] | None}
# A group:
#   {"logical_op": "AND|OR|NOT", "conditions": [...], "groups": [...]}


def _coerce_number(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def eval_condition(cond: dict, facts: dict[str, Any]) -> bool:
    fact_present = cond["fact_key"] in facts and facts[cond["fact_key"]] is not None
    op = cond["operator"]
    if op == "exists":
        return fact_present
    if op == "is_true":
        return bool(facts.get(cond["fact_key"])) is True
    if not fact_present:
        return False

    actual = facts[cond["fact_key"]]
    if op == "eq":
        return str(actual) == str(cond.get("value_text") if cond.get("value_text") is not None else cond.get("value_number"))
    if op == "neq":
        return str(actual) != str(cond.get("value_text") if cond.get("value_text") is not None else cond.get("value_number"))
    if op in ("gt", "gte", "lt", "lte", "between"):
        a = _coerce_number(actual)
        lo = _coerce_number(cond.get("value_number"))
        if a is None or lo is None:
            return False
        if op == "gt":
            return a > lo
        if op == "gte":
            return a >= lo
        if op == "lt":
            return a < lo
        if op == "lte":
            return a <= lo
        hi = _coerce_number(cond.get("value_number_high"))
        return hi is not None and lo <= a <= hi
    if op in ("in", "contains"):
        vs = cond.get("value_set") or set()
        return str(actual) in vs
    return False


def eval_group(group: dict, facts: dict[str, Any]) -> bool:
    results: list[bool] = [eval_condition(c, facts) for c in group.get("conditions", [])]
    results += [eval_group(g, facts) for g in group.get("groups", [])]
    op = group.get("logical_op", "AND")
    if not results:
        return True  # empty group is vacuously true
    if op == "AND":
        return all(results)
    if op == "OR":
        return any(results)
    if op == "NOT":
        return not any(results)
    return all(results)
