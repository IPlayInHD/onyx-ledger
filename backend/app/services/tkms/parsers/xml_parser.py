"""GovernmentXmlParser — government XML (<rules><rule>…</rule></rules>).

Security: XML external entities are disabled at parse time (no network/file
access from a document); nested formula/conditions are read from child elements
when present. Flat scalar rules otherwise, like CSV.
"""
from __future__ import annotations

from defusedxml.ElementTree import fromstring as safe_fromstring

from app.core.exceptions import ValidationError
from app.services.tkms.domain.models import ExtractedRuleSet
from app.services.tkms.parsers.base import BaseParser


class GovernmentXmlParser(BaseParser):
    name = "government_xml"
    version = "1.0.0"
    source = "generic"
    fmt = "xml"

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        try:
            root = safe_fromstring(text)
        except Exception as e:  # defusedxml raises on entity attacks / malformed
            raise ValidationError(f"Invalid or unsafe XML: {e}") from e

        rows: list[dict] = []
        for rule_el in root.findall(".//rule"):
            row: dict = {}
            conditions: list[dict] = []
            formula: dict | None = None
            for child in rule_el:
                if child.tag == "eligibility_conditions":
                    for cond in child.findall("condition"):
                        conditions.append({c.tag: (c.text or "").strip() for c in cond})
                elif child.tag == "formula":
                    formula = {c.tag: (c.text or "").strip() for c in child}
                else:
                    row[child.tag] = (child.text or "").strip()
            if conditions:
                row["eligibility_conditions"] = conditions
            if formula:
                row["formula"] = formula
            rows.append(row)
        return self._build_ruleset(rows)
