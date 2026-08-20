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

WHAT IS GOVERNED TODAY. Bracket tables, and the CPP/EI/CPP2 parameters listed in
`FEDERAL_CONSTANT_FIELDS`. Everything else the engine needs — basic personal
amounts, credit rates, dividend factors — has no published governed
representation yet and continues to come from the in-code bootstrap. The dataset
records exactly which jurisdictions and which constants were governed, so the
distinction is visible in a snapshot rather than assumed.

WHY NOT EVERY GOVERNED CONSTANT. Of the constants authored across the
completed ingestion batches, thirteen name a value the engine already reads —
the CPP/EI/CPP2 parameters, the Schedule 8 base and first-additional split
rates, and the annual FHSA participation room. The rest are inputs to rules,
formulas and expense guidance, or have no runtime consumer at all. Mapping
those into engine fields would invent a field per row to make a count look
complete, and each invented field is a second place a tax figure lives.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.database.models import CalcConstant, Jurisdiction, TaxBracket, TaxBracketSet
from app.services.tax_engine.core import data as bootstrap
from app.services.tax_engine.core.data import Bracket, TaxDataset, bootstrap_dataset

#: The jurisdiction code the federal bracket set is registered under.
FEDERAL_CODE = "FED"

#: The bracket-set kind the engine's marginal-rate ladder is built from.
#: `surtax` is a separate kind the engine models separately and is not a
#: substitute for it.
INCOME_TAX_KIND = "income_tax"

