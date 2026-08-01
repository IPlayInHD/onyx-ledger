"""Slip-aware field extraction (deterministic; no AI).

Two input paths mirror the validated prototype:
  1. STRUCTURED  {type, fields}  — highest confidence (0.99).
  2. TEXT        {type, text}     — OCR / PDF text scanned with slip-aware regex.

Real image OCR (photo/scan -> text) sits behind the OcrProvider port; this
module turns text into normalized fields once text exists.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# field -> list of regex patterns (first capture group = amount)
SLIP_MAP: dict[str, dict] = {
    "T4": {
        "fields": {
            "employmentIncome": [r"box\s*14[^0-9]{0,12}([\d,]+\.?\d*)", r"employment income[^0-9]{0,12}([\d,]+\.?\d*)"],
            "cppContrib": [r"box\s*16[^0-9]{0,12}([\d,]+\.?\d*)"],
            "eiContrib": [r"box\s*18[^0-9]{0,12}([\d,]+\.?\d*)"],
            "taxWithheld": [r"box\s*22[^0-9]{0,12}([\d,]+\.?\d*)"],
        },
        "expected": ["employmentIncome"],
    },
    "T5": {
        "fields": {
            "eligibleDividends": [r"box\s*24[^0-9]{0,12}([\d,]+\.?\d*)"],
            "interestIncome": [r"box\s*13[^0-9]{0,12}([\d,]+\.?\d*)", r"interest[^0-9]{0,12}([\d,]+\.?\d*)"],
        },
        "expected": ["interestIncome"],
    },
    "T2202": {"fields": {"tuition": [r"tuition[^0-9]{0,12}([\d,]+\.?\d*)"]}, "expected": ["tuition"]},
    "T2125": {
        "fields": {
            "selfEmploymentIncome": [r"gross[^0-9]{0,16}([\d,]+\.?\d*)"],
            "selfEmploymentExpenses": [r"expenses[^0-9]{0,12}([\d,]+\.?\d*)"],
        },
        "expected": ["selfEmploymentIncome"],
    },
    "T776": {
        "fields": {
            "rentalIncome": [r"gross rents?[^0-9]{0,12}([\d,]+\.?\d*)"],
            "rentalExpenses": [r"expenses[^0-9]{0,12}([\d,]+\.?\d*)"],
        },
        "expected": ["rentalIncome"],
    },
    "MEDICAL": {"fields": {"medicalExpenses": [r"(?:total|amount)[^0-9]{0,12}([\d,]+\.?\d*)"]}, "expected": ["medicalExpenses"]},
    "DONATION": {"fields": {"donations": [r"(?:total|amount|donation)[^0-9]{0,12}([\d,]+\.?\d*)"]}, "expected": ["donations"]},
}

# extracted field -> how it lands in the financial profile on confirmation
FIELD_TARGET: dict[str, tuple[str, str]] = {
    "employmentIncome": ("income", "employment"),
    "selfEmploymentIncome": ("income", "self_employment"),
    "rentalIncome": ("income", "rental"),
    "interestIncome": ("income", "interest"),
    "eligibleDividends": ("income", "eligible_dividends"),
    "capitalGains": ("income", "capital_gains"),
    "pensionIncome": ("income", "pension"),
    "medicalExpenses": ("expense", "medical"),
    "tuition": ("expense", "tuition"),
    "donations": ("expense", "donation"),
    "childCare": ("expense", "childcare"),
}

# extracted field -> fact_key (for provenance on extraction_field)
FIELD_FACT = {
    "employmentIncome": "income.employment",
    "selfEmploymentIncome": "income.self_employment.net",
    "medicalExpenses": "expense.medical.total",
    "tuition": "expense.tuition.total",
    "donations": "expense.donation.total",
}


def _num(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", "").replace("$", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def extract_fields(doc_type: str, *, text: str | None = None,
                   fields: dict | None = None) -> tuple[dict[str, Decimal], float]:
    """Return (normalized fields, confidence 0..1)."""
    spec = SLIP_MAP.get((doc_type or "").upper())
    if spec is None:
        return {}, 0.0

    if fields:  # structured path
        out = {k: v for k in fields if (v := _num(str(fields[k]))) is not None}
        return out, 0.99

    out2: dict[str, Decimal] = {}
    body = text or ""
    for field, patterns in spec["fields"].items():
        for pat in patterns:
            m = re.search(pat, body, re.IGNORECASE)
            if m and (val := _num(m.group(1))) is not None:
                out2[field] = val
                break
    hit = sum(1 for e in spec["expected"] if e in out2)
    confidence = round(0.55 + 0.44 * (hit / len(spec["expected"])), 2) if spec["expected"] else (0.8 if out2 else 0.0)
    return out2, (confidence if out2 else 0.0)
