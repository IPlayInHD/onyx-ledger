"""Government tax-data ingestion: Raw -> Extract -> Validate -> Transform.

Pure, deterministic parsing/validation (no DB, no framework) so it is unit-
testable. The service layer (service.py) loads the transformed rules into the
KB as DRAFT versions and opens a four-eyes change request.

Every ingested rule must carry: source, date, version, jurisdiction, tax_year.
"""
from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.core.exceptions import ValidationError

REQUIRED = ("code", "name", "category", "jurisdiction", "tax_year")
VALID_CATEGORIES = {"credit", "deduction", "benefit", "bracket", "limit", "threshold"}


@dataclass
class IngestRule:
    code: str
    name: str
    category: str
    jurisdiction: str            # 'FED' or province code
    tax_year: int
    description: str = ""
    max_amount: Decimal | None = None
    reduction_rate: Decimal | None = None
    source_url: str | None = None
    effective_date: str | None = None
    warnings: list[str] = field(default_factory=list)


# ---- Extract: raw payload -> list[dict] -------------------------------------
def extract(payload: str, fmt: str) -> list[dict]:
    fmt = fmt.lower()
    if fmt == "json":
        data = json.loads(payload)
        return data if isinstance(data, list) else [data]
    if fmt == "csv":
        return list(csv.DictReader(io.StringIO(payload)))
    if fmt == "xml":
        root = ET.fromstring(payload)
        rows = []
        for rule in root.findall(".//rule"):
            rows.append({child.tag: (child.text or "").strip() for child in rule})
        return rows
    raise ValidationError(f"Unsupported ingestion format '{fmt}'")


# ---- Validate + Transform: dict -> IngestRule -------------------------------
def _to_decimal(v: object) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def validate_and_transform(rows: list[dict]) -> tuple[list[IngestRule], list[str]]:
    rules: list[IngestRule] = []
    errors: list[str] = []
    for i, row in enumerate(rows):
        missing = [k for k in REQUIRED if not str(row.get(k, "")).strip()]
        if missing:
            errors.append(f"row {i}: missing {', '.join(missing)}")
            continue
        category = str(row["category"]).strip().lower()
        if category not in VALID_CATEGORIES:
            errors.append(f"row {i}: invalid category '{category}'")
            continue
        try:
            year = int(str(row["tax_year"]).strip())
        except ValueError:
            errors.append(f"row {i}: tax_year not an integer")
            continue
        if not (1900 <= year <= 2200):
            errors.append(f"row {i}: tax_year {year} out of range")
            continue

        rate = _to_decimal(row.get("reduction_rate"))
        rule = IngestRule(
            code=str(row["code"]).strip().upper(),
            name=str(row["name"]).strip(),
            category=category,
            jurisdiction=str(row["jurisdiction"]).strip().upper(),
            tax_year=year,
            description=str(row.get("description", "")).strip(),
            max_amount=_to_decimal(row.get("max_amount")),
            reduction_rate=rate,
            source_url=(row.get("source_url") or None),
            effective_date=(row.get("effective_date") or f"{year}-01-01"),
        )
        if rate is not None and not (Decimal(0) <= rate <= Decimal(1)):
            rule.warnings.append(f"reduction_rate {rate} outside 0..1")
        rules.append(rule)
    return rules, errors
