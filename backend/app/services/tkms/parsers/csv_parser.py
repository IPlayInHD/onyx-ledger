"""GovernmentCsvParser — flat CSV of tax rules (one rule per row).

CSV is a flat format, so nested formula/eligibility structures are not expressed
here; those rules carry scalar fields only (a richer format — JSON/Manual —
carries the condition tree). Deterministic: same bytes → same ruleset.
"""
from __future__ import annotations

import csv
import io

from app.services.tkms.domain.models import ExtractedRuleSet
from app.services.tkms.parsers.base import BaseParser


class GovernmentCsvParser(BaseParser):
    name = "government_csv"
    version = "1.0.0"
    source = "generic"
    fmt = "csv"

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        rows = list(csv.DictReader(io.StringIO(text)))
        return self._build_ruleset(rows)
