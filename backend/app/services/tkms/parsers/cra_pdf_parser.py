"""CRAPdfParser — CRA PDF parsing via OCR/LLM assist (stub behind the port).

`extract_text` would delegate to the existing OcrProvider port; `extract_rules`
would optionally use an LLM to help structure the text. Crucially the AI assists
EXTRACTION only: every rule it proposes is staged as tkms.extracted_rule, run
through validation, diffed, reviewed under four-eyes, versioned, and stored as
structured data before it can ever be published. The deterministic engine and
eligibility logic never consult AI.
"""
from __future__ import annotations

from app.services.tkms.domain.models import ExtractedRuleSet
from app.services.tkms.parsers.base import BaseParser


class CRAPdfParser(BaseParser):
    name = "cra_pdf"
    version = "0.1.0"
    source = "cra"
    fmt = "pdf"
    base_confidence = None  # OCR/LLM-derived confidence is set per run

    def extract_text(self, raw: bytes) -> str:
        raise NotImplementedError(
            "CRAPdfParser is a registered stub; wire OcrProvider for text extraction."
        )

    def extract_rules(self, text: str) -> ExtractedRuleSet:
        raise NotImplementedError(
            "CRAPdfParser is a registered stub; wire the (optionally AI-assisted) "
            "structured extractor. AI output must be validated + reviewed before publish."
        )
