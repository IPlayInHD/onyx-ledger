"""Governed reference data, resolved once and handed to the pure engine.

The engine takes its tax constants as DATA and never fetches — that is what
makes it deterministic and replayable, and this module does not change it. What
it adds is the boundary the engine's own docstring has always named: a
`TaxDataProvider` that reads brackets from `tax_kb` instead of leaving them as
constants nothing can govern.

Resolution happens where a session exists, produces an immutable `TaxDataset`,
and that dataset is then carried into `compute()`. Nothing inside the engine
acquires a session, and no calculation reaches for a row.

WHY A DATASET RATHER THAN A LOOKUP. One run must use ONE dataset. The baseline
runs through `TaxEngineService`, but candidate costs, scenarios and
counterfactuals go through the pure `compute()` on synchronous paths that hold
no session. If the service read governed rows and those paths kept reading
constants, a run's baseline and its candidates would be computed from different
tax law and every delta between them would be meaningless. Resolving once and
passing the result is what makes that impossible rather than merely unlikely.

WHAT IS GOVERNED TODAY. Bracket tables, and only bracket tables. Everything else
the engine needs — basic personal amounts, credit rates, CPP/EI parameters —
has no published governed representation yet, so it continues to come from the
in-code bootstrap. The dataset records exactly which jurisdictions were governed
so that the distinction is visible in a snapshot rather than assumed.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Jurisdiction, TaxBracket, TaxBracketSet
from app.services.tax_engine.core import data as bootstrap
from app.services.tax_engine.core.data import Bracket, TaxDataset, bootstrap_dataset

#: The jurisdiction code the federal bracket set is registered under.
FEDERAL_CODE = "FED"

#: The bracket-set kind the engine's marginal-rate ladder is built from.
#: `surtax` is a separate kind the engine models separately and is not a
#: substitute for it.
INCOME_TAX_KIND = "income_tax"


class ReferenceDataError(Exception):
    """Governed reference data exists but cannot be used as it stands."""


def _to_engine_brackets(rows: list[TaxBracket], ref: str) -> list[Bracket]:
    """Turn governed rows into the ladder the engine already consumes.

    The engine's `Bracket` carries only an upper bound, because the ladder is
    walked in order and each band starts where the previous one ended. Governed
    rows carry both bounds, so the conversion is checked rather than assumed: a
    table whose bands do not meet exactly would otherwise become a ladder that
    silently taxes a gap at the wrong rate.
    """
    ordered = sorted(rows, key=lambda r: r.ordinal)
    if not ordered:
        raise ReferenceDataError(f"{ref}: bracket set has no brackets")
    if ordered[0].lower_bound != Decimal(0):
        raise ReferenceDataError(
            f"{ref}: first band starts at {ordered[0].lower_bound}, not 0")
    for lower, upper in zip(ordered, ordered[1:], strict=False):
        if lower.upper_bound is None:
            raise ReferenceDataError(
                f"{ref}: band {lower.ordinal} is open-ended but is not last")
        if lower.upper_bound != upper.lower_bound:
            raise ReferenceDataError(
                f"{ref}: band {lower.ordinal} ends at {lower.upper_bound} but "
                f"band {upper.ordinal} starts at {upper.lower_bound}")
    if ordered[-1].upper_bound is not None:
        raise ReferenceDataError(
            f"{ref}: top band is bounded at {ordered[-1].upper_bound}, so the "
            "highest incomes fall outside every band")
    return [Bracket(up_to=r.upper_bound, rate=r.rate) for r in ordered]


class TaxDataProvider:
    """Resolves the dataset for a tax year from governed reference data.

    Falls back to the in-code bootstrap ONLY where no governed bracket set has
    been published for a jurisdiction, and says so in the dataset it returns.
    That is not a live-data fallback: it is the current, pre-publication state
    of the registry, made visible instead of implicit. A governed set that
    exists but is malformed raises — a broken table is never quietly replaced
    by constants.
    """

    def __init__(self, session: AsyncSession):
        self.s = session

    async def resolve(self, tax_year: int) -> TaxDataset:
        # ONE statement. Resolution sits on the optimization run's hot path and
        # inside snapshot capture, so a second round trip here is a second round
        # trip on every run — the kind of per-call cost that only shows up once
        # a statement budget is measured.
        rows = (await self.s.execute(
            select(TaxBracketSet, Jurisdiction, TaxBracket)
            .join(Jurisdiction, Jurisdiction.id == TaxBracketSet.jurisdiction_id)
            .outerjoin(TaxBracket, TaxBracket.bracket_set_id == TaxBracketSet.id)
            .where(TaxBracketSet.tax_year == tax_year,
                   TaxBracketSet.kind == INCOME_TAX_KIND))).all()
        if not rows:
            return bootstrap_dataset(tax_year)

        sets: dict[uuid.UUID, tuple[TaxBracketSet, Jurisdiction]] = {}
        by_set: dict[uuid.UUID, list[TaxBracket]] = {}
        for bracket_set, jurisdiction, bracket in rows:
            sets.setdefault(bracket_set.id, (bracket_set, jurisdiction))
            if bracket is not None:
                by_set.setdefault(bracket_set.id, []).append(bracket)

        federal = bootstrap.FEDERAL_2025
        provinces = dict(bootstrap.PROVINCES_2025)
        governed: set[str] = set()

        for bset, jurisdiction in sets.values():
            code = jurisdiction.code
            ladder = _to_engine_brackets(
                by_set.get(bset.id, []), f"{code} {tax_year} {bset.kind}")
            if code == FEDERAL_CODE:
                federal = replace(federal, brackets=ladder)
                governed.add(code)
            elif code in provinces:
                provinces[code] = replace(provinces[code], brackets=ladder)
                governed.add(code)
            # A governed set for a province the engine has no other constants
            # for (no BPA, no credit rate) is NOT silently adopted: half a
            # province's tax law is worse than none, because it computes.

        return TaxDataset(
            tax_year=tax_year,
            federal=federal,
            provinces=provinces,
            governed_jurisdictions=frozenset(governed),
        )
