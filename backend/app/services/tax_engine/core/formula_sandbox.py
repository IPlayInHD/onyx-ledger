"""Deterministic, sandboxed RPN evaluator for stored calc_formula expressions.

Only a whitelisted set of operators is supported — no arbitrary code, no
attribute access, no imports. Inputs are bound by name from a fact map.
Example: 'medical net 0.03 * 2834 min - 0 max 0.145 *'
"""
from __future__ import annotations

from decimal import Decimal

_BINARY = {"+", "-", "*", "/", "min", "max"}


def evaluate_rpn(expression: str, variables: dict[str, Decimal]) -> Decimal:
    stack: list[Decimal] = []
    for token in expression.split():
        if token in _BINARY:
            if len(stack) < 2:
                raise ValueError(f"stack underflow at '{token}'")
            b = stack.pop()
            a = stack.pop()
            stack.append(_apply(token, a, b))
        elif token in variables:
            stack.append(Decimal(variables[token]))
        else:
            try:
                stack.append(Decimal(token))
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"unknown token '{token}'") from e
    if len(stack) != 1:
        raise ValueError("invalid RPN expression (stack != 1)")
    return stack[0]


def _apply(op: str, a: Decimal, b: Decimal) -> Decimal:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a / b if b != 0 else Decimal(0)
    if op == "min":
        return min(a, b)
    if op == "max":
        return max(a, b)
    raise ValueError(f"unsupported operator '{op}'")
