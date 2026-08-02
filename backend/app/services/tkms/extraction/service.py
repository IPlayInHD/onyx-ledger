"""ExtractionService — promote staged extracted rules into DRAFT rule versions.

Promotion is a single transaction per rule: a `tax_kb.tax_rule_version` (status
`draft`, so inert to the engine) plus its `rules.*` condition tree and formula,
with full provenance back to the import job / parser. The immutable
`tkms.extracted_rule` payload remains the canonical record; the promoted rows are
the engine-consumable projection. Validation (next stage) guards the gap — e.g.
conditions/inputs that reference unknown facts are left out of the projection and
flagged there, so an incomplete draft can never publish.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, ValidationError
from app.database.models import (
    CalcFormula,
    CalcFormulaInput,
    FactDefinition,
    ImportJob,
    Jurisdiction,
    LegislationReference,
    RuleCondition,
    RuleConditionGroup,
    TaxRule,
    TaxRuleVersion,
    TaxYear,
)
from app.database.models import (
    ExtractedRule as ExtractedRuleRow,
)
from app.services.tkms.domain.models import EligibilityCondition, ExtractedRule, FormulaSpec


class ExtractionService:
    def __init__(self, session: AsyncSession):
        self.s = session
        self._fact_cache: set[str] | None = None

    async def promote(self, job_id: uuid.UUID) -> list[TaxRuleVersion]:
        """Promote every staged rule for a job into a draft version (idempotent)."""
        job = await self.s.get(ImportJob, job_id)
        if job is None:
            raise NotFound("Import job not found")

        rows = list(
            await self.s.scalars(
                select(ExtractedRuleRow)
                .where(ExtractedRuleRow.import_job_id == job_id)
                .order_by(ExtractedRuleRow.ordinal)
            )
        )
        if not rows:
            raise ValidationError("Nothing to promote: no extracted rules for this job")

        versions: list[TaxRuleVersion] = []
        for row in rows:
            if row.promoted_version_id is not None:
                existing = await self.s.get(TaxRuleVersion, row.promoted_version_id)
                if existing is not None:
                    versions.append(existing)
                    continue
            rule = ExtractedRule.from_payload(row.payload)
            version = await self._promote_one(rule, job, row.confidence)
            row.promoted_version_id = version.id
            versions.append(version)
        await self.s.flush()
        return versions

    # ---- one rule -> draft version + rules.* --------------------------------
    async def _promote_one(
        self, rule: ExtractedRule, job: ImportJob, confidence: Decimal | None
    ) -> TaxRuleVersion:
        jurisdiction = await self.s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == rule.jurisdiction)
        )
        if jurisdiction is None:
            raise ValidationError(f"Unknown jurisdiction '{rule.jurisdiction}' for {rule.rule_code}")

        year = await self.s.get(TaxYear, rule.tax_year)
        if year is None:
            raise ValidationError(
                f"tax_year {rule.tax_year} is not a known reference year "
                f"(seed ref.tax_year before importing)"
            )

        tax_rule = await self.s.scalar(select(TaxRule).where(TaxRule.code == rule.rule_code))
        if tax_rule is None:
            tax_rule = TaxRule(
                code=rule.rule_code,
                name=rule.name,
                category=rule.category,
                subcategory=rule.subcategory,
                jurisdiction_id=jurisdiction.id,
                province_code=None if rule.jurisdiction == "FED" else (rule.province or rule.jurisdiction),
            )
            self.s.add(tax_rule)
            await self.s.flush()

        leg_ref_id = await self._resolve_legislation_ref(rule)

        version = TaxRuleVersion(
            tax_rule_id=tax_rule.id,
            tax_year=rule.tax_year,
            effective_date=_as_date(rule.effective_date_or_default()),
            expiry_date=_as_date(rule.expiry_date) if rule.expiry_date else None,
            status="draft",
            description=rule.description or rule.name,
            max_amount=rule.max_amount,
            min_amount=rule.min_amount,
            income_threshold_low=rule.income_threshold_low,
            income_threshold_high=rule.income_threshold_high,
            reduction_rate=rule.reduction_rate,
            source_url=rule.source_url,
            legislation_reference_id=leg_ref_id,
            # provenance
            import_job_id=job.id,
            parser_version=job.parser_version,
            parser_confidence=confidence,
        )
        self.s.add(version)
        await self.s.flush()

        if rule.formula is not None:
            version.formula_id = await self._stage_formula(rule.formula)
        if rule.eligibility_conditions:
            await self._stage_conditions(version.id, rule.eligibility_conditions)
        await self.s.flush()
        return version

    async def _resolve_legislation_ref(self, rule: ExtractedRule) -> uuid.UUID | None:
        citation = rule.legislation_reference
        if not citation:
            return None
        existing = await self.s.scalar(
            select(LegislationReference).where(LegislationReference.citation == citation)
        )
        if existing is not None:
            return existing.id
        ref = LegislationReference(citation=citation, url=rule.source_url)
        self.s.add(ref)
        await self.s.flush()
        return ref.id

    async def _stage_formula(self, formula: FormulaSpec) -> uuid.UUID:
        code = await self._unique_formula_code(formula.code)
        cf = CalcFormula(
            code=code,
            expression=formula.expression,
            expression_lang=formula.expression_lang,
            output_unit=formula.output_unit,
        )
        self.s.add(cf)
        await self.s.flush()
        for param_name, fact_key in formula.inputs:
            if await self._fact_exists(fact_key):
                self.s.add(CalcFormulaInput(
                    formula_id=cf.id, param_name=param_name, fact_key=fact_key
                ))
        await self.s.flush()
        return cf.id

    async def _stage_conditions(
        self, version_id: uuid.UUID, conditions: tuple[EligibilityCondition, ...]
    ) -> None:
        root = RuleConditionGroup(rule_version_id=version_id, logical_op="AND", sort_order=0)
        self.s.add(root)
        await self.s.flush()
        order = 0
        for cond in conditions:
            if not await self._fact_exists(cond.fact_key):
                # unknown fact — omitted from the engine projection; validation flags it
                continue
            self.s.add(RuleCondition(
                group_id=root.id,
                fact_key=cond.fact_key,
                operator=cond.operator,
                value_type=cond.value_type,
                value_number=cond.value_number,
                value_number_high=cond.value_number_high,
                value_text=cond.value_text,
                value_boolean=cond.value_boolean,
                value_date=_as_date(cond.value_date) if cond.value_date else None,
                sort_order=order,
            ))
            order += 1
        await self.s.flush()

    # ---- helpers -------------------------------------------------------------
    async def _fact_exists(self, fact_key: str) -> bool:
        if self._fact_cache is None:
            keys = await self.s.scalars(select(FactDefinition.fact_key))
            self._fact_cache = set(keys)
        return fact_key in self._fact_cache

    async def _unique_formula_code(self, base: str) -> str:
        code = base
        n = 1
        while await self.s.scalar(select(CalcFormula.id).where(CalcFormula.code == code)):
            n += 1
            code = f"{base}_{n}"
        return code


def _as_date(iso: str):
    from datetime import date

    if isinstance(iso, date):
        return iso
    return date.fromisoformat(iso)
