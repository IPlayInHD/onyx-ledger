"""GovernmentJsonParser — government JSON, one rule per object.

JSON carries the full structure: nested `formula` object and an
`eligibility_conditions` list are normalized into the ExtractedRule contract.
Accepts either a top-level list or a single object.
"""
from __future__ import annotations

import json

from app.core.exceptions import ValidationError
from app.services.tkms.domain.models import ExtractedRuleSet
from app.services.tkms.parsers.base import BaseParser


class GovernmentJsonParser(BaseParser):
    name = "government_json"
    version = "1.0.0"
    source = "generic"
    fmt = "json"

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValidationError(f"Invalid JSON: {e}") from e
        if isinstance(data, dict) and "rules" in data:
            data = data["rules"]
        rows = data if isinstance(data, list) else [data]
        rows = [r for r in rows if isinstance(r, dict)]
        return self._build_ruleset(rows)
