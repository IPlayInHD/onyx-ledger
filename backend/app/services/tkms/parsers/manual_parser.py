"""ManualRuleParser — admin hand-entry of the canonical contract.

The escape hatch: an admin submits the exact ExtractedRule payload shape (the
same JSON that `ExtractedRule.as_payload()` produces), so a rule that no
automated parser handles can still enter the governed pipeline — and still goes
through validation, comparison, four-eyes review, and publication like any other.
Highest confidence (a human authored it), but never bypasses governance.
"""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

from app.core.exceptions import ValidationError
from app.services.tkms.domain.models import ExtractedRule, ExtractedRuleSet
from app.services.tkms.parsers.base import BaseParser, normalize_rule


class ManualRuleParser(BaseParser):
    name = "manual"
    version = "1.0.0"
    source = "manual"
    fmt = "manual"
    base_confidence = Decimal("1.0")   # a human authored it

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValidationError(f"Invalid manual rule JSON: {e}") from e
        rows = data if isinstance(data, list) else [data]

        rules: list[ExtractedRule] = []
        warnings: list[str] = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                warnings.append(f"entry {i} is not an object — skipped")
                continue
            # accept either the canonical payload or the loose row shape
            rule, w = normalize_rule(row)
            warnings.extend(f"entry {i}: {msg}" for msg in w)
            if rule is None:
                continue
            if rule.confidence is None:
                rule = replace(rule, confidence=self.base_confidence)
            rules.append(rule)

        return ExtractedRuleSet(
            parser_name=self.name,
            parser_version=self.version,
            source=self.source,
            rules=tuple(rules),
            confidence=self.base_confidence if rules else Decimal("0"),
            warnings=tuple(warnings),
        )
