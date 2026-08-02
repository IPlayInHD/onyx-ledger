"""CRAHtmlParser — CRA web-page parsing (stub behind the port).

The framework and contract are ready; a real implementation would fetch/clean
the CRA HTML and extract rules (optionally AI-ASSISTED for extraction only — its
output is still staged, validated, reviewed, versioned, and stored as structured
data before it can be published; AI never determines a calculation or eligibility).
"""
from __future__ import annotations

from app.services.tkms.domain.models import ExtractedRuleSet
from app.services.tkms.parsers.base import BaseParser


class CRAHtmlParser(BaseParser):
    name = "cra_html"
    version = "0.1.0"
    source = "cra"
    fmt = "html"

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        raise NotImplementedError(
            "CRAHtmlParser is a registered stub; wire the real (optionally "
            "AI-assisted) HTML extractor before enabling HTML imports."
        )
