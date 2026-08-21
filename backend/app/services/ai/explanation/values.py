"""Deterministic value-form builders shared by the assembler, the renderer and
the validators.

Every function here produces FORMATTING variants of one already-computed
value — comma grouping, a currency sign, a percent rendering, a spelled-out
date. Nothing performs arithmetic beyond the fixed decimal-shift a percent
rendering is defined as; the renderer therefore cannot become a calculator by
calling anything in this module.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

#: Shared token shapes. The assembler uses them to absorb numbers that appear
#: inside SUPPLIED governed text (citation texts, rule-authored prose, line
#: item labels) into the value table, and the validators use the very same
#: shapes to extract tokens from generated prose — one definition, so the two
#: sides cannot disagree about what counts as a number.
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
SPELLED_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},\s+\d{4}\b"
)
# The trailing guard rejects running into a word or a further decimal — but a
# sentence-ending period after an amount is ordinary prose, so a bare "." is
# allowed. Without the `\.\d` distinction, "$1,483.25." backtracked to "$1".
NUMBER_TOKEN_RE = re.compile(r"(?<![\w.])-?\$?\d[\d,]*(?:\.\d+)?\s?%?(?!\w|\.\d)")
DAYS_RE = re.compile(r"\b(\d{1,4})\s+days?\b", re.IGNORECASE)


def parse_spelled_date(text: str) -> date | None:
    """Parse "April 30, 2026" deterministically, without locale machinery."""
    try:
        month_part, day_part, year_part = text.replace(",", "").split()
        return date(int(year_part), _MONTHS.index(month_part) + 1, int(day_part))
    except (ValueError, IndexError):
        return None


def is_material_token(raw: str) -> bool:
    """Whether a numeric token is one the validators will check: percent,
    currency-marked, decimal-bearing, or magnitude ≥ 100. Small bare integers
    are ordinary prose."""
    token = normalize_token(raw)
    if token.endswith("%") or "$" in raw or "." in token:
        return True
    try:
        return abs(float(token.rstrip("%"))) >= 100
    except ValueError:
        return False


def normalize_token(token: str) -> str:
    """Canonicalize a numeric token for comparison: strip currency signs,
    grouping commas and spaces. The percent sign is kept — a rate rendered as
    a percentage is a different string from its fraction and must match a
    percent form explicitly."""
    return token.replace("$", "").replace(",", "").replace(" ", "").strip()


def money_forms(canonical: str) -> list[str]:
    """Formatting variants of a canonical scale-2 money string."""
    sign = "-" if canonical.startswith("-") else ""
    unsigned = canonical.lstrip("-")
    int_part, _, dec_part = unsigned.partition(".")
    grouped = f"{int(int_part):,}"
    forms = {
        canonical,
        f"{sign}{grouped}.{dec_part}",
        f"${canonical}" if not sign else f"-${unsigned}",
        f"{sign}${grouped}.{dec_part}",
    }
    if dec_part == "00":
        forms |= {
            f"{sign}{int_part}",
            f"{sign}{grouped}",
            f"{sign}${int_part}",
            f"{sign}${grouped}",
        }
    return sorted(forms)


def money_display(canonical: str) -> str:
    """The one display form the deterministic renderer uses."""
    sign = "-" if canonical.startswith("-") else ""
    unsigned = canonical.lstrip("-")
    int_part, _, dec_part = unsigned.partition(".")
    return f"{sign}${int(int_part):,}.{dec_part}"


def rate_forms(value: Decimal) -> list[str]:
    """A fraction and its percent rendering — the same value twice."""
    fraction = format(value.normalize(), "f")
    percent = format((value * 100).normalize(), "f")
    return sorted({fraction, str(value), f"{percent}%"})


def rate_display(value: Decimal) -> str:
    return f"{format((value * 100).normalize(), 'f')}%"


def count_forms(value: Decimal | int) -> list[str]:
    dec = Decimal(value)
    forms = {format(dec.normalize(), "f"), str(value)}
    if dec == dec.to_integral_value():
        forms.add(str(int(dec)))
    return sorted(forms)


def date_forms(value: date) -> list[str]:
    """ISO plus the spelled-out English form."""
    spelled = f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"
    return sorted({value.isoformat(), spelled})


def year_forms(year: int) -> list[str]:
    return [str(year)]
