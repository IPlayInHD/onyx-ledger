"""Department of Finance / provincial parsers (stubs behind the port).

Jurisdiction-specific sources (federal Finance releases, provincial budget
tables) get their own parsers. Registered so the port is discoverable and a
re-parse can target them; real implementations plug in without touching the
pipeline.
"""
from __future__ import annotations

from app.services.tkms.domain.models import ExtractedRuleSet
from app.services.tkms.parsers.base import BaseParser


class DepartmentFinanceParser(BaseParser):
    name = "finance_html"
    version = "0.1.0"
    source = "finance"
    fmt = "html"

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        raise NotImplementedError(
            "DepartmentFinanceParser is a registered stub; wire the real extractor."
        )


class ProvincialParser(BaseParser):
    name = "provincial_html"
    version = "0.1.0"
    source = "provincial"
    fmt = "html"

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        raise NotImplementedError(
            "ProvincialParser is a registered stub; wire the real extractor."
        )
