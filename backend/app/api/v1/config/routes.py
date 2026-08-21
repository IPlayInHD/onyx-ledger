"""What Onyx currently offers, stated by the backend.

THE FRONTEND MUST NOT KEEP ITS OWN LIST. It did: a hard-coded array of
provinces and another of tax years, maintained by hand alongside the engine's
actual capability. The two drifted immediately — the frontend offered Quebec,
which the engine cannot answer for, and a Quebec customer would have been shown
a confident figure computed with the wrong payroll contributions.

This endpoint is deliberately ANONYMOUS. Onboarding needs the list before an
account exists, and there is nothing sensitive in it: which provinces a tax
product supports is on its marketing page.

It is NOT a second tax registry. The launch scope in settings can only narrow
what the engine computes, and `tests/integration/test_launch_scope_contract.py`
asserts every pair it advertises actually resolves in the engine's dataset. A
launch list that claimed a jurisdiction the engine cannot price would fail the
build, not reach a customer.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(prefix="/config", tags=["config"])

#: Display names for the provinces this product offers. Presentation only — the
#: CODES are the contract, and an unknown code falls back to itself rather than
#: being dropped, because silently omitting a supported province would be worse
#: than showing a bare code.
PROVINCE_NAMES = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}


@router.get("/launch-scope")
async def launch_scope() -> dict:
    """The tax years and provinces a customer may currently choose."""
    settings = get_settings()
    return {
        "tax_years": list(settings.launch_tax_years),
        "provinces": [
            {"code": code, "name": PROVINCE_NAMES.get(code, code)}
            for code in settings.launch_provinces
        ],
    }
