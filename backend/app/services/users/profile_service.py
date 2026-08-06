"""Tax-profile mutation service — the authoritative write path (Entry 9).

The profile upsert used to live in the route handler, which made the route the
only place a freshness event could be emitted from. That is the wrong home for
it: a profile can also be written by an import, an administrative correction or
a future worker, and each of those would silently skip invalidation. The
mutation and its event belong together, in a service, inside one transaction.

**Not every profile change is a tax change.** `display_name`, `locale` and
`timezone` are presentation; staling a user's sealed optimizations because they
switched to French would be wrong and would train people to ignore the label.
`TAX_RELEVANT_FIELDS` is the governed list, and it is derived from what the
frozen snapshot actually consumes — province and marital status reach the engine
directly, and the rest gate rule eligibility.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TaxProfile
from app.services.ioe.freshness_producers import on_profile_changed

PROFILE_SERVICE_VERSION = "1.0.0"

# Fields whose value can change a tax calculation or a rule eligibility
# determination. A field NOT in this set is presentation or preference and must
# never stale a sealed result.
#
# `province_code` and `marital_status` are consumed directly by
# `TaxInput`; the remainder appear in rule eligibility conditions. Adding a
# tax-consuming column without adding it here is the mistake this constant
# exists to make visible, which is why a test asserts every engine-consumed
# profile field is listed.
TAX_RELEVANT_FIELDS: frozenset[str] = frozenset({
    "date_of_birth",          # age-derived credits and thresholds
    "province_code",          # every provincial bracket and credit
    "residency_status",       # residency gates most of the Act
    "marital_status",         # spousal amounts, transfers, income tests
    "is_student",             # tuition, education-related provisions
    "has_disability",         # disability amount and related credits
    "first_time_home_buyer",  # HBP / FHSA eligibility
    "employment_type",        # employment vs self-employment treatment
    "is_self_employed",       # business deductions, CPP treatment
    "housing_status",         # home-related provisions
    "owns_home",              # home-related provisions
})

# Deliberately excluded, recorded so the exclusion is a decision rather than an
# oversight: display_name, locale, timezone, industry, employer_name. The last
# two are descriptive metadata — no rule or engine field reads them today.
PRESENTATION_FIELDS: frozenset[str] = frozenset({
    "display_name", "locale", "timezone", "industry", "employer_name",
})


class ProfileService:
    """Owns tax-profile writes and the freshness event they imply."""

    def __init__(self, session: AsyncSession):
        self.s = session

    async def upsert_tax_profile(
        self, user_id: uuid.UUID, values: dict[str, Any]
    ) -> tuple[TaxProfile, bool]:
        """Apply the update and emit iff a TAX-RELEVANT field actually moved.

        Returns the row and whether a freshness event was emitted.

        "Actually moved" is compared against the stored value, not merely
        submitted: a client that PUTs the whole profile back unchanged must not
        invalidate anything. The change token is the sorted set of fields that
        differed, so re-submitting the same edit collides on the dedupe key
        while a different edit invalidates again.
        """
        profile = await self.s.get(TaxProfile, user_id)
        if profile is None:
            profile = TaxProfile(user_id=user_id)
            self.s.add(profile)
            await self.s.flush()

        changed: list[str] = []
        for field, value in values.items():
            if not hasattr(profile, field):
                continue
            if getattr(profile, field) != value:
                changed.append(field)
            setattr(profile, field, value)
        await self.s.flush()

        tax_relevant = sorted(set(changed) & TAX_RELEVANT_FIELDS)
        if not tax_relevant:
            return profile, False

        # Field NAMES only — never a value. A province code is arguably benign;
        # an employer name or a date of birth is not, and a payload that carries
        # "whatever changed" cannot stay safe as columns are added.
        emitted = await on_profile_changed(
            self.s, user_id, change_token=",".join(tax_relevant))
        return profile, emitted


__all__ = [
    "PRESENTATION_FIELDS",
    "PROFILE_SERVICE_VERSION",
    "TAX_RELEVANT_FIELDS",
    "ProfileService",
]