#: Governed constant code -> (FederalData field, the unit the field is in).
#:
#: Every entry names a value `compute()` already reads. The unit is checked
#: rather than trusted: `EI_PREMIUM_RATE` published as CAD would be a premium
#: amount wearing a rate's name, and multiplying insurable earnings by it would
#: produce a confident, enormous, wrong number.
#:
#: Deliberately absent: CPP_MAX_CONTRIBUTORY_EARNINGS and the two
#: MAX_SELF_EMPLOYED_CONTRIBUTION figures, which the engine DERIVES from the
#: ceiling, exemption and rate. Storing them as well would create a second
#: authority for a number already computed, and the two could disagree.
FEDERAL_CONSTANT_FIELDS: dict[str, tuple[str, str]] = {
    "CPP_MAX_PENSIONABLE_EARNINGS": ("cpp_max_pensionable", "CAD"),
    "CPP_BASIC_EXEMPTION": ("cpp_exemption", "CAD"),
    "CPP_CONTRIBUTION_RATE": ("cpp_rate", "ratio"),
    # The Schedule 8 split of the combined employee rate. Employee-side
    # values, like every rate above; the engine derives the self-employed
    # doubles rather than storing them again.
    "CPP_BASE_CONTRIBUTION_RATE_EMPLOYEE": ("cpp_base_rate", "ratio"),
    "CPP_FIRST_ADDITIONAL_CONTRIBUTION_RATE_EMPLOYEE":
        ("cpp_first_additional_rate", "ratio"),
    # Annual FHSA participation room — the default FHSA_ROOM capacity for a
    # run whose user declared none.
    "FHSA_ANNUAL_PARTICIPATION_ROOM": ("fhsa_annual", "CAD"),
    "CPP_MAX_EMPLOYEE_CONTRIBUTION": ("cpp_max", "CAD"),
    "CPP2_MAX_EMPLOYEE_CONTRIBUTION": ("cpp2_max", "CAD"),
    "CPP2_ADDITIONAL_MAX_PENSIONABLE_EARNINGS": ("cpp2_max_pensionable", "CAD"),
    "CPP2_CONTRIBUTION_RATE": ("cpp2_rate", "ratio"),
    "EI_MAX_INSURABLE_EARNINGS": ("ei_max_insurable", "CAD"),
    "EI_PREMIUM_RATE": ("ei_rate", "ratio"),
    "EI_MAX_EMPLOYEE_PREMIUM": ("ei_max", "CAD"),
}


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

    async def resolve(self, tax_year: int,
                      as_of: date | None = None) -> TaxDataset:
        """Resolve the whole dataset for a tax year in a bounded number of trips.

        `as_of` selects among constants that carry an intra-year effective
        period. It is genuinely optional because every constant the engine
        reads today is annual — CPP and EI ceilings are set for a year by
        statute — and requiring a date to resolve a year's brackets would be
        ceremony. When it is absent, periodised rows are not eligible at all
        rather than being guessed at; see `_constant_overlay`.
        """
        # TWO statements, one per family, and both flat in the size of the
        # year's data. Resolution sits on the optimization run's hot path and
        # inside snapshot capture, so each round trip here is a round trip on
        # every run — the kind of per-call cost that only shows up once a
        # statement budget is measured.
        rows = (await self.s.execute(
            select(TaxBracketSet, Jurisdiction, TaxBracket)
            .join(Jurisdiction, Jurisdiction.id == TaxBracketSet.jurisdiction_id)
            .outerjoin(TaxBracket, TaxBracket.bracket_set_id == TaxBracketSet.id)
            .where(TaxBracketSet.tax_year == tax_year,
                   TaxBracketSet.kind == INCOME_TAX_KIND))).all()
        federal, governed_constants = await self._constant_overlay(tax_year, as_of)
        if not rows:
            # No governed brackets, but constants may still have been applied.
            base = bootstrap_dataset(tax_year)
            if not governed_constants:
                return base
            return replace(base, federal=federal,
                           governed_constants=frozenset(governed_constants))

        sets: dict[uuid.UUID, tuple[TaxBracketSet, Jurisdiction]] = {}
        by_set: dict[uuid.UUID, list[TaxBracket]] = {}
        for bracket_set, jurisdiction, bracket in rows:
            sets.setdefault(bracket_set.id, (bracket_set, jurisdiction))
            if bracket is not None:
                by_set.setdefault(bracket_set.id, []).append(bracket)

        provinces = dict(bootstrap.PROVINCES_2025)
        governed: set[str] = set()

        for bset, jurisdiction in sets.values():
            code = jurisdiction.code
            rows_for_set = by_set.get(bset.id, [])
            if not rows_for_set:
                # A bracket set with no brackets states nothing about tax law and
                # cannot come from publication: validation requires a terminal
                # bracket, and the rows are written in the same transaction as
                # the set. Such a row exists only as a provenance anchor or as
                # residue, so it is not governed data — and treating it as a
                # broken table instead would let one stray row disable an entire
                # tax year for everybody.
                #
                # Not a silent fallback: the jurisdiction is simply absent from
                # `governed_jurisdictions`, which the sealed artifact records, so
                # "this year was not governed" stays visible. A set that DOES
                # carry brackets which do not form a valid ladder still raises.
                continue
            ladder = _to_engine_brackets(
                rows_for_set, f"{code} {tax_year} {bset.kind}")
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
            governed_constants=frozenset(governed_constants),
        )

    async def _constant_overlay(
        self, tax_year: int, as_of: date | None,
    ) -> tuple[bootstrap.FederalData, set[str]]:
        """Overlay governed CALC_CONSTANTs onto the federal constants.

        Returns the federal data to use and the codes that actually came from
        governed rows, so the dataset can record which figures stopped being
        in-code. A code with no published row keeps its bootstrap value and is
        simply absent from that set — the same visible, pre-publication state
        the bracket path already reports, not a live-data fallback.

        A row that exists but cannot be used raises. A governed constant whose
        unit disagrees with the field it feeds is not a value the engine may
        quietly ignore and it is not one the engine may use.
        """
        # Periodised rows are eligible only when a date was supplied, and only
        # when they contain it. Without a date there is no basis on which to
        # prefer one quarter over another, and picking one anyway is how a
        # replay silently acquires a number nobody chose.
        annual = CalcConstant.effective_from.is_(None) & CalcConstant.effective_to.is_(None)
        eligible: ColumnElement[bool] = annual
        if as_of is not None:
            eligible = or_(annual, (CalcConstant.effective_from <= as_of)
                           & (CalcConstant.effective_to >= as_of))
        rows = list(await self.s.scalars(
            select(CalcConstant).where(
                CalcConstant.tax_year == tax_year,
                CalcConstant.code.in_(FEDERAL_CONSTANT_FIELDS),
                eligible)))

        applied: set[str] = set()
        seen: dict[str, CalcConstant] = {}
        changes: dict[str, Any] = {}
        for row in rows:
            field, expected_unit = FEDERAL_CONSTANT_FIELDS[row.code]
            if row.code in seen:
                # The database's exclusion constraint makes this unreachable
                # for overlapping periods; reaching it means the constraint is
                # missing or the predicate above is wrong, and either way the
                # engine must not pick one row arbitrarily.
                raise ReferenceDataError(
                    f"{row.code} {tax_year}: two governed rows are eligible "
                    f"for {as_of or 'the whole year'}; resolution is ambiguous")
            if row.unit != expected_unit:
                raise ReferenceDataError(
                    f"{row.code} {tax_year}: published in {row.unit!r} but "
                    f"{field} is {expected_unit!r}; the value does not mean "
                    "what the field would read it as")
            if row.value is None:
                raise ReferenceDataError(f"{row.code} {tax_year}: no value")
            seen[row.code] = row
            changes[field] = Decimal(row.value)
            applied.add(row.code)
        # One `replace` for the whole overlay rather than one per row: a frozen
        # dataclass rebuilt ten times would allocate nine datasets nobody reads.
        federal = replace(bootstrap.FEDERAL_2025, **changes)
        return federal, applied
